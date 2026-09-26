from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_benchmark import (
    canonical_benchmark_json as canonical_json,
    canonical_benchmark_sha256 as semantic_hash,
)


HashAlgorithm = Literal['sha256(canonical_benchmark_json)']
AttemptStatus = Literal['COMPLETED', 'FAILED', 'BLOCKED']
TrackStatus = Literal['PASS', 'FAIL', 'BLOCKED']
ExperimentOutcome = Literal['PASS', 'FAIL', 'BLOCKED']


class AuthorityBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    r100a_manifest_id: str = Field(min_length=1)
    r100a_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    fixture_id: str = Field(min_length=1)
    fixture_hash_algorithm: HashAlgorithm
    candidate_manifest_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    candidate_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_source_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')


class SolverConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True)

    backend: Literal['MFEM']
    backend_version: Literal['4.10.0']
    source_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    primary_field: Literal['velocity_potential_phi']
    governing_equation: Literal['M*phi_tt+c^2*K*phi=c^2*b*q']
    pressure_conversion: Literal['p=rho*d(phi)/dt']
    source_mapping: Literal['phi_t(0+)=c^2*dt*M^-1*b*q[0]']
    time_integrator: Literal['Newmark(beta=0.25,gamma=0.5)']
    algorithmic_damping: Literal['none']
    linear_solver: Literal['serial CG with diagonal Jacobi preconditioner']
    linear_relative_tolerance: float
    linear_max_iterations: int
    qualification_residual_limit: float


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
    observation_time_s: float
    frequency_start_hz: float
    frequency_stop_hz: float
    frequency_step_hz: float
    magnitude_null_mask_below_db: float
    phase_null_mask_below_db: float
    magnitude_absolute_tolerance_db: float
    magnitude_relative_tolerance: float
    phase_tolerance_deg: float


class ResourceCeiling(BaseModel):
    model_config = ConfigDict(frozen=True)

    cpu_thread_budget: int
    ram_budget_mb: float
    disk_budget_mb: float
    max_solve_s_per_attempt: float
    max_output_mb_per_attempt: float
    subprocess_wall_timeout_s: float


class AttemptSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt_id: str = Field(min_length=1)
    order: int = Field(ge=1, le=8)
    uniform_refinements: int = Field(ge=0, le=3)
    sample_rate_hz: int = Field(gt=0)


class PromotionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    require_all_attempts: Literal[True] = True
    require_both_tracks_pass: Literal[True] = True
    best_trace_selection_forbidden: Literal[True] = True
    nonconverged_promotion_forbidden: Literal[True] = True
    promotable_attempt_id: str = Field(min_length=1)


class MfemConcaveExperimentPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-mfem-concave-bounded-experiment-plan-1']
    plan_id: str = Field(min_length=1)
    authority: AuthorityBinding
    solver_configuration: SolverConfiguration
    numerical_contract: NumericalContract
    resource_ceiling: ResourceCeiling
    attempts: tuple[AttemptSpec, ...]
    time_track: tuple[str, str, str]
    space_track: tuple[str, str, str]
    promotion: PromotionPolicy

    @model_validator(mode='after')
    def validate_bounded_axes(self) -> 'MfemConcaveExperimentPlan':
        if len(self.attempts) != 5:
            raise ValueError('bounded MFEM experiment must contain exactly five attempts')
        ids = [item.attempt_id for item in self.attempts]
        if len(ids) != len(set(ids)):
            raise ValueError('attempt ids must be unique')
        id_set = set(ids)
        if set(self.time_track) - id_set or set(self.space_track) - id_set:
            raise ValueError('track references unknown attempt')
        overlap = set(self.time_track) & set(self.space_track)
        if len(overlap) != 1:
            raise ValueError('time and space tracks must share exactly one pivot attempt')

        by_id = {item.attempt_id: item for item in self.attempts}
        time_specs = [by_id[item_id] for item_id in self.time_track]
        if len({item.order for item in time_specs}) != 1:
            raise ValueError('time track must hold H1 order fixed')
        if len({item.uniform_refinements for item in time_specs}) != 1:
            raise ValueError('time track must hold spatial refinement fixed')
        time_rates = [item.sample_rate_hz for item in time_specs]
        if not all(a < b for a, b in zip(time_rates, time_rates[1:])):
            raise ValueError('time track sample rates must strictly increase')

        space_specs = [by_id[item_id] for item_id in self.space_track]
        if len({item.order for item in space_specs}) != 1:
            raise ValueError('space track must hold H1 order fixed')
        if len({item.sample_rate_hz for item in space_specs}) != 1:
            raise ValueError('space track must hold solver dt fixed')
        space_levels = [item.uniform_refinements for item in space_specs]
        if not all(a < b for a, b in zip(space_levels, space_levels[1:])):
            raise ValueError('space track uniform refinements must strictly increase')

        pivot_id = next(iter(overlap))
        if pivot_id != self.time_track[-1] or pivot_id != self.space_track[1]:
            raise ValueError('shared pivot must be the finest time level and middle space level')
        if self.promotion.promotable_attempt_id != self.space_track[-1]:
            raise ValueError('only the finest predeclared space attempt may be promoted')
        return self

    def solver_configuration_hash(self) -> str:
        return semantic_hash(self.solver_configuration.model_dump(mode='json'))

    def mesh_refinement_configuration_hash(self) -> str:
        return semantic_hash(
            {
                'attempts': [item.model_dump(mode='json') for item in self.attempts],
                'time_track': list(self.time_track),
                'space_track': list(self.space_track),
                'promotion': self.promotion.model_dump(mode='json'),
            }
        )


class ExperimentAttemptResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt_id: str = Field(min_length=1)
    status: AttemptStatus
    reason_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    numerical_identity_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    elements: int | None = None
    ndofs: int | None = None
    solve_s: float | None = None
    peak_ram_mb: float | None = None
    output_mb: float | None = None
    source_mass_relative_residual: float | None = None
    max_implicit_relative_residual: float | None = None


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


class TrackEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    track_id: Literal['time', 'space']
    status: TrackStatus
    pair_ids: tuple[str, ...]
    complex_rms_relative_strictly_decreasing: bool | None = None
    final_pair_within_current_tolerance: bool | None = None
    violations: tuple[str, ...] = ()


class ExperimentDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal['r100b-mfem-concave-bounded-experiment-decision-1'] = (
        'r100b-mfem-concave-bounded-experiment-decision-1'
    )
    plan_id: str
    solver_configuration_sha256: str
    mesh_refinement_configuration_sha256: str
    outcome: ExperimentOutcome
    promoted_attempt_id: str | None
    attempt_results: tuple[ExperimentAttemptResult, ...]
    pair_metrics: tuple[PairMetrics, ...]
    track_evaluations: tuple[TrackEvaluation, ...]
    conclusion_codes: tuple[str, ...]
    deterministic_report_identity_sha256: str


def load_experiment_plan(path: str | Path) -> MfemConcaveExperimentPlan:
    return MfemConcaveExperimentPlan.model_validate_json(
        Path(path).read_text(encoding='utf-8')
    )


def validate_exact_authority_binding(
    plan: MfemConcaveExperimentPlan,
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


def _evaluate_track(
    plan: MfemConcaveExperimentPlan,
    *,
    track_id: Literal['time', 'space'],
    attempt_ids: tuple[str, str, str],
    pair_by_id: dict[str, PairMetrics],
) -> TrackEvaluation:
    required_pair_ids = (
        _pair_id(attempt_ids[0], attempt_ids[1]),
        _pair_id(attempt_ids[1], attempt_ids[2]),
    )
    missing = [pair_id for pair_id in required_pair_ids if pair_id not in pair_by_id]
    if missing:
        return TrackEvaluation(
            track_id=track_id,
            status='BLOCKED',
            pair_ids=required_pair_ids,
            violations=tuple(f'missing_pair:{item}' for item in missing),
        )

    first = pair_by_id[required_pair_ids[0]]
    final = pair_by_id[required_pair_ids[1]]
    rms_decreasing = first.complex_rms_relative > final.complex_rms_relative
    contract = plan.numerical_contract
    final_within = (
        final.magnitude_max_abs_db <= contract.magnitude_absolute_tolerance_db
        and final.magnitude_max_relative <= contract.magnitude_relative_tolerance
        and final.phase_max_error_deg <= contract.phase_tolerance_deg
    )

    violations: list[str] = []
    if not rms_decreasing:
        violations.append('complex_rms_relative_not_strictly_decreasing')
    if final.magnitude_max_abs_db > contract.magnitude_absolute_tolerance_db:
        violations.append('magnitude_absolute_tolerance_exceeded')
    if final.magnitude_max_relative > contract.magnitude_relative_tolerance:
        violations.append('magnitude_relative_tolerance_exceeded')
    if final.phase_max_error_deg > contract.phase_tolerance_deg:
        violations.append('phase_tolerance_exceeded')

    return TrackEvaluation(
        track_id=track_id,
        status='PASS' if not violations else 'FAIL',
        pair_ids=required_pair_ids,
        complex_rms_relative_strictly_decreasing=rms_decreasing,
        final_pair_within_current_tolerance=final_within,
        violations=tuple(violations),
    )


def _decision_identity_payload(
    plan: MfemConcaveExperimentPlan,
    *,
    outcome: ExperimentOutcome,
    promoted_attempt_id: str | None,
    attempts: tuple[ExperimentAttemptResult, ...],
    pairs: tuple[PairMetrics, ...],
    tracks: tuple[TrackEvaluation, ...],
    conclusion_codes: tuple[str, ...],
) -> dict[str, object]:
    return {
        'schema_version': 'r100b-mfem-concave-bounded-experiment-decision-1',
        'plan_id': plan.plan_id,
        'authority': plan.authority.model_dump(mode='json'),
        'solver_configuration_sha256': plan.solver_configuration_hash(),
        'mesh_refinement_configuration_sha256': plan.mesh_refinement_configuration_hash(),
        'outcome': outcome,
        'promoted_attempt_id': promoted_attempt_id,
        'attempts': [
            {
                'attempt_id': item.attempt_id,
                'status': item.status,
                'reason_code': item.reason_code,
                'numerical_identity_sha256': item.numerical_identity_sha256,
            }
            for item in attempts
        ],
        'pair_metrics': [item.model_dump(mode='json') for item in pairs],
        'track_evaluations': [item.model_dump(mode='json') for item in tracks],
        'conclusion_codes': list(conclusion_codes),
    }


def evaluate_bounded_experiment(
    plan: MfemConcaveExperimentPlan,
    *,
    attempt_results: tuple[ExperimentAttemptResult, ...],
    pair_metrics: tuple[PairMetrics, ...],
) -> ExperimentDecision:
    result_by_id = {item.attempt_id: item for item in attempt_results}
    if len(result_by_id) != len(attempt_results):
        raise ValueError('attempt result ids must be unique')
    expected_ids = tuple(item.attempt_id for item in plan.attempts)
    if set(result_by_id) != set(expected_ids):
        raise ValueError('attempt results must preserve every predeclared bounded attempt')
    ordered_attempts = tuple(result_by_id[item_id] for item_id in expected_ids)

    pair_by_id: dict[str, PairMetrics] = {}
    for item in pair_metrics:
        pair_id = _pair_id(item.coarse_attempt_id, item.fine_attempt_id)
        if pair_id in pair_by_id:
            raise ValueError(f'duplicate pair metrics: {pair_id}')
        pair_by_id[pair_id] = item
    ordered_pairs = tuple(
        sorted(
            pair_metrics,
            key=lambda item: (item.coarse_attempt_id, item.fine_attempt_id),
        )
    )

    time_eval = _evaluate_track(
        plan,
        track_id='time',
        attempt_ids=plan.time_track,
        pair_by_id=pair_by_id,
    )
    space_eval = _evaluate_track(
        plan,
        track_id='space',
        attempt_ids=plan.space_track,
        pair_by_id=pair_by_id,
    )
    tracks = (time_eval, space_eval)

    incomplete = [item for item in ordered_attempts if item.status != 'COMPLETED']
    if incomplete:
        outcome: ExperimentOutcome = 'BLOCKED'
        promoted_attempt_id = None
        conclusion_codes = tuple(
            sorted({f'attempt_{item.status.lower()}:{item.reason_code}' for item in incomplete})
        )
    elif any(item.status == 'BLOCKED' for item in tracks):
        outcome = 'BLOCKED'
        promoted_attempt_id = None
        conclusion_codes = tuple(
            f'{item.track_id}_track_blocked' for item in tracks if item.status == 'BLOCKED'
        )
    elif all(item.status == 'PASS' for item in tracks):
        outcome = 'PASS'
        promoted_attempt_id = plan.promotion.promotable_attempt_id
        conclusion_codes = ('time_and_space_convergence_pass_current_tolerance',)
    else:
        outcome = 'FAIL'
        promoted_attempt_id = None
        conclusion_codes = tuple(
            f'{item.track_id}_track_nonconverged' for item in tracks if item.status == 'FAIL'
        )

    identity_payload = _decision_identity_payload(
        plan,
        outcome=outcome,
        promoted_attempt_id=promoted_attempt_id,
        attempts=ordered_attempts,
        pairs=ordered_pairs,
        tracks=tracks,
        conclusion_codes=conclusion_codes,
    )
    return ExperimentDecision(
        plan_id=plan.plan_id,
        solver_configuration_sha256=plan.solver_configuration_hash(),
        mesh_refinement_configuration_sha256=plan.mesh_refinement_configuration_hash(),
        outcome=outcome,
        promoted_attempt_id=promoted_attempt_id,
        attempt_results=ordered_attempts,
        pair_metrics=ordered_pairs,
        track_evaluations=tracks,
        conclusion_codes=conclusion_codes,
        deterministic_report_identity_sha256=semantic_hash(identity_payload),
    )
