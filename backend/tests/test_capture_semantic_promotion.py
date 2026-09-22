from contextlib import closing
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


def _ingestion_fixture(
    bundle_digest: str = BUNDLE_DIGEST,
) -> tuple[dict, dict[str, bytes]]:
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
            bundle_digest,
            path,
            digest,
        )
        record = {
            'source_evidence_id': source_id,
            'bundle_digest': bundle_digest,
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
        bundle_digest,
        ANCHOR_ID,
        geometry['payload_sha256'],
    )
    handoff = {
        'raw_visual_mesh_handoff_id': handoff_id,
        'bundle_digest': bundle_digest,
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
        'bundle_digest': bundle_digest,
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
                'bundle_digest': bundle_digest,
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


def _promoted_fixture(
    tmp_path: Path,
):
    scene, capture, promotion, lineage, binding_id = _repositories(tmp_path)
    source = scene.latest('capture-doc')
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
    return scene, capture, promotion, lineage, binding_id, source, request


def test_referenced_ingestion_mesh_link_cannot_be_deleted(
    tmp_path: Path,
) -> None:
    """The composite FK rejects deleting a link a promotion depends on."""
    scene, _capture, _promotion, lineage, binding_id, _source, _request = (
        _promoted_fixture(tmp_path)
    )

    with closing(sqlite3.connect(scene.path)) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                DELETE FROM capture_ingestion_mesh_links
                WHERE lineage_digest=? AND binding_id=?
                ''',
                (lineage, binding_id),
            )
        connection.rollback()


def test_promotion_insert_requires_the_exact_linked_pair(
    tmp_path: Path,
) -> None:
    """A promotion whose run+binding both exist but are unlinked fails."""
    scene, capture, _promotion, lineage, _binding, source, _request = (
        _promoted_fixture(tmp_path)
    )

    # A second ingestion persists a relationally valid binding that is not
    # linked to the first lineage digest.
    plan2, payloads2 = _ingestion_fixture(bundle_digest='b' * 64)
    second = capture.ingest(plan2, payloads2)
    other_binding = capture.mesh_binding_ids_for_ingestion(
        second.lineage_digest
    )[0]

    with closing(sqlite3.connect(scene.path)) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                INSERT INTO capture_semantic_promotions(
                    promotion_id,
                    ingestion_lineage_digest,
                    raw_mesh_binding_id,
                    source_scene_revision_id,
                    scene_revision_id,
                    semantic_geometry_id,
                    request_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    'capture-semantic-promotion:' + 'f' * 64,
                    lineage,
                    other_binding,
                    source.revision_id,
                    source.revision_id,
                    'semantic-geometry:test',
                    '{}',
                    '2026-09-20T00:00:00+00:00',
                ),
            )
        connection.rollback()


def _downgrade_promotions_table(path: Path) -> None:
    """Rewrite the promotions table to its pre-normalization shape.

    Mirrors exactly what the pre-#439 ``CREATE TABLE`` produced: the same
    eight columns and four independent foreign keys, but no composite edge
    to ``capture_ingestion_mesh_links``.
    """

    with closing(sqlite3.connect(path)) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.executescript(
            '''
            ALTER TABLE capture_semantic_promotions
            RENAME TO capture_semantic_promotions_legacy;
            CREATE TABLE capture_semantic_promotions (
                promotion_id TEXT PRIMARY KEY,
                ingestion_lineage_digest TEXT NOT NULL,
                raw_mesh_binding_id TEXT NOT NULL,
                source_scene_revision_id TEXT NOT NULL,
                scene_revision_id TEXT NOT NULL,
                semantic_geometry_id TEXT NOT NULL,
                request_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                FOREIGN KEY(ingestion_lineage_digest)
                    REFERENCES capture_ingestion_runs(lineage_digest),
                FOREIGN KEY(raw_mesh_binding_id)
                    REFERENCES capture_raw_visual_mesh_bindings(binding_id),
                FOREIGN KEY(source_scene_revision_id)
                    REFERENCES scene_revisions(revision_id),
                FOREIGN KEY(scene_revision_id)
                    REFERENCES scene_revisions(revision_id)
            );
            INSERT INTO capture_semantic_promotions(
                promotion_id,
                ingestion_lineage_digest,
                raw_mesh_binding_id,
                source_scene_revision_id,
                scene_revision_id,
                semantic_geometry_id,
                request_json,
                created_at_utc
            )
            SELECT
                promotion_id,
                ingestion_lineage_digest,
                raw_mesh_binding_id,
                source_scene_revision_id,
                scene_revision_id,
                semantic_geometry_id,
                request_json,
                created_at_utc
            FROM capture_semantic_promotions_legacy;
            DROP TABLE capture_semantic_promotions_legacy;
            CREATE INDEX idx_capture_semantic_promotion_ingestion
            ON capture_semantic_promotions(
                ingestion_lineage_digest,
                raw_mesh_binding_id
            );
            '''
        )


def _promotion_link_fk_count(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return sum(
            1
            for row in connection.execute(
                'PRAGMA foreign_key_list(capture_semantic_promotions)'
            )
            if row['table'] == 'capture_ingestion_mesh_links'
        )


def test_legacy_promotion_table_gains_composite_link_fk(
    tmp_path: Path,
) -> None:
    scene, _capture, _promotion, lineage, binding_id, _source, request = (
        _promoted_fixture(tmp_path)
    )
    _downgrade_promotions_table(scene.path)
    assert _promotion_link_fk_count(scene.path) == 0

    migrated = CaptureSemanticPromotionRepository(
        scene, CaptureIngestionRepository(scene)
    )

    assert _promotion_link_fk_count(scene.path) == 2
    with closing(sqlite3.connect(scene.path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            '''
            SELECT *
            FROM capture_semantic_promotions
            ORDER BY promotion_id
            '''
        ).fetchall()
        index_sql = connection.execute(
            '''
            SELECT sql
            FROM sqlite_master
            WHERE type='index'
              AND name='idx_capture_semantic_promotion_ingestion'
            '''
        ).fetchone()
    assert [row['promotion_id'] for row in rows] == [request.promotion_id]
    assert index_sql is not None

    # The migrated row still deduplicates a repeated promotion.
    repeated = migrated.promote(request)
    assert not repeated.promotion_created

    # And the composite FK now blocks deleting the referenced link row.
    with closing(sqlite3.connect(scene.path)) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                DELETE FROM capture_ingestion_mesh_links
                WHERE lineage_digest=? AND binding_id=?
                ''',
                (lineage, binding_id),
            )
        connection.rollback()


def test_orphaned_legacy_promotion_fails_link_fk_migration(
    tmp_path: Path,
) -> None:
    scene, _capture, _promotion, lineage, binding_id, _source, _request = (
        _promoted_fixture(tmp_path)
    )
    _downgrade_promotions_table(scene.path)

    # Simulate the provenance-orphaned state the composite FK prevents: the
    # link row disappears while the promotion still references it. Foreign
    # keys are off for this connection so the damage can be staged.
    with closing(sqlite3.connect(scene.path)) as connection, connection:
        connection.execute(
            '''
            DELETE FROM capture_ingestion_mesh_links
            WHERE lineage_digest=? AND binding_id=?
            ''',
            (lineage, binding_id),
        )

    with pytest.raises(
        CaptureSemanticPromotionError,
        match='not backed by an ingestion-mesh link',
    ):
        CaptureSemanticPromotionRepository(
            scene, CaptureIngestionRepository(scene)
        )
