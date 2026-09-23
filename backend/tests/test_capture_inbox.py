"""Capture Inbox staging/classification/promotion contract (issue #589)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import uuid

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
    CaptureRevisionConflictError,
)
from htdt.capture_inbox import (
    CAPTURE_INBOX_UNASSIGNED_SCOPE,
    CaptureInboxError,
    CaptureInboxRepository,
    plan_authority_kinds,
)
from htdt.semantic_geometry import SemanticCoordinateTransform


SERIES_ID = '10000000-0000-4000-8000-000000000001'
SESSION_ID = '10000000-0000-4000-8000-000000000003'
SPACE_ID = '10000000-0000-4000-8000-000000000004'
ANCHOR_ID = '10000000-0000-4000-8000-000000000005'


def _plan_and_payloads(
    tmp_path: Path | None = None,
    *,
    revision_id: str | None = None,
    parent_revision_id: str | None = None,
    series_id: str = SERIES_ID,
    session_id: str = SESSION_ID,
    created_at: str = '2026-09-20T00:00:00Z',
) -> tuple[dict, dict[str, bytes]]:
    """Stage a contract-valid bundle: the four foundation payloads plus one
    mesh anchor, with the caller's revision/series/session identities."""
    workdir = (
        tmp_path if tmp_path is not None else Path(tempfile.mkdtemp())
    )
    revision_id = revision_id or str(uuid.uuid4())
    geometry_path = f'mesh/geometry/{ANCHOR_ID}.meshbin'
    manifest_overrides = {
        'capture_series_id': series_id,
        'capture_revision_id': revision_id,
        'parent_revision_id': parent_revision_id,
        'capture_session_ids': [session_id],
        'coordinate_space_ids': [SPACE_ID],
        'created_at': created_at,
        'finalized_at': created_at,
    }
    plan, payloads, _manifest = support.plan_and_payloads(
        workdir,
        files=support.mesh_specs_files(
            ((ANCHOR_ID, geometry_path, 3, 1),),
            anchor_transform=(
                1, 0, 0, 0,
                0, 1, 0, 0,
                0, 0, 1, 0,
                0, 0, 0, 1,
            ),
            session_id=session_id,
            space_id=SPACE_ID,
        ),
        manifest_overrides=manifest_overrides,
        id_map={
            support.SESSION_ID: session_id,
            support.SPACE_ID: SPACE_ID,
        },
    )
    return plan, payloads


def _rig(tmp_path: Path):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    return ingestion, inbox


def _stage_one(ingestion, inbox, tmp_path, **kwargs):
    plan, payloads = _plan_and_payloads(tmp_path, **kwargs)
    ingestion.ingest(plan, payloads)
    typed = CaptureIngestionPlan.model_validate(plan)
    result = inbox.stage(
        typed,
        arrival_source='file_import',
        source_detail='/captures/a.htdtcapture',
    )
    return result, typed


class TestStaging:
    def test_stage_creates_pending_item(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        result, typed = _stage_one(ingestion, inbox, tmp_path)
        assert result.created and result.outcome == 'staged'
        item = result.item
        assert item.inbox_item_id.startswith('capture-inbox-item:')
        assert item.scope == CAPTURE_INBOX_UNASSIGNED_SCOPE
        assert item.disposition == 'pending'
        assert item.bundle_validation == 'validated'
        assert item.primary_classification == 'new_series'
        assert item.arrival_count == 1

    def test_stage_requires_persisted_ingestion(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        plan, _payloads = _plan_and_payloads()
        with pytest.raises(CaptureInboxError, match='persisted'):
            inbox.stage(
                CaptureIngestionPlan.model_validate(plan),
                arrival_source='file_import',
            )

    def test_redelivery_is_already_staged_not_duplicated(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        plan, payloads = _plan_and_payloads()
        ingestion.ingest(plan, payloads)
        typed = CaptureIngestionPlan.model_validate(plan)
        first = inbox.stage(typed, arrival_source='paired_receiver')
        second = inbox.stage(typed, arrival_source='paired_receiver')
        assert second.outcome == 'already_staged'
        assert not second.created
        assert second.item.inbox_item_id == first.item.inbox_item_id
        assert second.item.arrival_count == 2
        assert len(inbox.list_items()) == 1

    def test_same_revision_different_digest_is_hard_conflict(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        revision = str(uuid.uuid4())
        _stage_one(ingestion, inbox, tmp_path, revision_id=revision)
        plan2, payloads2 = _plan_and_payloads(
            tmp_path,
            revision_id=revision,
            created_at='2026-09-20T00:00:02Z',
        )
        # The immutable revision registry rejects the reuse at ingest,
        # before the inbox could ever stage it (#335) — the conflict is
        # surfaced and recorded, never guessed at.
        with pytest.raises(CaptureRevisionConflictError):
            ingestion.ingest(plan2, payloads2)
        conflicts = ingestion.list_revision_conflicts()
        assert any(
            conflict.capture_revision_id == revision
            for conflict in conflicts
        )

    def test_out_of_order_arrival_fills_predecessor(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        rev_a, rev_b = str(uuid.uuid4()), str(uuid.uuid4())
        child, _ = _stage_one(
            ingestion, inbox, tmp_path,
            revision_id=rev_b, parent_revision_id=rev_a,
        )
        # out of order: parent arrives after its known child
        parent, _ = _stage_one(
            ingestion, inbox, tmp_path, revision_id=rev_a
        )
        assert parent.item.primary_classification == 'fills_missing_predecessor'
        comparison = inbox.compare(child.lineage_digest)
        assert comparison.predecessor_lineage_digest == parent.lineage_digest
        assert comparison.relationship == 'direct_child'

    def test_head_extension_and_branch_classification(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        rev_a = str(uuid.uuid4())
        a, _ = _stage_one(ingestion, inbox, tmp_path, revision_id=rev_a)
        b, _ = _stage_one(
            ingestion, inbox, tmp_path,
            revision_id=str(uuid.uuid4()), parent_revision_id=rev_a,
        )
        assert b.item.primary_classification == 'extends_known_head'
        # a second child of the same parent continues an already-known branch
        c, _ = _stage_one(
            ingestion, inbox, tmp_path,
            revision_id=str(uuid.uuid4()), parent_revision_id=rev_a,
        )
        assert c.item.primary_classification == 'continues_branch'
        # a series member whose parent is unknown is a new branch head
        orphan, _ = _stage_one(
            ingestion, inbox, tmp_path,
            revision_id=str(uuid.uuid4()),
            parent_revision_id=str(uuid.uuid4()),
        )
        assert orphan.item.primary_classification == 'parallel_branch_head'

    def test_unrelated_series_classified_new(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        _stage_one(ingestion, inbox, tmp_path)
        other, _ = _stage_one(
            ingestion, inbox, tmp_path, series_id=str(uuid.uuid4())
        )
        assert other.item.primary_classification == 'new_series'

    def test_arrival_order_does_not_set_precedence(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        rev_a, rev_b = str(uuid.uuid4()), str(uuid.uuid4())
        b, _ = _stage_one(
            ingestion, inbox, tmp_path,
            revision_id=rev_b, parent_revision_id=rev_a,
        )
        # B arrived first but is still classified against the graph, not order
        assert b.item.primary_classification == 'parallel_branch_head'
        a, _ = _stage_one(ingestion, inbox, tmp_path, revision_id=rev_a)
        assert a.item.primary_classification == 'fills_missing_predecessor'


class TestInspectionAndFacets:
    def test_inspect_reports_independent_facets(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)
        inspection = inbox.inspect(typed.lineage_digest)
        item = inspection.item
        assert item.dependency_state == 'not_evaluated'
        assert item.alignment_state == 'not_required'
        assert item.evidence_conflict_state == 'none'
        assert inspection.promotability == 'promotable'
        assert set(inspection.available_authority_kinds) == {
            'raw_visual_evidence',
            'semantic_geometry',
        }

    def test_facets_update_independently(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)
        inbox.set_dependency_state(
            typed.lineage_digest, 'unresolved', 'missing prior revision'
        )
        item = inbox.get(typed.lineage_digest)
        assert item.dependency_state == 'unresolved'
        assert item.evidence_conflict_state == 'none'
        assert inbox.inspect(typed.lineage_digest).promotability == 'blocked'
        inbox.set_dependency_state(typed.lineage_digest, 'satisfied')
        assert (
            inbox.inspect(typed.lineage_digest).promotability == 'promotable'
        )

    def test_defer_resume_reject_cycle(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)
        item = inbox.defer(typed.lineage_digest, 'waiting on rescan')
        assert item.disposition == 'deferred'
        item = inbox.resume(typed.lineage_digest)
        assert item.disposition == 'pending'
        item = inbox.reject(typed.lineage_digest, 'wrong room')
        assert item.disposition == 'rejected'
        with pytest.raises(CaptureInboxError, match='rejected'):
            inbox.record_promotion(
                typed.lineage_digest, 'raw_visual_evidence', 'authority:x'
            )
        item = inbox.resume(typed.lineage_digest)
        assert item.disposition == 'pending'


class TestPromotion:
    def test_granular_promotion_records_per_authority(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)
        records = inbox.promote(
            typed.lineage_digest,
            ['raw_visual_evidence'],
            reason='evidence first',
            executor=lambda plan, kind: f'promoted-{kind}',
        )
        assert len(records) == 1
        assert records[0].outcome == 'promoted'
        item = inbox.get(typed.lineage_digest)
        assert item.disposition == 'partially_promoted'
        inspection = inbox.inspect(typed.lineage_digest)
        assert inspection.promotability == 'partially_promotable'
        assert inspection.promoted_authority_kinds == ('raw_visual_evidence',)

    def test_all_kinds_promoted_flips_disposition(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)
        inbox.promote(
            typed.lineage_digest,
            ['raw_visual_evidence', 'semantic_geometry'],
            reason='full promote',
            executor=lambda plan, kind: f'promoted-{kind}',
        )
        assert inbox.get(typed.lineage_digest).disposition == 'promoted'
        assert (
            inbox.inspect(typed.lineage_digest).promotability == 'complete'
        )

    def test_partial_promotion_records_blocked_with_reason(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)

        def executor(plan, kind):
            if kind == 'semantic_geometry':
                raise RuntimeError('mesh exceeds ceiling tolerance')
            return f'promoted-{kind}'

        records = inbox.promote(
            typed.lineage_digest,
            ['raw_visual_evidence', 'semantic_geometry'],
            reason='selective',
            executor=executor,
        )
        outcomes = {record.authority_kind: record.outcome for record in records}
        assert outcomes == {
            'raw_visual_evidence': 'promoted',
            'semantic_geometry': 'blocked',
        }
        blocked = [
            record for record in records if record.outcome == 'blocked'
        ]
        assert 'mesh exceeds ceiling tolerance' in blocked[0].detail
        inspection = inbox.inspect(typed.lineage_digest)
        assert inspection.blocked_authority_kinds == ('semantic_geometry',)

    def test_promotion_blocked_by_unresolved_conflict(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)
        inbox.set_evidence_conflict_state(
            typed.lineage_digest, 'open', 'contradictory roomplan'
        )
        with pytest.raises(CaptureInboxError, match='conflict'):
            inbox.promote(
                typed.lineage_digest,
                ['raw_visual_evidence'],
                reason='x',
                executor=lambda p, k: 'id',
            )

    def test_promotion_of_absent_kind_records_blocked(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)
        with pytest.raises(CaptureInboxError, match='not present'):
            inbox.record_promotion(
                typed.lineage_digest, 'connected_space', 'authority:x'
            )
        blocked = inbox.record_blocked(
            typed.lineage_digest, 'connected_space', 'no connected doc'
        )
        assert blocked.outcome == 'blocked'

    def test_promotion_records_are_idempotent(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)
        first = inbox.record_promotion(
            typed.lineage_digest, 'raw_visual_evidence', 'authority:one'
        )
        second = inbox.record_promotion(
            typed.lineage_digest, 'raw_visual_evidence', 'authority:one'
        )
        assert first.promotion_record_id == second.promotion_record_id
        assert len(inbox.promotions_for(typed.lineage_digest)) == 1


class TestSupersession:
    def test_supersession_is_authority_specific(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        rev_a, rev_b = str(uuid.uuid4()), str(uuid.uuid4())
        a, _ = _stage_one(ingestion, inbox, tmp_path, revision_id=rev_a)
        b, _ = _stage_one(
            ingestion, inbox, tmp_path,
            revision_id=rev_b, parent_revision_id=rev_a,
        )
        inbox.promote(
            a.lineage_digest,
            ['raw_visual_evidence', 'semantic_geometry'],
            reason='v1',
            executor=lambda p, k: f'auth-a-{k}',
        )
        inbox.supersede(
            a.lineage_digest, b.lineage_digest,
            'semantic_geometry', reason='rescan replaces geometry',
        )
        # one authority kind superseded -> item not fully superseded
        assert inbox.get(a.lineage_digest).disposition == 'promoted'
        inbox.supersede(
            a.lineage_digest, b.lineage_digest,
            'raw_visual_evidence', reason='rescan replaces evidence',
        )
        assert inbox.get(a.lineage_digest).disposition == 'superseded'

    def test_supersede_requires_both_staged(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)
        with pytest.raises(CaptureInboxError, match='both items'):
            inbox.supersede(
                typed.lineage_digest, 'f' * 64,
                'raw_visual_evidence', reason='x',
            )


class TestComparison:
    def test_compare_reports_structure_not_spatial(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        rev_a = str(uuid.uuid4())
        a, typed_a = _stage_one(ingestion, inbox, tmp_path, revision_id=rev_a)
        b, typed_b = _stage_one(
            ingestion, inbox, tmp_path,
            revision_id=str(uuid.uuid4()), parent_revision_id=rev_a,
        )
        comparison = inbox.compare(b.lineage_digest)
        assert comparison.relationship == 'direct_child'
        assert comparison.source_evidence_count_self == len(
            typed_b.source_evidence
        )
        assert comparison.source_evidence_count_predecessor == len(
            typed_a.source_evidence
        )
        assert comparison.spatial_comparison == 'disabled_unaligned'
        assert 'disabled' in comparison.detail

    def test_registration_enables_diagnostic_comparison(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        rev_a = str(uuid.uuid4())
        a, _ = _stage_one(ingestion, inbox, tmp_path, revision_id=rev_a)
        b, _ = _stage_one(
            ingestion, inbox, tmp_path,
            revision_id=str(uuid.uuid4()), parent_revision_id=rev_a,
        )
        identity = SemanticCoordinateTransform(
            matrix_source_to_scene_m=(
                (1.0, 0.0, 0.0, 0.0),
                (0.0, 1.0, 0.0, 0.0),
                (0.0, 0.0, 1.0, 0.0),
                (0.0, 0.0, 0.0, 1.0),
            ),
            provenance='explicit_user_authority',
            reason='matched door frames',
        )
        registration = inbox.register_cross_revision_alignment(
            a.lineage_digest, b.lineage_digest, identity,
            alignment_method='identity',
        )
        assert registration.transform_class == 'rigid'
        comparison = inbox.compare(b.lineage_digest)
        assert (
            comparison.alignment_state
            == 'cross_revision_registration_present'
        )
        assert comparison.spatial_comparison == 'diagnostic_only'

    def test_registration_rejects_shear(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        rev_a = str(uuid.uuid4())
        a, _ = _stage_one(ingestion, inbox, tmp_path, revision_id=rev_a)
        b, _ = _stage_one(
            ingestion, inbox, tmp_path,
            revision_id=str(uuid.uuid4()), parent_revision_id=rev_a,
        )
        shear = SemanticCoordinateTransform(
            matrix_source_to_scene_m=(
                (1.0, 0.5, 0.0, 0.0),
                (0.0, 1.0, 0.0, 0.0),
                (0.0, 0.0, 1.0, 0.0),
                (0.0, 0.0, 0.0, 1.0),
            ),
            provenance='explicit_user_authority',
            reason='x',
        )
        from htdt.capture_semantic_promotion import CaptureAlignmentError

        with pytest.raises(CaptureAlignmentError):
            inbox.register_cross_revision_alignment(
                a.lineage_digest, b.lineage_digest, shear,
            )

    def test_compare_no_predecessor(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)
        comparison = inbox.compare(typed.lineage_digest)
        assert comparison.relationship == 'no_predecessor'
        assert comparison.predecessor_lineage_digest is None


class TestScopeAndNotes:
    def test_assign_scope_and_notes(self, tmp_path):
        ingestion, inbox = _rig(tmp_path)
        typed, _ = _stage_one(ingestion, inbox, tmp_path)
        item = inbox.assign_scope(typed.lineage_digest, 'document-1')
        assert item.scope == 'document-1'
        item = inbox.add_operator_note(
            typed.lineage_digest, 'verify doorway alignment'
        )
        assert 'verify doorway alignment' in item.operator_notes
        scoped = inbox.list_items(scope='document-1')
        assert [i.lineage_digest for i in scoped] == [typed.lineage_digest]
