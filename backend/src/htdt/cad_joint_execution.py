"""#945: canonical headless joint-optimization execution service.

A persisted JointOptimizationSpec (#524 UI authoring) is turned into a
bounded stream of exact candidates:

    spec -> canonical decision vectors (respecting candidate_budget)
         -> materialized SystemVariant + candidate CalibrationPlan
         -> JointCandidate persisted through the #174 repository
         -> evaluated ObjectiveVector bound + persisted
         -> repository-derived Pareto front

Execution never mutates scene/device state: physical intent stays inside
SystemVariant/SceneRevision authority and DSP intent inside a derived
CalibrationPlan — device application remains the #452 lifecycle.
"""

from __future__ import annotations

from itertools import product
from math import isfinite
from typing import Any, Callable, NamedTuple, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_calibration import (
    CadCalibrationChannel,
    CadCalibrationPlan,
    evaluate_calibration_support,
)
from .cad_measurement_quality import CadMeasurementQualityReport
from .cad_joint_optimization import (
    JointCandidate,
    JointDecisionValue,
    JointDspVariable,
    JointEvaluationInputRef,
    JointOptimizationSpec,
    JointPhysicalVariableRef,
    bind_joint_candidate_evaluation,
    build_joint_candidate,
    canonical_joint_sha256,
    require_joint_decision_materialization,
    unsupported_joint_objective_vector,
)
from .cad_joint_optimization_repository import (
    CadJointOptimizationRepository,
)
from .cad_repository import SceneRevision
from .cad_scene import rotate_orientation_world
from .cad_system_variant import (
    ProposedEntitySpec,
    SystemVariant,
    VariantProvenanceItem,
    build_system_variant,
)
from .cad_extended_search import (
    aim_horizontal_yaw_deg,
    body_horizontal_yaw_deg,
    direction_with_aim_pitch,
    direction_with_horizontal_yaw,
)
from .optimization_objectives import ObjectiveVector
from .pareto import ParetoEmptyError, ParetoResult

JOINT_EXECUTION_AUTHORITY_VERSION = 'issue945-joint-execution-1'
_GRID_EPSILON = 1e-9


class JointEvaluationOutcome(NamedTuple):
    """What one candidate evaluation contributes to the authority."""

    objective_vector: ObjectiveVector
    input_refs: tuple[JointEvaluationInputRef, ...]


class JointExecutionCandidateContext(NamedTuple):
    """Exact per-candidate state handed to the vector evaluator."""

    spec: JointOptimizationSpec
    candidate: JointCandidate
    physical_variant: SystemVariant
    calibration_plan: CadCalibrationPlan | None


JointVectorEvaluator = Callable[
    [JointExecutionCandidateContext],
    JointEvaluationOutcome,
]


class JointExecutionResult(BaseModel):
    """Immutable summary of one bounded execution pass over a spec."""

    model_config = ConfigDict(frozen=True)

    authority_version: str = JOINT_EXECUTION_AUTHORITY_VERSION
    result_id: str = Field(min_length=1)
    parent_spec_id: str = Field(min_length=1)
    parent_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    decision_vectors_total: int = Field(ge=0)
    candidates_generated: int = Field(ge=0)
    candidates_reused: int = Field(ge=0)
    candidates_blocked: int = Field(ge=0)
    evaluations_recorded: int = Field(ge=0)
    budget_limited: bool = False
    cancelled: bool = False
    pareto_candidate_ids: tuple[str, ...] = ()
    execution_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_result(self) -> 'JointExecutionResult':
        if self.execution_sha256 != canonical_joint_sha256(
            self.semantic_payload()
        ):
            raise ValueError('JointExecutionResult hash mismatch')
        if self.result_id != (
            f'joint-execution-{self.execution_sha256[:24]}'
        ):
            raise ValueError('JointExecutionResult deterministic ID mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'parent_spec_id': self.parent_spec_id,
            'parent_spec_sha256': self.parent_spec_sha256,
            'decision_vectors_total': self.decision_vectors_total,
            'candidates_generated': self.candidates_generated,
            'candidates_reused': self.candidates_reused,
            'candidates_blocked': self.candidates_blocked,
            'evaluations_recorded': self.evaluations_recorded,
            'budget_limited': self.budget_limited,
            'cancelled': self.cancelled,
            'pareto_candidate_ids': list(self.pareto_candidate_ids),
        }


def _numeric_grid(
    minimum: float,
    maximum: float,
    step: float | None,
) -> tuple[float, ...]:
    if step is None:
        if abs(float(maximum) - float(minimum)) > _GRID_EPSILON:
            raise ValueError(
                'numeric decision variable without a step cannot span a range'
            )
        return (float(minimum),)
    values: list[float] = []
    value = float(minimum)
    while value <= float(maximum) + _GRID_EPSILON:
        if not isfinite(value):
            raise ValueError('numeric decision grid produced a non-finite value')
        values.append(min(value, float(maximum)))
        value += float(step)
    return tuple(dict.fromkeys(values))


def _variable_values(
    variable: JointPhysicalVariableRef | JointDspVariable,
    domain: str,
) -> tuple[tuple[JointDecisionValue, ...], ...]:
    if isinstance(variable, JointPhysicalVariableRef):
        grid = _numeric_grid(
            variable.minimum, variable.maximum, variable.step
        )
    elif variable.parameter == 'polarity':
        if not variable.allowed_values:
            raise ValueError(
                'polarity decision variable requires explicit allowed_values'
            )
        grid = tuple(variable.allowed_values)
    else:
        grid = _numeric_grid(
            variable.minimum, variable.maximum, variable.step
        )
    return tuple(
        (
            JointDecisionValue(
                domain=domain,
                variable_id=variable.variable_id,
                value=value,
            ),
        )
        for value in grid
    )


def joint_decision_vector_count(spec: JointOptimizationSpec) -> int:
    """Total Cartesian size ignoring the budget cap (may exceed it)."""

    total = 1
    for variable in spec.physical_variables:
        total *= len(_variable_values(variable, 'physical'))
    for variable in spec.dsp_variables:
        total *= len(_variable_values(variable, 'dsp'))
    return total


def enumerate_joint_decision_vectors(
    spec: JointOptimizationSpec,
) -> tuple[tuple[JointDecisionValue, ...], ...]:
    """Canonical, bounded decision-vector stream for one spec.

    Vectors are ordered by canonical variable order (physical first, then
    DSP, each by variable_id) and truncated at ``candidate_budget`` — the
    immutable budget is never exceeded.
    """

    axes: list[tuple[tuple[JointDecisionValue, ...], ...]] = []
    variables: list[tuple[str, Any]] = sorted(
        (('physical', item) for item in spec.physical_variables),
        key=lambda item: item[1].variable_id,
    ) + sorted(
        (('dsp', item) for item in spec.dsp_variables),
        key=lambda item: item[1].variable_id,
    )
    for domain, variable in variables:
        axes.append(_variable_values(variable, domain))
    vectors: list[tuple[JointDecisionValue, ...]] = []
    for combo in product(*axes):
        vector = tuple(
            sorted(
                (item for group in combo for item in group),
                key=lambda item: (item.domain, item.variable_id),
            )
        )
        vectors.append(vector)
        if len(vectors) >= spec.candidate_budget:
            break
    return tuple(vectors)


def _apply_physical_decisions(
    baseline: SceneRevision,
    decisions: Sequence[JointDecisionValue],
    spec: JointOptimizationSpec,
) -> dict[str, Any]:
    """Return entity_id -> updated SceneEntity for physical decisions."""

    physical_by_id = {
        item.variable_id: item for item in spec.physical_variables
    }
    replacements: dict[str, Any] = {}
    for decision in decisions:
        if decision.domain != 'physical':
            continue
        variable = physical_by_id.get(decision.variable_id)
        if variable is None:
            raise ValueError(
                f'unknown physical decision variable: {decision.variable_id}'
            )
        if not isinstance(decision.value, (float, int)):
            raise ValueError('physical decision values must be numeric')
        entity = replacements.get(variable.entity_id)
        if entity is None:
            try:
                entity = baseline.document.entity(variable.entity_id)
            except KeyError as exc:
                raise ValueError(
                    f'physical decision variable {decision.variable_id} '
                    'references an entity missing from the baseline revision'
                ) from exc
        target = float(decision.value)
        if variable.parameter == 'x_m':
            entity = entity.model_copy(update={
                'position': entity.position.model_copy(
                    update={'x_m': target}
                )
            })
        elif variable.parameter == 'y_m':
            entity = entity.model_copy(update={
                'position': entity.position.model_copy(
                    update={'y_m': target}
                )
            })
        elif variable.parameter == 'z_m':
            entity = entity.model_copy(update={
                'position': entity.position.model_copy(
                    update={'z_m': target}
                )
            })
        elif variable.parameter == 'aim_yaw_deg':
            if entity.aim_xyz is None:
                raise ValueError(
                    'aim decision requires an explicit speaker aim'
                )
            entity = entity.model_copy(update={
                'aim_xyz': direction_with_horizontal_yaw(
                    entity.aim_xyz, target
                )
            })
        elif variable.parameter == 'aim_pitch_deg':
            if entity.aim_xyz is None:
                raise ValueError(
                    'aim decision requires an explicit speaker aim'
                )
            entity = entity.model_copy(update={
                'aim_xyz': direction_with_aim_pitch(entity.aim_xyz, target)
            })
        elif variable.parameter == 'body_yaw_deg':
            if entity.kind != 'speaker' or entity.aim_xyz is None:
                raise ValueError(
                    'physical toe-in requires an explicit-aim speaker'
                )
            delta = (
                (target - body_horizontal_yaw_deg(entity) + 180.0) % 360.0
                - 180.0
            )
            entity = entity.model_copy(update={
                'orientation': rotate_orientation_world(
                    entity.orientation, 'z', delta
                ),
                'aim_xyz': direction_with_horizontal_yaw(
                    entity.aim_xyz,
                    aim_horizontal_yaw_deg(entity.aim_xyz) + delta,
                ),
            })
        else:
            raise ValueError(
                f'unsupported physical decision parameter: '
                f'{variable.parameter}'
            )
        replacements[variable.entity_id] = entity
    return replacements


def materialize_joint_physical_variant(
    *,
    spec: JointOptimizationSpec,
    baseline: SceneRevision,
    base_variant: SystemVariant,
    decisions: Sequence[JointDecisionValue],
    created_at_utc: str,
) -> SystemVariant:
    """Materialize physical decisions into an exact SystemVariant.

    DSP-only candidates reuse the exact base SystemVariant unchanged.
    """

    physical = tuple(
        item for item in decisions if item.domain == 'physical'
    )
    if not physical:
        if (
            base_variant.variant_id != spec.base_system_variant_id
            or base_variant.variant_sha256
            != spec.base_system_variant_sha256
        ):
            raise ValueError(
                'DSP-only candidate must reuse the exact base SystemVariant'
            )
        return base_variant

    replacements = _apply_physical_decisions(baseline, physical, spec)
    digest = canonical_joint_sha256({
        'spec': spec.spec_id,
        'decisions': [
            item.model_dump(mode='json') for item in physical
        ],
    })
    role_ids = {
        item.role_id for item in base_variant.role_bindings
    }
    proposals = tuple(
        ProposedEntitySpec(
            spec_id=f'joint-proposal-{entity_id}-{digest[:12]}',
            entity=entity,
            role_binding_id=(
                entity.speaker_role
                if entity.speaker_role in role_ids
                else None
            ),
            provenance=(
                VariantProvenanceItem(
                    key='joint_execution_spec',
                    value=spec.spec_id,
                ),
            ),
        )
        for entity_id, entity in sorted(replacements.items())
    )
    return build_system_variant(
        baseline=baseline,
        name=f'joint-candidate-{digest[:12]}',
        role_bindings=base_variant.role_bindings,
        proposed_entities=proposals,
        provenance=(
            VariantProvenanceItem(
                key='joint_execution_spec',
                value=spec.spec_id,
            ),
        ),
        created_at_utc=created_at_utc,
    )


def _apply_dsp_decisions(
    *,
    spec: JointOptimizationSpec,
    base_plan: CadCalibrationPlan,
    dsp_decisions: Sequence[JointDecisionValue],
) -> tuple[CadCalibrationChannel, ...]:
    dsp_by_id = {item.variable_id: item for item in spec.dsp_variables}
    channels: dict[str, CadCalibrationChannel] = {
        item.channel_id: item for item in base_plan.channels
    }
    for decision in dsp_decisions:
        variable = dsp_by_id.get(decision.variable_id)
        if variable is None:
            raise ValueError(
                f'unknown DSP decision variable: {decision.variable_id}'
            )
        channel = channels.get(variable.channel_id)
        if channel is None:
            raise ValueError(
                f'DSP decision variable {variable.variable_id} references '
                'a channel missing from the base CalibrationPlan'
            )
        if variable.parameter == 'gain_db':
            channel = channel.model_copy(
                update={'gain_db': float(decision.value)}
            )
        elif variable.parameter == 'delay_s':
            channel = channel.model_copy(
                update={'delay_s': float(decision.value)}
            )
        elif variable.parameter == 'polarity':
            channel = channel.model_copy(
                update={'polarity': str(decision.value)}
            )
        elif variable.parameter.startswith('peq_'):
            field = {
                'peq_frequency_hz': 'frequency_hz',
                'peq_q': 'q',
                'peq_gain_db': 'gain_db',
            }[variable.parameter]
            peq = tuple(
                item.model_copy(
                    update={field: float(decision.value)}
                )
                if item.filter_id == variable.filter_id
                else item
                for item in channel.peq
            )
            if not any(
                item.filter_id == variable.filter_id for item in peq
            ):
                raise ValueError(
                    f'PEQ decision variable {variable.variable_id} '
                    'references a filter missing from the base '
                    'CalibrationPlan'
                )
            channel = channel.model_copy(update={'peq': peq})
        elif variable.parameter.startswith('crossover_'):
            index = variable.crossover_index
            if index is None or index >= len(channel.crossovers):
                raise ValueError(
                    f'crossover decision variable {variable.variable_id} '
                    'references a crossover missing from the base '
                    'CalibrationPlan'
                )
            field = {
                'crossover_frequency_hz': 'frequency_hz',
                'crossover_order': 'filter_order',
            }[variable.parameter]
            crossovers = tuple(
                item.model_copy(
                    update={field: float(decision.value)}
                )
                if position == index
                else item
                for position, item in enumerate(channel.crossovers)
            )
            channel = channel.model_copy(
                update={'crossovers': crossovers}
            )
        else:
            raise ValueError(
                f'unsupported DSP decision parameter: {variable.parameter}'
            )
        channels[variable.channel_id] = channel
    return tuple(
        channels[item.channel_id] for item in base_plan.channels
    )


def materialize_joint_candidate_plan(
    *,
    spec: JointOptimizationSpec,
    base_plan: CadCalibrationPlan,
    physical_variant: SystemVariant,
    quality_report: CadMeasurementQualityReport,
    dsp_decisions: Sequence[JointDecisionValue],
    created_at_utc: str,
) -> CadCalibrationPlan:
    """Derive a candidate CalibrationPlan carrying the DSP decisions.

    The candidate plan reuses the exact measurement/dataset/quality/device
    authority of the base plan (enforced by #174 validation) and binds the
    candidate's physical SystemVariant.
    """

    assert spec.dsp_authority is not None
    if (
        base_plan.plan_id != spec.dsp_authority.base_calibration_plan_id
        or base_plan.plan_semantic_sha256
        != spec.dsp_authority.base_calibration_plan_sha256
    ):
        raise ValueError(
            'candidate plan must derive from the exact base CalibrationPlan'
        )
    channels = _apply_dsp_decisions(
        spec=spec,
        base_plan=base_plan,
        dsp_decisions=dsp_decisions,
    )
    digest = canonical_joint_sha256({
        'spec': spec.spec_id,
        'variant': physical_variant.variant_sha256,
        'decisions': [
            item.model_dump(mode='json') for item in dsp_decisions
        ],
    })
    support_state, unsupported_reasons = evaluate_calibration_support(
        quality_report=quality_report,
        sample_rate_hz=base_plan.sample_rate_hz,
        channels=channels,
        target_curve=base_plan.target_curve,
        max_boost_db=base_plan.max_boost_db,
        max_cut_db=base_plan.max_cut_db,
        device_constraints=base_plan.device_constraints,
    )
    payload: dict[str, Any] = {
        'schema_version': base_plan.schema_version,
        'authority_version': base_plan.authority_version,
        'plan_id': f'{base_plan.plan_id}-joint-{digest[:12]}',
        'plan_version': base_plan.plan_version,
        'created_at_utc': created_at_utc,
        'source_kind': base_plan.source_kind,
        'document_id': base_plan.document_id,
        'scene_revision_id': base_plan.scene_revision_id,
        'scene_content_hash': base_plan.scene_content_hash,
        'system_variant_id': physical_variant.variant_id,
        'system_variant_sha256': physical_variant.variant_sha256,
        'source_measurement_id': base_plan.source_measurement_id,
        'source_measurement_sha256': base_plan.source_measurement_sha256,
        'source_dataset_id': base_plan.source_dataset_id,
        'source_dataset_sha256': base_plan.source_dataset_sha256,
        'measurement_quality_report_id': (
            base_plan.measurement_quality_report_id
        ),
        'measurement_quality_report_sha256': (
            base_plan.measurement_quality_report_sha256
        ),
        'sample_rate_hz': base_plan.sample_rate_hz,
        'channels': channels,
        'target_curve': base_plan.target_curve,
        'max_boost_db': base_plan.max_boost_db,
        'max_cut_db': base_plan.max_cut_db,
        'device_constraints': base_plan.device_constraints,
        'support_state': support_state,
        'unsupported_reasons': unsupported_reasons,
    }
    provisional = CadCalibrationPlan.model_construct(
        **payload,
        plan_semantic_sha256='0' * 64,
    )
    return CadCalibrationPlan(
        **payload,
        plan_semantic_sha256=canonical_joint_sha256(
            provisional.semantic_payload()
        ),
    )


def assess_joint_spec_staleness(
    *,
    spec: JointOptimizationSpec,
    baseline: SceneRevision,
    base_variant: SystemVariant,
    base_plan: CadCalibrationPlan | None,
) -> tuple[str, ...]:
    """Typed staleness: any bound baseline input change marks results stale."""

    reasons: list[str] = []
    if baseline.revision_id != spec.scene_revision_id:
        reasons.append('scene_revision_changed')
    if baseline.content_hash != spec.scene_content_hash:
        reasons.append('scene_content_changed')
    if (
        base_variant.variant_id != spec.base_system_variant_id
        or base_variant.variant_sha256 != spec.base_system_variant_sha256
    ):
        reasons.append('base_system_variant_changed')
    if spec.dsp_authority is not None:
        if base_plan is None:
            reasons.append('base_calibration_plan_missing')
        elif (
            base_plan.plan_id
            != spec.dsp_authority.base_calibration_plan_id
            or base_plan.plan_semantic_sha256
            != spec.dsp_authority.base_calibration_plan_sha256
        ):
            reasons.append('base_calibration_plan_changed')
    return tuple(reasons)


def run_joint_execution(
    *,
    repository: CadJointOptimizationRepository,
    spec: JointOptimizationSpec,
    baseline: SceneRevision,
    base_variant: SystemVariant,
    system_variant_repository: Any,
    calibration_repository: Any = None,
    base_plan: CadCalibrationPlan | None = None,
    quality_report: CadMeasurementQualityReport | None = None,
    evaluator: JointVectorEvaluator | None = None,
    created_at_utc: str,
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[JointExecutionResult], None] | None = None,
) -> JointExecutionResult:
    """Execute one persisted spec end to end, within its immutable budget.

    - Baseline staleness fails closed before any candidate is persisted.
    - Each decision vector materializes an exact SystemVariant and, for DSP
      candidates, a derived CalibrationPlan; both are re-validated by
      ``require_joint_decision_materialization`` before persistence.
    - Eligible candidates are evaluated through the supplied evaluator and
      their bindings persisted; missing evaluator produces the canonical
      unsupported vector.
    - ``is_cancelled`` is honored between candidates; partial progress stays
      persisted.
    """

    stale = assess_joint_spec_staleness(
        spec=spec,
        baseline=baseline,
        base_variant=base_variant,
        base_plan=base_plan,
    )
    if stale:
        raise ValueError(
            'joint spec baseline is stale: ' + ', '.join(stale)
        )
    if spec.dsp_variables and (
        base_plan is None
        or quality_report is None
        or calibration_repository is None
    ):
        raise ValueError(
            'DSP joint execution requires the exact base CalibrationPlan, '
            'MeasurementQualityReport, and calibration repository'
        )

    vectors = enumerate_joint_decision_vectors(spec)
    persisted_spec = repository.get_spec(spec.spec_id)
    if persisted_spec is None:
        repository.save_spec(spec)

    generated = 0
    reused = 0
    blocked = 0
    evaluated = 0
    budget_limited = joint_decision_vector_count(spec) > len(vectors)
    cancelled = False

    def _progress() -> JointExecutionResult:
        return _build_result(
            spec=spec,
            vectors=vectors,
            generated=generated,
            reused=reused,
            blocked=blocked,
            evaluated=evaluated,
            budget_limited=budget_limited,
            cancelled=cancelled,
            pareto_ids=(),
        )

    for vector in vectors:
        if is_cancelled is not None and is_cancelled():
            cancelled = True
            break
        physical_variant = materialize_joint_physical_variant(
            spec=spec,
            baseline=baseline,
            base_variant=base_variant,
            decisions=vector,
            created_at_utc=created_at_utc,
        )
        if physical_variant.variant_id != base_variant.variant_id:
            persisted_variant = next(
                (
                    item
                    for item in system_variant_repository.list_variants(
                        physical_variant.document_id
                    )
                    if item.variant_sha256
                    == physical_variant.variant_sha256
                ),
                None,
            )
            if persisted_variant is None:
                system_variant_repository.save_variant(physical_variant)
            else:
                physical_variant = persisted_variant
        dsp_decisions = tuple(
            item for item in vector if item.domain == 'dsp'
        )
        plan: CadCalibrationPlan | None = None
        if dsp_decisions:
            assert base_plan is not None and quality_report is not None
            plan = materialize_joint_candidate_plan(
                spec=spec,
                base_plan=base_plan,
                physical_variant=physical_variant,
                quality_report=quality_report,
                dsp_decisions=dsp_decisions,
                created_at_utc=created_at_utc,
            )
            if calibration_repository.get_plan(plan.plan_id) is None:
                calibration_repository.save_plan(plan)
        candidate = build_joint_candidate(
            spec=spec,
            physical_system_variant=physical_variant,
            decisions=vector,
            calibration_plan=plan,
            measurement_quality_report=(
                quality_report if dsp_decisions else None
            ),
        )
        require_joint_decision_materialization(
            spec=spec,
            baseline=baseline,
            physical_system_variant=physical_variant,
            calibration_plan=plan,
            decisions=vector,
        )
        prior = repository.get_candidate(candidate.candidate_id)
        if prior is not None:
            reused += 1
        else:
            repository.save_candidate(candidate)
            generated += 1
            if candidate.eligibility_state == 'BLOCKED':
                blocked += 1
            elif evaluator is not None:
                outcome = evaluator(
                    JointExecutionCandidateContext(
                        spec=spec,
                        candidate=candidate,
                        physical_variant=physical_variant,
                        calibration_plan=plan,
                    )
                )
                binding = bind_joint_candidate_evaluation(
                    spec=spec,
                    candidate=candidate,
                    objective_vector=outcome.objective_vector,
                    input_refs=outcome.input_refs,
                    created_at_utc=created_at_utc,
                )
                repository.save_evaluation(binding)
                evaluated += 1
            else:
                binding = bind_joint_candidate_evaluation(
                    spec=spec,
                    candidate=candidate,
                    objective_vector=unsupported_joint_objective_vector(
                        spec=spec, candidate=candidate
                    ),
                    input_refs=(
                        JointEvaluationInputRef(
                            evidence_class='derived',
                            source_kind='joint_execution',
                            source_id=spec.spec_id,
                            source_sha256=spec.semantic_sha256,
                        ),
                    ),
                    created_at_utc=created_at_utc,
                )
                repository.save_evaluation(binding)
                evaluated += 1
        if on_progress is not None:
            on_progress(_progress())

    pareto_ids: tuple[str, ...] = ()
    if not cancelled:
        try:
            front: ParetoResult | None = repository.pareto_front(spec.spec_id)
        except ParetoEmptyError:
            # Every eligible candidate is blocked or unevaluated: report an
            # empty front. Integrity failures (corrupt evidence, authority
            # mismatches) still propagate instead of reading as "no front".
            front = None
        if front is not None:
            pareto_ids = front.non_dominated_candidate_ids
    return _build_result(
        spec=spec,
        vectors=vectors,
        generated=generated,
        reused=reused,
        blocked=blocked,
        evaluated=evaluated,
        budget_limited=budget_limited,
        cancelled=cancelled,
        pareto_ids=pareto_ids,
    )


def _build_result(
    *,
    spec: JointOptimizationSpec,
    vectors: Sequence[Sequence[JointDecisionValue]],
    generated: int,
    reused: int,
    blocked: int,
    evaluated: int,
    budget_limited: bool,
    cancelled: bool,
    pareto_ids: Sequence[str],
) -> JointExecutionResult:
    payload = {
        'authority_version': JOINT_EXECUTION_AUTHORITY_VERSION,
        'parent_spec_id': spec.spec_id,
        'parent_spec_sha256': spec.semantic_sha256,
        'decision_vectors_total': len(vectors),
        'candidates_generated': generated,
        'candidates_reused': reused,
        'candidates_blocked': blocked,
        'evaluations_recorded': evaluated,
        'budget_limited': budget_limited,
        'cancelled': cancelled,
        'pareto_candidate_ids': list(pareto_ids),
    }
    digest = canonical_joint_sha256(payload)
    return JointExecutionResult(
        result_id=f'joint-execution-{digest[:24]}',
        parent_spec_id=spec.spec_id,
        parent_spec_sha256=spec.semantic_sha256,
        decision_vectors_total=len(vectors),
        candidates_generated=generated,
        candidates_reused=reused,
        candidates_blocked=blocked,
        evaluations_recorded=evaluated,
        budget_limited=budget_limited,
        cancelled=cancelled,
        pareto_candidate_ids=tuple(pareto_ids),
        execution_sha256=digest,
    )
