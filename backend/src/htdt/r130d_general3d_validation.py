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

    @model_validator(mode='after')
    def validate_frequency_axis(self) -> 'PhysicalQuantityContract':
        if tuple(self.frequency_hz) != tuple(sorted(set(self.frequency_hz))):
            raise ValueError('comparison frequencies must be sorted and unique')
        if any(item <= 0.0 or not math.isfinite(item) for item in self.frequency_hz):
            raise ValueError('comparison frequencies must be finite and positive')
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
        if self.uniform_refinements != (0, 1, 2):
            raise ValueError('MFEM refinement schedule is frozen to 0/1/2')
        if self.expected_element_counts != (6, 48, 384):
            raise ValueError('MFEM element schedule is frozen to 6/48/384')
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
        if self.points_per_wavelength != (6.0, 8.0, 10.0):
            raise ValueError('PFFDTD refinement schedule is frozen to PPW 6/8/10')
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
