from hashlib import sha256
import json
from pathlib import Path
import struct

import pytest

from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
    CaptureIngestionTransactionError,
)


BUNDLE_DIGEST = '2' * 64
SERIES_ID = '10000000-0000-4000-8000-000000000001'
REVISION_ID = '10000000-0000-4000-8000-000000000002'
SESSION_ID = '10000000-0000-4000-8000-000000000003'
SPACE_ID = '10000000-0000-4000-8000-000000000004'
ANCHOR_ID = '10000000-0000-4000-8000-000000000005'
ANNOTATION_ID = '10000000-0000-4000-8000-000000000006'


def _hash_parts(prefix: str, *parts: str) -> str:
    digest = sha256(prefix.encode('utf-8'))
    for part in parts:
        digest.update(b'\x00')
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def _meshbin() -> bytes:
    vertices = struct.pack(
        '<9f',
        0.0, 0.0, 0.0,
        1.0, 0.0, 0.0,
        0.0, 1.0, 0.0,
    )
    indices = struct.pack('<3I', 0, 1, 2)
    header = (
        b'HTDTMSH1'
        + struct.pack('<HH', 1, 0)
        + struct.pack('<I', 32)
        + struct.pack('<I', 3)
        + struct.pack('<I', 1)
        + bytes([4, 0])
        + struct.pack('<H', 0)
        + struct.pack('<I', 0)
    )
    return header + vertices + indices


def _plan_and_payloads() -> tuple[dict, dict[str, bytes]]:
    payloads = {
        'mesh/anchors.json': b'{"fixture":"anchors"}',
        f'mesh/geometry/{ANCHOR_ID}.meshbin': _meshbin(),
        'roomplan/captured-room-data.json': b'{"fixture":"raw-roomplan"}',
        'annotations/entities.json': b'{"fixture":"annotations"}',
    }

    source = []
    source_by_path = {}
    meta = {
        'mesh/anchors.json': (
            'application/json',
            'mesh_capture',
            'arkit_mesh_reconstruction',
        ),
        f'mesh/geometry/{ANCHOR_ID}.meshbin': (
            'application/vnd.htdt.meshbin',
            'mesh_capture',
            'arkit_mesh_reconstruction',
        ),
        'roomplan/captured-room-data.json': (
            'application/json',
            'roomplan_capture',
            'apple_roomplan_raw_scan',
        ),
        'annotations/entities.json': (
            'application/json',
            'annotation',
            'user_annotation',
        ),
    }

    for path in sorted(payloads):
        payload = payloads[path]
        digest = sha256(payload).hexdigest()
        source_id = _hash_parts(
            'htdt.capture.source-evidence.v1',
            BUNDLE_DIGEST,
            path,
            digest,
        )
        record = {
            'source_evidence_id': source_id,
            'bundle_digest': BUNDLE_DIGEST,
            'capture_revision_id': REVISION_ID,
            'path': path,
            'payload_sha256': digest,
            'bytes': len(payload),
            'media_type': meta[path][0],
            'producer': meta[path][1],
            'provenance_class': meta[path][2],
            'role': 'canonical',
            'source_refs': [],
        }
        source.append(record)
        source_by_path[path] = record

    geometry_path = f'mesh/geometry/{ANCHOR_ID}.meshbin'
    geometry = source_by_path[geometry_path]
    mesh_handoff_id = _hash_parts(
        'htdt.capture.raw-visual-mesh-handoff.v1',
        BUNDLE_DIGEST,
        ANCHOR_ID,
        geometry['payload_sha256'],
    )
    raw_mesh_handoff = {
        'raw_visual_mesh_handoff_id': mesh_handoff_id,
        'bundle_digest': BUNDLE_DIGEST,
        'anchor_id': ANCHOR_ID,
        'anchor_record_locator': f'mesh/anchors.json#anchor:{ANCHOR_ID}',
        'anchor_index_source_evidence_id': source_by_path[
            'mesh/anchors.json'
        ]['source_evidence_id'],
        'geometry_source_evidence_id': geometry['source_evidence_id'],
        'geometry_path': geometry_path,
        'geometry_sha256': geometry['payload_sha256'],
        'capture_session_id': SESSION_ID,
        'coordinate_space_id': SPACE_ID,
        'T_world_from_mesh_anchor': {
            'representation': 'column_major_4x4_f32',
            'values': [
                1, 0, 0, 0,
                0, 1, 0, 0,
                0, 0, 1, 0,
                0, 0, 0, 1,
            ],
        },
        'session_timestamp_s': 1.0,
        'vertex_count': 3,
        'face_count': 1,
    }

    roomplan_source = source_by_path[
        'roomplan/captured-room-data.json'
    ]
    roomplan = {
        'kind': 'raw_scan',
        'source_evidence_id': roomplan_source['source_evidence_id'],
        'path': roomplan_source['path'],
        'payload_sha256': roomplan_source['payload_sha256'],
        'provenance_class': 'apple_roomplan_raw_scan',
        'source_refs': [],
    }

    annotation_source = source_by_path['annotations/entities.json']
    authority_id = _hash_parts(
        'htdt.capture.authority-record.v1',
        BUNDLE_DIGEST,
        annotation_source['payload_sha256'],
        'annotation',
        ANNOTATION_ID,
    )
    authority = {
        'authority_record_handoff_id': authority_id,
        'record_kind': 'annotation',
        'record_id': ANNOTATION_ID,
        'record_locator': (
            f'annotations/entities.json#annotation:{ANNOTATION_ID}'
        ),
        'provenance_class': 'user_annotation',
        'coordinate_space_id': SPACE_ID,
        'source_evidence_id': annotation_source['source_evidence_id'],
        'source_payload_sha256': annotation_source['payload_sha256'],
    }

    projection = {
        'bundle_digest': BUNDLE_DIGEST,
        'source_evidence_ids': sorted(
            item['source_evidence_id'] for item in source
        ),
        'raw_visual_mesh_ids': [mesh_handoff_id],
        'authority_record_ids': [authority_id],
    }
    lineage_digest = sha256(
        json.dumps(
            projection,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()

    plan = {
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
        'roomplan_records': [roomplan],
        'raw_visual_mesh_handoffs': [raw_mesh_handoff],
        'authority_records': [authority],
        'lineage_digest': lineage_digest,
    }
    return plan, payloads


def test_transaction_commits_all_source_authorities_and_reopens(
    tmp_path: Path,
) -> None:
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    repository = CaptureIngestionRepository(scene)
    plan, payloads = _plan_and_payloads()

    result = repository.ingest(plan, payloads)

    assert result.created
    assert result.source_evidence_count == 4
    assert result.roomplan_record_count == 1
    assert result.raw_mesh_binding_count == 1
    assert result.authority_record_count == 1

    typed = CaptureIngestionPlan.model_validate(plan)
    reopened = repository.get_ingestion(typed.lineage_digest)
    assert reopened == typed

    geometry_source = next(
        item for item in typed.source_evidence
        if item.path.endswith('.meshbin')
    )
    persisted = repository.get_source_evidence(
        geometry_source.source_evidence_id
    )
    assert persisted is not None
    assert persisted.record == geometry_source
    assert persisted.payload == payloads[geometry_source.path]

    handoff = typed.raw_visual_mesh_handoffs[0]
    binding_id = repository.get_ingestion(
        typed.lineage_digest
    ).raw_visual_mesh_handoffs[0].raw_visual_mesh_handoff_id
    # Binding ID is adapter-derived, so locate it through the persisted DB link.
    import sqlite3
    with sqlite3.connect(scene.path) as connection:
        row = connection.execute(
            '''
            SELECT binding_id
            FROM capture_ingestion_mesh_links
            WHERE lineage_digest=?
            ''',
            (typed.lineage_digest,),
        ).fetchone()
    assert row is not None
    binding = repository.get_mesh_binding(row[0])
    assert binding is not None
    assert binding.handoff == handoff
    assert binding.raw_mesh.original_asset_bytes() == payloads[handoff.geometry_path]

    authority = repository.get_authority_record(
        typed.authority_records[0].authority_record_handoff_id
    )
    assert authority == typed.authority_records[0]


def test_reingestion_is_idempotent_for_same_plan_and_payloads(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()

    first = repository.ingest(plan, payloads)
    second = repository.ingest(plan, payloads)

    assert first.created
    assert not second.created
    assert first.lineage_digest == second.lineage_digest
    assert repository.source_evidence_count() == 4


def test_payload_failure_leaves_no_partial_source_evidence(
    tmp_path: Path,
) -> None:
    repository = CaptureIngestionRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    plan, payloads = _plan_and_payloads()
    typed = CaptureIngestionPlan.model_validate(plan)
    bad = dict(payloads)
    geometry_path = typed.raw_visual_mesh_handoffs[0].geometry_path
    bad[geometry_path] = bad[geometry_path] + b'tamper'

    with pytest.raises(
        CaptureIngestionTransactionError,
        match='byte-count mismatch',
    ):
        repository.ingest(typed, bad)

    assert repository.get_ingestion(typed.lineage_digest) is None
    assert repository.source_evidence_count() == 0


def test_plan_deterministic_identity_is_revalidated_backend_side() -> None:
    plan, _ = _plan_and_payloads()
    plan['source_evidence'][0]['source_evidence_id'] = 'f' * 64

    with pytest.raises(ValueError, match='deterministic identity mismatch'):
        CaptureIngestionPlan.model_validate(plan)



def test_plan_rejects_unresolved_source_reference_backend_side() -> None:
    plan, _ = _plan_and_payloads()
    plan['source_evidence'][0]['source_refs'] = ['path:missing/payload.bin']

    with pytest.raises(ValueError, match='unresolved source evidence path reference'):
        CaptureIngestionPlan.model_validate(plan)


def test_plan_rejects_roomplan_kind_path_provenance_mismatch() -> None:
    plan, _ = _plan_and_payloads()
    plan['roomplan_records'][0]['kind'] = 'postprocessed_inference'

    with pytest.raises(ValueError, match='RoomPlan kind/path/provenance mismatch'):
        CaptureIngestionPlan.model_validate(plan)


def test_plan_rejects_wrong_mesh_anchor_index_authority() -> None:
    plan, _ = _plan_and_payloads()
    annotation_source = next(
        item for item in plan['source_evidence']
        if item['path'] == 'annotations/entities.json'
    )
    plan['raw_visual_mesh_handoffs'][0][
        'anchor_index_source_evidence_id'
    ] = annotation_source['source_evidence_id']

    with pytest.raises(ValueError, match='raw mesh anchor-index authority mismatch'):
        CaptureIngestionPlan.model_validate(plan)


def test_plan_rejects_unpinned_ingestor_configuration() -> None:
    plan, _ = _plan_and_payloads()
    plan['ingestor']['configuration_digest'] = 'f' * 64

    with pytest.raises(ValueError):
        CaptureIngestionPlan.model_validate(plan)
