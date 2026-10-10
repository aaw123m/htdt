from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_benchmark import (
    canonical_benchmark_json as canonical_json,
    canonical_benchmark_sha256 as semantic_hash,
)


AttemptStatus = Literal['COMPLETED', 'FAILED', 'BLOCKED']
GateStatus = Literal['PASS', 'FAIL', 'BLOCKED']
ExperimentOutcome = Literal['PASS', 'FAIL', 'BLOCKED']


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


class IntegratorConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True)

    algorithm_id: Literal['gauss-legendre-2stage-pade22-linear']
    algorithm_version: Literal['1']
    order: Literal[4]
    substeps_per_output_interval: Literal[1, 2, 4] = 1
    substep_policy: Literal[
        'fixed equal GL2 substeps per output interval; no adaptive stepping'
    ] = 'fixed equal GL2 substeps per output interval; no adaptive stepping'
    propagation_form: Literal[
        'Pade[2/2] of first-order Hamiltonian generator; algebraically equal to 2-stage Gauss-Legendre RK for linear autonomous systems'
    ]
    dissipation_model: Literal['none; symmetric/symplectic rational propagator']
    candidate_matrix_policy: Literal['sparse CSR/CSC only; no dense inverse; no eigendecomposition']
    step_factorization: Literal[
        'factor sparse 2N block denominator once per sample-rate attempt and reuse every homogeneous step'
    ]
    mass_factorization: Literal['factor sparse M once and reuse for all source-kick solves']
    linear_solver: Literal['SuperLU via scipy.sparse.linalg.splu']
    permutation: Literal['COLAMD']
    diagonal_pivot_threshold: Literal[1.0]
    equilibration: Literal[True]
    iterative_refinement: Literal['DOUBLE']
    numerical_precision: Literal['IEEE-754 float64']
    scipy_version: Literal['1.14.1']
    numpy_version: Literal['2.1.3']
    residual_relative_tolerance: float
    residual_check_interval_steps: int

    @model_validator(mode='after')
    def validate_fixed_numerics(self) -> 'IntegratorConfiguration':
        if self.residual_relative_tolerance != 1e-10:
            raise ValueError('transient residual tolerance is frozen at 1e-10')
        if self.residual_check_interval_steps != 256:
            raise ValueError('residual check interval is frozen at 256 steps')
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


class ModalReferenceBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference_kind: Literal['diagnostic_full_basis_modal_pr277']
    pull_request: Literal[277]
    evidence_path: Literal['benchmarks/acoustics/evidence/r100b_mfem_modal_2026-09-20.json']
    modal_plan_path: Literal['benchmarks/acoustics/r100b_mfem_modal_experiment_plan.json']
    modal_plan_file_sha256: Literal[
        '15c1faf1e24ef99910912666d476ba784b7724edcb58517e3f62c40415e624de'
    ]
    expected_system_numeric_identity_sha256: Literal[
        'f48eb9a7fc5881fd8d2f26b32fc1f20df0f71290ecc4ed0337087073555cc61d'
    ]
    expected_basis_size: Literal[525]
    production_execution_candidate: Literal[False]
    candidate_must_not_require_reference: Literal[True]


class AttemptSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt_id: str = Field(min_length=1)
    sample_rate_hz: int = Field(gt=0)


class PromotionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    require_all_attempts: Literal[True] = True
    best_trace_selection_forbidden: Literal[True] = True
    nonconverged_promotion_forbidden: Literal[True] = True
    production_adoption_automatic_go_forbidden: Literal[True] = True


class MfemTransientExperimentPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[
        'r100b-mfem-transient-experiment-plan-1',
        'r100b-mfem-transient-experiment-plan-2',
        'r100b-mfem-transient-experiment-plan-3',
    ]
    plan_id: str = Field(min_length=1)
    authority: AuthorityBinding
    spatial_system: SpatialSystemBinding
    integrator: IntegratorConfiguration
    numerical_contract: NumericalContract
    resource_ceiling: ResourceCeiling
    modal_reference: ModalReferenceBinding
    attempts: tuple[AttemptSpec, ...]
    promotion: PromotionPolicy

    @model_validator(mode='after')
    def validate_exact_plan(self) -> 'MfemTransientExperimentPlan':
        expected = (
            ('transient-6000', 6000),
            ('transient-9000', 9000),
            ('transient-12000', 12000),
        )
        actual = tuple((item.attempt_id, item.sample_rate_hz) for item in self.attempts)
        if actual != expected:
            raise ValueError(
                'transient experiment attempts are frozen to transient-6000/9000/12000 in order'
            )
        expected_substeps = {
            'r100b-mfem-transient-experiment-plan-1': 1,
            'r100b-mfem-transient-experiment-plan-2': 2,
            'r100b-mfem-transient-experiment-plan-3': 4,
        }[self.schema_version]
        if self.integrator.substeps_per_output_interval != expected_substeps:
            raise ValueError(
                f'{self.schema_version} requires exactly {expected_substeps} '
                'GL2 substep(s) per output interval'
            )
        return self

    def spatial_system_configuration_hash(self) -> str:
        return semantic_hash(self.spatial_system.model_dump(mode='json'))

    def integrator_configuration_hash(self) -> str:
        return semantic_hash(self.integrator.model_dump(mode='json'))

    def output_grid_configuration_hash(self) -> str:
        return semantic_hash([item.model_dump(mode='json') for item in self.attempts])


class ExperimentAttemptResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt_id: str
    status: AttemptStatus
    reason_code: str
    reason: str
    sample_rate_hz: int
    output_interval_s: float | None = None
    substeps_per_output_interval: int | None = None
    internal_step_s: float | None = None
    internal_step_count: int | None = None
    numerical_identity_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    pressure_record_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    sample_count: int | None = None
    solve_s: float | None = None
    factorization_s: float | None = None
    peak_ram_mb: float | None = None
    output_mb: float | None = None
    mass_solve_relative_residual: float | None = None
    max_checked_step_relative_residual: float | None = None
    checked_step_residual_count: int | None = None
    denominator_nnz: int | None = None
    lu_nnz: int | None = None
    factor_storage_mb: float | None = None
    factorization_identity_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    sparse_candidate_path: bool = True
    dense_eigendecomposition_used: bool = False
    factorization_reused: bool = True


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


class ModalReferenceMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt_id: str
    modal_attempt_id: str
    sample_rate_hz: int
    magnitude_max_abs_db: float
    magnitude_max_relative: float
    phase_max_error_deg: float
    complex_rms_relative: float
    magnitude_sample_count: int
    phase_sample_count: int


class ExperimentDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-mfem-transient-experiment-decision-1'] = (
        'r100b-mfem-transient-experiment-decision-1'
    )
    plan_id: str
    spatial_system_configuration_sha256: str
    integrator_configuration_sha256: str
    output_grid_configuration_sha256: str
    outcome: ExperimentOutcome
    numerical_convergence_status: GateStatus
    modal_reference_agreement_status: GateStatus
    current_r100a_tolerance_status: GateStatus
    production_suitability_status: GateStatus
    production_adoption_decision: Literal['NO_GO']
    attempt_results: tuple[ExperimentAttemptResult, ...]
    pair_metrics: tuple[PairMetrics, ...]
    modal_reference_metrics: tuple[ModalReferenceMetrics, ...]
    violations: tuple[str, ...]
    conclusion_codes: tuple[str, ...]
    deterministic_report_identity_sha256: str


def load_experiment_plan(path: str | Path) -> MfemTransientExperimentPlan:
    return MfemTransientExperimentPlan.model_validate_json(
        Path(path).read_text(encoding='utf-8')
    )


def validate_exact_authority_binding(
    plan: MfemTransientExperimentPlan,
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
        ('candidate_source_commit_sha', candidate_source_commit_sha, expected.candidate_source_commit_sha),
    )
    for name, actual, wanted in checks:
        if actual != wanted:
            raise ValueError(f'{name} binding mismatch: {actual} != {wanted}')


def validate_modal_reference_system_identity(
    plan: MfemTransientExperimentPlan,
    actual_system_numeric_identity_sha256: str,
) -> None:
    expected = plan.modal_reference.expected_system_numeric_identity_sha256
    if actual_system_numeric_identity_sha256 != expected:
        raise ValueError(
            'modal reference semidiscrete system mismatch: '
            f'{actual_system_numeric_identity_sha256} != {expected}'
        )


def _within_tolerance(plan: MfemTransientExperimentPlan, item) -> bool:
    contract = plan.numerical_contract
    return (
        item.magnitude_max_abs_db <= contract.magnitude_absolute_tolerance_db
        and item.magnitude_max_relative <= contract.magnitude_relative_tolerance
        and item.phase_max_error_deg <= contract.phase_tolerance_deg
    )


def evaluate_transient_experiment(
    plan: MfemTransientExperimentPlan,
    *,
    attempt_results: tuple[ExperimentAttemptResult, ...],
    pair_metrics: tuple[PairMetrics, ...],
    modal_reference_metrics: tuple[ModalReferenceMetrics, ...],
) -> ExperimentDecision:
    expected_ids = tuple(item.attempt_id for item in plan.attempts)
    result_by_id = {item.attempt_id: item for item in attempt_results}
    if len(result_by_id) != len(attempt_results) or set(result_by_id) != set(expected_ids):
        raise ValueError('attempt results must preserve every predeclared transient attempt exactly')
    ordered_attempts = tuple(result_by_id[item_id] for item_id in expected_ids)
    for spec, result in zip(plan.attempts, ordered_attempts):
        if result.sample_rate_hz != spec.sample_rate_hz:
            raise ValueError('attempt sample rate differs from predeclared output grid')
        if result.status == 'COMPLETED':
            expected_output_interval_s = 1.0 / float(spec.sample_rate_hz)
            expected_substeps = plan.integrator.substeps_per_output_interval
            expected_internal_step_s = expected_output_interval_s / expected_substeps
            expected_internal_steps = (int(result.sample_count or 0) - 1) * expected_substeps
            if result.output_interval_s != expected_output_interval_s:
                raise ValueError('attempt output interval differs from predeclared output grid')
            if result.substeps_per_output_interval != expected_substeps:
                raise ValueError('attempt substep count differs from predeclared integrator authority')
            if result.internal_step_s != expected_internal_step_s:
                raise ValueError('attempt internal step differs from exact output/substep construction')
            if result.internal_step_count != expected_internal_steps:
                raise ValueError('attempt internal step count differs from exact substep construction')

    violations: list[str] = []
    incomplete = [item for item in ordered_attempts if item.status != 'COMPLETED']

    if incomplete:
        numerical_status: GateStatus = 'BLOCKED'
        modal_status: GateStatus = 'BLOCKED'
        tolerance_status: GateStatus = 'BLOCKED'
        production_status: GateStatus = 'BLOCKED'
        outcome: ExperimentOutcome = 'BLOCKED'
        conclusion_codes = tuple(
            sorted({f'attempt_{item.status.lower()}:{item.reason_code}' for item in incomplete})
        )
    else:
        required_pairs = (
            (expected_ids[0], expected_ids[1]),
            (expected_ids[1], expected_ids[2]),
        )
        pair_by_key = {(item.coarse_attempt_id, item.fine_attempt_id): item for item in pair_metrics}
        missing_pairs = [pair for pair in required_pairs if pair not in pair_by_key]

        modal_by_id = {item.attempt_id: item for item in modal_reference_metrics}
        missing_modal = [item_id for item_id in expected_ids if item_id not in modal_by_id]

        if missing_pairs:
            violations.extend(f'missing_pair:{a}->{b}' for a, b in missing_pairs)
        if missing_modal:
            violations.extend(f'missing_modal_reference:{item_id}' for item_id in missing_modal)

        if missing_pairs:
            numerical_status = 'BLOCKED'
            tolerance_status = 'BLOCKED'
        else:
            first = pair_by_key[required_pairs[0]]
            final = pair_by_key[required_pairs[1]]
            numerical_status = (
                'PASS'
                if first.complex_rms_relative > final.complex_rms_relative
                else 'FAIL'
            )
            if numerical_status == 'FAIL':
                violations.append('complex_rms_relative_not_strictly_decreasing')
            tolerance_status = 'PASS' if _within_tolerance(plan, final) else 'FAIL'
            if tolerance_status == 'FAIL':
                if final.magnitude_max_abs_db > plan.numerical_contract.magnitude_absolute_tolerance_db:
                    violations.append('magnitude_absolute_tolerance_exceeded')
                if final.magnitude_max_relative > plan.numerical_contract.magnitude_relative_tolerance:
                    violations.append('magnitude_relative_tolerance_exceeded')
                if final.phase_max_error_deg > plan.numerical_contract.phase_tolerance_deg:
                    violations.append('phase_tolerance_exceeded')

        if missing_modal:
            modal_status = 'BLOCKED'
        else:
            modal_status = (
                'PASS'
                if all(_within_tolerance(plan, modal_by_id[item_id]) for item_id in expected_ids)
                else 'FAIL'
            )
            if modal_status == 'FAIL':
                violations.append('modal_reference_agreement_tolerance_exceeded')

        production_ok = all(
            item.sparse_candidate_path
            and not item.dense_eigendecomposition_used
            and item.factorization_reused
            and item.factorization_identity_sha256 is not None
            and item.max_checked_step_relative_residual is not None
            and item.max_checked_step_relative_residual <= plan.integrator.residual_relative_tolerance
            for item in ordered_attempts
        )
        production_status = 'PASS' if production_ok else 'FAIL'
        if not production_ok:
            violations.append('production_transient_execution_contract_not_met')

        statuses = (
            numerical_status,
            modal_status,
            tolerance_status,
            production_status,
        )
        outcome = 'BLOCKED' if 'BLOCKED' in statuses else ('PASS' if all(x == 'PASS' for x in statuses) else 'FAIL')
        if outcome == 'PASS':
            conclusion_codes = (
                'low_dispersion_transient_track_passed_current_r100a_tolerance',
                'transient_agrees_with_diagnostic_modal_reference',
                'limited_production_suitability_passed',
                'candidate_wide_production_adoption_remains_no_go',
            )
        else:
            conclusion_codes = (
                'transient_candidate_not_qualified_by_predeclared_gates',
                'candidate_wide_production_adoption_remains_no_go',
            )

    identity_payload = {
        'plan_id': plan.plan_id,
        'spatial_system_configuration_sha256': plan.spatial_system_configuration_hash(),
        'integrator_configuration_sha256': plan.integrator_configuration_hash(),
        'output_grid_configuration_sha256': plan.output_grid_configuration_hash(),
        'outcome': outcome,
        'numerical_convergence_status': numerical_status,
        'modal_reference_agreement_status': modal_status,
        'current_r100a_tolerance_status': tolerance_status,
        'production_suitability_status': production_status,
        'production_adoption_decision': 'NO_GO',
        'attempt_results': [item.model_dump(mode='json') for item in ordered_attempts],
        'pair_metrics': [item.model_dump(mode='json') for item in pair_metrics],
        'modal_reference_metrics': [
            item.model_dump(mode='json') for item in modal_reference_metrics
        ],
        'violations': sorted(set(violations)),
        'conclusion_codes': list(conclusion_codes),
    }

    return ExperimentDecision(
        plan_id=plan.plan_id,
        spatial_system_configuration_sha256=plan.spatial_system_configuration_hash(),
        integrator_configuration_sha256=plan.integrator_configuration_hash(),
        output_grid_configuration_sha256=plan.output_grid_configuration_hash(),
        outcome=outcome,
        numerical_convergence_status=numerical_status,
        modal_reference_agreement_status=modal_status,
        current_r100a_tolerance_status=tolerance_status,
        production_suitability_status=production_status,
        production_adoption_decision='NO_GO',
        attempt_results=ordered_attempts,
        pair_metrics=pair_metrics,
        modal_reference_metrics=modal_reference_metrics,
        violations=tuple(sorted(set(violations))),
        conclusion_codes=tuple(conclusion_codes),
        deterministic_report_identity_sha256=semantic_hash(identity_payload),
    )
