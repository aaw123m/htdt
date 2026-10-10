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

from math import ceil, exp, floor, isfinite, log, log2, sqrt
from typing import TYPE_CHECKING, Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...cad_equipment import FrequencyDomain
from ...r120_geometry_compiler import ExactExternalAuthorityRef
from ...canonical_json import canonical_json as _canonical, canonical_sha256 as _digest, canonicalize_payload

if TYPE_CHECKING:
    from ...cad_acoustic_snapshot import AcousticSceneSnapshot


MODEL_CALIBRATION_SCHEMA_VERSION = 1
MODEL_CALIBRATION_SPEC_AUTHORITY_VERSION = 'r180-model-calibration-spec-1'
MODEL_CALIBRATION_RESULT_AUTHORITY_VERSION = 'r180-model-calibration-result-1'
MODEL_FREEZE_AUTHORITY_VERSION = 'r180-calibrated-model-freeze-1'
HOLDOUT_DISCIPLINE_AUTHORITY_VERSION = 'r180-holdout-discipline-1'

# Forward/inverse parameter transforms (pinned convention; replay depends on
# this name plus optimizer id, not on call-site context):
#   identity: z = x,            x = z
#   log:      z = ln(x),        x = exp(z)          (natural log, x > 0)
#   logit01:  z = ln(x/(1-x)),  x = sigmoid(z)      (natural log-odds, 0 < x < 1)
PARAMETER_TRANSFORM_CONVENTION = 'r180-parameter-transform-natural-log-v1'

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
            if self.transform == 'log' and self.lower_bound <= 0.0:
                raise ValueError(
                    'log transform requires positive parameter bounds'
                )
            if self.transform == 'logit01' and not (
                0.0 < self.lower_bound < self.upper_bound < 1.0
            ):
                raise ValueError(
                    'logit01 transform requires bounds inside (0, 1)'
                )
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
    # Grid generation convention: uniform steps in each parameter's declared
    # transform coordinate, then inverse-transformed to physical values.
    transform_convention: Literal[
        'r180-parameter-transform-natural-log-v1'
    ] = PARAMETER_TRANSFORM_CONVENTION
    #: Search-grid budget only — diagnostic/sensitivity evaluator calls are
    #: accounted separately on the result and do not consume this cap.
    max_evaluations: int = Field(gt=0)
    #: Reserved for future stochastic optimizers. deterministic_grid_search_v1
    #: never consumes it; it is excluded from the spec's semantic identity so
    #: two specs differing only in seed hash identically (#1034).
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
            exclude={
                'spec_id': True,
                'semantic_sha256': True,
                # ``deterministic_seed`` is a reserved no-op for the v1
                # deterministic grid optimizer, so it is deliberately excluded
                # from the preregistered semantic identity (#1034).
                'optimizer': {'deterministic_seed'},
            },
        )

    @property
    def fitted_parameters(self) -> tuple[CalibrationParameterDefinition, ...]:
        return tuple(item for item in self.parameters if item.role == 'fitted')


def build_model_calibration_spec(**kwargs: Any) -> AcousticModelCalibrationSpec:
    probe = AcousticModelCalibrationSpec.model_construct(**canonicalize_payload(AcousticModelCalibrationSpec, dict(
        spec_id='model-calibration-spec:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )))
    digest = _digest(probe.semantic_payload())
    return AcousticModelCalibrationSpec(
        spec_id=f'model-calibration-spec:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


class CalibrationResidualEvaluation(BaseModel):
    """Typed residual basis returned by a detailed evaluator (#1035).

    ``residuals`` are UNWEIGHTED canonical residuals — the engine applies the
    preregistered objective weighting itself. ``sample_ids`` / ``frequency_hz``
    give each residual a verifiable identity/coordinate; ``applied_weights``
    is the energy weighting the evaluator claims to have applied — the engine
    verifies it against the preregistered objective rather than trusting it.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    residuals: tuple[float, ...] = Field(min_length=1)
    sample_ids: tuple[str, ...] | None = None
    frequency_hz: tuple[float, ...] | None = None
    applied_weights: tuple[float, ...] | None = None

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'CalibrationResidualEvaluation':
        count = len(self.residuals)
        if any(not isfinite(float(value)) for value in self.residuals):
            raise ValueError('residual values must be finite')
        for name in ('sample_ids', 'frequency_hz', 'applied_weights'):
            value = getattr(self, name)
            if value is not None and len(value) != count:
                raise ValueError(f'{name} must match the residual basis length')
        if self.sample_ids is not None:
            if any(not str(item) for item in self.sample_ids):
                raise ValueError('sample ids must be non-empty')
            if len(set(self.sample_ids)) != count:
                raise ValueError('sample ids must be unique')
        if self.frequency_hz is not None and any(
            not isfinite(float(f)) or float(f) <= 0.0
            for f in self.frequency_hz
        ):
            raise ValueError('residual frequencies must be positive and finite')
        if self.applied_weights is not None and any(
            not isfinite(float(w)) or float(w) < 0.0
            for w in self.applied_weights
        ):
            raise ValueError('applied weights must be nonnegative and finite')
        return self


class CalibrationEvaluationPort(Protocol):
    """Deterministic objective evaluator the bounded optimizer drives.

    ``evaluate`` returns the per-sample residual vector in the declared
    observable/band over an anonymous basis; the engine requires every call —
    search and sensitivity probes alike — to share one basis length.

    Implementations may additionally provide ``evaluate_detailed`` returning a
    :class:`CalibrationResidualEvaluation` (or an equivalent mapping) with
    typed sample identity, frequency coordinates and claimed energy weights;
    the engine then verifies domain membership, a stable ordered sample basis
    and the preregistered weighting exactly.
    """

    def evaluate(
        self,
        parameter_values: dict[str, float],
    ) -> tuple[float, ...]:
        ...


#: 'converged' is intentionally absent: the deterministic v1 grid search
#: declares no convergence criterion, so a run either evaluated its whole
#: planned grid ('grid_complete') or stopped at the search budget
#: ('budget_truncated'). (#1034)
TerminationState = Literal['grid_complete', 'budget_truncated']

#: ``max_evaluations`` bounds the search grid only; post-fit sensitivity /
#: diagnostic evaluator calls are counted separately and never silently
#: bypass resource accounting. (#1034)
EVALUATION_BUDGET_SCOPE = 'search_grid'

OBJECTIVE_WEIGHTING_ALGORITHMS = {
    'uniform': 'uniform-v1',
    'per_octave': 'per-octave-v1',
}


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
    #: Total evaluator invocations — search grid plus every diagnostic /
    #: sensitivity probe; never just the search loop count. (#1034)
    evaluations_used: int = Field(gt=0)
    #: Which calls ``max_evaluations`` caps: the deterministic search grid
    #: only. Diagnostics are reported, never silently uncapped. (#1034)
    budget_scope: Literal['search_grid'] = EVALUATION_BUDGET_SCOPE
    planned_grid_points: int = Field(ge=1)
    search_evaluations: int = Field(ge=1)
    #: Planned grid points never evaluated because the budget cut the search.
    skipped_grid_points: int = Field(ge=0)
    #: Post-fit sensitivity/diagnostic evaluator invocations (cache misses
    #: only — reused grid residuals are not recomputed). (#1034)
    diagnostic_evaluations: int = Field(ge=0)
    #: Provenance of the residual basis the objective actually used. (#1035)
    residual_basis_kind: Literal['declared', 'anonymous']
    residual_basis_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    weighting_applied: str = Field(min_length=1)
    # Whether the sensitivity derivative is d(residual)/d(physical x) or
    # d(residual)/d(transformed z). Search coordinates are transformed;
    # reported sensitivity is the physical-parameter derivative.
    sensitivity_parameterization: Literal[
        'physical_parameter', 'transformed_coordinate'
    ]
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
        if self.evaluations_used != (
            self.search_evaluations + self.diagnostic_evaluations
        ):
            raise ValueError(
                'evaluations_used must equal search + diagnostic calls'
            )
        if self.skipped_grid_points != (
            self.planned_grid_points - self.search_evaluations
        ):
            raise ValueError(
                'skipped grid points must equal planned minus evaluated points'
            )
        if (
            self.residual_basis_kind == 'declared'
            and self.residual_basis_sha256 is None
        ):
            raise ValueError('a declared residual basis requires its hash')
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


# #948: legal fitted quantities per target kind. Anything outside the
# declared contract fails closed during target resolution.
CALIBRATION_TARGET_QUANTITIES: dict[str, frozenset[str]] = {
    'boundary_surface_material': frozenset(
        {'absorption', 'scattering', 'impedance'}
    ),
    'source_strength': frozenset(
        {'drive_gain', 'level_db', 'rms_pressure_pa'}
    ),
    'environment': frozenset({'sound_speed_m_s', 'temperature_c'}),
    'scattering_parameter': frozenset(
        {'scattering_coefficient', 'scattering_azimuth_spread'}
    ),
}


class ResolvedCalibrationTarget(BaseModel):
    """One parameter resolved against exact baseline authority (#948)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    parameter_id: str = Field(min_length=1)
    target_kind: ParameterTargetKind
    target_id: str = Field(min_length=1)
    quantity: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    resolved_identity: str = Field(min_length=1)
    resolved_authority_ref: ExactExternalAuthorityRef | None = None


def resolve_calibration_targets(
    spec: AcousticModelCalibrationSpec,
    snapshot: 'AcousticSceneSnapshot',
) -> tuple[ResolvedCalibrationTarget, ...]:
    """Resolve every spec parameter against the baseline snapshot.

    Fails closed on unresolved targets, unknown quantities, or targets whose
    snapshot authority is not exactly bound — a fitted value that cannot be
    applied to a real authority must never reach a solver.
    """

    if spec.baseline_snapshot_sha256 != snapshot.semantic_sha256:
        raise ValueError(
            'calibration spec does not pin this baseline snapshot'
        )
    surface_ids = {
        item.source_surface_id
        for item in snapshot.surface_boundary_configuration
    }
    surface_material = {
        item.source_surface_id: item.material_authority
        for item in snapshot.surface_boundary_configuration
    }
    surface_physics = {
        item.source_surface_id: item.boundary_physics_authority
        for item in snapshot.surface_boundary_configuration
    }
    source_ids = {
        item.source_entity_id for item in snapshot.sources
    }

    resolved: list[ResolvedCalibrationTarget] = []
    for parameter in spec.parameters:
        legal = CALIBRATION_TARGET_QUANTITIES[parameter.target_kind]
        if parameter.quantity not in legal:
            raise ValueError(
                f'calibration parameter {parameter.parameter_id} declares '
                f'illegal quantity {parameter.quantity!r} for '
                f'{parameter.target_kind}'
            )
        authority: ExactExternalAuthorityRef | None = None
        if parameter.target_kind == 'boundary_surface_material':
            if parameter.target_id not in surface_ids:
                raise ValueError(
                    f'calibration target surface {parameter.target_id} is '
                    'not bound in the baseline snapshot'
                )
            authority = surface_material[parameter.target_id]
            if authority is None:
                raise ValueError(
                    f'calibration target surface {parameter.target_id} has '
                    'no exact material authority to calibrate'
                )
            identity = f'surface:{parameter.target_id}'
        elif parameter.target_kind == 'scattering_parameter':
            if parameter.target_id not in surface_ids:
                raise ValueError(
                    f'calibration target surface {parameter.target_id} is '
                    'not bound in the baseline snapshot'
                )
            authority = surface_physics[parameter.target_id]
            if authority is None:
                raise ValueError(
                    f'calibration target surface {parameter.target_id} has '
                    'no exact boundary-physics authority to calibrate'
                )
            identity = f'surface:{parameter.target_id}'
        elif parameter.target_kind == 'source_strength':
            if parameter.target_id not in source_ids:
                raise ValueError(
                    f'calibration target source {parameter.target_id} is '
                    'not bound in the baseline snapshot'
                )
            identity = f'source:{parameter.target_id}'
        else:  # environment
            if parameter.target_id != parameter.quantity:
                raise ValueError(
                    'environment calibration parameters must target the '
                    'field they fit (target_id == quantity)'
                )
            environment = snapshot.environment
            if environment is None:
                raise ValueError(
                    'environment calibration requires a bound environment '
                    'authority on the baseline snapshot'
                )
            if parameter.quantity == 'sound_speed_m_s':
                authority = environment.sound_speed_source_authority
            else:
                authority = environment.temperature_source_authority
            if authority is None:
                raise ValueError(
                    f'environment field {parameter.quantity} has no exact '
                    'authority to calibrate'
                )
            identity = f'environment:{parameter.quantity}'
        resolved.append(
            ResolvedCalibrationTarget(
                parameter_id=parameter.parameter_id,
                target_kind=parameter.target_kind,
                target_id=parameter.target_id,
                quantity=parameter.quantity,
                unit=parameter.unit,
                resolved_identity=identity,
                resolved_authority_ref=authority,
            )
        )
    return tuple(resolved)


class CalibratedModelOverride(BaseModel):
    """One applied parameter value bound to its resolved authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    parameter_id: str = Field(min_length=1)
    target_kind: ParameterTargetKind
    target_id: str = Field(min_length=1)
    quantity: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    value: float
    resolved_identity: str = Field(min_length=1)
    resolved_authority_ref: ExactExternalAuthorityRef | None = None


class CalibratedAcousticModel(BaseModel):
    """Materialized calibrated acoustic configuration (#948).

    The replayable authority solvers consume: it pins the exact baseline
    snapshot, the calibration spec+result lineage, every resolved target,
    and the applied fitted/fixed values — the ``calibrated_model_sha256``
    therefore identifies a real calibrated model, not just a tuple of
    parameter values. Baseline authority is never mutated.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MODEL_CALIBRATION_SCHEMA_VERSION
    authority_version: Literal[
        'r180-calibrated-model-1'
    ] = 'r180-calibrated-model-1'
    materialized_model_id: str = Field(
        pattern=r'^materialized-calibrated-model:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    spec_id: str = Field(pattern=r'^model-calibration-spec:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    calibration_result_id: str = Field(
        pattern=r'^model-calibration-result:[0-9a-f]{64}$'
    )
    calibration_result_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    calibrated_model_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    baseline_snapshot_id: str = Field(min_length=1)
    baseline_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solver_id: str = Field(min_length=1)
    solver_version: str = Field(min_length=1)
    resolved_targets: tuple[ResolvedCalibrationTarget, ...] = Field(
        min_length=1
    )
    overrides: tuple[CalibratedModelOverride, ...] = Field(min_length=1)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'materialized_model_id', 'semantic_sha256'},
        )

    @model_validator(mode='after')
    def validate_model(self) -> 'CalibratedAcousticModel':
        target_ids = {item.parameter_id for item in self.resolved_targets}
        override_ids = {item.parameter_id for item in self.overrides}
        if target_ids != override_ids:
            raise ValueError(
                'calibrated overrides must cover every resolved target '
                'exactly once'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('materialized calibrated model hash mismatch')
        if (
            self.materialized_model_id
            != f'materialized-calibrated-model:{expected}'
        ):
            raise ValueError('materialized calibrated model id mismatch')
        return self

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.materialized_model_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def materialize_calibrated_model(
    spec: AcousticModelCalibrationSpec,
    result: AcousticModelCalibrationResult,
    snapshot: 'AcousticSceneSnapshot',
) -> CalibratedAcousticModel:
    """Apply fitted values to resolved targets, producing the model solvers execute."""

    if result.spec_semantic_sha256 != spec.semantic_sha256:
        raise ValueError('result does not belong to the supplied spec')
    resolved = resolve_calibration_targets(spec, snapshot)
    target_by_id = {item.parameter_id: item for item in resolved}
    applied = dict(result.fitted_values)
    for parameter in spec.parameters:
        if parameter.role == 'fixed':
            applied[parameter.parameter_id] = float(parameter.fixed_value)
    missing = set(target_by_id) - set(applied)
    if missing:
        raise ValueError(
            f'calibration result omits fitted values for {sorted(missing)}'
        )
    overrides = tuple(
        CalibratedModelOverride(
            parameter_id=parameter.parameter_id,
            target_kind=parameter.target_kind,
            target_id=parameter.target_id,
            quantity=parameter.quantity,
            unit=parameter.unit,
            value=applied[parameter.parameter_id],
            resolved_identity=target_by_id[
                parameter.parameter_id
            ].resolved_identity,
            resolved_authority_ref=target_by_id[
                parameter.parameter_id
            ].resolved_authority_ref,
        )
        for parameter in spec.parameters
    )
    core = {
        'schema_version': MODEL_CALIBRATION_SCHEMA_VERSION,
        'authority_version': 'r180-calibrated-model-1',
        'spec_id': spec.spec_id,
        'spec_semantic_sha256': spec.semantic_sha256,
        'calibration_result_id': result.result_id,
        'calibration_result_sha256': result.semantic_sha256,
        'calibrated_model_sha256': result.calibrated_model_sha256,
        'baseline_snapshot_id': snapshot.snapshot_id,
        'baseline_snapshot_sha256': snapshot.semantic_sha256,
        'solver_id': spec.solver_id,
        'solver_version': spec.solver_version,
        'resolved_targets': [
            item.model_dump(mode='json') for item in resolved
        ],
        'overrides': [item.model_dump(mode='json') for item in overrides],
    }
    digest = _digest(core)
    return CalibratedAcousticModel(
        materialized_model_id=f'materialized-calibrated-model:{digest}',
        semantic_sha256=digest,
        spec_id=spec.spec_id,
        spec_semantic_sha256=spec.semantic_sha256,
        calibration_result_id=result.result_id,
        calibration_result_sha256=result.semantic_sha256,
        calibrated_model_sha256=result.calibrated_model_sha256,
        baseline_snapshot_id=snapshot.snapshot_id,
        baseline_snapshot_sha256=snapshot.semantic_sha256,
        solver_id=spec.solver_id,
        solver_version=spec.solver_version,
        resolved_targets=resolved,
        overrides=overrides,
    )


def _forward_transform(
    parameter: CalibrationParameterDefinition,
    value: float,
) -> float:
    """Map a physical parameter value into its declared search coordinate."""
    if parameter.transform == 'identity':
        return value
    if parameter.transform == 'log':
        if value <= 0.0:
            raise ValueError('log transform requires positive parameter values')
        return log(value)
    # logit01 maps (0,1); bounds are validated inside the open interval.
    if not (0.0 < value < 1.0):
        raise ValueError('logit01 transform requires bounds inside (0, 1)')
    return log(value / (1.0 - value))


def _inverse_transform(
    parameter: CalibrationParameterDefinition,
    value: float,
) -> float:
    """Map a transformed search coordinate back to a physical value."""
    if parameter.transform == 'identity':
        return value
    if parameter.transform == 'log':
        return exp(value)
    # sigmoid; grid coordinates come from finite log-odds bounds, so the
    # argument never overflows exp in practice.
    return 1.0 / (1.0 + exp(-value))


def _grid_points(
    parameter: CalibrationParameterDefinition,
    per_parameter_count: int,
) -> tuple[float, ...]:
    """Uniform grid in the declared transform coordinate, in physical units.

    ``identity`` reduces to the physical linear grid; ``log`` and
    ``logit01`` distribute samples in ln(x) and log-odds space respectively
    and inverse-transform back to physical candidate values.
    """
    z_low = _forward_transform(parameter, float(parameter.lower_bound))
    z_high = _forward_transform(parameter, float(parameter.upper_bound))
    if per_parameter_count <= 1:
        return (_inverse_transform(parameter, 0.5 * (z_low + z_high)),)
    step = (z_high - z_low) / (per_parameter_count - 1)
    return tuple(
        _inverse_transform(parameter, z_low + index * step)
        for index in range(per_parameter_count)
    )


def _planned_grid_size(count: int, per_parameter_count: int) -> int:
    size = 1
    for _ in range(count):
        size *= per_parameter_count
    return size


def _objective_energy_weights(
    spec: AcousticModelCalibrationSpec,
    frequency_hz: Sequence[float] | None,
    sample_count: int,
) -> tuple[float, ...]:
    """Versioned per-sample energy weights for the preregistered objective.

    ``uniform-v1`` weights every residual sample equally.
    ``per-octave-v1`` divides each sample's energy weight by the number of
    samples in its octave band (octave index ``floor(log2(f / f_min))`` with
    ``f_min`` the declared domain minimum), so every octave contributes equal
    residual energy regardless of the sampling density inside it.
    """
    if spec.objective.weighting == 'uniform':
        return (1.0,) * sample_count
    if frequency_hz is None:
        raise ValueError(
            'per_octave weighting requires residual frequency coordinates; '
            'an evaluator returning a bare residual tuple cannot satisfy it'
        )
    reference = spec.objective.frequency_domain.minimum_hz
    octaves = [
        floor(log2(float(frequency) / reference)) for frequency in frequency_hz
    ]
    populations: dict[int, int] = {}
    for octave in octaves:
        populations[octave] = populations.get(octave, 0) + 1
    return tuple(1.0 / populations[octave] for octave in octaves)


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
    # The grid uses the largest per-parameter density whose full plan is
    # honest under the budget: when the planned product exceeds the budget the
    # run stops at the cap and reports 'budget_truncated' (#1034).
    per_parameter = max(
        1, int(ceil(spec.optimizer.max_evaluations ** (1.0 / count)))
    )
    grids = [_grid_points(parameter, per_parameter) for parameter in fitted]
    planned_grid_points = _planned_grid_size(count, per_parameter)

    base_values = {
        item.parameter_id: float(item.fixed_value)
        for item in spec.parameters
        if item.role == 'fixed'
    }

    # --- typed residual-basis verification (#1035) -------------------------
    # The evaluator's residual vector is the objective's canonical basis; it
    # must be stable (same ordered identity) for every call the engine makes,
    # search or sensitivity probe. A detailed evaluator declares its basis
    # (ordered sample ids / frequency coordinates); an anonymous one is still
    # bound to a fixed length.
    detailed_evaluator = getattr(evaluator, 'evaluate_detailed', None)
    declared_basis: tuple | None = None
    basis_kind: Literal['declared', 'anonymous'] = 'anonymous'
    basis_frequency_hz: tuple[float, ...] | None = None
    basis_sample_ids: tuple[str, ...] | None = None

    def _invoke_evaluator(
        values: dict[str, float],
    ) -> tuple[tuple[float, ...], tuple[float, ...]]:
        """Call the evaluator once; return (raw, objective-weighted) residuals."""
        nonlocal declared_basis, basis_kind
        nonlocal basis_frequency_hz, basis_sample_ids
        frequency_hz: tuple[float, ...] | None = None
        sample_ids: tuple[str, ...] | None = None
        applied_weights: tuple[float, ...] | None = None
        if detailed_evaluator is not None:
            detailed = detailed_evaluator(values)
            if not isinstance(detailed, CalibrationResidualEvaluation):
                detailed = CalibrationResidualEvaluation.model_validate(
                    detailed
                )
            basis_kind = 'declared'
            raw = tuple(float(v) for v in detailed.residuals)
            sample_ids = detailed.sample_ids
            frequency_hz = detailed.frequency_hz
            applied_weights = detailed.applied_weights
            if frequency_hz is not None and any(
                not spec.objective.frequency_domain.contains(f)
                for f in frequency_hz
            ):
                raise ValueError(
                    'residual sample frequency outside the declared '
                    'objective frequency domain'
                )
        else:
            raw = tuple(float(v) for v in evaluator.evaluate(values))
        if not raw:
            raise ValueError('calibration evaluator returned no residuals')
        basis = (
            'declared' if detailed_evaluator is not None else 'anonymous',
            len(raw),
            sample_ids,
            frequency_hz,
        )
        if declared_basis is None:
            declared_basis = basis
            basis_frequency_hz = frequency_hz
            basis_sample_ids = sample_ids
        elif basis != declared_basis:
            raise ValueError(
                'residual sample basis changed between evaluations; the '
                'engine refuses to compare across differing bases'
            )
        weights = _objective_energy_weights(
            spec, frequency_hz, len(raw)
        )
        if applied_weights is not None and (
            len(applied_weights) != len(weights)
            or any(
                abs(float(got) - float(expected)) > 1e-9
                for got, expected in zip(applied_weights, weights)
            )
        ):
            raise ValueError(
                'evaluator-applied weights do not match the preregistered '
                'objective weighting'
            )
        weighted = tuple(
            residual * sqrt(weight) for residual, weight in zip(raw, weights)
        )
        return raw, weighted

    # Residual cache keyed by the full parameter assignment: diagnostic probes
    # that coincide with evaluated grid points reuse their residuals instead
    # of issuing an untracked duplicate evaluator call (#1034).
    residual_cache: dict[
        tuple[tuple[str, float], ...],
        tuple[tuple[float, ...], tuple[float, ...]],
    ] = {}
    search_evaluations = 0
    diagnostic_evaluations = 0

    def _evaluate_point(
        values: dict[str, float],
        *,
        purpose: Literal['search', 'diagnostic'],
    ) -> tuple[tuple[float, ...], tuple[float, ...]]:
        nonlocal search_evaluations, diagnostic_evaluations
        key = tuple(sorted(
            (name, float(value)) for name, value in values.items()
        ))
        if key not in residual_cache:
            residual_cache[key] = _invoke_evaluator(values)
            if purpose == 'search':
                search_evaluations += 1
            else:
                diagnostic_evaluations += 1
        return residual_cache[key]

    best_norm: float | None = None
    best_values: dict[str, float] | None = None
    best_residuals: tuple[float, ...] | None = None
    evaluated_grid: list[tuple[dict[str, float], tuple[float, ...]]] = []

    def visit(prefix: dict[str, float], index: int) -> None:
        nonlocal best_norm, best_values, best_residuals
        if index == count:
            values = {**base_values, **prefix}
            _, weighted = _evaluate_point(values, purpose='search')
            evaluated_grid.append((dict(prefix), weighted))
            norm = sqrt(sum(r * r for r in weighted))
            if best_norm is None or norm < best_norm:
                best_norm = norm
                best_values = dict(prefix)
                best_residuals = weighted
            return
        parameter = fitted[index]
        for value in grids[index]:
            if search_evaluations >= spec.optimizer.max_evaluations:
                return
            visit({**prefix, parameter.parameter_id: value}, index + 1)

    visit({}, 0)
    if best_values is None or best_residuals is None or best_norm is None:
        raise ValueError('calibration produced no evaluations')

    termination: TerminationState = (
        'grid_complete'
        if search_evaluations >= planned_grid_points
        else 'budget_truncated'
    )
    skipped_grid_points = planned_grid_points - min(
        search_evaluations, planned_grid_points
    )

    # Local sensitivity: central finite difference of the residual vector
    # along each fitted parameter in *physical* units (the declared transform
    # only shapes the search grid; reported derivatives are d/dx, not d/dz),
    # using the grid step as the probe distance.
    # Probes that fall on evaluated grid points reuse cached residuals rather
    # than paying a second evaluator call (#1034); every actual call is
    # counted under diagnostic_evaluations and must satisfy the same residual
    # basis as the search (#1035).
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
        _, residuals_plus = _evaluate_point(
            {**base_values, **plus}, purpose='diagnostic'
        )
        _, residuals_minus = _evaluate_point(
            {**base_values, **minus}, purpose='diagnostic'
        )
        if len(residuals_plus) != len(residuals_minus):
            raise ValueError(
                'sensitivity residual vectors differ in length; refusing to '
                'truncate a difference over mismatched bases'
            )
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
    evaluations_used = search_evaluations + diagnostic_evaluations
    residual_basis_sha256: str | None = None
    if basis_kind == 'declared':
        residual_basis_sha256 = _digest(
            {
                'kind': 'calibration-residual-basis',
                'observable': spec.objective.observable,
                'frequency_domain': spec.objective.frequency_domain.model_dump(
                    mode='json'
                ),
                'weighting_applied': OBJECTIVE_WEIGHTING_ALGORITHMS[
                    spec.objective.weighting
                ],
                'sample_ids': list(basis_sample_ids)
                if basis_sample_ids is not None
                else None,
                'frequency_hz': list(basis_frequency_hz)
                if basis_frequency_hz is not None
                else None,
            }
        )
    weighting_applied = OBJECTIVE_WEIGHTING_ALGORITHMS[
        spec.objective.weighting
    ]

    result_kwargs = {
        'schema_version': MODEL_CALIBRATION_SCHEMA_VERSION,
        'authority_version': MODEL_CALIBRATION_RESULT_AUTHORITY_VERSION,
        'spec_id': spec.spec_id,
        'spec_semantic_sha256': spec.semantic_sha256,
        'fitted_values': fitted_values,
        'calibrated_model_id': f'calibrated-model:{model_digest}',
        'calibrated_model_sha256': model_digest,
        'training_residual_norm': best_norm,
        'training_sample_count': len(best_residuals),
        'termination_state': termination,
        'evaluations_used': evaluations_used,
        'budget_scope': EVALUATION_BUDGET_SCOPE,
        'planned_grid_points': planned_grid_points,
        'search_evaluations': search_evaluations,
        'skipped_grid_points': skipped_grid_points,
        'diagnostic_evaluations': diagnostic_evaluations,
        'residual_basis_kind': basis_kind,
        'residual_basis_sha256': residual_basis_sha256,
        'weighting_applied': weighting_applied,
        'sensitivity_parameterization': 'physical_parameter',
        'sensitivity': tuple(sensitivity_final),
        'correlation_groups': tuple(groups),
        'identifiability_verdict': verdict,
    }
    digest = _digest(
        AcousticModelCalibrationResult.model_construct(**canonicalize_payload(AcousticModelCalibrationResult, dict(
            **result_kwargs,
            result_id='model-calibration-result:' + '0' * 64,
            semantic_sha256='0' * 64,
        ))).semantic_payload()
    )
    return AcousticModelCalibrationResult(
        **result_kwargs,
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
    # #948: exact materialized calibrated-model authority this freeze binds;
    # serialized only when set so pre-materialization freezes digest as before.
    materialized_model_ref: ExactExternalAuthorityRef | None = None

    @model_validator(mode='after')
    def validate_freeze(self) -> 'CalibratedModelFreeze':
        if self.materialized_model_ref is not None:
            ref = self.materialized_model_ref
            if (
                not ref.authority_id.startswith(
                    'materialized-calibrated-model:'
                )
            ):
                raise ValueError(
                    'freeze materialized model must reference a '
                    'materialized-calibrated-model authority'
                )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('calibrated model freeze semantic hash mismatch')
        if self.freeze_id != f'calibrated-model-freeze:{expected}':
            raise ValueError('calibrated model freeze id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode='json',
            exclude={'freeze_id', 'semantic_sha256'},
        )
        if self.materialized_model_ref is None:
            payload.pop('materialized_model_ref', None)
        return payload


def freeze_calibrated_model(
    result: AcousticModelCalibrationResult,
    spec: AcousticModelCalibrationSpec,
    *,
    normalization_policy_sha256: str,
    materialized_model: CalibratedAcousticModel | None = None,
) -> CalibratedModelFreeze:
    if result.spec_semantic_sha256 != spec.semantic_sha256:
        raise ValueError('freeze result does not belong to the supplied spec')
    if materialized_model is not None:
        if materialized_model.calibration_result_sha256 != result.semantic_sha256:
            raise ValueError(
                'materialized model does not belong to this result'
            )
        if (
            materialized_model.calibrated_model_sha256
            != result.calibrated_model_sha256
        ):
            raise ValueError('materialized model hash mismatch')
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
    if materialized_model is not None:
        payload['materialized_model_ref'] = (
            materialized_model.authority_ref().model_dump(mode='json')
        )
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
        materialized_model_ref=(
            None
            if materialized_model is None
            else materialized_model.authority_ref()
        ),
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
