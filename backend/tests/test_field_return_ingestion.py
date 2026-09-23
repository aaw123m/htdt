from __future__ import annotations

import json
from pathlib import Path

import pytest

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
