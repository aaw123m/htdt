"""Connected-space promotion contract (issue #658)."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import struct
import uuid

import pytest

from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from htdt.capture_connected_space import (
    CaptureConnectedSpaceDocument,
    ConnectedSpacePromotionError,
    ConnectedSpacePromotionRepository,
)


SERIES_ID = '10000000-0000-4000-8000-000000000001'
SESSION_ID = '10000000-0000-4000-8000-000000000003'
SPACE_ID = '10000000-0000-4000-8000-000000000004'
ANCHOR_ID = '10000000-0000-4000-8000-000000000005'
INGESTOR_CONFIG = (
    '3e27eec298714a04fc6b48d94b354168396e2c4eea0cf9aa8284fa552de562b3'
)
REVISION_ID = '10000000-0000-4000-8000-000000000002'
REGION_THEATER = '10000000-0000-4000-8000-000000000011'
REGION_HALLWAY = '10000000-0000-4000-8000-000000000012'
REGION_STAIRS = '10000000-0000-4000-8000-000000000013'
PORTAL_DOOR = '10000000-0000-4000-8000-000000000021'
PORTAL_STAIR = '10000000-0000-4000-8000-000000000022'


def _hash_parts(prefix: str, *parts: str) -> str:
    digest = sha256(prefix.encode('utf-8'))
    for part in parts:
        digest.update(b'\x00')
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def _meshbin() -> bytes:
    vertices = b''.join(
        struct.pack('<3f', float(i), float(i % 7) * 0.5, float(i % 3) * 0.25)
        for i in range(3)
    )
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
    return header + vertices + struct.pack('<3I', 0, 1, 2)


def _connected_doc(
    *,
    revision_id: str = REVISION_ID,
    session_id: str = SESSION_ID,
    space_id: str = SPACE_ID,
    segments: tuple | None = None,
    portals: tuple | None = None,
) -> bytes:
    if segments is None:
        segments = (
            {
                'region_id': REGION_THEATER,
                'label': 'Theater',
                'kind': 'room',
                'state': 'completed',
                'coordinate_space_id': space_id,
                'capture_session_id': session_id,
                'evidence_refs': ['session/scan.lidar'],
                'revisit_count': 2,
            },
            {
                'region_id': REGION_HALLWAY,
                'label': 'Hallway',
                'kind': 'hallway',
                'state': 'completed',
                'coordinate_space_id': space_id,
                'capture_session_id': session_id,
                'evidence_refs': [],
                'revisit_count': 0,
            },
            {
                'region_id': REGION_STAIRS,
                'label': 'Stairwell',
                'kind': 'stairwell',
                'state': 'active',
                'coordinate_space_id': space_id,
                'capture_session_id': session_id,
                'evidence_refs': [],
                'revisit_count': 1,
            },
        )
    if portals is None:
        portals = (
            {
                'portal_id': PORTAL_DOOR,
                'region_a_id': REGION_THEATER,
                'region_b_id': REGION_HALLWAY,
                'kind': 'doorway',
                'coordinate_space_id': space_id,
                'label': 'Theater door',
                'evidence_refs': ['session/scan.lidar#frame1'],
            },
            {
                'portal_id': PORTAL_STAIR,
                'region_a_id': REGION_HALLWAY,
                'region_b_id': REGION_STAIRS,
                'kind': 'stair_opening',
                'coordinate_space_id': space_id,
                'label': 'Stair opening',
                'evidence_refs': [],
            },
        )
    doc = {
        'schema': 'htdt.capture.connected-spaces',
        'schema_version': '1.0.0',
        'capture_revision_id': revision_id,
        'capture_session_id': session_id,
        'coordinate_space_id': space_id,
        'segments': list(segments),
        'portals': list(portals),
    }
    return json.dumps(doc).encode('utf-8')


def _plan_and_payloads(
    *,
    revision_id: str = REVISION_ID,
    bundle_digest: str = '2' * 64,
    connected_payload: bytes | None = None,
    space_id: str = SPACE_ID,
) -> tuple[dict, dict[str, bytes]]:
    payloads = {
        'mesh/anchors.json': b'{"fixture":"anchors"}',
        f'mesh/geometry/{ANCHOR_ID}.meshbin': _meshbin(),
    }
    if connected_payload is not None:
        payloads['session/connected-spaces.json'] = connected_payload
    source = []
    source_by_path = {}
    for path in sorted(payloads):
        payload = payloads[path]
        digest = sha256(payload).hexdigest()
        record = {
            'source_evidence_id': _hash_parts(
                'htdt.capture.source-evidence.v1', bundle_digest, path, digest
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
            'provenance_class': 'arkit_mesh_reconstruction',
            'role': 'canonical',
            'source_refs': [],
        }
        source.append(record)
        source_by_path[path] = record
    geometry = source_by_path[f'mesh/geometry/{ANCHOR_ID}.meshbin']
    handoffs = [
        {
            'raw_visual_mesh_handoff_id': _hash_parts(
                'htdt.capture.raw-visual-mesh-handoff.v1',
                bundle_digest,
                ANCHOR_ID,
                geometry['payload_sha256'],
            ),
            'bundle_digest': bundle_digest,
            'anchor_id': ANCHOR_ID,
            'anchor_record_locator': f'mesh/anchors.json#anchor:{ANCHOR_ID}',
            'anchor_index_source_evidence_id': source_by_path[
                'mesh/anchors.json'
            ]['source_evidence_id'],
            'geometry_source_evidence_id': geometry['source_evidence_id'],
            'geometry_path': f'mesh/geometry/{ANCHOR_ID}.meshbin',
            'geometry_sha256': geometry['payload_sha256'],
            'capture_session_id': SESSION_ID,
            'coordinate_space_id': space_id,
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
    ]
    projection = {
        'bundle_digest': bundle_digest,
        'source_evidence_ids': sorted(
            item['source_evidence_id'] for item in source
        ),
        'raw_visual_mesh_ids': sorted(
            handoff['raw_visual_mesh_handoff_id'] for handoff in handoffs
        ),
        'authority_record_ids': [],
    }
    lineage_digest = sha256(
        json.dumps(projection, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    plan = {
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
            'capture_revision_id': revision_id,
            'parent_revision_id': None,
            'capture_session_ids': [SESSION_ID],
            'coordinate_space_ids': [space_id],
        },
        'source_evidence': source,
        'roomplan_records': [],
        'raw_visual_mesh_handoffs': handoffs,
        'authority_records': [],
        'lineage_digest': lineage_digest,
    }
    return plan, payloads


def _rig(tmp_path: Path):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    connected = ConnectedSpacePromotionRepository(scene, ingestion)
    return ingestion, connected


def _ingested_plan(
    ingestion, *, revision_id=REVISION_ID, bundle_digest='2' * 64,
    with_doc=True,
):
    doc = _connected_doc(revision_id=revision_id) if with_doc else None
    plan, payloads = _plan_and_payloads(
        revision_id=revision_id,
        bundle_digest=bundle_digest,
        connected_payload=doc,
    )
    ingestion.ingest(plan, payloads)
    return CaptureIngestionPlan.model_validate(plan), doc


class TestWireModel:
    def test_document_round_trip(self):
        doc = CaptureConnectedSpaceDocument.model_validate_json(_connected_doc())
        assert doc.capture_revision_id == REVISION_ID
        assert len(doc.segments) == 3
        assert doc.connected_document_id.startswith('capture-connected-space:')

    def test_portal_endpoints_must_be_known_regions(self):
        with pytest.raises(Exception, match='known regions'):
            CaptureConnectedSpaceDocument.model_validate_json(
                _connected_doc(
                    portals=(
                        {
                            'portal_id': PORTAL_DOOR,
                            'region_a_id': REGION_THEATER,
                            'region_b_id': str(uuid.uuid4()),
                            'kind': 'doorway',
                            'coordinate_space_id': SPACE_ID,
                            'label': '',
                            'evidence_refs': [],
                        },
                    )
                )
            )

    def test_empty_segments_rejected(self):
        with pytest.raises(Exception, match='no segments'):
            CaptureConnectedSpaceDocument.model_validate_json(
                _connected_doc(segments=(), portals=())
            )

    def test_wrong_schema_version_rejected(self):
        doc = json.loads(_connected_doc())
        doc['schema_version'] = '9.9.9'
        with pytest.raises(Exception, match='schema_version'):
            CaptureConnectedSpaceDocument.model_validate_json(
                json.dumps(doc).encode()
            )

    def test_segment_in_other_space_rejected(self):
        doc = json.loads(_connected_doc())
        doc['segments'][0]['coordinate_space_id'] = str(uuid.uuid4())
        with pytest.raises(Exception, match='coordinate space'):
            CaptureConnectedSpaceDocument.model_validate_json(
                json.dumps(doc).encode()
            )


class TestStaging:
    def test_stage_links_document_to_ingestion(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        staged = connected.stage_connected_document(plan, doc)
        assert staged.created
        docs = connected.connected_documents_for_lineage(plan.lineage_digest)
        assert len(docs) == 1
        summary = connected.connected_space_summary(plan.lineage_digest)
        assert summary['region_count'] == 3
        assert summary['portal_count'] == 2
        assert summary['topology_only_portals'] == 2

    def test_stage_is_idempotent(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        first = connected.stage_connected_document(plan, doc)
        second = connected.stage_connected_document(plan, doc)
        assert first.connected_document_id == second.connected_document_id
        assert not second.created

    def test_stage_rejects_mismatched_revision(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, _doc = _ingested_plan(ingestion)
        other_doc = _connected_doc(revision_id=str(uuid.uuid4()))
        with pytest.raises(ConnectedSpacePromotionError, match='revision'):
            connected.stage_connected_document(plan, other_doc)

    def test_stage_rejects_unbound_coordinate_space(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, _doc = _ingested_plan(ingestion)
        other = _connected_doc(space_id=str(uuid.uuid4()))
        with pytest.raises(ConnectedSpacePromotionError, match='coordinate space'):
            connected.stage_connected_document(plan, other)


class TestPromotion:
    def test_promote_creates_distinct_regions_and_portals(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        staged = connected.stage_connected_document(plan, doc)
        model = connected.promote_connected_space(
            document_id='doc-1',
            connected_document_id=staged.connected_document_id,
            world_to_scene_authority_id='capture-world-to-scene:' + 'a' * 64,
            reason='promote connected set',
        )
        assert model.revision == 1
        assert len(model.regions) == 3
        assert len(model.portals) == 2
        # three distinct physical concepts — never flattened
        kinds = {r.kind for r in model.regions}
        assert kinds == {'room', 'hallway', 'stairwell'}
        # one shared alignment for the connected set
        assert model.world_to_scene_authority_id == (
            'capture-world-to-scene:' + 'a' * 64
        )
        # topology only: no fabricated opening geometry
        assert all(p.geometry_state == 'topology_known' for p in model.portals)
        assert all(p.resolved_opening_id is None for p in model.portals)
        # stair opening stays vertical
        stair_portal = next(p for p in model.portals if p.kind == 'stair_opening')
        assert stair_portal.preserves_vertical
        assert not model.simple_path

    def test_single_region_model_keeps_simple_path(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        doc = _connected_doc(
            segments=(
                {
                    'region_id': REGION_THEATER,
                    'label': 'Theater',
                    'kind': 'room',
                    'state': 'completed',
                    'coordinate_space_id': SPACE_ID,
                    'capture_session_id': SESSION_ID,
                    'evidence_refs': [],
                    'revisit_count': 0,
                },
            ),
            portals=(),
        )
        plan, payloads = _plan_and_payloads(connected_payload=doc)
        ingestion.ingest(plan, payloads)
        typed = CaptureIngestionPlan.model_validate(plan)
        staged = connected.stage_connected_document(typed, doc)
        model = connected.promote_connected_space(
            document_id='doc-single',
            connected_document_id=staged.connected_document_id,
            reason='single room',
        )
        assert model.simple_path

    def test_role_is_workflow_not_geometry(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        staged = connected.stage_connected_document(plan, doc)
        model = connected.promote_connected_space(
            document_id='doc-1',
            connected_document_id=staged.connected_document_id,
            reason='promote',
        )
        hallway = next(r for r in model.regions if r.kind == 'hallway')
        revised = connected.set_region_role(
            model.physical_space_model_id,
            hallway.physical_region_id,
            'primary_theater',
            reason='operator picks the theater — not the first segment',
        )
        assert revised.revision == 2
        assert revised.parent_model_id == model.physical_space_model_id
        marked = next(
            r for r in revised.regions
            if r.physical_region_id == hallway.physical_region_id
        )
        assert marked.role == 'primary_theater'
        assert marked.kind == 'hallway'  # kind preserved; role is a facet

    def test_portal_geometry_resolution_is_explicit(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        staged = connected.stage_connected_document(plan, doc)
        model = connected.promote_connected_space(
            document_id='doc-1',
            connected_document_id=staged.connected_document_id,
            reason='promote',
        )
        portal = next(p for p in model.portals if p.kind == 'doorway')
        report = connected.compilation_report(model.physical_space_model_id)
        assert not report.compilable  # topology-only portal fails closed
        revised = connected.resolve_portal_geometry(
            model.physical_space_model_id,
            portal.physical_portal_id,
            'physical-opening:' + '1' * 64,
            reason='measured rough opening',
        )
        resolved = next(
            p for p in revised.portals
            if p.physical_portal_id == portal.physical_portal_id
        )
        assert resolved.geometry_state == 'geometry_resolved'
        assert resolved.resolved_opening_id == 'physical-opening:' + '1' * 64

    def test_opening_state_composes_with_geometry(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        staged = connected.stage_connected_document(plan, doc)
        model = connected.promote_connected_space(
            document_id='doc-1',
            connected_document_id=staged.connected_document_id,
            reason='promote',
        )
        portal = model.portals[0]
        revised = connected.set_portal_opening_state(
            model.physical_space_model_id,
            portal.physical_portal_id,
            'closed',
            reason='door closed during measurement',
        )
        assert revised.portals[0].opening_state == 'closed'
        # a closed portal does not require opening geometry to compile
        report = connected.compilation_report(revised.physical_space_model_id)
        assert not any('topology-only' in r for r in report.blocked_reasons
                       if portal.label in r)


class TestMergeAndReconcile:
    def test_merge_is_explicit_and_reversible(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        staged = connected.stage_connected_document(plan, doc)
        model = connected.promote_connected_space(
            document_id='doc-1',
            connected_document_id=staged.connected_document_id,
            reason='promote',
        )
        theater = next(r for r in model.regions if r.kind == 'room')
        hallway = next(r for r in model.regions if r.kind == 'hallway')
        merged = connected.merge_regions(
            model.physical_space_model_id,
            (theater.physical_region_id, hallway.physical_region_id),
            kind='open_plan_area',
            reason='open-plan interpretation',
        )
        merged_region = next(
            r for r in merged.regions if r.merged_from_region_ids
        )
        assert set(merged_region.merged_from_region_ids) == {
            theater.physical_region_id, hallway.physical_region_id,
        }
        # originals preserved with lineage pointer
        orig = next(
            r for r in merged.regions
            if r.physical_region_id == theater.physical_region_id
        )
        assert orig.merged_into_region_id == merged_region.physical_region_id
        # the doorway portal between merged regions is absorbed
        assert all(
            p.capture_portal_id != PORTAL_DOOR for p in merged.portals
        )
        restored = connected.unmerge_region(
            merged.physical_space_model_id,
            merged_region.physical_region_id,
            reason='revert to separate spaces',
        )
        live = {
            r.physical_region_id
            for r in restored.regions
            if r.merged_into_region_id is None
        }
        assert theater.physical_region_id in live
        assert hallway.physical_region_id in live

    def test_partial_recapture_updates_only_touched_regions(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        staged = connected.stage_connected_document(plan, doc)
        model = connected.promote_connected_space(
            document_id='doc-1',
            connected_document_id=staged.connected_document_id,
            reason='promote',
        )
        theater = next(r for r in model.regions if r.kind == 'room')
        connected.set_region_role(
            model.physical_space_model_id,
            theater.physical_region_id,
            'primary_theater',
            reason='role',
        )
        model = connected.latest_model('doc-1')

        # hallway-only recapture arrives under a new revision
        revision2 = str(uuid.uuid4())
        recapture = _connected_doc(
            revision_id=revision2,
            segments=(
                {
                    'region_id': REGION_HALLWAY,
                    'label': 'Hallway',
                    'kind': 'hallway',
                    'state': 'completed',
                    'coordinate_space_id': SPACE_ID,
                    'capture_session_id': SESSION_ID,
                    'evidence_refs': ['session/rescan.lidar'],
                    'revisit_count': 5,
                },
            ),
            portals=(),
        )
        plan2, payloads2 = _plan_and_payloads(
            revision_id=revision2,
            bundle_digest='3' * 64,
            connected_payload=recapture,
        )
        ingestion.ingest(plan2, payloads2)
        typed2 = CaptureIngestionPlan.model_validate(plan2)
        staged2 = connected.stage_connected_document(typed2, recapture)

        revised = connected.reconcile_connected_space(
            model.physical_space_model_id,
            staged2.connected_document_id,
            reason='hallway recapture',
        )
        hallway = next(
            r for r in revised.regions if r.kind == 'hallway'
        )
        assert hallway.evidence_refs == ('session/rescan.lidar',)
        assert hallway.revisit_count == 5
        # theater untouched — role and evidence preserved
        theater2 = next(
            r for r in revised.regions if r.kind == 'room'
        )
        assert theater2.role == 'primary_theater'
        assert theater2.evidence_refs == theater.evidence_refs
        # portals from the original promotion remain
        assert len(revised.portals) == 2

    def test_recapture_in_other_space_fails_closed(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        staged = connected.stage_connected_document(plan, doc)
        model = connected.promote_connected_space(
            document_id='doc-1',
            connected_document_id=staged.connected_document_id,
            reason='promote',
        )
        other_space = str(uuid.uuid4())
        revision2 = str(uuid.uuid4())
        other_doc = _connected_doc(
            revision_id=revision2, space_id=other_space
        )
        plan2, payloads2 = _plan_and_payloads(
            revision_id=revision2,
            bundle_digest='4' * 64,
            connected_payload=other_doc,
            space_id=other_space,
        )
        ingestion.ingest(plan2, payloads2)
        typed2 = CaptureIngestionPlan.model_validate(plan2)
        staged2 = connected.stage_connected_document(typed2, other_doc)
        with pytest.raises(ConnectedSpacePromotionError, match='coordinate space'):
            connected.reconcile_connected_space(
                model.physical_space_model_id,
                staged2.connected_document_id,
                reason='different space',
            )

    def test_repeated_promotion_fails_closed(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        staged = connected.stage_connected_document(plan, doc)
        connected.promote_connected_space(
            document_id='doc-1',
            connected_document_id=staged.connected_document_id,
            reason='promote',
        )
        with pytest.raises(ConnectedSpacePromotionError, match='reconcile'):
            connected.promote_connected_space(
                document_id='doc-1',
                connected_document_id=staged.connected_document_id,
                reason='again',
            )


class TestMembership:
    def test_membership_by_semantic_identity(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        staged = connected.stage_connected_document(plan, doc)
        model = connected.promote_connected_space(
            document_id='doc-1',
            connected_document_id=staged.connected_document_id,
            reason='promote',
        )
        theater = next(r for r in model.regions if r.kind == 'room')
        revised = connected.assign_membership(
            model.physical_space_model_id,
            'entity:subwoofer-1',
            'region',
            theater.physical_region_id,
            reason='sub lives in theater',
        )
        membership = revised.memberships[-1]
        assert membership.entity_ref == 'entity:subwoofer-1'
        assert membership.target_id == theater.physical_region_id

    def test_membership_rejects_unknown_target(self, tmp_path):
        ingestion, connected = _rig(tmp_path)
        plan, doc = _ingested_plan(ingestion)
        staged = connected.stage_connected_document(plan, doc)
        model = connected.promote_connected_space(
            document_id='doc-1',
            connected_document_id=staged.connected_document_id,
            reason='promote',
        )
        with pytest.raises(Exception, match='live region'):
            connected.assign_membership(
                model.physical_space_model_id,
                'entity:ghost',
                'region',
                'physical-region:' + 'f' * 64,
                reason='x',
            )
