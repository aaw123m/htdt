from __future__ import annotations

import json
from math import isclose
from statistics import NormalDist
from typing import Callable, Sequence

from .cad_constraint_models import CadConstraintSet
from .cad_objective_models import CadObjectiveEvaluation
from .cad_repository import SceneRevision
from .cad_scene import SceneDocument
from .cad_search_models import CadSearchSpec
from .optimization_objectives import ObjectiveMetric
from .optimization_robustness import (
    ROBUSTNESS_SCHEMA_VERSION,
    ROBUSTNESS_UNCERTAINTY_ALGORITHM_VERSION,
    DiscreteUncertaintyModel,
    DistributionAxisUncertainty,
    DistributionUncertaintyModel,
    EmpiricalUncertaintyModel,
    ExplicitInputUncertaintyModel,
    ExplicitPerturbationState,
    LocalPerturbation,
    PerturbationObjectiveResult,
    PerturbationSample,
    RobustnessEvaluation,
    RobustnessExecutionResult,
    RobustnessSampleCache,
    RobustnessSpec,
    SampledObjectiveEnvelope,
    _candidate_document,
    _candidate_from_payload,
    _metric_schema,
    _semantic_id,
    canonical_robustness_sha256,
    robustness_timestamp_utc,
)
from .optimization_robustness_multidimensional import (
    _multidimensional_sample,
    _validate_evaluation_authority,
)


UNCERTAINTY_SAMPLING_STRATEGY = 'deterministic_multidimensional_uncertainty'


def _canonical_uncertainty_model(
    model: ExplicitInputUncertaintyModel,
) -> ExplicitInputUncertaintyModel:
    if isinstance(model, DistributionUncertaintyModel):
        return DistributionUncertaintyModel(
            model_id=model.model_id,
            dependence=model.dependence,
            axes=tuple(sorted(model.axes, key=lambda item: item.axis_id)),
        )
    if isinstance(model, EmpiricalUncertaintyModel):
        return EmpiricalUncertaintyModel(
            model_id=model.model_id,
            samples=tuple(sorted(model.samples, key=lambda item: item.state_id)),
        )
    return DiscreteUncertaintyModel(
        model_id=model.model_id,
        states=tuple(sorted(model.states, key=lambda item: item.state_id)),
    )


def _explicit_states(
    model: ExplicitInputUncertaintyModel,
) -> tuple[ExplicitPerturbationState, ...]:
    if isinstance(model, EmpiricalUncertaintyModel):
        return model.samples
    if isinstance(model, DiscreteUncertaintyModel):
        return model.states
    return ()


def _has_explicit_probability(model: ExplicitInputUncertaintyModel) -> bool:
    if isinstance(model, DistributionUncertaintyModel):
        return True
    states = _explicit_states(model)
    return bool(states) and all(item.probability_weight is not None for item in states)


def _probability_semantics(
    model: ExplicitInputUncertaintyModel,
) -> str | None:
    if isinstance(model, DistributionUncertaintyModel):
        return 'explicit_distribution'
    if not _has_explicit_probability(model):
        return None
    if isinstance(model, EmpiricalUncertaintyModel):
        return 'explicit_empirical_weights'
    return 'explicit_discrete_weights'


def derive_uncertainty_robustness_spec(
    base_spec: RobustnessSpec,
    *,
    uncertainty_model: ExplicitInputUncertaintyModel,
    sample_count: int | None = None,
    seed: int | None = None,
    created_at_utc: str | None = None,
) -> RobustnessSpec:
    """Derive canonical O90B distribution/empirical/discrete authority."""

    if base_spec.sampling_strategy != 'deterministic_local_stencil':
        raise ValueError('explicit O90B uncertainty derivation requires an O90A spec')

    model = _canonical_uncertainty_model(uncertainty_model)
    if isinstance(model, DistributionUncertaintyModel):
        if seed is None:
            raise ValueError('distribution uncertainty requires an explicit seed')
        if sample_count is None or sample_count < 3:
            raise ValueError(
                'distribution uncertainty requires nominal plus at least two samples'
            )
        resolved_sample_count = int(sample_count)
        resolved_seed: int | None = int(seed)
    else:
        if seed is not None:
            raise ValueError('empirical/discrete enumeration does not use a seed')
        expected = 1 + len(_explicit_states(model))
        if sample_count is not None and int(sample_count) != expected:
            raise ValueError(
                'empirical/discrete sample_count is fixed by supplied states'
            )
        resolved_sample_count = expected
        resolved_seed = None

    identity = base_spec.identity_payload()
    identity.update(
        {
            'sampling_strategy': UNCERTAINTY_SAMPLING_STRATEGY,
            'algorithm_version': ROBUSTNESS_UNCERTAINTY_ALGORITHM_VERSION,
            'sampling_seed': resolved_seed,
            'sample_count': resolved_sample_count,
            'input_uncertainty_model': model.model_dump(mode='json'),
            'parent_robustness_spec_id': base_spec.robustness_spec_id,
            'parent_robustness_spec_sha256': base_spec.robustness_spec_sha256,
        }
    )
    digest = canonical_robustness_sha256(identity)
    payload = base_spec.model_dump(mode='json')
    payload.update(
        {
            'robustness_spec_id': _semantic_id('rob', digest),
            'sampling_strategy': UNCERTAINTY_SAMPLING_STRATEGY,
            'algorithm_version': ROBUSTNESS_UNCERTAINTY_ALGORITHM_VERSION,
            'sampling_seed': resolved_seed,
            'sample_count': resolved_sample_count,
            'linked_groups': [],
            'input_uncertainty_model': model.model_dump(mode='json'),
            'parent_robustness_spec_id': base_spec.robustness_spec_id,
            'parent_robustness_spec_sha256': base_spec.robustness_spec_sha256,
            'robustness_spec_sha256': digest,
            'created_at_utc': created_at_utc or robustness_timestamp_utc(),
        }
    )
    return RobustnessSpec.model_validate(payload)


def _model_hash(spec: RobustnessSpec) -> str:
    if spec.input_uncertainty_model is None:
        raise ValueError('explicit uncertainty spec has no uncertainty model')
    return canonical_robustness_sha256(
        spec.input_uncertainty_model.model_dump(mode='json')
    )


def _distribution_permutation(
    spec: RobustnessSpec,
    *,
    axis_id: str,
    count: int,
) -> tuple[int, ...]:
    ranked = []
    for rank in range(count):
        key = canonical_robustness_sha256(
            {
                'robustness_spec_sha256': spec.robustness_spec_sha256,
                'sampling_seed': spec.sampling_seed,
                'axis_id': axis_id,
                'rank': rank,
                'algorithm_version': spec.algorithm_version,
            }
        )
        ranked.append((key, rank))
    return tuple(rank for _key, rank in sorted(ranked))


def _distribution_delta(
    axis: DistributionAxisUncertainty,
    unit_coordinate: float,
) -> float:
    if axis.distribution == 'uniform':
        assert axis.min_delta is not None
        assert axis.max_delta is not None
        return float(axis.min_delta) + unit_coordinate * (
            float(axis.max_delta) - float(axis.min_delta)
        )

    assert axis.stddev is not None
    normal = NormalDist(mu=float(axis.mean_delta), sigma=float(axis.stddev))
    if axis.min_delta is None:
        return float(normal.inv_cdf(unit_coordinate))

    assert axis.max_delta is not None
    lower_cdf = normal.cdf(float(axis.min_delta))
    upper_cdf = normal.cdf(float(axis.max_delta))
    mapped = lower_cdf + unit_coordinate * (upper_cdf - lower_cdf)
    return float(normal.inv_cdf(mapped))


def _plan(
    spec: RobustnessSpec,
    *,
    sample_index: int,
    item_id: str,
    deltas: dict[str, float],
    probability_weight: float | None,
    nominal: bool,
) -> LocalPerturbation:
    model_sha = _model_hash(spec)
    step = 'nominal' if nominal else 'uncertainty'
    ordered_deltas = {axis_id: float(deltas[axis_id]) for axis_id in sorted(deltas)}
    identity = {
        'robustness_spec_sha256': spec.robustness_spec_sha256,
        'uncertainty_model_sha256': model_sha,
        'candidate_id': spec.candidate_id,
        'sample_index': sample_index,
        'uncertainty_item_id': item_id,
        'step': step,
        'parameter_deltas': ordered_deltas,
        'probability_weight': probability_weight,
        'sampling_seed': spec.sampling_seed,
        'algorithm_version': spec.algorithm_version,
    }
    return LocalPerturbation(
        sample_id=_semantic_id('rp', canonical_robustness_sha256(identity)),
        sample_index=sample_index,
        axis_id=None,
        step=step,
        parameter_deltas=ordered_deltas,
        uncertainty_model_sha256=model_sha,
        uncertainty_item_id=item_id,
        probability_weight=probability_weight,
    )


def build_uncertainty_sampling_plan(
    spec: RobustnessSpec,
) -> tuple[LocalPerturbation, ...]:
    """Build deterministic model-specific O90B sample identities and order."""

    if spec.sampling_strategy != UNCERTAINTY_SAMPLING_STRATEGY:
        raise ValueError('explicit uncertainty plan requires O90B uncertainty spec')
    if spec.input_uncertainty_model is None or spec.sample_count is None:
        raise ValueError('explicit uncertainty spec is incomplete')

    model = spec.input_uncertainty_model
    probability = _has_explicit_probability(model)
    plans = [
        _plan(
            spec,
            sample_index=0,
            item_id='nominal',
            deltas={},
            probability_weight=0.0 if probability else None,
            nominal=True,
        )
    ]

    if isinstance(model, DistributionUncertaintyModel):
        draw_count = spec.sample_count - 1
        axis_models = {axis.axis_id: axis for axis in model.axes}
        permutations = {
            axis_id: _distribution_permutation(
                spec,
                axis_id=axis_id,
                count=draw_count,
            )
            for axis_id in sorted(axis_models)
        }
        for draw_index in range(draw_count):
            deltas: dict[str, float] = {}
            for axis_id in sorted(axis_models):
                rank = permutations[axis_id][draw_index]
                unit_coordinate = (rank + 0.5) / draw_count
                deltas[axis_id] = _distribution_delta(
                    axis_models[axis_id],
                    unit_coordinate,
                )
            plans.append(
                _plan(
                    spec,
                    sample_index=draw_index + 1,
                    item_id=f'distribution-draw-{draw_index:06d}',
                    deltas=deltas,
                    probability_weight=1.0 / draw_count,
                    nominal=False,
                )
            )
    else:
        for item_index, state in enumerate(_explicit_states(model), start=1):
            plans.append(
                _plan(
                    spec,
                    sample_index=item_index,
                    item_id=state.state_id,
                    deltas=state.parameter_deltas,
                    probability_weight=state.probability_weight,
                    nominal=False,
                )
            )

    return tuple(plans)


def _validate_reusable_sample(
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
        and sample.uncertainty_model_sha256 == plan.uncertainty_model_sha256
        and sample.uncertainty_item_id == plan.uncertainty_item_id
        and sample.probability_weight == plan.probability_weight
    )
    if not expected:
        raise ValueError(
            'stale or incompatible completed robustness sample cannot be reused'
        )


def _merge_reusable_samples(
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
        _validate_reusable_sample(spec, plan, sample)
        existing = reusable.get(sample.sample_id)
        if existing is not None and existing.sample_sha256 != sample.sample_sha256:
            raise ValueError('conflicting completed robustness sample evidence')
        reusable[sample.sample_id] = sample
    return reusable


def _weighted_percentile(
    values: Sequence[tuple[float, float, str]],
    quantile: float,
) -> float:
    ordered = sorted(values, key=lambda item: (item[0], item[2]))
    total = sum(weight for _value, weight, _sample_id in ordered)
    if total <= 0.0:
        raise ValueError('weighted percentile requires positive probability mass')
    threshold = quantile * total
    cumulative = 0.0
    for value, weight, _sample_id in ordered:
        cumulative += weight
        if cumulative + 1e-15 >= threshold:
            return float(value)
    return float(ordered[-1][0])


def build_uncertainty_robustness_evaluations(
    spec: RobustnessSpec,
    samples: Sequence[PerturbationSample],
    *,
    created_at_utc: str | None = None,
) -> tuple[RobustnessEvaluation, ...]:
    """Summarize exact finite evidence with probability only when explicitly valid."""

    plans = build_uncertainty_sampling_plan(spec)
    expected_ids = tuple(plan.sample_id for plan in plans)
    ordered = tuple(sorted(samples, key=lambda item: item.sample_index))
    if tuple(item.sample_id for item in ordered) != expected_ids:
        raise ValueError('robustness samples do not match explicit uncertainty design')
    for plan, sample in zip(plans, ordered, strict=True):
        _validate_reusable_sample(spec, plan, sample)

    nominal = ordered[0]
    if nominal.step != 'nominal' or nominal.objective_vector is None:
        raise ValueError('explicit uncertainty robustness requires scored nominal sample')

    uncertainty_samples = ordered[1:]
    if not uncertainty_samples:
        raise ValueError('explicit uncertainty evaluation requires perturbation samples')
    feasible_fraction = sum(1 for item in ordered if item.feasible) / len(ordered)
    model = spec.input_uncertainty_model
    assert model is not None
    probability_enabled = _has_explicit_probability(model)
    probability_semantics = _probability_semantics(model)
    if probability_enabled:
        weights = [
            float(item.probability_weight or 0.0) for item in uncertainty_samples
        ]
        if not isclose(sum(weights), 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError('explicit probability sample weights must sum to 1')
        violation_probability = sum(
            (
                float(item.probability_weight or 0.0)
                for item in uncertainty_samples
                if item.failure_reason == 'hard_constraint_violation'
            ),
            0.0,
        )
        probability_sample_ids = tuple(item.sample_id for item in uncertainty_samples)
        percentile_semantics = 'explicit_probability_model'
    else:
        violation_probability = None
        probability_sample_ids = ()
        percentile_semantics = (
            'not_available_empirical_unweighted'
            if isinstance(model, EmpiricalUncertaintyModel)
            else 'not_available_discrete_unweighted'
        )

    provenance = {
        'robustness_spec_id': spec.robustness_spec_id,
        'robustness_spec_sha256': spec.robustness_spec_sha256,
        'document_id': spec.document_id,
        'scene_revision_id': spec.scene_revision_id,
        'scene_content_hash': spec.scene_content_hash,
        'candidate_id': spec.candidate_id,
        'candidate_sha256': spec.candidate_sha256,
        'nominal_objective_evaluation_id': spec.nominal_objective_evaluation_id,
        'nominal_objective_evaluation_sha256': (
            spec.nominal_objective_evaluation_sha256
        ),
        'objective_evaluation_spec_sha256': spec.objective_evaluation_spec_sha256,
        'model_id': spec.model_id,
        'model_version': spec.model_version,
        'prediction_provider_id': spec.prediction_provider_id,
        'fidelity': spec.fidelity,
        'sampling_strategy': spec.sampling_strategy,
        'algorithm_version': spec.algorithm_version,
        'sampling_seed': spec.sampling_seed,
        'uncertainty_model': model.model_dump(mode='json'),
        'uncertainty_model_sha256': _model_hash(spec),
        'sample_ids': [item.sample_id for item in ordered],
    }
    provenance_sha = canonical_robustness_sha256(provenance)
    timestamp = created_at_utc or robustness_timestamp_utc()

    evaluations: list[RobustnessEvaluation] = []
    for nominal_metric in nominal.objective_vector.metrics:
        scored: list[tuple[PerturbationSample, ObjectiveMetric]] = []
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

        percentile_values: dict[str, float] | None = None
        mean_value: float | None = None
        if probability_enabled:
            weighted_values: list[tuple[float, float, str]] = []
            for sample in uncertainty_samples:
                weight = float(sample.probability_weight or 0.0)
                if weight <= 0.0 or not sample.feasible:
                    continue
                if sample.objective_vector is None:
                    raise ValueError(
                        'probability statistics require all feasible probability '
                        'samples to retain objective evidence'
                    )
                metric = sample.objective_vector.metric(nominal_metric.objective_id)
                weighted_values.append(
                    (float(metric.value), weight, sample.sample_id)
                )
            scored_mass = sum(weight for _value, weight, _sid in weighted_values)
            if scored_mass <= 0.0:
                raise ValueError(
                    'probability objective statistics require positive feasible mass'
                )
            mean_value = sum(
                value * weight for value, weight, _sid in weighted_values
            ) / scored_mass
            percentile_values = {
                'p05': _weighted_percentile(weighted_values, 0.05),
                'p50': _weighted_percentile(weighted_values, 0.50),
                'p95': _weighted_percentile(weighted_values, 0.95),
            }

        envelope = SampledObjectiveEnvelope(
            sampled_min_sample_id=sampled_min_sample.sample_id,
            sampled_min_value=float(sampled_min_metric.value),
            sampled_max_sample_id=sampled_max_sample.sample_id,
            sampled_max_value=float(sampled_max_metric.value),
            percentile_values=percentile_values,
        )
        identity = {
            'schema_version': ROBUSTNESS_SCHEMA_VERSION,
            'robustness_spec_id': spec.robustness_spec_id,
            'robustness_spec_sha256': spec.robustness_spec_sha256,
            'candidate_id': spec.candidate_id,
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
            'sampling_provenance_sha256': provenance_sha,
            'percentile_semantics': percentile_semantics,
        }
        if probability_enabled:
            identity.update(
                {
                    'probability_semantics': probability_semantics,
                    'mean_value': mean_value,
                    'constraint_violation_probability': violation_probability,
                    'probability_sample_ids': list(probability_sample_ids),
                }
            )
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


def evaluate_uncertainty_robustness(
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
    """Evaluate/resume O90B with exact cache identity and stale-result rejection."""

    if spec.sampling_strategy != UNCERTAINTY_SAMPLING_STRATEGY:
        raise ValueError('O90B uncertainty evaluation requires explicit uncertainty spec')
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
    objective_schema = _metric_schema(nominal_objective.vector)
    plans = build_uncertainty_sampling_plan(spec)
    timestamp = created_at_utc or robustness_timestamp_utc()

    cache_samples: tuple[PerturbationSample, ...] = ()
    if cache is not None:
        cache.save_spec(spec)
        cache_samples = cache.list_reusable_samples(spec)

    reusable = _merge_reusable_samples(
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
    evaluations = build_uncertainty_robustness_evaluations(
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
