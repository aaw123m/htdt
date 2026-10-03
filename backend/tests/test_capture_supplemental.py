from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)


BUNDLE_DIGEST = '2' * 64
SERIES_ID = '10000000-0000-4000-8000-000000000001'
REVISION_ID = '10000000-0000-4000-8000-000000000002'
SESSION_ID = '10000000-0000-4000-8000-000000000003'
SPACE_ID = '10000000-0000-4000-8000-000000000004'
REGION_ID = '10000000-0000-4000-8000-000000000010'


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')


def _hash_parts(prefix: str, *parts: str) -> str:
    digest = sha256(prefix.encode('utf-8'))
    for part in parts:
        digest.update(b'\x00')
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def _source_record(path: str, payload: bytes, producer: str = 'supplemental_capture') -> dict:
    digest = sha256(payload).hexdigest()
    return {
        'source_evidence_id': _hash_parts(
            'htdt.capture.source-evidence.v1',
            BUNDLE_DIGEST,
            path,
            digest,
        ),
        'bundle_digest': BUNDLE_DIGEST,
        'capture_revision_id': REVISION_ID,
        'path': path,
        'payload_sha256': digest,
        'bytes': len(payload),
        'media_type': 'application/json',
        'producer': producer,
        'provenance_class': 'capture_app_derived',
        'role': 'canonical',
        'source_refs': [],
    }


def _supplemental(
    source: dict,
    *,
    bundle_digest: str = BUNDLE_DIGEST,
    kind: str,
    state: str,
    schema: str,
    version: str,
    coordinate_space_ids=(),
    capture_session_ids=(),
    plan_payload_sha256=None,
) -> dict:
    path = source['path']
    payload_sha = source['payload_sha256']
    return {
        'supplemental_document_handoff_id': _hash_parts(
            'htdt.capture.supplemental-document.v1',
            bundle_digest,
            path,
            payload_sha,
        ),
        'document_kind': kind,
        'validation_state': state,
        'schema': schema,
        'schema_version': version,
        'path': path,
        'source_evidence_id': source['source_evidence_id'],
        'source_payload_sha256': payload_sha,
        'coordinate_space_ids': list(coordinate_space_ids),
        'capture_session_ids': list(capture_session_ids),
        'plan_payload_sha256': plan_payload_sha256,
    }


def _plan(
    source: list[dict],
    supplemental: list[dict],
) -> dict:
    projection = {
        'bundle_digest': BUNDLE_DIGEST,
        'source_evidence_ids': sorted(
            item['source_evidence_id'] for item in source
        ),
        'raw_visual_mesh_ids': [],
        'authority_record_ids': [],
    }
    if supplemental:
        projection['supplemental_document_ids'] = sorted(
            item['supplemental_document_handoff_id'] for item in supplemental
        )
    lineage_digest = sha256(_canonical(projection)).hexdigest()
    return {
        'schema': 'htdt.capture.ingestion-plan',
        'schema_version': '1.0.0',
        'ingestor': {
            'name': 'htdt-capture-reference-ingestor',
            'version': '1.0.0',
            'configuration_digest': (
                '3e27eec298714a04fc6b48d94b354168396e2c4eea0cf9aa8284fa552de562b3'
            ),
        },
        'bundle': {
            'bundle_digest': BUNDLE_DIGEST,
            'capture_schema': 'htdt.capture.bundle',
            'capture_schema_version': '1.0.0',
            'capture_series_id': SERIES_ID,
            'capture_revision_id': REVISION_ID,
            'parent_revision_id': None,
            'capture_session_ids': [SESSION_ID],
            'coordinate_space_ids': [SPACE_ID],
        },
        'source_evidence': source,
        'roomplan_records': [],
        'raw_visual_mesh_handoffs': [],
        'authority_records': [],
        'supplemental_documents': supplemental,
        'lineage_digest': lineage_digest,
    }


SUPPLEMENTAL_DOCS: dict[str, dict] = {
    'session/capture-task-plan.json': {
        'schema': 'htdt.capture-task-plan',
        'schema_version': '1.0.0',
        'plan_id': 'mission-plan-1',
        'plan_version': '1',
        'project_ref': 'project-1',
        'room_name': 'Theater',
        'entity_checklist': [],
        'measurement_requests': [],
        'surface_review_tasks': [],
        'evidence_targets': [],
        'expected_channel_roles': [],
    },
    'session/connected-spaces.json': {
        'schema': 'htdt.capture.connected-spaces',
        'schema_version': '1.0.0',
        'capture_revision_id': support.REVISION_ID,
        'capture_session_id': support.SESSION_ID,
        'coordinate_space_id': support.SPACE_ID,
        'segments': [
            {
                'region_id': REGION_ID,
                'label': 'Theater',
                'kind': 'room',
                'state': 'active',
                'coordinate_space_id': support.SPACE_ID,
                'capture_session_id': support.SESSION_ID,
                'evidence_refs': [],
                'revisit_count': 0,
            }
        ],
        'portals': [],
    },
    'evidence/reference-targets.json': {
        'schema': 'htdt.capture.reference-targets',
        'schema_version': '1.0.0',
        'capture_revision_id': support.REVISION_ID,
        'capture_session_id': support.SESSION_ID,
        'targets': [],
        'observations': [],
        'diagnostics': [],
    },
    'derived/geometry-candidates.json': {
        'schema': 'htdt.capture.derived-geometry-candidates',
        'schema_version': '1.0.0',
        'capture_revision_id': support.REVISION_ID,
        'capture_session_id': support.SESSION_ID,
        'candidates': [],
    },
    'verification/as-built.json': {
        'schema': 'htdt.capture.as-built-verification',
        'schema_version': '1.0.0',
        'plan_id': 'mission-plan-1',
        'plan_version': '1',
        'capture_revision_id': support.REVISION_ID,
        'capture_session_id': support.SESSION_ID,
        'plan_sha256': 'f' * 64,
        'items': [],
    },
}

SUPPLEMENTAL_KINDS = {
    'session/capture-task-plan.json': 'capture_task_plan',
    'session/connected-spaces.json': 'connected_spaces',
    'evidence/reference-targets.json': 'reference_targets',
    'derived/geometry-candidates.json': 'derived_geometry_candidates',
    'verification/as-built.json': 'as_built_verification',
}


def _file_spec(path: str, document: dict) -> dict:
    return {
        'bytes': _canonical(document),
        'media_type': 'application/json',
        'producer': 'htdt_mission',
        'provenance_class': 'capture_app_derived',
        'role': 'canonical',
    }


def _attach_supplemental(plan: dict, documents: list[dict]) -> dict:
    """Recompute lineage_digest over a plan extended with documents."""
    plan = dict(plan)
    plan['supplemental_documents'] = documents
    projection = {
        'bundle_digest': plan['bundle']['bundle_digest'],
        'source_evidence_ids': sorted(
            item['source_evidence_id'] for item in plan['source_evidence']
        ),
        'raw_visual_mesh_ids': sorted(
            item['raw_visual_mesh_handoff_id']
            for item in plan['raw_visual_mesh_handoffs']
        ),
        'authority_record_ids': sorted(
            item['authority_record_handoff_id']
            for item in plan['authority_records']
        ),
    }
    if documents:
        projection['supplemental_document_ids'] = sorted(
            item['supplemental_document_handoff_id'] for item in documents
        )
    plan['lineage_digest'] = sha256(_canonical(projection)).hexdigest()
    return plan


def _supplemental_for(plan: dict, path: str, **kwargs) -> dict:
    source = next(
        item for item in plan['source_evidence'] if item['path'] == path
    )
    return _supplemental(
        source,
        bundle_digest=plan['bundle']['bundle_digest'],
        **kwargs,
    )


def test_supported_supplemental_documents_ingest_and_reopen(
    tmp_path: Path,
) -> None:
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    repository = CaptureIngestionRepository(scene)

    files = support.default_file_specs()
    files.update(
        {path: _file_spec(path, doc) for path, doc in SUPPLEMENTAL_DOCS.items()}
    )
    plan, payloads, _manifest = support.plan_and_payloads(
        tmp_path / 'bundle', files=files
    )
    # build_ingestion_plan now emits the typed handoffs itself — the
    # plan already carries one supported document per declared path.
    assert len(plan['supplemental_documents']) == 5

    result = repository.ingest(plan, payloads)
    assert result.created
    assert result.supplemental_document_count == 5

    typed = CaptureIngestionPlan.model_validate(plan)
    for document in typed.supplemental_documents:
        persisted = repository.get_supplemental_document(
            typed.lineage_digest,
            document.supplemental_document_handoff_id,
        )
        assert persisted == document

    # Re-import with identical plan is an idempotent no-op.
    again = repository.ingest(plan, payloads)
    assert not again.created
    assert again.supplemental_document_count == 5


def test_unknown_document_kind_is_preserved_as_unsupported(
    tmp_path: Path,
) -> None:
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    repository = CaptureIngestionRepository(scene)
    files = support.default_file_specs()
    # A future opaque payload: schema-owned JSON must match the published
    # contract, so an unknown kind arrives as raw non-JSON bytes.
    files['future/new-doc.bin'] = {
        'bytes': b'\x00\x01future-document-bytes',
        'media_type': 'application/octet-stream',
        'producer': 'future_capture',
        'provenance_class': 'capture_app_derived',
        'role': 'canonical',
    }
    plan, payloads, _manifest = support.plan_and_payloads(
        tmp_path / 'bundle', files=files
    )
    document = _supplemental_for(
        plan,
        'future/new-doc.bin',
        kind='semantic_promotion_v2',
        state='unsupported',
        schema='htdt.capture.semantic-promotion',
        version='2',
    )
    plan = _attach_supplemental(plan, [document])
    result = repository.ingest(plan, payloads)
    assert result.supplemental_document_count == 1
    persisted = repository.get_supplemental_document(
        plan['lineage_digest'],
        document['supplemental_document_handoff_id'],
    )
    assert persisted is not None
    assert persisted.validation_state == 'unsupported'
    assert persisted.document_kind == 'semantic_promotion_v2'


def test_plan_without_supplemental_uses_original_lineage() -> None:
    # Older plans (no supplemental set) keep their historical lineage digest —
    # the projection does not gain a supplemental key when empty.
    source = _source_record('roomplan/captured.json', b'{"r":1}')
    plan = _plan([source], [])
    typed = CaptureIngestionPlan.model_validate(plan)
    projection = {
        'bundle_digest': BUNDLE_DIGEST,
        'source_evidence_ids': sorted(
            item['source_evidence_id'] for item in plan['source_evidence']
        ),
        'raw_visual_mesh_ids': [],
        'authority_record_ids': [],
    }
    expected = sha256(_canonical(projection)).hexdigest()
    assert typed.lineage_digest == expected
    assert typed.supplemental_documents == ()


def test_supplemental_requires_matching_source_evidence() -> None:
    source = _source_record('session/capture-task-plan.json', b'{"plan":1}')
    document = _supplemental(
        source,
        kind='capture_task_plan',
        state='supported',
        schema='htdt.capture.capture-task-plan',
        version='1',
    )
    document['source_evidence_id'] = 'f' * 64
    plan = _plan([source], [document])
    with pytest.raises(ValueError):
        CaptureIngestionPlan.model_validate(plan)


def test_supplemental_coordinate_space_must_be_in_bundle() -> None:
    source = _source_record('session/connected-spaces.json', b'{"s":1}')
    document = _supplemental(
        source,
        kind='connected_spaces',
        state='supported',
        schema='htdt.capture.connected-spaces',
        version='1',
        coordinate_space_ids=('99999999-0000-4000-8000-000000000099',),
    )
    plan = _plan([source], [document])
    with pytest.raises(ValueError):
        CaptureIngestionPlan.model_validate(plan)


def test_supported_kind_cannot_be_marked_unsupported() -> None:
    source = _source_record('session/capture-task-plan.json', b'{"plan":1}')
    document = _supplemental(
        source,
        kind='capture_task_plan',
        state='unsupported',
        schema='htdt.capture.capture-task-plan',
        version='1',
    )
    plan = _plan([source], [document])
    with pytest.raises(ValueError):
        CaptureIngestionPlan.model_validate(plan)


def test_supported_kind_enforces_canonical_path() -> None:
    source = _source_record('wrong/path.json', b'{"p":1}')
    document = _supplemental(
        source,
        kind='capture_task_plan',
        state='supported',
        schema='htdt.capture.capture-task-plan',
        version='1',
    )
    plan = _plan([source], [document])
    with pytest.raises(ValueError):
        CaptureIngestionPlan.model_validate(plan)
