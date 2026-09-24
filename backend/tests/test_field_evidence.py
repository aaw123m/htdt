from pathlib import Path

import pytest

from htdt.cad_field_evidence import (
    EvidenceTarget,
    FieldEvidenceAsset,
    build_field_evidence,
)
from htdt.cad_field_evidence_repository import (
    CadFieldEvidenceRepository,
    FieldEvidenceConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene


DOC = F1_DOCUMENT_ID


def _seed(tmp_path: Path) -> tuple[SceneRepository, object]:
    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    )
    return scene_repository, saved


def _repository(tmp_path: Path) -> tuple[
    CadFieldEvidenceRepository, object
]:
    scene_repository, saved = _seed(tmp_path)
    return CadFieldEvidenceRepository(scene_repository), saved


def _target(revision, entity_id='speaker-fl') -> EvidenceTarget:
    return EvidenceTarget(
        kind='scene_entity',
        revision_id=revision.revision_id,
        entity_id=entity_id,
        ref_sha256=revision.content_hash,
    )


def _record(revision, asset=None, **kwargs):
    return build_field_evidence(
        document_id=kwargs.pop('document_id', DOC),
        kind=kwargs.pop('kind', 'installation_photo'),
        targets=kwargs.pop('targets', (_target(revision),)),
        provenance='installer phone upload',
        asset=asset,
        text=kwargs.pop('text', None),
        captured_at_utc='2026-09-24T00:00:00+00:00',
        observer='installer-a',
        created_at_utc='2026-09-24T00:00:00+00:00',
        **kwargs,
    )


def test_field_evidence_requires_targets_and_content() -> None:
    with pytest.raises(ValueError):
        build_field_evidence(
            document_id=DOC,
            kind='as_built_note',
            targets=(),
            provenance='test',
            text='empty targets rejected',
            created_at_utc='2026-09-24T00:00:00+00:00',
        )
    with pytest.raises(ValueError):
        build_field_evidence(
            document_id=DOC,
            kind='as_built_note',
            targets=(
                EvidenceTarget(kind='scene_revision', revision_id='rev-1'),
            ),
            provenance='test',
            created_at_utc='2026-09-24T00:00:00+00:00',
        )
    with pytest.raises(ValueError):
        EvidenceTarget(kind='scene_entity', entity_id='speaker-fl')
    with pytest.raises(ValueError):
        EvidenceTarget(kind='other', ref_id='thing')
    with pytest.raises(ValueError):
        EvidenceTarget(kind='installation_section', ref_id='bogus')


def test_field_evidence_asset_roundtrip_and_binding(tmp_path: Path) -> None:
    repository, saved = _repository(tmp_path)
    payload = b'\x89PNG fake-image-bytes'
    asset_sha = repository.store_asset(payload)
    assert repository.read_asset(asset_sha) == payload

    record = _record(
        saved.revision,
        asset=FieldEvidenceAsset(
            asset_sha256=asset_sha,
            media_kind='image',
            filename='speaker-fl-mounted.jpg',
            mime_type='image/jpeg',
            byte_length=len(payload),
        ),
    )
    repository.save_evidence(record)
    assert repository.get_evidence(record.evidence_id) == record
    with pytest.raises(FieldEvidenceConflictError):
        repository.save_evidence(record)

    found = repository.evidence_for_entity(
        saved.revision.revision_id,
        'speaker-fl',
        document_id=DOC,
    )
    assert [item.evidence_id for item in found] == [record.evidence_id]
    found = repository.evidence_for_revision(
        saved.revision.revision_id, document_id=DOC
    )
    assert [item.evidence_id for item in found] == [record.evidence_id]
    assert repository.evidence_for_revision('rev-2') == ()


def test_field_evidence_missing_target_fails_before_commit(
    tmp_path: Path,
) -> None:
    repository, saved = _repository(tmp_path)
    record = _record(
        saved.revision,
        targets=(
            EvidenceTarget(
                kind='scene_entity',
                revision_id=saved.revision.revision_id,
                entity_id='ghost-entity',
                ref_sha256=saved.revision.content_hash,
            ),
        ),
        text='entity does not exist in the pinned revision',
    )
    with pytest.raises(ValueError):
        repository.save_evidence(record)
    assert repository.list_evidence(DOC) == ()


def test_field_evidence_rejects_foreign_document_target(
    tmp_path: Path,
) -> None:
    repository, saved = _repository(tmp_path)
    record = _record(
        saved.revision,
        document_id='other-doc',
        text='revision belongs to fixture-f1, not other-doc',
    )
    with pytest.raises(ValueError):
        repository.save_evidence(record)


def test_field_evidence_historical_revision_pin_stays_valid(
    tmp_path: Path,
) -> None:
    scene_repository, saved = _seed(tmp_path)
    repository = CadFieldEvidenceRepository(scene_repository)
    later = scene_repository.save(
        saved.revision.document.model_copy(
            update={'entities': saved.revision.document.entities[:-1]}
        ),
        parent_revision_id=saved.revision.revision_id,
    )
    record = _record(saved.revision, text='pinned to historical rev')
    repository.save_evidence(record)
    assert repository.get_evidence(record.evidence_id) == record
    found = repository.evidence_for_revision(
        later.revision.revision_id, document_id=DOC
    )
    assert found == ()


def test_field_evidence_byte_length_mismatch_fails(tmp_path: Path) -> None:
    repository, saved = _repository(tmp_path)
    payload = b'image-bytes'
    asset_sha = repository.store_asset(payload)
    record = _record(
        saved.revision,
        asset=FieldEvidenceAsset(
            asset_sha256=asset_sha,
            media_kind='image',
            byte_length=len(payload) + 10,
        ),
    )
    with pytest.raises(ValueError):
        repository.save_evidence(record)


def test_field_evidence_missing_asset_fails_closed(tmp_path: Path) -> None:
    repository, saved = _repository(tmp_path)
    record = _record(
        saved.revision,
        asset=FieldEvidenceAsset(
            asset_sha256='f' * 64,
            media_kind='image',
            byte_length=4,
        ),
    )
    with pytest.raises(ValueError):
        repository.save_evidence(record)


def test_field_evidence_text_note_binds_without_asset(tmp_path: Path) -> None:
    repository, saved = _repository(tmp_path)
    record = _record(
        saved.revision,
        kind='as_built_note',
        text='Speaker FL mounted 3cm left of plan mark',
    )
    repository.save_evidence(record)
    assert repository.get_evidence(record.evidence_id).asset is None
    assert repository.list_evidence(DOC)[0].kind == 'as_built_note'


def test_field_evidence_records_are_immutable_hashes(tmp_path: Path) -> None:
    _scene_repository, saved = _seed(tmp_path)
    record = _record(saved.revision, text='note')
    assert len(record.evidence_sha256) == 64
    restored = type(record).model_validate_json(record.model_dump_json())
    assert restored == record
