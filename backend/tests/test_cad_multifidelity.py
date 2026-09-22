from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_multifidelity import (
    CadMultiFidelityRepository,
    MultiFidelityAuthorityRef,
    MultiFidelityPlan,
    MultiFidelityStageDefinition,
    MultiFidelityStageEvidenceContext,
    MultiFidelityStageOutcome,
    build_multifidelity_plan,
    build_multifidelity_stage_result,
    evaluate_multifidelity_screening,
    finalize_o100_multifidelity,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, SceneDocument
from htdt.cad_topology_comparison import (
    ComparisonEligibilityIssue,
    TopologyComparisonEvaluation,
    VariantBundleRef,
    VariantComparisonEligibility,
    canonical_topology_comparison_sha256,
)
from htdt.pareto import ParetoResult


NOW = '2026-09-20T00:00:00+00:00'


class _TopologyComparisonResolver:
    def __init__(
        self,
        path: Path,
        evaluation: TopologyComparisonEvaluation,
    ) -> None:
        self.path = Path(path)
        self._evaluation = evaluation

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> TopologyComparisonEvaluation | None:
        if evaluation_id == self._evaluation.evaluation_id:
            return self._evaluation
        return None


class _AuthorityRegistry:
    """Exact registry for plan-level multi-fidelity authorities."""

    def __init__(self, *refs: MultiFidelityAuthorityRef) -> None:
        self._refs = {ref.key(): ref for ref in refs}

    def register(self, ref: MultiFidelityAuthorityRef) -> None:
        self._refs[ref.key()] = ref

    def remove(self, ref: MultiFidelityAuthorityRef) -> None:
        self._refs.pop(ref.key(), None)

    def __call__(
        self,
        ref: MultiFidelityAuthorityRef,
    ) -> MultiFidelityAuthorityRef | None:
        return self._refs.get(ref.key())


class _StageEvidenceRegistry:
    """Evidence refs bound to an exact plan/stage/candidate/relationship."""

    def __init__(self) -> None:
        self._bindings: dict[
            tuple[str, str, str | None, str],
            tuple[
                str,
                str,
                tuple[str, str, str | None, str],
                tuple[str, str, str | None, str] | None,
            ],
        ] = {}

    def register(
        self,
        ref: MultiFidelityAuthorityRef,
        *,
        plan: MultiFidelityPlan,
        stage_id: str,
        candidate: MultiFidelityAuthorityRef,
        relationship: MultiFidelityAuthorityRef | None = None,
    ) -> None:
        self._bindings[ref.key()] = (
            plan.plan_id,
            stage_id,
            candidate.key(),
            None if relationship is None else relationship.key(),
        )

    def remove(self, ref: MultiFidelityAuthorityRef) -> None:
        self._bindings.pop(ref.key(), None)

    def __call__(
        self,
        ref: MultiFidelityAuthorityRef,
        context: MultiFidelityStageEvidenceContext,
    ) -> MultiFidelityAuthorityRef | None:
        binding = self._bindings.get(ref.key())
        if binding is None:
            return None
        plan_id, stage_id, candidate_key, relationship_key = binding
        if plan_id != context.plan.plan_id:
            return None
        if stage_id != context.stage.stage_id:
            return None
        if candidate_key != context.outcome.candidate.key():
            return None
        stage_relationship = context.stage.validated_screening_relationship_ref
        if relationship_key != (
            None
            if stage_relationship is None
            else stage_relationship.key()
        ):
            return None
        return ref


def _plan_authority_refs(
    plan: MultiFidelityPlan,
) -> tuple[MultiFidelityAuthorityRef, ...]:
    refs: list[MultiFidelityAuthorityRef] = [
        plan.baseline_authority,
        *plan.candidates,
    ]
    for stage in plan.stages:
        refs.append(stage.evaluator_authority)
        if stage.validated_screening_relationship_ref is not None:
            refs.append(stage.validated_screening_relationship_ref)
    return tuple(refs)


def _register_stage_evidence(
    evidence: _StageEvidenceRegistry,
    plan: MultiFidelityPlan,
    *stage_results,
) -> None:
    for result in stage_results:
        stage = plan.stage(result.stage_id)
        for outcome in result.outcomes:
            for ref in outcome.evidence_refs:
                evidence.register(
                    ref,
                    plan=plan,
                    stage_id=stage.stage_id,
                    candidate=outcome.candidate,
                    relationship=stage.validated_screening_relationship_ref,
                )


def _scene_repository(tmp_path: Path, document_id: str) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(
        SceneDocument(
            document_id=document_id,
            room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
            entities=(),
        ),
        parent_revision_id=None,
    )
    return scene_repository


def _ref(
    kind: str,
    identity: str,
    char: str,
    *,
    fidelity: str | None = None,
) -> MultiFidelityAuthorityRef:
    return MultiFidelityAuthorityRef(
        authority_kind=kind,
        authority_id=identity,
        authority_version='fixture-v1',
        semantic_sha256=char * 64,
        fidelity=fidelity,
    )


def _candidates():
    return (
        _ref('system_variant', 'variant-a', 'a'),
        _ref('system_variant', 'variant-b', 'b'),
        _ref('system_variant', 'variant-c', 'c'),
    )


def _plan(*, second_policy: str = 'validated_screening'):
    candidates = _candidates()
    relationship = (
        _ref('screening_relationship', 'coverage-to-final', '7')
        if second_policy == 'validated_screening'
        else None
    )
    return build_multifidelity_plan(
        domain='o100_topology',
        name='O100E fixture',
        baseline_authority=_ref(
            'scene_revision',
            'scene-fixture',
            '0',
        ),
        candidates=candidates,
        stages=(
            MultiFidelityStageDefinition(
                stage_id='geometry',
                order=0,
                name='Exact geometry hard gate',
                policy='hard_gate',
                fidelity_label='exact-geometry',
                evaluator_authority=_ref(
                    'evaluator',
                    'geometry-gate',
                    '1',
                    fidelity='exact-geometry',
                ),
            ),
            MultiFidelityStageDefinition(
                stage_id='coverage',
                order=1,
                name='Coverage screening',
                policy=second_policy,
                fidelity_label='directivity-coverage',
                evaluator_authority=_ref(
                    'evaluator',
                    'coverage-screen',
                    '2',
                    fidelity='directivity-coverage',
                ),
                validated_screening_relationship_ref=relationship,
            ),
            MultiFidelityStageDefinition(
                stage_id='final',
                order=2,
                name='Common-fidelity final comparison',
                policy='final_common_fidelity',
                fidelity_label='common-final',
                evaluator_authority=_ref(
                    'evaluator',
                    'o100d-final',
                    '3',
                    fidelity='common-final',
                ),
            ),
        ),
    )


def _screening(plan):
    a, b, c = plan.candidates
    geometry = build_multifidelity_stage_result(
        plan=plan,
        stage_id='geometry',
        input_candidates=(a, b, c),
        outcomes=(
            MultiFidelityStageOutcome(
                candidate=a,
                decision='ADVANCE',
                evidence_refs=(_ref('geometry', 'a-geometry', '4'),),
            ),
            MultiFidelityStageOutcome(
                candidate=b,
                decision='ADVANCE',
                evidence_refs=(_ref('geometry', 'b-geometry', '5'),),
            ),
            MultiFidelityStageOutcome(
                candidate=c,
                decision='PRUNED',
                evidence_refs=(_ref('constraint', 'c-collision', '6'),),
                reasons=('exact hard constraint collision',),
            ),
        ),
    )
    coverage = build_multifidelity_stage_result(
        plan=plan,
        stage_id='coverage',
        input_candidates=(a, b),
        outcomes=(
            MultiFidelityStageOutcome(
                candidate=a,
                decision='ADVANCE',
                evidence_refs=(_ref('coverage', 'a-coverage', '8'),),
            ),
            MultiFidelityStageOutcome(
                candidate=b,
                decision='ADVANCE',
                evidence_refs=(_ref('coverage', 'b-coverage', '9'),),
            ),
        ),
    )
    evaluation = evaluate_multifidelity_screening(
        plan=plan,
        stage_results=(geometry, coverage),
        created_at_utc=NOW,
    )
    return geometry, coverage, evaluation


def _final_comparison(
    *,
    candidate_ids: tuple[str, ...] = ('variant-a', 'variant-b'),
) -> TopologyComparisonEvaluation:
    all_ids = ('variant-a', 'variant-b', 'variant-c')
    eligibility = tuple(
        VariantComparisonEligibility(
            variant_id=variant_id,
            state=('ELIGIBLE' if variant_id in candidate_ids else 'INELIGIBLE'),
            issues=(
                ()
                if variant_id in candidate_ids
                else (
                    ComparisonEligibilityIssue(
                        code='screened_before_final',
                        detail='candidate did not enter common-fidelity final comparison',
                    ),
                )
            ),
        )
        for variant_id in all_ids
    )
    variant_hashes = {
        'variant-a': 'a' * 64,
        'variant-b': 'b' * 64,
        'variant-c': 'c' * 64,
    }
    bundles = tuple(
        VariantBundleRef(
            bundle_id=f'fixture-bundle:{variant_id}',
            bundle_sha256={
                'variant-a': '1' * 64,
                'variant-b': '2' * 64,
                'variant-c': '3' * 64,
            }[variant_id],
            variant_id=variant_id,
            variant_sha256=variant_hashes[variant_id],
        )
        for variant_id in candidate_ids
    )
    pareto = ParetoResult(
        objective_ids=('fixture-objective',),
        non_dominated_candidate_ids=candidate_ids,
        dominated_by={variant_id: () for variant_id in candidate_ids},
    )
    payload = {
        'schema_version': 1,
        'authority_version': 'o100d-topology-comparison-evaluation-1',
        'comparison_id': 'fixture-comparison',
        'comparison_semantic_sha256': 'd' * 64,
        'bundles': [item.model_dump(mode='json') for item in bundles],
        'eligibility': [item.model_dump(mode='json') for item in eligibility],
        'pareto_objective_ids': ['fixture-objective'],
        'pareto_result': pareto.model_dump(mode='json'),
        'created_at_utc': NOW,
    }
    digest = canonical_topology_comparison_sha256(payload)
    return TopologyComparisonEvaluation(
        evaluation_id=f'topology-evaluation-{digest[:24]}',
        comparison_id='fixture-comparison',
        comparison_semantic_sha256='d' * 64,
        bundles=bundles,
        eligibility=eligibility,
        pareto_objective_ids=('fixture-objective',),
        pareto_result=pareto,
        created_at_utc=NOW,
        evaluation_sha256=digest,
    )


def test_multifidelity_stage_chain_and_o100_final_common_fidelity() -> None:
    plan = _plan()
    _geometry, _coverage, screening = _screening(plan)

    assert screening.state == 'READY_FOR_COMMON_FIDELITY'
    assert [item.authority_id for item in screening.surviving_candidates] == [
        'variant-a',
        'variant-b',
    ]

    finalization = finalize_o100_multifidelity(
        plan=plan,
        screening=screening,
        final_comparison=_final_comparison(),
    )

    assert finalization.claim_state == 'COMPLETE'
    assert [item.authority_id for item in finalization.final_candidates] == [
        'variant-a',
        'variant-b',
    ]
    assert (
        finalization.final_comparison_ref.fidelity
        == 'common-final-comparison'
    )


def test_validated_screening_requires_explicit_relationship_authority() -> None:
    with pytest.raises(
        ValueError,
        match='validated screening requires an exact screening relationship',
    ):
        MultiFidelityStageDefinition(
            stage_id='coverage',
            order=0,
            name='invalid approximate screen',
            policy='validated_screening',
            fidelity_label='cheap-model',
            evaluator_authority=_ref('evaluator', 'cheap', '1'),
        )


def test_shortlist_budget_defer_is_preliminary_not_pruned() -> None:
    plan = _plan(second_policy='shortlist_only')
    a, b, c = plan.candidates
    geometry = build_multifidelity_stage_result(
        plan=plan,
        stage_id='geometry',
        input_candidates=(a, b, c),
        outcomes=tuple(
            MultiFidelityStageOutcome(
                candidate=item,
                decision='ADVANCE',
                evidence_refs=(_ref('geometry', f'{item.authority_id}-g', '4'),),
            )
            for item in (a, b, c)
        ),
    )
    shortlist = build_multifidelity_stage_result(
        plan=plan,
        stage_id='coverage',
        input_candidates=(a, b, c),
        outcomes=(
            MultiFidelityStageOutcome(
                candidate=a,
                decision='ADVANCE',
                evidence_refs=(_ref('coverage', 'a', '5'),),
            ),
            MultiFidelityStageOutcome(
                candidate=b,
                decision='ADVANCE',
                evidence_refs=(_ref('coverage', 'b', '6'),),
            ),
            MultiFidelityStageOutcome(
                candidate=c,
                decision='DEFERRED_BUDGET',
                evidence_refs=(_ref('coverage', 'c', '7'),),
                reasons=('stage budget exhausted before refinement',),
            ),
        ),
    )
    screening = evaluate_multifidelity_screening(
        plan=plan,
        stage_results=(geometry, shortlist),
        created_at_utc=NOW,
    )

    assert screening.state == 'PRELIMINARY_BUDGET'
    finalization = finalize_o100_multifidelity(
        plan=plan,
        screening=screening,
        final_comparison=_final_comparison(),
    )
    assert finalization.claim_state == 'PRELIMINARY_BUDGET'


def test_shortlist_only_cannot_claim_approximate_pruning() -> None:
    plan = _plan(second_policy='shortlist_only')
    a, b, _ = plan.candidates

    with pytest.raises(ValueError, match='candidate pruning is only authorized'):
        build_multifidelity_stage_result(
            plan=plan,
            stage_id='coverage',
            input_candidates=(a, b),
            outcomes=(
                MultiFidelityStageOutcome(
                    candidate=a,
                    decision='ADVANCE',
                ),
                MultiFidelityStageOutcome(
                    candidate=b,
                    decision='PRUNED',
                    evidence_refs=(_ref('coverage', 'b', '8'),),
                    reasons=('cheap score below cutoff',),
                ),
            ),
        )


def test_final_comparison_candidate_set_must_equal_exact_survivors() -> None:
    plan = _plan()
    _geometry, _coverage, screening = _screening(plan)

    with pytest.raises(
        ValueError,
        match='eligible set must equal exact screening survivors',
    ):
        finalize_o100_multifidelity(
            plan=plan,
            screening=screening,
            final_comparison=_final_comparison(candidate_ids=('variant-a',)),
        )


def test_multifidelity_plan_stage_results_and_screening_save_reopen(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repository(tmp_path, 'multifidelity-fixture')
    plan = _plan()
    geometry, coverage, screening = _screening(plan)
    final_comparison = _final_comparison()
    resolver = _TopologyComparisonResolver(
        scene_repository.path,
        final_comparison,
    )
    authorities = _AuthorityRegistry(*_plan_authority_refs(plan))
    evidence = _StageEvidenceRegistry()
    _register_stage_evidence(evidence, plan, geometry, coverage)
    repository = CadMultiFidelityRepository(
        scene_repository,
        authority_resolver=authorities,
        stage_evidence_resolver=evidence,
        topology_comparison_repository=resolver,
    )

    repository.save_plan(plan)
    repository.save_stage_result(geometry)
    repository.save_stage_result(coverage)
    repository.save_screening(screening)
    finalization = finalize_o100_multifidelity(
        plan=plan,
        screening=screening,
        final_comparison=final_comparison,
    )
    repository.save_finalization(finalization)

    reopened_scene = SceneRepository(scene_repository.path)
    reopened_resolver = _TopologyComparisonResolver(
        reopened_scene.path,
        final_comparison,
    )
    reopened = CadMultiFidelityRepository(
        reopened_scene,
        authority_resolver=authorities,
        stage_evidence_resolver=evidence,
        topology_comparison_repository=reopened_resolver,
    )
    assert reopened.get_plan(plan.plan_id) == plan
    assert reopened.get_stage_result(geometry.result_id) == geometry
    assert reopened.get_stage_result(coverage.result_id) == coverage
    assert reopened.get_screening(screening.evaluation_id) == screening
    assert reopened.get_finalization(finalization.finalization_id) == finalization


def test_o100_finalization_reopen_requires_typed_final_comparison_resolver(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repository(
        tmp_path,
        'multifidelity-finalization-fixture',
    )
    plan = _plan()
    geometry, coverage, screening = _screening(plan)
    final_comparison = _final_comparison()
    resolver = _TopologyComparisonResolver(
        scene_repository.path,
        final_comparison,
    )
    authorities = _AuthorityRegistry(*_plan_authority_refs(plan))
    evidence = _StageEvidenceRegistry()
    _register_stage_evidence(evidence, plan, geometry, coverage)
    repository = CadMultiFidelityRepository(
        scene_repository,
        authority_resolver=authorities,
        stage_evidence_resolver=evidence,
        topology_comparison_repository=resolver,
    )
    repository.save_plan(plan)
    repository.save_stage_result(geometry)
    repository.save_stage_result(coverage)
    repository.save_screening(screening)
    finalization = finalize_o100_multifidelity(
        plan=plan,
        screening=screening,
        final_comparison=final_comparison,
    )
    repository.save_finalization(finalization)

    unresolved = CadMultiFidelityRepository(
        SceneRepository(scene_repository.path),
        authority_resolver=authorities,
        stage_evidence_resolver=evidence,
    )
    with pytest.raises(
        ValueError,
        match='requires a typed topology comparison repository',
    ):
        unresolved.get_finalization(finalization.finalization_id)


def test_final_common_fidelity_requires_exact_bundle_for_each_survivor() -> None:
    plan = _plan()
    _geometry, _coverage, screening = _screening(plan)
    valid = _final_comparison()
    missing_bundle = valid.model_copy(
        update={
            'bundles': valid.bundles[:1],
            'evaluation_sha256': canonical_topology_comparison_sha256(
                {
                    **valid.semantic_payload(),
                    'bundles': [
                        item.model_dump(mode='json')
                        for item in valid.bundles[:1]
                    ],
                }
            ),
        }
    )
    payload = missing_bundle.semantic_payload()
    digest = canonical_topology_comparison_sha256(payload)
    missing_bundle = TopologyComparisonEvaluation(
        **{
            **payload,
            'evaluation_id': f'topology-evaluation-{digest[:24]}',
            'evaluation_sha256': digest,
        }
    )

    with pytest.raises(
        ValueError,
        match='requires exact VariantEvaluationBundle ref',
    ):
        finalize_o100_multifidelity(
            plan=plan,
            screening=screening,
            final_comparison=missing_bundle,
        )


@pytest.mark.parametrize(
    'role',
    ('baseline', 'candidate', 'evaluator', 'relationship'),
)
def test_save_plan_rejects_unresolvable_plan_authority(
    tmp_path: Path,
    role: str,
) -> None:
    plan = _plan()
    dropped = {
        'baseline': plan.baseline_authority,
        'candidate': plan.candidates[0],
        'evaluator': plan.stages[0].evaluator_authority,
        'relationship': plan.stages[1].validated_screening_relationship_ref,
    }[role]
    authorities = _AuthorityRegistry(
        *(ref for ref in _plan_authority_refs(plan) if ref != dropped)
    )
    repository = CadMultiFidelityRepository(
        _scene_repository(tmp_path, f'multifidelity-unresolved-{role}'),
        authority_resolver=authorities,
        stage_evidence_resolver=_StageEvidenceRegistry(),
    )

    with pytest.raises(
        ValueError,
        match='exact authority does not exist',
    ):
        repository.save_plan(plan)


def test_save_plan_rejects_mismatched_resolved_authority(
    tmp_path: Path,
) -> None:
    plan = _plan()
    mismatched = plan.candidates[0].model_copy(
        update={'authority_id': 'variant-other'}
    )

    def resolver(ref: MultiFidelityAuthorityRef):
        if ref == plan.candidates[0]:
            return mismatched
        return ref

    repository = CadMultiFidelityRepository(
        _scene_repository(tmp_path, 'multifidelity-mismatch'),
        authority_resolver=resolver,
        stage_evidence_resolver=_StageEvidenceRegistry(),
    )

    with pytest.raises(
        ValueError,
        match='multi-fidelity candidate exact authority mismatch',
    ):
        repository.save_plan(plan)


def test_get_plan_fails_closed_when_candidate_authority_dropped(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repository(
        tmp_path,
        'multifidelity-dropped-candidate',
    )
    plan = _plan()
    authorities = _AuthorityRegistry(*_plan_authority_refs(plan))
    repository = CadMultiFidelityRepository(
        scene_repository,
        authority_resolver=authorities,
        stage_evidence_resolver=_StageEvidenceRegistry(),
    )
    repository.save_plan(plan)

    authorities.remove(plan.candidates[0])
    with pytest.raises(
        ValueError,
        match='multi-fidelity candidate exact authority does not exist',
    ):
        repository.get_plan(plan.plan_id)


def test_save_stage_result_rejects_fabricated_pruned_evidence(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repository(
        tmp_path,
        'multifidelity-fabricated-evidence',
    )
    plan = _plan()
    geometry, _coverage, _screening_evaluation = _screening(plan)
    authorities = _AuthorityRegistry(*_plan_authority_refs(plan))
    evidence = _StageEvidenceRegistry()
    stage = plan.stage('geometry')
    # Register the honest ADVANCE evidence only; the PRUNED outcome's
    # 'constraint' evidence ref is never registered, so it is fabricated
    # authority even though the stage result self-hash is coherent.
    for outcome in geometry.outcomes:
        if outcome.decision == 'PRUNED':
            continue
        for ref in outcome.evidence_refs:
            evidence.register(
                ref,
                plan=plan,
                stage_id=stage.stage_id,
                candidate=outcome.candidate,
            )
    repository = CadMultiFidelityRepository(
        scene_repository,
        authority_resolver=authorities,
        stage_evidence_resolver=evidence,
    )
    repository.save_plan(plan)

    with pytest.raises(
        ValueError,
        match='evidence exact authority does not exist',
    ):
        repository.save_stage_result(geometry)
    assert repository.get_plan(plan.plan_id) == plan


def test_stage_evidence_bound_to_other_candidate_cannot_prune(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repository(
        tmp_path,
        'multifidelity-cross-candidate-evidence',
    )
    plan = _plan()
    a, b, c = plan.candidates
    authorities = _AuthorityRegistry(*_plan_authority_refs(plan))
    evidence = _StageEvidenceRegistry()
    shared_evidence = _ref('constraint', 'shared-collision', '6')
    # The collision evidence is honestly bound to candidate c's geometry
    # outcome; it must not be reusable to prune candidate a.
    evidence.register(
        shared_evidence,
        plan=plan,
        stage_id='geometry',
        candidate=c,
    )
    for candidate in (a, b):
        evidence.register(
            _ref('geometry', f'{candidate.authority_id}-geometry', '4'),
            plan=plan,
            stage_id='geometry',
            candidate=candidate,
        )
    result = build_multifidelity_stage_result(
        plan=plan,
        stage_id='geometry',
        input_candidates=(a, b, c),
        outcomes=(
            MultiFidelityStageOutcome(
                candidate=a,
                decision='PRUNED',
                evidence_refs=(shared_evidence,),
                reasons=('borrowed collision evidence',),
            ),
            MultiFidelityStageOutcome(
                candidate=b,
                decision='ADVANCE',
                evidence_refs=(
                    _ref('geometry', 'variant-b-geometry', '4'),
                ),
            ),
            MultiFidelityStageOutcome(
                candidate=c,
                decision='ADVANCE',
                evidence_refs=(
                    _ref('geometry', 'variant-c-geometry', '5'),
                ),
            ),
        ),
    )
    repository = CadMultiFidelityRepository(
        scene_repository,
        authority_resolver=authorities,
        stage_evidence_resolver=evidence,
    )
    repository.save_plan(plan)

    with pytest.raises(
        ValueError,
        match='evidence exact authority does not exist',
    ):
        repository.save_stage_result(result)


def test_stage_evidence_bound_to_other_stage_cannot_prune(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repository(
        tmp_path,
        'multifidelity-cross-stage-evidence',
    )
    plan = _plan()
    a, b, c = plan.candidates
    authorities = _AuthorityRegistry(*_plan_authority_refs(plan))
    evidence = _StageEvidenceRegistry()
    coverage_evidence = _ref('coverage', 'c-coverage-deficit', '8')
    # The coverage deficit evidence is bound to the coverage stage; it must
    # not authorize pruning inside the geometry hard-gate stage.
    evidence.register(
        coverage_evidence,
        plan=plan,
        stage_id='coverage',
        candidate=c,
        relationship=plan.stages[1].validated_screening_relationship_ref,
    )
    for candidate in (a, b):
        evidence.register(
            _ref('geometry', f'{candidate.authority_id}-geometry', '4'),
            plan=plan,
            stage_id='geometry',
            candidate=candidate,
        )
    result = build_multifidelity_stage_result(
        plan=plan,
        stage_id='geometry',
        input_candidates=(a, b, c),
        outcomes=(
            MultiFidelityStageOutcome(
                candidate=a,
                decision='ADVANCE',
                evidence_refs=(
                    _ref('geometry', 'variant-a-geometry', '4'),
                ),
            ),
            MultiFidelityStageOutcome(
                candidate=b,
                decision='ADVANCE',
                evidence_refs=(
                    _ref('geometry', 'variant-b-geometry', '4'),
                ),
            ),
            MultiFidelityStageOutcome(
                candidate=c,
                decision='PRUNED',
                evidence_refs=(coverage_evidence,),
                reasons=('borrowed coverage evidence',),
            ),
        ),
    )
    repository = CadMultiFidelityRepository(
        scene_repository,
        authority_resolver=authorities,
        stage_evidence_resolver=evidence,
    )
    repository.save_plan(plan)

    with pytest.raises(
        ValueError,
        match='evidence exact authority does not exist',
    ):
        repository.save_stage_result(result)


def test_validated_screening_evidence_must_match_declared_relationship(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repository(
        tmp_path,
        'multifidelity-relationship-evidence',
    )
    plan = _plan()
    a, b, _c = plan.candidates
    geometry, _coverage, _screening_evaluation = _screening(plan)
    authorities = _AuthorityRegistry(*_plan_authority_refs(plan))
    evidence = _StageEvidenceRegistry()
    _register_stage_evidence(evidence, plan, geometry)
    stage = plan.stage('coverage')
    other_relationship = _ref(
        'screening_relationship',
        'other-relationship',
        'e',
    )
    coverage_prune = _ref('coverage', 'b-coverage-deficit', '9')
    # Evidence bound to a different screening relationship than the one the
    # stage declares must not authorize pruning.
    evidence.register(
        coverage_prune,
        plan=plan,
        stage_id='coverage',
        candidate=b,
        relationship=other_relationship,
    )
    evidence.register(
        _ref('coverage', 'a-coverage', '8'),
        plan=plan,
        stage_id='coverage',
        candidate=a,
        relationship=stage.validated_screening_relationship_ref,
    )
    coverage = build_multifidelity_stage_result(
        plan=plan,
        stage_id='coverage',
        input_candidates=(a, b),
        outcomes=(
            MultiFidelityStageOutcome(
                candidate=a,
                decision='ADVANCE',
                evidence_refs=(_ref('coverage', 'a-coverage', '8'),),
            ),
            MultiFidelityStageOutcome(
                candidate=b,
                decision='PRUNED',
                evidence_refs=(coverage_prune,),
                reasons=('coverage deficit under unvalidated relationship',),
            ),
        ),
    )
    repository = CadMultiFidelityRepository(
        scene_repository,
        authority_resolver=authorities,
        stage_evidence_resolver=evidence,
    )
    repository.save_plan(plan)
    repository.save_stage_result(geometry)

    with pytest.raises(
        ValueError,
        match='evidence exact authority does not exist',
    ):
        repository.save_stage_result(coverage)


def test_get_stage_result_fails_closed_when_evidence_dropped(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repository(
        tmp_path,
        'multifidelity-dropped-evidence',
    )
    plan = _plan()
    geometry, _coverage, _screening_evaluation = _screening(plan)
    authorities = _AuthorityRegistry(*_plan_authority_refs(plan))
    evidence = _StageEvidenceRegistry()
    _register_stage_evidence(evidence, plan, geometry)
    repository = CadMultiFidelityRepository(
        scene_repository,
        authority_resolver=authorities,
        stage_evidence_resolver=evidence,
    )
    repository.save_plan(plan)
    repository.save_stage_result(geometry)

    pruned = next(
        outcome
        for outcome in geometry.outcomes
        if outcome.decision == 'PRUNED'
    )
    evidence.remove(pruned.evidence_refs[0])
    with pytest.raises(
        ValueError,
        match='evidence exact authority does not exist',
    ):
        repository.get_stage_result(geometry.result_id)


def test_screening_rejects_stage_result_with_dropped_evidence(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repository(
        tmp_path,
        'multifidelity-screening-dropped-evidence',
    )
    plan = _plan()
    geometry, coverage, screening = _screening(plan)
    authorities = _AuthorityRegistry(*_plan_authority_refs(plan))
    evidence = _StageEvidenceRegistry()
    _register_stage_evidence(evidence, plan, geometry, coverage)
    repository = CadMultiFidelityRepository(
        scene_repository,
        authority_resolver=authorities,
        stage_evidence_resolver=evidence,
    )
    repository.save_plan(plan)
    repository.save_stage_result(geometry)
    repository.save_stage_result(coverage)

    pruned = next(
        outcome
        for outcome in geometry.outcomes
        if outcome.decision == 'PRUNED'
    )
    evidence.remove(pruned.evidence_refs[0])
    with pytest.raises(
        ValueError,
        match='evidence exact authority does not exist',
    ):
        repository.save_screening(screening)
