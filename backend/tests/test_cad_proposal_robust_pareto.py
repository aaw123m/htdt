from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_proposal_robust_pareto import (
    CadProposalRobustParetoRepository,
    build_proposal_robust_pareto_evaluation,
)
from htdt.cad_proposal_robustness import ProposalRobustnessSpec
from htdt.cad_repository import SceneRepository
from htdt.cad_topology_comparison import (
    ExactAuthorityRef,
    ObjectiveEvidenceBinding,
    TopologyComparisonEvaluation,
    VariantBundleRef,
    VariantComparisonEligibility,
    VariantEvaluationBundle,
    canonical_topology_comparison_sha256,
)
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
)
from htdt.optimization_robustness import (
    RobustnessEvaluation,
    UncertaintyAxis,
    canonical_robustness_sha256,
)
from htdt.optimization_robustness_multidimensional import RobustParetoSelection
from htdt.pareto import pareto_front


NOW = '2026-09-20T00:00:00+00:00'
OBJECTIVE_ID = 'fixture.response_error_db'
COMPARISON_ID = 'fixture-comparison'
COMPARISON_SHA = 'd' * 64


def _definition() -> ObjectiveDefinition:
    return ObjectiveDefinition(
        objective_id=OBJECTIVE_ID,
        quantity='response error',
        unit='dB',
        direction='minimize',
        valid_domain=ObjectiveValidDomain(kind='finite_real'),
        comparison_model_id='fixture-model',
        comparison_model_version='1',
    )


def _source_ref(variant_id: str) -> ExactAuthorityRef:
    return ExactAuthorityRef(
        authority_kind='fixture_prediction',
        authority_id=f'prediction:{variant_id}',
        authority_version='fixture-result-v1',
        semantic_sha256={
            'variant-a': 'a' * 64,
            'variant-b': 'b' * 64,
        }[variant_id],
        evaluator_id='fixture-objective-evaluator',
        evaluator_version='1',
        model_id='fixture-model',
        model_version='1',
        fidelity='fixture-common-fidelity',
    )


def _bundle(
    variant_id: str,
    variant_sha256: str,
    value: float,
) -> VariantEvaluationBundle:
    definition = _definition()
    source = _source_ref(variant_id)
    vector = ObjectiveVector(
        candidate_id=variant_id,
        metrics=(
            ObjectiveMetric(
                objective_id=OBJECTIVE_ID,
                value=value,
                unit='dB',
                direction='minimize',
                definition=definition,
            ),
        ),
    )
    vector_sha = canonical_topology_comparison_sha256(
        vector.identity_payload()
    )
    evidence = (
        ObjectiveEvidenceBinding(
            objective_id=OBJECTIVE_ID,
            source_authority_kind=source.authority_kind,
            source_authority_id=source.authority_id,
            source_semantic_sha256=source.semantic_sha256,
        ),
    )
    base = {
        'schema_version': 1,
        'authority_version': 'o100d-topology-variant-bundle-1',
        'comparison_id': COMPARISON_ID,
        'comparison_semantic_sha256': COMPARISON_SHA,
        'variant_id': variant_id,
        'variant_sha256': variant_sha256,
        'coverage_evaluation': None,
        'direct_level_evaluation': None,
        'amplifier_headroom_evaluation': None,
        'standards_evaluation': None,
        'fr_prediction_refs': [source.identity_payload()],
        'reflection_prediction_refs': [],
        'installation_evidence_refs': [],
        'objective_vector': vector.identity_payload(),
        'objective_vector_sha256': vector_sha,
        'objective_evidence': [
            item.model_dump(mode='json') for item in evidence
        ],
    }
    digest = canonical_topology_comparison_sha256(base)
    return VariantEvaluationBundle(
        bundle_id=f'topology-bundle-{digest[:24]}',
        comparison_id=COMPARISON_ID,
        comparison_semantic_sha256=COMPARISON_SHA,
        variant_id=variant_id,
        variant_sha256=variant_sha256,
        fr_prediction_refs=(source,),
        objective_vector=vector,
        objective_vector_sha256=vector_sha,
        objective_evidence=evidence,
        bundle_sha256=digest,
    )


def _topology_evaluation(
    bundles: tuple[VariantEvaluationBundle, ...],
) -> TopologyComparisonEvaluation:
    bundle_refs = tuple(
        VariantBundleRef(
            bundle_id=item.bundle_id,
            bundle_sha256=item.bundle_sha256,
            variant_id=item.variant_id,
            variant_sha256=item.variant_sha256,
        )
        for item in bundles
    )
    eligibility = tuple(
        VariantComparisonEligibility(
            variant_id=item.variant_id,
            state='ELIGIBLE',
        )
        for item in bundles
    )
    nominal_pareto = pareto_front(
        tuple(item.objective_vector for item in bundles),
        (OBJECTIVE_ID,),
    )
    payload = {
        'schema_version': 1,
        'authority_version': 'o100d-topology-comparison-evaluation-1',
        'comparison_id': COMPARISON_ID,
        'comparison_semantic_sha256': COMPARISON_SHA,
        'bundles': [item.model_dump(mode='json') for item in bundle_refs],
        'eligibility': [item.model_dump(mode='json') for item in eligibility],
        'pareto_objective_ids': [OBJECTIVE_ID],
        'pareto_result': nominal_pareto.model_dump(mode='json'),
        'created_at_utc': NOW,
    }
    digest = canonical_topology_comparison_sha256(payload)
    return TopologyComparisonEvaluation(
        evaluation_id=f'topology-evaluation-{digest[:24]}',
        comparison_id=COMPARISON_ID,
        comparison_semantic_sha256=COMPARISON_SHA,
        bundles=bundle_refs,
        eligibility=eligibility,
        pareto_objective_ids=(OBJECTIVE_ID,),
        pareto_result=nominal_pareto,
        created_at_utc=NOW,
        evaluation_sha256=digest,
    )


def _proposal_spec(
    bundle: VariantEvaluationBundle,
    *,
    discriminator: str,
) -> ProposalRobustnessSpec:
    axis = UncertaintyAxis(
        axis_id='speaker-x',
        entity_id=f'speaker-{discriminator}',
        parameter='speaker_x_m',
        unit='m',
        nominal_value=1.0,
        minus_delta=0.1,
        plus_delta=0.1,
    )
    payload = {
        'schema_version': 1,
        'authority_version': 'o100f-proposal-robustness-1',
        'document_id': 'fixture-document',
        'scene_revision_id': 'fixture-revision',
        'scene_content_hash': '1' * 64,
        'template_variant_id': f'template-{discriminator}',
        'template_variant_sha256': '2' * 64,
        'candidate_variant_id': bundle.variant_id,
        'candidate_variant_sha256': bundle.variant_sha256,
        'topology_search_id': f'search-{discriminator}',
        'topology_search_sha256': '3' * 64,
        'topology_candidate_id': f'candidate-{discriminator}',
        'topology_candidate_sha256': '4' * 64,
        'candidate_set_sha256': '5' * 64,
        'materialized_scene_content_hash': '6' * 64,
        'constraint_snapshot_sha256': '7' * 64,
        'g10_constraint_spec_sha256': '8' * 64,
        'nominal_bundle_id': bundle.bundle_id,
        'nominal_bundle_sha256': bundle.bundle_sha256,
        'objective_ids': [OBJECTIVE_ID],
        'objective_contract_sha256': '9' * 64,
        'axes': [axis.model_dump(mode='json')],
        'sampling_strategy': 'deterministic_local_stencil',
        'algorithm_version': 'o90a-local-stencil-1',
        'software_version': 'fixture-1',
    }
    digest = canonical_robustness_sha256(payload)
    return ProposalRobustnessSpec(
        **payload,
        robustness_spec_id=f'proposal-robustness:{digest}',
        robustness_spec_sha256=digest,
        created_at_utc=NOW,
    )


def _robustness_evaluation(
    spec: ProposalRobustnessSpec,
    bundle: VariantEvaluationBundle,
    *,
    worst_value: float,
) -> RobustnessEvaluation:
    nominal = bundle.objective_vector.metric(OBJECTIVE_ID)
    identity = {
        'schema_version': 1,
        'robustness_spec_id': spec.robustness_spec_id,
        'robustness_spec_sha256': spec.robustness_spec_sha256,
        'candidate_id': bundle.variant_id,
        'objective_id': OBJECTIVE_ID,
        'objective_unit': nominal.unit,
        'direction': nominal.direction,
        'objective_definition': nominal.definition.model_dump(mode='json'),
        'nominal_sample_id': f'nominal:{bundle.variant_id}',
        'nominal_value': float(nominal.value),
        'local_sensitivities': [],
        'sampled_worst_semantics': 'sampled_worst',
        'sampled_worst_sample_id': f'worst:{bundle.variant_id}',
        'sampled_worst_value': worst_value,
        'sample_ids': [
            f'nominal:{bundle.variant_id}',
            f'worst:{bundle.variant_id}',
        ],
        'infeasible_sample_ids': [],
        'failed_sample_ids': [],
    }
    digest = canonical_robustness_sha256(identity)
    return RobustnessEvaluation(
        **identity,
        evaluation_id=f're-{digest[:24]}',
        evaluation_sha256=digest,
        created_at_utc=NOW,
    )


def _fixture():
    bundle_a = _bundle('variant-a', 'c' * 64, 1.0)
    bundle_b = _bundle('variant-b', 'e' * 64, 2.0)
    topology = _topology_evaluation((bundle_a, bundle_b))
    spec_a = _proposal_spec(bundle_a, discriminator='a')
    spec_b = _proposal_spec(bundle_b, discriminator='b')
    eval_a = _robustness_evaluation(spec_a, bundle_a, worst_value=3.0)
    eval_b = _robustness_evaluation(spec_b, bundle_b, worst_value=2.5)
    selection = RobustParetoSelection(
        nominal_objective_ids=(OBJECTIVE_ID,),
        robustness_objective_ids=(OBJECTIVE_ID,),
    )
    return topology, (bundle_a, bundle_b), (spec_a, spec_b), {
        'variant-a': (eval_a,),
        'variant-b': (eval_b,),
    }, selection


class _TopologyResolver:
    def __init__(
        self,
        path: Path,
        topology: TopologyComparisonEvaluation,
        bundles: tuple[VariantEvaluationBundle, ...],
    ) -> None:
        self.path = Path(path)
        self.topology = topology
        self.bundles = {item.bundle_id: item for item in bundles}

    def get_evaluation(self, evaluation_id: str):
        if evaluation_id == self.topology.evaluation_id:
            return self.topology
        return None

    def get_bundle(self, bundle_id: str):
        return self.bundles.get(bundle_id)


class _RobustnessResolver:
    def __init__(
        self,
        path: Path,
        specs: tuple[ProposalRobustnessSpec, ...],
        evaluations,
    ) -> None:
        self.path = Path(path)
        self.specs = {item.robustness_spec_id: item for item in specs}
        self.evaluations = {
            spec.robustness_spec_id: tuple(evaluations[spec.candidate_variant_id])
            for spec in specs
        }

    def get_spec(self, robustness_spec_id: str):
        return self.specs.get(robustness_spec_id)

    def list_evaluations(self, robustness_spec_id: str):
        return self.evaluations.get(robustness_spec_id, ())


def test_proposal_robust_pareto_reuses_existing_nominal_robust_tradeoff() -> None:
    topology, bundles, specs, evaluations, selection = _fixture()

    result = build_proposal_robust_pareto_evaluation(
        topology_evaluation=topology,
        bundles=bundles,
        robustness_specs=specs,
        robustness_evaluations=evaluations,
        selection=selection,
        created_at_utc=NOW,
    )

    assert topology.pareto_result.non_dominated_candidate_ids == ('variant-a',)
    assert set(result.pareto_result.non_dominated_candidate_ids) == {
        'variant-a',
        'variant-b',
    }
    assert result.pareto_result.objective_ids == (
        f'nominal::{OBJECTIVE_ID}',
        f'robust.sampled_worst::{OBJECTIVE_ID}',
    )
    assert tuple(item.variant_id for item in result.candidates) == (
        'variant-a',
        'variant-b',
    )


def test_proposal_robust_pareto_requires_complete_o100d_eligible_set() -> None:
    topology, bundles, specs, evaluations, selection = _fixture()

    with pytest.raises(
        ValueError,
        match='bundles must equal O100D eligible candidate set',
    ):
        build_proposal_robust_pareto_evaluation(
            topology_evaluation=topology,
            bundles=bundles[:1],
            robustness_specs=specs[:1],
            robustness_evaluations={
                'variant-a': evaluations['variant-a'],
            },
            selection=selection,
            created_at_utc=NOW,
        )


def test_proposal_robust_pareto_save_reopen_reresolves_exact_authorities(
    tmp_path: Path,
) -> None:
    topology, bundles, specs, evaluations, selection = _fixture()
    result = build_proposal_robust_pareto_evaluation(
        topology_evaluation=topology,
        bundles=bundles,
        robustness_specs=specs,
        robustness_evaluations=evaluations,
        selection=selection,
        created_at_utc=NOW,
    )

    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    topology_resolver = _TopologyResolver(
        scene_repository.path,
        topology,
        bundles,
    )
    robustness_resolver = _RobustnessResolver(
        scene_repository.path,
        specs,
        evaluations,
    )
    repository = CadProposalRobustParetoRepository(
        scene_repository=scene_repository,
        topology_comparison_repository=topology_resolver,
        proposal_robustness_repository=robustness_resolver,
    )
    repository.save(result)

    reopened = CadProposalRobustParetoRepository(
        scene_repository=SceneRepository(scene_repository.path),
        topology_comparison_repository=topology_resolver,
        proposal_robustness_repository=robustness_resolver,
    )
    assert reopened.get(result.evaluation_id) == result

    stale_ref = result.candidates[0].robustness_evaluations[0]
    robustness_resolver.evaluations[
        result.candidates[0].robustness_spec_id
    ] = ()
    with pytest.raises(
        ValueError,
        match='robustness evaluation disappeared',
    ):
        reopened.get(result.evaluation_id)
