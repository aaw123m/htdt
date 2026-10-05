"""REV56-LIFECYCLE regression tests — #596 equipment substitution /
change-impact authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.cad_substitution_impact import (
    AffectedAuthorityEntry,
    DimensionalAssessment,
    ScheduleLineItem,
    SUBSTITUTION_DIMENSIONS,
    SubstitutionEquipmentRef,
    SubstitutionEvidenceRef,
    build_equipment_schedule,
    build_substitution_proposal,
    compose_reverification_scope,
    disposition_for_role,
    evaluate_approval_decision,
    evaluate_change_impact,
    reconcile_as_built,
)
from htdt.cad_substitution_impact_repository import (
    CadSubstitutionImpactRepository,
    SubstitutionImpactConflictError,
    SubstitutionImpactIntegrityError,
)


DOC = 'doc-rev56-subst'
T0 = '2026-10-05T00:00:00+00:00'
T1 = '2026-10-05T01:00:00+00:00'
T2 = '2026-10-05T02:00:00+00:00'
T3 = '2026-10-05T03:00:00+00:00'


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _equipment(
    definition_id: str = 'spk-main',
    version: str = '1.0',
    digit: str = 'a',
    *,
    pinned: bool = True,
) -> SubstitutionEquipmentRef:
    return SubstitutionEquipmentRef(
        definition_id=definition_id,
        definition_version=version,
        definition_sha256=(digit * 64) if pinned else None,
    )


_ORIGINAL = _equipment('spk-main', '1.0', 'a')
_PROPOSED = _equipment('spk-alt', '2.1', 'b')


def _proposal(**overrides):
    kwargs = dict(
        document_id=DOC,
        original=_ORIGINAL,
        proposed=_PROPOSED,
        reason_kind='availability',
        reason_detail='original model discontinued',
        evidence_class='declared_equivalent',
        requester='integrator-a',
        requested_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_substitution_proposal(**kwargs)


def _evidence(bound_to: str, ref_id: str = 'ev-1') -> SubstitutionEvidenceRef:
    return SubstitutionEvidenceRef(
        ref=AuthorityRef(kind='equipment_evidence', ref_id=ref_id),
        bound_to=bound_to,
    )


def _dim(
    dimension: str,
    verdict: str = 'meets_or_exceeds_requirement',
    **overrides,
) -> DimensionalAssessment:
    kwargs = dict(
        dimension=dimension,
        product_difference='different',
        verdict=verdict,
        evidence_refs=(_evidence('proposed', f'ev-{dimension}'),),
    )
    kwargs.update(overrides)
    return DimensionalAssessment(**kwargs)


def _clean_dimensions() -> tuple[DimensionalAssessment, ...]:
    return tuple(
        _dim(dimension) for dimension in SUBSTITUTION_DIMENSIONS
    )


# ---------------------------------------------------------------------------
# Proposal identity pin (#596 §1)
# ---------------------------------------------------------------------------


def test_proposal_roundtrip_and_append_only(tmp_path: Path):
    repository = CadSubstitutionImpactRepository(_scene_repo(tmp_path))
    proposal = _proposal()
    assert proposal.proposal_id.startswith('subprop-')
    repository.save_proposal(proposal)
    assert repository.get_proposal(proposal.proposal_id) == proposal
    assert repository.list_proposals(DOC) == (proposal,)
    repository.save_proposal(proposal)  # idempotent
    forged = proposal.model_copy(update={'reason_detail': 'budget cut'})
    with pytest.raises(SubstitutionImpactIntegrityError):
        repository.save_proposal(forged)


def test_same_exact_evidence_requires_identical_products():
    """A label-level "same model" claim is not same-exact evidence unless
    the pinned identities literally match."""
    with pytest.raises(ValueError):
        _proposal(evidence_class='same_exact_evidence')
    same = _equipment('spk-main', '1.0', 'a')
    proposal = _proposal(
        proposed=same,
        reason_kind='service_replacement',
        reason_detail='unit swap under warranty',
        evidence_class='same_exact_evidence',
    )
    assert proposal.original.identical_to(proposal.proposed)


def test_identical_products_only_for_service_replacement():
    same = _equipment('spk-main', '1.0', 'a')
    with pytest.raises(ValueError):
        _proposal(
            proposed=same,
            reason_kind='budget',
            reason_detail='same product cheaper elsewhere',
        )


# ---------------------------------------------------------------------------
# Per-dimension fail-closed rules (#596 §3/§4)
# ---------------------------------------------------------------------------


def test_passing_verdict_rejects_original_only_evidence():
    """Old-product evidence is context, never a passing proof."""
    with pytest.raises(ValueError):
        _dim(
            'electroacoustic',
            evidence_refs=(_evidence('original', 'ev-old'),),
        )


def test_same_exact_dimension_needs_shared_exact_evidence():
    with pytest.raises(ValueError):
        _dim(
            'physical_installation',
            verdict='equivalent_by_same_exact_evidence',
            product_difference='same',
            evidence_refs=(_evidence('proposed'),),
        )


def test_requirement_verdict_fails_blocks_passing_dimension():
    requirement = AuthorityRef(kind='project_requirement', ref_id='req-1')
    with pytest.raises(ValueError):
        _dim(
            'video_projection',
            requirement_ref=requirement,
            requirement_verdict='fails',
        )


def test_not_applicable_needs_reason():
    with pytest.raises(ValueError):
        _dim('video_projection', verdict='not_applicable')
    ok = _dim(
        'video_projection',
        verdict='not_applicable',
        product_difference='same',
        note='audio-only device, no video dimension',
        evidence_refs=(),
    )
    assert ok.verdict == 'not_applicable'


# ---------------------------------------------------------------------------
# Change-impact assessment (#596 §5-§10)
# ---------------------------------------------------------------------------


def test_assessment_same_exact_impossible_for_different_products():
    proposal = _proposal()
    dims = list(_clean_dimensions())
    dims[0] = _dim(
        'physical_installation',
        verdict='equivalent_by_same_exact_evidence',
        product_difference='same',
        evidence_refs=(_evidence('shared_exact'),),
    )
    with pytest.raises(ValueError):
        evaluate_change_impact(
            proposal=proposal, dimensions=dims, assessed_at_utc=T1
        )


def test_technical_verdict_derivation():
    proposal = _proposal()
    acceptable = evaluate_change_impact(
        proposal=proposal, dimensions=_clean_dimensions(), assessed_at_utc=T1
    )
    assert acceptable.technical_verdict == 'technically_acceptable'

    dims = list(_clean_dimensions())
    dims[1] = _dim('electroacoustic', verdict='different_but_acceptable')
    limited = evaluate_change_impact(
        proposal=proposal, dimensions=dims, assessed_at_utc=T1
    )
    assert limited.technical_verdict == 'technically_limited'
    assert limited.limitation_notes  # auto-derived from the dimension

    dims = list(_clean_dimensions())
    dims[4] = _dim(
        'interoperability',
        verdict='incompatible',
        product_difference='worse',
    )
    incompatible = evaluate_change_impact(
        proposal=proposal, dimensions=dims, assessed_at_utc=T1
    )
    assert incompatible.technical_verdict == 'technically_incompatible'

    dims = list(_clean_dimensions())
    dims[5] = _dim(
        'infrastructure',
        verdict='insufficient_evidence',
        product_difference='unknown',
        evidence_refs=(),
    )
    indeterminate = evaluate_change_impact(
        proposal=proposal, dimensions=dims, assessed_at_utc=T1
    )
    assert indeterminate.technical_verdict == 'indeterminate'


def test_dependent_refs_derive_dispositions_from_matrix():
    proposal = _proposal()
    geometry_ref = AuthorityRef(
        kind='geometry_derivation', ref_id='geo-1'
    )
    other_ref = AuthorityRef(kind='unknown_authority', ref_id='u-1')
    dims = list(_clean_dimensions())
    dims[0] = _dim(
        'physical_installation',
        verdict='incompatible',
        product_difference='worse',
    )
    assessment = evaluate_change_impact(
        proposal=proposal,
        dimensions=dims,
        dependent_refs=(
            (geometry_ref, 'geometry_clearance'),
            (other_ref, 'other'),
        ),
        assessed_at_utc=T1,
    )
    by_ref = {
        e.affected_ref.ref_id: e.disposition
        for e in assessment.affected_entries
    }
    assert by_ref['geo-1'] == 'remeasure'
    # Unknown dependency coverage is never "still valid" (#596 §5).
    assert by_ref['u-1'] == 'review_required'
    assert assessment.technical_verdict == 'technically_incompatible'


def test_disposition_for_role_mapping():
    assert (
        disposition_for_role('geometry_clearance', {'physical_installation': 'incompatible'})
        == 'remeasure'
    )
    assert (
        disposition_for_role(
            'geometry_clearance', {'physical_installation': 'insufficient_evidence'}
        )
        == 'review_required'
    )
    assert (
        disposition_for_role(
            'directivity_prediction',
            {'electroacoustic': 'meets_or_exceeds_requirement'},
        )
        == 'unaffected'
    )
    assert disposition_for_role('other', {}) == 'review_required'


def test_reverification_scope_is_targeted_not_global():
    proposal = _proposal()
    geo_ref = AuthorityRef(kind='geometry_derivation', ref_id='geo-1')
    dsp_ref = AuthorityRef(kind='dsp_routing', ref_id='dsp-1')
    assessment = evaluate_change_impact(
        proposal=proposal,
        dimensions=_clean_dimensions(),
        dependent_refs=((dsp_ref, 'dsp_routing'),),
        extra_dispositions=(
            AffectedAuthorityEntry(
                affected_ref=geo_ref,
                role='geometry_clearance',
                disposition='remeasure',
                reason='mounting pattern differs',
            ),
        ),
        assessed_at_utc=T1,
    )
    tasks = compose_reverification_scope(assessment)
    assert {t.task_kind for t in tasks} == {'geometry_recheck'}
    assert tasks[0].target_refs == (geo_ref,)


# ---------------------------------------------------------------------------
# Approval state machine (#596 §10-§12)
# ---------------------------------------------------------------------------


def _assessed(verdict_dims=None, **kw):
    proposal = _proposal()
    dims = verdict_dims or _clean_dimensions()
    return proposal, evaluate_change_impact(
        proposal=proposal, dimensions=dims, assessed_at_utc=T1, **kw
    )


def test_engineering_approval_requires_acceptable_verdict():
    proposal, assessment = _assessed()
    decision = evaluate_approval_decision(
        proposal=proposal,
        state='engineering_approved',
        assessment=assessment,
        approver='design-lead',
        decided_at_utc=T2,
    )
    assert decision.technical_verdict == 'technically_acceptable'

    dims = list(_clean_dimensions())
    dims[2] = _dim('signal_dsp', verdict='inferior', product_difference='worse')
    _, limited = _assessed(verdict_dims=dims)
    with pytest.raises(ValueError):
        evaluate_approval_decision(
            proposal=proposal,
            state='engineering_approved',
            assessment=limited,
            decided_at_utc=T2,
        )
    decision = evaluate_approval_decision(
        proposal=proposal,
        state='engineering_approved_with_limitations',
        assessment=limited,
        decided_at_utc=T2,
    )
    assert decision.limitations_carried


def test_commercial_override_never_rewrites_technical_verdict():
    dims = list(_clean_dimensions())
    dims[3] = _dim(
        'video_projection', verdict='incompatible', product_difference='worse'
    )
    proposal, assessment = _assessed(verdict_dims=dims)
    with pytest.raises(ValueError):
        evaluate_approval_decision(
            proposal=proposal,
            state='commercial_override_accepted',
            assessment=assessment,
            commercial_state='client_approved',
            decided_at_utc=T2,
        )
    decision = evaluate_approval_decision(
        proposal=proposal,
        state='commercial_override_accepted',
        assessment=assessment,
        commercial_state='change_order_approved',
        override_rationale='client accepts projector limitation',
        approver='client',
        decided_at_utc=T2,
    )
    assert decision.technical_verdict == 'technically_incompatible'
    assert decision.commercial_state == 'change_order_approved'


def test_as_built_verified_requires_matching_reconciliation():
    proposal, assessment = _assessed()
    wrong_item = _equipment('spk-wrong', '9.9', 'c')
    reconciliation = reconcile_as_built(
        proposal=proposal,
        installed=wrong_item,
        serial_or_asset_tag='SN-001',
        reconciled_at_utc=T3,
    )
    assert reconciliation.verdict == 'differs_from_approved'
    with pytest.raises(ValueError):
        evaluate_approval_decision(
            proposal=proposal,
            state='as_built_verified',
            assessment=assessment,
            reconciliation=reconciliation,
            decided_at_utc=T3,
        )
    matching = reconcile_as_built(
        proposal=proposal,
        installed=_PROPOSED,
        serial_or_asset_tag='SN-002',
        reconciled_at_utc=T3,
    )
    assert matching.verdict == 'matches_approved'
    decision = evaluate_approval_decision(
        proposal=proposal,
        state='as_built_verified',
        assessment=assessment,
        reconciliation=matching,
        decided_at_utc=T3,
    )
    assert decision.state == 'as_built_verified'


def test_reconciliation_identity_unverified():
    proposal = _proposal()
    reconciliation = reconcile_as_built(
        proposal=proposal,
        installed=_equipment('spk-alt', '2.1', pinned=False),
        reconciled_at_utc=T3,
    )
    assert reconciliation.verdict == 'identity_unverified'


def test_decision_binds_exact_proposal_revision():
    proposal, assessment = _assessed()
    other_proposal = _proposal(reason_detail='different reason text')
    with pytest.raises(ValueError):
        evaluate_approval_decision(
            proposal=other_proposal,
            state='engineering_approved',
            assessment=assessment,
            decided_at_utc=T2,
        )


# ---------------------------------------------------------------------------
# Repository integration + equipment schedule (#596 §14)
# ---------------------------------------------------------------------------


def test_repository_requires_persisted_proposal(tmp_path: Path):
    repository = CadSubstitutionImpactRepository(_scene_repo(tmp_path))
    proposal, assessment = _assessed()
    with pytest.raises(SubstitutionImpactIntegrityError):
        repository.save_assessment(assessment)
    repository.save_proposal(proposal)
    repository.save_assessment(assessment)
    assert repository.get_assessment(assessment.assessment_id) == (
        assessment
    )
    assert repository.list_assessments(
        DOC, proposal_id=proposal.proposal_id
    ) == (assessment,)
    forged = assessment.model_copy(update={'document_id': 'doc-forged'})
    with pytest.raises(SubstitutionImpactIntegrityError):
        repository.save_assessment(forged)


def test_decision_and_reconciliation_roundtrip(tmp_path: Path):
    repository = CadSubstitutionImpactRepository(_scene_repo(tmp_path))
    proposal, assessment = _assessed()
    repository.save_proposal(proposal)
    repository.save_assessment(assessment)
    decision = evaluate_approval_decision(
        proposal=proposal,
        state='engineering_approved',
        assessment=assessment,
        decided_at_utc=T2,
    )
    repository.save_decision(decision)
    assert repository.get_decision(decision.decision_id) == decision
    reconciliation = reconcile_as_built(
        proposal=proposal,
        installed=_PROPOSED,
        reconciled_at_utc=T3,
    )
    repository.save_reconciliation(reconciliation)
    assert repository.get_reconciliation(
        reconciliation.reconciliation_id
    ) == reconciliation
    with pytest.raises(SubstitutionImpactConflictError):
        other = reconcile_as_built(
            proposal=proposal,
            installed=_equipment('spk-x', '0.1', 'd'),
            reconciled_at_utc=T3,
        ).model_copy(
            update={'reconciliation_id': reconciliation.reconciliation_id}
        )
        repository.save_reconciliation(other)


def test_equipment_schedule_history_is_append_only(tmp_path: Path):
    repository = CadSubstitutionImpactRepository(_scene_repo(tmp_path))
    design = build_equipment_schedule(
        document_id=DOC,
        phase='design',
        lines=(
            ScheduleLineItem(
                line_id='line-1', equipment=_ORIGINAL
            ),
        ),
        recorded_at_utc=T0,
    )
    approved = build_equipment_schedule(
        document_id=DOC,
        phase='approved_substitution',
        lines=(
            ScheduleLineItem(
                line_id='line-1', equipment=_PROPOSED
            ),
        ),
        supersedes_schedule_id=design.schedule_id,
        recorded_at_utc=T1,
    )
    repository.save_schedule(design)
    repository.save_schedule(approved)
    schedules = repository.list_schedules(DOC)
    assert [s.phase for s in schedules] == ['design', 'approved_substitution']
    # The design-phase row still names the original product.
    assert schedules[0].lines[0].equipment.definition_id == 'spk-main'
    assert approved.supersedes_schedule_id == design.schedule_id
    with pytest.raises(ValueError):
        build_equipment_schedule(
            document_id=DOC,
            phase='procured',
            lines=(
                ScheduleLineItem(line_id='l', equipment=_ORIGINAL),
                ScheduleLineItem(line_id='l', equipment=_PROPOSED),
            ),
            recorded_at_utc=T2,
        )
