from __future__ import annotations

from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .optimization_objectives import ObjectiveMetric, ObjectiveVector


PARETO_LEGACY_ALGORITHM_VERSION = 'pareto-front-1'
PARETO_ALGORITHM_VERSION = 'pareto-front-2'


class ParetoError(ValueError):
    pass


class ParetoResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    algorithm_version: Literal[
        'pareto-front-1',
        'pareto-front-2',
    ] = PARETO_ALGORITHM_VERSION
    objective_ids: tuple[str, ...] = Field(min_length=1)
    non_dominated_candidate_ids: tuple[str, ...]
    dominated_by: dict[str, tuple[str, ...]]

    @model_validator(mode='after')
    def valid_candidate_partition(self) -> 'ParetoResult':
        if len(self.objective_ids) != len(set(self.objective_ids)):
            raise ValueError('Pareto objective ids must be unique')
        return self


def _selected_metrics(
    vector: ObjectiveVector,
    objective_ids: tuple[str, ...],
) -> tuple[ObjectiveMetric, ...]:
    metrics: list[ObjectiveMetric] = []
    for objective_id in objective_ids:
        try:
            metric = vector.metric(objective_id)
        except KeyError as exc:
            raise ParetoError(
                f'candidate {vector.candidate_id} is missing objective {objective_id}'
            ) from exc
        if metric.state != 'available' or metric.value is None:
            raise ParetoError(
                f'candidate {vector.candidate_id} objective {objective_id} '
                f'is not comparison-eligible: {metric.state}'
            )
        metrics.append(metric)
    return tuple(metrics)


def _validate_comparison_authority(
    selected_by_candidate: dict[str, tuple[ObjectiveMetric, ...]],
    objective_ids: tuple[str, ...],
) -> None:
    reference_candidate_id = next(iter(selected_by_candidate))
    reference = selected_by_candidate[reference_candidate_id]
    for candidate_id, metrics in selected_by_candidate.items():
        for objective_id, expected, actual in zip(
            objective_ids,
            reference,
            metrics,
            strict=True,
        ):
            if actual.definition_id != expected.definition_id:
                raise ParetoError(
                    f'objective {objective_id} is not comparable between '
                    f'{reference_candidate_id} and {candidate_id}: '
                    'definition/unit/direction/model mismatch'
                )


def dominates(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    """Legacy minimize-only numeric dominance helper."""

    if len(a) != len(b) or not a:
        raise ParetoError('dominance comparison requires non-empty vectors of equal length')
    return all(left <= right for left, right in zip(a, b, strict=True)) and any(
        left < right for left, right in zip(a, b, strict=True)
    )


def _dominates_metrics(
    a: tuple[ObjectiveMetric, ...],
    b: tuple[ObjectiveMetric, ...],
) -> bool:
    if len(a) != len(b) or not a:
        raise ParetoError('dominance comparison requires non-empty vectors of equal length')

    no_worse = True
    strictly_better = False
    for left, right in zip(a, b, strict=True):
        if left.definition_id != right.definition_id:
            raise ParetoError(
                f'objective {left.objective_id} comparison authority mismatch'
            )
        left_value = left.comparison_value()
        right_value = right.comparison_value()
        if left.direction == 'minimize':
            if left_value > right_value:
                no_worse = False
                break
            if left_value < right_value:
                strictly_better = True
        elif left.direction == 'maximize':
            if left_value < right_value:
                no_worse = False
                break
            if left_value > right_value:
                strictly_better = True
        else:  # pragma: no cover - Pydantic guards the closed direction enum.
            raise ParetoError(f'unsupported Pareto objective direction: {left.direction}')
    return no_worse and strictly_better


def pareto_front(
    vectors: Sequence[ObjectiveVector],
    objective_ids: Sequence[str] | None = None,
) -> ParetoResult:
    if not vectors:
        raise ParetoError('Pareto extraction requires at least one objective vector')

    candidate_ids = [vector.candidate_id for vector in vectors]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ParetoError('candidate ids must be unique for Pareto extraction')

    if objective_ids is None:
        selected = tuple(metric.objective_id for metric in vectors[0].metrics)
    else:
        selected = tuple(objective_ids)
    if not selected or len(selected) != len(set(selected)):
        raise ParetoError('Pareto objective ids must be non-empty and unique')

    metrics_by_candidate = {
        vector.candidate_id: _selected_metrics(vector, selected)
        for vector in vectors
    }
    _validate_comparison_authority(metrics_by_candidate, selected)

    dominated_by: dict[str, tuple[str, ...]] = {}
    non_dominated: list[str] = []
    for vector in vectors:
        vector_metrics = metrics_by_candidate[vector.candidate_id]
        dominators = tuple(
            other.candidate_id
            for other in vectors
            if other.candidate_id != vector.candidate_id
            and _dominates_metrics(
                metrics_by_candidate[other.candidate_id],
                vector_metrics,
            )
        )
        dominated_by[vector.candidate_id] = dominators
        if not dominators:
            non_dominated.append(vector.candidate_id)

    legacy_minimize = all(
        metric.is_legacy_minimize
        for metrics in metrics_by_candidate.values()
        for metric in metrics
    )
    return ParetoResult(
        algorithm_version=(
            PARETO_LEGACY_ALGORITHM_VERSION
            if legacy_minimize
            else PARETO_ALGORITHM_VERSION
        ),
        objective_ids=selected,
        non_dominated_candidate_ids=tuple(non_dominated),
        dominated_by=dominated_by,
    )
