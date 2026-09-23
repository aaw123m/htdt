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
    BakeoffPlatform,
    BakeoffRun,
    load_bakeoff_candidate_manifest,
    validate_bakeoff_run,
)
from htdt.acoustic_bakeoff_observation import (
    RawConvergenceLevel,
    RawObservationSample,
    evaluate_monotonic_convergence_observable,
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
FIXTURE_ID = 'wave-rectangular-convergence-v1'
PROBE_SCHEMA = 'r100b-pffdtd-complex-convergence-artifact-1'
PROBE_ID = 'pffdtd-rectangular-complex-pressure-convergence'
ADAPTER_ID = 'htdt-r100b-pffdtd-complex-pressure-convergence'
ADAPTER_VERSION = '2'
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
        device_notes='GitHub-hosted Windows PFFDTD Python/Numba CPU complex-pressure convergence probe.',
    )


def _hard_gates(evidence_ref: str, *, reproducible: bool) -> tuple[BakeoffHardGateEvidence, ...]:
    categories = (
        'physics_correctness',
        'cpu_baseline',
        'windows_packaging',
        'license_redistribution',
        'required_capability',
        'reproducible_authority',
    )
    items: list[BakeoffHardGateEvidence] = []
    for category in categories:
        if category == 'reproducible_authority' and reproducible:
            items.append(
                BakeoffHardGateEvidence(
                    category=category,
                    status='pass',
                    evidence_ref=evidence_ref,
                    summary=(
                        'Pinned PFFDTD source, compatibility diff, exact R100A density/source/time/frequency '
                        'authority, all refinement controls, complex pressure spectra and resources are recorded.'
                    ),
                )
            )
        else:
            items.append(
                BakeoffHardGateEvidence(
                    category=category,
                    status='not_run',
                    summary=(
                        'Candidate-wide hard gate remains open. This run evaluates only the '
                        'R100A rectangular complex-pressure convergence fixture.'
                    ),
                )
            )
    return tuple(items)


def _directory_size_mb(path: Path) -> float:
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file()) / (1024.0 * 1024.0)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _frequency_grid(fixture) -> np.ndarray:
    grid = fixture.comparison.frequency_grid
    if grid.kind != 'uniform':
        raise ValueError('PFFDTD convergence probe requires the R100A uniform frequency grid')
    assert grid.start_hz is not None
    assert grid.stop_hz is not None
    assert grid.step_hz is not None
    count_float = (grid.stop_hz - grid.start_hz) / grid.step_hz
    count = int(round(count_float))
    if not math.isclose(count_float, count, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError('R100A uniform frequency range is not an integer number of steps')
    return grid.start_hz + np.arange(count + 1, dtype=np.float64) * grid.step_hz


def _validate_fixture_contract(fixture) -> None:
    if fixture.comparison.fourier_sign != 'exp(-i*omega*t)':
        raise ValueError('PFFDTD pressure adapter supports only exp(-i*omega*t)')
    if fixture.comparison.time_zero_reference != 'source_excitation_t0':
        raise ValueError('PFFDTD pressure adapter requires source_excitation_t0')
    if fixture.comparison.window != 'none' or fixture.comparison.filter != 'none':
        raise ValueError('PFFDTD convergence probe requires no window and no filter')
    if fixture.comparison.interpolation != 'linear_complex':
        raise ValueError('PFFDTD convergence probe requires linear_complex comparison')
    if fixture.comparison.observation_time_s is None:
        raise ValueError('PFFDTD convergence probe requires observation_time_s')
    if fixture.comparison.time_step_s is not None:
        raise ValueError('R100A-4 convergence time_step_s must remain solver-native')
    finite_record = fixture.comparison.finite_record_transfer
    if finite_record is None:
        raise ValueError('PFFDTD convergence probe requires R100A-4 finite-record transfer authority')
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
        raise ValueError('PFFDTD convergence finite-record transfer authority changed')
    if len(fixture.sources) != 1 or len(fixture.receivers) != 1:
        raise ValueError('PFFDTD convergence probe requires one source and one receiver')
    source = fixture.sources[0]
    if source.normalization != 'volume_velocity_m3_s':
        raise ValueError('PFFDTD pressure adapter requires volume_velocity_m3_s source authority')
    if source.directivity != 'omnidirectional':
        raise ValueError('PFFDTD pressure adapter requires omnidirectional source authority')
    if not math.isclose(source.amplitude, 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError('PFFDTD first convergence slice requires unit volume-velocity amplitude')
    if not math.isclose(source.phase_deg, 0.0, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError('PFFDTD first convergence slice requires zero source phase')
    if fixture.environment.density_kg_m3 <= 0.0:
        raise ValueError('PFFDTD pressure adapter requires explicit positive air density')
    if len(fixture.observables) != 1:
        raise ValueError('PFFDTD convergence probe requires exactly one convergence observable')
    observable = fixture.observables[0]
    if (
        observable.kind != 'complex_pressure_transfer_pa_per_m3_s'
        or observable.unit != 'Pa/(m3/s)'
        or observable.acceptance_relation != 'monotonic_convergence'
    ):
        raise ValueError('PFFDTD convergence probe received incompatible P/Q observable authority')


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
) -> tuple[RawConvergenceLevel, dict[str, object], dict[str, np.ndarray], float, float, float]:
    authority_c = float(fixture.environment.sound_speed_m_s)
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
    if not math.isclose(float(engine.h), target_h_m, rel_tol=0.0, abs_tol=1e-12):
        raise RuntimeError(f'PFFDTD grid spacing mismatch: {engine.h} != {target_h_m}')
    if not math.isclose(float(engine.c), authority_c, rel_tol=0.0, abs_tol=1e-12):
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
        receiver_count=len(fixture.receivers),
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
    post_s = time.perf_counter() - post_started

    samples = tuple(
        RawObservationSample(
            sample_key=f'H@{float(frequency_hz):g}Hz',
            frequency_hz=float(frequency_hz),
            real_value=float(value.real),
            imag_value=float(value.imag),
        )
        for frequency_hz, value in zip(frequencies_hz, pressure_transfer)
    )
    level = RawConvergenceLevel(
        level_id=f'h={target_h_m:g}m',
        refinement_parameter='cartesian_grid_spacing_m',
        refinement_value=target_h_m,
        samples=samples,
        diagnostics=(
            f'PFFDTD internal sound speed={float(engine.c):.12g} m/s',
            f'R100A sound speed={float(fixture.environment.sound_speed_m_s):.12g} m/s',
            f'R100A density={float(fixture.environment.density_kg_m3):.12g} kg/m3',
            'PFFDTD velocity potential is converted to a physical pressure time record with the declared second-order adapter derivative p=rho*d(phi)/dt.',
            'R100A-4 transfer uses dt-weighted direct scored-frequency analysis DTFT of the pressure record and physical volume-velocity source record, then P_T/Q_T.',
        ),
    )
    detail = {
        'level_id': level.level_id,
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
        'receiver_pressure_pa': pressure_record,
        'source_volume_velocity_m3_s': physical_source,
        'pressure_transfer_real_pa_per_m3_s': pressure_transfer.real,
        'pressure_transfer_imag_pa_per_m3_s': pressure_transfer.imag,
        'frequency_hz': frequencies_hz,
    }
    compile_s = setup_s + warm_prepare_s + jit_s + prepare_s
    return level, detail, traces, compile_s, solve_s, post_s


def _execute(
    upstream_root: Path,
    work_dir: Path,
    output: Path,
    benchmark,
    candidates,
) -> tuple[BakeoffRun, dict[str, object]]:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    _validate_fixture_contract(fixture)

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
    if frequencies_hz[0] < 20.0 or frequencies_hz[-1] > FMAX_HZ:
        raise ValueError(
            f'comparison grid {frequencies_hz[0]}..{frequencies_hz[-1]} Hz '
            f'is outside probe support 20..{FMAX_HZ} Hz'
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    material_dir = work_dir / 'materials'
    material_dir.mkdir(parents=True, exist_ok=True)
    model_path = work_dir / 'r100a_convergence_pffdtd.json'
    model_path.write_text(
        json.dumps(compile_rigid_fixture_model(fixture), indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )

    monitor = PeakRssMonitor()
    monitor.start()
    levels: list[RawConvergenceLevel] = []
    level_details: list[dict[str, object]] = []
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
            level, detail, traces, level_compile, level_solve, level_post = _run_level(
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
            levels.append(level)
            level_details.append(detail)
            for key, value in traces.items():
                archive_payload[f'{label}_{key}'] = value
            compile_s += level_compile
            solve_s += level_solve
            postprocess_s += level_post
            solver_output_mb += float(detail['sim_outs_mb'])
    finally:
        peak_ram_mb = monitor.stop()

    observable = fixture.observables[0]
    evaluation_started = time.perf_counter()
    observable_evidence = evaluate_monotonic_convergence_observable(
        observable,
        tuple(levels),
    )
    postprocess_s += time.perf_counter() - evaluation_started

    output.parent.mkdir(parents=True, exist_ok=True)
    signal_path = output.with_name('pffdtd_complex_convergence_signals.npz')
    np.savez_compressed(signal_path, **archive_payload)
    signal_sha = _file_sha256(signal_path)
    signal_mb = signal_path.stat().st_size / (1024.0 * 1024.0)
    disk_mb = _directory_size_mb(work_dir) + signal_mb
    output_mb = solver_output_mb + signal_mb

    status = 'pass' if observable_evidence.status == 'pass' else 'fail'
    evidence_ref = f'artifact:{output.as_posix()}'
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
        observables=(observable_evidence,),
        diagnostics=(
            'Three nested Cartesian grids use the exact R100A geometry/source/receiver authority.',
            'Full R100A 2.0 s record is solved at each grid; no 256-step modal shortcut is used.',
            'No window, filter or frequency interpolation is applied.',
            'Complex pressure is evaluated on the exact 20..300 Hz / 1 Hz R100A grid.',
            f'signal_archive={signal_path.name}',
            f'signal_archive_sha256={signal_sha}',
        ),
    )

    run = BakeoffRun(
        run_id=f'pffdtd-complex-convergence-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(),
        fixture_evidence=(fixture_evidence,),
        hard_gates=_hard_gates(evidence_ref, reproducible=True),
        notes=(
            'Fixture-scoped PFFDTD complex-pressure grid-convergence evidence only.',
            'A failing convergence fixture remains candidate evidence and does not fail repository CI.',
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)

    details = {
        'candidate_source_commit_sha': candidate.source_commit_sha,
        'compatibility_patch': compatibility,
        'probe_controls': {
            'fmax_hz': FMAX_HZ,
            'grid_spacings_m': list(GRID_SPACINGS_M),
            'observation_time_s': float(fixture.comparison.observation_time_s),
            'frequency_start_hz': float(frequencies_hz[0]),
            'frequency_stop_hz': float(frequencies_hz[-1]),
            'frequency_step_hz': float(frequencies_hz[1] - frequencies_hz[0]),
            'frequency_count': int(frequencies_hz.size),
            'fourier_sign': fixture.comparison.fourier_sign,
            'window': fixture.comparison.window,
            'filter': fixture.comparison.filter,
            'source_normalization': fixture.sources[0].normalization,
            'source_amplitude': fixture.sources[0].amplitude,
            'source_phase_deg': fixture.sources[0].phase_deg,
            'finite_record_transfer': fixture.comparison.finite_record_transfer.model_dump(mode='json'),
            'solver_time_step_policy': 'solver_native_recorded',
            'density_kg_m3': fixture.environment.density_kg_m3,
            'thread_budget': min(THREAD_BUDGET, os.cpu_count() or 1),
            'setup_processes': NPROCS,
        },
        'runtime_versions': _runtime_versions(),
        'levels': level_details,
        'convergence_levels': [level.model_dump(mode='json') for level in levels],
        'observable_evidence': observable_evidence.model_dump(mode='json'),
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
    return run, details


def _blocked_run(benchmark, candidates, evidence_ref: str, reason: str) -> BakeoffRun:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    run = BakeoffRun(
        run_id=f'pffdtd-complex-convergence-blocked-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(),
        fixture_evidence=(
            BakeoffFixtureEvidence(
                fixture_id=fixture.fixture_id,
                status='blocked',
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                backend_version=candidate.source_commit_sha[:12],
                precision='float64',
                diagnostics=(reason,),
            ),
        ),
        hard_gates=_hard_gates(evidence_ref, reproducible=False),
        notes=('Complex-pressure convergence probe did not produce numerical evidence.', reason),
    )
    validate_bakeoff_run(benchmark, candidates, run)
    return run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run R100B PFFDTD complex-pressure grid convergence probe')
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--candidates', required=True, type=Path)
    parser.add_argument('--upstream-root', required=True, type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    benchmark = load_acoustic_benchmark_manifest(args.manifest)
    candidates = load_bakeoff_candidate_manifest(args.candidates)
    evidence_ref = f'artifact:{args.output.as_posix()}'
    error: str | None = None
    details: dict[str, object] = {}

    try:
        run, details = _execute(
            args.upstream_root,
            args.work_dir,
            args.output,
            benchmark,
            candidates,
        )
        outcome = run.fixture_evidence[0].status
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        details = {
            'traceback': traceback.format_exc(),
            'runtime_versions': _runtime_versions(),
            'upstream_root': str(args.upstream_root),
        }
        run = _blocked_run(benchmark, candidates, evidence_ref, error)
        outcome = 'blocked'

    payload = {
        'schema_version': PROBE_SCHEMA,
        'probe_id': PROBE_ID,
        'probe_outcome': outcome,
        'r100a_manifest_id': benchmark.manifest_id,
        'r100a_semantic_hash': benchmark.semantic_hash(),
        'candidate_manifest_hash': candidates.semantic_hash(),
        'bakeoff_run': run.model_dump(mode='json'),
        'details': details,
        'error': error,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    print(
        json.dumps(
            {
                'probe_id': PROBE_ID,
                'probe_outcome': outcome,
                'run_id': run.run_id,
                'fixture_status': run.fixture_evidence[0].status,
                'observable_statuses': {
                    item.observable_id: item.status
                    for item in run.fixture_evidence[0].observables
                },
                'error': error,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
