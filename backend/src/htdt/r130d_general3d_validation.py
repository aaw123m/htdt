from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Literal, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator


PLAN_SCHEMA = 'htdt.r130d.general3d-validation-plan-1'
EVIDENCE_SCHEMA = 'htdt.r130d.general3d-validation-evidence-1'
EVIDENCE_ENVELOPE_SCHEMA = 'htdt.r130d.general3d-validation-envelope-1'
TARGET_WINDOW_DIAGNOSTIC_PLAN_SCHEMA = (
    'htdt.r130d.target-window-diagnostic-plan-1'
)
TARGET_WINDOW_DIAGNOSTIC_PLAN_SHA256 = (
    'ff42a7e0c44ed4726ea34edfa2549d018d66a37181786df18bbd4cf59c61d0db'
)


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def semantic_hash(value: object) -> str:
    return sha256(canonical_json(value).encode('utf-8')).hexdigest()


def load_target_window_diagnostic_plan(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('R130D target-window diagnostic plan must be a JSON object')
    if payload.get('schema_version') != TARGET_WINDOW_DIAGNOSTIC_PLAN_SCHEMA:
        raise ValueError('R130D target-window diagnostic plan schema mismatch')
    digest = semantic_hash(payload)
    if digest != TARGET_WINDOW_DIAGNOSTIC_PLAN_SHA256:
        raise ValueError(
            'R130D target-window diagnostic plan differs from the frozen pre-run '
            f'authority: {digest}'
        )
    return payload


def target_window_sampling_metadata(
    *,
    solver: str,
    requested_duration_s: float,
    dt_s: float,
    sample_count: int,
    frequency_hz: Sequence[float],
    source_sampling: str,
    pressure_sampling: str,
) -> dict[str, Any]:
    duration = float(requested_duration_s)
    dt = float(dt_s)
    count = int(sample_count)
    if not solver:
        raise ValueError('sampling metadata solver must be non-empty')
    if not math.isfinite(duration) or duration <= 0.0:
        raise ValueError('requested duration must be finite and positive')
    if not math.isfinite(dt) or dt <= 0.0:
        raise ValueError('native dt must be finite and positive')
    if count < 1:
        raise ValueError('sample count must be positive')
    frequencies = [float(item) for item in frequency_hz]
    if (
        not frequencies
        or any(not math.isfinite(item) or item <= 0.0 for item in frequencies)
    ):
        raise ValueError('frequency grid must be finite and positive')

    first = 0.0
    last = float((count - 1) * dt)
    native_end = float(count * dt)
    tolerance = max(1.0e-15, abs(dt) * 1.0e-12)
    if last >= duration + tolerance:
        raise ValueError('native record contains a sample at/after target duration')
    if native_end + tolerance < duration:
        raise ValueError('native record does not cover requested target duration')

    return {
        'solver': solver,
        'requested_duration_s': duration,
        'native_dt_s': dt,
        'generated_sample_count': count,
        'actual_first_sample_time_s': first,
        'actual_last_sample_time_s': last,
        'canonical_effective_integration_interval_s': [first, native_end],
        'canonical_endpoint_convention': (
            'sample timestamps are t_n=n*dt with t_n<T; every native sample '
            'receives a full dt left-rectangle weight'
        ),
        'actual_n_dt_s': native_end,
        'n_dt_minus_requested_duration_s': native_end - duration,
        'target_effective_integration_interval_s': [first, duration],
        'target_endpoint_convention': (
            'exact [0,T); final left-rectangle cell is clipped at T and no '
            'sample at T is included'
        ),
        'phasor_convention': 'exp(-i*omega*t)',
        'analysis_fourier_kernel': 'exp(+i*omega*t)',
        'canonical_rectangular_weighting_rule': (
            'dt * sum_n y[n] * exp(+i*2*pi*f*n*dt)'
        ),
        'target_window_weighting_rule': (
            'sum_n Delta_t[n] * y[n] * exp(+i*2*pi*f*n*dt), '
            'Delta_t[n]=min(dt,T-n*dt)'
        ),
        'source_q_t_sampling': source_sampling,
        'pressure_p_t_sampling': pressure_sampling,
        'frequency_evaluation_rule': (
            'direct evaluation at exact requested frequencies; no FFT-bin '
            'rounding, masking change, shift, or fit'
        ),
        'frequency_hz': frequencies,
    }


def native_window_left_rectangle_spectrum(
    samples: Sequence[complex] | np.ndarray,
    *,
    dt_s: float,
    frequency_hz: Sequence[float] | np.ndarray,
) -> np.ndarray:
    values = np.asarray(samples)
    if values.ndim != 1 or values.size < 1:
        raise ValueError('native-window spectrum requires a non-empty 1D trace')
    if not (
        np.all(np.isfinite(values.real))
        and np.all(np.isfinite(values.imag))
    ):
        raise ValueError('native-window trace must be finite')
    dt = float(dt_s)
    frequencies = np.asarray(frequency_hz, dtype=np.float64)
    if not math.isfinite(dt) or dt <= 0.0:
        raise ValueError('native-window dt must be finite and positive')
    if (
        frequencies.ndim != 1
        or frequencies.size == 0
        or not np.all(np.isfinite(frequencies))
        or np.any(frequencies <= 0.0)
    ):
        raise ValueError('native-window frequencies must be finite and positive')
    times = np.arange(values.size, dtype=np.float64) * dt
    kernel = np.exp(
        2j * np.pi * frequencies[:, None] * times[None, :]
    )
    spectrum = dt * (kernel @ values.astype(np.complex128, copy=False))
    if not (
        np.all(np.isfinite(spectrum.real))
        and np.all(np.isfinite(spectrum.imag))
    ):
        raise ValueError('native-window spectrum is non-finite')
    return np.asarray(spectrum, dtype=np.complex128)


def native_window_left_rectangle_transfer(
    pressure_trace: Sequence[complex] | np.ndarray,
    source_volume_velocity_trace: Sequence[complex] | np.ndarray,
    *,
    dt_s: float,
    frequency_hz: Sequence[float] | np.ndarray,
) -> np.ndarray:
    pressure = np.asarray(pressure_trace)
    source = np.asarray(source_volume_velocity_trace)
    if pressure.ndim != 1 or source.ndim != 1 or pressure.shape != source.shape:
        raise ValueError(
            'native-window P/Q requires matching 1D pressure/source traces'
        )
    p_spectrum = native_window_left_rectangle_spectrum(
        pressure, dt_s=dt_s, frequency_hz=frequency_hz
    )
    q_spectrum = native_window_left_rectangle_spectrum(
        source, dt_s=dt_s, frequency_hz=frequency_hz
    )
    source_floor = np.finfo(np.float64).eps * max(
        1.0, float(np.max(np.abs(q_spectrum)))
    )
    if np.any(np.abs(q_spectrum) <= source_floor):
        raise ValueError('native-window physical source spectrum is zero')
    transfer = p_spectrum / q_spectrum
    if not (
        np.all(np.isfinite(transfer.real))
        and np.all(np.isfinite(transfer.imag))
    ):
        raise ValueError('native-window transfer is non-finite')
    return np.asarray(transfer, dtype=np.complex128)


def analytic_sampled_complex_harmonic_left_rectangle_spectrum(
    *,
    amplitude: complex,
    harmonic_frequency_hz: float,
    analysis_frequency_hz: Sequence[float] | np.ndarray,
    dt_s: float,
    sample_count: int,
) -> np.ndarray:
    frequencies = np.asarray(analysis_frequency_hz, dtype=np.float64)
    dt = float(dt_s)
    count = int(sample_count)
    harmonic = float(harmonic_frequency_hz)
    if not math.isfinite(dt) or dt <= 0.0 or count < 1:
        raise ValueError('analytic sampled harmonic requires positive dt/count')
    delta = frequencies - harmonic
    phase_step = np.exp(2j * np.pi * delta * dt)
    denominator = 1.0 - phase_step
    near = np.abs(denominator) <= 1.0e-13
    series = np.empty(frequencies.shape, dtype=np.complex128)
    series[near] = float(count)
    if np.any(~near):
        series[~near] = (
            1.0 - np.power(phase_step[~near], count)
        ) / denominator[~near]
    return complex(amplitude) * dt * series


def target_window_clipped_left_rectangle_spectrum(
    samples: Sequence[complex] | np.ndarray,
    *,
    dt_s: float,
    target_duration_s: float,
    frequency_hz: Sequence[float] | np.ndarray,
) -> np.ndarray:
    values = np.asarray(samples)
    if values.ndim != 1 or values.size < 1:
        raise ValueError('target-window clipped left-rectangle spectrum requires a non-empty 1D trace')
    if not (
        np.all(np.isfinite(values.real))
        and np.all(np.isfinite(values.imag))
    ):
        raise ValueError('target-window clipped left-rectangle trace must be finite')

    dt = float(dt_s)
    duration = float(target_duration_s)
    frequencies = np.asarray(frequency_hz, dtype=np.float64)
    if not math.isfinite(dt) or dt <= 0.0:
        raise ValueError('target-window clipped left-rectangle dt must be finite and positive')
    if not math.isfinite(duration) or duration <= 0.0:
        raise ValueError('target-window clipped left-rectangle duration must be finite and positive')
    if (
        frequencies.ndim != 1
        or frequencies.size == 0
        or not np.all(np.isfinite(frequencies))
        or np.any(frequencies <= 0.0)
    ):
        raise ValueError('target-window clipped left-rectangle frequencies must be finite and positive')

    coverage_end = float(values.size * dt)
    tolerance = max(1.0e-15, abs(dt) * 1.0e-12)
    if coverage_end + tolerance < duration:
        raise ValueError('target-window clipped left-rectangle trace does not cover target duration')

    starts = np.arange(values.size, dtype=np.float64) * dt
    active = starts < duration
    starts = starts[active]
    widths = np.minimum(dt, duration - starts)
    active_values = values[active].astype(np.complex128, copy=False)
    kernel = np.exp(
        2j * np.pi * frequencies[:, None] * starts[None, :]
    )
    spectrum = (kernel * widths[None, :]) @ active_values
    if not (
        np.all(np.isfinite(spectrum.real))
        and np.all(np.isfinite(spectrum.imag))
    ):
        raise ValueError('target-window clipped left-rectangle spectrum is non-finite')
    return np.asarray(spectrum, dtype=np.complex128)


def target_window_clipped_left_rectangle_transfer(
    pressure_trace: Sequence[complex] | np.ndarray,
    source_volume_velocity_trace: Sequence[complex] | np.ndarray,
    *,
    dt_s: float,
    target_duration_s: float,
    frequency_hz: Sequence[float] | np.ndarray,
) -> np.ndarray:
    pressure = np.asarray(pressure_trace)
    source = np.asarray(source_volume_velocity_trace)
    if pressure.ndim != 1 or source.ndim != 1 or pressure.shape != source.shape:
        raise ValueError(
            'target-window P/Q requires matching 1D pressure/source traces'
        )
    p_spectrum = target_window_clipped_left_rectangle_spectrum(
        pressure,
        dt_s=dt_s,
        target_duration_s=target_duration_s,
        frequency_hz=frequency_hz,
    )
    q_spectrum = target_window_clipped_left_rectangle_spectrum(
        source,
        dt_s=dt_s,
        target_duration_s=target_duration_s,
        frequency_hz=frequency_hz,
    )
    source_floor = np.finfo(np.float64).eps * max(
        1.0, float(np.max(np.abs(q_spectrum)))
    )
    if np.any(np.abs(q_spectrum) <= source_floor):
        raise ValueError('target-window physical source spectrum is zero')
    transfer = p_spectrum / q_spectrum
    if not (
        np.all(np.isfinite(transfer.real))
        and np.all(np.isfinite(transfer.imag))
    ):
        raise ValueError('target-window transfer is non-finite')
    return np.asarray(transfer, dtype=np.complex128)


def analytic_complex_harmonic_spectrum(
    *,
    amplitude: complex,
    harmonic_frequency_hz: float,
    analysis_frequency_hz: Sequence[float] | np.ndarray,
    duration_s: float,
) -> np.ndarray:
    frequencies = np.asarray(analysis_frequency_hz, dtype=np.float64)
    duration = float(duration_s)
    harmonic = float(harmonic_frequency_hz)
    if not math.isfinite(duration) or duration <= 0.0:
        raise ValueError('analytic harmonic duration must be finite and positive')
    if not math.isfinite(harmonic):
        raise ValueError('analytic harmonic frequency must be finite')
    delta = frequencies - harmonic
    output = np.empty(frequencies.shape, dtype=np.complex128)
    near = np.isclose(delta, 0.0, rtol=0.0, atol=1.0e-14)
    output[near] = complex(amplitude) * duration
    if np.any(~near):
        d = delta[~near]
        output[~near] = complex(amplitude) * (
            np.exp(2j * np.pi * d * duration) - 1.0
        ) / (2j * np.pi * d)
    return output


class FixtureContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    fixture_id: str = Field(min_length=1)
    source_key: Literal['sloped']
    geometry_kind: Literal['explicit_polyhedral']
    vertices_m: tuple[tuple[float, float, float], ...] = Field(min_length=4)
    faces: tuple[tuple[str, tuple[int, ...]], ...] = Field(min_length=4)
    base_tetrahedra: tuple[tuple[int, int, int, int], ...] = Field(min_length=1)
    base_tetrahedralization_volume_m3: float = Field(gt=0.0)
    source_position_m: tuple[float, float, float]
    receiver_position_m: tuple[float, float, float]
    boundary_model: Literal['natural_neumann_rigid']
    density_kg_m3: float = Field(gt=0.0)
    sound_speed_m_s: float = Field(gt=0.0)

    @model_validator(mode='after')
    def validate_indices(self) -> 'FixtureContract':
        n = len(self.vertices_m)
        for _, loop in self.faces:
            if len(loop) < 3 or any(index < 0 or index >= n for index in loop):
                raise ValueError('fixture face index is invalid')
        for tet in self.base_tetrahedra:
            if len(set(tet)) != 4 or any(index < 0 or index >= n for index in tet):
                raise ValueError('fixture tetrahedron index is invalid')
        return self


class PhysicalQuantityContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    quantity: Literal['finite_record_complex_acoustic_pressure_per_volume_velocity']
    unit: Literal['Pa/(m3/s)']
    record_interval: Literal['[0,T)']
    duration_s: float = Field(gt=0.0)
    frequency_hz: tuple[float, ...] = Field(min_length=2)
    phasor_convention: Literal['exp(-i*omega*t)']
    analysis_fourier_kernel: Literal['exp(+i*omega*t)']
    source_contract: str = Field(min_length=1)
    pffdtd_artifact_normalization: str = Field(min_length=1)
    mfem_initial_condition: str = Field(min_length=1)
    pressure_conversion: str = Field(min_length=1)
    normalization_claim: str = Field(min_length=1)
    window_function: Literal['rectangular_no_taper'] = 'rectangular_no_taper'
    frequency_bin_policy: str = 'arbitrary finite-record evaluation frequencies'
    source_normalization: str = 'unit discrete volume-velocity impulse'
    receiver_observable: str = 'point acoustic pressure'
    geometry_units: Literal['m'] = 'm'

    @model_validator(mode='after')
    def validate_frequency_axis(self) -> 'PhysicalQuantityContract':
        if tuple(self.frequency_hz) != tuple(sorted(set(self.frequency_hz))):
            raise ValueError('comparison frequencies must be sorted and unique')
        if any(item <= 0.0 or not math.isfinite(item) for item in self.frequency_hz):
            raise ValueError('comparison frequencies must be finite and positive')
        if self.frequency_bin_policy == 'record_coherent_integer_cycles':
            for frequency in self.frequency_hz:
                cycles = float(frequency) * float(self.duration_s)
                if not math.isclose(cycles, round(cycles), rel_tol=0.0, abs_tol=1.0e-12):
                    raise ValueError(
                        'record-coherent comparison requires an integer cycle count'
                    )
        return self


class IndependentReferenceContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    solver: Literal['MFEM']
    source_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    formulation: str = Field(min_length=1)
    spatial_discretization: str = Field(min_length=1)
    pffdtd_voxel_or_triangle_intersection_reuse: Literal[False] = False
    polynomial_order: int = Field(ge=1)
    uniform_refinements: tuple[int, ...] = Field(min_length=3)
    expected_element_counts: tuple[int, ...] = Field(min_length=3)
    expected_dofs: tuple[int, ...] | None = None
    boundary_condition: str = Field(min_length=1)
    mass_assembly: str = Field(min_length=1)
    stiffness_assembly: str = Field(min_length=1)
    source_functional: str = Field(min_length=1)
    receiver_functional: str = Field(min_length=1)
    modal_solver: str = Field(min_length=1)
    modal_numpy_version: str = Field(min_length=1)
    modal_scipy_version: str = Field(min_length=1)
    modal_sample_rate_hz: int = Field(ge=1)
    mass_orthonormality_max_abs_tolerance: float = Field(gt=0.0)
    generalized_eigen_residual_relative_tolerance: float = Field(gt=0.0)

    @model_validator(mode='after')
    def validate_schedule(self) -> 'IndependentReferenceContract':
        legacy = ((0, 1, 2), (6, 48, 384))
        diagnosed = ((1, 2, 3), (48, 384, 3072))
        schedule = (self.uniform_refinements, self.expected_element_counts)
        if schedule not in (legacy, diagnosed):
            raise ValueError(
                'MFEM refinement schedule must be the PR #282 legacy series '
                'or the predeclared diagnosed 1/2/3 series'
            )
        if self.uniform_refinements == diagnosed[0]:
            if self.expected_dofs != (125, 729, 4913):
                raise ValueError('diagnosed MFEM DOF schedule is frozen to 125/729/4913')
        elif self.expected_dofs is not None and len(self.expected_dofs) != 3:
            raise ValueError('legacy MFEM expected_dofs must have three entries when set')
        return self


class PffdtdContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_commit_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    backend: Literal['Python/Numba CPU']
    points_per_wavelength: tuple[float, ...] = Field(min_length=3)
    fmax_hz: float = Field(gt=0.0)
    max_grid_cells: int = Field(ge=1)
    max_time_steps: int = Field(ge=1)
    max_output_bytes: int = Field(ge=1)
    max_solver_wall_seconds: float = Field(gt=0.0)
    solver_threads: int = Field(ge=1)
    setup_processes: int = Field(ge=1)

    @model_validator(mode='after')
    def validate_schedule(self) -> 'PffdtdContract':
        if self.points_per_wavelength not in (
            (6.0, 8.0, 10.0),
            (8.0, 10.0, 12.0),
        ):
            raise ValueError(
                'PFFDTD refinement schedule must be PR #282 legacy 6/8/10 '
                'or the predeclared diagnosed 8/10/12 series'
            )
        return self


class MetricThreshold(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    complex_rms_relative_max: float = Field(gt=0.0)
    magnitude_max_relative: float = Field(gt=0.0)
    phase_max_deg: float = Field(gt=0.0)
    magnitude_max_db: float | None = Field(default=None, gt=0.0)


class AcceptanceContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    magnitude_mask_relative_db: float
    reference_self_convergence: MetricThreshold
    pffdtd_self_convergence: MetricThreshold
    cross_solver_fine_fine: MetricThreshold
    rules: tuple[str, ...] = Field(min_length=1)


class ResourceCeiling(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    workflow_timeout_minutes: int = Field(ge=1)
    mfem_build_parallelism: int = Field(ge=1)
    max_reference_wall_seconds_per_level: float = Field(gt=0.0)
    max_reference_peak_ram_mb: float = Field(gt=0.0)
    max_total_evidence_mb: float = Field(gt=0.0)


class ScopeNonClaims(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    production_solver_selected: Literal[False] = False
    concave_validated: Literal[False] = False
    multi_region_validated: Literal[False] = False
    portal_validated: Literal[False] = False
    owned_room_evidence: Literal[False] = False
    gpu_validated: Literal[False] = False


class R130DGeneral3DValidationPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal['htdt.r130d.general3d-validation-plan-1'] = PLAN_SCHEMA
    plan_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    fixture: FixtureContract
    physical_quantity: PhysicalQuantityContract
    independent_reference: IndependentReferenceContract
    pffdtd: PffdtdContract
    acceptance: AcceptanceContract
    resource_ceiling: ResourceCeiling
    scope_nonclaims: ScopeNonClaims

    def plan_sha256(self) -> str:
        return semantic_hash(self.model_dump(mode='json'))

    def fixture_sha256(self) -> str:
        return semantic_hash(self.fixture.model_dump(mode='json'))

    def reference_mesh_sha256(self, refinement: int) -> str:
        if refinement not in self.independent_reference.uniform_refinements:
            raise ValueError('reference refinement is not predeclared')
        return semantic_hash(
            {
                'fixture_sha256': self.fixture_sha256(),
                'base_tetrahedra': self.fixture.base_tetrahedra,
                'uniform_refinement': refinement,
                'polynomial_order': self.independent_reference.polynomial_order,
                'mesh_algorithm': 'MFEM uniform tetra refinement',
                'mfem_source_commit_sha': self.independent_reference.source_commit_sha,
            }
        )


def load_validation_plan(path: str | Path) -> R130DGeneral3DValidationPlan:
    return R130DGeneral3DValidationPlan.model_validate_json(
        Path(path).read_text(encoding='utf-8')
    )


def validate_exact_binding(
    plan: R130DGeneral3DValidationPlan,
    *,
    vertices_m: Sequence[Sequence[float]],
    faces: Sequence[tuple[str, Sequence[int]]],
    source_position_m: Sequence[float],
    receiver_position_m: Sequence[float],
    quantity: str,
    unit: str,
    phasor_convention: str,
    analysis_fourier_kernel: str,
    pffdtd_source_commit_sha: str,
    independent_source_commit_sha: str,
) -> None:
    expected_faces = tuple((key, tuple(loop)) for key, loop in plan.fixture.faces)
    actual_faces = tuple((str(key), tuple(int(i) for i in loop)) for key, loop in faces)
    checks: tuple[tuple[str, object, object], ...] = (
        (
            'vertices',
            tuple(tuple(float(x) for x in row) for row in vertices_m),
            plan.fixture.vertices_m,
        ),
        ('faces', actual_faces, expected_faces),
        (
            'source_position_m',
            tuple(float(x) for x in source_position_m),
            plan.fixture.source_position_m,
        ),
        (
            'receiver_position_m',
            tuple(float(x) for x in receiver_position_m),
            plan.fixture.receiver_position_m,
        ),
        ('quantity', quantity, plan.physical_quantity.quantity),
        ('unit', unit, plan.physical_quantity.unit),
        (
            'phasor_convention',
            phasor_convention,
            plan.physical_quantity.phasor_convention,
        ),
        (
            'analysis_fourier_kernel',
            analysis_fourier_kernel,
            plan.physical_quantity.analysis_fourier_kernel,
        ),
        (
            'pffdtd_source_commit_sha',
            pffdtd_source_commit_sha,
            plan.pffdtd.source_commit_sha,
        ),
        (
            'independent_source_commit_sha',
            independent_source_commit_sha,
            plan.independent_reference.source_commit_sha,
        ),
    )
    for name, actual, expected in checks:
        if actual != expected:
            raise ValueError(f'R130D validation binding mismatch for {name}')


def validate_refinement_schedule(
    plan: R130DGeneral3DValidationPlan,
    *,
    reference_refinements: Sequence[int],
    pffdtd_points_per_wavelength: Sequence[float],
) -> None:
    if tuple(int(x) for x in reference_refinements) != (
        plan.independent_reference.uniform_refinements
    ):
        raise ValueError('reference refinement schedule differs from predeclared plan')
    if tuple(float(x) for x in pffdtd_points_per_wavelength) != (
        plan.pffdtd.points_per_wavelength
    ):
        raise ValueError('PFFDTD refinement schedule differs from predeclared plan')


class ObservableContractMismatch(ValueError):
    pass


def validate_physical_observable_contract(
    *,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> None:
    required = (
        'quantity',
        'unit',
        'source_position_m',
        'receiver_position_m',
        'source_convention',
        'pressure_normalization',
        'excitation_normalization',
        'phasor_convention',
        'analysis_fourier_kernel',
        'record_duration_s',
        'record_interval',
        'window_function',
        'frequency_hz',
        'sound_speed_m_s',
        'density_kg_m3',
        'boundary_condition',
        'geometry_sha256',
        'geometry_units',
    )
    for name in required:
        if name not in expected or name not in actual:
            raise ObservableContractMismatch(
                f'R130D physical observable contract missing {name}'
            )
        left = expected[name]
        right = actual[name]
        if isinstance(left, float) or isinstance(right, float):
            try:
                equal = math.isclose(
                    float(left), float(right), rel_tol=0.0, abs_tol=1.0e-12
                )
            except (TypeError, ValueError):
                equal = False
        else:
            equal = left == right
        if not equal:
            raise ObservableContractMismatch(
                f'R130D physical observable contract mismatch for {name}: '
                f'{right!r} != {left!r}'
            )


class PairMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    complex_rms_relative: float = Field(ge=0.0)
    magnitude_max_relative: float = Field(ge=0.0)
    magnitude_max_db: float = Field(ge=0.0)
    phase_max_deg: float = Field(ge=0.0)
    mask_floor: float = Field(ge=0.0)
    compared_frequency_count: int = Field(ge=1)
    frequency_metrics: tuple[dict[str, float | bool], ...] = Field(min_length=1)


def _as_complex(values: Sequence[Sequence[float]]) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError('complex values must be [[real, imag], ...]')
    if not np.all(np.isfinite(array)):
        raise ValueError('complex values must be finite')
    return array[:, 0] + 1j * array[:, 1]


def compare_complex_transfer(
    *,
    reference: Sequence[Sequence[float]],
    candidate: Sequence[Sequence[float]],
    frequency_hz: Sequence[float],
    magnitude_mask_relative_db: float,
) -> PairMetrics:
    ref = _as_complex(reference)
    cand = _as_complex(candidate)
    freq = np.asarray(frequency_hz, dtype=np.float64)
    if ref.shape != cand.shape or ref.shape != freq.shape:
        raise ValueError('comparison axes do not match')
    ref_mag = np.abs(ref)
    mask_floor = float(np.max(ref_mag) * (10.0 ** (magnitude_mask_relative_db / 20.0)))
    floor = max(mask_floor, np.finfo(np.float64).tiny)
    mask = ref_mag >= floor
    if not np.any(mask):
        raise ValueError('magnitude mask removed every comparison frequency')

    delta = cand - ref
    rms = float(
        np.linalg.norm(delta[mask])
        / max(float(np.linalg.norm(ref[mask])), np.finfo(np.float64).tiny)
    )
    mag_rel = np.abs(np.abs(cand) - ref_mag) / np.maximum(ref_mag, floor)
    ratio = np.maximum(np.abs(cand), floor) / np.maximum(ref_mag, floor)
    mag_db = np.abs(20.0 * np.log10(ratio))
    phase = np.angle(cand / ref, deg=True)
    phase = np.abs((phase + 180.0) % 360.0 - 180.0)

    per_frequency = tuple(
        {
            'frequency_hz': float(freq[index]),
            'masked_in': bool(mask[index]),
            'magnitude_absolute': float(abs(abs(cand[index]) - ref_mag[index])),
            'magnitude_relative': float(mag_rel[index]),
            'magnitude_db': float(mag_db[index]),
            'phase_deg': float(phase[index]),
            'complex_relative': float(
                abs(delta[index]) / max(abs(ref[index]), floor)
            ),
        }
        for index in range(freq.size)
    )
    return PairMetrics(
        complex_rms_relative=rms,
        magnitude_max_relative=float(np.max(mag_rel[mask])),
        magnitude_max_db=float(np.max(mag_db[mask])),
        phase_max_deg=float(np.max(phase[mask])),
        mask_floor=mask_floor,
        compared_frequency_count=int(np.count_nonzero(mask)),
        frequency_metrics=per_frequency,
    )


ValidationStatus = Literal['PASS', 'FAIL', 'BLOCKED']


def metric_status(metrics: PairMetrics, threshold: MetricThreshold) -> ValidationStatus:
    if metrics.complex_rms_relative > threshold.complex_rms_relative_max:
        return 'FAIL'
    if metrics.magnitude_max_relative > threshold.magnitude_max_relative:
        return 'FAIL'
    if metrics.phase_max_deg > threshold.phase_max_deg:
        return 'FAIL'
    if (
        threshold.magnitude_max_db is not None
        and metrics.magnitude_max_db > threshold.magnitude_max_db
    ):
        return 'FAIL'
    return 'PASS'


class SelfConvergenceAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    state: Literal['SELF_CONVERGENCE_PASS', 'SELF_CONVERGENCE_FAILED']
    pair_count: int = Field(ge=2)
    final_pair_status: Literal['PASS', 'FAIL']
    complex_rms_trend: Literal['DECREASING', 'NON_DECREASING']
    magnitude_relative_trend: Literal['DECREASING', 'NON_DECREASING']
    phase_trend: Literal['DECREASING', 'NON_DECREASING']
    pair_metrics: tuple[PairMetrics, ...] = Field(min_length=2)


def _decreasing(values: Sequence[float]) -> bool:
    numeric = [float(value) for value in values]
    tolerance = 1.0e-12
    return (
        len(numeric) >= 2
        and all(
            following <= previous * (1.0 + tolerance) + tolerance
            for previous, following in zip(numeric, numeric[1:])
        )
        and numeric[-1] < numeric[0]
    )


def assess_refinement_series(
    metrics: Sequence[PairMetrics],
    threshold: MetricThreshold,
) -> SelfConvergenceAssessment:
    pairs = tuple(metrics)
    if len(pairs) < 2:
        raise ValueError(
            'self-convergence requires at least two adjacent comparisons '
            'from three predeclared levels'
        )
    rms_decreasing = _decreasing([item.complex_rms_relative for item in pairs])
    magnitude_decreasing = _decreasing(
        [item.magnitude_max_relative for item in pairs]
    )
    phase_decreasing = _decreasing([item.phase_max_deg for item in pairs])
    final_status = metric_status(pairs[-1], threshold)
    passed = bool(
        final_status == 'PASS'
        and rms_decreasing
        and magnitude_decreasing
        and phase_decreasing
    )
    return SelfConvergenceAssessment(
        state='SELF_CONVERGENCE_PASS' if passed else 'SELF_CONVERGENCE_FAILED',
        pair_count=len(pairs),
        final_pair_status=final_status,
        complex_rms_trend='DECREASING' if rms_decreasing else 'NON_DECREASING',
        magnitude_relative_trend=(
            'DECREASING' if magnitude_decreasing else 'NON_DECREASING'
        ),
        phase_trend='DECREASING' if phase_decreasing else 'NON_DECREASING',
        pair_metrics=pairs,
    )


def validation_decision_v2(
    *,
    execution_state: Literal['PASS', 'EXECUTION_FAILED'],
    contract_state: Literal['MATCH', 'CONTRACT_MISMATCH'],
    reference_assessment: SelfConvergenceAssessment | None,
    pffdtd_assessment: SelfConvergenceAssessment | None,
    cross_solver_metrics: PairMetrics | None,
    plan: R130DGeneral3DValidationPlan,
) -> dict[str, str]:
    if execution_state != 'PASS':
        return {
            'execution_state': 'EXECUTION_FAILED',
            'contract_state': contract_state,
            'reference_self_convergence_state': 'NOT_EVALUATED',
            'pffdtd_self_convergence_state': 'NOT_EVALUATED',
            'cross_solver_state': 'CROSS_SOLVER_BLOCKED',
            'validation_state': 'NOT_VALIDATED',
            'reference_build_execution_status': 'FAIL',
            'reference_self_convergence_status': 'BLOCKED',
            'pffdtd_self_convergence_status': 'BLOCKED',
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'BLOCKED',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }
    if contract_state != 'MATCH':
        return {
            'execution_state': 'PASS',
            'contract_state': 'CONTRACT_MISMATCH',
            'reference_self_convergence_state': (
                reference_assessment.state
                if reference_assessment is not None
                else 'NOT_EVALUATED'
            ),
            'pffdtd_self_convergence_state': (
                pffdtd_assessment.state
                if pffdtd_assessment is not None
                else 'NOT_EVALUATED'
            ),
            'cross_solver_state': 'CROSS_SOLVER_BLOCKED',
            'validation_state': 'NOT_VALIDATED',
            'reference_build_execution_status': 'PASS',
            'reference_self_convergence_status': (
                'PASS'
                if reference_assessment is not None
                and reference_assessment.state == 'SELF_CONVERGENCE_PASS'
                else 'BLOCKED'
            ),
            'pffdtd_self_convergence_status': (
                'PASS'
                if pffdtd_assessment is not None
                and pffdtd_assessment.state == 'SELF_CONVERGENCE_PASS'
                else 'BLOCKED'
            ),
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'BLOCKED',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }
    if reference_assessment is None or pffdtd_assessment is None:
        raise ValueError('successful execution requires both self-convergence assessments')

    reference_pass = reference_assessment.state == 'SELF_CONVERGENCE_PASS'
    pffdtd_pass = pffdtd_assessment.state == 'SELF_CONVERGENCE_PASS'
    if not reference_pass or not pffdtd_pass:
        return {
            'execution_state': 'PASS',
            'contract_state': 'MATCH',
            'reference_self_convergence_state': reference_assessment.state,
            'pffdtd_self_convergence_state': pffdtd_assessment.state,
            'cross_solver_state': 'CROSS_SOLVER_BLOCKED',
            'validation_state': 'NOT_VALIDATED',
            'reference_build_execution_status': 'PASS',
            'reference_self_convergence_status': 'PASS' if reference_pass else 'FAIL',
            'pffdtd_self_convergence_status': 'PASS' if pffdtd_pass else 'FAIL',
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'FAIL',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }
    if cross_solver_metrics is None:
        raise ValueError(
            'cross-solver metrics are required only after both self-convergence gates pass'
        )
    cross_pass = (
        metric_status(cross_solver_metrics, plan.acceptance.cross_solver_fine_fine)
        == 'PASS'
    )
    return {
        'execution_state': 'PASS',
        'contract_state': 'MATCH',
        'reference_self_convergence_state': reference_assessment.state,
        'pffdtd_self_convergence_state': pffdtd_assessment.state,
        'cross_solver_state': (
            'CROSS_SOLVER_PASS' if cross_pass else 'CROSS_SOLVER_FAILED'
        ),
        'validation_state': 'VALIDATED' if cross_pass else 'NOT_VALIDATED',
        'reference_build_execution_status': 'PASS',
        'reference_self_convergence_status': 'PASS',
        'pffdtd_self_convergence_status': 'PASS',
        'cross_solver_agreement_status': 'PASS' if cross_pass else 'FAIL',
        'fixture_validation_result': 'PASS' if cross_pass else 'FAIL',
        'general_3d_validation_state': 'VALIDATED' if cross_pass else 'NOT_VALIDATED',
    }


def validation_decision(
    *,
    execution_status: ValidationStatus,
    reference_metrics: PairMetrics | None,
    pffdtd_metrics: PairMetrics | None,
    cross_solver_metrics: PairMetrics | None,
    plan: R130DGeneral3DValidationPlan,
) -> dict[str, str]:
    if execution_status != 'PASS':
        return {
            'reference_build_execution_status': execution_status,
            'reference_self_convergence_status': 'BLOCKED',
            'pffdtd_self_convergence_status': 'BLOCKED',
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'BLOCKED',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }
    if reference_metrics is None or pffdtd_metrics is None:
        return {
            'reference_build_execution_status': 'PASS',
            'reference_self_convergence_status': 'BLOCKED',
            'pffdtd_self_convergence_status': 'BLOCKED',
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'BLOCKED',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }

    reference_status = metric_status(
        reference_metrics, plan.acceptance.reference_self_convergence
    )
    pffdtd_status = metric_status(
        pffdtd_metrics, plan.acceptance.pffdtd_self_convergence
    )
    if reference_status != 'PASS' or pffdtd_status != 'PASS':
        return {
            'reference_build_execution_status': 'PASS',
            'reference_self_convergence_status': reference_status,
            'pffdtd_self_convergence_status': pffdtd_status,
            'cross_solver_agreement_status': 'BLOCKED',
            'fixture_validation_result': 'FAIL',
            'general_3d_validation_state': 'NOT_VALIDATED',
        }
    if cross_solver_metrics is None:
        cross_status: ValidationStatus = 'BLOCKED'
    else:
        cross_status = metric_status(
            cross_solver_metrics, plan.acceptance.cross_solver_fine_fine
        )
    fixture_status: ValidationStatus = (
        'PASS' if cross_status == 'PASS' else ('FAIL' if cross_status == 'FAIL' else 'BLOCKED')
    )
    return {
        'reference_build_execution_status': 'PASS',
        'reference_self_convergence_status': reference_status,
        'pffdtd_self_convergence_status': pffdtd_status,
        'cross_solver_agreement_status': cross_status,
        'fixture_validation_result': fixture_status,
        'general_3d_validation_state': (
            'VALIDATED_BOUNDED_SLOPED_FIXTURE'
            if fixture_status == 'PASS'
            else 'NOT_VALIDATED'
        ),
    }


def save_evidence(path: str | Path, payload: dict[str, Any]) -> str:
    if payload.get('schema_version') != EVIDENCE_SCHEMA:
        raise ValueError('R130D evidence payload schema mismatch')
    digest = semantic_hash(payload)
    envelope = {
        'schema_version': EVIDENCE_ENVELOPE_SCHEMA,
        'semantic_sha256': digest,
        'payload': payload,
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(canonical_json(envelope) + '\n', encoding='utf-8')
    return digest


def load_evidence(path: str | Path) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    if document.get('schema_version') != EVIDENCE_ENVELOPE_SCHEMA:
        raise ValueError('R130D evidence envelope schema mismatch')
    payload = document.get('payload')
    if not isinstance(payload, dict) or payload.get('schema_version') != EVIDENCE_SCHEMA:
        raise ValueError('R130D evidence payload is malformed')
    if document.get('semantic_sha256') != semantic_hash(payload):
        raise ValueError('R130D evidence payload was modified')
    return payload
