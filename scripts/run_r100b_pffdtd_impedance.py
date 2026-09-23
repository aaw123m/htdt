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
from htdt.acoustic_pffdtd_adapter import pffdtd_git_head
from htdt.acoustic_pffdtd_impedance_adapter import (
    compile_impedance_fixture_boundary,
    compile_impedance_fixture_model,
)


CANDIDATE_ID = 'pffdtd-main-aa319f6'
FIXTURE_ID = 'wave-normal-incidence-impedance-v1'
PROBE_SCHEMA = 'r100b-pffdtd-impedance-reflection-artifact-1'
PROBE_ID = 'pffdtd-explicit-impedance-reflection'
ADAPTER_ID = 'htdt-r100b-pffdtd-explicit-impedance'
ADAPTER_VERSION = '1'
THREAD_BUDGET = 1


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
            'GitHub-hosted Windows CPU probe of the pinned PFFDTD boundary model. '
            'This fixture does not claim spatial FDTD propagation validation.'
        ),
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
                        'Pinned PFFDTD commit, exact R100A-2 impedance/source/receiver/environment '
                        'authority, dependency versions, adapter version, compiled DEF and raw '
                        'complex reflection samples are recorded.'
                    ),
                )
            )
        else:
            items.append(
                BakeoffHardGateEvidence(
                    category=category,
                    status='not_run',
                    summary=(
                        'Candidate-wide hard gate remains open. Fixture status records only the '
                        'explicit impedance/reflection slice and is not a production solver decision.'
                    ),
                )
            )
    return tuple(items)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_size_mb(path: Path) -> float:
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file()) / (1024.0 * 1024.0)


def _runtime_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ('numpy', 'scipy', 'h5py', 'matplotlib', 'psutil', 'pydantic', 'pytest'):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = 'not-installed'
    return result


def _htdt_git_head() -> str:
    return subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'],
        text=True,
    ).strip().lower()


def _validate_fixture_contract(fixture) -> None:
    if fixture.comparison.fourier_sign != 'exp(-i*omega*t)':
        raise ValueError('PFFDTD impedance probe requires exp(-i*omega*t) authority')
    if fixture.comparison.time_zero_reference != 'source_excitation_t0':
        raise ValueError('PFFDTD impedance probe requires source_excitation_t0')
    if fixture.comparison.window != 'none' or fixture.comparison.filter != 'none':
        raise ValueError('PFFDTD impedance probe requires no window and no filter')
    if fixture.comparison.interpolation != 'linear_complex':
        raise ValueError('PFFDTD impedance probe requires linear_complex comparison authority')
    if fixture.comparison.frequency_grid.kind != 'explicit':
        raise ValueError('PFFDTD impedance probe requires explicit frequency samples')
    if len(fixture.sources) != 1 or len(fixture.receivers) != 1:
        raise ValueError('PFFDTD impedance probe requires one source and one receiver')
    source = fixture.sources[0]
    if source.normalization != 'volume_velocity_m3_s':
        raise ValueError('PFFDTD impedance probe requires volume_velocity_m3_s source authority')
    if source.directivity != 'omnidirectional':
        raise ValueError('PFFDTD impedance probe requires omnidirectional source authority')
    if len(fixture.observables) != 1:
        raise ValueError('PFFDTD impedance probe requires one reflection observable')
    observable = fixture.observables[0]
    if observable.kind != 'complex_reflection_coefficient' or observable.unit != '1':
        raise ValueError('PFFDTD impedance probe requires complex_reflection_coefficient / 1')


def _authority_snapshot(fixture) -> dict[str, object]:
    source = fixture.sources[0]
    receiver = fixture.receivers[0]
    observable = fixture.observables[0]
    return {
        'fixture_id': fixture.fixture_id,
        'source': source.model_dump(mode='json'),
        'receiver': receiver.model_dump(mode='json'),
        'environment': fixture.environment.model_dump(mode='json'),
        'comparison': fixture.comparison.model_dump(mode='json'),
        'observable': observable.model_dump(mode='json'),
    }


def _execute(
    upstream_root: Path,
    work_dir: Path,
    output: Path,
    benchmark,
    candidates,
) -> tuple[BakeoffRun, RawFixtureObservation, dict[str, object]]:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    _validate_fixture_contract(fixture)

    actual_head = pffdtd_git_head(upstream_root)
    if actual_head != candidate.source_commit_sha:
        raise RuntimeError(
            f'PFFDTD checkout mismatch: expected {candidate.source_commit_sha}, got {actual_head}'
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    material_dir = work_dir / 'materials'
    material_dir.mkdir(parents=True, exist_ok=True)
    model_path = work_dir / 'r100a_impedance_pffdtd.json'
    material_path = material_dir / 'z-2z0.h5'

    monitor = PeakRssMonitor()
    monitor.start()
    compile_started = time.perf_counter()
    boundary = compile_impedance_fixture_boundary(fixture)
    model = compile_impedance_fixture_model(fixture)
    model_path.write_text(
        json.dumps(model, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )

    upstream_python = upstream_root / 'python'
    if not (upstream_python / 'materials' / 'adm_funcs.py').is_file():
        raise RuntimeError(f'PFFDTD material runtime missing: {upstream_python}')
    sys.path.insert(0, str(upstream_python))
    from materials.adm_funcs import compute_Rf_from_DEF, write_freq_ind_mat_from_Zn

    normalized_impedance = float(boundary['normalized_impedance'])
    write_freq_ind_mat_from_Zn(normalized_impedance, material_path)
    with h5py.File(material_path, 'r') as handle:
        solver_def = np.asarray(handle['DEF'][...], dtype=np.float64)

    expected_def = np.asarray(boundary['def_coefficients'], dtype=np.float64)
    if solver_def.shape != expected_def.shape or not np.array_equal(solver_def, expected_def):
        raise RuntimeError(
            f'PFFDTD material writer changed exact DEF authority: '
            f'expected={expected_def.tolist()} actual={solver_def.tolist()}'
        )
    compile_s = time.perf_counter() - compile_started

    frequencies_hz = np.asarray(
        fixture.comparison.frequency_grid.values_hz,
        dtype=np.float64,
    )
    if tuple(float(item) for item in frequencies_hz) != tuple(boundary['frequencies_hz']):
        raise RuntimeError('compiled impedance frequencies do not match R100A authority')

    solve_started = time.perf_counter()
    d_coeff, e_coeff, f_coeff = solver_def.T
    reflection, normalized_admittance, branch_impedance, branch_reflection = compute_Rf_from_DEF(
        1j * 2.0 * np.pi * frequencies_hz,
        d_coeff,
        e_coeff,
        f_coeff,
    )
    solve_s = time.perf_counter() - solve_started

    reflection = np.asarray(reflection, dtype=np.complex128)
    if reflection.shape != frequencies_hz.shape:
        raise RuntimeError(f'unexpected PFFDTD reflection shape: {reflection.shape}')
    if not np.all(np.isfinite(reflection.real)) or not np.all(np.isfinite(reflection.imag)):
        raise RuntimeError('PFFDTD reflection output contains non-finite values')

    expected = fixture.observables[0]
    if len(expected.samples) != reflection.size:
        raise RuntimeError('R100A reflection sample count does not match PFFDTD output')

    post_started = time.perf_counter()
    samples = tuple(
        RawObservationSample(
            sample_key=expected_sample.sample_key,
            frequency_hz=float(frequency_hz),
            real_value=float(value.real),
            imag_value=float(value.imag),
        )
        for expected_sample, frequency_hz, value in zip(
            expected.samples,
            frequencies_hz,
            reflection,
        )
    )
    evidence_ref = f'artifact:{output.as_posix()}'
    raw = RawFixtureObservation(
        fixture_id=fixture.fixture_id,
        evidence_ref=evidence_ref,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        backend_version=candidate.source_commit_sha,
        precision='float64',
        compile_s=compile_s,
        solve_s=solve_s,
        postprocess_s=0.0,
        peak_ram_mb=0.0,
        disk_mb=_directory_size_mb(work_dir),
        output_mb=float(
            reflection.nbytes
            + np.asarray(normalized_admittance).nbytes
            + np.asarray(branch_impedance).nbytes
            + np.asarray(branch_reflection).nbytes
        ) / (1024.0 * 1024.0),
        observations=(
            RawObservableObservation(
                observable_id=expected.observable_id,
                kind=expected.kind,
                unit=expected.unit,
                samples=samples,
                diagnostics=(
                    'Raw complex R is emitted by pinned PFFDTD materials.adm_funcs.compute_Rf_from_DEF.',
                    'No absorption coefficient is used or converted.',
                ),
            ),
        ),
        diagnostics=(
            f'candidate_source_commit={candidate.source_commit_sha}',
            f'adapter={ADAPTER_ID}@{ADAPTER_VERSION}',
            f'physical_Z={boundary["physical_resistance_pa_s_m"]}+'
            f'{boundary["physical_reactance_pa_s_m"]}j Pa*s/m',
            f'rho_c={boundary["characteristic_impedance_pa_s_m"]} Pa*s/m',
            f'normalized_Z={normalized_impedance}',
            f'normalized_Y={boundary["normalized_admittance"]}',
            f'DEF={solver_def.tolist()}',
            f'fourier_sign={fixture.comparison.fourier_sign}',
            f'time_zero={fixture.comparison.time_zero_reference}',
            f'source_normalization={fixture.sources[0].normalization}',
        ),
    )
    provisional = evaluate_sampled_fixture(fixture, raw)
    postprocess_s = time.perf_counter() - post_started
    peak_ram_mb = monitor.stop()

    raw = raw.model_copy(
        update={
            'postprocess_s': postprocess_s,
            'peak_ram_mb': peak_ram_mb,
            'disk_mb': _directory_size_mb(work_dir),
        }
    )
    evidence = evaluate_sampled_fixture(fixture, raw)

    run = BakeoffRun(
        run_id=f'pffdtd-impedance-reflection-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(),
        fixture_evidence=(evidence,),
        hard_gates=_hard_gates(evidence_ref, reproducible=True),
        notes=(
            'Fixture-scoped explicit impedance/reflection evidence only.',
            'Workflow success is not candidate PASS; inspect fixture status in the artifact.',
            'This boundary-model probe does not claim end-to-end spatial FDTD reflection propagation.',
            f'provisional_status={provisional.status}',
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)

    root = Path(__file__).resolve().parents[1]
    reflection_details = [
        {
            'sample_key': sample.sample_key,
            'frequency_hz': sample.frequency_hz,
            'real': sample.real_value,
            'imag': sample.imag_value,
            'magnitude': float(abs(value)),
            'phase_deg': float(np.angle(value, deg=True)),
        }
        for sample, value in zip(samples, reflection)
    ]
    details = {
        'authority': _authority_snapshot(fixture),
        'compiled_boundary': {
            key: (
                value.tolist()
                if isinstance(value, np.ndarray)
                else value
            )
            for key, value in boundary.items()
        },
        'reflection_samples': reflection_details,
        'resources': {
            'compile_s': raw.compile_s,
            'solve_s': raw.solve_s,
            'postprocess_s': raw.postprocess_s,
            'peak_ram_mb': raw.peak_ram_mb,
            'disk_mb': raw.disk_mb,
            'output_mb': raw.output_mb,
        },
        'provenance': {
            'candidate_source_commit_sha': candidate.source_commit_sha,
            'candidate_source_actual_sha': actual_head,
            'htdt_source_commit_sha': os.environ.get('HTDT_PROBE_SOURCE_SHA', _htdt_git_head()),
            'htdt_checkout_commit_sha': _htdt_git_head(),
            'adapter_id': ADAPTER_ID,
            'adapter_version': ADAPTER_VERSION,
            'adapter_sha256': _file_sha256(root / 'backend' / 'src' / 'htdt' / 'acoustic_pffdtd_impedance_adapter.py'),
            'probe_sha256': _file_sha256(Path(__file__).resolve()),
            'upstream_boundary_function': 'python/materials/adm_funcs.py:compute_Rf_from_DEF',
            'upstream_material_writer': 'python/materials/adm_funcs.py:write_freq_ind_mat_from_Zn',
            'runtime_versions': _runtime_versions(),
            'model_sha256': _file_sha256(model_path),
            'material_h5_sha256': _file_sha256(material_path),
        },
        'capability_boundary': {
            'supported_here': (
                'exact frequency-independent purely resistive specific impedance '
                'mapped to one PFFDTD DEF branch'
            ),
            'blocked_without_new_authority': (
                'reactive or frequency-varying specific-impedance tables requiring DEF fitting'
            ),
            'spatial_fdtd_reflection_validated': False,
        },
    }
    return run, raw, details


def _blocked_run(benchmark, candidates, reason: str) -> BakeoffRun:
    candidate = _candidate(candidates)
    fixture = _fixture(benchmark)
    run = BakeoffRun(
        run_id=f'pffdtd-impedance-reflection-blocked-{os.environ.get("GITHUB_RUN_ID", "manual")}',
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
                backend_version=candidate.source_commit_sha,
                precision='float64',
                diagnostics=(reason,),
            ),
        ),
        hard_gates=_hard_gates('', reproducible=False),
        notes=(
            'Explicit impedance/reflection probe did not produce numerical fixture evidence.',
            reason,
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)
    return run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run R100B PFFDTD explicit impedance/reflection probe')
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
    error: str | None = None
    raw: RawFixtureObservation | None = None
    details: dict[str, object] = {}

    try:
        run, raw, details = _execute(
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
            'authority': _authority_snapshot(_fixture(benchmark)),
        }
        run = _blocked_run(benchmark, candidates, error)
        outcome = 'blocked'

    payload = {
        'schema_version': PROBE_SCHEMA,
        'probe_id': PROBE_ID,
        'probe_outcome': outcome,
        'workflow_semantics': {
            'workflow_success_means_probe_completed': True,
            'workflow_success_does_not_mean_candidate_pass': True,
            'candidate_result_is_probe_outcome': outcome,
        },
        'r100a_manifest_id': benchmark.manifest_id,
        'r100a_semantic_hash': benchmark.semantic_hash(),
        'candidate_manifest_hash': candidates.semantic_hash(),
        'raw_observation': raw.model_dump(mode='json') if raw is not None else None,
        'bakeoff_run': run.model_dump(mode='json'),
        'details': details,
        'error': error,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )

    summary = {
        'probe_outcome': outcome,
        'run_id': run.run_id,
        'fixture_status': run.fixture_evidence[0].status,
        'error': error,
    }
    if raw is not None:
        summary['reflection_samples'] = details.get('reflection_samples', [])
        summary['observable_statuses'] = {
            item.observable_id: item.status
            for item in run.fixture_evidence[0].observables
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
