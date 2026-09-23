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
    RawFixtureObservation,
    RawObservableObservation,
    RawObservationSample,
    evaluate_sampled_fixture,
)
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest
from htdt.acoustic_pffdtd_adapter import (
    apply_pffdtd_runtime_compatibility_patches,
    compile_rigid_fixture_model,
    pffdtd_git_head,
    recombine_pffdtd_receiver_traces,
)


CANDIDATE_ID = 'pffdtd-main-aa319f6'
FIXTURE_ID = 'wave-rigid-rectangular-modes-v1'
PROBE_SCHEMA = 'r100b-pffdtd-rigid-modes-artifact-1'
PROBE_ID = 'pffdtd-rigid-rectangular-modes'
ADAPTER_ID = 'htdt-r100b-pffdtd-rigid-mode-convergence'
ADAPTER_VERSION = '1'
FMAX_HZ = 100.0
DURATION_S = 0.75
GRID_SPACINGS_M = (0.5, 0.25, 0.125)
THREAD_BUDGET = 4
NPROCS = 1
ZERO_PAD_FACTOR = 64
SEARCH_HALF_WIDTH_HZ = 2.5
RICHARDSON_ORDER = 2


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
        device_notes='GitHub-hosted Windows PFFDTD Python/Numba CPU rigid-mode convergence probe.',
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
                        'Pinned PFFDTD source, compatibility diff, grid controls, raw traces, '
                        'mode estimator, resources and R100 authority hashes are recorded.'
                    ),
                )
            )
        else:
            items.append(
                BakeoffHardGateEvidence(
                    category=category,
                    status='not_run',
                    summary=(
                        'Candidate-wide hard gate remains open. This run scores only the '
                        'R100A rigid rectangular eigenfrequency fixture.'
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


def _next_power_of_two(value: int) -> int:
    return 1 << max(0, value - 1).bit_length()


def _estimate_mode_frequency(
    trace: np.ndarray,
    time_step_s: float,
    expected_hz: float,
) -> dict[str, float | int]:
    signal = np.asarray(trace, dtype=np.float64)
    if signal.ndim != 1 or signal.size < 64:
        raise ValueError(f'mode estimator requires a 1D trace with >=64 samples, got {signal.shape}')
    if not np.all(np.isfinite(signal)):
        raise ValueError('mode estimator input must be finite')

    centered = signal - float(np.mean(signal))
    window = np.hanning(centered.size)
    windowed = centered * window
    nfft = _next_power_of_two(centered.size * ZERO_PAD_FACTOR)
    spectrum = np.abs(np.fft.rfft(windowed, n=nfft))
    frequencies = np.fft.rfftfreq(nfft, d=time_step_s)

    lo = expected_hz - SEARCH_HALF_WIDTH_HZ
    hi = expected_hz + SEARCH_HALF_WIDTH_HZ
    indices = np.flatnonzero((frequencies >= lo) & (frequencies <= hi))
    if indices.size < 3:
        raise ValueError(f'frequency search window has too few bins around {expected_hz} Hz')
    local = spectrum[indices]
    peak_index = int(indices[int(np.argmax(local))])
    if peak_index <= 0 or peak_index >= spectrum.size - 1:
        raise ValueError('mode estimator peak lies on spectrum boundary')

    three = np.log(np.maximum(spectrum[peak_index - 1:peak_index + 2], np.finfo(float).tiny))
    denominator = three[0] - 2.0 * three[1] + three[2]
    delta = 0.0 if denominator == 0.0 else 0.5 * (three[0] - three[2]) / denominator
    delta = float(np.clip(delta, -1.0, 1.0))
    estimated_hz = (peak_index + delta) / (nfft * time_step_s)

    local_median = float(np.median(local))
    peak_to_median = float(spectrum[peak_index] / max(local_median, np.finfo(float).tiny))
    return {
        'estimated_hz': float(estimated_hz),
        'expected_hz': float(expected_hz),
        'search_lo_hz': float(lo),
        'search_hi_hz': float(hi),
        'native_bin_hz': float(1.0 / (centered.size * time_step_s)),
        'zero_padded_bin_hz': float(1.0 / (nfft * time_step_s)),
        'nfft': int(nfft),
        'peak_to_local_median': peak_to_median,
    }


def _prepare_engine(SimEngine, sim_dir: Path):
    engine = SimEngine(sim_dir, energy_on=False, nthreads=min(THREAD_BUDGET, os.cpu_count() or 1))
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
    warm_jit: bool,
) -> tuple[dict[str, object], np.ndarray, np.ndarray, np.ndarray, float, float, float]:
    internal_c = 343.2 * math.sqrt(float(fixture.environment.temperature_c) / 20.0)
    ppw = internal_c / (FMAX_HZ * target_h_m)

    setup_started = time.perf_counter()
    sim_setup(
        insig_type='impulse',
        fmax=FMAX_HZ,
        PPW=ppw,
        save_folder=level_dir,
        model_json_file=model_path,
        mat_folder=material_dir,
        mat_files_dict={},
        duration=DURATION_S,
        Tc=float(fixture.environment.temperature_c),
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

    solve_started = time.perf_counter()
    engine.run_all(nsteps=max(1, min(256, int(engine.Nt))))
    solve_s = time.perf_counter() - solve_started

    post_started = time.perf_counter()
    engine.save_outputs()
    output_path = level_dir / 'sim_outs.h5'
    with h5py.File(output_path, 'r') as handle:
        raw_grid = np.asarray(handle['u_out'][...], dtype=np.float64)
    receiver = recombine_pffdtd_receiver_traces(
        raw_grid,
        engine.out_alpha,
        receiver_count=len(fixture.receivers),
        nt=int(engine.Nt),
    )
    if float(np.max(np.abs(receiver))) <= 0.0:
        raise RuntimeError(f'PFFDTD receiver trace is zero at h={target_h_m}')

    estimates: dict[str, dict[str, float | int]] = {}
    for observable in fixture.observables:
        if len(observable.samples) != 1 or observable.kind != 'eigenfrequency_hz':
            raise RuntimeError(
                f'rigid mode probe requires one eigenfrequency sample per observable: '
                f'{observable.observable_id}'
            )
        expected_hz = observable.samples[0].scalar_value
        if expected_hz is None:
            raise RuntimeError(f'{observable.observable_id} has no scalar expected frequency')
        estimates[observable.observable_id] = _estimate_mode_frequency(
            receiver[0],
            float(engine.Ts),
            float(expected_hz),
        )
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
        'sound_speed_m_s': float(engine.c),
        'courant': float(engine.l),
        'setup_s': setup_s,
        'warm_prepare_s': warm_prepare_s,
        'jit_s': jit_s,
        'engine_prepare_s': prepare_s,
        'solve_s': solve_s,
        'postprocess_s': post_s,
        'sim_outs_mb': output_path.stat().st_size / (1024.0 * 1024.0),
        'generated_disk_mb': _directory_size_mb(level_dir),
        'raw_grid_shape': list(raw_grid.shape),
        'receiver_shape': list(receiver.shape),
        'receiver_max_abs': float(np.max(np.abs(receiver))),
        'mode_estimates': estimates,
    }
    compile_s = setup_s + warm_prepare_s + jit_s + prepare_s
    return (
        detail,
        raw_grid,
        receiver,
        np.asarray(engine.out_alpha, dtype=np.float64).copy(),
        compile_s,
        solve_s,
        post_s,
    )


def _execute(
    upstream_root: Path,
    work_dir: Path,
    output: Path,
    benchmark,
    candidates,
) -> tuple[RawFixtureObservation, BakeoffRun, dict[str, object]]:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
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

    work_dir.mkdir(parents=True, exist_ok=True)
    material_dir = work_dir / 'materials'
    material_dir.mkdir(parents=True, exist_ok=True)
    model_path = work_dir / 'r100a_rigid_modes_pffdtd.json'
    model_path.write_text(
        json.dumps(compile_rigid_fixture_model(fixture), indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )

    monitor = PeakRssMonitor()
    monitor.start()
    levels: list[dict[str, object]] = []
    traces: dict[str, np.ndarray] = {}
    compile_s = 0.0
    solve_s = 0.0
    postprocess_s = 0.0
    output_mb = 0.0
    try:
        for index, target_h_m in enumerate(GRID_SPACINGS_M):
            label = f'h_{str(target_h_m).replace(".", "p")}'
            level_dir = work_dir / label
            level_dir.mkdir(parents=True, exist_ok=True)
            (
                detail,
                raw_grid,
                receiver,
                out_alpha,
                level_compile,
                level_solve,
                level_post,
            ) = _run_level(
                sim_setup=sim_setup,
                SimEngine=SimEngine,
                fixture=fixture,
                model_path=model_path,
                material_dir=material_dir,
                level_dir=level_dir,
                target_h_m=target_h_m,
                warm_jit=index == 0,
            )
            levels.append(detail)
            traces[f'{label}_raw_grid'] = raw_grid
            traces[f'{label}_receiver'] = receiver
            traces[f'{label}_out_alpha'] = out_alpha
            compile_s += level_compile
            solve_s += level_solve
            postprocess_s += level_post
            output_mb += float(detail['sim_outs_mb'])
    finally:
        peak_ram_mb = monitor.stop()

    if len(levels) != 3:
        raise RuntimeError(f'expected three PFFDTD refinement levels, got {len(levels)}')

    coarse, medium, fine = levels
    raw_observations: list[RawObservableObservation] = []
    convergence: dict[str, object] = {}
    for observable in fixture.observables:
        oid = observable.observable_id
        coarse_hz = float(coarse['mode_estimates'][oid]['estimated_hz'])
        medium_hz = float(medium['mode_estimates'][oid]['estimated_hz'])
        fine_hz = float(fine['mode_estimates'][oid]['estimated_hz'])
        medium_delta = abs(medium_hz - coarse_hz)
        fine_delta = abs(fine_hz - medium_hz)
        if fine_delta >= medium_delta:
            raise RuntimeError(
                f'{oid} does not show decreasing grid-refinement delta: '
                f'coarse->medium={medium_delta}, medium->fine={fine_delta}'
            )

        factor = (GRID_SPACINGS_M[1] / GRID_SPACINGS_M[2]) ** RICHARDSON_ORDER
        extrapolated_hz = fine_hz + (fine_hz - medium_hz) / (factor - 1.0)
        sample = observable.samples[0]
        raw_observations.append(
            RawObservableObservation(
                observable_id=oid,
                kind='eigenfrequency_hz',
                unit='Hz',
                samples=(
                    RawObservationSample(
                        sample_key=sample.sample_key,
                        scalar_value=extrapolated_hz,
                    ),
                ),
                diagnostics=(
                    f'h={GRID_SPACINGS_M[0]}m: {coarse_hz:.9f} Hz',
                    f'h={GRID_SPACINGS_M[1]}m: {medium_hz:.9f} Hz',
                    f'h={GRID_SPACINGS_M[2]}m: {fine_hz:.9f} Hz',
                    f'Richardson p={RICHARDSON_ORDER}: {extrapolated_hz:.9f} Hz',
                ),
            )
        )
        convergence[oid] = {
            'coarse_hz': coarse_hz,
            'medium_hz': medium_hz,
            'fine_hz': fine_hz,
            'coarse_to_medium_delta_hz': medium_delta,
            'medium_to_fine_delta_hz': fine_delta,
            'delta_ratio': medium_delta / max(fine_delta, np.finfo(float).tiny),
            'richardson_order': RICHARDSON_ORDER,
            'extrapolated_hz': extrapolated_hz,
        }

    output.parent.mkdir(parents=True, exist_ok=True)
    signal_path = output.with_name('pffdtd_rigid_modes_signals.npz')
    np.savez_compressed(signal_path, **traces)
    signal_sha = _file_sha256(signal_path)
    generated_disk_mb = _directory_size_mb(work_dir)

    evidence_ref = f'artifact:{output.as_posix()}'
    raw = RawFixtureObservation(
        fixture_id=fixture.fixture_id,
        evidence_ref=evidence_ref,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        backend_version=candidate.source_commit_sha[:12],
        precision='float64',
        compile_s=compile_s,
        solve_s=solve_s,
        postprocess_s=postprocess_s,
        peak_ram_mb=peak_ram_mb,
        disk_mb=generated_disk_mb,
        output_mb=output_mb,
        observations=tuple(raw_observations),
        diagnostics=(
            'Three nested Cartesian grid spacings divide all room dimensions exactly.',
            'Final observable uses second-order Richardson extrapolation from h=0.25 and h=0.125 m.',
            'h=0.5 m level is retained as an independent decreasing-delta convergence check.',
            f'PFFDTD internal sound speed is {float(fine["sound_speed_m_s"]):.9f} m/s; '
            f'R100A authority is {fixture.environment.sound_speed_m_s:.9f} m/s.',
            f'signal_archive={signal_path.name}',
            f'signal_archive_sha256={signal_sha}',
        ),
    )
    fixture_evidence = evaluate_sampled_fixture(fixture, raw)

    run = BakeoffRun(
        run_id=f'pffdtd-rigid-modes-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(),
        fixture_evidence=(fixture_evidence,),
        hard_gates=_hard_gates(evidence_ref, reproducible=True),
        notes=(
            'Fixture-scoped rigid rectangular modal validation only.',
            'Candidate-wide physics/cpu/windows gates remain open until all applicable R100B evidence exists.',
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)

    details = {
        'candidate_source_commit_sha': candidate.source_commit_sha,
        'compatibility_patch': compatibility,
        'probe_controls': {
            'fmax_hz': FMAX_HZ,
            'duration_s': DURATION_S,
            'grid_spacings_m': list(GRID_SPACINGS_M),
            'zero_pad_factor': ZERO_PAD_FACTOR,
            'search_half_width_hz': SEARCH_HALF_WIDTH_HZ,
            'richardson_order': RICHARDSON_ORDER,
            'thread_budget': min(THREAD_BUDGET, os.cpu_count() or 1),
            'setup_processes': NPROCS,
        },
        'runtime_versions': _runtime_versions(),
        'levels': levels,
        'convergence': convergence,
        'resources': {
            'compile_s': compile_s,
            'solve_s': solve_s,
            'postprocess_s': postprocess_s,
            'peak_ram_mb': peak_ram_mb,
            'disk_mb': generated_disk_mb,
            'output_mb': output_mb,
        },
        'signal_archive': signal_path.name,
        'signal_archive_sha256': signal_sha,
    }
    return raw, run, details


def _blocked_run(benchmark, candidates, evidence_ref: str, reason: str) -> BakeoffRun:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    run = BakeoffRun(
        run_id=f'pffdtd-rigid-modes-blocked-{os.environ.get("GITHUB_RUN_ID", "manual")}',
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
        notes=('Rigid-mode probe did not produce numerical fixture evidence.', reason),
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
    outcome: str,
    error: str | None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema_version': PROBE_SCHEMA,
        'probe_id': PROBE_ID,
        'probe_outcome': outcome,
        'r100a_manifest_id': benchmark.manifest_id,
        'r100a_semantic_hash': benchmark.semantic_hash(),
        'candidate_manifest_hash': candidates.semantic_hash(),
        'raw_observation': raw.model_dump(mode='json') if raw is not None else None,
        'bakeoff_run': run.model_dump(mode='json'),
        'details': details,
        'error': error,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run R100B PFFDTD rigid rectangular modal probe')
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

    _write_artifact(
        args.output,
        benchmark,
        candidates,
        raw=raw,
        run=run,
        details=details,
        outcome=outcome,
        error=error,
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
