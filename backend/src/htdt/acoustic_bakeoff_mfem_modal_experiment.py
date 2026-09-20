from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AttemptStatus = Literal['COMPLETED', 'FAILED', 'BLOCKED']
ExperimentOutcome = Literal['PASS', 'FAIL', 'BLOCKED']


def canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def semantic_hash(payload: object) -> str:
    return sha256(canonical_json(payload).encode('utf-8')).hexdigest()


class AuthorityBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    r100a_manifest_id: str = Field(min_length=1)
    r100a_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    fixture_id: Literal['wave-concave-l-room-v1']
    candidate_manifest_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: Literal['mfem-v4.10-d964264']
    candidate_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_source_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')


class SpatialSystemBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    h1_order: Literal[2]
    uniform_refinements: Literal[1]
    expected_elements: Literal[40]
    expected_ndofs: Literal[525]
    geometry: Literal['exact-five-hex-l-prism']
    boundary_model: Literal['natural-neumann-rigid']
    primary_field: Literal['velocity_potential_phi']
    governing_equation: Literal['M*phi_tt+Kc2*phi=c^2*b*q']
    mass_assembly: Literal['MFEM MassIntegrator']
    stiffness_assembly: Literal['MFEM DiffusionIntegrator(c^2)']
    source_functional: Literal['MFEM DomainLFIntegrator(DeltaCoefficient)']
    receiver_functional: Literal['MFEM DomainLFIntegrator(DeltaCoefficient)']


class ModalConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True)

    generalized_eigenproblem: Literal['Kc2*v=lambda*M*v']
    mass_normalization: Literal['V.T*M*V=I']
    basis_size: Literal[525]
    retained_eigenvalue_range: Literal['all finite generalized eigenpairs']
    truncation_rule: Literal['none; retain all 525 generalized eigenpairs']
    negative_eigenvalue_relative_tolerance: float
    negative_eigenvalue_rule: Literal[
        'block if lambda < -tol*max(1,max_abs_lambda); clamp remaining negative lambda to zero'
    ]
    source_projection: Literal['g=V.T*M*phi_t0']
    receiver_projection: Literal['r.T*V']
    initial_conditions: Literal['phi(0)=0; phi_t(0+)=c^2*dt*M^-1*b*q0']
    reconstruction: Literal[
        'p(t)=rho*sum_j((r.T*v_j)*g_j*cos(sqrt(lambda_j)*t))'
    ]
    numerical_precision: Literal['IEEE-754 float64']
    eigen_library: Literal['SciPy']
    eigen_library_version: Literal['1.14.1']
    numpy_version: Literal['2.1.3']
    generalized_eigh_driver: Literal['gvd']
    mass_orthonormality_max_abs_tolerance: float
    generalized_eigen_residual_relative_tolerance: float

    @model_validator(mode='after')
    def validate_fixed_numerics(self) -> 'ModalConfiguration':
        if self.negative_eigenvalue_relative_tolerance != 1e-10:
            raise ValueError('negative eigenvalue tolerance is frozen at 1e-10')
        if self.mass_orthonormality_max_abs_tolerance != 1e-10:
            raise ValueError('mass orthonormality tolerance is frozen at 1e-10')
        if self.generalized_eigen_residual_relative_tolerance != 1e-10:
            raise ValueError('generalized eigen residual tolerance is frozen at 1e-10')
        return self


class NumericalContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    quantity: Literal['finite_record_complex_pressure_transfer']
    unit: Literal['Pa/(m3/s)']
    source_normalization: Literal['volume_velocity_m3_s']
    coordinate_system: Literal['x_right_y_rear_z_up']
    interpolation: Literal['linear_complex']
    fourier_sign: Literal['exp(-i*omega*t)']
    finite_record_dtft_kernel: Literal['exp(+i*2*pi*f*n*dt)']
    record_interval: Literal['half_open_0_T']
    observation_time_s: Literal[2.0]
    frequency_start_hz: Literal[20.0]
    frequency_stop_hz: Literal[300.0]
    frequency_step_hz: Literal[1.0]
    magnitude_null_mask_below_db: Literal[-60.0]
    phase_null_mask_below_db: Literal[-40.0]
    magnitude_absolute_tolerance_db: Literal[0.75]
    magnitude_relative_tolerance: Literal[0.05]
    phase_tolerance_deg: Literal[8.0]


class ResourceCeiling(BaseModel):
    model_config = ConfigDict(frozen=True)

    cpu_thread_budget: Literal[4]
    ram_budget_mb: Literal[8192.0]
    disk_budget_mb: Literal[2048.0]
    max_solve_s_per_attempt: Literal[300.0]
    max_output_mb_per_attempt: Literal[512.0]
    subprocess_wall_timeout_s: Literal[330.0]


class AttemptSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt_id: str = Field(min_length=1)
    sample_rate_hz: int = Field(gt=0)


class PromotionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    require_all_attempts: Literal[True] = True
    best_trace_selection_forbidden: Literal[True] = True
    nonconverged_promotion_forbidden: Literal[True] = True
    production_promotion_forbidden: Literal[True] = True


class MfemModalExperimentPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-mfem-modal-experiment-plan-1']
    plan_id: str = Field(min_length=1)
    authority: AuthorityBinding
    spatial_system: SpatialSystemBinding
    modal_configuration: ModalConfiguration
    numerical_contract: NumericalContract
    resource_ceiling: ResourceCeiling
    attempts: tuple[AttemptSpec, ...]
    promotion: PromotionPolicy

    @model_validator(mode='after')
    def validate_exact_plan(self) -> 'MfemModalExperimentPlan':
        expected = (
            ('modal-6000', 6000),
            ('modal-9000', 9000),
            ('modal-12000', 12000),
        )
        actual = tuple((item.attempt_id, item.sample_rate_hz) for item in self.attempts)
        if actual != expected:
            raise ValueError(
                'modal experiment attempts are frozen to modal-6000/9000/12000 in order'
            )
        return self

    def spatial_system_configuration_hash(self) -> str:
        return semantic_hash(self.spatial_system.model_dump(mode='json'))

    def modal_configuration_hash(self) -> str:
        return semantic_hash(self.modal_configuration.model_dump(mode='json'))

    def output_grid_configuration_hash(self) -> str:
        return semantic_hash(
            [item.model_dump(mode='json') for item in self.attempts]
        )


class ExperimentAttemptResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt_id: str
    status: AttemptStatus
    reason_code: str
    reason: str
    numerical_identity_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    sample_rate_hz: int
    sample_count: int | None = None
    solve_s: float | None = None
    peak_ram_mb: float | None = None
    output_mb: float | None = None
    source_mass_relative_residual: float | None = None


class PairMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    coarse_attempt_id: str
    fine_attempt_id: str
    magnitude_max_abs_db: float
    magnitude_max_relative: float
    phase_max_error_deg: float
    complex_rms_relative: float
    magnitude_sample_count: int
    phase_sample_count: int


class ExperimentDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-mfem-modal-experiment-decision-1'] = (
        'r100b-mfem-modal-experiment-decision-1'
    )
    plan_id: str
    spatial_system_configuration_sha256: str
    modal_configuration_sha256: str
    output_grid_configuration_sha256: str
    outcome: ExperimentOutcome
    promoted_attempt_id: None = None
    attempt_results: tuple[ExperimentAttemptResult, ...]
    pair_metrics: tuple[PairMetrics, ...]
    complex_rms_relative_strictly_decreasing: bool | None
    final_pair_within_current_tolerance: bool | None
    violations: tuple[str, ...]
    conclusion_codes: tuple[str, ...]
    deterministic_report_identity_sha256: str


def load_experiment_plan(path: str | Path) -> MfemModalExperimentPlan:
    return MfemModalExperimentPlan.model_validate_json(
        Path(path).read_text(encoding='utf-8')
    )


def validate_exact_authority_binding(
    plan: MfemModalExperimentPlan,
    *,
    r100a_manifest_id: str,
    r100a_semantic_hash: str,
    candidate_manifest_hash: str,
    candidate_id: str,
    candidate_semantic_hash: str,
    candidate_source_commit_sha: str,
) -> None:
    expected = plan.authority
    checks = (
        ('r100a_manifest_id', r100a_manifest_id, expected.r100a_manifest_id),
        ('r100a_semantic_hash', r100a_semantic_hash, expected.r100a_semantic_hash),
        ('candidate_manifest_hash', candidate_manifest_hash, expected.candidate_manifest_hash),
        ('candidate_id', candidate_id, expected.candidate_id),
        ('candidate_semantic_hash', candidate_semantic_hash, expected.candidate_semantic_hash),
        (
            'candidate_source_commit_sha',
            candidate_source_commit_sha,
            expected.candidate_source_commit_sha,
        ),
    )
    for name, actual, wanted in checks:
        if actual != wanted:
            raise ValueError(f'{name} binding mismatch: {actual} != {wanted}')


def _pair_id(coarse: str, fine: str) -> str:
    return f'{coarse}->{fine}'


def evaluate_modal_experiment(
    plan: MfemModalExperimentPlan,
    *,
    attempt_results: tuple[ExperimentAttemptResult, ...],
    pair_metrics: tuple[PairMetrics, ...],
) -> ExperimentDecision:
    expected_ids = tuple(item.attempt_id for item in plan.attempts)
    result_by_id = {item.attempt_id: item for item in attempt_results}
    if len(result_by_id) != len(attempt_results) or set(result_by_id) != set(expected_ids):
        raise ValueError('attempt results must preserve every predeclared modal attempt exactly')
    ordered_attempts = tuple(result_by_id[item_id] for item_id in expected_ids)
    for spec, result in zip(plan.attempts, ordered_attempts):
        if result.sample_rate_hz != spec.sample_rate_hz:
            raise ValueError('attempt result sample rate differs from predeclared output grid')

    pair_by_id: dict[str, PairMetrics] = {}
    for item in pair_metrics:
        pair_id = _pair_id(item.coarse_attempt_id, item.fine_attempt_id)
        if pair_id in pair_by_id:
            raise ValueError(f'duplicate pair metrics: {pair_id}')
        pair_by_id[pair_id] = item
    required_pair_ids = (
        _pair_id(expected_ids[0], expected_ids[1]),
        _pair_id(expected_ids[1], expected_ids[2]),
    )

    incomplete = [item for item in ordered_attempts if item.status != 'COMPLETED']
    rms_decreasing: bool | None = None
    final_within: bool | None = None
    violations: list[str] = []

    if incomplete:
        outcome: ExperimentOutcome = 'BLOCKED'
        conclusion_codes = tuple(
            sorted({f'attempt_{item.status.lower()}:{item.reason_code}' for item in incomplete})
        )
    else:
        missing = [pair_id for pair_id in required_pair_ids if pair_id not in pair_by_id]
        if missing:
            outcome = 'BLOCKED'
            violations.extend(f'missing_pair:{pair_id}' for pair_id in missing)
            conclusion_codes = ('modal_pair_comparison_blocked',)
        else:
            first = pair_by_id[required_pair_ids[0]]
            final = pair_by_id[required_pair_ids[1]]
            rms_decreasing = first.complex_rms_relative > final.complex_rms_relative
            contract = plan.numerical_contract
            final_within = (
                final.magnitude_max_abs_db <= contract.magnitude_absolute_tolerance_db
                and final.magnitude_max_relative <= contract.magnitude_relative_tolerance
                and final.phase_max_error_deg <= contract.phase_tolerance_deg
            )
            if not rms_decreasing:
                violations.append('complex_rms_relative_not_strictly_decreasing')
            if final.magnitude_max_abs_db > contract.magnitude_absolute_tolerance_db:
                violations.append('magnitude_absolute_tolerance_exceeded')
            if final.magnitude_max_relative > contract.magnitude_relative_tolerance:
                violations.append('magnitude_relative_tolerance_exceeded')
            if final.phase_max_error_deg > contract.phase_tolerance_deg:
                violations.append('phase_tolerance_exceeded')
            if violations:
                outcome = 'FAIL'
                conclusion_codes = (
                    'modal_time_track_nonconverged',
                    'newmark_only_not_sufficient_explanation',
                )
            else:
                outcome = 'PASS'
                conclusion_codes = (
                    'modal_time_track_converged_current_r100a_tolerance',
                    'newmark_temporal_evolution_isolated_as_major_candidate_cause',
                )

    ordered_pairs = tuple(
        pair_by_id[pair_id] for pair_id in required_pair_ids if pair_id in pair_by_id
    )
    identity_payload = {
        'schema_version': 'r100b-mfem-modal-experiment-decision-1',
        'plan_id': plan.plan_id,
        'spatial_system_configuration_sha256': plan.spatial_system_configuration_hash(),
        'modal_configuration_sha256': plan.modal_configuration_hash(),
        'output_grid_configuration_sha256': plan.output_grid_configuration_hash(),
        'outcome': outcome,
        'promoted_attempt_id': None,
        'attempts': [
            {
                'attempt_id': item.attempt_id,
                'status': item.status,
                'reason_code': item.reason_code,
                'numerical_identity_sha256': item.numerical_identity_sha256,
                'sample_rate_hz': item.sample_rate_hz,
            }
            for item in ordered_attempts
        ],
        'pair_metrics': [item.model_dump(mode='json') for item in ordered_pairs],
        'complex_rms_relative_strictly_decreasing': rms_decreasing,
        'final_pair_within_current_tolerance': final_within,
        'violations': violations,
        'conclusion_codes': list(conclusion_codes),
    }
    return ExperimentDecision(
        plan_id=plan.plan_id,
        spatial_system_configuration_sha256=plan.spatial_system_configuration_hash(),
        modal_configuration_sha256=plan.modal_configuration_hash(),
        output_grid_configuration_sha256=plan.output_grid_configuration_hash(),
        outcome=outcome,
        attempt_results=ordered_attempts,
        pair_metrics=ordered_pairs,
        complex_rms_relative_strictly_decreasing=rms_decreasing,
        final_pair_within_current_tolerance=final_within,
        violations=tuple(violations),
        conclusion_codes=tuple(conclusion_codes),
        deterministic_report_identity_sha256=semantic_hash(identity_payload),
    )
