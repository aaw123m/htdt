from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path

import pytest

from htdt.cad_schema import connect_sqlite
from htdt.field_return_ingestion import (
    FieldReturnConflictError,
    FieldReturnError,
    FieldReturnManifest,
    FieldReturnRepository,
    classify_contribution_duplicate,
    classify_inbound_document,
    stage_field_return,
    validate_field_return,
    verify_task_fulfillment,
)
from htdt.project_identity import (
    duplicate_project_reference,
    new_project_reference,
)


RECORD_ID = '10000000-0000-4000-8000-000000000010'
TASK_ID = '20000000-0000-4000-8000-000000000020'
MISSION_ID = '30000000-0000-4000-8000-000000000030'
CONTRIBUTION_ID = '40000000-0000-4000-8000-000000000040'


def _manifest_dict(**overrides) -> dict:
    manifest = {
        'schema': 'htdt.field-return',
        'schema_version': 1,
        'contribution_id': CONTRIBUTION_ID,
        'mission_id': MISSION_ID,
        'plan_sha256': 'a' * 64,
        'project': new_project_reference(
            document_id='doc-1', room_id='room-1'
        ).model_dump(mode='json'),
        'authority_binding_scope': 'contribution_id',
        'records': [
            {
                'record_id': RECORD_ID,
                'kind': 'equipment_identity',
                'subject_ref': 'speaker:spk-fl',
                'payload_sha256': 'b' * 64,
                'evidence_refs': ['photo:abc'],
                'method': 'visual_inspection',
            }
        ],
        'task_outcomes': [
            {
                'task_id': TASK_ID,
                'outcome': 'fulfilled',
                'fulfilled_by_ref': RECORD_ID,
            }
        ],
    }
    manifest.update(overrides)
    return manifest


def test_inbound_classification_by_declared_schema() -> None:
    assert classify_inbound_document({'schema': 'htdt.field-return'}) == 'field_return'
    assert classify_inbound_document({'schema': 'htdt.capture.bundle'}) == 'capture_bundle'
    assert classify_inbound_document({'schema': 'htdt.capture.mission-package'}) == 'mission_package'
    assert classify_inbound_document({'schema': 'htdt.equipment.catalog-snapshot'}) == 'equipment_catalog_snapshot'
    assert classify_inbound_document({'schema': 'something.else'}) == 'unsupported'
    assert classify_inbound_document('raw-string') == 'unsupported'
    assert classify_inbound_document({}) == 'unsupported'


def test_valid_manifest_validates_typed_semantics() -> None:
    manifest = validate_field_return(_manifest_dict())
    assert isinstance(manifest, FieldReturnManifest)
    assert manifest.contribution_id == CONTRIBUTION_ID
    assert manifest.authority_binding_scope == 'contribution_id'
    assert manifest.records[0].kind == 'equipment_identity'
    assert len(manifest.content_digest()) == 64


def test_validate_rejects_non_field_return_documents() -> None:
    with pytest.raises(FieldReturnError):
        validate_field_return({'schema': 'htdt.capture.bundle'})
    with pytest.raises(FieldReturnError):
        validate_field_return('raw')
    with pytest.raises(FieldReturnError):
        validate_field_return(
            _manifest_dict(schema_version=2)
        )


def test_validate_rejects_invalid_legacy_project_shape() -> None:
    document = _manifest_dict(project={'unexpected': 'shape'})
    with pytest.raises(FieldReturnError):
        validate_field_return(document)


def test_fulfillment_requires_resolvable_reference() -> None:
    # fulfilled outcome without a resolvable record ref -> unresolved.
    document = _manifest_dict()
    document['task_outcomes'] = [
        {
            'task_id': TASK_ID,
            'outcome': 'fulfilled',
            'fulfilled_by_ref': '99999999-0000-4000-8000-000000000099',
        }
    ]
    manifest = validate_field_return(document)
    outcomes = dict(verify_task_fulfillment(manifest))
    assert outcomes[TASK_ID] == 'unresolved_insufficient'

    resolved = validate_field_return(_manifest_dict())
    outcomes = dict(verify_task_fulfillment(resolved))
    assert outcomes[TASK_ID] == 'fulfilled'

    skipped = _manifest_dict()
    skipped['task_outcomes'] = [
        {'task_id': TASK_ID, 'outcome': 'skipped', 'fulfilled_by_ref': None}
    ]
    outcomes = dict(verify_task_fulfillment(validate_field_return(skipped)))
    assert outcomes[TASK_ID] == 'skipped'


def test_routing_classification_uses_exact_identity() -> None:
    known = new_project_reference(document_id='doc-1', room_id='room-1')
    document = _manifest_dict()
    document['project'] = known.model_dump(mode='json')
    staged = stage_field_return(
        json.dumps(document).encode('utf-8'), [known]
    )
    assert staged.validation_state == 'validated'
    assert staged.routing == 'exact_project_match'
    assert staged.matched_project_id == known.project_id
    assert staged.mission_id == MISSION_ID

    clone = duplicate_project_reference(known)
    document['project'] = clone.model_dump(mode='json')
    staged = stage_field_return(
        json.dumps(document).encode('utf-8'), [known]
    )
    assert staged.routing == 'known_project_lineage'

    stranger = new_project_reference(document_id='doc-1')
    document['project'] = stranger.model_dump(mode='json')
    staged = stage_field_return(
        json.dumps(document).encode('utf-8'), [known]
    )
    assert staged.routing == 'unknown_project_reference'
    assert staged.matched_project_id is None

    document['project'] = 'opaque-legacy-ref'
    staged = stage_field_return(
        json.dumps(document).encode('utf-8'), [known]
    )
    assert staged.routing == 'legacy_project_ref'

    del document['project']
    staged = stage_field_return(json.dumps(document).encode('utf-8'), [known])
    assert staged.routing == 'unrouted'


def test_unsupported_version_is_preserved_not_dropped() -> None:
    document = _manifest_dict(schema_version=99)
    artifact = json.dumps(document).encode('utf-8')
    staged = stage_field_return(artifact, [])
    assert staged.validation_state == 'unsupported'
    assert staged.contribution_id == CONTRIBUTION_ID
    assert staged.manifest_json is not None
    assert 'unsupported' in (staged.detail or '')


def test_malformed_artifact_stages_as_diagnostic() -> None:
    staged = stage_field_return(b'not-json{', [])
    assert staged.validation_state == 'malformed'
    assert staged.contribution_id is None

    staged = stage_field_return(
        json.dumps({'schema': 'htdt.capture.bundle'}).encode('utf-8'), []
    )
    assert staged.validation_state == 'malformed'


def test_repository_stages_and_handles_duplicates(tmp_path: Path) -> None:
    repository = FieldReturnRepository(tmp_path / 'field.sqlite3')
    artifact = json.dumps(_manifest_dict()).encode('utf-8')
    staged = repository.stage(artifact, [])
    assert staged.validation_state == 'validated'

    # Exact duplicate is an idempotent no-op.
    staged_again = repository.stage(artifact, [])
    assert staged_again.contribution_id == staged.contribution_id
    assert len(repository.list_staged()) == 1

    # Same identity, different bytes -> conflict.
    conflicting = _manifest_dict(records=[])
    with pytest.raises(FieldReturnConflictError):
        repository.stage(json.dumps(conflicting).encode('utf-8'), [])

    reopened = FieldReturnRepository(tmp_path / 'field.sqlite3')
    fetched = reopened.get(CONTRIBUTION_ID)
    assert fetched is not None
    assert fetched.artifact_sha256 == staged.artifact_sha256
    assert fetched.routing == staged.routing


def test_repository_retains_artifact_bytes(tmp_path: Path) -> None:
    repository = FieldReturnRepository(tmp_path / 'field.sqlite3')
    artifact = json.dumps(_manifest_dict()).encode('utf-8')
    staged = repository.stage(artifact, [])

    # Bytes survive independently of the staging row's lifetime.
    assert repository.artifact_bytes(CONTRIBUTION_ID) == artifact

    reopened = FieldReturnRepository(tmp_path / 'field.sqlite3')
    assert reopened.artifact_bytes(CONTRIBUTION_ID) == artifact
    assert reopened.get(CONTRIBUTION_ID).artifact_retained is True
    assert reopened.list_staged()[0].artifact_retained is True
    assert reopened.artifact_bytes('missing-contribution') is None


def test_redelivery_heals_a_pre_retention_row(tmp_path: Path) -> None:
    repository = FieldReturnRepository(tmp_path / 'field.sqlite3')
    artifact = json.dumps(_manifest_dict()).encode('utf-8')
    repository.stage(artifact, [])
    # Simulate a row staged before byte retention existed.
    with closing(connect_sqlite(tmp_path / 'field.sqlite3')) as connection:
        connection.execute('DELETE FROM htdt_content_blobs')
        connection.commit()
    assert repository.get(CONTRIBUTION_ID).artifact_retained is False

    staged, created = repository.stage_artifact(artifact, [])
    assert created is False
    assert repository.artifact_bytes(CONTRIBUTION_ID) == artifact
    assert repository.get(CONTRIBUTION_ID).artifact_retained is True


def test_duplicate_classification_is_deterministic() -> None:
    assert (
        classify_contribution_duplicate(
            existing_contribution_id='a',
            existing_artifact_sha256='x',
            incoming_contribution_id='a',
            incoming_artifact_sha256='x',
        )
        == 'exact_duplicate'
    )
    assert (
        classify_contribution_duplicate(
            existing_contribution_id='a',
            existing_artifact_sha256='x',
            incoming_contribution_id='a',
            incoming_artifact_sha256='y',
        )
        == 'identity_conflict'
    )
    assert (
        classify_contribution_duplicate(
            existing_contribution_id='a',
            existing_artifact_sha256='x',
            incoming_contribution_id='b',
            incoming_artifact_sha256='x',
        )
        == 'distinct'
    )


EVIDENCE_ID = '50000000-0000-4000-8000-000000000050'
EXTERNAL_ENTITY_ID = '60000000-0000-4000-8000-000000000060'
MISSING_ITEM_ID = '70000000-0000-4000-8000-000000000070'


def _container_with_refs() -> bytes:
    from test_capture_receiver_field_return import (
        _container,
        _doc_ref,
        _root_document,
    )

    from hashlib import sha256 as _sha256

    asset_payload = b'asset-bytes'
    evidence_payload = json.dumps(
        {
            'schema': 'htdt.field_return.field-evidence',
            'contribution_ref': {
                'kind': 'field_return',
                'id': CONTRIBUTION_ID,
            },
            'records': [
                {
                    'evidence_id': EVIDENCE_ID,
                    'channel_role': 'FL',
                    'note': 'wall position confirmed',
                }
            ],
        }
    ).encode('utf-8')
    root = _root_document(
        contribution_id=CONTRIBUTION_ID,
        task_fulfillment_ledger=[
            {
                'item_ref': 'task_item:t-alpha',
                'title': 'verify speaker placement',
                'requirement': 'recommended',
                'outcome': 'fulfilled',
                'fulfilled_by_refs': [
                    f'field_evidence:{EVIDENCE_ID}',
                    f'sha256:{_sha256(asset_payload).hexdigest()}',
                    f'entity:{EXTERNAL_ENTITY_ID}',
                    f'inventory_item:{MISSING_ITEM_ID}',
                ],
            },
            {
                'item_ref': 'task_item:t-beta',
                'title': 'ambient temperature',
                'requirement': 'optional',
                'outcome': 'unfulfilled',
                'fulfilled_by_refs': [],
            },
        ],
        authority_documents=[
            _doc_ref(
                'authority/field-evidence.json',
                'htdt.field_return.field-evidence',
                evidence_payload,
            )
        ],
        evidence_assets=[
            _doc_ref(
                'evidence/p1.bin',
                'application/octet-stream',
                asset_payload,
            )
        ],
    )
    return _container(
        {
            'field-return.json': json.dumps(root).encode('utf-8'),
            'authority/field-evidence.json': evidence_payload,
            'evidence/p1.bin': asset_payload,
        }
    )


def test_resolve_return_refs_container(tmp_path: Path) -> None:
    repository = FieldReturnRepository(tmp_path / 'field.sqlite3')
    artifact = _container_with_refs()
    staged = repository.stage(artifact, [])
    assert staged.contribution_id == CONTRIBUTION_ID

    tasks = repository.resolve_return_refs(CONTRIBUTION_ID)
    assert tasks is not None and len(tasks) == 2

    alpha, beta = tasks
    assert alpha.task_id == 't-alpha' and alpha.outcome == 'fulfilled'
    resolved, asset, external, unresolved = alpha.refs
    assert resolved.state == 'resolved'
    assert resolved.document_schema == 'htdt.field_return.field-evidence'
    assert resolved.document_path == 'authority/field-evidence.json'
    assert resolved.record['evidence_id'] == EVIDENCE_ID
    assert resolved.record['channel_role'] == 'FL'
    assert asset.state == 'evidence_asset'
    assert asset.asset_path == 'evidence/p1.bin'
    assert external.state == 'external'
    assert external.ref == f'entity:{EXTERNAL_ENTITY_ID}'
    assert unresolved.state == 'unresolved'
    assert unresolved.ref == f'inventory_item:{MISSING_ITEM_ID}'
    assert beta.task_id == 't-beta' and beta.refs == ()


def test_resolve_return_refs_flat_manifest(tmp_path: Path) -> None:
    repository = FieldReturnRepository(tmp_path / 'field.sqlite3')
    artifact = json.dumps(_manifest_dict()).encode('utf-8')
    repository.stage(artifact, [])

    tasks = repository.resolve_return_refs(CONTRIBUTION_ID)
    assert tasks is not None and len(tasks) == 1
    task = tasks[0]
    assert task.task_id == TASK_ID and task.outcome == 'fulfilled'
    (ref,) = task.refs
    assert ref.state == 'resolved'
    assert ref.record['record_id'] == RECORD_ID
    assert ref.record['kind'] == 'equipment_identity'


def test_resolve_return_refs_absent_artifact(tmp_path: Path) -> None:
    repository = FieldReturnRepository(tmp_path / 'field.sqlite3')
    assert repository.resolve_return_refs('missing-contribution') is None
    artifact = json.dumps(_manifest_dict()).encode('utf-8')
    repository.stage(artifact, [])
    with closing(connect_sqlite(tmp_path / 'field.sqlite3')) as connection:
        connection.execute('DELETE FROM htdt_content_blobs')
        connection.commit()
    assert repository.resolve_return_refs(CONTRIBUTION_ID) is None
