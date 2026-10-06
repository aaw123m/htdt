"""REV60-COLLABENV regression tests — multi-user collaboration/approval
authority (#721) and acoustic-material environmental/aging
applicability (#776)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_collaboration import (
    ApprovalRecord,
    BranchProposal,
    ClientAcceptance,
    CollaborationActor,
    CollaborationEvent,
    ConflictResolution,
    InformationStateRecord,
    ProposalPromotion,
    ReviewDecision,
    RevisionAuthorship,
    SiblingDivergence,
    check_capability,
    compose_acceptance_verdict,
    evaluate_approval_currency,
    evaluate_divergence,
    evaluate_state_transition,
)
from htdt.cad_collaboration_repository import (
    CadCollaborationRepository,
    CollaborationIntegrityError,
)
from htdt.cad_material_condition import (
    AcousticMaterialConditionState,
    MaterialDurabilityEvidence,
    MaterialEvidenceApplicability,
    ReinspectionAssessment,
    evaluate_material_applicability,
    evaluate_reinspection_need,
)
from htdt.cad_material_condition_repository import (
    CadMaterialConditionRepository,
    MaterialConditionIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256


DOC = 'doc-collabenv'
_SHA = canonical_sha256({'fixture': 'sha'})
_OTHER_SHA = canonical_sha256({'fixture': 'other'})
_TS = '2026-10-06T00:00:00Z'


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _sealed(record: object, sha_field: str) -> bool:
    """Recompute the canonical seal from the identity payload."""
    return getattr(record, sha_field) == canonical_sha256(
        record.identity_payload()
    )


def _actor(
    roles: tuple[str, ...] = ('designer',), rid: str = 'actor-1'
) -> CollaborationActor:
    return CollaborationActor.create(
        document_id=DOC,
        display_label='作業者',
        roles=roles,
        registered_at_utc=_TS,
        note=rid,
    )


def _authorship(
    *,
    author: CollaborationActor,
    parent: str | None,
    result: str,
    changed: tuple[AuthorityRef, ...] = (),
    depends: tuple[AuthorityRef, ...] = (),
    scope: str = 'declared_refs',
    ts: str = _TS,
) -> RevisionAuthorship:
    return RevisionAuthorship.create(
        document_id=DOC,
        author_ref=AuthorityRef(
            kind='collaboration_actor',
            ref_id=author.actor_id,
            ref_sha256=author.actor_sha256,
        ),
        parent_revision_id=parent,
        result_revision_id=result,
        changed_refs=changed,
        depends_refs=depends,
        change_scope=scope,
        recorded_at_utc=ts,
    )


# ---------------------------------------------------------------------------
# #721 collaboration/approval authority


def test_actor_role_capability_least_privilege() -> None:
    designer = _actor(('designer',))
    assert check_capability(designer, 'edit_design')
    assert not check_capability(designer, 'approve')
    installer = _actor(('integrator_installer',))
    # Installers update as-built observations, never approved history.
    assert check_capability(installer, 'update_asbuilt')
    assert not check_capability(installer, 'edit_design')
    assert not check_capability(installer, 'promote_proposal')
    observer = _actor(('read_only_observer',))
    assert check_capability(observer, 'view')
    assert not check_capability(observer, 'comment')
    assert not check_capability(observer, 'edit_design')
    client = _actor(('client_stakeholder',))
    assert check_capability(client, 'client_accept')
    assert not check_capability(client, 'approve')
    with pytest.raises(ValueError):
        from htdt.cad_collaboration import require_capability

        require_capability(observer, 'edit_design')


def test_authorship_requires_sha_pinned_refs() -> None:
    actor = _actor()
    with pytest.raises(ValidationError):
        RevisionAuthorship.create(
            document_id=DOC,
            author_ref=AuthorityRef(
                kind='collaboration_actor', ref_id=actor.actor_id
            ),
            parent_revision_id='rev-0',
            result_revision_id='rev-1',
            changed_refs=(_ref('room', 'r1'),),
            recorded_at_utc=_TS,
        )


def test_authorship_declared_scope_needs_refs() -> None:
    actor = _actor()
    with pytest.raises(ValidationError):
        _authorship(
            author=actor, parent='rev-0', result='rev-1',
            changed=(), scope='declared_refs',
        )


# COL10 — two users edit different objects on the same parent.


def test_col10_independent_edits_merge_cleanly() -> None:
    actor = _actor()
    left = _authorship(
        author=actor, parent='rev-0', result='rev-1L',
        changed=(_ref('room', 'room-a'),),
        ts='2026-10-06T00:00:01Z',
    )
    right = _authorship(
        author=actor, parent='rev-0', result='rev-1R',
        changed=(_ref('speaker', 'sp-2'),),
        ts='2026-10-06T00:00:02Z',
    )
    verdict = evaluate_divergence(
        left, right, recorded_at_utc='2026-10-06T00:00:03Z'
    )
    assert verdict.verdict == 'divergent_mergeable'
    assert verdict.outcome == 'auto_merge_safe'
    assert verdict.base_revision_id == 'rev-0'
    assert _sealed(verdict, 'assessment_sha256')


# COL20 — same-object semantic conflict: text merge cannot hide it.


def test_col20_same_object_conflict_named() -> None:
    actor = _actor()
    room = _ref('room', 'room-a')
    left = _authorship(
        author=actor, parent='rev-0', result='rev-1L',
        changed=(room,), ts='2026-10-06T00:00:01Z',
    )
    right = _authorship(
        author=actor, parent='rev-0', result='rev-1R',
        changed=(room,), ts='2026-10-06T00:00:02Z',
    )
    verdict = evaluate_divergence(
        left, right, recorded_at_utc='2026-10-06T00:00:03Z'
    )
    assert verdict.verdict == 'divergent_conflicted'
    assert verdict.outcome == 'authority_conflict'
    assert {r.ref_id for r in verdict.overlap_refs} == {'room-a'}


# COL30 — dependency-aware staling: B's calibration depends on the
# geometry A just moved.


def test_col30_dependency_stale_detected() -> None:
    actor = _actor()
    geometry = _ref('room', 'room-a')
    left = _authorship(
        author=actor, parent='rev-0', result='rev-1L',
        changed=(geometry,), ts='2026-10-06T00:00:01Z',
    )
    right = _authorship(
        author=actor, parent='rev-0', result='rev-1R',
        changed=(_ref('calibration', 'cal-1'),),
        depends=(geometry,), ts='2026-10-06T00:00:02Z',
    )
    verdict = evaluate_divergence(
        left, right, recorded_at_utc='2026-10-06T00:00:03Z'
    )
    assert verdict.verdict == 'divergent_dependency_stale'
    assert verdict.outcome == 'dependency_stale'
    assert {r.ref_id for r in verdict.stale_dependency_refs} == {'room-a'}


# Undeclared scope cannot prove disjointness.


def test_undeclared_scope_forces_manual_review() -> None:
    actor = _actor()
    left = _authorship(
        author=actor, parent='rev-0', result='rev-1L',
        scope='whole_document', ts='2026-10-06T00:00:01Z',
    )
    right = _authorship(
        author=actor, parent='rev-0', result='rev-1R',
        changed=(_ref('speaker', 'sp-2'),), ts='2026-10-06T00:00:02Z',
    )
    verdict = evaluate_divergence(
        left, right, recorded_at_utc='2026-10-06T00:00:03Z'
    )
    assert verdict.outcome == 'manual_review_required'


# COL60 — offline divergence: ancestry, not wall-clock, reconciles.


def test_col60_offline_divergence_rebase_or_converge() -> None:
    actor = _actor()
    left = _authorship(
        author=actor, parent='rev-a', result='rev-1L',
        changed=(_ref('room', 'r1'),), ts='2026-10-06T00:00:01Z',
    )
    right = _authorship(
        author=actor, parent='rev-b', result='rev-1R',
        changed=(_ref('room', 'r2'),), ts='2026-10-06T00:00:02Z',
    )
    verdict = evaluate_divergence(
        left, right, recorded_at_utc='2026-10-06T00:00:03Z'
    )
    assert verdict.verdict == 'unrelated_lineage'
    assert verdict.outcome == 'rebase_required'
    converged = evaluate_divergence(
        left, right,
        right_ancestor_of_left=True,
        recorded_at_utc='2026-10-06T00:00:04Z',
    )
    assert converged.verdict == 'convergent'
    assert converged.outcome == 'auto_merge_safe'
    with pytest.raises(ValueError):
        evaluate_divergence(
            left,
            _authorship(
                author=actor, parent='rev-b', result='rev-1R',
                changed=(_ref('room', 'r2'),), ts=_TS,
            ).model_copy(update={'document_id': 'other-doc'}),
            recorded_at_utc=_TS,
        )


# COL40 — a scope-bound approval never leaks: screen-option approval
# does not bless the audio layout.


def test_col40_partial_approval_stays_scoped() -> None:
    approver = _actor(('reviewer_approver',))
    approval = ApprovalRecord.create(
        document_id=DOC,
        subject_ref=_ref('system_variant', 'variant-screen'),
        scope_kind='system_variant',
        permitted_uses=('client_presentation',),
        approver_ref=AuthorityRef(
            kind='collaboration_actor',
            ref_id=approver.actor_id,
            ref_sha256=approver.actor_sha256,
        ),
        decision='approved',
        approved_at_utc=_TS,
    )
    assert approval.scope_kind == 'system_variant'
    assert approval.permitted_uses == ('client_presentation',)
    assert 'procurement' not in approval.permitted_uses
    assert _sealed(approval, 'approval_sha256')


def test_approval_requires_permitted_uses() -> None:
    approver = _actor(('reviewer_approver',))
    with pytest.raises(ValidationError):
        ApprovalRecord.create(
            document_id=DOC,
            subject_ref=_ref('scene_revision', 'rev-1'),
            scope_kind='whole_revision',
            permitted_uses=(),
            approver_ref=_ref('collaboration_actor', approver.actor_id),
            decision='approved',
            approved_at_utc=_TS,
        )
    # A rejection permits nothing.
    with pytest.raises(ValidationError):
        ApprovalRecord.create(
            document_id=DOC,
            subject_ref=_ref('scene_revision', 'rev-1'),
            scope_kind='whole_revision',
            permitted_uses=('procurement',),
            approver_ref=_ref('collaboration_actor', approver.actor_id),
            decision='rejected',
            approved_at_utc=_TS,
        )
    # Exceptions must be pinned.
    with pytest.raises(ValidationError):
        ApprovalRecord.create(
            document_id=DOC,
            subject_ref=_ref('scene_revision', 'rev-1'),
            scope_kind='whole_revision',
            permitted_uses=('design_coordination',),
            approver_ref=_ref('collaboration_actor', approver.actor_id),
            decision='approved_with_exceptions',
            approved_at_utc=_TS,
        )


# COL50 — client override is a separate non-technical axis.


def test_col50_client_acceptance_never_technical() -> None:
    client = _actor(('client_stakeholder',))
    acceptance = ClientAcceptance.create(
        document_id=DOC,
        subject_ref=_ref('system_variant', 'variant-screen'),
        client_ref=AuthorityRef(
            kind='collaboration_actor',
            ref_id=client.actor_id,
            ref_sha256=client.actor_sha256,
        ),
        accepted_dimensions=('aesthetic', 'commercial'),
        declared_limitations=('有限な代替機器のまま技術的制限あり',),
        accepted_at_utc=_TS,
    )
    assert compose_acceptance_verdict(acceptance) == (
        'client_accepted_non_technical'
    )
    # Acceptance dimensions are closed: technical scopes cannot be
    # expressed — validation refuses them.
    with pytest.raises(ValidationError):
        ClientAcceptance.create(
            document_id=DOC,
            subject_ref=_ref('system_variant', 'v1'),
            client_ref=_ref('collaboration_actor', client.actor_id),
            accepted_dimensions=('technical_performance',),
            accepted_at_utc=_TS,
        )


# COL70 — approvals stale on dependency change but stay visible.


def test_col70_approval_currency() -> None:
    approver = _actor(('reviewer_approver',))
    approval = ApprovalRecord.create(
        document_id=DOC,
        subject_ref=_ref('scene_revision', 'rev-1'),
        scope_kind='whole_revision',
        permitted_uses=('construction_installation',),
        approver_ref=AuthorityRef(
            kind='collaboration_actor',
            ref_id=approver.actor_id,
            ref_sha256=approver.actor_sha256,
        ),
        decision='approved',
        approved_at_utc=_TS,
    )
    assert evaluate_approval_currency(
        approval, subject_current_sha256=_SHA
    ) == 'current'
    assert evaluate_approval_currency(
        approval, subject_current_sha256=_OTHER_SHA
    ) == 'stale_dependency_changed'
    assert evaluate_approval_currency(
        approval, subject_exists=False
    ) == 'subject_missing'
    assert evaluate_approval_currency(
        approval, superseded_by=_ref('approval_record', 'appr-2')
    ) == 'superseded'
    rejected = ApprovalRecord.create(
        document_id=DOC,
        subject_ref=_ref('scene_revision', 'rev-2'),
        scope_kind='whole_revision',
        approver_ref=_ref('collaboration_actor', approver.actor_id),
        decision='rejected',
        approved_at_utc=_TS,
    )
    assert evaluate_approval_currency(rejected) == 'never_in_force'


# Information-state ladder + guarded promotion.


def test_state_ladder_guards() -> None:
    assert evaluate_state_transition('draft', 'review_required')
    assert evaluate_state_transition(
        'reviewed', 'approved_for_declared_use'
    )
    assert evaluate_state_transition(
        'approved_for_declared_use', 'as_installed_observed'
    )
    assert not evaluate_state_transition('draft', 'approved_for_declared_use')
    assert not evaluate_state_transition('archived', 'draft')
    actor = _actor()
    state = InformationStateRecord.create(
        document_id=DOC,
        subject_ref=_ref('scene_revision', 'rev-1'),
        state='approved_for_declared_use',
        actor_ref=_ref('collaboration_actor', actor.actor_id),
        declared_use_profile='HT coordination profile v1',
        recorded_at_utc=_TS,
    )
    assert _sealed(state, 'state_sha256')
    # approved_for_declared_use must declare the profile.
    with pytest.raises(ValidationError):
        InformationStateRecord.create(
            document_id=DOC,
            subject_ref=_ref('scene_revision', 'rev-1'),
            state='approved_for_declared_use',
            actor_ref=_ref('collaboration_actor', actor.actor_id),
            recorded_at_utc=_TS,
        )


# Review decisions compose with #730 review notes; resolutions stay in
# the audit trail.


def test_review_decision_binds_and_resolves() -> None:
    actor = _actor()
    decision = ReviewDecision.create(
        document_id=DOC,
        subject_ref=_ref('scene_revision', 'rev-1'),
        author_ref=_ref('collaboration_actor', actor.actor_id),
        kind='comment_resolution',
        status='resolved_accepted',
        note_refs=(_ref('review_note', 'note-1'),),
        resolution_note='代替案を採用',
        recorded_at_utc=_TS,
    )
    assert _sealed(decision, 'decision_sha256')
    with pytest.raises(ValidationError):
        ReviewDecision.create(
            document_id=DOC,
            subject_ref=_ref('scene_revision', 'rev-1'),
            author_ref=_ref('collaboration_actor', actor.actor_id),
            kind='design_decision',
            status='resolved_accepted',
            recorded_at_utc=_TS,
        )


# Conflict resolutions and branch proposals keep lineage.


def test_conflict_resolution_and_proposal_promotion() -> None:
    actor = _actor()
    resolver = _actor(('reviewer_approver',))
    divergence = SiblingDivergence.create(
        document_id=DOC,
        base_revision_id='rev-0',
        left_authorship_ref=_ref('revision_authorship', 'raut-l'),
        right_authorship_ref=_ref('revision_authorship', 'raut-r'),
        overlap_refs=(_ref('room', 'room-a'),),
        verdict='divergent_conflicted',
        outcome='authority_conflict',
        recorded_at_utc=_TS,
    )
    resolution = ConflictResolution.create(
        document_id=DOC,
        assessment_ref=AuthorityRef(
            kind='sibling_divergence',
            ref_id=divergence.assessment_id,
            ref_sha256=divergence.assessment_sha256,
        ),
        resolution_kind='manual_composition',
        resolver_ref=_ref('collaboration_actor', resolver.actor_id),
        result_revision_ref=_ref('scene_revision', 'rev-merged'),
        resolved_at_utc=_TS,
    )
    assert _sealed(resolution, 'resolution_sha256')
    # A resolving outcome must pin the result revision.
    with pytest.raises(ValidationError):
        ConflictResolution.create(
            document_id=DOC,
            assessment_ref=_ref('sibling_divergence', 'sdiv-1'),
            resolution_kind='keep_left',
            resolver_ref=_ref('collaboration_actor', resolver.actor_id),
            resolved_at_utc=_TS,
        )
    proposal = BranchProposal.create(
        document_id=DOC,
        proposal_kind='alternative_design',
        base_revision_ref=_ref('scene_revision', 'rev-0'),
        proposal_revision_ref=_ref('scene_revision', 'rev-alt'),
        author_ref=_ref('collaboration_actor', actor.actor_id),
        created_at_utc=_TS,
    )
    promotion = ProposalPromotion.create(
        document_id=DOC,
        proposal_ref=AuthorityRef(
            kind='branch_proposal',
            ref_id=proposal.proposal_id,
            ref_sha256=proposal.proposal_sha256,
        ),
        result_revision_ref=_ref('scene_revision', 'rev-alt-main'),
        promoted_by_ref=_ref('collaboration_actor', resolver.actor_id),
        promoted_at_utc=_TS,
    )
    assert _sealed(proposal, 'proposal_sha256')
    assert _sealed(promotion, 'promotion_sha256')


# COL80 — append-only audit events: accountable kinds name an actor.


def test_col80_audit_event_accountability() -> None:
    actor = _actor()
    event = CollaborationEvent.create(
        document_id=DOC,
        kind='approve',
        actor_ref=_ref('collaboration_actor', actor.actor_id),
        subject_refs=(_ref('scene_revision', 'rev-1'),),
        recorded_at_utc=_TS,
    )
    assert _sealed(event, 'event_sha256')
    with pytest.raises(ValidationError):
        CollaborationEvent.create(
            document_id=DOC,
            kind='approve',
            subject_refs=(_ref('scene_revision', 'rev-1'),),
            recorded_at_utc=_TS,
        )
    # System-level import may omit the actor.
    imp = CollaborationEvent.create(
        document_id=DOC,
        kind='import',
        subject_refs=(_ref('scene_revision', 'rev-imp'),),
        detail='offline bundle merged by ancestry',
        recorded_at_utc=_TS,
    )
    assert _sealed(imp, 'event_sha256')


# ---------------------------------------------------------------------------
# #776 material environmental/aging applicability


def _specimen(
    state: str = 'new_as_tested', **kwargs
) -> AcousticMaterialConditionState:
    return AcousticMaterialConditionState.create(
        document_id=DOC,
        material_ref=_ref('acoustic_material', 'mat-1'),
        context='source_specimen',
        condition_state=state,
        observed_at_utc=_TS,
        **kwargs,
    )


def _installed(
    state: str = 'installed_dry', **kwargs
) -> AcousticMaterialConditionState:
    return AcousticMaterialConditionState.create(
        document_id=DOC,
        material_ref=_ref('acoustic_material', 'mat-1'),
        context='installed',
        condition_state=state,
        observed_at_utc=_TS,
        **kwargs,
    )


# MATENV10 — stable synthetic material stays directly applicable.


def test_matenv10_stable_material_directly_applicable() -> None:
    verdict = evaluate_material_applicability(
        _ref('acoustic_material', 'mat-1'),
        _specimen('new_as_tested'),
        _installed('installed_dry'),
        (
            MaterialDurabilityEvidence.create(
                document_id=DOC,
                material_family_ref=_ref('acoustic_material', 'mat-1'),
                evidence_class='long_term_field_measurement',
                affected_quantities=('diffuse_absorption',),
                change_direction='negligible_within_uncertainty',
                duration_hours=200000.0,
                observed_at_utc=_TS,
            ),
        ),
        assessed_at_utc=_TS,
    )
    assert verdict.verdict == 'directly_applicable'
    assert _sealed(verdict, 'applicability_sha256')


# MATENV20 — humidity-sensitive specimen: conditioning evidence selects
# the right applicability.


def test_matenv20_conditioned_specimen_bounded_by_evidence() -> None:
    specimen = _specimen(
        'conditioned',
        conditioning_temperature_c=23.0,
        conditioning_rh_percent=80.0,
    )
    installed = _installed('installed_dry')
    no_evidence = evaluate_material_applicability(
        _ref('acoustic_material', 'mat-1'),
        specimen, installed, (), assessed_at_utc=_TS,
    )
    assert no_evidence.verdict == (
        'applicable_with_unquantified_environmental_limitation'
    )
    evidence = MaterialDurabilityEvidence.create(
        document_id=DOC,
        material_family_ref=_ref('acoustic_material', 'mat-1'),
        evidence_class='controlled_climate_exposure',
        affected_quantities=('diffuse_absorption', 'airflow_resistivity'),
        change_direction='decrease',
        temperature_c=23.0,
        rh_percent=80.0,
        duration_hours=720.0,
        observed_at_utc=_TS,
    )
    bounded = evaluate_material_applicability(
        _ref('acoustic_material', 'mat-1'),
        specimen, installed, (evidence,), assessed_at_utc=_TS,
    )
    assert bounded.verdict == 'applicable_within_declared_domain'
    assert {r.ref_id for r in bounded.matched_evidence_refs} == {
        evidence.evidence_id
    }


# MATENV30 — room RH alone never corrects coefficients.


def test_matenv30_room_rh_alone_is_a_limitation_not_a_correction() -> None:
    verdict = evaluate_material_applicability(
        _ref('acoustic_material', 'mat-1'),
        _specimen('new_as_tested'),
        _installed('unknown'),
        (), assessed_at_utc=_TS,
    )
    assert verdict.verdict == (
        'applicable_with_unquantified_environmental_limitation'
    )
    assert not verdict.consumed_refs
    # No installed observation at all: still a limitation, never a
    # fabricated moisture correction.
    verdict2 = evaluate_material_applicability(
        _ref('acoustic_material', 'mat-1'),
        _specimen('new_as_tested'), None, (), assessed_at_utc=_TS,
    )
    assert verdict2.verdict == (
        'applicable_with_unquantified_environmental_limitation'
    )


# MATENV40 — a water event staleness: prior evidence becomes limited.


def test_matenv40_water_event_stales_and_requests_remeasure() -> None:
    wet = _installed(
        'wet_water_damaged',
        moisture_event_refs=(_ref('maintenance_event', 'leak-1'),),
    )
    verdict = evaluate_material_applicability(
        _ref('acoustic_material', 'mat-1'),
        _specimen('new_as_tested'), wet, (), assessed_at_utc=_TS,
    )
    assert verdict.verdict == 'insufficient_durability_evidence'
    reinspection = evaluate_reinspection_need(wet, assessed_at_utc=_TS)
    assert reinspection.trigger == 'water_leak'
    assert reinspection.verdict == 'remeasurement_recommended'


# MATENV50 — thermal aging: measured aged parameters bound the verdict.


def test_matenv50_thermal_aging_with_covering_evidence() -> None:
    aged = _installed('thermally_aged')
    evidence = MaterialDurabilityEvidence.create(
        document_id=DOC,
        material_family_ref=_ref('acoustic_material', 'mat-1'),
        evidence_class='accelerated_aging_test',
        affected_quantities=('airflow_resistivity', 'diffuse_absorption'),
        change_direction='decrease',
        temperature_c=80.0,
        duration_hours=168.0,
        observed_at_utc=_TS,
    )
    # Evidence bounds it but was not consumed into a state-specific
    # model yet — still applicable within the declared domain.
    verdict = evaluate_material_applicability(
        _ref('acoustic_material', 'mat-1'),
        _specimen('new_as_tested'), aged, (evidence,), assessed_at_utc=_TS,
    )
    assert verdict.verdict == 'applicable_within_declared_domain'
    # When #615 consumed the aged parameters, the consumed identity is
    # pinned in the applicability record.
    consumed = evaluate_material_applicability(
        _ref('acoustic_material', 'mat-1'),
        _specimen('new_as_tested'), aged, (evidence,),
        consumed_refs=(_ref('porous_material_model', 'pm-aged'),),
        assessed_at_utc=_TS,
    )
    assert consumed.verdict == 'applicable_within_declared_domain'
    assert {r.ref_id for r in consumed.consumed_refs} == {'pm-aged'}


# MATENV60 — accelerated aging stays its own class.


def test_matenv60_accelerated_class_never_eq_years() -> None:
    evidence = MaterialDurabilityEvidence.create(
        document_id=DOC,
        material_family_ref=_ref('acoustic_material', 'mat-1'),
        evidence_class='accelerated_aging_test',
        affected_quantities=('diffuse_absorption',),
        temperature_c=70.0,
        duration_hours=336.0,
        observed_at_utc=_TS,
    )
    assert evidence.evidence_class == 'accelerated_aging_test'
    assert not evidence.service_year_mapping_validated
    # Exposure evidence must bound at least one domain axis.
    with pytest.raises(ValidationError):
        MaterialDurabilityEvidence.create(
            document_id=DOC,
            material_family_ref=_ref('acoustic_material', 'mat-1'),
            evidence_class='accelerated_aging_test',
            affected_quantities=('diffuse_absorption',),
            observed_at_utc=_TS,
        )
    # Long-term field evidence must record duration.
    with pytest.raises(ValidationError):
        MaterialDurabilityEvidence.create(
            document_id=DOC,
            material_family_ref=_ref('acoustic_material', 'mat-1'),
            evidence_class='long_term_field_measurement',
            observed_at_utc=_TS,
        )


# MATENV70 — calibration trap: degradation is a hypothesis, not a
# relabel, and unobserved states fail closed.


def test_matenv70_no_auto_diagnosis_without_state() -> None:
    verdict = evaluate_material_applicability(
        _ref('acoustic_material', 'mat-1'), None, None,
        assessed_at_utc=_TS,
    )
    assert verdict.verdict == 'insufficient_durability_evidence'
    # directly_applicable cannot claim condition-specific consumption.
    with pytest.raises(ValidationError):
        MaterialEvidenceApplicability.create(
            document_id=DOC,
            material_ref=_ref('acoustic_material', 'mat-1'),
            verdict='directly_applicable',
            consumed_refs=(_ref('porous_material_model', 'pm-1'),),
            assessed_at_utc=_TS,
        )


# Condition-state validation: honest UNKNOWN and context discipline.


def test_condition_state_validation() -> None:
    # 'conditioned' without conditioning facts is rejected.
    with pytest.raises(ValidationError):
        _specimen('conditioned')
    # Moisture content requires its measurement method.
    with pytest.raises(ValidationError):
        _specimen(
            'conditioned',
            conditioning_rh_percent=50.0,
            moisture_content_percent=8.5,
        )
    ok = _specimen(
        'conditioned',
        conditioning_rh_percent=50.0,
        moisture_content_percent=8.5,
        moisture_content_method='gravimetric',
    )
    assert _sealed(ok, 'condition_sha256')
    # Installed-only fields are rejected on specimens (and vice versa).
    with pytest.raises(ValidationError):
        _specimen('new_as_tested', cavity_kind='enclosed')
    with pytest.raises(ValidationError):
        _installed('installed_dry', test_method='ISO 354')


# ---------------------------------------------------------------------------
# Repository roundtrip / idempotent / tamper


_COLLAB_TABLES = (
    'cad_collaboration_actors',
    'cad_revision_authorship',
    'cad_information_states',
    'cad_approval_records',
    'cad_review_decisions',
    'cad_sibling_divergences',
    'cad_conflict_resolutions',
    'cad_branch_proposals',
    'cad_proposal_promotions',
    'cad_client_acceptances',
    'cad_collaboration_events',
)

_MATENV_TABLES = (
    'cad_material_condition_states',
    'cad_material_durability_evidence',
    'cad_material_evidence_applicability',
    'cad_material_reinspections',
)


def _collab_repo(tmp_path: Path) -> CadCollaborationRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadCollaborationRepository(scene)


def _matenv_repo(tmp_path: Path) -> CadMaterialConditionRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadMaterialConditionRepository(scene)


def test_new_tables_exist(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with connect_sqlite(db) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    for table in _COLLAB_TABLES + _MATENV_TABLES:
        assert table in names


def test_collaboration_repository_roundtrip(tmp_path: Path) -> None:
    repo = _collab_repo(tmp_path)
    actor = _actor()
    repo.save_actor(actor)
    assert repo.get_actor(actor.actor_id) == actor
    repo.save_actor(actor)  # idempotent

    authorship = _authorship(
        author=actor, parent='rev-0', result='rev-1',
        changed=(_ref('room', 'r1'),),
    )
    repo.save_authorship(authorship)
    assert repo.get_authorship(authorship.authorship_id) == authorship

    state = InformationStateRecord.create(
        document_id=DOC,
        subject_ref=_ref('scene_revision', 'rev-1'),
        state='review_required',
        actor_ref=_ref('collaboration_actor', actor.actor_id),
        recorded_at_utc=_TS,
    )
    repo.save_information_state(state)
    assert repo.get_information_state(state.state_id) == state

    approval = ApprovalRecord.create(
        document_id=DOC,
        subject_ref=_ref('scene_revision', 'rev-1'),
        scope_kind='whole_revision',
        permitted_uses=('design_coordination',),
        approver_ref=_ref('collaboration_actor', actor.actor_id),
        decision='approved',
        approved_at_utc=_TS,
    )
    repo.save_approval(approval)
    assert repo.get_approval(approval.approval_id) == approval

    decision = ReviewDecision.create(
        document_id=DOC,
        subject_ref=_ref('scene_revision', 'rev-1'),
        author_ref=_ref('collaboration_actor', actor.actor_id),
        kind='design_decision',
        recorded_at_utc=_TS,
    )
    repo.save_review_decision(decision)
    assert repo.get_review_decision(decision.decision_id) == decision

    divergence = SiblingDivergence.create(
        document_id=DOC,
        base_revision_id='rev-0',
        left_authorship_ref=_ref('revision_authorship', 'raut-l'),
        right_authorship_ref=_ref('revision_authorship', 'raut-r'),
        verdict='divergent_mergeable',
        outcome='auto_merge_safe',
        recorded_at_utc=_TS,
    )
    repo.save_sibling_divergence(divergence)
    assert repo.get_sibling_divergence(
        divergence.assessment_id
    ) == divergence

    resolution = ConflictResolution.create(
        document_id=DOC,
        assessment_ref=_ref('sibling_divergence', divergence.assessment_id),
        resolution_kind='defer',
        resolver_ref=_ref('collaboration_actor', actor.actor_id),
        resolved_at_utc=_TS,
    )
    repo.save_conflict_resolution(resolution)
    assert repo.get_conflict_resolution(
        resolution.resolution_id
    ) == resolution

    proposal = BranchProposal.create(
        document_id=DOC,
        proposal_kind='experimental_calibration',
        base_revision_ref=_ref('scene_revision', 'rev-0'),
        proposal_revision_ref=_ref('scene_revision', 'rev-alt'),
        author_ref=_ref('collaboration_actor', actor.actor_id),
        created_at_utc=_TS,
    )
    repo.save_branch_proposal(proposal)
    assert repo.get_branch_proposal(proposal.proposal_id) == proposal

    promotion = ProposalPromotion.create(
        document_id=DOC,
        proposal_ref=_ref('branch_proposal', proposal.proposal_id),
        result_revision_ref=_ref('scene_revision', 'rev-alt-main'),
        promoted_by_ref=_ref('collaboration_actor', actor.actor_id),
        promoted_at_utc=_TS,
    )
    repo.save_proposal_promotion(promotion)
    assert repo.get_proposal_promotion(
        promotion.promotion_id
    ) == promotion

    acceptance = ClientAcceptance.create(
        document_id=DOC,
        subject_ref=_ref('system_variant', 'v1'),
        client_ref=_ref('collaboration_actor', actor.actor_id),
        accepted_dimensions=('aesthetic',),
        accepted_at_utc=_TS,
    )
    repo.save_client_acceptance(acceptance)
    assert repo.get_client_acceptance(
        acceptance.acceptance_id
    ) == acceptance

    event = CollaborationEvent.create(
        document_id=DOC,
        kind='merge',
        actor_ref=_ref('collaboration_actor', actor.actor_id),
        subject_refs=(_ref('scene_revision', 'rev-merged'),),
        recorded_at_utc=_TS,
    )
    repo.save_event(event)
    assert repo.get_event(event.event_id) == event


def test_material_condition_repository_roundtrip(tmp_path: Path) -> None:
    repo = _matenv_repo(tmp_path)
    specimen = _specimen('conditioned', conditioning_rh_percent=65.0)
    repo.save_condition_state(specimen)
    assert repo.get_condition_state(specimen.condition_id) == specimen
    repo.save_condition_state(specimen)  # idempotent

    installed = _installed('moisture_exposed')
    repo.save_condition_state(installed)
    assert repo.get_condition_state(installed.condition_id) == installed

    evidence = MaterialDurabilityEvidence.create(
        document_id=DOC,
        material_family_ref=_ref('acoustic_material', 'mat-1'),
        evidence_class='controlled_climate_exposure',
        affected_quantities=('complex_impedance',),
        rh_percent=75.0,
        duration_hours=500.0,
        observed_at_utc=_TS,
    )
    repo.save_durability_evidence(evidence)
    assert repo.get_durability_evidence(evidence.evidence_id) == evidence

    verdict = evaluate_material_applicability(
        _ref('acoustic_material', 'mat-1'),
        specimen, installed, (evidence,), assessed_at_utc=_TS,
    )
    repo.save_applicability(verdict)
    assert repo.get_applicability(verdict.applicability_id) == verdict

    reinspection = evaluate_reinspection_need(installed, assessed_at_utc=_TS)
    repo.save_reinspection(reinspection)
    assert repo.get_reinspection(
        reinspection.assessment_id
    ) == reinspection


def test_collaboration_repository_tamper_detected(tmp_path: Path) -> None:
    repo = _collab_repo(tmp_path)
    actor = _actor()
    repo.save_actor(actor)
    with connect_sqlite(repo.path) as conn:
        conn.execute(
            "UPDATE cad_collaboration_actors SET identity_basis='unknown' "
            'WHERE actor_id=?',
            (actor.actor_id,),
        )
        conn.commit()
    with pytest.raises(CollaborationIntegrityError):
        repo.get_actor(actor.actor_id)


def test_material_condition_repository_tamper_detected(
    tmp_path: Path,
) -> None:
    repo = _matenv_repo(tmp_path)
    specimen = _specimen('new_as_tested')
    repo.save_condition_state(specimen)
    with connect_sqlite(repo.path) as conn:
        conn.execute(
            "UPDATE cad_material_condition_states "
            "SET condition_state='wet_water_damaged' WHERE condition_id=?",
            (specimen.condition_id,),
        )
        conn.commit()
    with pytest.raises(MaterialConditionIntegrityError):
        repo.get_condition_state(specimen.condition_id)

