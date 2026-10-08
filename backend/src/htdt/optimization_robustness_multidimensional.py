from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_constraint_models import CadConstraintSet
from .cad_constraints import evaluate_cad_constraints
from .cad_objective_models import CadObjectiveEvaluation
from .cad_orientation_constraints import orientation_constraint_rejections
from .cad_repository import SceneRevision
from .cad_scene import SceneDocument, scene_content_hash
from .cad_search_models import CadSearchSpec
from .optimization_objectives import ObjectiveMetric, ObjectiveVector
from .optimization_robustness import (
    ROBUSTNESS_MULTIDIMENSIONAL_ALGORITHM_VERSION,
    ROBUSTNESS_SCHEMA_VERSION,
    LinkedPerturbationGroup,
    LocalPerturbation,
    PerturbationObjectiveResult,
    PerturbationSample,
    RobustnessEvaluation,
    RobustnessExecutionResult,
    RobustnessSampleCache,
    RobustnessSpec,
    SampledObjectiveEnvelope,
    UncertaintyAxis,
    _candidate_document,
    _candidate_from_payload,
    _copy_nominal_vector,
    _domain_rejections,
    _metric_schema,
    _semantic_id,
    _validate_constraint_workspace_authority,
    apply_local_perturbation,
    canonical_robustness_sha256,
    robustness_timestamp_utc,
)
from .pareto import ParetoResult, pareto_front


MULTIDIMENSIONAL_SAMPLING_STRATEGY = 'deterministic_multidimensional_bounded'


class RobustParetoSelection(BaseModel):
    """Explicit nominal/robust objective selection for O40 Pareto reuse."""

    model_config = ConfigDict(frozen=True)

    nominal_objective_ids: tuple[str, ...] = Field(min_length=1)
    robustness_objective_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_objectives(self) -> 'RobustParetoSelection':
        if len(self.nominal_objective_ids) != len(set(self.nominal_objective_ids)):
            raise ValueError('nominal Pareto objective IDs must be unique')
        if len(self.robustness_objective_ids) != len(
            set(self.robustness_objective_ids)
        ):
            raise ValueError('robust Pareto objective IDs must be unique')
        return self


def derive_multidimensional_robustness_spec(
    base_spec: RobustnessSpec,
    *,
    sample_count: int,
    seed: int,
    linked_groups: Sequence[LinkedPerturbationGroup] = (),
    created_at_utc: str | None = None,
) -> RobustnessSpec:
    """Derive O90B sampling authority from an exact immutable O90A spec."""

    if base_spec.sampling_strategy != 'deterministic_local_stencil':
        raise ValueError('O90B derivation requires an O90A local robustness spec')
    if sample_count < 3:
        raise ValueError(
            'multidimensional robustness requires at least three samples '
            '(nominal, negative corner, positive corner)'
        )

    ordered_groups = tuple(sorted(linked_groups, key=lambda item: item.group_id))
    identity = base_spec.identity_payload()
    identity.update(
        {
            'sampling_strategy': MULTIDIMENSIONAL_SAMPLING_STRATEGY,
            'algorithm_version': ROBUSTNESS_MULTIDIMENSIONAL_ALGORITHM_VERSION,
            'sampling_seed': int(seed),
            'sample_count': int(sample_count),
            'linked_groups': [
                group.model_dump(mode='json') for group in ordered_groups
            ],
            'parent_robustness_spec_id': base_spec.robustness_spec_id,
            'parent_robustness_spec_sha256': base_spec.robustness_spec_sha256,
        }
    )
    digest = canonical_robustness_sha256(identity)

    payload = base_spec.model_dump(mode='json')
    payload.update(
        {
            'robustness_spec_id': _semantic_id('rob', digest),
            'sampling_strategy': MULTIDIMENSIONAL_SAMPLING_STRATEGY,
            'algorithm_version': ROBUSTNESS_MULTIDIMENSIONAL_ALGORITHM_VERSION,
            'sampling_seed': int(seed),
            'sample_count': int(sample_count),
            'linked_groups': [
                group.model_dump(mode='json') for group in ordered_groups
            ],
            'parent_robustness_spec_id': base_spec.robustness_spec_id,
            'parent_robustness_spec_sha256': base_spec.robustness_spec_sha256,
            'robustness_spec_sha256': digest,
            'created_at_utc': created_at_utc or robustness_timestamp_utc(),
        }
    )
    return RobustnessSpec.model_validate(payload)


def _stable_normalized_coordinate(
    spec: RobustnessSpec,
    *,
    sample_index: int,
    key: str,
) -> float:
    """Return deterministic [-1, 1] design coordinate without probability meaning."""

    digest = canonical_robustness_sha256(
        {
            'robustness_spec_sha256': spec.robustness_spec_sha256,
            'sampling_seed': spec.sampling_seed,
            'sample_index': sample_index,
            'coordinate_key': key,
            'algorithm_version': spec.algorithm_version,
        }
    )
    integer = int(digest[:16], 16)
    unit = integer / float((1 << 64) - 1)
    return 2.0 * unit - 1.0


def _axis_delta(axis: UncertaintyAxis, normalized: float) -> float:
    bounded = max(-1.0, min(1.0, float(normalized)))
    if bounded < 0.0:
        return bounded * float(axis.minus_delta)
    return bounded * float(axis.plus_delta)


def build_multidimensional_sampling_plan(
    spec: RobustnessSpec,
) -> tuple[LocalPerturbation, ...]:
    """Build reproducible finite O90B design samples over declared bounded axes."""

    if spec.sampling_strategy != MULTIDIMENSIONAL_SAMPLING_STRATEGY:
        raise ValueError('multidimensional plan requires an O90B robustness spec')
    assert spec.sample_count is not None
    assert spec.sampling_seed is not None

    axes = tuple(sorted(spec.axes, key=lambda item: item.axis_id))
    axis_by_id = {axis.axis_id: axis for axis in axes}
    linked_by_axis: dict[str, tuple[LinkedPerturbationGroup, float]] = {}
    for group in spec.linked_groups:
        for axis_id, multiplier in group.axis_multipliers.items():
            linked_by_axis[axis_id] = (group, float(multiplier))

    plans: list[LocalPerturbation] = []

    def add(sample_index: int, deltas: dict[str, float], *, nominal: bool) -> None:
        step = 'nominal' if nominal else 'multidimensional'
        identity = {
            'robustness_spec_sha256': spec.robustness_spec_sha256,
            'candidate_id': spec.candidate_id,
            'sample_index': sample_index,
            'step': step,
            'parameter_deltas': deltas,
            'sampling_seed': spec.sampling_seed,
            'algorithm_version': spec.algorithm_version,
        }
        plans.append(
            LocalPerturbation(
                sample_id=_semantic_id(
                    'rp',
                    canonical_robustness_sha256(identity),
                ),
                sample_index=sample_index,
                axis_id=None,
                step=step,
                parameter_deltas=deltas,
            )
        )

    add(0, {}, nominal=True)
    for sample_index in range(1, spec.sample_count):
        group_coordinate: dict[str, float] = {}
        deltas: dict[str, float] = {}
        for axis in axes:
            linked = linked_by_axis.get(axis.axis_id)
            if sample_index == 1:
                normalized = -1.0
            elif sample_index == 2:
                normalized = 1.0
            elif linked is None:
                normalized = _stable_normalized_coordinate(
                    spec,
                    sample_index=sample_index,
                    key=f'axis:{axis.axis_id}',
                )
            else:
                group, _multiplier = linked
                normalized = group_coordinate.setdefault(
                    group.group_id,
                    _stable_normalized_coordinate(
                        spec,
                        sample_index=sample_index,
                        key=f'linked-group:{group.group_id}',
                    ),
                )

            if linked is not None:
                group, multiplier = linked
                if sample_index <= 2:
                    group_coordinate.setdefault(group.group_id, normalized)
                normalized = group_coordinate[group.group_id] * multiplier
            deltas[axis.axis_id] = _axis_delta(axis, normalized)

        add(
            sample_index,
            {axis_id: deltas[axis_id] for axis_id in sorted(deltas)},
            nominal=False,
        )

    return tuple(plans)


def _validate_evaluation_authority(
    *,
    source_revision: SceneRevision,
    search_spec: CadSearchSpec,
    spec: RobustnessSpec,
    constraint_set: CadConstraintSet,
    nominal_objective: CadObjectiveEvaluation,
) -> None:
    if (
        source_revision.revision_id != spec.scene_revision_id
        or source_revision.content_hash != spec.scene_content_hash
        or source_revision.document_id != spec.document_id
    ):
        raise ValueError('robustness source SceneRevision authority mismatch')
    if (
        search_spec.search_spec_id != spec.search_spec_id
        or search_spec.search_spec_sha256 != spec.search_spec_sha256
    ):
        raise ValueError('robustness SearchSpec authority mismatch')
    if (
        nominal_objective.evaluation_id != spec.nominal_objective_evaluation_id
        or nominal_objective.evaluation_sha256
        != spec.nominal_objective_evaluation_sha256
        or nominal_objective.evaluation_spec_sha256
        != spec.objective_evaluation_spec_sha256
        or nominal_objective.candidate_id != spec.candidate_id
    ):
        raise ValueError('robustness O30 objective authority mismatch')
    _validate_constraint_workspace_authority(search_spec, constraint_set)


def _multidimensional_sample(
    *,
    spec: RobustnessSpec,
    plan: LocalPerturbation,
    nominal_document: SceneDocument,
    constraint_set: CadConstraintSet,
    nominal_objective: CadObjectiveEvaluation,
    evaluator: Callable[[SceneDocument, str], PerturbationObjectiveResult],
    objective_schema: tuple[tuple[str, str, str], ...],
    created_at_utc: str,
) -> PerturbationSample:
    axis_by_id = {axis.axis_id: axis for axis in spec.axes}
    document = nominal_document
    domain_rejections: list[str] = []
    changed_entity_ids: set[str] = set()
    failure_reason: str | None = None

    for axis_id in sorted(plan.parameter_deltas):
        axis = axis_by_id[axis_id]
        delta = float(plan.parameter_deltas[axis_id])
        domain_rejections.extend(_domain_rejections(axis, delta))
        changed_entity_ids.add(axis.entity_id)
        try:
            document = apply_local_perturbation(document, axis, delta)
        except Exception as exc:  # error-boundary: per-sample evaluation — any failure type is sealed as failure_reason on this sample's record, never silently passed or allowed to abort the batch
            domain_rejections.append(f'__perturbation_unsupported__:{axis.axis_id}')
            failure_reason = f'perturbation_failed:{exc}'

    if plan.step == 'nominal':
        changed_entity_ids.update(axis.entity_id for axis in spec.axes)

    g10 = evaluate_cad_constraints(document, constraint_set)
    o80 = orientation_constraint_rejections(
        document,
        constraint_set,
        changed_entity_ids=tuple(sorted(changed_entity_ids)),
    )
    feasible = (
        g10.constraints_satisfied
        and not o80
        and not domain_rejections
    )

    result: PerturbationObjectiveResult | None = None
    if feasible:
        if plan.step == 'nominal':
            result = PerturbationObjectiveResult(
                prediction_result_ref=spec.nominal_prediction_result_ref,
                objective_vector=_copy_nominal_vector(
                    nominal_objective,
                    plan.sample_id,
                ),
            )
        else:
            try:
                result = evaluator(document, plan.sample_id)
                if _metric_schema(result.objective_vector) != objective_schema:
                    raise ValueError(
                        'perturbed objective schema does not match nominal O30 vector'
                    )
                if result.objective_vector.candidate_id != plan.sample_id:
                    raise ValueError(
                        'perturbed O30 vector candidate_id must equal sample_id'
                    )
            except Exception as exc:  # error-boundary: per-sample objective evaluation — any failure type is sealed as failure_reason on this sample's record, never silently passed or allowed to abort the batch
                result = None
                failure_reason = f'objective_evaluation_failed:{exc}'
    elif failure_reason is None:
        failure_reason = 'hard_constraint_violation'

    payload = {
        'schema_version': ROBUSTNESS_SCHEMA_VERSION,
        'sample_id': plan.sample_id,
        'robustness_spec_id': spec.robustness_spec_id,
        'robustness_spec_sha256': spec.robustness_spec_sha256,
        'candidate_id': spec.candidate_id,
        'sample_index': plan.sample_index,
        'axis_id': None,
        'step': plan.step,
        'parameter_deltas': plan.parameter_deltas,
        'perturbed_scene_content_hash': scene_content_hash(document),
        'feasible': feasible,
        'g10_results': [item.model_dump(mode='json') for item in g10.results],
        'o80_rejection_ids': list(o80),
        'domain_rejection_ids': sorted(set(domain_rejections)),
        'model_id': spec.model_id,
        'model_version': spec.model_version,
        'prediction_provider_id': spec.prediction_provider_id,
        'fidelity': spec.fidelity,
        'objective_evaluation_spec_sha256': (
            spec.objective_evaluation_spec_sha256
        ),
        'prediction_result_ref': (
            None if result is None else result.prediction_result_ref
        ),
        'objective_vector': (
            None
            if result is None
            else result.objective_vector.identity_payload()
        ),
        'failure_reason': failure_reason,
    }
    if plan.uncertainty_model_sha256 is not None:
        payload.update(
            {
                'uncertainty_model_sha256': plan.uncertainty_model_sha256,
                'uncertainty_item_id': plan.uncertainty_item_id,
                'probability_weight': plan.probability_weight,
            }
        )
    return PerturbationSample(
        **payload,
        sample_sha256=canonical_robustness_sha256(payload),
        created_at_utc=created_at_utc,
    )


def _validate_bounded_reusable_sample(
    spec: RobustnessSpec,
    plan: LocalPerturbation,
    sample: PerturbationSample,
) -> None:
    expected = (
        sample.sample_id == plan.sample_id
        and sample.sample_index == plan.sample_index
        and sample.step == plan.step
        and sample.parameter_deltas == plan.parameter_deltas
        and sample.robustness_spec_id == spec.robustness_spec_id
        and sample.robustness_spec_sha256 == spec.robustness_spec_sha256
        and sample.candidate_id == spec.candidate_id
        and sample.model_id == spec.model_id
        and sample.model_version == spec.model_version
        and sample.prediction_provider_id == spec.prediction_provider_id
        and sample.fidelity == spec.fidelity
        and sample.objective_evaluation_spec_sha256
        == spec.objective_evaluation_spec_sha256
        and sample.uncertainty_model_sha256 is None
        and sample.uncertainty_item_id is None
        and sample.probability_weight is None
    )
    if not expected:
        raise ValueError(
            'stale or incompatible completed robustness sample cannot be reused'
        )


def _merge_bounded_reusable_samples(
    spec: RobustnessSpec,
    plans: Sequence[LocalPerturbation],
    sources: Sequence[PerturbationSample],
) -> dict[str, PerturbationSample]:
    plan_by_id = {plan.sample_id: plan for plan in plans}
    reusable: dict[str, PerturbationSample] = {}
    for sample in sources:
        plan = plan_by_id.get(sample.sample_id)
        if plan is None:
            raise ValueError(
                'stale or incompatible completed robustness sample cannot be reused'
            )
        _validate_bounded_reusable_sample(spec, plan, sample)
        existing = reusable.get(sample.sample_id)
        if existing is not None and existing.sample_sha256 != sample.sample_sha256:
            raise ValueError('conflicting completed robustness sample evidence')
        reusable[sample.sample_id] = sample
    return reusable


def execute_multidimensional_robustness(
    *,
    source_revision: SceneRevision,
    search_spec: CadSearchSpec,
    spec: RobustnessSpec,
    constraint_set: CadConstraintSet,
    nominal_objective: CadObjectiveEvaluation,
    evaluator: Callable[[SceneDocument, str], PerturbationObjectiveResult],
    cache: RobustnessSampleCache | None = None,
    completed_samples: Sequence[PerturbationSample] = (),
    cancel_requested: Callable[[], bool] | None = None,
    created_at_utc: str | None = None,
) -> RobustnessExecutionResult:
    """Evaluate/resume the PR #149 bounded design without changing its semantics."""

    if spec.sampling_strategy != MULTIDIMENSIONAL_SAMPLING_STRATEGY:
        raise ValueError('O90B evaluation requires multidimensional sampling spec')
    _validate_evaluation_authority(
        source_revision=source_revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraint_set,
        nominal_objective=nominal_objective,
    )

    candidate = _candidate_from_payload(
        spec.candidate_kind,
        json.loads(spec.candidate_payload_json),
    )
    nominal_document = _candidate_document(source_revision, candidate)
    plans = build_multidimensional_sampling_plan(spec)
    timestamp = created_at_utc or robustness_timestamp_utc()
    objective_schema = _metric_schema(nominal_objective.vector)

    cache_samples: tuple[PerturbationSample, ...] = ()
    if cache is not None:
        cache.save_spec(spec)
        cache_samples = cache.list_reusable_samples(spec)
    reusable = _merge_bounded_reusable_samples(
        spec,
        plans,
        tuple(completed_samples) + tuple(cache_samples),
    )
    finished = dict(reusable)
    reused_ids: list[str] = []
    computed_ids: list[str] = []

    for plan in plans:
        cached = reusable.get(plan.sample_id)
        if cached is not None:
            reused_ids.append(plan.sample_id)
            continue
        if cancel_requested is not None and cancel_requested():
            samples = tuple(
                finished[plan_item.sample_id]
                for plan_item in plans
                if plan_item.sample_id in finished
            )
            return RobustnessExecutionResult(
                status='cancelled',
                robustness_spec_id=spec.robustness_spec_id,
                robustness_spec_sha256=spec.robustness_spec_sha256,
                samples=samples,
                reused_sample_ids=tuple(reused_ids),
                computed_sample_ids=tuple(computed_ids),
            )

        sample = _multidimensional_sample(
            spec=spec,
            plan=plan,
            nominal_document=nominal_document,
            constraint_set=constraint_set,
            nominal_objective=nominal_objective,
            evaluator=evaluator,
            objective_schema=objective_schema,
            created_at_utc=timestamp,
        )
        finished[plan.sample_id] = sample
        computed_ids.append(plan.sample_id)
        if cache is not None:
            cache.save_sample(sample)

    samples = tuple(finished[plan.sample_id] for plan in plans)
    evaluations = build_multidimensional_robustness_evaluations(
        spec,
        samples,
        created_at_utc=timestamp,
    )
    if cache is not None:
        cache.save_evaluations(evaluations)
    return RobustnessExecutionResult(
        status='completed',
        robustness_spec_id=spec.robustness_spec_id,
        robustness_spec_sha256=spec.robustness_spec_sha256,
        samples=samples,
        evaluations=evaluations,
        reused_sample_ids=tuple(reused_ids),
        computed_sample_ids=tuple(computed_ids),
    )


def evaluate_multidimensional_robustness(
    *,
    source_revision: SceneRevision,
    search_spec: CadSearchSpec,
    spec: RobustnessSpec,
    constraint_set: CadConstraintSet,
    nominal_objective: CadObjectiveEvaluation,
    evaluator: Callable[[SceneDocument, str], PerturbationObjectiveResult],
    created_at_utc: str | None = None,
) -> tuple[tuple[PerturbationSample, ...], tuple[RobustnessEvaluation, ...]]:
    """Preserve the original all-at-once PR #149 evaluation API."""

    result = execute_multidimensional_robustness(
        source_revision=source_revision,
        search_spec=search_spec,
        spec=spec,
        constraint_set=constraint_set,
        nominal_objective=nominal_objective,
        evaluator=evaluator,
        created_at_utc=created_at_utc,
    )
    if result.status != 'completed':
        raise RuntimeError('non-cancellable O90B evaluation was unexpectedly cancelled')
    return result.samples, result.evaluations


def build_multidimensional_evaluations_from_provenance(
    *,
    robustness_spec_id: str,
    robustness_spec_sha256: str,
    candidate_id: str,
    expected_sample_ids: Sequence[str],
    samples: Sequence[Any],
    sampling_provenance: Mapping[str, Any],
    created_at_utc: str | None = None,
) -> tuple[RobustnessEvaluation, ...]:
    """Summarize a finite bounded design without redefining O90B semantics.

    The caller owns the exact sampling-plan and provenance authority. This
    helper owns only the existing sampled envelope, feasible fraction and
    direction-aware sampled-worst calculation.
    """

    expected_ids = tuple(expected_sample_ids)
    ordered = tuple(sorted(samples, key=lambda item: item.sample_index))
    if tuple(item.sample_id for item in ordered) != expected_ids:
        raise ValueError(
            'robustness samples do not match deterministic multidimensional design'
        )
    if any(
        item.robustness_spec_id != robustness_spec_id
        or item.robustness_spec_sha256 != robustness_spec_sha256
        or item.candidate_id != candidate_id
        for item in ordered
    ):
        raise ValueError('robustness samples belong to another authority')

    nominal = ordered[0]
    if nominal.step != 'nominal' or nominal.objective_vector is None:
        raise ValueError('multidimensional robustness requires scored nominal sample')

    timestamp = created_at_utc or robustness_timestamp_utc()
    feasible_fraction = sum(1 for item in ordered if item.feasible) / len(ordered)
    sampling_provenance_sha256 = canonical_robustness_sha256(
        dict(sampling_provenance)
    )

    evaluations: list[RobustnessEvaluation] = []
    for nominal_metric in nominal.objective_vector.metrics:
        scored: list[tuple[Any, ObjectiveMetric]] = []
        for sample in ordered:
            if sample.objective_vector is None:
                continue
            try:
                metric = sample.objective_vector.metric(nominal_metric.objective_id)
            except KeyError:
                continue
            scored.append((sample, metric))
        if not scored:
            raise ValueError(
                f'objective {nominal_metric.objective_id} has no scored samples'
            )

        sampled_min_sample, sampled_min_metric = min(
            scored,
            key=lambda pair: float(pair[1].value),
        )
        sampled_max_sample, sampled_max_metric = max(
            scored,
            key=lambda pair: float(pair[1].value),
        )
        if nominal_metric.direction == 'minimize':
            worst_sample, worst_metric = sampled_max_sample, sampled_max_metric
        else:
            worst_sample, worst_metric = sampled_min_sample, sampled_min_metric

        envelope = SampledObjectiveEnvelope(
            sampled_min_sample_id=sampled_min_sample.sample_id,
            sampled_min_value=float(sampled_min_metric.value),
            sampled_max_sample_id=sampled_max_sample.sample_id,
            sampled_max_value=float(sampled_max_metric.value),
            percentile_values=None,
        )
        identity = {
            'schema_version': ROBUSTNESS_SCHEMA_VERSION,
            'robustness_spec_id': robustness_spec_id,
            'robustness_spec_sha256': robustness_spec_sha256,
            'candidate_id': candidate_id,
            'objective_id': nominal_metric.objective_id,
            'objective_unit': nominal_metric.unit,
            'direction': nominal_metric.direction,
            'nominal_sample_id': nominal.sample_id,
            'nominal_value': float(nominal_metric.value),
            'local_sensitivities': [],
            'sampled_worst_semantics': 'sampled_worst',
            'sampled_worst_sample_id': worst_sample.sample_id,
            'sampled_worst_value': float(worst_metric.value),
            'sample_ids': [item.sample_id for item in ordered],
            'infeasible_sample_ids': [
                item.sample_id for item in ordered if not item.feasible
            ],
            'failed_sample_ids': [
                item.sample_id
                for item in ordered
                if item.feasible and item.objective_vector is None
            ],
            'sampled_envelope': envelope.model_dump(mode='json'),
            'feasible_fraction': feasible_fraction,
            'sampling_provenance_sha256': sampling_provenance_sha256,
            'percentile_semantics': 'not_available_bounded_interval',
        }
        if nominal_metric.definition is not None:
            identity['objective_definition'] = nominal_metric.definition.model_dump(
                mode='json'
            )
        digest = canonical_robustness_sha256(identity)
        evaluations.append(
            RobustnessEvaluation(
                **identity,
                evaluation_id=_semantic_id('re', digest),
                evaluation_sha256=digest,
                created_at_utc=timestamp,
            )
        )
    return tuple(evaluations)


def build_multidimensional_robustness_evaluations(
    spec: RobustnessSpec,
    samples: Sequence[PerturbationSample],
    *,
    created_at_utc: str | None = None,
) -> tuple[RobustnessEvaluation, ...]:
    """Summarize finite sampled envelopes without fabricating probabilities."""

    expected_ids = tuple(
        item.sample_id for item in build_multidimensional_sampling_plan(spec)
    )
    ordered = tuple(sorted(samples, key=lambda item: item.sample_index))
    provenance = {
        'robustness_spec_id': spec.robustness_spec_id,
        'robustness_spec_sha256': spec.robustness_spec_sha256,
        'parent_robustness_spec_id': spec.parent_robustness_spec_id,
        'parent_robustness_spec_sha256': spec.parent_robustness_spec_sha256,
        'sampling_strategy': spec.sampling_strategy,
        'algorithm_version': spec.algorithm_version,
        'sampling_seed': spec.sampling_seed,
        'sample_count': spec.sample_count,
        'linked_groups': [
            item.model_dump(mode='json') for item in spec.linked_groups
        ],
        'sample_ids': [item.sample_id for item in ordered],
        'model_id': spec.model_id,
        'model_version': spec.model_version,
        'prediction_provider_id': spec.prediction_provider_id,
        'fidelity': spec.fidelity,
        'objective_evaluation_spec_sha256': spec.objective_evaluation_spec_sha256,
    }
    return build_multidimensional_evaluations_from_provenance(
        robustness_spec_id=spec.robustness_spec_id,
        robustness_spec_sha256=spec.robustness_spec_sha256,
        candidate_id=spec.candidate_id,
        expected_sample_ids=expected_ids,
        samples=ordered,
        sampling_provenance=provenance,
        created_at_utc=created_at_utc,
    )


def build_nominal_robust_pareto_vector(
    nominal_vector: ObjectiveVector,
    robustness_evaluations: Sequence[RobustnessEvaluation],
    selection: RobustParetoSelection,
) -> ObjectiveVector:
    """Create an O40-compatible vector with visibly separate nominal/robust axes."""

    by_objective = {
        evaluation.objective_id: evaluation
        for evaluation in robustness_evaluations
    }
    metrics: list[ObjectiveMetric] = []

    for objective_id in selection.nominal_objective_ids:
        nominal = nominal_vector.metric(objective_id)
        derived_id = f'nominal::{objective_id}'
        metrics.append(
            ObjectiveMetric(
                objective_id=derived_id,
                value=nominal.comparison_value(),
                unit=nominal.unit,
                direction=nominal.direction,
                definition=(
                    None
                    if nominal.definition is None
                    else nominal.definition.derived(derived_id)
                ),
            )
        )

    for objective_id in selection.robustness_objective_ids:
        try:
            robust = by_objective[objective_id]
        except KeyError as exc:
            raise ValueError(
                f'missing robustness evaluation for objective {objective_id}'
            ) from exc
        if robust.candidate_id != nominal_vector.candidate_id:
            raise ValueError('nominal and robustness candidate IDs must match')
        nominal = nominal_vector.metric(objective_id)
        if robust.objective_definition is None:
            if (
                nominal.definition is not None
                or robust.objective_unit != nominal.unit
                or robust.direction != nominal.direction
            ):
                raise ValueError(
                    f'robust objective authority mismatch for {objective_id}'
                )
        elif robust.objective_definition.definition_id != nominal.definition_id:
            raise ValueError(
                f'robust objective definition mismatch for {objective_id}'
            )
        derived_id = f'robust.sampled_worst::{objective_id}'
        metrics.append(
            ObjectiveMetric(
                objective_id=derived_id,
                value=float(robust.sampled_worst_value),
                unit=robust.objective_unit,
                direction=robust.direction,
                definition=(
                    None
                    if robust.objective_definition is None
                    else robust.objective_definition.derived(derived_id)
                ),
            )
        )

    return ObjectiveVector(
        candidate_id=nominal_vector.candidate_id,
        metrics=tuple(metrics),
    )


def robust_pareto_front(
    candidates: Sequence[
        tuple[ObjectiveVector, Sequence[RobustnessEvaluation]]
    ],
    selection: RobustParetoSelection,
) -> ParetoResult:
    """Delegate nominal/robust trade-off dominance to the existing O40 authority."""

    vectors = tuple(
        build_nominal_robust_pareto_vector(
            nominal_vector,
            robustness_evaluations,
            selection,
        )
        for nominal_vector, robustness_evaluations in candidates
    )
    return pareto_front(vectors)
