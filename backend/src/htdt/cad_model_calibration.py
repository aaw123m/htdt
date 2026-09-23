"""R180 acoustic model calibration authority (#522).

Distinct from microphone calibration, DSP CalibrationPlan, O70 residual
correction and O60 validation: this module defines the immutable
preregistered spec (fitted/fixed parameters with bounds, evidence refs,
objective, optimizer, deterministic seed, intended holdout), the calibration
result (fitted values producing a *new* calibrated model identity, never a
mutation of the baseline), local identifiability classification, the frozen
model authority consumed by holdout validation, and the holdout-reuse
discipline record.

A low residual never auto-promotes a fitted parameter to measured physical
truth — predictive fit quality and physical identifiability are reported
separately.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite, sqrt
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import FrequencyDomain
from .r120_geometry_compiler import ExactExternalAuthorityRef


MODEL_CALIBRATION_SCHEMA_VERSION = 1
MODEL_CALIBRATION_SPEC_AUTHORITY_VERSION = 'r180-model-calibration-spec-1'
MODEL_CALIBRATION_RESULT_AUTHORITY_VERSION = 'r180-model-calibration-result-1'
MODEL_FREEZE_AUTHORITY_VERSION = 'r180-calibrated-model-freeze-1'
HOLDOUT_DISCIPLINE_AUTHORITY_VERSION = 'r180-holdout-discipline-1'

ParameterTargetKind = Literal[
    'boundary_surface_material',
    'source_strength',
    'environment',
    'scattering_parameter',
]
ParameterTransform = Literal['identity', 'log', 'logit01']
IdentifiabilityState = Literal[
    'identifiable_within_experiment',
    'weakly_identifiable',
    'correlated_non_identifiable',
    'not_evaluated',
]


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class CalibrationParameterDefinition(BaseModel):
    """One typed model parameter; never an anonymous vector component."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    parameter_id: str = Field(min_length=1)
    target_kind: ParameterTargetKind
    target_id: str = Field(min_length=1)
    quantity: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    model_family: str = Field(min_length=1)
    transform: ParameterTransform = 'identity'
    role: Literal['fitted', 'fixed']
    lower_bound: float | None = None
    upper_bound: float | None = None
    fixed_value: float | None = None

    @field_validator('lower_bound', 'upper_bound', 'fixed_value')
    @classmethod
    def finite_bounds(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='parameter bound')

    @model_validator(mode='after')
    def validate_parameter(self) -> 'CalibrationParameterDefinition':
        if self.role == 'fitted':
            if self.lower_bound is None or self.upper_bound is None:
                raise ValueError('fitted parameter requires preregistered bounds')
            if self.upper_bound <= self.lower_bound:
                raise ValueError('parameter upper bound must exceed lower bound')
            if self.fixed_value is not None:
                raise ValueError('fitted parameter cannot carry a fixed value')
        else:
            if self.fixed_value is None:
                raise ValueError('fixed parameter requires an explicit value')
            if self.lower_bound is not None or self.upper_bound is not None:
                raise ValueError('fixed parameter must not carry bounds')
        return self


class CalibrationEvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    evidence_kind: Literal['measurement', 'campaign']
    evidence_id: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class CalibrationObjectiveSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    objective_id: Literal['weighted_residual_energy_v1'] = (
        'weighted_residual_energy_v1'
    )
    observable: str = Field(min_length=1)
    frequency_domain: FrequencyDomain
    weighting: Literal['uniform', 'per_octave'] = 'uniform'


class CalibrationOptimizerSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    optimizer_id: Literal['deterministic_grid_search_v1'] = (
        'deterministic_grid_search_v1'
    )
    optimizer_version: Literal['1'] = '1'
    max_evaluations: int = Field(gt=0)
    deterministic_seed: int = Field(ge=0, default=0)


class AcousticModelCalibrationSpec(BaseModel):
    """Preregistered calibration design; the semantic hash is the registration."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MODEL_CALIBRATION_SCHEMA_VERSION
    authority_version: Literal[
        'r180-model-calibration-spec-1'
    ] = MODEL_CALIBRATION_SPEC_AUTHORITY_VERSION
    spec_id: str = Field(pattern=r'^model-calibration-spec:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    baseline_snapshot_id: str = Field(min_length=1)
    baseline_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_id: str = Field(min_length=1)
    solver_version: str = Field(min_length=1)
    calibration_evidence: tuple[CalibrationEvidenceRef, ...] = Field(
        min_length=1
    )
    parameters: tuple[CalibrationParameterDefinition, ...] = Field(min_length=1)
    objective: CalibrationObjectiveSpec
    optimizer: CalibrationOptimizerSpec
    required_measurement_capabilities: tuple[str, ...] = ()
    holdout_campaign_ref: ExactExternalAuthorityRef

    @model_validator(mode='after')
    def validate_spec(self) -> 'AcousticModelCalibrationSpec':
        parameter_ids = [item.parameter_id for item in self.parameters]
        if len(parameter_ids) != len(set(parameter_ids)):
            raise ValueError('calibration parameter ids must be unique')
        if not any(item.role == 'fitted' for item in self.parameters):
            raise ValueError('a calibration spec must fit at least one parameter')
        evidence_ids = [
            (item.evidence_kind, item.evidence_id)
            for item in self.calibration_evidence
        ]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError('calibration evidence refs must be unique')
        if self.holdout_campaign_ref in tuple(self.calibration_evidence):
            raise ValueError('holdout evidence cannot double as calibration data')
        holdout_ids = {item.evidence_id for item in self.calibration_evidence}
        if self.holdout_campaign_ref.authority_id in holdout_ids:
            raise ValueError(
                'preregistered holdout campaign must be distinct from '
                'calibration evidence'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('calibration spec semantic hash mismatch')
        if self.spec_id != f'model-calibration-spec:{expected}':
            raise ValueError('calibration spec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'spec_id', 'semantic_sha256'},
        )

    @property
    def fitted_parameters(self) -> tuple[CalibrationParameterDefinition, ...]:
        return tuple(item for item in self.parameters if item.role == 'fitted')


def build_model_calibration_spec(**kwargs: Any) -> AcousticModelCalibrationSpec:
    probe = AcousticModelCalibrationSpec.model_construct(
        spec_id='model-calibration-spec:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )
    digest = _digest(probe.semantic_payload())
    return AcousticModelCalibrationSpec(
        spec_id=f'model-calibration-spec:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


class CalibrationEvaluationPort(Protocol):
    """Deterministic objective evaluator the bounded optimizer drives.

    ``evaluate`` returns the per-sample weighted residual vector in the
    declared observable/band; ``residual_norm`` defaults to the L2 norm.
    """

    def evaluate(
        self,
        parameter_values: dict[str, float],
    ) -> tuple[float, ...]:
        ...


TerminationState = Literal['converged', 'budget_exhausted']


class ParameterSensitivityEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    parameter_id: str = Field(min_length=1)
    sensitivity_norm: float = Field(ge=0.0)
    identifiability: IdentifiabilityState


class ParameterCorrelationGroup(BaseModel):
    """One correlated/non-identifiable parameter group (|corr| above threshold)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    parameter_ids: tuple[str, ...] = Field(min_length=2)
    max_abs_correlation: float = Field(gt=0.0, le=1.0)

    @model_validator(mode='after')
    def validate_group(self) -> 'ParameterCorrelationGroup':
        if len(set(self.parameter_ids)) != len(self.parameter_ids):
            raise ValueError('correlation group parameter ids must be unique')
        return self


class AcousticModelCalibrationResult(BaseModel):
    """Immutable calibration outcome; the calibrated model is a NEW identity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MODEL_CALIBRATION_SCHEMA_VERSION
    authority_version: Literal[
        'r180-model-calibration-result-1'
    ] = MODEL_CALIBRATION_RESULT_AUTHORITY_VERSION
    result_id: str = Field(pattern=r'^model-calibration-result:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    spec_id: str = Field(pattern=r'^model-calibration-spec:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    fitted_values: tuple[tuple[str, float], ...] = Field(min_length=1)
    calibrated_model_id: str = Field(pattern=r'^calibrated-model:[0-9a-f]{64}$')
    calibrated_model_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    training_residual_norm: float = Field(ge=0.0)
    training_sample_count: int = Field(gt=0)
    termination_state: TerminationState
    evaluations_used: int = Field(gt=0)
    sensitivity: tuple[ParameterSensitivityEvidence, ...]
    correlation_groups: tuple[ParameterCorrelationGroup, ...] = ()
    identifiability_verdict: Literal[
        'identifiable_within_experiment',
        'weakly_identifiable',
        'contains_non_identifiable_group',
        'not_evaluated',
    ]

    @model_validator(mode='after')
    def validate_result(self) -> 'AcousticModelCalibrationResult':
        fitted_ids = [item[0] for item in self.fitted_values]
        if len(fitted_ids) != len(set(fitted_ids)):
            raise ValueError('fitted values must be unique per parameter')
        for _, value in self.fitted_values:
            _finite(value, field_name='fitted value')
        expected_model = _digest(
            {
                'kind': 'calibrated-model',
                'spec_semantic_sha256': self.spec_semantic_sha256,
                'fitted_values': [
                    [name, float(value)] for name, value in self.fitted_values
                ],
            }
        )
        if self.calibrated_model_sha256 != expected_model:
            raise ValueError('calibrated model hash mismatch')
        if self.calibrated_model_id != f'calibrated-model:{expected_model}':
            raise ValueError('calibrated model id mismatch')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('calibration result semantic hash mismatch')
        if self.result_id != f'model-calibration-result:{expected}':
            raise ValueError('calibration result id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'result_id', 'semantic_sha256'},
        )


def _grid_points(
    parameter: CalibrationParameterDefinition,
    per_parameter_count: int,
) -> tuple[float, ...]:
    low = float(parameter.lower_bound)
    high = float(parameter.upper_bound)
    if per_parameter_count <= 1:
        return (0.5 * (low + high),)
    step = (high - low) / (per_parameter_count - 1)
    return tuple(low + index * step for index in range(per_parameter_count))


def _transform_search(
    parameter: CalibrationParameterDefinition,
    value: float,
) -> float:
    """Map a fitted value onto the preregistered transform domain."""
    if parameter.transform == 'identity':
        return value
    if parameter.transform == 'log':
        if value <= 0.0:
            raise ValueError('log transform requires positive parameter values')
        return value
    # logit01 maps (0,1); bounds must already lie inside the open interval.
    if not (0.0 < value < 1.0):
        raise ValueError('logit01 transform requires bounds inside (0, 1)')
    return value


def run_model_calibration(
    spec: AcousticModelCalibrationSpec,
    evaluator: CalibrationEvaluationPort,
    *,
    correlation_threshold: float = 0.95,
) -> AcousticModelCalibrationResult:
    """Bounded deterministic grid-search calibration over the spec's parameters.

    The optimizer evaluates every grid point in declaration order and keeps
    the lexicographically first minimum — no stochastic choice in MVP.
    """
    fitted = spec.fitted_parameters
    count = len(fitted)
    per_parameter = max(1, int(spec.optimizer.max_evaluations ** (1.0 / count)))
    grids = [_grid_points(parameter, per_parameter) for parameter in fitted]
    for parameter in fitted:
        for value in _grid_points(parameter, per_parameter):
            _transform_search(parameter, value)

    base_values = {
        item.parameter_id: float(item.fixed_value)
        for item in spec.parameters
        if item.role == 'fixed'
    }

    evaluations = 0
    best_norm: float | None = None
    best_values: dict[str, float] | None = None
    best_residuals: tuple[float, ...] | None = None
    evaluated_grid: list[tuple[dict[str, float], tuple[float, ...]]] = []

    def visit(prefix: dict[str, float], index: int) -> None:
        nonlocal evaluations, best_norm, best_values, best_residuals
        if index == count:
            values = {**base_values, **prefix}
            residuals = tuple(float(v) for v in evaluator.evaluate(values))
            evaluations += 1
            evaluated_grid.append((dict(prefix), residuals))
            norm = sqrt(sum(r * r for r in residuals))
            if best_norm is None or norm < best_norm:
                best_norm = norm
                best_values = dict(prefix)
                best_residuals = residuals
            return
        parameter = fitted[index]
        for value in grids[index]:
            if evaluations >= spec.optimizer.max_evaluations:
                return
            visit({**prefix, parameter.parameter_id: value}, index + 1)

    visit({}, 0)
    if best_values is None or best_residuals is None or best_norm is None:
        raise ValueError('calibration produced no evaluations')

    termination: TerminationState = (
        'converged'
        if evaluations < spec.optimizer.max_evaluations
        else 'budget_exhausted'
    )

    # Local sensitivity: central finite difference of the residual vector
    # along each fitted parameter, using the grid step as the probe distance.
    sensitivity: list[ParameterSensitivityEvidence] = []
    sensitivity_columns: dict[str, list[float]] = {}
    for index, parameter in enumerate(fitted):
        grid = grids[index]
        step = grid[1] - grid[0] if len(grid) > 1 else max(
            float(parameter.upper_bound) - float(parameter.lower_bound), 1e-9
        )
        plus = dict(best_values)
        minus = dict(best_values)
        plus[parameter.parameter_id] = min(
            float(parameter.upper_bound), best_values[parameter.parameter_id] + step
        )
        minus[parameter.parameter_id] = max(
            float(parameter.lower_bound), best_values[parameter.parameter_id] - step
        )
        delta = plus[parameter.parameter_id] - minus[parameter.parameter_id]
        if delta <= 0.0:
            sensitivity_columns[parameter.parameter_id] = [
                0.0
            ] * len(best_residuals)
            sensitivity.append(
                ParameterSensitivityEvidence(
                    parameter_id=parameter.parameter_id,
                    sensitivity_norm=0.0,
                    identifiability='weakly_identifiable',
                )
            )
            continue
        residuals_plus = evaluator.evaluate({**base_values, **plus})
        residuals_minus = evaluator.evaluate({**base_values, **minus})
        column = [
            (float(p) - float(m)) / delta
            for p, m in zip(residuals_plus, residuals_minus)
        ]
        sensitivity_columns[parameter.parameter_id] = column
        norm = sqrt(sum(v * v for v in column))
        sensitivity.append(
            ParameterSensitivityEvidence(
                parameter_id=parameter.parameter_id,
                sensitivity_norm=norm,
                identifiability=(
                    'weakly_identifiable' if norm <= 1e-12 else 'identifiable_within_experiment'
                ),
            )
        )

    # Correlation groups from the normalized sensitivity columns.
    correlated: dict[str, set[str]] = {}
    ids = [parameter.parameter_id for parameter in fitted]
    for i, left_id in enumerate(ids):
        for right_id in ids[i + 1 :]:
            left = sensitivity_columns[left_id]
            right = sensitivity_columns[right_id]
            left_norm = sqrt(sum(v * v for v in left))
            right_norm = sqrt(sum(v * v for v in right))
            if left_norm <= 1e-12 or right_norm <= 1e-12:
                continue
            correlation = abs(
                sum(l * r for l, r in zip(left, right))
            ) / (left_norm * right_norm)
            if correlation >= correlation_threshold:
                correlated.setdefault(left_id, set()).add(right_id)
                correlated.setdefault(right_id, set()).add(left_id)
    seen: set[str] = set()
    groups: list[ParameterCorrelationGroup] = []
    for parameter_id in ids:
        if parameter_id in seen or parameter_id not in correlated:
            continue
        stack = [parameter_id]
        members: set[str] = set()
        max_corr = 0.0
        while stack:
            current = stack.pop()
            if current in members:
                continue
            members.add(current)
            seen.add(current)
            stack.extend(correlated.get(current, ()))
            for other in correlated.get(current, ()):
                left = sensitivity_columns[current]
                right = sensitivity_columns[other]
                left_norm = sqrt(sum(v * v for v in left))
                right_norm = sqrt(sum(v * v for v in right))
                if left_norm > 1e-12 and right_norm > 1e-12:
                    corr = abs(sum(l * r for l, r in zip(left, right))) / (
                        left_norm * right_norm
                    )
                    max_corr = max(max_corr, corr)
        groups.append(
            ParameterCorrelationGroup(
                parameter_ids=tuple(sorted(members)),
                max_abs_correlation=min(1.0, max_corr),
            )
        )

    grouped = {pid for group in groups for pid in group.parameter_ids}
    sensitivity_final: list[ParameterSensitivityEvidence] = []
    for item in sensitivity:
        if item.parameter_id in grouped:
            sensitivity_final.append(
                ParameterSensitivityEvidence(
                    parameter_id=item.parameter_id,
                    sensitivity_norm=item.sensitivity_norm,
                    identifiability='correlated_non_identifiable',
                )
            )
        else:
            sensitivity_final.append(item)

    if not sensitivity_final:
        verdict: Literal[
            'identifiable_within_experiment',
            'weakly_identifiable',
            'contains_non_identifiable_group',
            'not_evaluated',
        ] = 'not_evaluated'
    elif groups:
        verdict = 'contains_non_identifiable_group'
    elif any(
        item.identifiability == 'weakly_identifiable'
        for item in sensitivity_final
    ):
        verdict = 'weakly_identifiable'
    else:
        verdict = 'identifiable_within_experiment'

    fitted_values = tuple(
        (parameter.parameter_id, float(best_values[parameter.parameter_id]))
        for parameter in fitted
    )
    model_digest = _digest(
        {
            'kind': 'calibrated-model',
            'spec_semantic_sha256': spec.semantic_sha256,
            'fitted_values': [
                [name, float(value)] for name, value in fitted_values
            ],
        }
    )
    payload = {
        'schema_version': MODEL_CALIBRATION_SCHEMA_VERSION,
        'authority_version': MODEL_CALIBRATION_RESULT_AUTHORITY_VERSION,
        'spec_id': spec.spec_id,
        'spec_semantic_sha256': spec.semantic_sha256,
        'fitted_values': [[name, value] for name, value in fitted_values],
        'calibrated_model_id': f'calibrated-model:{model_digest}',
        'calibrated_model_sha256': model_digest,
        'training_residual_norm': best_norm,
        'training_sample_count': len(best_residuals),
        'termination_state': termination,
        'evaluations_used': evaluations,
        'sensitivity': [
            item.model_dump(mode='json') for item in sensitivity_final
        ],
        'correlation_groups': [
            item.model_dump(mode='json') for item in groups
        ],
        'identifiability_verdict': verdict,
    }
    digest = _digest(payload)
    return AcousticModelCalibrationResult(
        spec_id=spec.spec_id,
        spec_semantic_sha256=spec.semantic_sha256,
        fitted_values=fitted_values,
        calibrated_model_id=f'calibrated-model:{model_digest}',
        calibrated_model_sha256=model_digest,
        training_residual_norm=best_norm,
        training_sample_count=len(best_residuals),
        termination_state=termination,
        evaluations_used=evaluations,
        sensitivity=tuple(sensitivity_final),
        correlation_groups=tuple(groups),
        identifiability_verdict=verdict,
        result_id=f'model-calibration-result:{digest}',
        semantic_sha256=digest,
    )


class CalibratedModelFreeze(BaseModel):
    """Frozen calibrated-model authority consumed by independent holdout.

    Frozen before holdout; holdout data must never alter this configuration.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MODEL_CALIBRATION_SCHEMA_VERSION
    authority_version: Literal[
        'r180-calibrated-model-freeze-1'
    ] = MODEL_FREEZE_AUTHORITY_VERSION
    freeze_id: str = Field(pattern=r'^calibrated-model-freeze:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    calibration_result_id: str = Field(pattern=r'^model-calibration-result:[0-9a-f]{64}$')
    calibration_result_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    spec_id: str = Field(pattern=r'^model-calibration-spec:[0-9a-f]{64}$')
    calibrated_model_id: str = Field(pattern=r'^calibrated-model:[0-9a-f]{64}$')
    calibrated_model_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_id: str = Field(min_length=1)
    solver_version: str = Field(min_length=1)
    observable_contract: str = Field(min_length=1)
    normalization_policy_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    holdout_campaign_ref: ExactExternalAuthorityRef

    @model_validator(mode='after')
    def validate_freeze(self) -> 'CalibratedModelFreeze':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('calibrated model freeze semantic hash mismatch')
        if self.freeze_id != f'calibrated-model-freeze:{expected}':
            raise ValueError('calibrated model freeze id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'freeze_id', 'semantic_sha256'},
        )


def freeze_calibrated_model(
    result: AcousticModelCalibrationResult,
    spec: AcousticModelCalibrationSpec,
    *,
    normalization_policy_sha256: str,
) -> CalibratedModelFreeze:
    if result.spec_semantic_sha256 != spec.semantic_sha256:
        raise ValueError('freeze result does not belong to the supplied spec')
    payload = {
        'schema_version': MODEL_CALIBRATION_SCHEMA_VERSION,
        'authority_version': MODEL_FREEZE_AUTHORITY_VERSION,
        'calibration_result_id': result.result_id,
        'calibration_result_sha256': result.semantic_sha256,
        'spec_id': spec.spec_id,
        'calibrated_model_id': result.calibrated_model_id,
        'calibrated_model_sha256': result.calibrated_model_sha256,
        'solver_id': spec.solver_id,
        'solver_version': spec.solver_version,
        'observable_contract': spec.objective.observable,
        'normalization_policy_sha256': normalization_policy_sha256,
        'holdout_campaign_ref': spec.holdout_campaign_ref.model_dump(mode='json'),
    }
    digest = _digest(payload)
    return CalibratedModelFreeze(
        calibration_result_id=result.result_id,
        calibration_result_sha256=result.semantic_sha256,
        spec_id=spec.spec_id,
        calibrated_model_id=result.calibrated_model_id,
        calibrated_model_sha256=result.calibrated_model_sha256,
        solver_id=spec.solver_id,
        solver_version=spec.solver_version,
        observable_contract=spec.objective.observable,
        normalization_policy_sha256=normalization_policy_sha256,
        holdout_campaign_ref=spec.holdout_campaign_ref,
        freeze_id=f'calibrated-model-freeze:{digest}',
        semantic_sha256=digest,
    )


class HoldoutDisciplineRecord(BaseModel):
    """Records whether the consumed campaign was the preregistered holdout.

    If a campaign was already consumed for development/refit, it cannot be
    claimed as an independent holdout for a new production claim.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MODEL_CALIBRATION_SCHEMA_VERSION
    authority_version: Literal[
        'r180-holdout-discipline-1'
    ] = HOLDOUT_DISCIPLINE_AUTHORITY_VERSION
    record_id: str = Field(pattern=r'^holdout-discipline:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    freeze_id: str = Field(pattern=r'^calibrated-model-freeze:[0-9a-f]{64}$')
    freeze_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    consumed_campaign_ref: ExactExternalAuthorityRef
    verdict: Literal['independent_holdout', 'holdout_reused_for_development']
    reasons: tuple[str, ...]

    @model_validator(mode='after')
    def validate_record(self) -> 'HoldoutDisciplineRecord':
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError('holdout discipline reasons must be unique')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('holdout discipline semantic hash mismatch')
        if self.record_id != f'holdout-discipline:{expected}':
            raise ValueError('holdout discipline id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'record_id', 'semantic_sha256'},
        )


def evaluate_holdout_discipline(
    freeze: CalibratedModelFreeze,
    spec: AcousticModelCalibrationSpec,
    *,
    consumed_campaign_ref: ExactExternalAuthorityRef,
    previously_consumed_campaign_ids: Sequence[str],
) -> HoldoutDisciplineRecord:
    """Decide whether the consumed campaign is an independent holdout."""
    if freeze.holdout_campaign_ref != spec.holdout_campaign_ref:
        raise ValueError('freeze/spec holdout refs diverged')
    if freeze.spec_id != spec.spec_id:
        raise ValueError('freeze does not belong to the supplied spec')
    reasons: list[str] = []
    if consumed_campaign_ref != spec.holdout_campaign_ref:
        reasons.append(
            'consumed campaign is not the preregistered holdout reference'
        )
    if consumed_campaign_ref.authority_id in set(
        previously_consumed_campaign_ids
    ):
        reasons.append(
            'consumed campaign was already used for calibration/development'
        )
    verdict = 'independent_holdout' if not reasons else 'holdout_reused_for_development'
    payload = {
        'schema_version': MODEL_CALIBRATION_SCHEMA_VERSION,
        'authority_version': HOLDOUT_DISCIPLINE_AUTHORITY_VERSION,
        'freeze_id': freeze.freeze_id,
        'freeze_semantic_sha256': freeze.semantic_sha256,
        'consumed_campaign_ref': consumed_campaign_ref.model_dump(mode='json'),
        'verdict': verdict,
        'reasons': sorted(set(reasons)),
    }
    digest = _digest(payload)
    return HoldoutDisciplineRecord(
        freeze_id=freeze.freeze_id,
        freeze_semantic_sha256=freeze.semantic_sha256,
        consumed_campaign_ref=consumed_campaign_ref,
        verdict=verdict,
        reasons=tuple(sorted(set(reasons))),
        record_id=f'holdout-discipline:{digest}',
        semantic_sha256=digest,
    )
