from __future__ import annotations

import argparse
from importlib.metadata import version as distribution_version
import json
import os
from pathlib import Path
import platform
import threading
import time

import numpy as np
import psutil

from htdt.acoustic_bakeoff import (
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


CANDIDATE_ID = 'pyroomacoustics-v0.10.1-f02b01d'
FIXTURE_ID = 'geometric-direct-first-reflection-v1'
ADAPTER_ID = 'htdt-r100b-pyroomacoustics-image-source'
ADAPTER_VERSION = '1'


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
        device_notes='GitHub-hosted Windows CPU reference probe; GPU is not used by this fixture.',
    )


def _hard_gates(evidence_ref: str, *, reproducible: bool) -> tuple[BakeoffHardGateEvidence, ...]:
    return (
        BakeoffHardGateEvidence(
            category='physics_correctness',
            status='not_run',
            summary=(
                'Candidate-wide physics gate remains open. This run evaluates only the '
                'R100A direct/first-reflection fixture.'
            ),
        ),
        BakeoffHardGateEvidence(
            category='reproducible_authority',
            status='pass' if reproducible else 'not_run',
            evidence_ref=evidence_ref if reproducible else None,
            summary=(
                'Pinned candidate/version, exact R100A semantic hash, adapter version, '
                'platform and raw observations are recorded in this artifact.'
                if reproducible
                else 'Backend package was unavailable, so numerical authority was not executed.'
            ),
        ),
    )


def _blocked_run(benchmark, candidates, evidence_ref: str, reason: str) -> BakeoffRun:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    from htdt.acoustic_bakeoff import BakeoffFixtureEvidence

    run = BakeoffRun(
        run_id=f'pyroom-blocked-{os.environ.get("GITHUB_RUN_ID", "manual")}',
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
            'Reference probe was blocked before numerical execution.',
            reason,
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)
    return run


def _nearest_image(images: np.ndarray, target: np.ndarray, *, label: str) -> np.ndarray:
    distances = np.linalg.norm(images - target[None, :], axis=1)
    index = int(np.argmin(distances))
    if float(distances[index]) > 1e-8:
        raise RuntimeError(
            f'pyroomacoustics did not emit expected {label} image source; '
            f'nearest distance={float(distances[index])}'
        )
    return images[index]


def _execute_probe(
    benchmark,
    candidates,
    evidence_ref: str,
    wheel_sha256: str | None,
) -> tuple[RawFixtureObservation, BakeoffRun]:
    import pyroomacoustics as pra

    installed_version = distribution_version('pyroomacoustics')
    if installed_version != '0.10.1':
        raise RuntimeError(
            f'expected pyroomacoustics 0.10.1, installed {installed_version}'
        )

    fixture = _fixture(benchmark)
    if fixture.portals or fixture.terminations or fixture.obstacles:
        raise RuntimeError('direct/first-reflection probe expects the obstacle-free closed fixture')
    if len(fixture.regions) != 1 or len(fixture.sources) != 1 or len(fixture.receivers) != 1:
        raise RuntimeError('direct/first-reflection probe expects one region/source/receiver')

    vertices = np.asarray(
        [_position_tuple(vertex.position) for vertex in fixture.regions[0].vertices],
        dtype=np.float64,
    )
    lower = vertices.min(axis=0)
    upper = vertices.max(axis=0)
    dimensions = upper - lower
    unique_axes = [np.unique(vertices[:, axis]).size for axis in range(3)]
    if unique_axes != [2, 2, 2]:
        raise RuntimeError('pyroomacoustics ShoeBox adapter only accepts an axis-aligned box fixture')

    source = np.asarray(_position_tuple(fixture.sources[0].position), dtype=np.float64) - lower
    receiver = np.asarray(_position_tuple(fixture.receivers[0].position), dtype=np.float64) - lower

    geometric_materials = [
        material
        for material in fixture.materials
        if material.geometric_model == 'banded' and material.geometric_bands
    ]
    if len(geometric_materials) != 1:
        raise RuntimeError('fixture must expose exactly one banded geometric material')
    absorptions = {float(band.absorption) for band in geometric_materials[0].geometric_bands}
    scatterings = {float(band.scattering) for band in geometric_materials[0].geometric_bands}
    if len(absorptions) != 1 or scatterings != {0.0}:
        raise RuntimeError(
            'this pyroomacoustics reference probe requires frequency-constant absorption '
            'and zero scattering'
        )
    absorption = next(iter(absorptions))

    monitor = PeakRssMonitor()
    monitor.start()
    room = pra.ShoeBox(
        dimensions.tolist(),
        fs=16000,
        materials=pra.Material(absorption),
        max_order=1,
    )
    room.add_source(source.tolist())
    room.add_microphone(receiver.tolist())

    solve_started = time.perf_counter()
    room.image_source_model()
    solve_s = time.perf_counter() - solve_started

    post_started = time.perf_counter()
    images = np.asarray(room.sources[0].images, dtype=np.float64).T
    if images.ndim != 2 or images.shape[1] != 3:
        raise RuntimeError(f'unexpected image-source array shape: {images.shape}')

    direct_image = _nearest_image(images, source, label='direct')
    ymin_image_target = source.copy()
    ymin_image_target[1] = -source[1]
    ymin_image = _nearest_image(images, ymin_image_target, label='y-min first reflection')

    direct_length = float(np.linalg.norm(receiver - direct_image))
    reflected_length = float(np.linalg.norm(receiver - ymin_image))
    denominator = receiver[1] - ymin_image[1]
    if denominator == 0.0:
        raise RuntimeError('cannot intersect y-min image path with the y=0 plane')
    t = -ymin_image[1] / denominator
    reflection_point = ymin_image + t * (receiver - ymin_image)
    if not (0.0 <= t <= 1.0):
        raise RuntimeError('computed y-min reflection point lies outside the image path')

    raw = RawFixtureObservation(
        fixture_id=fixture.fixture_id,
        evidence_ref=evidence_ref,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        backend_version=installed_version,
        precision='float64',
        compile_s=0.0,
        solve_s=solve_s,
        postprocess_s=0.0,
        peak_ram_mb=0.0,
        disk_mb=0.0,
        output_mb=float(images.nbytes) / (1024.0 * 1024.0),
        observations=(
            RawObservableObservation(
                observable_id='direct-length',
                kind='direct_path_length_m',
                unit='m',
                samples=(
                    RawObservationSample(
                        sample_key='src->rx',
                        scalar_value=direct_length,
                    ),
                ),
            ),
            RawObservableObservation(
                observable_id='direct-delay',
                kind='arrival_time_s',
                unit='s',
                samples=(
                    RawObservationSample(
                        sample_key='src->rx',
                        scalar_value=direct_length / fixture.environment.sound_speed_m_s,
                    ),
                ),
            ),
            RawObservableObservation(
                observable_id='first-reflection-point-ymin',
                kind='reflection_point_m',
                unit='m',
                samples=(
                    RawObservationSample(
                        sample_key='ymin',
                        vector_value=tuple(float(value) for value in reflection_point + lower),
                    ),
                ),
            ),
            RawObservableObservation(
                observable_id='first-reflection-length-ymin',
                kind='reflected_path_length_m',
                unit='m',
                samples=(
                    RawObservationSample(
                        sample_key='ymin',
                        scalar_value=reflected_length,
                    ),
                ),
            ),
        ),
        diagnostics=(
            f'image_source_count={images.shape[0]}',
            f'wheel_sha256={wheel_sha256 or "not-recorded"}',
            'R100A geometry/material/source/receiver authority mapped into pyroomacoustics ShoeBox.',
        ),
    )
    provisional_evidence = evaluate_sampled_fixture(fixture, raw)
    postprocess_s = time.perf_counter() - post_started
    peak_ram_mb = monitor.stop()

    raw = raw.model_copy(
        update={
            'postprocess_s': postprocess_s,
            'peak_ram_mb': peak_ram_mb,
        }
    )
    evidence = evaluate_sampled_fixture(fixture, raw)

    candidate = _candidate(candidates)
    run = BakeoffRun(
        run_id=f'pyroom-direct-first-reflection-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(fixture.resource_budget.cpu_thread_budget),
        fixture_evidence=(evidence,),
        hard_gates=_hard_gates(evidence_ref, reproducible=True),
        notes=(
            'Official pyroomacoustics 0.10.1 Windows wheel probe.',
            'Only geometric direct path and y-min first specular reflection are evaluated.',
            f'provisional_status={provisional_evidence.status}',
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)
    return raw, run


def _write_artifact(
    output: Path,
    benchmark,
    candidates,
    *,
    raw: RawFixtureObservation | None,
    run: BakeoffRun,
    probe_outcome: str,
    wheel_sha256: str | None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema_version': 'r100b-probe-artifact-1',
        'probe': 'pyroomacoustics-direct-first-reflection',
        'probe_outcome': probe_outcome,
        'r100a_manifest_id': benchmark.manifest_id,
        'r100a_semantic_hash': benchmark.semantic_hash(),
        'candidate_manifest_hash': candidates.semantic_hash(),
        'wheel_sha256': wheel_sha256,
        'raw_observation': raw.model_dump(mode='json') if raw is not None else None,
        'bakeoff_run': run.model_dump(mode='json'),
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run the R100B pyroomacoustics reference probe')
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--candidates', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--wheel-sha256')
    parser.add_argument('--blocked-reason')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    benchmark = load_acoustic_benchmark_manifest(args.manifest)
    candidates = load_bakeoff_candidate_manifest(args.candidates)
    evidence_ref = f'artifact:{args.output.as_posix()}'

    if args.blocked_reason:
        run = _blocked_run(benchmark, candidates, evidence_ref, args.blocked_reason)
        _write_artifact(
            args.output,
            benchmark,
            candidates,
            raw=None,
            run=run,
            probe_outcome='blocked',
            wheel_sha256=args.wheel_sha256,
        )
        print(json.dumps({'probe_outcome': 'blocked', 'reason': args.blocked_reason}, indent=2))
        return 0

    raw, run = _execute_probe(
        benchmark,
        candidates,
        evidence_ref,
        args.wheel_sha256,
    )
    fixture_status = run.fixture_evidence[0].status
    _write_artifact(
        args.output,
        benchmark,
        candidates,
        raw=raw,
        run=run,
        probe_outcome=fixture_status,
        wheel_sha256=args.wheel_sha256,
    )
    print(
        json.dumps(
            {
                'probe_outcome': fixture_status,
                'run_id': run.run_id,
                'run_semantic_hash': run.semantic_hash(),
                'observable_statuses': {
                    item.observable_id: item.status
                    for item in run.fixture_evidence[0].observables
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
