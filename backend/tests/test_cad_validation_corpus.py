from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_validation_corpus import (
    CorpusSourceTopology,
    CorpusUncertaintyRecord,
    ValidationBenchmarkSpec,
    ValidationCorpusEntry,
    build_benchmark_spec,
    build_corpus_entry,
    split_role_is_scorable,
)
from htdt.cad_validation_corpus_repository import (
    CadValidationCorpusRepository,
    ValidationCorpusError,
)


def _scene(document_id: str) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='Front Left',
                speaker_role='FL',
                position=Position3(x_m=1.35, y_m=0.75, z_m=1.05),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
            ),
        ),
    )


def _setup(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _scene('doc-corpus'), parent_revision_id=None
    ).revision
    repository = CadValidationCorpusRepository(scene_repository)
    return repository, revision


def _entry(document_id: str, revision, **overrides) -> ValidationCorpusEntry:
    kwargs: dict = {
        'document_id': document_id,
        'split_role': 'calibration',
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'system_variant_id': 'variant-1',
        'system_variant_sha256': sha256(b'variant-1').hexdigest(),
        'receiver_identity': 'receiver-mlp',
        'evidence_sha256s': (sha256(b'fr-raw').hexdigest(),),
        'provider_id': 'htdt-roomsolver',
        'provider_model_version': 'r160-hybrid-1',
        'observables': ('magnitude_fr',),
        'geometry_classes': ('rectangular',),
        'source_topology': CorpusSourceTopology(
            topology='stereo_pair',
            source_count=2,
            bass_management='none',
            dsp_state='raw',
        ),
        'created_at_utc': '2026-01-01T00:00:00+00:00',
    }
    kwargs.update(overrides)
    return build_corpus_entry(**kwargs)


def _spec(**overrides) -> ValidationBenchmarkSpec:
    kwargs: dict = {
        'provider_id': 'htdt-roomsolver',
        'provider_model_version': 'r160-hybrid-1',
        'numerical_settings_json': '{"frequency_hz":[20,200]}',
        'observable': 'magnitude_fr',
        'metric': 'log_magnitude_residual',
        'metric_version': 'lmr-1',
        'tolerance_json': '{"max_db":3.0}',
        'created_at_utc': '2026-01-02T00:00:00+00:00',
    }
    kwargs.update(overrides)
    return build_benchmark_spec(**kwargs)


def test_corpus_entry_round_trip_and_hash(tmp_path: Path) -> None:
    repository, revision = _setup(tmp_path)
    entry = _entry(revision.document_id, revision)
    assert len(entry.corpus_entry_sha256) == 64

    repository.save_entry(entry)
    assert repository.get_entry(entry.corpus_entry_id) == entry
    assert repository.find_entry_by_sha256(entry.corpus_entry_sha256) == entry
    assert repository.list_entries(revision.document_id) == (entry,)
    assert repository.get_entry('missing') is None


def test_corpus_entry_append_only(tmp_path: Path) -> None:
    repository, revision = _setup(tmp_path)
    entry = _entry(revision.document_id, revision)
    repository.save_entry(entry)
    with pytest.raises(ValidationCorpusError):
        repository.save_entry(entry)


def test_corpus_entry_semantic_sha_must_be_unique(tmp_path: Path) -> None:
    repository, revision = _setup(tmp_path)
    entry = _entry(
        revision.document_id, revision, corpus_entry_id='entry-fixed'
    )
    repository.save_entry(entry)
    # An identical rebuild resolves to the same semantic sha.
    rebuild = _entry(
        revision.document_id, revision, corpus_entry_id='entry-fixed'
    )
    assert rebuild.corpus_entry_sha256 == entry.corpus_entry_sha256
    with pytest.raises(ValidationCorpusError):
        repository.save_entry(rebuild)


def test_entry_id_changes_the_semantic_sha(tmp_path: Path) -> None:
    _, revision = _setup(tmp_path)
    first = _entry('doc', revision, corpus_entry_id='entry-a')
    second = _entry('doc', revision, corpus_entry_id='entry-b')
    assert first.corpus_entry_sha256 != second.corpus_entry_sha256


def test_list_entries_cross_project_scope(tmp_path: Path) -> None:
    repository, revision = _setup(tmp_path)
    entry_a = _entry(revision.document_id, revision)
    entry_b = _entry('doc-other', revision)
    repository.save_entry(entry_a)
    repository.save_entry(entry_b)
    assert {e.corpus_entry_id for e in repository.list_entries(None)} == {
        entry_a.corpus_entry_id,
        entry_b.corpus_entry_id,
    }
    assert repository.list_entries(revision.document_id) == (entry_a,)


def test_corpora_entry_validator_mask_pairs(tmp_path: Path) -> None:
    _, revision = _setup(tmp_path)
    with pytest.raises(ValueError, match='frequency_mask_hz'):
        _entry('doc', revision, frequency_mask_hz=(200.0, 20.0))
    with pytest.raises(ValueError, match='time_mask_s'):
        _entry('doc', revision, time_mask_s=(1.0, 0.5))


def test_corpora_entry_ineligible_not_public(tmp_path: Path) -> None:
    _, revision = _setup(tmp_path)
    with pytest.raises(ValueError, match='ineligible'):
        _entry(
            'doc',
            revision,
            split_role='ineligible',
            license_classification='public',
        )
    entry = _entry('doc', revision, split_role='ineligible')
    assert entry.split_role == 'ineligible'


def test_uncertainty_none_is_unknown_not_zero(tmp_path: Path) -> None:
    _, revision = _setup(tmp_path)
    record = CorpusUncertaintyRecord(source='repeatability', repeat_count=5)
    assert record.standard_uncertainty is None
    entry = _entry('doc', revision, uncertainty_records=(record,))
    assert entry.uncertainty_records[0].standard_uncertainty is None


def test_context_id_and_hash_supplied_together(tmp_path: Path) -> None:
    _, revision = _setup(tmp_path)
    with pytest.raises(ValueError, match='acquisition context'):
        _entry('doc', revision, acquisition_context_id='ctx-1')
    entry = _entry(
        'doc',
        revision,
        acquisition_context_id='ctx-1',
        acquisition_context_sha256=sha256(b'ctx').hexdigest(),
    )
    assert entry.acquisition_context_id == 'ctx-1'


def test_benchmark_spec_round_trip(tmp_path: Path) -> None:
    repository, _ = _setup(tmp_path)
    spec = _spec(frequency_mask_hz=(20.0, 200.0))
    repository.save_benchmark_spec(spec)
    assert repository.get_benchmark_spec(spec.benchmark_spec_id) == spec
    assert (
        repository.find_benchmark_spec_by_sha256(spec.benchmark_spec_sha256)
        == spec
    )
    assert repository.list_benchmark_specs() == (spec,)
    with pytest.raises(ValidationCorpusError):
        repository.save_benchmark_spec(spec)


def test_benchmark_spec_hash_binds_payload() -> None:
    spec = _spec()
    assert len(spec.benchmark_spec_sha256) == 64
    tampered = spec.model_dump()
    tampered['metric'] = 'arrival_time_error'
    with pytest.raises(ValueError, match='hash mismatch'):
        ValidationBenchmarkSpec(**tampered)


def test_split_role_scorable() -> None:
    assert split_role_is_scorable('calibration')
    assert split_role_is_scorable('holdout')
    assert not split_role_is_scorable('ineligible')
