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
    CaptureSemanticPromotionRequest,
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
    normals = struct.pack(
        '<9f',
        0.0, 0.0, 1.0,
        0.0, 0.0, 1.0,
        0.0, 0.0, 1.0,
    )
    indices = struct.pack('<3I', 0, 1, 2)
    classifications = bytes([1])
    header = (
        b'HTDTMSH1'
        + struct.pack('<HH', 1, 0)
        + struct.pack('<I', 32)
        + struct.pack('<I', 3)
        + struct.pack('<I', 1)
        + bytes([4, 0x03])
        + struct.pack('<H', 0)
        + struct.pack('<I', 0)
    )
    return header + vertices + normals + indices + classifications


def _ingestion_fixture(
    bundle_digest: str = BUNDLE_DIGEST,
    space_id: str = SPACE_ID,
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
        'coordinate_space_id': space_id,
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
                'coordinate_space_ids': [space_id],
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
    binding_id = capture.mesh_binding_ids_for_run(
        ingestion.ingestion_run_id
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
        ingestion.ingestion_run_id,
        binding_id,
    )


def _identity_alignment(
    capture: CaptureIngestionRepository,
    space_id: str,
    bundle_digest: str = BUNDLE_DIGEST,
):
    authority = capture.coordinate_authority_for(bundle_digest, space_id)
    assert authority is not None
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
        coordinate_authority=authority,
        transform=transform,
    )


def test_capture_mesh_promotion_binds_exact_transform_and_scene_revision(
    tmp_path: Path,
) -> None:
    scene, capture, promotion, run_id, binding_id = _repositories(
        tmp_path
    )
    source = scene.latest('capture-doc')
    assert source is not None

    inspection = promotion.inspect_capture_mesh(
        ingestion_run_id=run_id,
        raw_mesh_binding_id=binding_id,
    )
    assert inspection.capture_coordinate_space_id == SPACE_ID
    assert len(inspection.triangle_ids) == 1
    assert inspection.source_normals_present
    assert [
        (item.classification_value, item.classification_label)
        for item in inspection.face_classification_advisories
    ] == [(1, 'wall')]
    assert inspection.face_classification_advisories[0].source_primitive == (
        'htdt-meshbin-face:0'
    )

    request = make_capture_semantic_promotion_request(
        ingestion_run_id=run_id,
        raw_mesh_binding_id=binding_id,
        target_document_id='capture-doc',
        source_scene_revision_id=source.revision_id,
        world_to_scene_authority=_identity_alignment(capture, SPACE_ID),
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


def test_promotion_rejects_authority_scoped_to_other_bundle(
    tmp_path: Path,
) -> None:
    """#365: the same coordinate-space UUID under a different bundle is a
    different authority — the alignment cannot satisfy this run."""
    scene, capture, promotion, run_id, binding_id = _repositories(
        tmp_path
    )
    source = scene.latest('capture-doc')
    assert source is not None

    other_plan, other_payloads = _ingestion_fixture(
        bundle_digest='b' * 64
    )
    capture.ingest(other_plan, other_payloads)

    request = make_capture_semantic_promotion_request(
        ingestion_run_id=run_id,
        raw_mesh_binding_id=binding_id,
        target_document_id='capture-doc',
        source_scene_revision_id=source.revision_id,
        world_to_scene_authority=_identity_alignment(
            capture, SPACE_ID, bundle_digest='b' * 64
        ),
        readiness_policy='allow_blocked_semantic_authority',
        reason='same UUID under a different bundle must not apply',
    )

    with pytest.raises(
        CaptureSemanticPromotionError,
        match='authority scope mismatch',
    ):
        promotion.promote(request)

    assert scene.latest('capture-doc').revision_id == source.revision_id


def test_promotion_rejects_wrong_capture_coordinate_authority(
    tmp_path: Path,
) -> None:
    scene, capture, promotion, run_id, binding_id = _repositories(
        tmp_path
    )
    source = scene.latest('capture-doc')
    assert source is not None

    wrong_space = '20000000-0000-4000-8000-000000000099'
    other_plan, other_payloads = _ingestion_fixture(
        bundle_digest='b' * 64, space_id=wrong_space
    )
    capture.ingest(other_plan, other_payloads)

    request = make_capture_semantic_promotion_request(
        ingestion_run_id=run_id,
        raw_mesh_binding_id=binding_id,
        target_document_id='capture-doc',
        source_scene_revision_id=source.revision_id,
        world_to_scene_authority=_identity_alignment(
            capture, wrong_space, bundle_digest='b' * 64
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
    scene, capture, promotion, run_id, binding_id = _repositories(
        tmp_path
    )
    source = scene.latest('capture-doc')
    assert source is not None

    request = make_capture_semantic_promotion_request(
        ingestion_run_id=run_id,
        raw_mesh_binding_id=binding_id,
        target_document_id='capture-doc',
        source_scene_revision_id=source.revision_id,
        world_to_scene_authority=_identity_alignment(capture, SPACE_ID),
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
    scene, capture, promotion, run_id, binding_id = _repositories(tmp_path)
    source = scene.latest('capture-doc')
    request = make_capture_semantic_promotion_request(
        ingestion_run_id=run_id,
        raw_mesh_binding_id=binding_id,
        target_document_id='capture-doc',
        source_scene_revision_id=source.revision_id,
        world_to_scene_authority=_identity_alignment(capture, SPACE_ID),
        readiness_policy='allow_blocked_semantic_authority',
        reason='retain imported capture mesh as explicit semantic authority',
    )
    result = promotion.promote(request)
    assert result.promotion_created
    return scene, capture, promotion, run_id, binding_id, source, request


def test_referenced_ingestion_mesh_link_cannot_be_deleted(
    tmp_path: Path,
) -> None:
    """The composite FK rejects deleting a link a promotion depends on."""
    scene, _capture, _promotion, run_id, binding_id, _source, _request = (
        _promoted_fixture(tmp_path)
    )

    with closing(sqlite3.connect(scene.path)) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                DELETE FROM capture_ingestion_mesh_links
                WHERE ingestion_run_id=? AND binding_id=?
                ''',
                (run_id, binding_id),
            )
        connection.rollback()


def test_promotion_insert_requires_the_exact_linked_pair(
    tmp_path: Path,
) -> None:
    """A promotion whose run+binding both exist but are unlinked fails."""
    scene, capture, _promotion, run_id, _binding, source, _request = (
        _promoted_fixture(tmp_path)
    )

    # A second ingestion persists a relationally valid binding that is not
    # linked to the first lineage digest.
    plan2, payloads2 = _ingestion_fixture(bundle_digest='b' * 64)
    second = capture.ingest(plan2, payloads2)
    other_binding = capture.mesh_binding_ids_for_run(
        second.ingestion_run_id
    )[0]

    with closing(sqlite3.connect(scene.path)) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                INSERT INTO capture_semantic_promotions(
                    promotion_id,
                    ingestion_run_id,
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
                    run_id,
                    other_binding,
                    source.revision_id,
                    source.revision_id,
                    'semantic-geometry:test',
                    '{}',
                    '2026-09-20T00:00:00+00:00',
                ),
            )
        connection.rollback()


def _legacy_request_json(request, lineage_digest: str) -> tuple[str, str]:
    """Render the request/promotion identity the pre-run contract produced.

    Legacy rows stored ``ingestion_lineage_digest`` instead of
    ``ingestion_run_id`` and a world-to-scene authority with only the bare
    coordinate-space UUID — the exact payload the migration must translate
    back losslessly.
    """

    payload = request.model_dump(mode='json', exclude={'promotion_id'})
    payload.pop('ingestion_run_id')
    payload['ingestion_lineage_digest'] = lineage_digest
    authority = dict(payload['world_to_scene_authority'])
    authority.pop('coordinate_authority_id', None)
    authority['authority_id'] = 'capture-world-to-scene:' + sha256(
        json.dumps(
            {
                'domain': 'htdt.capture.world-to-scene-authority.v1',
                'coordinate_space_id': authority['coordinate_space_id'],
                'transform': authority['transform'],
            },
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()
    payload['world_to_scene_authority'] = authority
    legacy_id = 'capture-semantic-promotion:' + sha256(
        json.dumps(
            {
                'domain': 'htdt.capture.semantic-promotion.v1',
                **{k: v for k, v in payload.items() if v is not None},
            },
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()
    return json.dumps(
        {**payload, 'promotion_id': legacy_id},
        sort_keys=True,
        separators=(',', ':'),
    ), legacy_id


def _downgrade_promotions_table(path: Path, lineage_digest: str) -> None:
    """Rewrite the promotions table to its pre-run-identity shape.

    Mirrors exactly what the lineage-scoped ``CREATE TABLE`` produced: the
    eight legacy columns with the lineage digest keying both the row and
    its request JSON — the state ``_migrate_promotion_run_scope`` must
    rebind deterministically.
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
                p.promotion_id,
                r.lineage_digest,
                p.raw_mesh_binding_id,
                p.source_scene_revision_id,
                p.scene_revision_id,
                p.semantic_geometry_id,
                p.request_json,
                p.created_at_utc
            FROM capture_semantic_promotions_legacy AS p
            JOIN capture_ingestion_runs AS r
              ON r.ingestion_run_id = p.ingestion_run_id;
            DROP TABLE capture_semantic_promotions_legacy;
            CREATE INDEX idx_capture_semantic_promotion_ingestion
            ON capture_semantic_promotions(
                ingestion_lineage_digest,
                raw_mesh_binding_id
            );
            '''
        )
        # Regress request_json + promotion_id to the true legacy payload.
        connection.execute('PRAGMA foreign_keys=OFF')
        for row in connection.execute(
            'SELECT promotion_id, request_json '
            'FROM capture_semantic_promotions'
        ).fetchall():
            request = CaptureSemanticPromotionRequest.model_validate_json(
                row[1]
            )
            legacy_json, legacy_id = _legacy_request_json(
                request, lineage_digest
            )
            connection.execute(
                'UPDATE capture_semantic_promotions '
                'SET promotion_id=?, request_json=? WHERE promotion_id=?',
                (legacy_id, legacy_json, row[0]),
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
    scene, capture, _promotion, run_id, binding_id, _source, request = (
        _promoted_fixture(tmp_path)
    )
    lineage = capture.list_ingestion_runs()[0].lineage_digest
    _downgrade_promotions_table(scene.path, lineage)
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
                WHERE ingestion_run_id=? AND binding_id=?
                ''',
                (run_id, binding_id),
            )
        connection.rollback()


def test_orphaned_legacy_promotion_fails_link_fk_migration(
    tmp_path: Path,
) -> None:
    scene, capture, _promotion, run_id, binding_id, _source, _request = (
        _promoted_fixture(tmp_path)
    )
    lineage = capture.list_ingestion_runs()[0].lineage_digest
    _downgrade_promotions_table(scene.path, lineage)

    # Simulate the provenance-orphaned state the composite FK prevents: the
    # link row disappears while the promotion still references it. Foreign
    # keys are off for this connection so the damage can be staged.
    with closing(sqlite3.connect(scene.path)) as connection, connection:
        connection.execute(
            '''
            DELETE FROM capture_ingestion_mesh_links
            WHERE ingestion_run_id=? AND binding_id=?
            ''',
            (run_id, binding_id),
        )

    with pytest.raises(
        CaptureSemanticPromotionError,
        match='not backed by an ingestion-mesh link',
    ):
        CaptureSemanticPromotionRepository(
            scene, CaptureIngestionRepository(scene)
        )
