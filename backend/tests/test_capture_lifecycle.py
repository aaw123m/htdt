"""Cross-cutting capture lifecycle contracts (#351/#352/#353/#359/#365/
#368/#412/#413): run identity, scoped coordinate authorities, library
discovery, multi-anchor composition, conflict policy, replay, retention,
and provenance trust — all exercised end to end on one native database.
"""

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import struct

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import SceneDocument
from htdt.capture_ingestion_transaction import (
    SUPPORTED_INGESTOR_IDENTITIES,
    CaptureIngestionRepository,
)
from htdt.capture_retention import (
    CaptureRetentionError,
    CaptureRetentionService,
)
from htdt.capture_semantic_promotion import (
    CapturePromotionReplayError,
    CaptureSemanticPromotionError,
    CaptureSemanticPromotionRepository,
    make_capture_mesh_composition_request,
    make_capture_semantic_promotion_request,
    make_capture_world_to_scene_authority,
)
from htdt.semantic_geometry import SemanticCoordinateTransform


BUNDLE_DIGEST = 'a' * 64
OTHER_BUNDLE_DIGEST = 'b' * 64
SERIES_ID = '30000000-0000-4000-8000-000000000001'
REVISION_ID = '30000000-0000-4000-8000-000000000002'
SESSION_ID = '30000000-0000-4000-8000-000000000003'
SPACE_ID = '30000000-0000-4000-8000-000000000004'
ANCHOR_A = '30000000-0000-4000-8000-000000000005'
ANCHOR_B = '30000000-0000-4000-8000-000000000006'
INGESTOR_CONFIG = (
    '3e27eec298714a04fc6b48d94b354168396e2c4eea0cf9aa8284fa552de562b3'
)


def _hash_parts(prefix: str, *parts: str) -> str:
    digest = sha256(prefix.encode('utf-8'))
    for part in parts:
        digest.update(b'\x00')
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def _meshbin(x_offset: float = 0.0) -> bytes:
    vertices = struct.pack(
        '<9f',
        x_offset, 0.0, 0.0,
        1.0 + x_offset, 0.0, 0.0,
        x_offset, 1.0, 0.0,
    )
    normals = struct.pack('<9f', 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0)
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
    anchors: tuple[tuple[str, float], ...] = ((ANCHOR_A, 0.0),),
    ingestor_name: str = 'htdt-capture-reference-ingestor',
    ingestor_version: str = '1.0.0',
    revision_id: str = REVISION_ID,
    series_id: str = SERIES_ID,
    parent_revision_id: str | None = None,
    extra_source: dict | None = None,
) -> tuple[dict, dict[str, bytes]]:
    payloads: dict[str, bytes] = {'mesh/anchors.json': b'{"fixture":"anchors"}'}
    for anchor_id, offset in anchors:
        payloads[f'mesh/geometry/{anchor_id}.meshbin'] = _meshbin(offset)
    if extra_source is not None:
        payloads[extra_source['path']] = extra_source['payload']

    source = []
    by_path = {}
    for path in sorted(payloads):
        payload = payloads[path]
        digest = sha256(payload).hexdigest()
        record = {
            'source_evidence_id': _hash_parts(
                'htdt.capture.source-evidence.v1',
                bundle_digest,
                path,
                digest,
            ),
            'bundle_digest': bundle_digest,
            'capture_revision_id': revision_id,
            'path': path,
            'payload_sha256': digest,
            'bytes': len(payload),
            'media_type': (
                'application/vnd.htdt.meshbin'
                if path.endswith('.meshbin')
                else 'application/json'
            ),
            'producer': 'mesh_capture',
            'provenance_class': (
                extra_source['provenance_class']
                if extra_source is not None and path == extra_source['path']
                else 'arkit_mesh_reconstruction'
            ),
            'role': (
                extra_source['role']
                if extra_source is not None and path == extra_source['path']
                else 'canonical'
            ),
            'source_refs': [],
        }
        source.append(record)
        by_path[path] = record

    handoffs = []
    for index, (anchor_id, _offset) in enumerate(anchors):
        geometry_path = f'mesh/geometry/{anchor_id}.meshbin'
        geometry = by_path[geometry_path]
        handoffs.append(
            {
                'raw_visual_mesh_handoff_id': _hash_parts(
                    'htdt.capture.raw-visual-mesh-handoff.v1',
                    bundle_digest,
                    anchor_id,
                    geometry['payload_sha256'],
                ),
                'bundle_digest': bundle_digest,
                'anchor_id': anchor_id,
                'anchor_record_locator': (
                    f'mesh/anchors.json#anchor:{anchor_id}'
                ),
                'anchor_index_source_evidence_id':
                    by_path['mesh/anchors.json']['source_evidence_id'],
                'geometry_source_evidence_id':
                    geometry['source_evidence_id'],
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
                        float(index), 2.0, 3.0, 1.0,
                    ],
                },
                'session_timestamp_s': 1.0,
                'vertex_count': 3,
                'face_count': 1,
            }
        )

    lineage_projection = {
        'bundle_digest': bundle_digest,
        'source_evidence_ids': sorted(
            item['source_evidence_id'] for item in source
        ),
        'raw_visual_mesh_ids': sorted(
            item['raw_visual_mesh_handoff_id'] for item in handoffs
        ),
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
                'name': ingestor_name,
                'version': ingestor_version,
                'configuration_digest': INGESTOR_CONFIG,
            },
            'bundle': {
                'bundle_digest': bundle_digest,
                'capture_schema': 'htdt.capture.bundle',
                'capture_schema_version': '1.0.0',
                'capture_series_id': series_id,
                'capture_revision_id': revision_id,
                'parent_revision_id': parent_revision_id,
                'capture_session_ids': [SESSION_ID],
                'coordinate_space_ids': [space_id],
            },
            'source_evidence': source,
            'roomplan_records': [],
            'raw_visual_mesh_handoffs': handoffs,
            'authority_records': [],
            'lineage_digest': lineage_digest,
        },
        payloads,
    )


def _repositories(tmp_path: Path):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    capture = CaptureIngestionRepository(scene)
    plan, payloads = _ingestion_fixture(
        anchors=((ANCHOR_A, 0.0), (ANCHOR_B, 1.0))
    )
    ingestion = capture.ingest(plan, payloads)
    binding_ids = sorted(
        capture.mesh_binding_ids_for_run(ingestion.ingestion_run_id)
    )
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
        binding_ids,
        root.revision.revision_id,
    )


def _identity_alignment(capture: CaptureIngestionRepository, space_id: str):
    authority = capture.coordinate_authority_for(BUNDLE_DIGEST, space_id)
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


def _promotion_request(capture, run_id, scene, **overrides):
    source = scene.latest('capture-doc')
    kwargs = {
        'ingestion_run_id': run_id,
        'raw_mesh_binding_id': overrides.pop('raw_mesh_binding_id', None),
        'mesh_composition_id': overrides.pop('mesh_composition_id', None),
        'target_document_id': 'capture-doc',
        'source_scene_revision_id': source.revision_id,
        'world_to_scene_authority': _identity_alignment(
            capture, overrides.pop('space_id', SPACE_ID)
        ),
        'readiness_policy': 'allow_blocked_semantic_authority',
        'reason': 'retain imported capture mesh as semantic authority',
    }
    kwargs.update(overrides)
    return make_capture_semantic_promotion_request(**kwargs)


def test_run_identity_separates_lineage_from_processing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#413: the same lineage ingested by two supported ingestor versions
    persists two distinct runs that share one lineage digest."""
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    capture = CaptureIngestionRepository(scene)
    monkeypatch.setitem(
        SUPPORTED_INGESTOR_IDENTITIES,
        ('htdt-capture-reference-ingestor', '2.0.0'),
        'f' * 64,
    )
    plan_v2, payloads_v2 = _ingestion_fixture(
        ingestor_version='2.0.0',
        anchors=((ANCHOR_A, 0.0), (ANCHOR_B, 1.0)),
    )
    plan_v2['ingestor']['configuration_digest'] = 'f' * 64

    plan_v1, payloads_v1 = _ingestion_fixture(
        anchors=((ANCHOR_A, 0.0), (ANCHOR_B, 1.0))
    )
    first = capture.ingest(plan_v1, payloads_v1)
    second = capture.ingest(plan_v2, payloads_v2)

    assert first.created and second.created
    assert first.ingestion_run_id != second.ingestion_run_id
    assert first.lineage_digest == second.lineage_digest

    runs = capture.list_ingestion_runs(lineage_digest=first.lineage_digest)
    assert {run.ingestion_run_id for run in runs} == {
        first.ingestion_run_id,
        second.ingestion_run_id,
    }
    run = capture.get_ingestion_run(second.ingestion_run_id)
    assert run is not None
    assert run.ingestor_version == '2.0.0'
    assert run.lineage_digest == first.lineage_digest
    plan = capture.get_ingestion_run_plan(second.ingestion_run_id)
    assert plan is not None
    assert plan.ingestor.version == '2.0.0'

    for run_id in (first.ingestion_run_id, second.ingestion_run_id):
        assert len(capture.mesh_binding_ids_for_run(run_id)) == 2
    assert (
        set(capture.mesh_binding_ids_for_lineage(first.lineage_digest))
        == set(capture.mesh_binding_ids_for_run(first.ingestion_run_id))
        | set(capture.mesh_binding_ids_for_run(second.ingestion_run_id))
    )

    # Idempotent: re-ingesting the exact same plan finds the same run.
    repeat = capture.ingest(plan_v2, payloads_v2)
    assert repeat.ingestion_run_id == second.ingestion_run_id
    assert not repeat.created


def test_coordinate_authority_scopes_space_uuid_to_immutable_bundle(
    tmp_path: Path,
) -> None:
    """#365: the same coordinate-space UUID in two bundles registers two
    distinct authorities keyed by (bundle_digest, coordinate_space_id)."""
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    capture = CaptureIngestionRepository(scene)
    capture.ingest(*_ingestion_fixture())
    capture.ingest(
        *_ingestion_fixture(bundle_digest=OTHER_BUNDLE_DIGEST)
    )

    first = capture.coordinate_authority_for(BUNDLE_DIGEST, SPACE_ID)
    second = capture.coordinate_authority_for(OTHER_BUNDLE_DIGEST, SPACE_ID)
    assert first is not None and second is not None
    assert first.coordinate_authority_id != second.coordinate_authority_id
    assert first.bundle_digest == BUNDLE_DIGEST
    assert second.bundle_digest == OTHER_BUNDLE_DIGEST
    assert first.coordinate_space_id == second.coordinate_space_id == SPACE_ID

    assert capture.get_coordinate_authority(
        first.coordinate_authority_id
    ) == first
    assert {
        authority.coordinate_authority_id
        for authority in capture.list_coordinate_authorities()
    } == {first.coordinate_authority_id, second.coordinate_authority_id}
    assert capture.coordinate_authority_for(
        'c' * 64, SPACE_ID
    ) is None


def test_capture_library_discovers_revisions_after_restart(
    tmp_path: Path,
) -> None:
    """#353: imported revisions are enumerable, queryable, and carry
    parent/topology plus per-revision counts after a full reopen."""
    path = tmp_path / 'cad.sqlite3'
    parent_id = '30000000-0000-4000-8000-000000000010'
    SceneRepository(path)
    capture = CaptureIngestionRepository(SceneRepository(path))
    capture.ingest(*_ingestion_fixture())
    plan2, payloads2 = _ingestion_fixture(
        revision_id='30000000-0000-4000-8000-000000000011',
        parent_revision_id=parent_id,
        bundle_digest='d' * 64,
    )
    capture.ingest(plan2, payloads2)

    # Reopen from disk: the library surface is rebuilt purely from
    # persisted run/link rows, never from payload BLOBs.
    reopened = CaptureIngestionRepository(SceneRepository(path))
    revisions = reopened.list_capture_revisions()
    ids = [rev.capture_revision_id for rev in revisions]
    assert ids == sorted(ids)
    assert set(ids) == {
        REVISION_ID,
        '30000000-0000-4000-8000-000000000011',
    }

    first = reopened.get_capture_revision(REVISION_ID)
    assert first is not None
    assert first.capture_series_id == SERIES_ID
    assert first.bundle_digest == BUNDLE_DIGEST
    assert first.parent_revision_id is None
    # A root revision has no unresolved parent.
    assert first.parent_known
    assert first.capture_session_ids == (SESSION_ID,)
    assert first.coordinate_space_ids == (SPACE_ID,)
    assert len(first.ingestion_run_ids) == 1
    assert first.lineage_digests
    assert first.source_evidence_count == 2
    assert len(first.raw_mesh_binding_ids) == 1
    assert first.authority_record_count == 0
    assert first.promotion_ids == ()

    second = reopened.get_capture_revision(
        '30000000-0000-4000-8000-000000000011'
    )
    assert second is not None
    # The declared parent is not persisted: unresolved-parent state is
    # explicit rather than silently treated as a root.
    assert second.parent_revision_id == parent_id
    assert not second.parent_known

    series = reopened.list_series_revisions(SERIES_ID)
    assert {rev.capture_revision_id for rev in series} == set(ids)
    assert reopened.get_capture_revision('f' * 36) is None

    # Bounded deterministic pagination: the keyset cursor reproduces the
    # tail of the ordering without repeating the first page.
    page_one = reopened.list_capture_revisions(limit=1)
    page_two = reopened.list_capture_revisions(
        limit=1, after_revision_id=page_one[0].capture_revision_id
    )
    assert [rev.capture_revision_id for rev in page_one + page_two] == ids

    by_revision = reopened.list_ingestion_runs(
        capture_revision_id=REVISION_ID
    )
    assert [run.ingestion_run_id for run in by_revision] == list(
        first.ingestion_run_ids
    )


def test_multi_anchor_composition_then_room_level_promotion(
    tmp_path: Path,
) -> None:
    """#351: several ARMeshAnchor bindings of one run compose into a single
    room-level geometry that promotes atomically."""
    scene, capture, promotion, run_id, binding_ids, _root = (
        _repositories(tmp_path)
    )
    assert len(binding_ids) == 2

    authority = capture.coordinate_authority_for(BUNDLE_DIGEST, SPACE_ID)
    request = make_capture_mesh_composition_request(
        ingestion_run_id=run_id,
        coordinate_authority_id=authority.coordinate_authority_id,
        raw_mesh_binding_ids=tuple(binding_ids),
    )
    result = promotion.compose(request)
    assert result.created
    assert result.triangle_count == 2
    assert result.vertex_count == 6

    repeated = promotion.compose(request)
    assert not repeated.created
    assert repeated.composed_mesh_id == result.composed_mesh_id

    composition = promotion.get_composition(result.composition_id)
    assert composition is not None
    assert composition.composed_mesh.mesh_id == result.composed_mesh_id
    assert composition.overlap_policy == 'retain_all_verbatim'
    # Anchor-local triangle namespaces survive into the composed mesh.
    primitives = {
        triangle.source_primitive
        for triangle in composition.composed_mesh.triangles
    }
    assert len(primitives) == 2
    assert all(
        primitive.startswith(tuple(binding_ids))
        for primitive in primitives
    )

    listed = promotion.list_compositions(ingestion_run_id=run_id)
    assert [item.composition_id for item in listed] == [
        result.composition_id
    ]
    inspection = promotion.inspect_capture_composition(
        mesh_composition_id=result.composition_id
    )
    assert len(inspection.triangle_ids) == 2

    source = scene.latest('capture-doc')
    promote_request = _promotion_request(
        capture,
        run_id,
        scene,
        mesh_composition_id=result.composition_id,
    )
    promoted = promotion.promote(promote_request)
    assert promoted.promotion_created
    geometry = scene.get(promoted.scene_revision_id).document
    geometry = geometry.r120_semantic_geometry
    assert geometry is not None
    # Composed mesh is already capture-world: the world-to-scene authority
    # applies verbatim rather than compounding an anchor transform.
    assert geometry.source_to_scene_transform.matrix_source_to_scene_m == (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    record = promotion.get_promotion(promoted.promotion_id)
    assert record is not None
    assert record.mesh_composition_id == result.composition_id
    assert record.raw_mesh_binding_id is None

    again = promotion.promote(promote_request)
    assert not again.promotion_created
    assert again.scene_revision_id == promoted.scene_revision_id


def test_composition_rejects_incompatible_coordinate_spaces(
    tmp_path: Path,
) -> None:
    """#351: bindings from different coordinate spaces fail closed."""
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    capture = CaptureIngestionRepository(scene)
    plan, payloads = _ingestion_fixture(
        anchors=((ANCHOR_A, 0.0), (ANCHOR_B, 1.0))
    )
    # Two anchors living in incompatible spaces cannot compose verbatim.
    plan['raw_visual_mesh_handoffs'][1]['coordinate_space_id'] = (
        '30000000-0000-4000-8000-000000000099'
    )
    plan['bundle']['coordinate_space_ids'] = [
        SPACE_ID,
        '30000000-0000-4000-8000-000000000099',
    ]
    # The handoff identity is bound to the payload, not the space, so the
    # plan's lineage digest is unchanged by this fixture edit only if the
    # digest excludes space — recompute to stay honest.
    lineage_projection = {
        'bundle_digest': BUNDLE_DIGEST,
        'source_evidence_ids': sorted(
            item['source_evidence_id'] for item in plan['source_evidence']
        ),
        'raw_visual_mesh_ids': sorted(
            item['raw_visual_mesh_handoff_id']
            for item in plan['raw_visual_mesh_handoffs']
        ),
        'authority_record_ids': [],
    }
    plan['lineage_digest'] = sha256(
        json.dumps(
            lineage_projection, sort_keys=True, separators=(',', ':')
        ).encode('utf-8')
    ).hexdigest()
    ingestion = capture.ingest(plan, payloads)
    binding_ids = sorted(
        capture.mesh_binding_ids_for_run(ingestion.ingestion_run_id)
    )
    authority = capture.coordinate_authority_for(BUNDLE_DIGEST, SPACE_ID)
    promotion = CaptureSemanticPromotionRepository(scene, capture)
    request = make_capture_mesh_composition_request(
        ingestion_run_id=ingestion.ingestion_run_id,
        coordinate_authority_id=authority.coordinate_authority_id,
        raw_mesh_binding_ids=tuple(binding_ids),
    )
    with pytest.raises(
        CaptureSemanticPromotionError, match='coordinate space'
    ):
        promotion.compose(request)


def test_conflict_policy_gates_scene_geometry_replacement(
    tmp_path: Path,
) -> None:
    """#359: promotion onto a revision already carrying geometry fails
    closed unless the explicit replace_exact policy names the exact prior
    geometry identity and semantic hash."""
    scene, capture, promotion, run_id, binding_ids, _root = (
        _repositories(tmp_path)
    )
    first = promotion.promote(
        _promotion_request(
            capture, run_id, scene,
            raw_mesh_binding_id=binding_ids[0],
        )
    )
    assert first.promotion_created
    geometry = scene.get(first.scene_revision_id).document
    prior = geometry.r120_semantic_geometry

    # Promoting again onto the new head (which now carries geometry)
    # without an explicit conflict policy fails closed.
    with pytest.raises(
        CaptureSemanticPromotionError,
        match='explicit geometry_conflict_policy',
    ):
        promotion.promote(
            _promotion_request(
                capture, run_id, scene,
                raw_mesh_binding_id=binding_ids[1],
            )
        )
    # A policy that names the wrong prior identity also fails closed.
    with pytest.raises(
        CaptureSemanticPromotionError,
        match='expected prior semantic geometry',
    ):
        promotion.promote(
            _promotion_request(
                capture, run_id, scene,
                raw_mesh_binding_id=binding_ids[1],
                geometry_conflict_policy='replace_exact',
                expected_prior_geometry_id=prior.geometry_id,
                expected_prior_geometry_semantic_hash='0' * 64,
            )
        )
    # Declaring the policy where no prior geometry exists fails closed.
    with pytest.raises(
        CaptureSemanticPromotionError,
        match='no semantic geometry to replace',
    ):
        empty = scene.save(
            SceneDocument(
                document_id='empty-doc',
                schema_version=4,
                room=None,
                entities=(),
            ),
            parent_revision_id=None,
        )
        promotion.promote(
            make_capture_semantic_promotion_request(
                ingestion_run_id=run_id,
                raw_mesh_binding_id=binding_ids[1],
                target_document_id='empty-doc',
                source_scene_revision_id=empty.revision.revision_id,
                world_to_scene_authority=_identity_alignment(
                    capture, SPACE_ID
                ),
                readiness_policy='allow_blocked_semantic_authority',
                reason='declared policy but nothing to replace',
                geometry_conflict_policy='replace_exact',
                expected_prior_geometry_id=prior.geometry_id,
                expected_prior_geometry_semantic_hash=(
                    prior.semantic_hash_sha256
                ),
            )
        )

    # The correctly-bound policy replaces and persists supersession.
    second_request = _promotion_request(
        capture, run_id, scene,
        raw_mesh_binding_id=binding_ids[1],
        geometry_conflict_policy='replace_exact',
        expected_prior_geometry_id=prior.geometry_id,
        expected_prior_geometry_semantic_hash=(
            prior.semantic_hash_sha256
        ),
    )
    second = promotion.promote(second_request)
    assert second.promotion_created
    record = promotion.get_promotion(second.promotion_id)
    assert record is not None
    assert record.prior_semantic_geometry_id == prior.geometry_id

    # Replaying the exact persisted request is idempotent: it must not be
    # re-gated by the conflict policy against the head it created.
    repeat = promotion.promote(second_request)
    assert not repeat.promotion_created
    assert repeat.scene_revision_id == second.scene_revision_id


def test_promotion_replay_reproduces_persisted_derivation(
    tmp_path: Path,
) -> None:
    """#368: replaying the persisted request against the exact source
    authorities reproduces the linked geometry bit for bit."""
    scene, capture, promotion, run_id, binding_ids, _root = (
        _repositories(tmp_path)
    )
    promoted = promotion.promote(
        _promotion_request(
            capture, run_id, scene,
            raw_mesh_binding_id=binding_ids[0],
        )
    )
    replay = promotion.verify_persisted_promotion(promoted.promotion_id)
    assert replay.verified
    assert replay.scene_revision_id == promoted.scene_revision_id
    assert replay.ingestion_run_id == run_id

    all_results = promotion.verify_all_promotions()
    assert [item.promotion_id for item in all_results] == [
        promoted.promotion_id
    ]

    # Unknown promotion id reports explicitly instead of passing silently.
    with pytest.raises(CapturePromotionReplayError, match='not persisted'):
        promotion.verify_persisted_promotion(
            'capture-semantic-promotion:' + '0' * 64
        )

    # A tampered persisted identity no longer matches the linked geometry.
    with closing(sqlite3.connect(scene.path)) as connection, connection:
        connection.execute(
            '''
            UPDATE capture_semantic_promotions
            SET semantic_geometry_id='semantic-geometry:tampered'
            WHERE promotion_id=?
            ''',
            (promoted.promotion_id,),
        )
    with pytest.raises(
        CapturePromotionReplayError, match='re-derived semantic geometry'
    ):
        promotion.verify_persisted_promotion(promoted.promotion_id)


def test_retention_inventory_plan_and_reference_safe_purge(
    tmp_path: Path,
) -> None:
    """#352: inventory counts the catalog; a dry-run plan reports
    dependents and bytes; purge deletes only when reference-safe and
    garbage-collects blobs after their last reference."""
    scene, capture, promotion, run_id, binding_ids, _root = (
        _repositories(tmp_path)
    )
    retention = CaptureRetentionService(scene)

    inventory = retention.inventory()
    assert inventory.ingestion_run_count == 1
    assert inventory.capture_revision_count == 1
    assert inventory.source_evidence_count == 3
    assert inventory.mesh_binding_count == 2
    assert inventory.coordinate_authority_count == 1
    assert inventory.content_blob_count == 3
    assert inventory.content_blob_bytes > 0

    # Nothing references the revision: the plan is ready, names the
    # deletable records, and measures reclaimable bytes.
    plan = retention.plan_capture_revision_purge(REVISION_ID)
    assert plan.status == 'ready'
    assert plan.ingestion_run_ids == (run_id,)
    assert len(plan.deletable_source_evidence_ids) == 3
    assert len(plan.deletable_mesh_binding_ids) == 2
    assert len(plan.deletable_coordinate_authority_ids) == 1
    assert plan.blocking_dependents == ()
    assert plan.reclaimable_bytes > 0
    assert len(plan.reclaimed_blob_sha256) == 3

    # A promotion on one of the run's bindings blocks the purge with the
    # exact dependent named.
    promoted = promotion.promote(
        _promotion_request(
            capture, run_id, scene,
            raw_mesh_binding_id=binding_ids[0],
        )
    )
    blocked = retention.plan_capture_revision_purge(REVISION_ID)
    assert blocked.status == 'blocked'
    assert [
        dep.identifier for dep in blocked.blocking_dependents
        if dep.kind == 'semantic_promotion'
    ] == [promoted.promotion_id]
    with pytest.raises(CaptureRetentionError, match='still has dependents'):
        retention.purge_capture_revision(REVISION_ID)
    assert capture.get_ingestion_run(run_id) is not None

    with closing(sqlite3.connect(scene.path)) as connection, connection:
        connection.execute(
            'DELETE FROM capture_semantic_promotions WHERE promotion_id=?',
            (promoted.promotion_id,),
        )
    # Purge a second, unrelated revision while the first is still
    # referenced — cross-revision sharing keeps shared blobs alive.
    plan2, payloads2 = _ingestion_fixture(
        bundle_digest='e' * 64,
        revision_id='30000000-0000-4000-8000-000000000012',
    )
    other = capture.ingest(plan2, payloads2)
    shared_sha = sha256(payloads2['mesh/anchors.json']).hexdigest()
    done = retention.purge_capture_revision(
        '30000000-0000-4000-8000-000000000012'
    )
    assert done.status == 'ready'
    assert capture.get_ingestion_run(other.ingestion_run_id) is None
    # The identical anchors.json payload survives in the blob store
    # because the remaining revision still references it.
    with closing(sqlite3.connect(scene.path)) as connection:
        blob_row = connection.execute(
            'SELECT 1 FROM htdt_content_blobs WHERE payload_sha256=?',
            (shared_sha,),
        ).fetchone()
    assert blob_row is not None

    with pytest.raises(CaptureRetentionError, match='not persisted'):
        retention.purge_capture_revision(
            '30000000-0000-4000-8000-000000000012'
        )
    absent = retention.plan_capture_revision_purge(
        '30000000-0000-4000-8000-000000000012'
    )
    assert absent.status == 'absent'


def test_provenance_trust_separates_asserted_labels_from_verified_origin(
    tmp_path: Path,
) -> None:
    """#412: a bundle self-asserting `backend_derived` is still only
    verified as an unsigned capture-bundle import — the forged label can
    never become authenticated provenance."""
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    capture = CaptureIngestionRepository(scene)
    forged = {
        'path': 'derived/backend.json',
        'payload': b'{"claimed":"backend-derived"}',
        'provenance_class': 'backend_derived',
        'role': 'derived',
    }
    plan, payloads = _ingestion_fixture(extra_source=forged)
    capture.ingest(plan, payloads)

    forged_id = next(
        record['source_evidence_id']
        for record in plan['source_evidence']
        if record['path'] == 'derived/backend.json'
    )
    trust = capture.provenance_trust(forged_id)
    assert trust is not None
    assert trust.asserted_provenance_class == 'backend_derived'
    assert trust.asserted_producer == 'mesh_capture'
    assert trust.verified_import_origin == 'unsigned_capture_bundle_import'
    assert not trust.producer_authenticated

    mesh_id = next(
        record['source_evidence_id']
        for record in plan['source_evidence']
        if record['path'].endswith('.meshbin')
    )
    mesh_trust = capture.provenance_trust(mesh_id)
    assert mesh_trust is not None
    assert mesh_trust.asserted_provenance_class == 'arkit_mesh_reconstruction'
    assert mesh_trust.verified_import_origin == (
        'unsigned_capture_bundle_import'
    )

    assert capture.provenance_trust('f' * 64) is None
