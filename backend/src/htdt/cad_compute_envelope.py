"""Product compute envelope for heavy acoustic workloads (#778).

R140 owns execution planning and hardware-aware scheduling; this module
owns the user/product contract that runs BEFORE allocation:

1. estimate resources for a canonical workload spec;
2. classify it into the bounded problem classes (C-small / C-medium /
   C-large / outside-local-envelope);
3. propose a bounded response — equivalent execution change, explicit
   lower-fidelity proposal, or fail-closed — never silently degrading;
4. label the fidelity stage (screening / medium / high_fidelity) so a
   screening result can never look like the final solve;
5. record deterministic benchmark evidence per hardware profile.

Estimates are heuristics, not promises: they exist to prevent
crash-first/OOM behavior and to surface the dominant cost drivers in user
language. Semantic request identity never changes under an equivalent
execution change; a lower-fidelity run always produces a proposal object
showing requested vs. executed specs and requires explicit acceptance.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ProblemClass = Literal[
    'c_small',
    'c_medium',
    'c_large',
    'c_outside_local_envelope',
]

ExecutionBackend = Literal['cpu', 'gpu']

FidelityStage = Literal['screening', 'medium', 'high_fidelity']

AdaptationKind = Literal[
    'equivalent_execution',
    'lower_fidelity_proposal',
    'fail_closed',
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


class WorkloadSpec(BaseModel):
    """Canonical cost-driving request parameters (#778 §2).

    Every field a user chose that drives cost appears here; the semantic
    prediction request itself stays a separate authority.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    provider_id: str = Field(min_length=1)
    backend: ExecutionBackend = 'gpu'
    upper_frequency_hz: float = Field(gt=0.0)
    mesh_resolution_m: float = Field(gt=0.0)
    domain_volume_m3: float = Field(gt=0.0)
    geometry_triangle_count: int = Field(ge=0)
    source_count: int = Field(ge=1)
    receiver_count: int = Field(ge=1)
    field_sample_count: int = Field(ge=0)
    candidate_count: int = Field(ge=1)
    boundary_model: str = 'standard'

    def spec_digest(self) -> str:
        return sha256(
            _canonical(self.model_dump(mode='json')).encode('utf-8')
        ).hexdigest()


class HardwareEnvelope(BaseModel):
    """Declared limits of a Windows profile (#778 §6)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    profile_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    cpu_cores: int = Field(ge=1)
    ram_mb: int = Field(gt=0)
    vram_mb: int = Field(gt=0)
    scratch_mb: int = Field(gt=0)
    gpu_available: bool = True


# Reference classes, not timing guarantees (docs/ §6).
REFERENCE_HARDWARE_PROFILES: tuple[HardwareEnvelope, ...] = (
    HardwareEnvelope(
        profile_id='ref-minimum-supported',
        label='Minimum supported Windows profile',
        cpu_cores=4,
        ram_mb=8 * 1024,
        vram_mb=2 * 1024,
        scratch_mb=16 * 1024,
        gpu_available=True,
    ),
    HardwareEnvelope(
        profile_id='ref-enthusiast',
        label='Typical enthusiast PC',
        cpu_cores=8,
        ram_mb=32 * 1024,
        vram_mb=8 * 1024,
        scratch_mb=64 * 1024,
        gpu_available=True,
    ),
    HardwareEnvelope(
        profile_id='ref-workstation',
        label='High-end local GPU workstation',
        cpu_cores=16,
        ram_mb=128 * 1024,
        vram_mb=24 * 1024,
        scratch_mb=256 * 1024,
        gpu_available=True,
    ),
)


class ResourceEstimate(BaseModel):
    """Heuristic preflight estimate — prevents crash-first behavior, not a
    promised ETA."""

    model_config = ConfigDict(frozen=True)

    estimated_ram_mb: float = Field(ge=0.0)
    estimated_vram_mb: float = Field(ge=0.0)
    estimated_scratch_mb: float = Field(ge=0.0)
    wave_cell_count: float = Field(ge=0.0)
    dominant_drivers: tuple[str, ...] = ()


def _wave_cells(spec: WorkloadSpec) -> float:
    """Grid cells grow with volume/resolution³ and with frequency: each
    wavelength span needs a fixed number of cells, so cost scales with
    (upper_frequency)³ for a fixed resolution-per-wavelength."""
    base_cells = spec.domain_volume_m3 / (spec.mesh_resolution_m ** 3)
    frequency_factor = max(1.0, spec.upper_frequency_hz / 100.0) ** 3
    return base_cells * frequency_factor


def estimate_workload(spec: WorkloadSpec) -> ResourceEstimate:
    """Deterministic heuristic estimate over the cost dimensions."""

    cells = _wave_cells(spec)
    # Per-cell solver state (~96 B) scaled by sources; field output and
    # candidate batches scale linearly.
    ram_mb = (
        512.0
        + cells * 96.0e-6 * spec.source_count
        + spec.receiver_count * spec.field_sample_count * 32.0e-6
        + spec.candidate_count * 8.0
    )
    vram_mb = (
        512.0 + cells * 128.0e-6 * spec.source_count
        if spec.backend == 'gpu'
        else 0.0
    )
    scratch_mb = (
        256.0
        + spec.field_sample_count * spec.receiver_count * 32.0e-6
        + cells * 16.0e-6
    )
    drivers: list[tuple[str, float]] = [
        ('upper_frequency_hz', spec.upper_frequency_hz / 100.0),
        ('mesh_resolution_m', 1.0 / spec.mesh_resolution_m),
        ('source_count', float(spec.source_count)),
        ('receiver_count', float(spec.receiver_count)),
        ('field_sample_count', float(spec.field_sample_count)),
        ('candidate_count', float(spec.candidate_count)),
        ('geometry_triangle_count', spec.geometry_triangle_count / 1000.0),
    ]
    dominant = tuple(
        name
        for name, _ in sorted(
            drivers, key=lambda item: -item[1]
        )[:3]
    )
    return ResourceEstimate(
        estimated_ram_mb=ram_mb,
        estimated_vram_mb=vram_mb,
        estimated_scratch_mb=scratch_mb,
        wave_cell_count=cells,
        dominant_drivers=dominant,
    )


class PreflightReport(BaseModel):
    """The §3 preflight card: what the user picked, what it costs, and
    which product envelope class it lands in."""

    model_config = ConfigDict(frozen=True)

    spec: WorkloadSpec
    estimate: ResourceEstimate
    problem_class: ProblemClass
    execution_mode: ExecutionBackend
    supported: bool
    detail: str = ''


def _headroom(
    estimate: ResourceEstimate, envelope: HardwareEnvelope
) -> float:
    """Smallest headroom ratio across the constrained resources."""
    ratios = [
        envelope.ram_mb / max(estimate.estimated_ram_mb, 1.0),
        envelope.scratch_mb / max(estimate.estimated_scratch_mb, 1.0),
    ]
    if estimate.estimated_vram_mb > 0:
        ratios.append(
            envelope.vram_mb / max(estimate.estimated_vram_mb, 1.0)
        )
    return min(ratios)


def classify_workload(
    spec: WorkloadSpec, envelope: HardwareEnvelope
) -> tuple[ProblemClass, ResourceEstimate]:
    """Map the estimate onto the bounded product classes."""
    estimate = estimate_workload(spec)
    headroom = _headroom(estimate, envelope)
    if headroom < 1.0:
        problem_class: ProblemClass = 'c_outside_local_envelope'
    elif headroom < 2.0:
        problem_class = 'c_large'
    elif headroom < 8.0:
        problem_class = 'c_medium'
    else:
        problem_class = 'c_small'
    return problem_class, estimate


def preflight_workload(
    spec: WorkloadSpec, envelope: HardwareEnvelope
) -> PreflightReport:
    """Run estimation before any allocation — the §3 card."""
    problem_class, estimate = classify_workload(spec, envelope)
    execution_mode = spec.backend
    if spec.backend == 'gpu' and not envelope.gpu_available:
        execution_mode = 'cpu'
    supported = problem_class != 'c_outside_local_envelope'
    detail = (
        f'envelope {envelope.profile_id}: '
        f'RAM {estimate.estimated_ram_mb:.0f}/{envelope.ram_mb} MB, '
        f'VRAM {estimate.estimated_vram_mb:.0f}/{envelope.vram_mb} MB, '
        f'scratch {estimate.estimated_scratch_mb:.0f}/'
        f'{envelope.scratch_mb} MB'
    )
    return PreflightReport(
        spec=spec,
        estimate=estimate,
        problem_class=problem_class,
        execution_mode=execution_mode,
        supported=supported,
        detail=detail,
    )


class AdaptationProposal(BaseModel):
    """A bounded degradation response (#778 §4).

    ``equivalent_execution`` preserves the semantic request identity
    (e.g. GPU->CPU, chunked receivers). ``lower_fidelity_proposal`` shows
    requested vs. executed spec plus impact — it requires explicit user
    acceptance unless the preset already authorizes bounded adaptation.
    ``fail_closed`` carries no executed spec.
    """

    model_config = ConfigDict(frozen=True)

    kind: AdaptationKind
    requested_spec: WorkloadSpec
    executed_spec: WorkloadSpec | None = None
    capability_impact: str = ''
    resolution_impact: str = ''
    requires_user_acceptance: bool = False
    detail: str = ''

    @model_validator(mode='after')
    def valid_proposal(self) -> 'AdaptationProposal':
        if self.kind == 'equivalent_execution':
            if self.executed_spec is None:
                raise ValueError(
                    'equivalent execution requires an executed spec'
                )
            if self.requires_user_acceptance:
                raise ValueError(
                    'equivalent execution must not require acceptance — '
                    'it preserves semantics'
                )
        elif self.kind == 'lower_fidelity_proposal':
            if self.executed_spec is None:
                raise ValueError(
                    'a lower-fidelity proposal must show the executed spec'
                )
            if not self.requires_user_acceptance:
                raise ValueError(
                    'a quality-changing adaptation always requires '
                    'explicit acceptance'
                )
        else:
            if self.executed_spec is not None:
                raise ValueError(
                    'fail_closed must not carry an executed spec — do not '
                    'save a lower-quality solve as the original request'
                )
        return self


def _equivalent_spec(spec: WorkloadSpec) -> WorkloadSpec:
    """GPU->CPU: same problem, different execution path."""
    return spec.model_copy(update={'backend': 'cpu'})


def _reduced_fidelity_spec(spec: WorkloadSpec) -> WorkloadSpec:
    """Representative bounded reduction: coarser grid + lower upper band +
    half the field samples, rounded through the same model."""
    return spec.model_copy(
        update={
            'upper_frequency_hz': spec.upper_frequency_hz * 0.5,
            'mesh_resolution_m': spec.mesh_resolution_m * 1.5,
            'field_sample_count': spec.field_sample_count // 2,
        }
    )


def plan_adaptation(
    spec: WorkloadSpec,
    envelope: HardwareEnvelope,
) -> AdaptationProposal:
    """Bounded §4 response to an out-of-envelope or heavy workload.

    Order: equivalent execution → lower-fidelity proposal → fail closed.
    """

    report = preflight_workload(spec, envelope)
    if report.supported:
        # Still answer the equivalent-execution question when the user
        # asked for GPU without a GPU (semantic identity preserved).
        if spec.backend == 'gpu' and not envelope.gpu_available:
            return AdaptationProposal(
                kind='equivalent_execution',
                requested_spec=spec,
                executed_spec=_equivalent_spec(spec),
                detail='GPU unavailable — CPU execution preserves semantics',
            )
        return AdaptationProposal(
            kind='equivalent_execution',
            requested_spec=spec,
            executed_spec=spec,
            detail='workload fits the local envelope as requested',
        )

    # Outside the envelope: try an equivalent execution change first.
    if spec.backend == 'gpu':
        cpu_spec = _equivalent_spec(spec)
        cpu_estimate = estimate_workload(cpu_spec)
        cpu_class, _ = classify_workload(cpu_spec, envelope)
        if cpu_class != 'c_outside_local_envelope':
            return AdaptationProposal(
                kind='equivalent_execution',
                requested_spec=spec,
                executed_spec=cpu_spec,
                detail=(
                    'GPU path exceeds the local envelope; CPU execution '
                    f'estimates {cpu_estimate.estimated_ram_mb:.0f} MB RAM '
                    'and fits'
                ),
            )

    # Then a bounded lower-fidelity proposal — never applied silently.
    reduced = _reduced_fidelity_spec(spec)
    reduced_report = preflight_workload(reduced, envelope)
    if reduced_report.supported:
        return AdaptationProposal(
            kind='lower_fidelity_proposal',
            requested_spec=spec,
            executed_spec=reduced,
            capability_impact=(
                'upper band and field density reduced — the executed '
                'result must not be presented as the requested solve'
            ),
            resolution_impact=(
                f'frequency {spec.upper_frequency_hz:.0f}→'
                f'{reduced.upper_frequency_hz:.0f} Hz, '
                f'grid {spec.mesh_resolution_m:.3f}→'
                f'{reduced.mesh_resolution_m:.3f} m'
            ),
            requires_user_acceptance=True,
            detail='bounded fidelity reduction fits the local envelope',
        )

    return AdaptationProposal(
        kind='fail_closed',
        requested_spec=spec,
        capability_impact=(
            'requested capability cannot be preserved within the declared '
            'safe/validated local envelope'
        ),
        detail=(
            f'{report.detail}; dominant drivers: '
            f'{", ".join(report.estimate.dominant_drivers)}'
        ),
    )


FIDELITY_STAGE_LABELS: dict[FidelityStage, str] = {
    'screening': 'Screening — cheap first pass, not the final prediction',
    'medium': 'Medium fidelity — reduced candidate set evaluation',
    'high_fidelity': 'High fidelity — final solve on finalists',
}


def fidelity_stage_label(stage: FidelityStage) -> str:
    """Every result must carry its stage — a screening result is never
    presented as the high-fidelity prediction (#778 §5)."""
    return FIDELITY_STAGE_LABELS[stage]


class BenchmarkRecord(BaseModel):
    """Deterministic per-case performance evidence (#778 §7).

    This is performance evidence only — never acoustic validation
    evidence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    benchmark_id: str = Field(min_length=1)
    case_name: str = Field(min_length=1)
    spec_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    hardware_profile_id: str = Field(min_length=1)
    backend: ExecutionBackend
    solver_identity: str = Field(min_length=1)
    runtime_s: float = Field(ge=0.0)
    peak_memory_mb: float = Field(ge=0.0)
    output_size_mb: float = Field(ge=0.0)
    recorded_at_utc: str = Field(min_length=1)
    detail: str = ''


class ComputeEvidenceRepository:
    """Append-only benchmark evidence store on the shared cad DB."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_compute_benchmarks (
                    benchmark_id TEXT PRIMARY KEY,
                    case_name TEXT NOT NULL,
                    spec_digest TEXT NOT NULL,
                    hardware_profile_id TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    solver_identity TEXT NOT NULL,
                    runtime_s REAL NOT NULL,
                    peak_memory_mb REAL NOT NULL,
                    output_size_mb REAL NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def save_record(self, record: BenchmarkRecord) -> BenchmarkRecord:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_compute_benchmarks('
                'benchmark_id, case_name, spec_digest, hardware_profile_id,'
                ' backend, solver_identity, runtime_s, peak_memory_mb,'
                ' output_size_mb, recorded_at_utc, payload_json) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(benchmark_id) DO NOTHING',
                (
                    record.benchmark_id,
                    record.case_name,
                    record.spec_digest,
                    record.hardware_profile_id,
                    record.backend,
                    record.solver_identity,
                    record.runtime_s,
                    record.peak_memory_mb,
                    record.output_size_mb,
                    record.recorded_at_utc,
                    record.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_compute_benchmarks '
                'WHERE benchmark_id=?',
                (record.benchmark_id,),
            ).fetchone()
            if row['payload_json'] != record.model_dump_json():
                raise ValueError(
                    f'benchmark {record.benchmark_id} already persisted '
                    'with different content'
                )
        return record

    def get_record(self, benchmark_id: str) -> BenchmarkRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_compute_benchmarks '
                'WHERE benchmark_id=?',
                (benchmark_id,),
            ).fetchone()
        if row is None:
            return None
        return BenchmarkRecord.model_validate_json(row['payload_json'])

    def list_records_for_case(
        self, case_name: str
    ) -> tuple[BenchmarkRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_compute_benchmarks '
                'WHERE case_name=? '
                'ORDER BY recorded_at_utc ASC, benchmark_id ASC',
                (case_name,),
            ).fetchall()
        return tuple(
            BenchmarkRecord.model_validate_json(row['payload_json'])
            for row in rows
        )


def record_benchmark(
    spec: WorkloadSpec,
    envelope: HardwareEnvelope,
    *,
    case_name: str,
    solver_identity: str,
    runtime_s: float,
    peak_memory_mb: float,
    output_size_mb: float,
    backend: ExecutionBackend | None = None,
    benchmark_id: str | None = None,
    detail: str = '',
    recorded_at_utc: str | None = None,
) -> BenchmarkRecord:
    """Stamp deterministic benchmark evidence pinned to the spec digest."""
    return BenchmarkRecord(
        benchmark_id=benchmark_id or f'benchmark-{sha256(_canonical({"case": case_name, "digest": spec.spec_digest(), "runtime": runtime_s}).encode()).hexdigest()[:16]}',
        case_name=case_name,
        spec_digest=spec.spec_digest(),
        hardware_profile_id=envelope.profile_id,
        backend=backend or spec.backend,
        solver_identity=solver_identity,
        runtime_s=runtime_s,
        peak_memory_mb=peak_memory_mb,
        output_size_mb=output_size_mb,
        recorded_at_utc=recorded_at_utc or _utc_now(),
        detail=detail,
    )


__all__ = [
    'AdaptationKind',
    'AdaptationProposal',
    'BenchmarkRecord',
    'ComputeEvidenceRepository',
    'ExecutionBackend',
    'FIDELITY_STAGE_LABELS',
    'FidelityStage',
    'HardwareEnvelope',
    'PreflightReport',
    'ProblemClass',
    'REFERENCE_HARDWARE_PROFILES',
    'ResourceEstimate',
    'WorkloadSpec',
    'classify_workload',
    'estimate_workload',
    'fidelity_stage_label',
    'plan_adaptation',
    'preflight_workload',
    'record_benchmark',
]
