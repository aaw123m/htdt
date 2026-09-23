from __future__ import annotations

import argparse
from hashlib import sha256
from importlib.metadata import version as distribution_version
import json
import os
from pathlib import Path
import platform
import subprocess
import threading
import time

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
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest
from htdt.acoustic_pyroom_stochastic import (
    PyroomStochasticObservation,
    PyroomStochasticRawEvidence,
    evaluate_pyroom_stochastic_fixture,
    load_pyroom_stochastic_authority,
    stochastic_fixture_semantic_hash,
    stochastic_sample_keys,
)


CANDIDATE_ID = 'pyroomacoustics-v0.10.1-f02b01d'
FIXTURE_ID = 'geometric-seed-repeatability-v1'
ADAPTER_ID = 'htdt-r100b-pyroomacoustics-stochastic-ray'
ADAPTER_VERSION = '1'


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


def _htdt_source_provenance(authority_path: Path) -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    provenance = _htdt_git_provenance()
    provenance.update(
        {
            'probe_sha256': _sha256_file(Path(__file__).resolve()),
            'evaluator_sha256': _sha256_file(
                root / 'backend' / 'src' / 'htdt' / 'acoustic_pyroom_stochastic.py'
            ),
            'stochastic_authority_file_sha256': _sha256_file(authority_path),
        }
    )
    return provenance


class PeakRssMonitor:
    def __init__(self) -> None:
        self._process = psutil.Process()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self.peak_bytes = self._process.memory_info().rss

    def _sample(self) -> None:
        while not self._stop.wait(0.005):
            self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> float:
        self.peak_bytes = max(self.peak_bytes, self._process.memory_info().rss)
        self._stop.set()
        self._thread.join(timeout=1.0)
        return self.peak_bytes / (1024.0 * 1024.0)


def _position_tuple(position) -> tuple[float, float, float]:
    return (float(position.x_m), float(position.y_m), float(position.z_m))


def _candidate(candidates):
    return next(item for item in candidates.candidates if item.candidate_id == CANDIDATE_ID)


def _fixture(benchmark):
    return next(item for item in benchmark.fixtures if item.fixture_id == FIXTURE_ID)


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
            'GitHub-hosted Windows CPU stochastic reference probe; '
            'pyroomacoustics ray tracing uses no GPU in this gate.'
        ),
    )


def _hard_gates(evidence_ref: str, *, reproducible: bool) -> tuple[BakeoffHardGateEvidence, ...]:
    return (
        BakeoffHardGateEvidence(
            category='physics_correctness',
            status='not_run',
            summary=(
                'Candidate-wide physics correctness remains open. This run evaluates only '
                'the frozen R100A-2 stochastic seed/repeatability fixture plus its '
                'pre-registered R100B ray-budget convergence controls.'
            ),
        ),
        BakeoffHardGateEvidence(
            category='reproducible_authority',
            status='pass' if reproducible else 'not_run',
            evidence_ref=evidence_ref if reproducible else None,
            summary=(
                'Exact R100A-2 hash, fixture hash, stochastic authority hash, candidate '
                'source commit, seeds, budgets, dependency versions and raw histograms '
                'are recorded.'
                if reproducible
                else 'Numerical stochastic evidence was not executed.'
            ),
        ),
    )


def _blocked_run(benchmark, candidates, evidence_ref: str, reason: str) -> BakeoffRun:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    run = BakeoffRun(
        run_id=f'pyroom-stochastic-blocked-{os.environ.get("GITHUB_RUN_ID", "manual")}',
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
        notes=(
            'The stochastic gate is BLOCKED, not a zero response and not a PASS.',
            reason,
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)
    return run


def _fixture_mapping(fixture, authority):
    if fixture.fixture_id != FIXTURE_ID:
        raise RuntimeError(f'unexpected fixture id: {fixture.fixture_id}')
    if fixture.portals or fixture.terminations or fixture.obstacles:
        raise RuntimeError('pyroom stochastic ShoeBox adapter requires the frozen closed box fixture')
    if len(fixture.regions) != 1 or len(fixture.sources) != 1 or len(fixture.receivers) != 1:
        raise RuntimeError('stochastic fixture must contain one region, source and receiver')
    if fixture.random_seed is None:
        raise RuntimeError('stochastic fixture must provide the frozen R100A seed')

    vertices = np.asarray(
        [_position_tuple(vertex.position) for vertex in fixture.regions[0].vertices],
        dtype=np.float64,
    )
    lower = vertices.min(axis=0)
    upper = vertices.max(axis=0)
    unique_axes = [np.unique(vertices[:, axis]).size for axis in range(3)]
    if unique_axes != [2, 2, 2]:
        raise RuntimeError('pyroom stochastic adapter only accepts the exact axis-aligned box')
    dimensions = upper - lower

    source_authority = fixture.sources[0]
    if (
        source_authority.normalization != 'unit_energy_j'
        or float(source_authority.amplitude) != 1.0
        or float(source_authority.phase_deg) != 0.0
        or source_authority.directivity != 'omnidirectional'
    ):
        raise RuntimeError('stochastic source authority is not the frozen unit-energy omni source')
    source = np.asarray(_position_tuple(source_authority.position), dtype=np.float64) - lower

    receiver_authority = fixture.receivers[0]
    if (
        receiver_authority.calibration_state != 'ideal_flat'
        or receiver_authority.timing_reference != 'source_t0'
    ):
        raise RuntimeError('stochastic receiver authority changed unexpectedly')
    receiver = np.asarray(_position_tuple(receiver_authority.position), dtype=np.float64) - lower

    if len(fixture.materials) != 1:
        raise RuntimeError('stochastic fixture must expose exactly one material')
    material = fixture.materials[0]
    if material.geometric_model != 'banded' or not material.geometric_bands:
        raise RuntimeError('stochastic fixture requires explicit banded geometric material')
    centers = tuple(float(band.center_hz) for band in material.geometric_bands)
    absorption = tuple(float(band.absorption) for band in material.geometric_bands)
    scattering = tuple(float(band.scattering) for band in material.geometric_bands)
    if centers != (125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0):
        raise RuntimeError(f'unexpected frozen material frequency bands: {centers}')
    if any(value != 0.15 for value in absorption):
        raise RuntimeError(f'unexpected frozen absorption coefficients: {absorption}')
    if any(value != 0.2 for value in scattering):
        raise RuntimeError(f'unexpected frozen scattering coefficients: {scattering}')

    if tuple(float(value) for value in fixture.comparison.frequency_grid.values_hz) != tuple(
        authority.frequency_hz
    ):
        raise RuntimeError('stochastic R100B frequency controls no longer match R100A-2')

    return dimensions, lower, source, receiver, centers, absorption, scattering


def _histogram_digest(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array, dtype=np.float64)
    digest = sha256()
    digest.update(str(tuple(contiguous.shape)).encode('ascii'))
    digest.update(b'|float64|')
    digest.update(contiguous.tobytes(order='C'))
    return digest.hexdigest()


def _extract_decay(
    selected_histogram: np.ndarray,
    authority,
    histogram_bin_size_s: float,
) -> tuple[str, tuple[float, ...], str]:
    if np.any(~np.isfinite(selected_histogram)):
        raise RuntimeError('ray histogram contains non-finite energy')
    if np.any(selected_histogram < 0.0):
        raise RuntimeError('ray histogram contains negative energy')

    cumulative = np.cumsum(selected_histogram[:, ::-1], axis=1)[:, ::-1]
    values: list[float] = []
    for band_index, frequency in enumerate(authority.frequency_hz):
        reference_energy = float(cumulative[band_index, 0])
        if reference_energy <= 0.0:
            return (
                'insufficient_support',
                (),
                f'frequency {frequency:g} Hz has no positive cumulative ray energy',
            )
        for sample_time in authority.sample_times_s:
            bin_index = int(round(sample_time / histogram_bin_size_s))
            if bin_index >= cumulative.shape[1]:
                return (
                    'insufficient_support',
                    (),
                    (
                        f'required sample t={sample_time:.3f}s is beyond histogram support '
                        f'({cumulative.shape[1]} bins)'
                    ),
                )
            energy = float(cumulative[band_index, bin_index])
            if energy <= 0.0:
                return (
                    'insufficient_support',
                    (),
                    (
                        f'frequency {frequency:g} Hz at t={sample_time:.3f}s has no positive '
                        'cumulative ray energy'
                    ),
                )
            values.append(float(10.0 * np.log10(energy / reference_energy)))
    return 'ok', tuple(values), 'all required decay samples have positive ray-energy support'


def _run_observation(
    pra,
    fixture,
    authority,
    mapping,
    *,
    seed: int,
    ray_budget: int,
    replicate: int,
    observation_id: str,
    archive_member: str,
) -> tuple[PyroomStochasticObservation, np.ndarray]:
    dimensions, _lower, source, receiver, centers, absorption, scattering = mapping
    level_index = authority.ray_budgets.index(ray_budget)
    receiver_radius_m = authority.receiver_radius_sequence_m[level_index]
    histogram_bin_size_s = authority.histogram_bin_size_sequence_s[level_index]

    monitor = PeakRssMonitor()
    monitor.start()
    try:
        pra.random.seed(numpy=seed, libroom=seed)

        setup_started = time.perf_counter()
        material = pra.Material(
            energy_absorption={
                'description': 'R100A-2 geo-scattering absorption',
                'coeffs': list(absorption),
                'center_freqs': list(centers),
            },
            scattering={
                'description': 'R100A-2 geo-scattering scattering',
                'coeffs': list(scattering),
                'center_freqs': list(centers),
            },
        )
        room = pra.ShoeBox(
            dimensions.tolist(),
            fs=authority.sampling_rate_hz,
            max_order=-1,
            materials=material,
            temperature=float(fixture.environment.temperature_c),
            humidity=float(fixture.environment.relative_humidity_percent),
            air_absorption=False,
            ray_tracing=True,
            use_rand_ism=False,
        )
        room.set_sound_speed(float(fixture.environment.sound_speed_m_s))
        room.add_source(source.tolist())
        room.add_microphone(receiver.tolist())
        room.set_ray_tracing(
            n_rays=ray_budget,
            receiver_radius=receiver_radius_m,
            energy_thres=authority.energy_threshold,
            time_thres=authority.time_threshold_s,
            hist_bin_size=histogram_bin_size_s,
        )
        actual_bin_size = float(room.rt_args['hist_bin_size'])
        if abs(actual_bin_size - histogram_bin_size_s) > 1e-12:
            raise RuntimeError(
                'pyroom histogram bin quantization changed the frozen estimator: '
                f'{actual_bin_size} != {histogram_bin_size_s}'
            )
        if float(room.c) != float(fixture.environment.sound_speed_m_s):
            raise RuntimeError(
                f'pyroom sound speed {room.c} does not match R100A-2 '
                f'{fixture.environment.sound_speed_m_s}'
            )
        setup_s = time.perf_counter() - setup_started

        solve_started = time.perf_counter()
        room.ray_tracing()
        solve_s = time.perf_counter() - solve_started

        post_started = time.perf_counter()
        histograms = room.rt_histograms[0][0]
        if len(histograms) != 1:
            raise RuntimeError(
                f'omnidirectional receiver produced {len(histograms)} directional histograms; '
                'the frozen estimator expects exactly one'
            )
        histogram = np.asarray(histograms[0], dtype=np.float64)
        if histogram.ndim != 2:
            raise RuntimeError(f'unexpected ray histogram shape: {histogram.shape}')

        pyroom_centers = np.asarray(room.octave_bands.centers, dtype=np.float64)
        selected_indices: list[int] = []
        for frequency in authority.frequency_hz:
            distances = np.abs(pyroom_centers - frequency)
            index = int(np.argmin(distances))
            if float(distances[index]) > 1e-9:
                raise RuntimeError(
                    f'pyroom octave bank does not expose required {frequency:g} Hz band; '
                    f'centers={pyroom_centers.tolist()}'
                )
            selected_indices.append(index)
        selected = np.ascontiguousarray(histogram[selected_indices, :], dtype=np.float64)

        status, values_db, diagnostic = _extract_decay(
            selected, authority, histogram_bin_size_s
        )
        digest = _histogram_digest(selected)
        postprocess_s = time.perf_counter() - post_started
    finally:
        peak_ram_mb = monitor.stop()

    observation = PyroomStochasticObservation(
        observation_id=observation_id,
        seed=seed,
        ray_budget=ray_budget,
        receiver_radius_m=receiver_radius_m,
        histogram_bin_size_s=histogram_bin_size_s,
        replicate=replicate,
        status=status,
        sample_keys=stochastic_sample_keys(authority),
        values_db=values_db,
        selected_band_centers_hz=authority.frequency_hz,
        histogram_shape=tuple(int(value) for value in selected.shape),
        histogram_sha256=digest,
        archive_member=archive_member,
        histogram_total_energy=float(selected.sum()),
        nonzero_bin_count=int(np.count_nonzero(selected)),
        setup_s=setup_s,
        solve_s=solve_s,
        postprocess_s=postprocess_s,
        peak_ram_mb=peak_ram_mb,
        raw_output_bytes=int(selected.nbytes),
        diagnostic=diagnostic,
    )
    return observation, selected


def _dependency_versions() -> tuple[str, ...]:
    names = ('pyroomacoustics', 'numpy', 'scipy', 'psutil')
    return tuple(f'{name}=={distribution_version(name)}' for name in names)


def _write_blocked_artifact(
    output: Path,
    benchmark,
    candidates,
    authority,
    run: BakeoffRun,
    *,
    reason: str,
    environment_setup_s: float,
    authority_path: Path,
) -> None:
    fixture = _fixture(benchmark)
    candidate = _candidate(candidates)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema_version': 'r100b-pyroom-stochastic-artifact-1',
        'probe': 'pyroomacoustics-stochastic-seed-convergence',
        'probe_outcome': 'blocked',
        'workflow_semantics': (
            'A successful workflow can still contain candidate FAIL or BLOCKED evidence.'
        ),
        'block_reason': reason,
        'environment_setup_s': environment_setup_s,
        'r100a_manifest_id': benchmark.manifest_id,
        'r100a_semantic_hash': benchmark.semantic_hash(),
        'fixture_semantic_hash': stochastic_fixture_semantic_hash(fixture),
        'stochastic_authority_semantic_hash': authority.semantic_hash(),
        'candidate_manifest_hash': candidates.semantic_hash(),
        'fixture_authority': fixture.model_dump(mode='json'),
        'stochastic_authority': authority.model_dump(mode='json'),
        'candidate_provenance': candidate.model_dump(mode='json'),
        'htdt_source_provenance': _htdt_source_provenance(authority_path),
        'raw_evidence': None,
        'evaluation': None,
        'bakeoff_run': run.model_dump(mode='json'),
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def _execute(
    benchmark,
    candidates,
    authority,
    *,
    evidence_ref: str,
    raw_output: Path,
    wheel_filename: str | None,
    wheel_sha256: str | None,
):
    import pyroomacoustics as pra

    installed_version = distribution_version('pyroomacoustics')
    if installed_version != authority.backend_version:
        raise RuntimeError(
            f'expected pyroomacoustics {authority.backend_version}, installed {installed_version}'
        )

    fixture = _fixture(benchmark)
    candidate = _candidate(candidates)
    if candidate.candidate_id != authority.candidate_id:
        raise RuntimeError('candidate manifest no longer matches stochastic authority')
    mapping = _fixture_mapping(fixture, authority)

    observations: list[PyroomStochasticObservation] = []
    archive_arrays: dict[str, np.ndarray] = {}

    def capture(
        *,
        seed: int,
        budget: int,
        replicate: int,
        observation_id: str,
    ) -> None:
        member = f'observation_{len(observations):03d}'
        observation, histogram = _run_observation(
            pra,
            fixture,
            authority,
            mapping,
            seed=seed,
            ray_budget=budget,
            replicate=replicate,
            observation_id=observation_id,
            archive_member=member,
        )
        observations.append(observation)
        archive_arrays[member] = histogram

    finest_budget = authority.ray_budgets[-1]
    for replicate in range(authority.same_seed_repeats):
        capture(
            seed=int(fixture.random_seed),
            budget=finest_budget,
            replicate=replicate,
            observation_id=(
                f'replay-seed-{fixture.random_seed}-rays-{finest_budget}-rep-{replicate}'
            ),
        )

    for budget in authority.ray_budgets:
        for seed in authority.independent_seeds:
            capture(
                seed=seed,
                budget=budget,
                replicate=0,
                observation_id=f'independent-seed-{seed}-rays-{budget}',
            )

    raw_output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(raw_output, **archive_arrays)
    raw_archive_bytes = raw_output.stat().st_size
    raw_archive_sha256 = sha256(raw_output.read_bytes()).hexdigest()

    raw = PyroomStochasticRawEvidence(
        evidence_ref=evidence_ref,
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        fixture_id=fixture.fixture_id,
        fixture_semantic_hash=stochastic_fixture_semantic_hash(fixture),
        stochastic_authority_id=authority.authority_id,
        stochastic_authority_semantic_hash=authority.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        backend_version=installed_version,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        wheel_filename=wheel_filename,
        wheel_sha256=wheel_sha256,
        raw_archive_sha256=raw_archive_sha256,
        raw_archive_bytes=raw_archive_bytes,
        dependencies=_dependency_versions(),
        observations=tuple(observations),
    )
    evidence, evaluation = evaluate_pyroom_stochastic_fixture(
        benchmark, candidates, fixture, authority, raw
    )

    run = BakeoffRun(
        run_id=f'pyroom-stochastic-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(fixture.resource_budget.cpu_thread_budget),
        fixture_evidence=(evidence,),
        hard_gates=_hard_gates(evidence_ref, reproducible=True),
        notes=(
            'Pinned pyroomacoustics v0.10.1 stochastic R100B reference gate.',
            'Same-seed deterministic replay is recorded separately from statistical convergence.',
            'Independent-seed variation is preserved for every ray budget.',
            'This fixture result does not select a production solver or finalize the R100B ADR.',
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)
    return raw, evaluation, run


def _write_artifact(
    output: Path,
    benchmark,
    candidates,
    authority,
    *,
    raw: PyroomStochasticRawEvidence,
    evaluation,
    run: BakeoffRun,
    environment_setup_s: float,
    authority_path: Path,
) -> None:
    fixture = _fixture(benchmark)
    candidate = _candidate(candidates)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema_version': 'r100b-pyroom-stochastic-artifact-1',
        'probe': 'pyroomacoustics-stochastic-seed-convergence',
        'probe_outcome': run.fixture_evidence[0].status,
        'workflow_semantics': (
            'Workflow PASS means the evidence harness completed; candidate fixture PASS/FAIL '
            'is the separate probe_outcome field.'
        ),
        'environment_setup_s': environment_setup_s,
        'r100a_manifest_id': benchmark.manifest_id,
        'r100a_semantic_hash': benchmark.semantic_hash(),
        'fixture_semantic_hash': stochastic_fixture_semantic_hash(fixture),
        'stochastic_authority_semantic_hash': authority.semantic_hash(),
        'candidate_manifest_hash': candidates.semantic_hash(),
        'fixture_authority': fixture.model_dump(mode='json'),
        'stochastic_authority': authority.model_dump(mode='json'),
        'candidate_provenance': candidate.model_dump(mode='json'),
        'dependency_provenance': {
            'candidate_source_ref': candidate.source_ref,
            'candidate_source_commit_sha': candidate.source_commit_sha,
            'wheel_filename': raw.wheel_filename,
            'wheel_sha256': raw.wheel_sha256,
            'dependencies': list(raw.dependencies),
            **_htdt_source_provenance(authority_path),
        },
        'raw_evidence': raw.model_dump(mode='json'),
        'evaluation': evaluation.model_dump(mode='json'),
        'bakeoff_run': run.model_dump(mode='json'),
        'bakeoff_run_semantic_hash': run.semantic_hash(),
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run the R100B pyroomacoustics stochastic seed/convergence gate'
    )
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--candidates', required=True, type=Path)
    parser.add_argument('--authority', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--raw-output', required=True, type=Path)
    parser.add_argument('--wheel-filename')
    parser.add_argument('--wheel-sha256')
    parser.add_argument('--environment-setup-s', type=float, default=0.0)
    parser.add_argument('--blocked-reason')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    benchmark = load_acoustic_benchmark_manifest(args.manifest)
    candidates = load_bakeoff_candidate_manifest(args.candidates)
    authority = load_pyroom_stochastic_authority(args.authority)
    evidence_ref = f'artifact:{args.output.as_posix()}'

    if authority.fixture_id != FIXTURE_ID or authority.candidate_id != CANDIDATE_ID:
        raise RuntimeError('stochastic authority does not target the pinned Issue #153 fixture/candidate')

    if args.blocked_reason:
        run = _blocked_run(benchmark, candidates, evidence_ref, args.blocked_reason)
        _write_blocked_artifact(
            args.output,
            benchmark,
            candidates,
            authority,
            run,
            reason=args.blocked_reason,
            environment_setup_s=args.environment_setup_s,
            authority_path=args.authority,
        )
        print(
            json.dumps(
                {
                    'probe_outcome': 'blocked',
                    'reason': args.blocked_reason,
                    'r100a_semantic_hash': benchmark.semantic_hash(),
                    'stochastic_authority_semantic_hash': authority.semantic_hash(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    raw, evaluation, run = _execute(
        benchmark,
        candidates,
        authority,
        evidence_ref=evidence_ref,
        raw_output=args.raw_output,
        wheel_filename=args.wheel_filename,
        wheel_sha256=args.wheel_sha256,
    )
    _write_artifact(
        args.output,
        benchmark,
        candidates,
        authority,
        raw=raw,
        evaluation=evaluation,
        run=run,
        environment_setup_s=args.environment_setup_s,
        authority_path=args.authority,
    )
    print(
        json.dumps(
            {
                'probe_outcome': run.fixture_evidence[0].status,
                'repeatability_status': evaluation.repeatability_status,
                'convergence_status': evaluation.convergence_status,
                'monotonic_budget_refinement': evaluation.monotonic_budget_refinement,
                'adjacent_budget_mean_rms_delta_db': (
                    evaluation.adjacent_budget_mean_rms_delta_db
                ),
                'finest_budget_seed_stddev_max_db': (
                    evaluation.finest_budget_seed_stddev_max_db
                ),
                'run_id': run.run_id,
                'run_semantic_hash': run.semantic_hash(),
                'r100a_semantic_hash': benchmark.semantic_hash(),
                'stochastic_authority_semantic_hash': authority.semantic_hash(),
                'raw_archive_sha256': raw.raw_archive_sha256,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
