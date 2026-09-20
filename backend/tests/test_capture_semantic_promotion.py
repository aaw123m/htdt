from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import struct

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import SceneDocument
from htdt.capture_ingestion_transaction import CaptureIngestionRepository
from htdt.capture_semantic_promotion import (
    CaptureSemanticPromotionError,
    CaptureSemanticPromotionRepository,
    make_capture_semantic_promotion_request,
    make_capture_world_to_scene_authority,
)
from htdt.semantic_geometry import SemanticCoordinateTransform


BUNDLE_DIGEST = 'a' * 64
SERIES_ID = '20000000-0000-4000-8000-000000000001'
REVISION_ID = '20000000-0000-4000-8000-000000000002'
SESSION_ID = '20000000-0000-4000-8000-000000000003'
SPACE_ID = '20000000-0000-4000-8000-000000000004'
ANCHOR_ID = '20000000-0000-4000-8000-000000000005'
INGESTOR_CONFIG = (
    '3e27eec298714a04fc6b48d94b354168396e2c4eea0cf9aa8284fa552de562b3'
)


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


def _ingestion_fixture() -> tuple[dict, dict[str, bytes]]:
    geometry_path = f'mesh/geometry/{ANCHOR_ID}.meshbin'
    payloads = {
        'mesh/anchors.json': b'{"fixture":"anchors"}',
        geometry_path: _meshbin(),
    }
    source = []
    by_path = {}
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
            'media_type': (
                'application/vnd.htdt.meshbin'
                if path.endswith('.meshbin')
                else 'application/json'
            ),
            'producer': 'mesh_capture',
            'provenance_class': 'arkit_mesh_reconstruction',
            'role': 'canonical',
            'source_refs': [],
        }
        source.append(record)
        by_path[path] = record

    geometry = by_path[geometry_path]
    handoff_id = _hash_parts(
        'htdt.capture.raw-visual-mesh-handoff.v1',
        BUNDLE_DIGEST,
        ANCHOR_ID,
        geometry['payload_sha256'],
    )
    handoff = {
        'raw_visual_mesh_handoff_id': handoff_id,
        'bundle_digest': BUNDLE_DIGEST,
        'anchor_id': ANCHOR_ID,
        'anchor_record_locator': f'mesh/anchors.json#anchor:{ANCHOR_ID}',
        'anchor_index_source_evidence_id':
            by_path['mesh/anchors.json']['source_evidence_id'],
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
                1, 2, 3, 1,
            ],
        },
        'session_timestamp_s': 1.0,
        'vertex_count': 3,
        'face_count': 1,
    }

    lineage_projection = {
        'bundle_digest': BUNDLE_DIGEST,
        'source_evidence_ids': sorted(
            item['source_evidence_id'] for item in source
        ),
        'raw_visual_mesh_ids': [handoff_id],
        'authority_record_ids': [],
    }
    lineage_digest = sha256(
        json.dumps(
            lineage_projection,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()

    return (
        {
            'schema': 'htdt.capture.ingestion-plan',
            'schema_version': '1.0.0',
            'ingestor': {
                'name': 'htdt-capture-reference-ingestor',
                'version': '1.0.0',
                'configuration_digest': INGESTOR_CONFIG,
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
            'raw_visual_mesh_handoffs': [handoff],
            'authority_records': [],
            'lineage_digest': lineage_digest,
        },
        payloads,
    )


def _repositories(
    tmp_path: Path,
) -> tuple[
    SceneRepository,
    CaptureIngestionRepository,
    CaptureSemanticPromotionRepository,
    str,
    str,
]:
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    capture = CaptureIngestionRepository(scene)
    plan, payloads = _ingestion_fixture()
    ingestion = capture.ingest(plan, payloads)
    binding_id = capture.mesh_binding_ids_for_ingestion(
        ingestion.lineage_digest
    )[0]

    root = scene.save(
        SceneDocument(
            document_id='capture-doc',
            schema_version=4,
            room=None,
            entities=(),
        ),
        parent_revision_id=None,
    )
    promotion = CaptureSemanticPromotionRepository(scene, capture)
    return (
        scene,
        capture,
        promotion,
        ingestion.lineage_digest,
        binding_id,
    )


def _identity_alignment(space_id: str):
    transform = SemanticCoordinateTransform(
        matrix_source_to_scene_m=(
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        ),
        provenance='explicit_user_authority',
        reason='fixture explicitly aligns capture world to HTDT scene',
    )
    return make_capture_world_to_scene_authority(
        coordinate_space_id=space_id,
        transform=transform,
    )


def test_capture_mesh_promotion_binds_exact_transform_and_scene_revision(
    tmp_path: Path,
) -> None:
    scene, capture, promotion, lineage, binding_id = _repositories(
        tmp_path
    )
    source = scene.latest('capture-doc')
    assert source is not None

    inspection = promotion.inspect_capture_mesh(
        ingestion_lineage_digest=lineage,
        raw_mesh_binding_id=binding_id,
    )
    assert inspection.capture_coordinate_space_id == SPACE_ID
    assert len(inspection.triangle_ids) == 1

    request = make_capture_semantic_promotion_request(
        ingestion_lineage_digest=lineage,
        raw_mesh_binding_id=binding_id,
        target_document_id='capture-doc',
        source_scene_revision_id=source.revision_id,
        world_to_scene_authority=_identity_alignment(SPACE_ID),
        readiness_policy='allow_blocked_semantic_authority',
        reason='retain imported capture mesh as explicit semantic authority',
    )
    result = promotion.promote(request)

    assert result.promotion_created
    assert result.scene_revision_created
    promoted = scene.get(result.scene_revision_id)
    assert promoted is not None
    geometry = promoted.document.r120_semantic_geometry
    assert geometry is not None
    assert geometry.geometry_id == result.semantic_geometry_id
    assert geometry.source_scene_revision_id == source.revision_id
    assert geometry.source_to_scene_transform.matrix_source_to_scene_m == (
        (1.0, 0.0, 0.0, 1.0),
        (0.0, 1.0, 0.0, 2.0),
        (0.0, 0.0, 1.0, 3.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    assert (
        geometry.vertices[0].x_m,
        geometry.vertices[0].y_m,
        geometry.vertices[0].z_m,
    ) == (1.0, 2.0, 3.0)
    assert geometry.solver_ready is False

    repeated = promotion.promote(request)
    assert repeated.scene_revision_id == result.scene_revision_id
    assert not repeated.promotion_created
    assert not repeated.scene_revision_created


def test_promotion_rejects_wrong_capture_coordinate_authority(
    tmp_path: Path,
) -> None:
    scene, capture, promotion, lineage, binding_id = _repositories(
        tmp_path
    )
    source = scene.latest('capture-doc')
    assert source is not None

    request = make_capture_semantic_promotion_request(
        ingestion_lineage_digest=lineage,
        raw_mesh_binding_id=binding_id,
        target_document_id='capture-doc',
        source_scene_revision_id=source.revision_id,
        world_to_scene_authority=_identity_alignment(
            '20000000-0000-4000-8000-000000000099'
        ),
        readiness_policy='allow_blocked_semantic_authority',
        reason='fixture mismatch',
    )

    with pytest.raises(
        CaptureSemanticPromotionError,
        match='coordinate space mismatch',
    ):
        promotion.promote(request)

    assert scene.latest('capture-doc').revision_id == source.revision_id


def test_require_ready_policy_fails_before_scene_revision_commit(
    tmp_path: Path,
) -> None:
    scene, capture, promotion, lineage, binding_id = _repositories(
        tmp_path
    )
    source = scene.latest('capture-doc')
    assert source is not None

    request = make_capture_semantic_promotion_request(
        ingestion_lineage_digest=lineage,
        raw_mesh_binding_id=binding_id,
        target_document_id='capture-doc',
        source_scene_revision_id=source.revision_id,
        world_to_scene_authority=_identity_alignment(SPACE_ID),
        readiness_policy='require_r120_compiler_contract_ready',
        reason='fixture requires ready geometry',
    )

    with pytest.raises(
        CaptureSemanticPromotionError,
        match='does not satisfy required R120',
    ):
        promotion.promote(request)

    assert scene.latest('capture-doc').revision_id == source.revision_id
    with sqlite3.connect(scene.path) as connection:
        count = connection.execute(
            'SELECT COUNT(*) FROM capture_semantic_promotions'
        ).fetchone()[0]
    assert count == 0
