from __future__ import annotations

import argparse
from hashlib import sha256
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import threading
import time

import psutil

from htdt.acoustic_bakeoff import (
    BakeoffFixtureEvidence,
    BakeoffHardGateEvidence,
    BakeoffPlatform,
    BakeoffRun,
    load_bakeoff_candidate_manifest,
    validate_bakeoff_run,
)
from htdt.acoustic_bakeoff_observation import (
    RawFixtureObservation,
    RawObservableObservation,
    RawObservationSample,
    evaluate_sampled_fixture,
)
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest


CANDIDATE_ID = 'mfem-v4.10-d964264'
FIXTURE_ID = 'wave-explicit-radiation-termination-v1'
ADAPTER_ID = 'htdt-r100b-mfem-radiation-termination'
ADAPTER_VERSION = '2'
ARTIFACT_SCHEMA = 'r100b-mfem-radiation-termination-artifact-1'

ORDER_MIN = 2
ORDER_MAX = 5
MESH_DIVISIONS = (6, 4, 2)
EXPECTED_ELEMENTS = MESH_DIVISIONS[0] * MESH_DIVISIONS[1] * MESH_DIVISIONS[2]
EXPECTED_RADIATION_FACES = MESH_DIVISIONS[1] * MESH_DIVISIONS[2]
EXPECTED_BOUNDARY_FACES = 2 * (
    MESH_DIVISIONS[1] * MESH_DIVISIONS[2]
    + MESH_DIVISIONS[0] * MESH_DIVISIONS[2]
    + MESH_DIVISIONS[0] * MESH_DIVISIONS[1]
)
SOLVER_RESIDUAL_LIMIT = 1.0e-8
CONVERGENCE_QUALIFICATION_FRACTION = 0.5


class ProcessPeakRssMonitor:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self.peak_bytes = 0

    def _sample(self) -> None:
        while not self._stop.wait(0.01):
            try:
                root = psutil.Process(self.process.pid)
                total = root.memory_info().rss
                for child in root.children(recursive=True):
                    try:
                        total += child.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                self.peak_bytes = max(self.peak_bytes, total)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> float:
        self._stop.set()
        self._thread.join(timeout=1.0)
        return self.peak_bytes / (1024.0 * 1024.0)


def _candidate(candidates):
    return next(item for item in candidates.candidates if item.candidate_id == CANDIDATE_ID)


def _fixture(benchmark):
    return next(item for item in benchmark.fixtures if item.fixture_id == FIXTURE_ID)


def _position_tuple(position) -> tuple[float, float, float]:
    return (float(position.x_m), float(position.y_m), float(position.z_m))


def _git_head(path: Path) -> str:
    return subprocess.check_output(
        ['git', '-C', str(path), 'rev-parse', 'HEAD'],
        text=True,
    ).strip().lower()


def _git_rev_parse(revision: str) -> str | None:
    completed = subprocess.run(
        ['git', 'rev-parse', '--verify', revision],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip().lower()
    return value or None


def _htdt_git_provenance() -> dict[str, str]:
    checkout = _git_rev_parse('HEAD')
    if checkout is None:
        raise RuntimeError('cannot resolve HTDT checkout commit')
    event_head = os.environ.get('HTDT_PR_HEAD_SHA', '').strip().lower()
    if len(event_head) == 40 and all(ch in '0123456789abcdef' for ch in event_head):
        pr_head = event_head
    else:
        pr_head = _git_rev_parse('HEAD^2') or checkout
    return {
        'checkout_commit_sha': checkout,
        'pr_head_commit_sha': pr_head,
    }


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return sum(
        item.stat().st_size
        for item in path.rglob('*')
        if item.is_file()
    ) / (1024.0 * 1024.0)


def _runtime_versions() -> dict[str, str]:
    result = {'python': platform.python_version()}
    for name in ('pydantic', 'psutil'):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = 'not-installed'
    return result


def _platform(thread_budget: int) -> BakeoffPlatform:
    logical_threads = os.cpu_count() or 1
    return BakeoffPlatform(
        os=platform.platform(),
        architecture=platform.machine() or 'unknown',
        python_version=platform.python_version(),
        cpu=platform.processor() or os.environ.get('PROCESSOR_IDENTIFIER', 'unknown'),
        logical_threads=logical_threads,
        thread_budget=min(thread_budget, logical_threads),
        gpu=None,
        device_notes=(
            'GitHub-hosted Windows MFEM v4.10 serial H1 complex-Robin radiation probe.'
        ),
    )


def _hard_gates(
    evidence_ref: str,
    *,
    reproducible: bool,
) -> tuple[BakeoffHardGateEvidence, ...]:
    categories = (
        'physics_correctness',
        'cpu_baseline',
        'windows_packaging',
        'license_redistribution',
        'required_capability',
        'reproducible_authority',
    )
    result: list[BakeoffHardGateEvidence] = []
    for category in categories:
        if category == 'reproducible_authority' and reproducible:
            result.append(
                BakeoffHardGateEvidence(
                    category=category,
                    status='pass',
                    evidence_ref=evidence_ref,
                    summary=(
                        'Pinned MFEM source, exact R100A-3/candidate hashes, Robin block '
                        'operator, p-refinement, raw samples and source/resource provenance '
                        'are recorded.'
                    ),
                )
            )
        else:
            result.append(
                BakeoffHardGateEvidence(
                    category=category,
                    status='not_run',
                    summary=(
                        'Candidate-wide hard gate remains open; this slice evaluates only '
                        'the frozen R100A-3 radiation-termination fixture.'
                    ),
                )
            )
    return tuple(result)


def _validate_authority(benchmark, fixture) -> None:
    expected_revision = {'r100a-3': 3, 'r100a-4': 4}.get(benchmark.schema_version)
    if expected_revision is None or benchmark.revision != expected_revision:
        raise ValueError('MFEM radiation probe requires R100A-3/4 with matching revision')
    if tuple(fixture.required_capabilities) != (
        'wave_rigid',
        'wave_radiation_termination',
    ):
        raise ValueError('radiation fixture capability authority changed')
    if len(fixture.regions) != 1 or fixture.regions[0].region_id != 'room':
        raise ValueError('radiation fixture region authority changed')

    expected_vertices = {
        'room-v000': (0.0, 0.0, 0.0),
        'room-v100': (6.0, 0.0, 0.0),
        'room-v110': (6.0, 4.0, 0.0),
        'room-v010': (0.0, 4.0, 0.0),
        'room-v001': (0.0, 0.0, 2.5),
        'room-v101': (6.0, 0.0, 2.5),
        'room-v111': (6.0, 4.0, 2.5),
        'room-v011': (0.0, 4.0, 2.5),
    }
    region = fixture.regions[0]
    actual_vertices = {
        item.vertex_id: _position_tuple(item.position) for item in region.vertices
    }
    if actual_vertices != expected_vertices:
        raise ValueError('radiation fixture geometry authority changed')

    expected_face_boundaries = {
        'room-zmin': 'b-rigid',
        'room-zmax': 'b-rigid',
        'room-ymin': 'b-rigid',
        'room-ymax': 'b-rigid',
        'room-xmin': 'b-rigid',
        'room-xmax': 'b-interface',
    }
    actual_face_boundaries = {
        item.face_id: item.boundary_id for item in region.faces
    }
    if actual_face_boundaries != expected_face_boundaries:
        raise ValueError('radiation face/boundary mapping authority changed')

    material_by_id = {item.material_id: item for item in fixture.materials}
    boundary_by_id = {item.boundary_id: item for item in fixture.boundaries}
    if (
        boundary_by_id['b-rigid'].material_id not in material_by_id
        or material_by_id[boundary_by_id['b-rigid'].material_id].wave_model != 'rigid'
        or boundary_by_id['b-interface'].material_id not in material_by_id
        or material_by_id[boundary_by_id['b-interface'].material_id].wave_model != 'unsupported'
    ):
        raise ValueError('radiation material/boundary authority changed')

    if len(fixture.terminations) != 1:
        raise ValueError('radiation fixture must contain one termination')
    termination = fixture.terminations[0]
    if (
        termination.termination_id != 'open-right'
        or termination.region_id != 'room'
        or termination.kind != 'radiation'
        or termination.boundary_id != 'b-interface'
        or termination.radiation_model != 'local_first_order_outgoing'
        or termination.normal_convention != 'outward_from_region'
        or termination.characteristic_impedance_model != 'rho_c_from_environment'
        or termination.pressure_velocity_equation != 'p_eq_rho_c_u_n'
        or termination.wavenumber_equation != 'k_eq_omega_over_c'
        or termination.helmholtz_robin_equation != 'dp_dn_minus_i_k_p_eq_0'
    ):
        raise ValueError('radiation termination mathematical authority changed')
    expected_aperture = (
        (6.0, 0.0, 0.0),
        (6.0, 0.0, 2.5),
        (6.0, 4.0, 2.5),
        (6.0, 4.0, 0.0),
    )
    if tuple(_position_tuple(item) for item in termination.aperture) != expected_aperture:
        raise ValueError('radiation termination aperture authority changed')

    if (
        float(fixture.environment.density_kg_m3) != 1.2
        or float(fixture.environment.sound_speed_m_s) != 343.0
    ):
        raise ValueError('radiation environment authority changed')

    comparison = fixture.comparison
    if (
        comparison.coordinate_system != 'x_right_y_rear_z_up'
        or comparison.length_unit != 'm'
        or comparison.pressure_unit != 'Pa'
        or comparison.fourier_sign != 'exp(-i*omega*t)'
        or comparison.phase_wrap != '[-180,180)'
        or comparison.time_zero_reference != 'source_excitation_t0'
        or comparison.floating_point != 'float64'
        or comparison.interpolation != 'linear_complex'
        or comparison.window != 'none'
        or comparison.filter != 'none'
    ):
        raise ValueError('radiation comparison authority changed')

    grid = comparison.frequency_grid
    if (
        grid.kind != 'uniform'
        or float(grid.start_hz) != 20.0
        or float(grid.stop_hz) != 300.0
        or float(grid.step_hz) != 1.0
    ):
        raise ValueError('radiation frequency-grid authority changed')

    if len(fixture.sources) != 1 or len(fixture.receivers) != 1:
        raise ValueError('radiation fixture requires exactly one source and receiver')
    source = fixture.sources[0]
    receiver = fixture.receivers[0]
    if (
        _position_tuple(source.position) != (1.0, 2.0, 1.0)
        or source.normalization != 'volume_velocity_m3_s'
        or float(source.amplitude) != 1.0
        or float(source.phase_deg) != 0.0
        or source.directivity != 'omnidirectional'
        or _position_tuple(receiver.position) != (5.0, 2.0, 1.0)
        or receiver.calibration_state != 'ideal_flat'
        or receiver.calibration_profile_id is not None
        or receiver.timing_reference != 'source_t0'
    ):
        raise ValueError('radiation source/receiver authority changed')

    observable_by_id = {item.observable_id: item for item in fixture.observables}
    if set(observable_by_id) != {'termination-fr', 'termination-complex-pressure'}:
        raise ValueError('radiation observable authority changed')
    if observable_by_id['termination-fr'].unit != 'dB re 1 Pa/(m3/s)':
        raise ValueError('radiation transfer-magnitude unit changed')
    if observable_by_id['termination-complex-pressure'].unit != 'Pa':
        raise ValueError('radiation complex-pressure unit changed')


def _expected_frequency_grid(fixture) -> list[float]:
    grid = fixture.comparison.frequency_grid
    count = int(round((float(grid.stop_hz) - float(grid.start_hz)) / float(grid.step_hz))) + 1
    return [float(grid.start_hz) + index * float(grid.step_hz) for index in range(count)]


def _complex(sample: dict[str, object]) -> complex:
    return complex(float(sample['pressure_real_pa']), float(sample['pressure_imag_pa']))


def _wrapped_phase_deg(value: complex) -> float:
    return math.degrees(math.atan2(value.imag, value.real))


def _wrapped_phase_delta_deg(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _validate_raw(
    fixture,
    raw: dict[str, object],
) -> tuple[list[dict[str, object]], list[str]]:
    if raw.get('schema_version') != 'r100b-mfem-radiation-transfer-raw-1':
        raise ValueError('unexpected MFEM radiation raw schema')
    if raw.get('compiled_geometry') != '6x4x2.5-structured-6x4x2-hex':
        raise ValueError('MFEM radiation compiled geometry changed')
    if raw.get('fourier_sign') != fixture.comparison.fourier_sign:
        raise ValueError('MFEM radiation Fourier authority changed')
    if raw.get('radiation_normal') != 'outward-from-region-xmax':
        raise ValueError('MFEM radiation outward-normal authority changed')
    if raw.get('radiation_boundary_equation') != 'dp_dn_minus_i_k_p_eq_0':
        raise ValueError('MFEM radiation Robin equation changed')
    if raw.get('complex_operator') != 'K-k^2M-i*k*B_rad':
        raise ValueError('MFEM radiation complex operator changed')
    if raw.get('real_block_operator') != '[A0,+kB;-kB,A0]':
        raise ValueError('MFEM radiation real-block sign pattern changed')
    if raw.get('source_rhs') != '[0,-omega*rho*Q*f]':
        raise ValueError('MFEM radiation source RHS sign changed')
    if [int(item) for item in raw.get('mesh_divisions', [])] != list(MESH_DIVISIONS):
        raise ValueError('MFEM radiation mesh divisions changed')
    if float(raw.get('density_kg_m3', -1.0)) != float(fixture.environment.density_kg_m3):
        raise ValueError('MFEM radiation density changed')
    if float(raw.get('sound_speed_m_s', -1.0)) != float(fixture.environment.sound_speed_m_s):
        raise ValueError('MFEM radiation sound speed changed')

    source = fixture.sources[0]
    receiver = fixture.receivers[0]
    if tuple(float(item) for item in raw.get('source_position_m', [])) != _position_tuple(source.position):
        raise ValueError('MFEM radiation source position changed')
    if tuple(float(item) for item in raw.get('receiver_position_m', [])) != _position_tuple(receiver.position):
        raise ValueError('MFEM radiation receiver position changed')
    if float(raw.get('source_amplitude_m3_s', -1.0)) != float(source.amplitude):
        raise ValueError('MFEM radiation source amplitude changed')

    expected_grid = _expected_frequency_grid(fixture)
    orders = raw.get('orders')
    if not isinstance(orders, list):
        raise ValueError('raw MFEM radiation output is missing p-refinement orders')
    expected_orders = list(range(ORDER_MIN, ORDER_MAX + 1))
    actual_orders = [int(item['order']) for item in orders]
    if actual_orders != expected_orders:
        raise ValueError(f'expected p-refinement orders {expected_orders}, got {actual_orders}')

    previous_ndofs = 0
    solver_violations: list[str] = []
    for level in orders:
        if int(level.get('elements', 0)) != EXPECTED_ELEMENTS:
            raise ValueError('MFEM radiation mesh element count changed')
        if int(level.get('boundary_faces', 0)) != EXPECTED_BOUNDARY_FACES:
            raise ValueError('MFEM radiation boundary-face count changed')
        if int(level.get('radiation_boundary_faces', 0)) != EXPECTED_RADIATION_FACES:
            raise ValueError('MFEM radiation x=6 boundary participation changed')
        ndofs = int(level.get('ndofs', 0))
        if ndofs <= previous_ndofs:
            raise ValueError('MFEM radiation p-refinement DOF count is not increasing')
        previous_ndofs = ndofs

        samples = level.get('samples')
        if not isinstance(samples, list) or len(samples) != len(expected_grid):
            raise ValueError('MFEM radiation output does not cover the frozen frequency grid')
        for sample, expected_frequency in zip(samples, expected_grid, strict=True):
            actual_frequency = float(sample['frequency_hz'])
            if abs(actual_frequency - expected_frequency) > max(
                1.0e-9, abs(expected_frequency) * 1.0e-12
            ):
                raise ValueError(
                    f'MFEM radiation frequency mismatch: {actual_frequency} != {expected_frequency}'
                )
            residual = float(sample['relative_residual'])
            pressure_real = float(sample['pressure_real_pa'])
            pressure_imag = float(sample['pressure_imag_pa'])
            converged = bool(sample.get('converged', False))
            if not math.isfinite(residual) or not math.isfinite(pressure_real) or not math.isfinite(pressure_imag):
                solver_violations.append(
                    f'p{int(level["order"])} {actual_frequency:g} Hz produced non-finite solver output'
                )
            if not converged:
                solver_violations.append(
                    f'p{int(level["order"])} {actual_frequency:g} Hz GMRES did not converge'
                )
            if residual > SOLVER_RESIDUAL_LIMIT:
                solver_violations.append(
                    f'p{int(level["order"])} {actual_frequency:g} Hz residual {residual} '
                    f'exceeds fixed {SOLVER_RESIDUAL_LIMIT} qualification limit'
                )
    return orders, solver_violations


def _pair_metrics(
    fixture,
    coarse: dict[str, object],
    fine: dict[str, object],
) -> dict[str, float | int]:
    observable_by_id = {item.observable_id: item for item in fixture.observables}
    magnitude = observable_by_id['termination-fr']
    complex_pressure = observable_by_id['termination-complex-pressure']
    magnitude_mask_db = float(magnitude.tolerance.null_mask_below_db)
    phase_mask_db = float(complex_pressure.tolerance.null_mask_below_db)
    source_q = float(fixture.sources[0].amplitude)

    magnitude_abs: list[float] = []
    phase_error: list[float] = []
    squared_diff = 0.0
    squared_reference = 0.0

    for coarse_sample, fine_sample in zip(
        coarse['samples'], fine['samples'], strict=True
    ):
        coarse_p = _complex(coarse_sample)
        fine_p = _complex(fine_sample)
        difference = coarse_p - fine_p
        squared_diff += abs(difference) ** 2
        squared_reference += abs(fine_p) ** 2

        coarse_db = 20.0 * math.log10(max(abs(coarse_p) / source_q, 1.0e-300))
        fine_db = 20.0 * math.log10(max(abs(fine_p) / source_q, 1.0e-300))
        if fine_db >= magnitude_mask_db:
            magnitude_abs.append(abs(coarse_db - fine_db))
        if fine_db >= phase_mask_db and abs(coarse_p) > 0.0 and abs(fine_p) > 0.0:
            phase_error.append(
                _wrapped_phase_delta_deg(
                    _wrapped_phase_deg(coarse_p),
                    _wrapped_phase_deg(fine_p),
                )
            )

    if not magnitude_abs or not phase_error:
        raise ValueError('radiation null masks removed all p-refinement samples')

    return {
        'coarse_order': int(coarse['order']),
        'fine_order': int(fine['order']),
        'magnitude_max_abs_db': max(magnitude_abs),
        'phase_max_error_deg': max(phase_error),
        'complex_rms_relative': math.sqrt(
            squared_diff / max(squared_reference, 1.0e-300)
        ),
        'magnitude_sample_count': len(magnitude_abs),
        'phase_sample_count': len(phase_error),
    }


def _convergence_evidence(
    fixture,
    orders: list[dict[str, object]],
) -> tuple[list[dict[str, float | int]], dict[str, object]]:
    pair_metrics = [
        _pair_metrics(fixture, orders[index], orders[index + 1])
        for index in range(len(orders) - 1)
    ]
    sequence = [float(item['complex_rms_relative']) for item in pair_metrics]
    monotonic = all(
        sequence[index] > sequence[index + 1]
        for index in range(len(sequence) - 1)
    )
    final = pair_metrics[-1]
    observable_by_id = {item.observable_id: item for item in fixture.observables}
    magnitude = observable_by_id['termination-fr']
    complex_pressure = observable_by_id['termination-complex-pressure']

    magnitude_limit = (
        float(magnitude.tolerance.absolute) * CONVERGENCE_QUALIFICATION_FRACTION
    )
    complex_limit = (
        float(complex_pressure.tolerance.relative) * CONVERGENCE_QUALIFICATION_FRACTION
    )
    phase_limit = (
        float(complex_pressure.tolerance.phase_deg) * CONVERGENCE_QUALIFICATION_FRACTION
    )

    violations: list[str] = []
    if not monotonic:
        violations.append('complex RMS p-refinement error is not strictly decreasing')
    if float(final['magnitude_max_abs_db']) > magnitude_limit:
        violations.append(
            f'final p4->p5 magnitude delta {final["magnitude_max_abs_db"]} dB '
            f'exceeds fixed qualification {magnitude_limit} dB'
        )
    if float(final['complex_rms_relative']) > complex_limit:
        violations.append(
            f'final p4->p5 complex RMS relative delta {final["complex_rms_relative"]} '
            f'exceeds fixed qualification {complex_limit}'
        )
    if float(final['phase_max_error_deg']) > phase_limit:
        violations.append(
            f'final p4->p5 phase delta {final["phase_max_error_deg"]} deg '
            f'exceeds fixed qualification {phase_limit} deg'
        )

    qualification = {
        'qualified': not violations,
        'qualification_fraction_of_frozen_tolerance': CONVERGENCE_QUALIFICATION_FRACTION,
        'complex_rms_relative_strictly_decreasing': monotonic,
        'complex_rms_relative_sequence': sequence,
        'limits': {
            'magnitude_abs_db': magnitude_limit,
            'complex_rms_relative': complex_limit,
            'phase_deg': phase_limit,
        },
        'violations': violations,
    }
    return pair_metrics, qualification


def _raw_finest_observation(
    fixture,
    finest: dict[str, object],
    *,
    evidence_ref: str,
    backend_version: str,
    compile_s: float,
    solve_s: float,
    postprocess_s: float,
    peak_ram_mb: float,
    disk_mb: float,
    output_mb: float,
) -> RawFixtureObservation:
    source_q = float(fixture.sources[0].amplitude)
    magnitude_samples: list[RawObservationSample] = []
    complex_samples: list[RawObservationSample] = []
    for sample in finest['samples']:
        frequency_hz = float(sample['frequency_hz'])
        integer_hz = int(round(frequency_hz))
        pressure = _complex(sample)
        magnitude_samples.append(
            RawObservationSample(
                sample_key=f'mag@{integer_hz}Hz',
                frequency_hz=frequency_hz,
                scalar_value=20.0 * math.log10(
                    max(abs(pressure) / source_q, 1.0e-300)
                ),
            )
        )
        complex_samples.append(
            RawObservationSample(
                sample_key=f'P@{integer_hz}Hz',
                frequency_hz=frequency_hz,
                real_value=pressure.real,
                imag_value=pressure.imag,
            )
        )

    expected_by_id = {item.observable_id: item for item in fixture.observables}
    return RawFixtureObservation(
        fixture_id=fixture.fixture_id,
        evidence_ref=evidence_ref,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        backend_version=backend_version,
        precision='float64',
        compile_s=compile_s,
        solve_s=solve_s,
        postprocess_s=postprocess_s,
        peak_ram_mb=peak_ram_mb,
        disk_mb=disk_mb,
        output_mb=output_mb,
        observations=(
            RawObservableObservation(
                observable_id='termination-fr',
                kind='transfer_magnitude_db',
                unit=expected_by_id['termination-fr'].unit,
                samples=tuple(magnitude_samples),
                diagnostics=(
                    'Computed as 20*log10(|P/Q|/(1 Pa/(m3/s))) from the finest p=5 MFEM pressure.',
                ),
            ),
            RawObservableObservation(
                observable_id='termination-complex-pressure',
                kind='field_pressure_pa',
                unit=expected_by_id['termination-complex-pressure'].unit,
                samples=tuple(complex_samples),
                diagnostics=(
                    'Complex pressure is taken directly from the finest p=5 real-block MFEM solve.',
                ),
            ),
        ),
        diagnostics=(
            'R100A-3 Robin boundary is assembled only on x=6 / b-interface.',
            'Rigid x=0/y/z walls remain natural Neumann boundaries.',
            'Real block operator is [A0,+kB;-kB,A0] for A0=K-k^2M.',
        ),
    )


def _resource_violations_values(
    fixture,
    *,
    compile_s: float,
    solve_s: float,
    postprocess_s: float,
    peak_ram_mb: float,
    disk_mb: float,
    output_mb: float,
) -> list[str]:
    budget = fixture.resource_budget
    checks = (
        ('compile_s', compile_s, float(budget.max_compile_s)),
        ('solve_s', solve_s, float(budget.max_solve_s)),
        ('postprocess_s', postprocess_s, float(budget.max_postprocess_s)),
        ('peak_ram_mb', peak_ram_mb, float(budget.ram_budget_mb)),
        ('disk_mb', disk_mb, float(budget.disk_budget_mb)),
        ('output_mb', output_mb, float(budget.max_output_mb)),
    )
    return [
        f'{name}={value} exceeds budget {limit}'
        for name, value, limit in checks
        if value > limit
    ]


def _resource_violations(fixture, raw: RawFixtureObservation) -> list[str]:
    return _resource_violations_values(
        fixture,
        compile_s=float(raw.compile_s),
        solve_s=float(raw.solve_s),
        postprocess_s=float(raw.postprocess_s),
        peak_ram_mb=float(raw.peak_ram_mb),
        disk_mb=float(raw.disk_mb),
        output_mb=float(raw.output_mb),
    )


def _blocked_fixture(*, backend_version: str, reason: str) -> BakeoffFixtureEvidence:
    return BakeoffFixtureEvidence(
        fixture_id=FIXTURE_ID,
        status='blocked',
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        backend_version=backend_version,
        precision='float64',
        diagnostics=(reason,),
    )


def _execute(
    benchmark,
    candidates,
    *,
    mfem_root: Path,
    executable: Path,
    build_dir: Path,
    work_dir: Path,
    evidence_ref: str,
    native_build_s: float | None,
) -> tuple[dict[str, object], BakeoffRun]:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    _validate_authority(benchmark, fixture)

    if 'wave_radiation_termination' not in candidate.probe_capabilities:
        raise RuntimeError(
            'MFEM candidate manifest does not authorize wave_radiation_termination probe'
        )

    actual_head = _git_head(mfem_root)
    if actual_head != candidate.source_commit_sha:
        raise RuntimeError(
            f'MFEM checkout mismatch: expected {candidate.source_commit_sha}, got {actual_head}'
        )
    if not executable.is_file():
        raise RuntimeError(f'MFEM radiation probe executable is missing: {executable}')

    work_dir.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / 'mfem_radiation_transfer_raw.json'
    source = fixture.sources[0]
    receiver = fixture.receivers[0]
    grid = fixture.comparison.frequency_grid

    command = [
        str(executable),
        '--density', str(fixture.environment.density_kg_m3),
        '--sound-speed', str(fixture.environment.sound_speed_m_s),
        '--source-x', str(source.position.x_m),
        '--source-y', str(source.position.y_m),
        '--source-z', str(source.position.z_m),
        '--receiver-x', str(receiver.position.x_m),
        '--receiver-y', str(receiver.position.y_m),
        '--receiver-z', str(receiver.position.z_m),
        '--source-amplitude', str(source.amplitude),
        '--frequency-start', str(grid.start_hz),
        '--frequency-stop', str(grid.stop_hz),
        '--frequency-step', str(grid.step_hz),
        '--order-min', str(ORDER_MIN),
        '--order-max', str(ORDER_MAX),
        '--output', str(raw_path),
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    monitor = ProcessPeakRssMonitor(process)
    monitor.start()
    execution_budget_s = (
        float(fixture.resource_budget.max_compile_s)
        + float(fixture.resource_budget.max_solve_s)
    )
    process_started = time.perf_counter()
    timed_out = False
    try:
        stdout, _ = process.communicate(timeout=execution_budget_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, _ = process.communicate()
    process_wall_s = time.perf_counter() - process_started
    peak_ram_mb = monitor.stop()

    if timed_out:
        reason = (
            f'MFEM radiation executable exceeded the combined frozen assembly/solve '
            f'budget {execution_budget_s:g} s; observed wall time before termination '
            f'{process_wall_s:.6f} s. Exact compile/solve split is unavailable because '
            'the process was terminated once resource PASS became impossible.'
        )
        fixture_evidence = BakeoffFixtureEvidence(
            fixture_id=fixture.fixture_id,
            status='fail',
            evidence_ref=evidence_ref,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            backend_version='4.10',
            precision='float64',
            peak_ram_mb=peak_ram_mb,
            disk_mb=_directory_size_mb(work_dir),
            output_mb=(
                raw_path.stat().st_size / (1024.0 * 1024.0)
                if raw_path.is_file()
                else 0.0
            ),
            diagnostics=(reason,),
        )
        run = BakeoffRun(
            run_id=f'mfem-radiation-termination-{os.environ.get("GITHUB_RUN_ID", "manual")}',
            r100a_manifest_id=benchmark.manifest_id,
            r100a_semantic_hash=benchmark.semantic_hash(),
            candidate_manifest_hash=candidates.semantic_hash(),
            candidate_id=candidate.candidate_id,
            candidate_source_commit_sha=candidate.source_commit_sha,
            platform=_platform(fixture.resource_budget.cpu_thread_budget),
            fixture_evidence=(fixture_evidence,),
            hard_gates=_hard_gates(evidence_ref, reproducible=True),
            notes=(
                'Workflow completion and radiation fixture result are separate authorities.',
                'The executable was terminated only after the frozen combined assembly/solve resource budget made candidate PASS impossible.',
                'No pressure samples are synthesized for the timed-out solve.',
            ),
        )
        validate_bakeoff_run(benchmark, candidates, run)
        root = Path(__file__).resolve().parents[1]
        details = {
            'raw': None,
            'convergence': [],
            'qualification': {
                'qualified': False,
                'violations': [reason],
            },
            'solver_violations': [reason],
            'central_observable_evidence': None,
            'resource_evidence': {
                'native_build_s': native_build_s,
                'execution_budget_s': execution_budget_s,
                'observed_process_wall_s': process_wall_s,
                'peak_ram_mb': peak_ram_mb,
                'work_disk_mb': _directory_size_mb(work_dir),
                'raw_output_mb': (
                    raw_path.stat().st_size / (1024.0 * 1024.0)
                    if raw_path.is_file()
                    else 0.0
                ),
                'resource_timeout': True,
            },
            'source_provenance': {
                **_htdt_git_provenance(),
                'mfem_source_commit_sha': actual_head,
                'probe_source_sha256': _sha256_file(
                    root / 'benchmarks' / 'acoustics' / 'mfem_probe' / 'radiation_transfer.cpp'
                ),
                'harness_source_sha256': _sha256_file(Path(__file__).resolve()),
                'probe_executable_sha256': _sha256_file(executable),
                'runtime_versions': _runtime_versions(),
            },
            'process_stdout_tail': stdout[-6000:],
        }
        return details, run

    if process.returncode != 0:
        raise RuntimeError(
            f'MFEM radiation probe executable exited {process.returncode}: {stdout[-6000:]}'
        )
    if not raw_path.is_file():
        raise RuntimeError('MFEM radiation probe did not produce raw JSON output')

    raw = json.loads(raw_path.read_text(encoding='utf-8'))
    orders, solver_violations = _validate_raw(fixture, raw)
    backend_version = str(raw.get('mfem_version', '4.10'))
    compile_s = sum(float(item['assemble_s']) for item in orders)
    solve_s = sum(float(item['solve_s']) for item in orders)
    disk_mb = _directory_size_mb(work_dir)
    output_mb = raw_path.stat().st_size / (1024.0 * 1024.0)

    post_started = time.perf_counter()
    pair_metrics: list[dict[str, float | int]] = []
    convergence: dict[str, object] = {
        'qualified': False,
        'qualification_fraction_of_frozen_tolerance': CONVERGENCE_QUALIFICATION_FRACTION,
        'violations': list(solver_violations),
    }

    if solver_violations:
        pre_postprocess_s = time.perf_counter() - post_started
        resource_violations = _resource_violations_values(
            fixture,
            compile_s=compile_s,
            solve_s=solve_s,
            postprocess_s=pre_postprocess_s,
            peak_ram_mb=peak_ram_mb,
            disk_mb=disk_mb,
            output_mb=output_mb,
        )
        fixture_evidence = BakeoffFixtureEvidence(
            fixture_id=fixture.fixture_id,
            status='fail',
            evidence_ref=evidence_ref,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            backend_version=backend_version,
            precision='float64',
            compile_s=compile_s,
            solve_s=solve_s,
            postprocess_s=pre_postprocess_s,
            peak_ram_mb=peak_ram_mb,
            disk_mb=disk_mb,
            output_mb=output_mb,
            diagnostics=tuple(solver_violations + resource_violations),
        )
        central_evidence = None
    else:
        pair_metrics, convergence = _convergence_evidence(fixture, orders)
        pre_postprocess_s = time.perf_counter() - post_started
        raw_finest = _raw_finest_observation(
            fixture,
            orders[-1],
            evidence_ref=evidence_ref,
            backend_version=backend_version,
            compile_s=compile_s,
            solve_s=solve_s,
            postprocess_s=pre_postprocess_s,
            peak_ram_mb=peak_ram_mb,
            disk_mb=disk_mb,
            output_mb=output_mb,
        )
        central_evidence = evaluate_sampled_fixture(fixture, raw_finest)
        resource_violations = _resource_violations(fixture, raw_finest)

        extra_diagnostics: list[str] = []
        status = central_evidence.status
        if not bool(convergence['qualified']):
            status = 'fail'
            extra_diagnostics.extend(str(item) for item in convergence['violations'])
        if resource_violations:
            status = 'fail'
            extra_diagnostics.extend(resource_violations)

        fixture_evidence = central_evidence.model_copy(
            update={
                'status': status,
                'diagnostics': tuple(central_evidence.diagnostics) + tuple(extra_diagnostics),
            }
        )

    run = BakeoffRun(
        run_id=f'mfem-radiation-termination-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(fixture.resource_budget.cpu_thread_budget),
        fixture_evidence=(fixture_evidence,),
        hard_gates=_hard_gates(evidence_ref, reproducible=True),
        notes=(
            'Workflow completion and radiation fixture result are separate authorities.',
            'Candidate capability authorizes this bounded probe only; it is not production adoption.',
            'p=2..5 uses one fixed structured 6x4x2 mesh; only polynomial order changes.',
            'A non-converged p-refinement sequence remains FAIL even if the finest trace alone meets analytical tolerances.',
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)

    root = Path(__file__).resolve().parents[1]
    cmake_cache = build_dir / 'CMakeCache.txt'
    details = {
        'raw': raw,
        'convergence': pair_metrics,
        'qualification': convergence,
        'solver_violations': solver_violations,
        'central_observable_evidence': (
            [
                item.model_dump(mode='json') for item in central_evidence.observables
            ]
            if central_evidence is not None
            else None
        ),
        'resource_evidence': {
            'native_build_s': native_build_s,
            'fem_assembly_s': compile_s,
            'solve_s': solve_s,
            'postprocess_s': pre_postprocess_s,
            'peak_ram_mb': peak_ram_mb,
            'work_disk_mb': disk_mb,
            'raw_output_mb': output_mb,
            'probe_executable_mb': executable.stat().st_size / (1024.0 * 1024.0),
            'resource_violations': resource_violations,
        },
        'source_provenance': {
            **_htdt_git_provenance(),
            'mfem_source_commit_sha': actual_head,
            'probe_source_sha256': _sha256_file(
                root / 'benchmarks' / 'acoustics' / 'mfem_probe' / 'radiation_transfer.cpp'
            ),
            'harness_source_sha256': _sha256_file(Path(__file__).resolve()),
            'probe_executable_sha256': _sha256_file(executable),
            'cmake_cache_sha256': _sha256_file(cmake_cache) if cmake_cache.is_file() else None,
            'runtime_versions': _runtime_versions(),
        },
    }
    return details, run


def _blocked_run(
    benchmark,
    candidates,
    *,
    evidence_ref: str,
    reason: str,
) -> BakeoffRun:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    _validate_authority(benchmark, fixture)
    run = BakeoffRun(
        run_id=f'mfem-radiation-termination-blocked-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(fixture.resource_budget.cpu_thread_budget),
        fixture_evidence=(
            _blocked_fixture(backend_version='unavailable', reason=reason),
        ),
        hard_gates=_hard_gates(evidence_ref, reproducible=False),
        notes=(
            'Workflow completion and radiation fixture result are separate authorities.',
            reason,
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)
    return run


def _write_artifact(
    output: Path,
    benchmark,
    candidates,
    *,
    run: BakeoffRun,
    details: dict[str, object] | None,
    error: str | None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema_version': ARTIFACT_SCHEMA,
        'probe': 'mfem-r100a3-radiation-termination',
        'workflow_result_is_fixture_result': False,
        'radiation_fixture_outcome': run.fixture_evidence[0].status,
        'r100a_manifest_id': benchmark.manifest_id,
        'r100a_semantic_hash': benchmark.semantic_hash(),
        'candidate_manifest_hash': candidates.semantic_hash(),
        'candidate_id': run.candidate_id,
        'details': details,
        'error': error,
        'bakeoff_run': run.model_dump(mode='json'),
        'bakeoff_run_semantic_hash': run.semantic_hash(),
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run the R100B MFEM R100A-3 radiation termination gate'
    )
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--candidates', required=True, type=Path)
    parser.add_argument('--mfem-root', required=True, type=Path)
    parser.add_argument('--build-dir', required=True, type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--build-s', type=float)
    parser.add_argument('--blocked-reason')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    benchmark = load_acoustic_benchmark_manifest(args.manifest)
    candidates = load_bakeoff_candidate_manifest(args.candidates)
    fixture = _fixture(benchmark)
    _validate_authority(benchmark, fixture)
    evidence_ref = f'artifact:{args.output.as_posix()}'

    if args.blocked_reason:
        run = _blocked_run(
            benchmark,
            candidates,
            evidence_ref=evidence_ref,
            reason=args.blocked_reason,
        )
        _write_artifact(
            args.output,
            benchmark,
            candidates,
            run=run,
            details=None,
            error=args.blocked_reason,
        )
    else:
        if args.executable is None:
            raise RuntimeError('--executable is required unless --blocked-reason is supplied')
        details, run = _execute(
            benchmark,
            candidates,
            mfem_root=args.mfem_root,
            executable=args.executable,
            build_dir=args.build_dir,
            work_dir=args.work_dir,
            evidence_ref=evidence_ref,
            native_build_s=args.build_s,
        )
        _write_artifact(
            args.output,
            benchmark,
            candidates,
            run=run,
            details=details,
            error=None,
        )

    print(
        json.dumps(
            {
                'workflow_status': 'pass',
                'radiation_fixture_outcome': run.fixture_evidence[0].status,
                'run_id': run.run_id,
                'run_semantic_hash': run.semantic_hash(),
                'r100a_semantic_hash': benchmark.semantic_hash(),
                'candidate_manifest_hash': candidates.semantic_hash(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
