from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_multifidelity import (
    CadMultiFidelityRepository,
    MultiFidelityAuthorityRef,
    MultiFidelityStageDefinition,
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
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(
        SceneDocument(
            document_id='multifidelity-fixture',
            room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
            entities=(),
        ),
        parent_revision_id=None,
    )
    plan = _plan()
    geometry, coverage, screening = _screening(plan)
    final_comparison = _final_comparison()
    resolver = _TopologyComparisonResolver(
        scene_repository.path,
        final_comparison,
    )
    repository = CadMultiFidelityRepository(
        scene_repository,
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
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(
        SceneDocument(
            document_id='multifidelity-finalization-fixture',
            room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.4),
            entities=(),
        ),
        parent_revision_id=None,
    )
    plan = _plan()
    geometry, coverage, screening = _screening(plan)
    final_comparison = _final_comparison()
    resolver = _TopologyComparisonResolver(
        scene_repository.path,
        final_comparison,
    )
    repository = CadMultiFidelityRepository(
        scene_repository,
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
        SceneRepository(scene_repository.path)
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
