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


def _repository(tmp_path: Path) -> CadFieldEvidenceRepository:
    return CadFieldEvidenceRepository(
        SceneRepository(tmp_path / 'scene.sqlite3')
    )


def _record(asset: FieldEvidenceAsset | None = None, **kwargs):
    return build_field_evidence(
        document_id='doc-1',
        kind=kwargs.pop('kind', 'installation_photo'),
        targets=(
            EvidenceTarget(
                kind='scene_entity',
                revision_id='rev-1',
                entity_id='speaker-fl',
            ),
        ),
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
            document_id='doc-1',
            kind='as_built_note',
            targets=(),
            provenance='test',
            text='empty targets rejected',
            created_at_utc='2026-09-24T00:00:00+00:00',
        )
    with pytest.raises(ValueError):
        build_field_evidence(
            document_id='doc-1',
            kind='as_built_note',
            targets=(
                EvidenceTarget(kind='scene_revision', revision_id='rev-1'),
            ),
            provenance='test',
            created_at_utc='2026-09-24T00:00:00+00:00',
        )
    with pytest.raises(ValueError):
        EvidenceTarget(kind='scene_entity', entity_id='speaker-fl')


def test_field_evidence_asset_roundtrip_and_binding(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    payload = b'\x89PNG fake-image-bytes'
    asset_sha = repository.store_asset(payload)
    assert repository.read_asset(asset_sha) == payload

    record = _record(
        asset=FieldEvidenceAsset(
            asset_sha256=asset_sha,
            media_kind='image',
            filename='speaker-fl-mounted.jpg',
            mime_type='image/jpeg',
            byte_length=len(payload),
        )
    )
    repository.save_evidence(record)
    assert repository.get_evidence(record.evidence_id) == record
    with pytest.raises(FieldEvidenceConflictError):
        repository.save_evidence(record)

    found = repository.evidence_for_entity('rev-1', 'speaker-fl')
    assert [item.evidence_id for item in found] == [record.evidence_id]
    found = repository.evidence_for_revision('rev-1')
    assert [item.evidence_id for item in found] == [record.evidence_id]
    assert repository.evidence_for_revision('rev-2') == ()


def test_field_evidence_missing_asset_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    record = _record(
        asset=FieldEvidenceAsset(
            asset_sha256='f' * 64,
            media_kind='image',
            byte_length=4,
        )
    )
    with pytest.raises(ValueError):
        repository.save_evidence(record)


def test_field_evidence_text_note_binds_without_asset(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    record = _record(
        kind='as_built_note',
        text='Speaker FL mounted 3cm left of plan mark',
    )
    repository.save_evidence(record)
    assert repository.get_evidence(record.evidence_id).asset is None
    assert repository.list_evidence('doc-1')[0].kind == 'as_built_note'


def test_field_evidence_records_are_immutable_hashes() -> None:
    record = _record(text='note')
    assert len(record.evidence_sha256) == 64
    restored = type(record).model_validate_json(record.model_dump_json())
    assert restored == record
