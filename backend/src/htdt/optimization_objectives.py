from __future__ import annotations

from hashlib import sha256
from itertools import combinations
import json
from math import isfinite, sqrt
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .comparison import ComparisonError, FrequencyResponse, compare_frequency_responses


OBJECTIVE_ALGORITHM_VERSION = 'objective-vector-1'
OBJECTIVE_DEFINITION_VERSION = 'objective-definition-1'
OBJECTIVE_COMPARISON_TRANSFORM_VERSION = 'identity-physical-value-1'

ObjectiveDirection = Literal['minimize', 'maximize']
ObjectiveState = Literal['available', 'missing', 'unsupported']


class ObjectiveError(ValueError):
    pass


def canonical_objective_definition_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def canonical_objective_definition_sha256(value: Any) -> str:
    return sha256(canonical_objective_definition_json(value).encode('utf-8')).hexdigest()


class ObjectiveValidDomain(BaseModel):
    """Declared physical domain for one objective quantity."""

    model_config = ConfigDict(frozen=True)

    kind: Literal['finite_real', 'bounded_real']
    minimum: float | None = None
    maximum: float | None = None
    minimum_inclusive: bool = True
    maximum_inclusive: bool = True

    @model_validator(mode='after')
    def valid_domain(self) -> 'ObjectiveValidDomain':
        if self.kind == 'finite_real':
            if self.minimum is not None or self.maximum is not None:
                raise ValueError('finite_real objective domain cannot carry bounds')
            return self

        if self.minimum is None and self.maximum is None:
            raise ValueError('bounded_real objective domain requires at least one bound')
        if self.minimum is not None and not isfinite(float(self.minimum)):
            raise ValueError('objective domain minimum must be finite')
        if self.maximum is not None and not isfinite(float(self.maximum)):
            raise ValueError('objective domain maximum must be finite')
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.maximum < self.minimum
        ):
            raise ValueError('objective domain maximum must be >= minimum')
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.maximum == self.minimum
            and not (self.minimum_inclusive and self.maximum_inclusive)
        ):
            raise ValueError('objective domain cannot be empty')
        return self

    def contains(self, value: float) -> bool:
        numeric = float(value)
        if not isfinite(numeric):
            return False
        if self.kind == 'finite_real':
            return True
        if self.minimum is not None:
            if self.minimum_inclusive:
                if numeric < self.minimum:
                    return False
            elif numeric <= self.minimum:
                return False
        if self.maximum is not None:
            if self.maximum_inclusive:
                if numeric > self.maximum:
                    return False
            elif numeric >= self.maximum:
                return False
        return True


class ObjectiveDefinition(BaseModel):
    """Versioned comparison authority for a physical optimization objective."""

    model_config = ConfigDict(frozen=True)

    definition_version: Literal['objective-definition-1'] = OBJECTIVE_DEFINITION_VERSION
    objective_id: str = Field(min_length=1)
    quantity: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    direction: ObjectiveDirection
    valid_domain: ObjectiveValidDomain
    comparison_model_id: str = Field(min_length=1)
    comparison_model_version: str = Field(min_length=1)
    comparison_transform_version: Literal[
        'identity-physical-value-1'
    ] = OBJECTIVE_COMPARISON_TRANSFORM_VERSION

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json')

    @property
    def semantic_hash(self) -> str:
        return canonical_objective_definition_sha256(self.identity_payload())

    @property
    def definition_id(self) -> str:
        return f'objdef-{self.semantic_hash}'

    @classmethod
    def legacy(
        cls,
        *,
        objective_id: str,
        unit: str,
        direction: ObjectiveDirection = 'minimize',
    ) -> 'ObjectiveDefinition':
        return cls(
            objective_id=objective_id,
            quantity=objective_id,
            unit=unit,
            direction=direction,
            valid_domain=ObjectiveValidDomain(kind='finite_real'),
            comparison_model_id='legacy-objective-metric',
            comparison_model_version='1',
        )

    def derived(self, objective_id: str) -> 'ObjectiveDefinition':
        return self.model_copy(update={'objective_id': objective_id})


class ResponseObjectiveSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    low_hz: float = Field(gt=0.0)
    high_hz: float = Field(gt=0.0)
    reference_band_hz: tuple[float, float] | None = None
    excluded_bands: tuple[tuple[float, float], ...] = ()

    @model_validator(mode='after')
    def valid_bands(self) -> 'ResponseObjectiveSpec':
        if not isfinite(self.low_hz) or not isfinite(self.high_hz):
            raise ValueError('objective response band must be finite')
        if self.high_hz <= self.low_hz:
            raise ValueError('objective response high_hz must be greater than low_hz')
        if self.reference_band_hz is not None:
            low, high = self.reference_band_hz
            if not all(isfinite(value) and value > 0.0 for value in (low, high)) or high <= low:
                raise ValueError('reference band must be finite, positive and ordered')
        for low, high in self.excluded_bands:
            if not all(isfinite(value) and value > 0.0 for value in (low, high)) or high < low:
                raise ValueError('excluded bands must be finite, positive and ordered')
        return self


class ObjectiveMetric(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective_id: str = Field(min_length=1)
    value: float | None
    unit: str = Field(min_length=1)
    direction: ObjectiveDirection = 'minimize'
    state: ObjectiveState = 'available'
    definition: ObjectiveDefinition | None = None

    @model_validator(mode='after')
    def valid_metric(self) -> 'ObjectiveMetric':
        if self.definition is None:
            if self.direction != 'minimize':
                raise ValueError('maximize objective requires an explicit ObjectiveDefinition')
            if self.state != 'available':
                raise ValueError(
                    'missing/unsupported objective requires an explicit ObjectiveDefinition'
                )
        else:
            if (
                self.definition.objective_id != self.objective_id
                or self.definition.unit != self.unit
                or self.definition.direction != self.direction
            ):
                raise ValueError(
                    'objective metric id/unit/direction must match ObjectiveDefinition'
                )

        if self.state == 'available':
            if self.value is None or not isfinite(float(self.value)):
                raise ValueError('available objective metric value must be finite')
            if (
                self.definition is not None
                and not self.definition.valid_domain.contains(float(self.value))
            ):
                raise ValueError('objective metric value is outside its declared valid domain')
        elif self.value is not None:
            raise ValueError('missing/unsupported objective metric must not carry a value')
        return self

    @property
    def is_legacy_minimize(self) -> bool:
        return (
            self.definition is None
            and self.state == 'available'
            and self.direction == 'minimize'
        )

    def effective_definition(self) -> ObjectiveDefinition:
        if self.definition is not None:
            return self.definition
        return ObjectiveDefinition.legacy(
            objective_id=self.objective_id,
            unit=self.unit,
            direction=self.direction,
        )

    @property
    def definition_id(self) -> str:
        return self.effective_definition().definition_id

    def comparison_value(self) -> float:
        if self.state != 'available' or self.value is None:
            raise ObjectiveError(
                f'objective {self.objective_id} is not comparison-eligible: {self.state}'
            )
        return float(self.value)

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'objective_id': self.objective_id,
            'value': self.value,
            'unit': self.unit,
            'direction': self.direction,
        }
        # Preserve the historical objective-vector-1 hash shape for legacy
        # minimize metrics while making all new authority explicit.
        if not self.is_legacy_minimize:
            payload['state'] = self.state
            payload['definition'] = (
                None
                if self.definition is None
                else self.definition.model_dump(mode='json')
            )
        return payload


class ObjectiveVector(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    metrics: tuple[ObjectiveMetric, ...] = Field(min_length=1)
    algorithm_version: Literal['objective-vector-1'] = OBJECTIVE_ALGORITHM_VERSION

    @model_validator(mode='after')
    def unique_objectives(self) -> 'ObjectiveVector':
        ids = [metric.objective_id for metric in self.metrics]
        if len(ids) != len(set(ids)):
            raise ValueError('objective ids must be unique within a vector')
        return self

    def metric(self, objective_id: str) -> ObjectiveMetric:
        for metric in self.metrics:
            if metric.objective_id == objective_id:
                return metric
        raise KeyError(objective_id)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'candidate_id': self.candidate_id,
            'metrics': [metric.identity_payload() for metric in self.metrics],
            'algorithm_version': self.algorithm_version,
        }


def _comparison(
    response: FrequencyResponse,
    reference: FrequencyResponse,
    spec: ResponseObjectiveSpec,
):
    try:
        return compare_frequency_responses(
            response,
            reference,
            spec.low_hz,
            spec.high_hz,
            reference_band_hz=spec.reference_band_hz,
            excluded_bands=spec.excluded_bands,
        )
    except ComparisonError as exc:
        raise ObjectiveError(str(exc)) from exc


def target_response_objectives(
    candidate_id: str,
    response: FrequencyResponse,
    target: FrequencyResponse,
    spec: ResponseObjectiveSpec,
    *,
    prefix: str = 'response',
) -> ObjectiveVector:
    result = _comparison(response, target, spec)
    if result.rms_difference_db is None or not result.difference_db:
        raise ObjectiveError('target response objective requires at least two valid frequency points')

    peak_excess = max(0.0, max(result.difference_db))
    dip_deficit = max(0.0, -min(result.difference_db))
    metrics: list[ObjectiveMetric] = [
        ObjectiveMetric(
            objective_id=f'{prefix}.rms_difference_db',
            value=result.rms_difference_db,
            unit='dB',
        ),
        ObjectiveMetric(
            objective_id=f'{prefix}.peak_excess_db',
            value=peak_excess,
            unit='dB',
        ),
        ObjectiveMetric(
            objective_id=f'{prefix}.dip_deficit_db',
            value=dip_deficit,
            unit='dB',
        ),
    ]
    if spec.reference_band_hz is not None:
        if result.shape_rms_db is None:
            raise ObjectiveError('shape objective requires at least two valid reference-band points')
        metrics.append(ObjectiveMetric(
            objective_id=f'{prefix}.shape_rms_db',
            value=result.shape_rms_db,
            unit='dB',
        ))
    return ObjectiveVector(candidate_id=candidate_id, metrics=tuple(metrics))


def pair_response_objectives(
    candidate_id: str,
    a: FrequencyResponse,
    b: FrequencyResponse,
    spec: ResponseObjectiveSpec,
    *,
    prefix: str = 'pair',
) -> ObjectiveVector:
    result = _comparison(a, b, spec)
    if result.rms_difference_db is None:
        raise ObjectiveError('pair response objective requires at least two valid frequency points')

    metrics: list[ObjectiveMetric] = [
        ObjectiveMetric(
            objective_id=f'{prefix}.rms_difference_db',
            value=result.rms_difference_db,
            unit='dB',
        ),
    ]
    if spec.reference_band_hz is not None:
        if result.shape_rms_db is None:
            raise ObjectiveError('pair shape objective requires at least two valid reference-band points')
        metrics.append(ObjectiveMetric(
            objective_id=f'{prefix}.shape_rms_db',
            value=result.shape_rms_db,
            unit='dB',
        ))
    return ObjectiveVector(candidate_id=candidate_id, metrics=tuple(metrics))


def seat_pairwise_objectives(
    candidate_id: str,
    responses: Sequence[FrequencyResponse],
    spec: ResponseObjectiveSpec,
    *,
    prefix: str = 'seat',
) -> ObjectiveVector:
    if len(responses) < 2:
        raise ObjectiveError('seat spread objective requires at least two responses')

    level_values: list[float] = []
    shape_values: list[float] = []
    for a, b in combinations(responses, 2):
        result = _comparison(a, b, spec)
        if result.rms_difference_db is None:
            raise ObjectiveError('seat spread objective requires at least two valid points per pair')
        level_values.append(float(result.rms_difference_db))
        if spec.reference_band_hz is not None:
            if result.shape_rms_db is None:
                raise ObjectiveError('seat shape objective requires at least two valid reference-band points per pair')
            shape_values.append(float(result.shape_rms_db))

    level_rms = sqrt(sum(value * value for value in level_values) / len(level_values))
    metrics: list[ObjectiveMetric] = [
        ObjectiveMetric(
            objective_id=f'{prefix}.pairwise_rms_difference_max_db',
            value=max(level_values),
            unit='dB',
        ),
        ObjectiveMetric(
            objective_id=f'{prefix}.pairwise_rms_difference_rms_db',
            value=level_rms,
            unit='dB',
        ),
    ]
    if shape_values:
        shape_rms = sqrt(sum(value * value for value in shape_values) / len(shape_values))
        metrics.extend((
            ObjectiveMetric(
                objective_id=f'{prefix}.pairwise_shape_max_db',
                value=max(shape_values),
                unit='dB',
            ),
            ObjectiveMetric(
                objective_id=f'{prefix}.pairwise_shape_rms_db',
                value=shape_rms,
                unit='dB',
            ),
        ))
    return ObjectiveVector(candidate_id=candidate_id, metrics=tuple(metrics))


def _xyz(position: Mapping[str, float]) -> tuple[float, float, float]:
    try:
        values = (
            float(position['x_m']),
            float(position['y_m']),
            float(position['z_m']),
        )
    except KeyError as exc:
        raise ObjectiveError('movement position requires x_m/y_m/z_m') from exc
    if not all(isfinite(value) for value in values):
        raise ObjectiveError('movement positions must be finite')
    return values


def movement_objectives(
    candidate_id: str,
    baseline_positions: Mapping[str, Mapping[str, float]],
    candidate_positions: Mapping[str, Mapping[str, float]],
    *,
    prefix: str = 'movement',
) -> ObjectiveVector:
    if not candidate_positions:
        raise ObjectiveError('movement objective requires at least one moved entity')

    distances: list[float] = []
    for entity_id in sorted(candidate_positions):
        if entity_id not in baseline_positions:
            raise ObjectiveError(f'missing movement baseline for entity: {entity_id}')
        before = _xyz(baseline_positions[entity_id])
        after = _xyz(candidate_positions[entity_id])
        distances.append(sqrt(sum((b - a) ** 2 for a, b in zip(before, after, strict=True))))

    return ObjectiveVector(
        candidate_id=candidate_id,
        metrics=(
            ObjectiveMetric(
                objective_id=f'{prefix}.total_m',
                value=sum(distances),
                unit='m',
            ),
            ObjectiveMetric(
                objective_id=f'{prefix}.max_m',
                value=max(distances),
                unit='m',
            ),
        ),
    )


def merge_objective_vectors(candidate_id: str, *vectors: ObjectiveVector) -> ObjectiveVector:
    if not vectors:
        raise ObjectiveError('at least one objective vector is required')

    merged: list[ObjectiveMetric] = []
    seen: set[str] = set()
    for vector in vectors:
        if vector.candidate_id != candidate_id:
            raise ObjectiveError('cannot merge objective vectors for different candidates')
        for metric in vector.metrics:
            if metric.objective_id in seen:
                raise ObjectiveError(f'duplicate objective while merging: {metric.objective_id}')
            seen.add(metric.objective_id)
            merged.append(metric)
    return ObjectiveVector(candidate_id=candidate_id, metrics=tuple(merged))
