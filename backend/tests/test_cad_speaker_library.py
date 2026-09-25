"""#772 curated speaker / source reference library tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.cad_speaker_library import (
    BUILTIN_SPEAKER_LIBRARY,
    build_speaker_dataset,
    build_speaker_definition,
    on_axis_response_payload,
)
from htdt.cad_speaker_library_repository import (
    CadSpeakerLibraryRepository,
    SpeakerLibraryConflictError,
)

NOW = '2026-09-23T00:00:00+00:00'


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    return scene_repository, revision, CadSpeakerLibraryRepository(
        scene_repository
    )


def _speaker(document_id=None, **over):
    payload = {
        'model': 'Test speaker',
        'configuration': 'bookshelf',
        'created_at_utc': NOW,
        'document_id': document_id,
    }
    payload.update(over)
    return build_speaker_definition(**payload)


def _on_axis_dataset(speaker_id, **over):
    payload = {
        'speaker_id': speaker_id,
        'kind': 'on_axis_frequency_response',
        'payload_kind': 'inline',
        'payload_json': on_axis_response_payload(
            frequency_hz=(80.0, 160.0, 315.0, 630.0),
            level_db=(-3.0, -1.0, 0.0, 0.0),
        ),
        'provenance_class': 'user_measured',
        'created_at_utc': NOW,
    }
    payload.update(over)
    return build_speaker_dataset(**payload)


def test_definition_and_datasets_are_separate(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    speaker = _speaker(document_id=revision.document_id)
    repository.save_speaker(speaker)
    on_axis = _on_axis_dataset(speaker.speaker_id)
    repository.save_dataset(on_axis)
    impedance = build_speaker_dataset(
        speaker_id=speaker.speaker_id,
        kind='impedance_curve',
        payload_kind='inline',
        payload_json=json.dumps(
            {
                'frequency_hz': [80.0, 160.0],
                'magnitude_ohm': [8.0, 7.2],
                'phase_deg': [0.0, 5.0],
            }
        ),
        provenance_class='laboratory_measured',
        created_at_utc=NOW,
    )
    repository.save_dataset(impedance)
    kinds = {
        item.kind for item in repository.list_datasets(speaker.speaker_id)
    }
    assert kinds == {'on_axis_frequency_response', 'impedance_curve'}


def test_inline_payload_must_match_declared_kind(tmp_path: Path) -> None:
    speaker = _speaker(document_id=None)
    # FR table passed as 'directivity' -> rejected (no silent widening).
    bad_directivity = dict(
        speaker_id=speaker.speaker_id,
        kind='directivity',
        payload_kind='inline',
        payload_json=on_axis_response_payload(
            frequency_hz=(80.0,), level_db=(0.0,)
        ),
        provenance_class='generic_reference_preset',
        created_at_utc=NOW,
    )
    with pytest.raises(ValidationError, match='directivity'):
        build_speaker_dataset(**bad_directivity)
    # Unparseable inline JSON -> rejected.
    with pytest.raises(ValidationError, match='valid JSON'):
        build_speaker_dataset(
            speaker_id=speaker.speaker_id,
            kind='impedance_curve',
            payload_kind='inline',
            payload_json='{not json',
            provenance_class='user_measured',
            created_at_utc=NOW,
        )


def test_external_payload_contract(tmp_path: Path) -> None:
    speaker = _speaker(document_id=None)
    external = build_speaker_dataset(
        speaker_id=speaker.speaker_id,
        kind='directivity',
        payload_kind='external',
        asset_uri='file:///C:/data/spinorama.json',
        asset_sha256='a' * 64,
        provenance_class='independent_published',
        license_name='CC-BY-SA-4.0',
        redistribution_permitted=True,
        created_at_utc=NOW,
    )
    assert external.asset_uri == 'file:///C:/data/spinorama.json'
    # External payload cannot carry inline bytes; inline cannot carry a URI.
    with pytest.raises(ValidationError):
        build_speaker_dataset(
            speaker_id=speaker.speaker_id,
            kind='other',
            payload_kind='external',
            payload_json='{}',
            asset_uri='file:///x',
            provenance_class='user_measured',
            created_at_utc=NOW,
        )
    with pytest.raises(ValidationError):
        build_speaker_dataset(
            speaker_id=speaker.speaker_id,
            kind='other',
            payload_kind='external',
            provenance_class='user_measured',
            created_at_utc=NOW,
        )


def test_shared_datasets_require_redistribution_license(tmp_path: Path) -> None:
    _scenes, _rev, repository = _repositories(tmp_path)
    shared = _speaker(document_id=None)
    repository.save_speaker(shared)
    with pytest.raises(ValueError, match='redistribution'):
        repository.save_dataset(_on_axis_dataset(shared.speaker_id))
    repository.save_dataset(
        _on_axis_dataset(
            shared.speaker_id,
            redistribution_permitted=True,
            license_name='CC0-1.0',
        )
    )


def test_datasets_require_persisted_speaker(tmp_path: Path) -> None:
    _scenes, _rev, repository = _repositories(tmp_path)
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_dataset(_on_axis_dataset('ghost-speaker'))


def test_datasets_append_only_per_version_kind(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    speaker = _speaker(document_id=revision.document_id)
    repository.save_speaker(speaker)
    repository.save_dataset(_on_axis_dataset(speaker.speaker_id))
    with pytest.raises(SpeakerLibraryConflictError):
        repository.save_dataset(_on_axis_dataset(speaker.speaker_id))
    repository.save_dataset(_on_axis_dataset(speaker.speaker_id, version='2'))
    assert len(repository.list_datasets(speaker.speaker_id)) == 2


def test_builtin_library_seeds_idempotently(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    installed = repository.install_builtin_library()
    assert installed == len(BUILTIN_SPEAKER_LIBRARY)
    assert repository.install_builtin_library() == 0
    speakers = repository.list_speakers(revision.document_id)
    assert len(speakers) == len(BUILTIN_SPEAKER_LIBRARY)
    for speaker, _dataset in BUILTIN_SPEAKER_LIBRARY:
        stored = repository.get_speaker(speaker.speaker_id)
        assert stored == speaker
        datasets = repository.list_datasets(speaker.speaker_id)
        assert datasets[0].provenance_class == 'generic_reference_preset'
        assert datasets[0].redistribution_permitted is True
        assert datasets[0].limitations
        # Directivity is not implied by an on-axis response.
        assert (
            repository.list_datasets(speaker.speaker_id, kind='directivity')
            == ()
        )


def test_speaker_hash_integrity(tmp_path: Path) -> None:
    speaker = _speaker(document_id=None)
    payload = speaker.model_dump(mode='python')
    payload['model'] = 'Tampered'
    with pytest.raises(ValidationError, match='hash mismatch'):
        type(speaker)(**payload)
