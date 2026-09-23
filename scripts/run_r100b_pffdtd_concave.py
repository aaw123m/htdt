from __future__ import annotations

import argparse
import gc
from hashlib import sha256
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
import traceback

import h5py
import numpy as np
import psutil

from htdt.acoustic_bakeoff import (
    BakeoffFixtureEvidence,
    BakeoffHardGateEvidence,
    BakeoffObservableEvidence,
    BakeoffPlatform,
    BakeoffRun,
    load_bakeoff_candidate_manifest,
    observable_tolerance_violations,
    validate_bakeoff_run,
)
from htdt.acoustic_bakeoff_observation import (
    RawFixtureObservation,
    RawObservableObservation,
    RawObservationSample,
)
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest
from htdt.acoustic_pffdtd_adapter import (
    apply_pffdtd_runtime_compatibility_patches,
    compile_rigid_fixture_model,
    finite_record_pressure_transfer,
    pffdtd_git_head,
    pffdtd_velocity_potential_to_pressure_trace,
    recombine_pffdtd_receiver_traces,
)


CANDIDATE_ID = 'pffdtd-main-aa319f6'
FIXTURE_ID = 'wave-concave-l-room-v1'
PROBE_SCHEMA = 'r100b-pffdtd-concave-artifact-1'
PROBE_ID = 'pffdtd-concave-complex-pressure'
ADAPTER_ID = 'htdt-r100b-pffdtd-concave'
ADAPTER_VERSION = '3'
FMAX_HZ = 300.0
GRID_SPACINGS_M = (0.5, 0.25, 0.125)
THREAD_BUDGET = 4
NPROCS = 1


class PeakRssMonitor:
    def __init__(self) -> None:
        self._process = psutil.Process()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self.peak_bytes = self._process.memory_info().rss

    def _sample(self) -> None:
        while not self._stop.wait(0.01):
            self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> float:
        self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)
        self._stop.set()
        self._thread.join(timeout=1.0)
        return self.peak_bytes / (1024.0 * 1024.0)


def _candidate(candidates):
    return next(item for item in candidates.candidates if item.candidate_id == CANDIDATE_ID)


def _fixture(benchmark):
    return next(item for item in benchmark.fixtures if item.fixture_id == FIXTURE_ID)


def _runtime_versions() -> dict[str, str]:
    names = ('numpy', 'numba', 'h5py', 'scipy', 'tqdm', 'psutil', 'memory-profiler')
    result: dict[str, str] = {}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = 'not-installed'
    return result


def _platform() -> BakeoffPlatform:
    logical_threads = os.cpu_count() or 1
    return BakeoffPlatform(
        os=platform.platform(),
        architecture=platform.machine() or 'unknown',
        python_version=platform.python_version(),
        cpu=platform.processor() or os.environ.get('PROCESSOR_IDENTIFIER', 'unknown'),
        logical_threads=logical_threads,
        thread_budget=min(THREAD_BUDGET, logical_threads),
        gpu=None,
        device_notes=(
            'GitHub-hosted Windows PFFDTD Python/Numba CPU exact-concave pressure probe.'
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
                        'Pinned PFFDTD source/compatibility diff, exact concave triangulation, '
                        'R100A-3 source/receiver/environment/frequency authority, three grid '
                        'levels, raw complex pressure and resources are recorded.'
                    ),
                )
            )
        else:
            result.append(
                BakeoffHardGateEvidence(
                    category=category,
                    status='not_run',
                    summary=(
                        'Candidate-wide hard gate remains open. This slice evaluates only '
                        'the exact concave wave fixture and never selects a production solver.'
                    ),
                )
            )
    return tuple(result)


def _directory_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return sum(
        item.stat().st_size
        for item in path.rglob('*')
        if item.is_file()
    ) / (1024.0 * 1024.0)


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
    if len(event_head) == 40 and all(character in '0123456789abcdef' for character in event_head):
        pr_head = event_head
    else:
        pr_head = _git_rev_parse('HEAD^2') or checkout
    return {
        'checkout_commit_sha': checkout,
        'pr_head_commit_sha': pr_head,
    }


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_provenance(upstream_root: Path) -> dict[str, object]:
    root = Path(__file__).resolve().parents[1]
    result: dict[str, object] = {
        **_htdt_git_provenance(),
        'harness_source_sha256': _file_sha256(Path(__file__).resolve()),
        'adapter_source_sha256': _file_sha256(
            root / 'backend' / 'src' / 'htdt' / 'acoustic_pffdtd_adapter.py'
        ),
    }
    try:
        result['pffdtd_source_commit_sha'] = pffdtd_git_head(upstream_root)
    except Exception:
        result['pffdtd_source_commit_sha'] = None

    for key, relative in (
        ('upstream_sim_consts_sha256', Path('python/fdtd/sim_consts.py')),
        ('upstream_sim_setup_sha256', Path('python/sim_setup.py')),
    ):
        source = upstream_root / relative
        result[key] = _file_sha256(source) if source.is_file() else None
    return result


def _position(position) -> tuple[float, float, float]:
    return (float(position.x_m), float(position.y_m), float(position.z_m))


def _validate_fixture_contract(fixture) -> None:
    if fixture.fixture_id != FIXTURE_ID:
        raise ValueError('wrong PFFDTD concave fixture')
    if fixture.required_capabilities != ('wave_rigid',):
        raise ValueError('concave fixture capability authority changed')
    if len(fixture.regions) != 1 or fixture.regions[0].region_id != 'lroom':
        raise ValueError('concave fixture region authority changed')
    if fixture.obstacles or fixture.portals or fixture.terminations:
        raise ValueError('concave fixture topology authority changed')

    bottom_xy = (
        (0.0, 0.0),
        (6.0, 0.0),
        (6.0, 4.0),
        (4.0, 4.0),
        (4.0, 2.0),
        (2.0, 2.0),
        (2.0, 4.0),
        (0.0, 4.0),
    )
    expected_vertices: dict[str, tuple[float, float, float]] = {}
    for index, (x_m, y_m) in enumerate(bottom_xy):
        expected_vertices[f'lroom-b{index}'] = (x_m, y_m, 0.0)
        expected_vertices[f'lroom-t{index}'] = (x_m, y_m, 2.5)
    actual_vertices = {
        item.vertex_id: _position(item.position)
        for item in fixture.regions[0].vertices
    }
    if actual_vertices != expected_vertices:
        raise ValueError('concave fixture geometry authority changed')

    expected_faces = {
        'lroom-bottom': (
            ('lroom-b0', 'lroom-b1', 'lroom-b2', 'lroom-b3', 'lroom-b4', 'lroom-b5', 'lroom-b6', 'lroom-b7'),
            'b-rigid',
        ),
        'lroom-top': (
            ('lroom-t7', 'lroom-t6', 'lroom-t5', 'lroom-t4', 'lroom-t3', 'lroom-t2', 'lroom-t1', 'lroom-t0'),
            'b-rigid',
        ),
        'lroom-side0': (('lroom-b0', 'lroom-b1', 'lroom-t1', 'lroom-t0'), 'b-rigid'),
        'lroom-side1': (('lroom-b1', 'lroom-b2', 'lroom-t2', 'lroom-t1'), 'b-rigid'),
        'lroom-side2': (('lroom-b2', 'lroom-b3', 'lroom-t3', 'lroom-t2'), 'b-rigid'),
        'lroom-side3': (('lroom-b3', 'lroom-b4', 'lroom-t4', 'lroom-t3'), 'b-rigid'),
        'lroom-side4': (('lroom-b4', 'lroom-b5', 'lroom-t5', 'lroom-t4'), 'b-rigid'),
        'lroom-side5': (('lroom-b5', 'lroom-b6', 'lroom-t6', 'lroom-t5'), 'b-rigid'),
        'lroom-side6': (('lroom-b6', 'lroom-b7', 'lroom-t7', 'lroom-t6'), 'b-rigid'),
        'lroom-side7': (('lroom-b7', 'lroom-b0', 'lroom-t0', 'lroom-t7'), 'b-rigid'),
    }
    actual_faces = {
        face.face_id: (tuple(face.vertex_ids), face.boundary_id)
        for face in fixture.regions[0].faces
    }
    if actual_faces != expected_faces:
        raise ValueError('concave fixture face topology authority changed')

    if (
        len(fixture.materials) != 1
        or fixture.materials[0].material_id != 'rigid'
        or fixture.materials[0].wave_model != 'rigid'
    ):
        raise ValueError('concave fixture rigid material authority changed')
    if len(fixture.boundaries) != 1:
        raise ValueError('concave fixture boundary authority changed')
    if (
        fixture.boundaries[0].boundary_id != 'b-rigid'
        or fixture.boundaries[0].material_id != fixture.materials[0].material_id
    ):
        raise ValueError('concave fixture rigid boundary binding changed')
    if any(item.boundary_id != 'b-rigid' for item in fixture.regions[0].faces):
        raise ValueError('concave fixture contains a non-rigid face')

    if len(fixture.sources) != 1 or len(fixture.receivers) != 1:
        raise ValueError('concave fixture requires one source and receiver')
    source = fixture.sources[0]
    receiver = fixture.receivers[0]
    if (
        source.region_id != 'lroom'
        or _position(source.position) != (1.0, 1.0, 1.0)
        or source.normalization != 'volume_velocity_m3_s'
        or float(source.amplitude) != 1.0
        or float(source.phase_deg) != 0.0
        or source.directivity != 'omnidirectional'
        or receiver.region_id != 'lroom'
        or _position(receiver.position) != (5.0, 1.0, 1.0)
        or (
            float(receiver.forward.x),
            float(receiver.forward.y),
            float(receiver.forward.z),
        ) != (0.0, 1.0, 0.0)
        or receiver.calibration_state != 'ideal_flat'
        or receiver.calibration_profile_id is not None
        or receiver.timing_reference != 'source_t0'
    ):
        raise ValueError('concave source/receiver authority changed')

    environment = fixture.environment
    if (
        float(environment.temperature_c) != 20.0
        or float(environment.sound_speed_m_s) != 343.0
        or float(environment.relative_humidity_percent or -1.0) != 50.0
        or float(environment.pressure_pa or -1.0) != 101325.0
        or float(environment.density_kg_m3) != 1.2
    ):
        raise ValueError('concave environment authority changed')

    comparison = fixture.comparison
    grid = comparison.frequency_grid
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
        or comparison.time_step_s is not None
        or float(comparison.observation_time_s or -1.0) != 2.0
        or comparison.finite_record_transfer is None
        or grid.kind != 'uniform'
        or float(grid.start_hz) != 20.0
        or float(grid.stop_hz) != 300.0
        or float(grid.step_hz) != 1.0
    ):
        raise ValueError('concave comparison authority changed')

    finite_record = comparison.finite_record_transfer
    assert finite_record is not None
    if (
        finite_record.excitation_model != 'causal_discrete_unit_sample_volume_velocity'
        or finite_record.sample_zero_reference != 'source_t0'
        or finite_record.record_interval != 'half_open_0_T'
        or finite_record.solver_time_step_policy != 'solver_native_recorded'
        or finite_record.dtft_kernel != 'exp(+i*2*pi*f*n*dt)'
        or finite_record.dtft_measure != 'dt_weighted_sum'
        or finite_record.numerator_quantity != 'physical_pressure'
        or finite_record.numerator_record_policy != 'solver_pressure_or_declared_primary_field_conversion'
        or finite_record.denominator_record != 'physical_volume_velocity_samples_on_solver_time_grid'
        or finite_record.transfer_definition != 'pressure_over_volume_velocity'
        or finite_record.frequency_evaluation != 'direct_scored_frequency_dtft'
        or finite_record.source_spectrum_requirement != 'finite_nonzero_on_scored_grid'
        or finite_record.zero_padding != 'none'
    ):
        raise ValueError('concave finite-record transfer authority changed')

    observable_by_id = {item.observable_id: item for item in fixture.observables}
    if set(observable_by_id) != {'lroom-fr', 'lroom-phase'}:
        raise ValueError('concave observable authority changed')
    magnitude = observable_by_id['lroom-fr']
    phase = observable_by_id['lroom-phase']
    if (
        magnitude.kind != 'transfer_magnitude_db'
        or magnitude.unit != 'dB re 1 Pa/(m3/s)'
        or magnitude.reference_kind != 'independent_solver'
        or magnitude.acceptance_relation != 'matches_reference'
        or magnitude.samples
        or float(magnitude.tolerance.absolute) != 0.75
        or float(magnitude.tolerance.relative) != 0.05
        or float(magnitude.tolerance.null_mask_below_db) != -60.0
    ):
        raise ValueError('concave magnitude authority changed')
    if (
        phase.kind != 'transfer_phase_deg'
        or phase.unit != 'deg'
        or phase.reference_kind != 'independent_solver'
        or phase.acceptance_relation != 'matches_reference'
        or phase.samples
        or float(phase.tolerance.phase_deg) != 8.0
        or float(phase.tolerance.null_mask_below_db) != -40.0
    ):
        raise ValueError('concave phase authority changed')


def _validate_compiled_concave_mesh(
    points: np.ndarray,
    triangles: np.ndarray,
) -> dict[str, object]:
    if points.shape != (16, 3) or triangles.shape != (28, 3):
        raise ValueError(
            f'exact concave compiler shape changed: points={points.shape}, triangles={triangles.shape}'
        )

    edge_counts: dict[tuple[int, int], int] = {}
    for triangle in triangles:
        for offset in range(3):
            edge = tuple(
                sorted(
                    (
                        int(triangle[offset]),
                        int(triangle[(offset + 1) % 3]),
                    )
                )
            )
            edge_counts[edge] = edge_counts.get(edge, 0) + 1
    if set(edge_counts.values()) != {2}:
        raise ValueError('compiled concave surface is not a closed two-manifold')

    signed_volume_m3 = sum(
        float(
            np.dot(
                points[triangle[0]],
                np.cross(points[triangle[1]], points[triangle[2]]),
            )
        )
        / 6.0
        for triangle in triangles
    )
    if not math.isclose(signed_volume_m3, 50.0, rel_tol=0.0, abs_tol=1.0e-10):
        raise ValueError(
            f'compiled concave volume changed: {signed_volume_m3} m3 != 50 m3'
        )

    horizontal_area_m2: dict[str, float] = {}
    for label, z_m in (('bottom', 0.0), ('top', 2.5)):
        horizontal = [
            triangle
            for triangle in triangles
            if np.allclose(points[triangle, 2], z_m, rtol=0.0, atol=1.0e-12)
        ]
        area = sum(
            abs(
                float(
                    (points[triangle[1], 0] - points[triangle[0], 0])
                    * (points[triangle[2], 1] - points[triangle[0], 1])
                    - (points[triangle[1], 1] - points[triangle[0], 1])
                    * (points[triangle[2], 0] - points[triangle[0], 0])
                )
            )
            * 0.5
            for triangle in horizontal
        )
        if len(horizontal) != 6 or not math.isclose(
            area,
            20.0,
            rel_tol=0.0,
            abs_tol=1.0e-10,
        ):
            raise ValueError(
                f'compiled concave {label} triangulation changed: '
                f'triangles={len(horizontal)}, area={area} m2'
            )
        horizontal_area_m2[label] = area

    return {
        'points': int(points.shape[0]),
        'triangles': int(triangles.shape[0]),
        'closed_two_manifold': True,
        'signed_volume_m3': signed_volume_m3,
        'horizontal_area_m2': horizontal_area_m2,
    }


def _frequency_grid(fixture) -> np.ndarray:
    grid = fixture.comparison.frequency_grid
    count_float = (float(grid.stop_hz) - float(grid.start_hz)) / float(grid.step_hz)
    count = int(round(count_float))
    if not math.isclose(count_float, count, rel_tol=0.0, abs_tol=1.0e-12):
        raise ValueError('concave frequency grid is not an integer number of steps')
    return float(grid.start_hz) + np.arange(count + 1, dtype=np.float64) * float(grid.step_hz)


def _prepare_engine(SimEngine, sim_dir: Path):
    engine = SimEngine(
        sim_dir,
        energy_on=False,
        nthreads=min(THREAD_BUDGET, os.cpu_count() or 1),
    )
    engine.load_h5_data()
    engine.setup_mask()
    engine.allocate_mem()
    engine.set_coeffs()
    engine.checks()
    return engine


def _run_level(
    *,
    sim_setup,
    SimEngine,
    fixture,
    model_path: Path,
    material_dir: Path,
    level_dir: Path,
    target_h_m: float,
    frequencies_hz: np.ndarray,
    warm_jit: bool,
) -> tuple[dict[str, object], dict[str, np.ndarray], float, float, float]:
    authority_c = float(fixture.environment.sound_speed_m_s)
    # PFFDTD aa319f6 exposes Tc instead of c and computes
    # c = 343.2*sqrt(Tc/20). Use Tc only as a numerical control so the
    # solver wave speed equals the frozen R100A-4 343.0 m/s exactly.
    # Physical temperature authority remains 20 C and is recorded separately.
    solver_tc_for_sound_speed = 20.0 * (authority_c / 343.2) ** 2
    ppw = authority_c / (FMAX_HZ * target_h_m)
    duration_s = float(fixture.comparison.observation_time_s)

    setup_started = time.perf_counter()
    sim_setup(
        insig_type='impulse',
        fmax=FMAX_HZ,
        PPW=ppw,
        save_folder=level_dir,
        model_json_file=model_path,
        mat_folder=material_dir,
        mat_files_dict={},
        duration=duration_s,
        Tc=solver_tc_for_sound_speed,
        rh=float(fixture.environment.relative_humidity_percent or 50.0),
        source_num=1,
        draw_vox=False,
        fcc_flag=False,
        Nprocs=NPROCS,
        compress=0,
    )
    setup_s = time.perf_counter() - setup_started

    warm_prepare_s = 0.0
    jit_s = 0.0
    if warm_jit:
        warm_prepare_started = time.perf_counter()
        warm_engine = _prepare_engine(SimEngine, level_dir)
        warm_prepare_s = time.perf_counter() - warm_prepare_started
        jit_started = time.perf_counter()
        warm_engine.run_steps(0, 1)
        jit_s = time.perf_counter() - jit_started
        del warm_engine
        gc.collect()

    prepare_started = time.perf_counter()
    engine = _prepare_engine(SimEngine, level_dir)
    prepare_s = time.perf_counter() - prepare_started
    if not math.isclose(float(engine.h), target_h_m, rel_tol=0.0, abs_tol=1.0e-12):
        raise RuntimeError(f'PFFDTD grid spacing mismatch: {engine.h} != {target_h_m}')
    if not math.isclose(float(engine.c), authority_c, rel_tol=0.0, abs_tol=1.0e-12):
        raise RuntimeError(
            f'PFFDTD sound-speed mapping mismatch: {engine.c} != {authority_c}'
        )

    record_last_time_s = float((int(engine.Nt) - 1) * engine.Ts)
    record_next_time_s = float(int(engine.Nt) * engine.Ts)
    record_tolerance_s = max(1.0e-12, abs(float(engine.Ts)) * 1.0e-9)
    if not (
        record_last_time_s < duration_s
        and record_next_time_s + record_tolerance_s >= duration_s
    ):
        raise RuntimeError(
            'PFFDTD finite-record samples do not satisfy the frozen [0,T) contract: '
            f'last={record_last_time_s}, next={record_next_time_s}, T={duration_s}'
        )

    solve_started = time.perf_counter()
    engine.run_all(nsteps=int(engine.Nt))
    solve_s = time.perf_counter() - solve_started

    post_started = time.perf_counter()
    engine.save_outputs()
    output_path = level_dir / 'sim_outs.h5'
    with h5py.File(output_path, 'r') as handle:
        raw_grid = np.asarray(handle['u_out'][...], dtype=np.float64)
    receiver_potential = recombine_pffdtd_receiver_traces(
        raw_grid,
        engine.out_alpha,
        receiver_count=1,
        nt=int(engine.Nt),
    )
    if receiver_potential.shape != (1, int(engine.Nt)):
        raise RuntimeError(f'unexpected PFFDTD receiver trace shape: {receiver_potential.shape}')

    source = fixture.sources[0]
    physical_source = np.zeros(int(engine.Nt), dtype=np.float64)
    physical_source[0] = float(source.amplitude)
    pressure_record = pffdtd_velocity_potential_to_pressure_trace(
        receiver_potential[0],
        time_step_s=float(engine.Ts),
        density_kg_m3=float(fixture.environment.density_kg_m3),
    )
    pressure_transfer = finite_record_pressure_transfer(
        pressure_record,
        physical_source,
        time_step_s=float(engine.Ts),
        frequency_hz=frequencies_hz,
    )
    if not np.all(np.isfinite(pressure_transfer.real)) or not np.all(np.isfinite(pressure_transfer.imag)):
        raise RuntimeError('PFFDTD concave pressure transfer contains non-finite values')
    post_s = time.perf_counter() - post_started

    detail = {
        'target_h_m': target_h_m,
        'points_per_wavelength_at_fmax': ppw,
        'Nx': int(engine.Nx),
        'Ny': int(engine.Ny),
        'Nz': int(engine.Nz),
        'Nt': int(engine.Nt),
        'Nr': int(engine.Nr),
        'Ns': int(engine.Ns),
        'h_m': float(engine.h),
        'time_step_s': float(engine.Ts),
        'sample_rate_hz': float(1.0 / engine.Ts),
        'record_last_time_s': record_last_time_s,
        'record_next_time_s': record_next_time_s,
        'record_interval_authority': '[0,T)',
        'requested_observation_time_s': duration_s,
        'sound_speed_m_s': float(engine.c),
        'authority_temperature_c': float(fixture.environment.temperature_c),
        'solver_temperature_control_c': solver_tc_for_sound_speed,
        'sound_speed_mapping': 'c=343.2*sqrt(Tc/20); Tc_control=20*(authority_c/343.2)^2',
        'pressure_record_conversion': 'p=rho*d(phi)/dt; second-order centered interior and one-sided endpoints',
        'finite_record_transfer': 'dt-weighted direct DTFT P_T/Q_T from physical pressure/source records',
        'air_absorption_postprocess_applied': False,
        'courant': float(engine.l),
        'setup_s': setup_s,
        'warm_prepare_s': warm_prepare_s,
        'jit_s': jit_s,
        'engine_prepare_s': prepare_s,
        'solve_s': solve_s,
        'postprocess_s': post_s,
        'sim_outs_mb': output_path.stat().st_size / (1024.0 * 1024.0),
        'generated_disk_mb': _directory_size_mb(level_dir),
        'receiver_potential_max_abs': float(np.max(np.abs(receiver_potential))),
        'pressure_record_max_abs_pa': float(np.max(np.abs(pressure_record))),
        'pressure_transfer_max_abs_pa_per_m3_s': float(np.max(np.abs(pressure_transfer))),
    }
    traces = {
        'receiver_velocity_potential': receiver_potential,
        'receiver_pressure_pa': pressure_record.copy(),
        'source_volume_velocity_m3_s': physical_source.copy(),
        'pressure_transfer_real_pa_per_m3_s': pressure_transfer.real.copy(),
        'pressure_transfer_imag_pa_per_m3_s': pressure_transfer.imag.copy(),
        'frequency_hz': frequencies_hz.copy(),
    }
    compile_s = setup_s + warm_prepare_s + jit_s + prepare_s
    return detail, traces, compile_s, solve_s, post_s


def _wrapped_phase_deg(value: complex) -> float:
    return math.degrees(math.atan2(value.imag, value.real))


def _wrapped_phase_delta_deg(first: float, second: float) -> float:
    return abs((first - second + 180.0) % 360.0 - 180.0)


def _pair_metrics(
    coarse: np.ndarray,
    fine: np.ndarray,
    *,
    magnitude_mask_db: float,
    phase_mask_db: float,
) -> dict[str, float | int]:
    if coarse.shape != fine.shape or coarse.ndim != 1:
        raise ValueError('concave convergence pressure grids do not match')

    coarse_magnitude = np.abs(coarse)
    fine_magnitude = np.abs(fine)
    coarse_db = 20.0 * np.log10(np.maximum(coarse_magnitude, 1.0e-300))
    fine_db = 20.0 * np.log10(np.maximum(fine_magnitude, 1.0e-300))

    magnitude_mask = fine_db >= magnitude_mask_db
    phase_mask = (fine_db >= phase_mask_db) & (coarse_magnitude > 0.0) & (fine_magnitude > 0.0)
    if not np.any(magnitude_mask) or not np.any(phase_mask):
        raise ValueError('concave null masks removed every convergence sample')

    magnitude_abs = np.abs(coarse_db[magnitude_mask] - fine_db[magnitude_mask])
    magnitude_rel = (
        np.abs(coarse_magnitude[magnitude_mask] - fine_magnitude[magnitude_mask])
        / np.maximum(fine_magnitude[magnitude_mask], 1.0e-300)
    )
    phase_error = [
        _wrapped_phase_delta_deg(
            _wrapped_phase_deg(complex(first)),
            _wrapped_phase_deg(complex(second)),
        )
        for first, second in zip(coarse[phase_mask], fine[phase_mask], strict=True)
    ]
    squared_diff = float(np.sum(np.abs(coarse - fine) ** 2))
    squared_reference = float(np.sum(np.abs(fine) ** 2))

    return {
        'magnitude_max_abs_db': float(np.max(magnitude_abs)),
        'magnitude_max_relative': float(np.max(magnitude_rel)),
        'phase_max_error_deg': float(max(phase_error)),
        'complex_rms_relative': math.sqrt(
            squared_diff / max(squared_reference, 1.0e-300)
        ),
        'magnitude_sample_count': int(np.count_nonzero(magnitude_mask)),
        'phase_sample_count': int(np.count_nonzero(phase_mask)),
    }


def _self_convergence(
    fixture,
    pressures: list[np.ndarray],
) -> tuple[list[dict[str, object]], dict[str, object], bool]:
    if len(pressures) != len(GRID_SPACINGS_M):
        raise ValueError('concave self-convergence requires exactly three grid levels')
    observable_by_id = {item.observable_id: item for item in fixture.observables}
    magnitude = observable_by_id['lroom-fr']
    phase = observable_by_id['lroom-phase']

    pair_metrics: list[dict[str, object]] = []
    for index in range(len(pressures) - 1):
        metrics = _pair_metrics(
            pressures[index],
            pressures[index + 1],
            magnitude_mask_db=float(magnitude.tolerance.null_mask_below_db),
            phase_mask_db=float(phase.tolerance.null_mask_below_db),
        )
        metrics['coarse_h_m'] = GRID_SPACINGS_M[index]
        metrics['fine_h_m'] = GRID_SPACINGS_M[index + 1]
        pair_metrics.append(metrics)

    sequence = [float(item['complex_rms_relative']) for item in pair_metrics]
    monotonic = all(
        sequence[index] > sequence[index + 1]
        for index in range(len(sequence) - 1)
    )
    final = pair_metrics[-1]

    violations: list[str] = []
    if not monotonic:
        violations.append('complex RMS grid-refinement error is not strictly decreasing')
    if float(final['magnitude_max_abs_db']) > float(magnitude.tolerance.absolute):
        violations.append(
            f'final h=0.25->0.125 magnitude delta {final["magnitude_max_abs_db"]} dB '
            f'exceeds frozen {magnitude.tolerance.absolute} dB candidate-qualification limit'
        )
    if float(final['magnitude_max_relative']) > float(magnitude.tolerance.relative):
        violations.append(
            f'final h=0.25->0.125 relative magnitude delta '
            f'{final["magnitude_max_relative"]} exceeds frozen '
            f'{magnitude.tolerance.relative} candidate-qualification limit'
        )
    if float(final['phase_max_error_deg']) > float(phase.tolerance.phase_deg):
        violations.append(
            f'final h=0.25->0.125 phase delta {final["phase_max_error_deg"]} deg '
            f'exceeds frozen {phase.tolerance.phase_deg} deg candidate-qualification limit'
        )

    qualification = {
        'qualified': not violations,
        'purpose': (
            'candidate self-refinement qualification only; this is not '
            'R100A independent_solver observable scoring'
        ),
        'complex_rms_relative_strictly_decreasing': monotonic,
        'complex_rms_relative_sequence': sequence,
        'limits_reused_from_frozen_observables': {
            'magnitude_absolute_db': float(magnitude.tolerance.absolute),
            'magnitude_relative': float(magnitude.tolerance.relative),
            'phase_deg': float(phase.tolerance.phase_deg),
        },
        'violations': violations,
    }
    return pair_metrics, qualification, not violations

def _raw_candidate_observation(
    fixture,
    frequencies_hz: np.ndarray,
    pressure: np.ndarray,
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
    magnitude_samples: list[RawObservationSample] = []
    phase_samples: list[RawObservationSample] = []
    for frequency_hz, transfer_value in zip(frequencies_hz, pressure, strict=True):
        transfer = complex(transfer_value)
        key = f'f={float(frequency_hz):.12g}Hz'
        magnitude_samples.append(
            RawObservationSample(
                sample_key=key,
                frequency_hz=float(frequency_hz),
                scalar_value=20.0 * math.log10(max(abs(transfer), 1.0e-300)),
            )
        )
        phase_samples.append(
            RawObservationSample(
                sample_key=key,
                frequency_hz=float(frequency_hz),
                scalar_value=_wrapped_phase_deg(transfer),
            )
        )

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
                observable_id='lroom-fr',
                kind='transfer_magnitude_db',
                unit='dB re 1 Pa/(m3/s)',
                samples=tuple(magnitude_samples),
                diagnostics=('Computed from finest h=0.125 m finite-record transfer as 20*log10(|P_T/Q_T|/(1 Pa/(m3/s))).',),
            ),
            RawObservableObservation(
                observable_id='lroom-phase',
                kind='transfer_phase_deg',
                unit='deg',
                samples=tuple(phase_samples),
                diagnostics=('Computed from finest h=0.125 m finite-record P_T/Q_T phase under exp(-i*omega*t).',),
            ),
        ),
        diagnostics=(
            'Raw candidate observations are retained even when independent-reference scoring is blocked.',
            'The exact frozen L-prism is compiled; no rectangular approximation is permitted.',
        ),
    )


def _resource_violations(fixture, raw: RawFixtureObservation) -> list[str]:
    budget = fixture.resource_budget
    checks = (
        ('compile_s', raw.compile_s, float(budget.max_compile_s)),
        ('solve_s', raw.solve_s, float(budget.max_solve_s)),
        ('postprocess_s', raw.postprocess_s, float(budget.max_postprocess_s)),
        ('peak_ram_mb', raw.peak_ram_mb, float(budget.ram_budget_mb)),
        ('disk_mb', raw.disk_mb, float(budget.disk_budget_mb)),
        ('output_mb', raw.output_mb, float(budget.max_output_mb)),
    )
    return [
        f'{name}={value} exceeds frozen budget {limit}'
        for name, value, limit in checks
        if value > limit
    ]


def _reference_observations(
    fixture,
    reference_path: Path,
) -> tuple[dict[str, RawObservableObservation], dict[str, object]]:
    payload = json.loads(reference_path.read_text(encoding='utf-8'))
    if payload.get('concave_reference_outcome') != 'pass':
        raise ValueError(
            f'independent reference is not qualified: '
            f'{payload.get("concave_reference_outcome")!r}'
        )
    # The artifact must belong to the same current benchmark/candidate manifest.
    # Exact hash checks are completed by the caller because the fixture alone
    # does not own manifest semantic identity.
    raw_payload = payload.get('details', {}).get('raw_reference_observation')
    if not isinstance(raw_payload, dict):
        raise ValueError('qualified independent reference artifact lacks raw_reference_observation')
    raw = RawFixtureObservation.model_validate(raw_payload)
    if raw.fixture_id != fixture.fixture_id:
        raise ValueError('independent reference fixture id differs from concave authority')
    raw_by_id = {item.observable_id: item for item in raw.observations}
    if set(raw_by_id) != {'lroom-fr', 'lroom-phase'}:
        raise ValueError('independent reference observable ids differ from concave authority')
    expected_by_id = {item.observable_id: item for item in fixture.observables}
    for observable_id, observation in raw_by_id.items():
        expected = expected_by_id[observable_id]
        if observation.kind != expected.kind or observation.unit != expected.unit:
            raise ValueError(
                f'independent reference {observable_id} kind/unit differs from concave authority: '
                f'{observation.kind}/{observation.unit} != {expected.kind}/{expected.unit}'
            )
    return raw_by_id, payload


def _cross_solver_evidence(
    fixture,
    candidate_raw: RawFixtureObservation,
    reference_by_id: dict[str, RawObservableObservation],
) -> tuple[BakeoffObservableEvidence, BakeoffObservableEvidence]:
    candidate_by_id = {item.observable_id: item for item in candidate_raw.observations}
    magnitude = next(item for item in fixture.observables if item.observable_id == 'lroom-fr')
    phase = next(item for item in fixture.observables if item.observable_id == 'lroom-phase')

    candidate_mag = {item.sample_key: item for item in candidate_by_id['lroom-fr'].samples}
    candidate_phase = {item.sample_key: item for item in candidate_by_id['lroom-phase'].samples}
    reference_mag = {item.sample_key: item for item in reference_by_id['lroom-fr'].samples}
    reference_phase = {item.sample_key: item for item in reference_by_id['lroom-phase'].samples}
    keys = set(reference_mag)
    if (
        set(candidate_mag) != keys
        or set(candidate_phase) != keys
        or set(reference_phase) != keys
    ):
        raise ValueError('candidate/reference concave sample grids differ')

    magnitude_abs: list[float] = []
    magnitude_rel: list[float] = []
    phase_error: list[float] = []
    for key in sorted(keys):
        ref_db = float(reference_mag[key].scalar_value)
        cand_db = float(candidate_mag[key].scalar_value)
        if ref_db >= float(magnitude.tolerance.null_mask_below_db):
            magnitude_abs.append(abs(cand_db - ref_db))
            ref_linear = 10.0 ** (ref_db / 20.0)
            cand_linear = 10.0 ** (cand_db / 20.0)
            magnitude_rel.append(
                abs(cand_linear - ref_linear) / max(ref_linear, 1.0e-300)
            )
        if ref_db >= float(phase.tolerance.null_mask_below_db):
            phase_error.append(
                _wrapped_phase_delta_deg(
                    float(candidate_phase[key].scalar_value),
                    float(reference_phase[key].scalar_value),
                )
            )

    if not magnitude_abs or not phase_error:
        raise ValueError('independent-reference null masks removed every concave sample')

    magnitude_evidence = BakeoffObservableEvidence(
        observable_id='lroom-fr',
        status='pass',
        summary='PFFDTD finest-grid magnitude compared against qualified independent solver reference.',
        absolute_error=max(magnitude_abs),
        relative_error=max(magnitude_rel),
    )
    phase_evidence = BakeoffObservableEvidence(
        observable_id='lroom-phase',
        status='pass',
        summary='PFFDTD finest-grid phase compared against qualified independent solver reference.',
        phase_error_deg=max(phase_error),
    )
    result: list[BakeoffObservableEvidence] = []
    for expected, evidence in (
        (magnitude, magnitude_evidence),
        (phase, phase_evidence),
    ):
        violations = observable_tolerance_violations(expected, evidence)
        if violations:
            evidence = evidence.model_copy(
                update={
                    'status': 'fail',
                    'summary': '; '.join(violations),
                }
            )
        result.append(evidence)
    return result[0], result[1]


def _blocked_fixture(
    candidate,
    reason: str,
) -> BakeoffFixtureEvidence:
    return BakeoffFixtureEvidence(
        fixture_id=FIXTURE_ID,
        status='blocked',
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        backend_version=candidate.source_commit_sha[:12],
        precision='float64',
        diagnostics=(reason,),
    )


def _execute(
    upstream_root: Path,
    work_dir: Path,
    output: Path,
    benchmark,
    candidates,
    *,
    reference_artifact: Path | None,
) -> tuple[RawFixtureObservation, BakeoffRun, dict[str, object]]:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    _validate_fixture_contract(fixture)
    if 'wave_rigid' not in candidate.probe_capabilities:
        raise RuntimeError('PFFDTD candidate does not authorize wave_rigid concave probe')

    actual_head = pffdtd_git_head(upstream_root)
    if actual_head != candidate.source_commit_sha:
        raise RuntimeError(
            f'PFFDTD checkout mismatch: expected {candidate.source_commit_sha}, got {actual_head}'
        )
    compatibility = apply_pffdtd_runtime_compatibility_patches(upstream_root)

    upstream_python = upstream_root / 'python'
    if not (upstream_python / 'sim_setup.py').is_file():
        raise RuntimeError(f'PFFDTD Python runtime missing: {upstream_python}')
    sys.path.insert(0, str(upstream_python))
    from sim_setup import sim_setup
    from fdtd.sim_fdtd import SimEngine

    frequencies_hz = _frequency_grid(fixture)
    work_dir.mkdir(parents=True, exist_ok=True)
    material_dir = work_dir / 'materials'
    material_dir.mkdir(parents=True, exist_ok=True)
    model_path = work_dir / 'r100a_concave_pffdtd.json'
    compiled_model = compile_rigid_fixture_model(fixture)
    rigid = compiled_model['mats_hash']['_RIGID']
    points = np.asarray(rigid['pts'], dtype=np.float64)
    triangles = np.asarray(rigid['tris'], dtype=np.int64)
    compiled_geometry = _validate_compiled_concave_mesh(points, triangles)
    model_path.write_text(
        json.dumps(compiled_model, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )

    monitor = PeakRssMonitor()
    monitor.start()
    levels: list[dict[str, object]] = []
    pressures: list[np.ndarray] = []
    archive_payload: dict[str, np.ndarray] = {}
    compile_s = 0.0
    solve_s = 0.0
    postprocess_s = 0.0
    solver_output_mb = 0.0
    try:
        for index, target_h_m in enumerate(GRID_SPACINGS_M):
            label = f'h_{str(target_h_m).replace(".", "p")}'
            level_dir = work_dir / label
            level_dir.mkdir(parents=True, exist_ok=True)
            detail, traces, level_compile, level_solve, level_post = _run_level(
                sim_setup=sim_setup,
                SimEngine=SimEngine,
                fixture=fixture,
                model_path=model_path,
                material_dir=material_dir,
                level_dir=level_dir,
                target_h_m=target_h_m,
                frequencies_hz=frequencies_hz,
                warm_jit=index == 0,
            )
            levels.append(detail)
            pressure = (
                np.asarray(traces['pressure_transfer_real_pa_per_m3_s'], dtype=np.float64)
                + 1j * np.asarray(traces['pressure_transfer_imag_pa_per_m3_s'], dtype=np.float64)
            )
            pressures.append(pressure)
            for key, value in traces.items():
                archive_payload[f'{label}_{key}'] = value
            compile_s += level_compile
            solve_s += level_solve
            postprocess_s += level_post
            solver_output_mb += float(detail['sim_outs_mb'])
    finally:
        peak_ram_mb = monitor.stop()

    evidence_ref = f'artifact:{output.as_posix()}'
    output.parent.mkdir(parents=True, exist_ok=True)
    signal_path = output.with_name('pffdtd_concave_signals.npz')
    np.savez_compressed(signal_path, **archive_payload)
    signal_sha = _file_sha256(signal_path)
    signal_mb = signal_path.stat().st_size / (1024.0 * 1024.0)
    disk_mb = _directory_size_mb(work_dir) + signal_mb
    output_mb = solver_output_mb + signal_mb

    convergence_started = time.perf_counter()
    convergence, convergence_qualification, self_qualified = _self_convergence(
        fixture,
        pressures,
    )
    postprocess_s += time.perf_counter() - convergence_started

    raw = _raw_candidate_observation(
        fixture,
        frequencies_hz,
        pressures[-1],
        evidence_ref=evidence_ref,
        backend_version=candidate.source_commit_sha[:12],
        compile_s=compile_s,
        solve_s=solve_s,
        postprocess_s=postprocess_s,
        peak_ram_mb=peak_ram_mb,
        disk_mb=disk_mb,
        output_mb=output_mb,
    )
    resource_violations = _resource_violations(fixture, raw)

    reference_status: dict[str, object] = {
        'supplied': reference_artifact is not None,
        'qualified': False,
    }
    if not self_qualified:
        fixture_evidence = BakeoffFixtureEvidence(
            fixture_id=fixture.fixture_id,
            status='fail',
            evidence_ref=evidence_ref,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            backend_version=candidate.source_commit_sha[:12],
            precision='float64',
            compile_s=compile_s,
            solve_s=solve_s,
            postprocess_s=postprocess_s,
            peak_ram_mb=peak_ram_mb,
            disk_mb=disk_mb,
            output_mb=output_mb,
            diagnostics=(
                'Candidate failed its pre-reference exact-concave grid-convergence qualification.',
                *tuple(str(item) for item in convergence_qualification['violations']),
                'No independent-reference comparison was scored; R100A observable evidence remains absent.',
            ),
        )
    elif resource_violations:
        fixture_evidence = BakeoffFixtureEvidence(
            fixture_id=fixture.fixture_id,
            status='fail',
            evidence_ref=evidence_ref,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            backend_version=candidate.source_commit_sha[:12],
            precision='float64',
            compile_s=compile_s,
            solve_s=solve_s,
            postprocess_s=postprocess_s,
            peak_ram_mb=peak_ram_mb,
            disk_mb=disk_mb,
            output_mb=output_mb,
            diagnostics=tuple(
                [
                    'Candidate self-convergence passed but frozen resource budget failed.',
                    'No independent-reference comparison was scored; R100A observable evidence remains absent.',
                ]
                + resource_violations
            ),
        )
    elif reference_artifact is None:
        reason = (
            'No qualified current independent_solver reference artifact was supplied. '
            'The current MFEM concave reference is FAIL and its finest trace must not be '
            'promoted to expected authority.'
        )
        fixture_evidence = _blocked_fixture(candidate, reason)
        reference_status['reason'] = reason
    else:
        payload = json.loads(reference_artifact.read_text(encoding='utf-8'))
        reference_errors: list[str] = []
        if payload.get('r100a_semantic_hash') != benchmark.semantic_hash():
            reference_errors.append('independent reference R100A semantic hash is stale')
        if payload.get('candidate_manifest_hash') != candidates.semantic_hash():
            reference_errors.append('independent reference candidate-manifest hash is stale')
        if payload.get('concave_reference_outcome') != 'pass':
            reference_errors.append(
                f'independent reference outcome is {payload.get("concave_reference_outcome")!r}, not PASS'
            )

        if reference_errors:
            reason = '; '.join(reference_errors)
            fixture_evidence = _blocked_fixture(candidate, reason)
            reference_status.update(
                {
                    'qualified': False,
                    'artifact_schema': payload.get('schema_version'),
                    'reference_run_id': payload.get('bakeoff_run', {}).get('run_id'),
                    'reason': reason,
                }
            )
        else:
            reference_by_id, payload = _reference_observations(
                fixture,
                reference_artifact,
            )
            cross_evidence = _cross_solver_evidence(fixture, raw, reference_by_id)
            reference_status.update(
                {
                    'qualified': True,
                    'artifact_schema': payload.get('schema_version'),
                    'reference_run_id': payload.get('bakeoff_run', {}).get('run_id'),
                }
            )
            status = 'pass' if all(item.status == 'pass' for item in cross_evidence) else 'fail'
            fixture_evidence = BakeoffFixtureEvidence(
                fixture_id=fixture.fixture_id,
                status=status,
                evidence_ref=evidence_ref,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                backend_version=candidate.source_commit_sha[:12],
                precision='float64',
                compile_s=compile_s,
                solve_s=solve_s,
                postprocess_s=postprocess_s,
                peak_ram_mb=peak_ram_mb,
                disk_mb=disk_mb,
                output_mb=output_mb,
                observables=cross_evidence,
                diagnostics=(
                    'Candidate passed pre-reference grid-convergence qualification.',
                    'Finest h=0.125 m trace is scored only against a separately qualified independent reference.',
                ),
            )

    run = BakeoffRun(
        run_id=f'pffdtd-concave-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(),
        fixture_evidence=(fixture_evidence,),
        hard_gates=_hard_gates(evidence_ref, reproducible=True),
        notes=(
            'Exact concave candidate evidence; no rectangular approximation is permitted.',
            'Candidate self-convergence and independent-reference scoring are separate gates.',
            'PFFDTD Tc is used only as a numerical control to reproduce the frozen 343.0 m/s wave speed; physical temperature authority remains 20 C and no air-absorption postprocess is applied.',
            'An unqualified independent reference cannot be used as expected truth.',
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)

    details = {
        'candidate_source_commit_sha': candidate.source_commit_sha,
        'compatibility_patch': compatibility,
        'source_provenance': _source_provenance(upstream_root),
        'compiled_model': {
            'model_json_sha256': _file_sha256(model_path),
            **compiled_geometry,
        },
        'probe_controls': {
            'fmax_hz': FMAX_HZ,
            'grid_spacings_m': list(GRID_SPACINGS_M),
            'thread_budget': min(THREAD_BUDGET, os.cpu_count() or 1),
            'setup_processes': NPROCS,
            'finite_record_transfer': fixture.comparison.finite_record_transfer.model_dump(mode='json'),
            'solver_time_step_policy': 'solver_native_recorded',
            'comparison_grid_hz': {
                'start': float(frequencies_hz[0]),
                'stop': float(frequencies_hz[-1]),
                'step': float(frequencies_hz[1] - frequencies_hz[0]),
                'count': int(frequencies_hz.size),
            },
        },
        'runtime_versions': _runtime_versions(),
        'levels': levels,
        'self_convergence': convergence,
        'self_convergence_qualification': convergence_qualification,
        'self_convergence_qualified': self_qualified,
        'resource_violations': resource_violations,
        'reference_status': reference_status,
        'raw_candidate_observation': raw.model_dump(mode='json'),
        'resources': {
            'compile_s': compile_s,
            'solve_s': solve_s,
            'postprocess_s': postprocess_s,
            'peak_ram_mb': peak_ram_mb,
            'disk_mb': disk_mb,
            'output_mb': output_mb,
        },
        'signal_archive': signal_path.name,
        'signal_archive_sha256': signal_sha,
    }
    return raw, run, details


def _blocked_run(
    benchmark,
    candidates,
    evidence_ref: str,
    reason: str,
) -> BakeoffRun:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    _validate_fixture_contract(fixture)
    run = BakeoffRun(
        run_id=f'pffdtd-concave-blocked-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(),
        fixture_evidence=(
            _blocked_fixture(candidate, reason),
        ),
        hard_gates=_hard_gates(evidence_ref, reproducible=False),
        notes=(
            'PFFDTD exact-concave probe did not produce candidate numerical evidence.',
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
    raw: RawFixtureObservation | None,
    run: BakeoffRun,
    details: dict[str, object],
    error: str | None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema_version': PROBE_SCHEMA,
        'probe_id': PROBE_ID,
        'workflow_result_is_fixture_result': False,
        'probe_outcome': run.fixture_evidence[0].status,
        'r100a_manifest_id': benchmark.manifest_id,
        'r100a_semantic_hash': benchmark.semantic_hash(),
        'candidate_manifest_hash': candidates.semantic_hash(),
        'raw_candidate_observation': (
            raw.model_dump(mode='json') if raw is not None else None
        ),
        'bakeoff_run': run.model_dump(mode='json'),
        'bakeoff_run_semantic_hash': run.semantic_hash(),
        'details': details,
        'error': error,
    }
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run R100B PFFDTD exact-concave candidate probe'
    )
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--candidates', required=True, type=Path)
    parser.add_argument('--upstream-root', required=True, type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--reference-artifact', type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    benchmark = load_acoustic_benchmark_manifest(args.manifest)
    candidates = load_bakeoff_candidate_manifest(args.candidates)
    evidence_ref = f'artifact:{args.output.as_posix()}'

    raw: RawFixtureObservation | None = None
    details: dict[str, object] = {}
    error: str | None = None
    try:
        raw, run, details = _execute(
            args.upstream_root,
            args.work_dir,
            args.output,
            benchmark,
            candidates,
            reference_artifact=args.reference_artifact,
        )
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        details = {
            'traceback': traceback.format_exc(),
            'runtime_versions': _runtime_versions(),
            'upstream_root': str(args.upstream_root),
            'source_provenance': _source_provenance(args.upstream_root),
        }
        run = _blocked_run(benchmark, candidates, evidence_ref, error)

    _write_artifact(
        args.output,
        benchmark,
        candidates,
        raw=raw,
        run=run,
        details=details,
        error=error,
    )
    print(
        json.dumps(
            {
                'probe_id': PROBE_ID,
                'probe_outcome': run.fixture_evidence[0].status,
                'workflow_result_is_fixture_result': False,
                'run_id': run.run_id,
                'run_semantic_hash': run.semantic_hash(),
                'r100a_semantic_hash': benchmark.semantic_hash(),
                'candidate_manifest_hash': candidates.semantic_hash(),
                'error': error,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
