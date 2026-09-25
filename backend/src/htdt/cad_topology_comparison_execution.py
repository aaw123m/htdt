"""O100D canonical comparison execution (#982, #988).

``SystemExpansionWorkflowService.comparison()`` is a presentation reader over
already-persisted comparison rows — nothing in the product previously took
selected SystemVariants, ran the typed O100D evaluators, and persisted the
spec/bundle/evaluation graph. This module is the single execution authority
that closes that gap:

    baseline + selected exact SystemVariants + explicit evaluation policy
    -> SystemTopologyComparisonSpec      (persisted, content-addressed)
    -> per-variant typed evaluations     (persisted; only what capability
                                          and equipment bindings support)
    -> VariantEvaluationBundle records   (exact refs + canonical vectors)
    -> TopologyComparisonEvaluation      (eligibility + Pareto)

Capability gating is explicit: a lane that cannot run for a candidate —
missing equipment binding, no persisted directivity dataset, absent
authority — contributes nothing to that candidate's bundle. The bundle
validator then reports the missing objective explicitly (never a zero or
favorable placeholder) and the candidate is ineligible.

Cost/budget evidence (#988) rides the same path: when a ``CostScenario`` is
supplied, ``evaluate_variant_installation_cost`` runs per candidate and the
independent cost/effort ObjectiveVector axes bind to the persisted
``VariantCostEvaluation`` through ``installation_evidence_refs``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .cad_amplifier_headroom import (
    AmplifierOutputCapability,
    PlaybackRouting,
    SimultaneousChannelCondition,
    SpeakerElectricalLoadAuthority,
    ElectricalQuantity,
    ElectricalValue,
    amplifier_headroom_objective_definitions,
    amplifier_headroom_objective_vector,
    build_playback_chain_scenario,
    evaluate_playback_chain,
)
from .cad_amplifier_headroom_repository import CadAmplifierHeadroomRepository
from .cad_coverage import (
    FrequencyAggregationSemantics,
    build_coverage_evaluation_scenario,
    coverage_objective_definitions,
    coverage_objective_vector,
    evaluate_coverage,
)
from .cad_coverage_repository import CadCoverageRepository
from .cad_current_equipment import current_equipment_projection
from .cad_direct_level import (
    DirectLevelFrequencyBand,
    ReferenceInputCondition,
    SeatPopulation,
    build_playback_excitation_scenario,
    direct_level_objective_definitions,
    direct_level_objective_vector,
    evaluate_direct_level,
)
from .cad_direct_level_repository import CadDirectLevelRepository
from .cad_directivity_repository import CadDirectivityRepository
from .cad_equipment import EquipmentDefinition
from .cad_equipment_repository import CadEquipmentRepository
from .cad_installation_cost import (
    CostScenario,
    VariantCostEvaluation,
    evaluate_variant_installation_cost,
    installation_cost_objective_vector,
)
from .cad_installation_cost_repository import CadInstallationCostRepository
from .cad_repository import SceneRepository, SceneRevision
from .cad_standards import (
    CriterionObservation,
    StandardsEvaluationTarget,
    StandardsProfile,
    evaluate_standards_profile,
)
from .cad_standards_repository import CadStandardsRepository
from .cad_system_variant import SystemVariant, materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_topology_comparison import (
    ExactAuthorityRef,
    ObjectiveEvidenceBinding,
    ResolvedObjectiveAuthority,
    SystemTopologyComparisonSpec,
    TopologyComparisonEvaluation,
    VariantEvaluationBundle,
    VariantRole,
    amplifier_headroom_evaluation_ref,
    build_system_topology_comparison_spec,
    build_variant_evaluation_bundle,
    compared_system_variant,
    coverage_evaluation_ref,
    direct_level_evaluation_ref,
    evaluate_topology_comparison,
    standards_evaluation_ref,
)
from .cad_topology_comparison_repository import (
    AuthorityResolver,
    CadTopologyComparisonRepository,
)
from .optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveVector,
)


COST_EVALUATION_AUTHORITY_KIND = 'installation_cost_evaluation'


def installation_cost_evaluation_ref(
    evaluation: VariantCostEvaluation,
) -> ExactAuthorityRef:
    """Exact ref for a persisted ``VariantCostEvaluation`` (#988)."""
    return ExactAuthorityRef(
        authority_kind=COST_EVALUATION_AUTHORITY_KIND,
        authority_id=evaluation.evaluation_id,
        authority_version=evaluation.authority_version,
        semantic_sha256=evaluation.evaluation_sha256,
        evaluator_id='o100c-installation-cost',
        evaluator_version=evaluation.authority_version,
        model_id='o100c-installation-cost-objectives',
        model_version=evaluation.scenario.effort_model_version,
        fidelity='deterministic-exact',
    )


def installation_cost_resolver(
    repository: CadInstallationCostRepository,
) -> AuthorityResolver:
    """Comparison-repository resolver for cost/effort objective metrics (#988)."""

    def resolve(authority_id: str):
        evaluation = repository.get_evaluation(authority_id)
        if evaluation is None:
            return None
        return ResolvedObjectiveAuthority(
            ref=installation_cost_evaluation_ref(evaluation),
            objective_vector=installation_cost_objective_vector(evaluation),
        )

    return resolve


@dataclass(frozen=True)
class CoverageLanePolicy:
    """Explicit coverage-evaluation inputs shared by every candidate."""

    source_entity_id: str
    channel_role_id: str
    seat_entity_ids: tuple[str, ...]
    evaluation_frequencies_hz: tuple[float, ...]
    coverage_threshold_db: float
    frequency_aggregation_semantics: FrequencyAggregationSemantics = (
        'worst_over_requested_frequencies'
    )
    population_id: str = 'comparison-seat-population'
    directivity_dataset_id: str | None = None


@dataclass(frozen=True)
class DirectLevelLanePolicy:
    """Explicit direct-level excitation inputs shared by every candidate."""

    source_entity_id: str
    channel_role_id: str
    seat_entity_ids: tuple[str, ...]
    reference_input: ReferenceInputCondition
    target_spl_db_spl: float
    frequency_band: DirectLevelFrequencyBand
    target_reference_condition: str
    continuous_reference_duration_s: float = 60.0
    peak_reference_duration_s: float = 0.1
    weighting: str = 'unweighted'
    population_id: str = 'comparison-seat-population'


@dataclass(frozen=True)
class PlaybackChainLanePolicy:
    """Explicit amplifier/playback-chain inputs shared by every candidate."""

    amplifier_capability: AmplifierOutputCapability
    routing: PlaybackRouting
    requested_input: ElectricalValue
    requested_output_quantity: ElectricalQuantity
    requested_continuous_output_value: float
    requested_peak_output_value: float
    target_spl_db_spl: float
    target_reference_condition: str
    acoustic_target_distance_m: float
    frequency_band: DirectLevelFrequencyBand
    speaker_load: SpeakerElectricalLoadAuthority | None = None
    source_entity_id: str | None = None
    continuous_duration_s: float = 60.0
    peak_duration_s: float = 0.1
    weighting: str = 'unweighted'
    target_mode: str = 'continuous'
    simultaneous_channel_condition: SimultaneousChannelCondition | None = None


@dataclass(frozen=True)
class ComparisonCandidate:
    """One exact SystemVariant entered into the comparison."""

    variant: SystemVariant
    role: VariantRole = 'proposed'
    comparison_label: str | None = None


@dataclass(frozen=True)
class ComparisonEvaluationPolicy:
    """Explicit capability-gated evaluation plan for one comparison (#982).

    A ``None`` lane is excluded from the spec's objective declarations — the
    comparison simply carries no authority for it. A lane that is declared
    but cannot run for a candidate yields explicit ``unsupported`` metrics on
    that candidate's bundle.
    """

    coverage: CoverageLanePolicy | None = None
    direct_level: DirectLevelLanePolicy | None = None
    playback_chain: PlaybackChainLanePolicy | None = None
    cost_scenario: CostScenario | None = None
    standards_observations: Mapping[
        str, tuple[CriterionObservation, ...]
    ] = field(default_factory=dict)
    standards_applicable_domains: tuple[str, ...] = ('*',)
    # Objective ids produced by the plan that count toward eligibility.
    # Empty means "every produced objective is required"; ids listed in
    # ``optional_objective_ids`` are demoted to Pareto-only axes.
    required_objective_ids: frozenset[str] = frozenset()
    optional_objective_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LaneStatus:
    """Per-candidate lane outcome — why evidence exists or is unsupported."""

    lane: str
    state: str  # 'evaluated' | 'unsupported'
    reason: str | None = None
    authority_id: str | None = None


@dataclass(frozen=True)
class CandidateExecution:
    variant: SystemVariant
    bundle: VariantEvaluationBundle | None
    lane_statuses: tuple[LaneStatus, ...]


@dataclass(frozen=True)
class TopologyComparisonExecution:
    """The persisted result graph of one O100D comparison run."""

    spec: SystemTopologyComparisonSpec
    bundles: tuple[VariantEvaluationBundle, ...]
    evaluation: TopologyComparisonEvaluation
    candidates: tuple[CandidateExecution, ...]


@dataclass(frozen=True)
class _LanePlan:
    state: str  # 'planned' | 'unsupported'
    reason: str | None = None
    scenario: object | None = None
    equipment: EquipmentDefinition | None = None
    dataset: object | None = None
    policy: object | None = None


def _resolve_bound_equipment(
    *,
    variant: SystemVariant,
    entity_id: str,
    equipment_repository: CadEquipmentRepository,
) -> EquipmentDefinition | None:
    bindings = [
        binding
        for binding in variant.equipment_bindings
        if binding.entity_id == entity_id
    ]
    if len(bindings) != 1:
        return None
    binding = bindings[0]
    return equipment_repository.get_definition_by_hash(
        binding.equipment_definition_sha256
    )


def execute_topology_comparison(
    *,
    scene_repository: SceneRepository,
    variant_repository: CadSystemVariantRepository,
    comparison_repository: CadTopologyComparisonRepository,
    standards_repository: CadStandardsRepository,
    equipment_repository: CadEquipmentRepository,
    baseline: SceneRevision,
    name: str,
    standards_profile: StandardsProfile,
    candidates: Sequence[ComparisonCandidate | SystemVariant],
    policy: ComparisonEvaluationPolicy | None = None,
    include_current: bool = False,
    current_label: str = '現在',
    coverage_repository: CadCoverageRepository | None = None,
    direct_level_repository: CadDirectLevelRepository | None = None,
    amplifier_headroom_repository: CadAmplifierHeadroomRepository | None = None,
    directivity_repository: CadDirectivityRepository | None = None,
    cost_repository: CadInstallationCostRepository | None = None,
    equipment_binding_repository=None,
    created_at_utc: str,
) -> TopologyComparisonExecution:
    """Run the canonical O100D sequence and persist the comparison graph.

    Every authority the run touches — spec, typed evaluations, bundles,
    evaluation — is persisted through its repository, so reopening the
    comparison later replays exact hashes rather than trusting caches.
    """
    policy = policy or ComparisonEvaluationPolicy()
    standards_repository.save_profile(standards_profile)

    candidate_inputs: list[ComparisonCandidate] = []
    if include_current:
        projection = current_equipment_projection(
            scene_repository=scene_repository,
            variant_repository=variant_repository,
            revision_id=baseline.revision_id,
            binding_repository=equipment_binding_repository,
        )
        existing_projection = variant_repository.get_variant(
            projection.variant_id
        )
        if (
            existing_projection is None
            or existing_projection.variant_sha256 != projection.variant_sha256
        ):
            variant_repository.save_variant(projection)
        candidate_inputs.append(
            ComparisonCandidate(
                variant=projection,
                role='current',
                comparison_label=current_label,
            )
        )
    for item in candidates:
        if isinstance(item, SystemVariant):
            candidate_inputs.append(
                ComparisonCandidate(
                    variant=item,
                    role='proposed',
                    comparison_label=item.name,
                )
            )
        else:
            candidate_inputs.append(item)

    if len(candidate_inputs) < 2:
        raise ValueError(
            'a SystemTopologyComparisonSpec requires at least two '
            'candidates; pass include_current=True or more proposals'
        )
    variants = [item.variant for item in candidate_inputs]
    for variant in variants:
        if (
            variant.document_id != baseline.document_id
            or variant.baseline_revision_id != baseline.revision_id
            or variant.baseline_content_hash != baseline.content_hash
        ):
            raise ValueError(
                'comparison candidate is not bound to the baseline revision'
            )
        existing_variant = variant_repository.get_variant(
            variant.variant_id
        )
        if (
            existing_variant is None
            or existing_variant.variant_sha256 != variant.variant_sha256
        ):
            variant_repository.save_variant(variant)

    materialized = {
        variant.variant_id: materialize_system_variant(baseline, variant)
        for variant in variants
    }

    # ------------------------------------------------------------------
    # Phase 1 — per-candidate lane planning (capability gating).
    # ------------------------------------------------------------------
    coverage_plans: dict[str, _LanePlan] = {}
    direct_level_plans: dict[str, _LanePlan] = {}
    playback_plans: dict[str, _LanePlan] = {}
    cost_plans: dict[str, _LanePlan] = {}

    for variant in variants:
        scene = materialized[variant.variant_id]

        coverage = policy.coverage
        if coverage is None:
            pass
        elif coverage_repository is None or directivity_repository is None:
            coverage_plans[variant.variant_id] = _LanePlan(
                state='unsupported',
                reason='coverage repositories are not configured',
            )
        else:
            plan = _plan_coverage(
                variant=variant,
                scene=scene,
                coverage=coverage,
                equipment_repository=equipment_repository,
                directivity_repository=directivity_repository,
            )
            coverage_plans[variant.variant_id] = plan

        direct_level = policy.direct_level
        if direct_level is None:
            pass
        elif direct_level_repository is None:
            direct_level_plans[variant.variant_id] = _LanePlan(
                state='unsupported',
                reason='direct-level repository is not configured',
            )
        else:
            direct_level_plans[variant.variant_id] = _plan_direct_level(
                variant=variant,
                scene=scene,
                policy=direct_level,
                equipment_repository=equipment_repository,
            )

        playback = policy.playback_chain
        if playback is None:
            pass
        elif amplifier_headroom_repository is None:
            playback_plans[variant.variant_id] = _LanePlan(
                state='unsupported',
                reason='amplifier headroom repository is not configured',
            )
        else:
            playback_plans[variant.variant_id] = _plan_playback_chain(
                variant=variant,
                baseline=baseline,
                policy=playback,
                equipment_repository=equipment_repository,
            )

        if policy.cost_scenario is not None:
            if cost_repository is None:
                cost_plans[variant.variant_id] = _LanePlan(
                    state='unsupported',
                    reason='cost repository is not configured',
                )
            else:
                cost_plans[variant.variant_id] = _LanePlan(state='planned')

    # ------------------------------------------------------------------
    # Phase 2 — canonical objective declarations.
    # Spec objectives bind the first candidate's scenario per lane; a
    # candidate whose resolved scenario differs keeps its own definitions
    # and receives explicit ineligibility instead of a silent comparison.
    # ------------------------------------------------------------------
    coverage_defs: tuple[ObjectiveDefinition, ...] = ()
    direct_level_defs: tuple[ObjectiveDefinition, ...] = ()
    playback_defs: tuple[ObjectiveDefinition, ...] = ()
    cost_defs: tuple[ObjectiveDefinition, ...] = ()

    for variant in variants:
        plan = coverage_plans.get(variant.variant_id)
        if plan is not None and plan.state == 'planned':
            coverage_defs = coverage_objective_definitions(plan.scenario)
            break
    for variant in variants:
        plan = direct_level_plans.get(variant.variant_id)
        if plan is not None and plan.state == 'planned':
            direct_level_defs = direct_level_objective_definitions(
                plan.scenario
            )
            break
    for variant in variants:
        plan = playback_plans.get(variant.variant_id)
        if plan is not None and plan.state == 'planned':
            playback_defs = amplifier_headroom_objective_definitions(
                plan.scenario
            )
            break

    cost_evaluations: dict[str, VariantCostEvaluation] = {}
    if policy.cost_scenario is not None and cost_repository is not None:
        records = cost_repository.list_records(baseline.document_id)
        for variant in variants:
            evaluation = evaluate_variant_installation_cost(
                revision=baseline,
                variant=variant,
                scenario=policy.cost_scenario,
                cost_records=records,
            )
            cost_repository.save_evaluation(evaluation)
            cost_evaluations[variant.variant_id] = evaluation
        if cost_evaluations:
            first = next(iter(cost_evaluations.values()))
            cost_defs = tuple(
                metric.definition
                for metric in installation_cost_objective_vector(
                    first
                ).metrics
                if metric.definition is not None
            )

    all_definitions = (
        *coverage_defs, *direct_level_defs, *playback_defs, *cost_defs
    )
    required: list[ObjectiveDefinition] = []
    optional: list[ObjectiveDefinition] = []
    known_ids = {item.objective_id for item in all_definitions}
    unknown_required = set(policy.required_objective_ids) - known_ids
    if unknown_required:
        raise ValueError(
            'required objectives are not produced by the evaluation plan: '
            f'{sorted(unknown_required)}'
        )
    unknown_optional = set(policy.optional_objective_ids) - known_ids
    if unknown_optional:
        raise ValueError(
            'optional objectives are not produced by the evaluation plan: '
            f'{sorted(unknown_optional)}'
        )
    for definition in all_definitions:
        demoted = definition.objective_id in policy.optional_objective_ids
        promoted = definition.objective_id in policy.required_objective_ids
        if policy.required_objective_ids:
            required.append(definition) if promoted else optional.append(
                definition
            )
        elif demoted:
            optional.append(definition)
        else:
            required.append(definition)
    if not required:
        raise ValueError(
            'comparison requires at least one required objective; the '
            'evaluation plan produced none'
        )

    spec = build_system_topology_comparison_spec(
        name=name,
        baseline=baseline,
        candidate_variants=tuple(
            compared_system_variant(
                item.variant,
                role=item.role,
                comparison_label=(
                    item.comparison_label
                    if item.comparison_label is not None
                    else item.variant.name
                ),
            )
            for item in candidate_inputs
        ),
        required_objectives=tuple(required),
        optional_objectives=tuple(optional),
        standards_profile=standards_profile,
    )
    comparison_repository.save_spec(spec)

    # ------------------------------------------------------------------
    # Phase 3 — per-candidate evaluation + bundle persistence.
    # A lane that cannot run contributes no metrics: every bundle metric
    # must bind an exact evaluation authority, so capability gaps surface
    # as missing-objective eligibility issues and lane statuses — never
    # as zero or favorable placeholder values.
    # ------------------------------------------------------------------
    executions: list[CandidateExecution] = []
    bundles: list[VariantEvaluationBundle] = []
    for variant in variants:
        scene = materialized[variant.variant_id]
        lane_statuses: list[LaneStatus] = []
        metrics: list[ObjectiveMetric] = []
        evidence: list[ObjectiveEvidenceBinding] = []

        coverage_ref = None
        coverage_eval = _run_coverage(
            variant=variant,
            baseline=baseline,
            plan=coverage_plans.get(variant.variant_id),
            repository=coverage_repository,
            lane_statuses=lane_statuses,
            metrics=metrics,
            evidence=evidence,
        )
        if coverage_eval is not None:
            coverage_ref = coverage_evaluation_ref(coverage_eval)

        direct_ref = None
        direct_eval = _run_direct_level(
            variant=variant,
            baseline=baseline,
            plan=direct_level_plans.get(variant.variant_id),
            repository=direct_level_repository,
            lane_statuses=lane_statuses,
            metrics=metrics,
            evidence=evidence,
        )
        if direct_eval is not None:
            direct_ref = direct_level_evaluation_ref(direct_eval)

        playback_ref = None
        playback_eval = _run_playback_chain(
            variant=variant,
            baseline=baseline,
            plan=playback_plans.get(variant.variant_id),
            repository=amplifier_headroom_repository,
            lane_statuses=lane_statuses,
            metrics=metrics,
            evidence=evidence,
        )
        if playback_eval is not None:
            playback_ref = amplifier_headroom_evaluation_ref(playback_eval)

        cost_ref = None
        installation_refs: tuple[ExactAuthorityRef, ...] = ()
        cost_evaluation = cost_evaluations.get(variant.variant_id)
        if cost_evaluation is not None:
            cost_ref = installation_cost_evaluation_ref(cost_evaluation)
            installation_refs = (cost_ref,)
            cost_vector = installation_cost_objective_vector(cost_evaluation)
            metrics.extend(cost_vector.metrics)
            for metric in cost_vector.metrics:
                evidence.append(
                    ObjectiveEvidenceBinding(
                        objective_id=metric.objective_id,
                        source_authority_kind=cost_ref.authority_kind,
                        source_authority_id=cost_ref.authority_id,
                        source_semantic_sha256=cost_ref.semantic_sha256,
                    )
                )
            lane_statuses.append(
                LaneStatus(
                    lane='installation_cost',
                    state='evaluated',
                    authority_id=cost_evaluation.evaluation_id,
                )
            )
        elif policy.cost_scenario is not None:
            lane_statuses.append(
                LaneStatus(
                    lane='installation_cost',
                    state='unsupported',
                    reason='cost repository is not configured',
                )
            )

        standards_observations = tuple(
            policy.standards_observations.get(variant.variant_id, ())
        )
        standards_eval = evaluate_standards_profile(
            profile=standards_profile,
            target=StandardsEvaluationTarget(
                document_id=baseline.document_id,
                scene_revision_id=baseline.revision_id,
                scene_content_hash=baseline.content_hash,
                system_variant_id=variant.variant_id,
                system_variant_sha256=variant.variant_sha256,
                entity_ids=tuple(
                    entity.entity_id for entity in scene.entities
                ),
                applicable_domains=policy.standards_applicable_domains,
            ),
            observations=standards_observations,
            created_at_utc=created_at_utc,
        )
        standards_repository.save_evaluation(standards_eval)
        lane_statuses.append(
            LaneStatus(
                lane='standards',
                state='evaluated',
                authority_id=standards_eval.evaluation_id,
            )
        )

        bundle: VariantEvaluationBundle | None = None
        if metrics:
            bundle = build_variant_evaluation_bundle(
                spec=spec,
                variant=variant,
                objective_vector=ObjectiveVector(
                    candidate_id=variant.variant_id,
                    metrics=tuple(metrics),
                ),
                objective_evidence=tuple(evidence),
                coverage_evaluation=coverage_ref,
                direct_level_evaluation=direct_ref,
                amplifier_headroom_evaluation=playback_ref,
                standards_evaluation=standards_evaluation_ref(standards_eval),
                installation_evidence_refs=installation_refs,
            )
            comparison_repository.save_bundle(bundle)
            bundles.append(bundle)
        executions.append(
            CandidateExecution(
                variant=variant,
                bundle=bundle,
                lane_statuses=tuple(lane_statuses),
            )
        )

    evaluation = evaluate_topology_comparison(
        spec=spec,
        bundles=tuple(bundles),
        created_at_utc=created_at_utc,
    )
    comparison_repository.save_evaluation(evaluation)

    return TopologyComparisonExecution(
        spec=spec,
        bundles=tuple(bundles),
        evaluation=evaluation,
        candidates=tuple(executions),
    )


def _source_speaker(
    scene,
    *,
    entity_id: str,
    channel_role_id: str,
):
    try:
        entity = scene.entity(entity_id)
    except KeyError:
        return None, f'評価対象スピーカー {entity_id} が候補に存在しません'
    if entity.kind != 'speaker':
        return None, f'{entity_id} はスピーカーではありません'
    if entity.speaker_role != channel_role_id:
        return None, (
            f'{entity_id} のチャンネルロール {entity.speaker_role} が '
            f'{channel_role_id} と一致しません'
        )
    return entity, None


def _plan_coverage(
    *,
    variant: SystemVariant,
    scene,
    coverage: CoverageLanePolicy,
    equipment_repository: CadEquipmentRepository,
    directivity_repository: CadDirectivityRepository,
) -> _LanePlan:
    _entity, reason = _source_speaker(
        scene,
        entity_id=coverage.source_entity_id,
        channel_role_id=coverage.channel_role_id,
    )
    if reason is not None:
        return _LanePlan(state='unsupported', reason=reason)
    equipment = _resolve_bound_equipment(
        variant=variant,
        entity_id=coverage.source_entity_id,
        equipment_repository=equipment_repository,
    )
    if equipment is None:
        return _LanePlan(
            state='unsupported',
            reason='評価対象スピーカーに一意の機器バインドがありません',
        )
    datasets = directivity_repository.list_datasets_for_definition(
        equipment.semantic_sha256
    )
    if coverage.directivity_dataset_id is not None:
        selected = [
            item
            for item in datasets
            if item.dataset_id == coverage.directivity_dataset_id
        ]
        if not selected:
            return _LanePlan(
                state='unsupported',
                reason='指定された DirectivityDataset が機器にバインドされていません',
            )
        dataset = selected[0]
    elif len(datasets) == 1:
        dataset = datasets[0]
    else:
        return _LanePlan(
            state='unsupported',
            reason=(
                'DirectivityDataset が一意に決まりません '
                f'({len(datasets)}件)'
            ),
        )
    scenario = build_coverage_evaluation_scenario(
        source_entity_id=coverage.source_entity_id,
        channel_role_id=coverage.channel_role_id,
        receiver_population=SeatPopulation(
            population_id=coverage.population_id,
            seat_entity_ids=coverage.seat_entity_ids,
        ),
        directivity_dataset=dataset,
        equipment_definition=equipment,
        evaluation_frequencies_hz=coverage.evaluation_frequencies_hz,
        frequency_aggregation_semantics=(
            coverage.frequency_aggregation_semantics
        ),
        coverage_threshold_db=coverage.coverage_threshold_db,
    )
    return _LanePlan(
        state='planned',
        scenario=scenario,
        equipment=equipment,
        dataset=dataset,
    )


def _plan_direct_level(
    *,
    variant: SystemVariant,
    scene,
    policy: DirectLevelLanePolicy,
    equipment_repository: CadEquipmentRepository,
) -> _LanePlan:
    _entity, reason = _source_speaker(
        scene,
        entity_id=policy.source_entity_id,
        channel_role_id=policy.channel_role_id,
    )
    if reason is not None:
        return _LanePlan(state='unsupported', reason=reason)
    equipment = _resolve_bound_equipment(
        variant=variant,
        entity_id=policy.source_entity_id,
        equipment_repository=equipment_repository,
    )
    if equipment is None:
        return _LanePlan(
            state='unsupported',
            reason='評価対象スピーカーに一意の機器バインドがありません',
        )
    scenario = build_playback_excitation_scenario(
        source_entity_id=policy.source_entity_id,
        channel_role_id=policy.channel_role_id,
        reference_input=policy.reference_input,
        target_spl_db_spl=policy.target_spl_db_spl,
        target_reference_condition=policy.target_reference_condition,
        continuous_reference_duration_s=policy.continuous_reference_duration_s,
        peak_reference_duration_s=policy.peak_reference_duration_s,
        frequency_band=policy.frequency_band,
        weighting=policy.weighting,
        receiver_population=SeatPopulation(
            population_id=policy.population_id,
            seat_entity_ids=policy.seat_entity_ids,
        ),
    )
    return _LanePlan(
        state='planned', scenario=scenario, equipment=equipment
    )


def _plan_playback_chain(
    *,
    variant: SystemVariant,
    baseline: SceneRevision,
    policy: PlaybackChainLanePolicy,
    equipment_repository: CadEquipmentRepository,
) -> _LanePlan:
    equipment: EquipmentDefinition | None = None
    if policy.source_entity_id is not None:
        equipment = _resolve_bound_equipment(
            variant=variant,
            entity_id=policy.source_entity_id,
            equipment_repository=equipment_repository,
        )
        if equipment is None:
            return _LanePlan(
                state='unsupported',
                reason=(
                    '再生チェーンのソース機器バインドが一意に決まりません'
                ),
            )
    else:
        return _LanePlan(
            state='unsupported',
            reason='再生チェーンのソース機器が指定されていません',
        )
    scenario = build_playback_chain_scenario(
        revision=baseline,
        variant=variant,
        source_equipment=equipment,
        amplifier_capability=policy.amplifier_capability,
        speaker_load=policy.speaker_load,
        routing=policy.routing,
        requested_input=policy.requested_input,
        requested_output_quantity=policy.requested_output_quantity,
        requested_continuous_output_value=(
            policy.requested_continuous_output_value
        ),
        requested_peak_output_value=policy.requested_peak_output_value,
        continuous_duration_s=policy.continuous_duration_s,
        peak_duration_s=policy.peak_duration_s,
        frequency_band=policy.frequency_band,
        weighting=policy.weighting,
        target_spl_db_spl=policy.target_spl_db_spl,
        target_reference_condition=policy.target_reference_condition,
        acoustic_target_distance_m=policy.acoustic_target_distance_m,
        target_mode=policy.target_mode,
        simultaneous_channel_condition=(
            policy.simultaneous_channel_condition
            if policy.simultaneous_channel_condition is not None
            else SimultaneousChannelCondition(channels_driven=1)
        ),
    )
    return _LanePlan(
        state='planned',
        scenario=scenario,
        equipment=equipment,
        policy=policy,
    )


def _binding(
    metric: ObjectiveMetric,
    ref: ExactAuthorityRef,
) -> ObjectiveEvidenceBinding:
    return ObjectiveEvidenceBinding(
        objective_id=metric.objective_id,
        source_authority_kind=ref.authority_kind,
        source_authority_id=ref.authority_id,
        source_semantic_sha256=ref.semantic_sha256,
    )


def _run_coverage(
    *,
    variant: SystemVariant,
    baseline: SceneRevision,
    plan: _LanePlan | None,
    repository: CadCoverageRepository | None,
    lane_statuses: list[LaneStatus],
    metrics: list[ObjectiveMetric],
    evidence: list[ObjectiveEvidenceBinding],
):
    if plan is None:
        return None
    if plan.state != 'planned' or repository is None:
        lane_statuses.append(
            LaneStatus(
                lane='coverage',
                state='unsupported',
                reason=plan.reason,
            )
        )
        return None
    repository.save_scenario(plan.scenario)
    evaluation = evaluate_coverage(
        revision=baseline,
        variant=variant,
        equipment_definition=plan.equipment,
        directivity_dataset=plan.dataset,
        scenario=plan.scenario,
    )
    repository.save_evaluation(evaluation)
    ref = coverage_evaluation_ref(evaluation)
    vector = coverage_objective_vector(evaluation)
    metrics.extend(vector.metrics)
    evidence.extend(_binding(metric, ref) for metric in vector.metrics)
    lane_statuses.append(
        LaneStatus(
            lane='coverage',
            state='evaluated',
            authority_id=evaluation.evaluation_id,
        )
    )
    return evaluation


def _run_direct_level(
    *,
    variant: SystemVariant,
    baseline: SceneRevision,
    plan: _LanePlan | None,
    repository: CadDirectLevelRepository | None,
    lane_statuses: list[LaneStatus],
    metrics: list[ObjectiveMetric],
    evidence: list[ObjectiveEvidenceBinding],
):
    if plan is None:
        return None
    if plan.state != 'planned' or repository is None:
        lane_statuses.append(
            LaneStatus(
                lane='direct_level',
                state='unsupported',
                reason=plan.reason,
            )
        )
        return None
    repository.save_scenario(plan.scenario)
    evaluation = evaluate_direct_level(
        revision=baseline,
        variant=variant,
        equipment_definition=plan.equipment,
        scenario=plan.scenario,
    )
    repository.save_evaluation(evaluation)
    ref = direct_level_evaluation_ref(evaluation)
    vector = direct_level_objective_vector(evaluation)
    metrics.extend(vector.metrics)
    evidence.extend(_binding(metric, ref) for metric in vector.metrics)
    lane_statuses.append(
        LaneStatus(
            lane='direct_level',
            state='evaluated',
            authority_id=evaluation.evaluation_id,
        )
    )
    return evaluation


def _run_playback_chain(
    *,
    variant: SystemVariant,
    baseline: SceneRevision,
    plan: _LanePlan | None,
    repository: CadAmplifierHeadroomRepository | None,
    lane_statuses: list[LaneStatus],
    metrics: list[ObjectiveMetric],
    evidence: list[ObjectiveEvidenceBinding],
):
    if plan is None:
        return None
    if plan.state != 'planned' or repository is None:
        lane_statuses.append(
            LaneStatus(
                lane='playback_chain',
                state='unsupported',
                reason=plan.reason,
            )
        )
        return None
    repository.save_scenario(plan.scenario)
    chain_policy = plan.policy
    evaluation = evaluate_playback_chain(
        revision=baseline,
        variant=variant,
        equipment_definition=plan.equipment,
        amplifier_capability=chain_policy.amplifier_capability,
        speaker_load=chain_policy.speaker_load,
        scenario=plan.scenario,
    )
    repository.save_evaluation(evaluation)
    ref = amplifier_headroom_evaluation_ref(evaluation)
    vector = amplifier_headroom_objective_vector(evaluation)
    metrics.extend(vector.metrics)
    evidence.extend(_binding(metric, ref) for metric in vector.metrics)
    lane_statuses.append(
        LaneStatus(
            lane='playback_chain',
            state='evaluated',
            authority_id=evaluation.evaluation_id,
        )
    )
    return evaluation
