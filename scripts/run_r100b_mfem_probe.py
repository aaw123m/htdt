from __future__ import annotations

import argparse
import json
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
FIXTURE_ID = 'wave-rigid-rectangular-modes-v1'
ADAPTER_ID = 'htdt-r100b-mfem-neumann-modes'
ADAPTER_VERSION = '1'


class ProcessPeakRssMonitor:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._stop = threading.Event()
        self.peak_bytes = 0
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        try:
            root = psutil.Process(self.process.pid)
        except psutil.Error:
            return
        while not self._stop.wait(0.005):
            total = 0
            processes = [root]
            try:
                processes.extend(root.children(recursive=True))
            except psutil.Error:
                pass
            for item in processes:
                try:
                    total += item.memory_info().rss
                except psutil.Error:
                    pass
            self.peak_bytes = max(self.peak_bytes, total)

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


def _git_head(root: Path) -> str:
    return subprocess.check_output(
        ['git', '-C', str(root), 'rev-parse', 'HEAD'],
        text=True,
    ).strip().lower()


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
        device_notes='GitHub-hosted Windows MFEM v4.10 serial H1 FEM rigid-mode reference probe.',
    )


def _directory_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return sum(
        item.stat().st_size
        for item in path.rglob('*')
        if item.is_file()
    ) / (1024.0 * 1024.0)


def _box_dimensions(fixture) -> tuple[float, float, float]:
    if len(fixture.regions) != 1 or fixture.portals or fixture.terminations or fixture.obstacles:
        raise ValueError('MFEM rigid-mode probe expects one closed obstacle-free region')
    vertices = fixture.regions[0].vertices
    xs = [item.position.x_m for item in vertices]
    ys = [item.position.y_m for item in vertices]
    zs = [item.position.z_m for item in vertices]
    dims = (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    if any(value <= 0.0 for value in dims):
        raise ValueError(f'invalid room dimensions: {dims}')
    for axis in (xs, ys, zs):
        if len(set(axis)) != 2:
            raise ValueError('MFEM first reference slice requires an axis-aligned rectangular box')
    return dims


def _hard_gates(evidence_ref: str, reproducible: bool) -> tuple[BakeoffHardGateEvidence, ...]:
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
                        'Pinned MFEM v4.10 source, exact R100A/candidate hashes, '
                        'FEM orders, platform, raw eigenfrequencies and resource evidence '
                        'are recorded in this artifact.'
                    ),
                )
            )
        else:
            result.append(
                BakeoffHardGateEvidence(
                    category=category,
                    status='not_run',
                    summary=(
                        'Candidate-wide hard gate remains open. This slice scores one '
                        'R100A rigid analytical-mode fixture only.'
                    ),
                )
            )
    return tuple(result)


def _blocked_run(benchmark, candidates, evidence_ref: str, reason: str) -> BakeoffRun:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    run = BakeoffRun(
        run_id=f'mfem-rigid-modes-blocked-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(fixture.resource_budget.cpu_thread_budget),
        fixture_evidence=(
            BakeoffFixtureEvidence(
                fixture_id=fixture.fixture_id,
                status='blocked',
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                backend_version='unavailable',
                precision='float64',
                diagnostics=(reason,),
            ),
        ),
        hard_gates=_hard_gates(evidence_ref, reproducible=False),
        notes=(reason,),
    )
    validate_bakeoff_run(benchmark, candidates, run)
    return run


def _match_modes(fixture, frequencies: list[float]) -> tuple[RawObservableObservation, ...]:
    expected = []
    for observable in fixture.observables:
        if len(observable.samples) != 1 or observable.samples[0].scalar_value is None:
            raise ValueError(f'{observable.observable_id} is not a scalar one-sample mode authority')
        expected.append(
            (
                float(observable.samples[0].scalar_value),
                observable,
                observable.samples[0],
            )
        )
    expected.sort(key=lambda item: item[0])

    unused = list(enumerate(frequencies))
    observations: list[RawObservableObservation] = []
    for expected_hz, observable, sample in expected:
        if not unused:
            raise ValueError('MFEM returned too few eigenfrequencies for R100A mode matching')
        best_offset = min(
            range(len(unused)),
            key=lambda offset: abs(unused[offset][1] - expected_hz),
        )
        spectrum_index, actual_hz = unused.pop(best_offset)
        observations.append(
            RawObservableObservation(
                observable_id=observable.observable_id,
                kind=observable.kind,
                unit=observable.unit,
                samples=(
                    RawObservationSample(
                        sample_key=sample.sample_key,
                        scalar_value=float(actual_hz),
                    ),
                ),
                diagnostics=(
                    f'matched_spectrum_index={spectrum_index}',
                    f'expected_hz={expected_hz:.12g}',
                ),
            )
        )
    return tuple(observations)


def _execute(
    benchmark,
    candidates,
    mfem_root: Path,
    executable: Path,
    work_dir: Path,
    evidence_ref: str,
    build_s: float | None,
) -> tuple[dict[str, object], BakeoffRun]:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    actual_head = _git_head(mfem_root)
    if actual_head != candidate.source_commit_sha:
        raise RuntimeError(
            f'MFEM checkout mismatch: expected {candidate.source_commit_sha}, got {actual_head}'
        )
    if not executable.is_file():
        raise RuntimeError(f'MFEM probe executable is missing: {executable}')

    lx, ly, lz = _box_dimensions(fixture)
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / 'mfem_rigid_modes_raw.json'

    command = [
        str(executable),
        '--lx', str(lx),
        '--ly', str(ly),
        '--lz', str(lz),
        '--sound-speed', str(fixture.environment.sound_speed_m_s),
        '--order-min', '2',
        '--order-max', '5',
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
    stdout, _ = process.communicate()
    peak_ram_mb = monitor.stop()
    if process.returncode != 0:
        raise RuntimeError(
            f'MFEM probe executable exited {process.returncode}: {stdout[-4000:]}'
        )
    if not raw_path.is_file():
        raise RuntimeError('MFEM probe did not produce its raw JSON output')

    raw_payload = json.loads(raw_path.read_text(encoding='utf-8'))
    orders = raw_payload.get('orders')
    if not isinstance(orders, list) or not orders:
        raise RuntimeError('MFEM raw output has no polynomial-order evidence')
    finest = max(orders, key=lambda item: int(item['order']))
    frequencies = [float(value) for value in finest['frequencies_hz']]

    post_started = time.perf_counter()
    raw_observation = RawFixtureObservation(
        fixture_id=fixture.fixture_id,
        evidence_ref=evidence_ref,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        backend_version=str(raw_payload.get('mfem_version', '4.10')),
        precision='float64',
        compile_s=float(finest['assemble_s']),
        solve_s=float(finest['eigensolve_s']),
        postprocess_s=0.0,
        peak_ram_mb=peak_ram_mb,
        disk_mb=_directory_size_mb(work_dir),
        output_mb=raw_path.stat().st_size / (1024.0 * 1024.0),
        observations=_match_modes(fixture, frequencies),
        diagnostics=(
            f'mfem_source_commit={actual_head}',
            f'polynomial_orders={",".join(str(item["order"]) for item in orders)}',
            f'cmake_build_s={build_s if build_s is not None else "not-recorded"}',
            'Natural FEM boundary condition is Neumann/rigid; no essential boundary DOFs are eliminated.',
            'Highest polynomial order is used for R100A acceptance; lower orders remain convergence evidence.',
        ),
    )
    provisional = evaluate_sampled_fixture(fixture, raw_observation)
    postprocess_s = time.perf_counter() - post_started
    raw_observation = raw_observation.model_copy(update={'postprocess_s': postprocess_s})
    evidence = evaluate_sampled_fixture(fixture, raw_observation)

    run = BakeoffRun(
        run_id=f'mfem-rigid-modes-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(fixture.resource_budget.cpu_thread_budget),
        fixture_evidence=(evidence,),
        hard_gates=_hard_gates(evidence_ref, reproducible=True),
        notes=(
            'MFEM v4.10 serial H1 FEM Neumann Laplacian reference.',
            'One high-order hexahedral element is evaluated at orders 2..5.',
            f'provisional_fixture_status={provisional.status}',
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)

    details = {
        'mfem_source_commit_sha': actual_head,
        'cmake_build_s': build_s,
        'raw_solver_output': raw_payload,
        'selected_order': int(finest['order']),
        'selected_frequencies_hz': frequencies,
        'peak_ram_mb': peak_ram_mb,
        'stdout_tail': stdout[-4000:],
        'raw_observation': raw_observation.model_dump(mode='json'),
    }
    return details, run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run R100B MFEM rigid analytical-mode reference probe')
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--candidates', required=True, type=Path)
    parser.add_argument('--mfem-root', required=True, type=Path)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--build-s', type=float)
    parser.add_argument('--blocked-reason')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    benchmark = load_acoustic_benchmark_manifest(args.manifest)
    candidates = load_bakeoff_candidate_manifest(args.candidates)
    evidence_ref = f'artifact:{args.output.as_posix()}'

    if args.blocked_reason:
        run = _blocked_run(benchmark, candidates, evidence_ref, args.blocked_reason)
        payload = {
            'schema_version': 'r100b-mfem-probe-artifact-1',
            'probe_outcome': 'blocked',
            'r100a_manifest_id': benchmark.manifest_id,
            'r100a_semantic_hash': benchmark.semantic_hash(),
            'candidate_manifest_hash': candidates.semantic_hash(),
            'details': None,
            'bakeoff_run': run.model_dump(mode='json'),
        }
    else:
        if args.executable is None:
            raise SystemExit('--executable is required unless --blocked-reason is used')
        try:
            details, run = _execute(
                benchmark,
                candidates,
                args.mfem_root,
                args.executable,
                args.work_dir,
                evidence_ref,
                args.build_s,
            )
            outcome = run.fixture_evidence[0].status
            payload = {
                'schema_version': 'r100b-mfem-probe-artifact-1',
                'probe_outcome': outcome,
                'r100a_manifest_id': benchmark.manifest_id,
                'r100a_semantic_hash': benchmark.semantic_hash(),
                'candidate_manifest_hash': candidates.semantic_hash(),
                'details': details,
                'bakeoff_run': run.model_dump(mode='json'),
            }
        except Exception as exc:
            run = _blocked_run(
                benchmark,
                candidates,
                evidence_ref,
                f'{type(exc).__name__}: {exc}',
            )
            payload = {
                'schema_version': 'r100b-mfem-probe-artifact-1',
                'probe_outcome': 'blocked',
                'r100a_manifest_id': benchmark.manifest_id,
                'r100a_semantic_hash': benchmark.semantic_hash(),
                'candidate_manifest_hash': candidates.semantic_hash(),
                'details': {'error': f'{type(exc).__name__}: {exc}'},
                'bakeoff_run': run.model_dump(mode='json'),
            }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    print(
        json.dumps(
            {
                'probe_outcome': payload['probe_outcome'],
                'output': str(args.output),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
