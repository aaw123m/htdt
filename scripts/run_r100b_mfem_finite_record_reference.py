from __future__ import annotations

import argparse
import cmath
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


CANDIDATE_ID = 'mfem-v4.10-d964264'
FIXTURE_ID = 'wave-concave-l-room-v1'
ADAPTER_ID = 'htdt-r100b-mfem-concave-finite-record'
ADAPTER_VERSION = '1'
ARTIFACT_SCHEMA = 'r100b-mfem-concave-finite-record-reference-artifact-1'
RAW_SCHEMA = 'r100b-mfem-concave-finite-record-raw-1'
REFERENCE_QUALIFICATION_FRACTION = 0.25
SOLVER_RESIDUAL_LIMIT = 1.0e-8

# Frozen before observing any transient result. Refinement simultaneously
# increases H1 order and decreases solver-native dt; geometry remains the exact
# five-cell L-prism at every level.
REFINEMENT_LEVELS = (
    {'level_id': 'p2-dt1over6000', 'order': 2, 'sample_rate_hz': 6000},
    {'level_id': 'p3-dt1over8000', 'order': 3, 'sample_rate_hz': 8000},
    {'level_id': 'p4-dt1over10000', 'order': 4, 'sample_rate_hz': 10000},
    {'level_id': 'p5-dt1over12000', 'order': 5, 'sample_rate_hz': 12000},
)


class ReferenceBlocked(RuntimeError):
    pass


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
    if (
        len(event_head) == 40
        and all(character in '0123456789abcdef' for character in event_head)
    ):
        pr_head = event_head
    else:
        pr_head = _git_rev_parse('HEAD^2') or checkout
    return {
        'checkout_commit_sha': checkout,
        'pr_head_commit_sha': pr_head,
    }


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
            'Pinned MFEM v4.10 serial H1 transient finite-record independent reference. '
            'No PFFDTD result participates in source generation, refinement, or qualification.'
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
                        'Pinned MFEM source commit, exact R100A-4 semantic hash, finite-record '
                        'contract, raw pressure/source records, frozen refinement sequence and '
                        'resource/provenance evidence are recorded.'
                    ),
                )
            )
        else:
            result.append(
                BakeoffHardGateEvidence(
                    category=category,
                    status='not_run',
                    summary=(
                        'Candidate-wide hard gate remains open; this slice establishes only '
                        'independent finite-record reference evidence and makes no production '
                        'solver selection.'
                    ),
                )
            )
    return tuple(result)


def _blocked_fixture(reason: str) -> BakeoffFixtureEvidence:
    return BakeoffFixtureEvidence(
        fixture_id=FIXTURE_ID,
        status='blocked',
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        backend_version='unavailable',
        precision='float64',
        diagnostics=(reason,),
    )


def _blocked_run(benchmark, candidates, evidence_ref: str, reason: str) -> BakeoffRun:
    fixture = _fixture(benchmark)
    candidate = _candidate(candidates)
    run = BakeoffRun(
        run_id=f'mfem-concave-finite-record-blocked-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(fixture.resource_budget.cpu_thread_budget),
        fixture_evidence=(_blocked_fixture(reason),),
        hard_gates=_hard_gates(evidence_ref, reproducible=False),
        notes=(
            'Workflow completion and independent-reference status are separate authorities.',
            'No PFFDTD result or PR #154 harmonic trace is promoted to reference truth.',
            reason,
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)
    return run


def _expected_vertices() -> dict[str, tuple[float, float, float]]:
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
    expected: dict[str, tuple[float, float, float]] = {}
    for index, (x, y) in enumerate(bottom_xy):
        expected[f'lroom-b{index}'] = (x, y, 0.0)
        expected[f'lroom-t{index}'] = (x, y, 2.5)
    return expected


def _position_tuple(position) -> tuple[float, float, float]:
    return (float(position.x_m), float(position.y_m), float(position.z_m))


def _validate_authority(benchmark, fixture) -> None:
    if benchmark.schema_version != 'r100a-4' or int(benchmark.revision) != 4:
        raise ValueError('finite-record reference requires exact R100A-4 authority')
    if fixture.fixture_id != FIXTURE_ID:
        raise ValueError('wrong finite-record fixture')
    if fixture.required_capabilities != ('wave_rigid',):
        raise ValueError('concave required capability authority changed')
    if fixture.obstacles or fixture.portals or fixture.terminations:
        raise ValueError('concave fixture unexpectedly gained obstacle/portal/termination authority')
    if len(fixture.regions) != 1 or fixture.regions[0].region_id != 'lroom':
        raise ValueError('concave region authority changed')

    actual_vertices = {
        item.vertex_id: _position_tuple(item.position)
        for item in fixture.regions[0].vertices
    }
    if actual_vertices != _expected_vertices():
        raise ValueError('concave L-prism vertices differ from frozen authority')
    if any(face.boundary_id != 'b-rigid' for face in fixture.regions[0].faces):
        raise ValueError('concave exterior boundary authority is no longer uniformly rigid')

    if len(fixture.materials) != 1 or fixture.materials[0].wave_model != 'rigid':
        raise ValueError('concave material authority changed')
    if len(fixture.sources) != 1 or len(fixture.receivers) != 1:
        raise ValueError('concave source/receiver cardinality changed')

    source = fixture.sources[0]
    receiver = fixture.receivers[0]
    if (
        source.source_id != 'src'
        or source.region_id != 'lroom'
        or _position_tuple(source.position) != (1.0, 1.0, 1.0)
        or source.normalization != 'volume_velocity_m3_s'
        or float(source.amplitude) != 1.0
        or float(source.phase_deg) != 0.0
        or source.directivity != 'omnidirectional'
    ):
        raise ValueError('concave source authority changed')
    if (
        receiver.receiver_id != 'rx'
        or receiver.region_id != 'lroom'
        or _position_tuple(receiver.position) != (5.0, 1.0, 1.0)
        or receiver.timing_reference != 'source_t0'
    ):
        raise ValueError('concave receiver authority changed')

    environment = fixture.environment
    if (
        float(environment.density_kg_m3) != 1.2
        or float(environment.sound_speed_m_s) != 343.0
    ):
        raise ValueError('concave density/sound-speed authority changed')

    comparison = fixture.comparison
    if (
        comparison.fourier_sign != 'exp(-i*omega*t)'
        or comparison.time_zero_reference != 'source_excitation_t0'
        or comparison.floating_point != 'float64'
        or comparison.window != 'none'
        or comparison.filter != 'none'
        or comparison.time_step_s is not None
        or float(comparison.observation_time_s or -1.0) != 2.0
    ):
        raise ValueError('concave numerical comparison authority changed')

    grid = comparison.frequency_grid
    if (
        grid.kind != 'uniform'
        or float(grid.start_hz or -1.0) != 20.0
        or float(grid.stop_hz or -1.0) != 300.0
        or float(grid.step_hz or -1.0) != 1.0
    ):
        raise ValueError('concave frequency-grid authority changed')

    transfer = comparison.finite_record_transfer
    if transfer is None:
        raise ValueError('concave R100A-4 finite-record transfer authority is missing')
    exact_transfer = {
        'excitation_model': 'causal_discrete_unit_sample_volume_velocity',
        'sample_zero_reference': 'source_t0',
        'record_interval': 'half_open_0_T',
        'solver_time_step_policy': 'solver_native_recorded',
        'dtft_kernel': 'exp(+i*2*pi*f*n*dt)',
        'dtft_measure': 'dt_weighted_sum',
        'numerator_quantity': 'physical_pressure',
        'numerator_record_policy': 'solver_pressure_or_declared_primary_field_conversion',
        'denominator_record': 'physical_volume_velocity_samples_on_solver_time_grid',
        'transfer_definition': 'pressure_over_volume_velocity',
        'frequency_evaluation': 'direct_scored_frequency_dtft',
        'source_spectrum_requirement': 'finite_nonzero_on_scored_grid',
        'zero_padding': 'none',
    }
    if transfer.model_dump(mode='json') != exact_transfer:
        raise ValueError('concave finite-record transfer contract differs from R100A-4')

    observable_by_id = {item.observable_id: item for item in fixture.observables}
    if set(observable_by_id) != {'lroom-fr', 'lroom-phase'}:
        raise ValueError('concave observable authority changed')
    if (
        observable_by_id['lroom-fr'].unit != 'dB re 1 Pa/(m3/s)'
        or observable_by_id['lroom-fr'].reference_kind != 'independent_solver'
        or observable_by_id['lroom-fr'].acceptance_relation != 'matches_reference'
        or observable_by_id['lroom-fr'].samples
    ):
        raise ValueError('concave magnitude observable authority changed')
    if (
        observable_by_id['lroom-phase'].unit != 'deg'
        or observable_by_id['lroom-phase'].reference_kind != 'independent_solver'
        or observable_by_id['lroom-phase'].acceptance_relation != 'matches_reference'
        or observable_by_id['lroom-phase'].samples
    ):
        raise ValueError('concave phase observable authority changed')


def _expected_frequency_grid(fixture) -> list[float]:
    grid = fixture.comparison.frequency_grid
    count = int(round((float(grid.stop_hz) - float(grid.start_hz)) / float(grid.step_hz))) + 1
    return [float(grid.start_hz) + index * float(grid.step_hz) for index in range(count)]


def _float_close(actual: float, expected: float) -> bool:
    return abs(actual - expected) <= max(1.0e-12, abs(expected) * 1.0e-12)


def _validate_raw_level(
    fixture,
    payload: dict[str, object],
    level: dict[str, object],
) -> tuple[list[float], list[float]]:
    if payload.get('schema_version') != RAW_SCHEMA:
        raise ReferenceBlocked('unexpected MFEM finite-record raw schema')
    if payload.get('fixture_id') != FIXTURE_ID:
        raise ReferenceBlocked('raw MFEM fixture id differs from authority')
    if payload.get('geometry') != 'exact-five-hex-l-prism':
        raise ReferenceBlocked('raw MFEM geometry is not the exact five-hex L-prism')
    if payload.get('boundary_model') != 'natural-neumann-rigid':
        raise ReferenceBlocked('raw MFEM boundary model is not rigid natural Neumann')
    if payload.get('primary_field') != 'velocity_potential_phi':
        raise ReferenceBlocked('raw MFEM primary-field declaration changed')
    if payload.get('pressure_conversion') != 'p=rho*d(phi)/dt':
        raise ReferenceBlocked('raw MFEM pressure conversion differs from declared authority mapping')
    if payload.get('time_integrator') != 'Newmark(beta=0.25,gamma=0.5)':
        raise ReferenceBlocked('raw MFEM time integrator differs from frozen refinement plan')
    if payload.get('algorithmic_damping') != 'none':
        raise ReferenceBlocked('algorithmic damping is forbidden for this reference')
    if (
        payload.get('window') != 'none'
        or payload.get('filter') != 'none'
        or payload.get('zero_padding') != 'none'
    ):
        raise ReferenceBlocked('window/filter/zero-padding was introduced into finite record')

    if int(payload.get('order', -1)) != int(level['order']):
        raise ReferenceBlocked('raw MFEM polynomial order differs from frozen refinement plan')
    if int(payload.get('uniform_refinements', -1)) != 0:
        raise ReferenceBlocked('raw MFEM geometry refinement differs from frozen plan')
    if int(payload.get('elements', -1)) != 5:
        raise ReferenceBlocked('raw MFEM exact geometry must retain five conforming cells')
    if int(payload.get('sample_rate_hz', -1)) != int(level['sample_rate_hz']):
        raise ReferenceBlocked('raw MFEM sample rate differs from frozen refinement plan')

    source = fixture.sources[0]
    receiver = fixture.receivers[0]
    expected_dt = 1.0 / int(level['sample_rate_hz'])
    observation_time = float(fixture.comparison.observation_time_s)
    expected_count = int(round(observation_time / expected_dt))
    expected_last = (expected_count - 1) * expected_dt

    scalar_checks = (
        ('density_kg_m3', float(fixture.environment.density_kg_m3)),
        ('sound_speed_m_s', float(fixture.environment.sound_speed_m_s)),
        ('source_amplitude_m3_s', float(source.amplitude)),
        ('source_t0_s', 0.0),
        ('observation_time_s', observation_time),
        ('dt_s', expected_dt),
        ('last_sample_time_s', expected_last),
    )
    for name, expected in scalar_checks:
        try:
            actual = float(payload[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReferenceBlocked(f'raw MFEM output is missing valid {name}') from exc
        if not _float_close(actual, expected):
            raise ReferenceBlocked(f'raw MFEM {name} differs from authority/plan: {actual} != {expected}')

    if int(payload.get('sample_count', -1)) != expected_count:
        raise ReferenceBlocked('raw MFEM record does not contain exactly [0,T) solver samples')
    if payload.get('record_interval') != 'half_open_0_T':
        raise ReferenceBlocked('raw MFEM record interval is not [0,T)')
    if payload.get('source_normalization') != 'volume_velocity_m3_s':
        raise ReferenceBlocked('raw MFEM source normalization differs from authority')
    if payload.get('sample_zero_state') != 'after_source_t0_kick_before_first_homogeneous_step':
        raise ReferenceBlocked('raw MFEM source-t0/sample-zero mapping changed')

    for name, expected in (
        ('source_position_m', list(_position_tuple(source.position))),
        ('receiver_position_m', list(_position_tuple(receiver.position))),
    ):
        actual = payload.get(name)
        if not isinstance(actual, list) or len(actual) != 3:
            raise ReferenceBlocked(f'raw MFEM output is missing {name}')
        if any(not _float_close(float(a), float(e)) for a, e in zip(actual, expected)):
            raise ReferenceBlocked(f'raw MFEM {name} differs from authority')

    source_residual = float(payload.get('source_mass_relative_residual', math.inf))
    implicit_residual = float(payload.get('max_implicit_relative_residual', math.inf))
    if not math.isfinite(source_residual) or source_residual > SOLVER_RESIDUAL_LIMIT:
        raise ReferenceBlocked(
            f'MFEM source mass residual {source_residual} exceeds fixed '
            f'{SOLVER_RESIDUAL_LIMIT} qualification limit'
        )
    if not math.isfinite(implicit_residual) or implicit_residual > SOLVER_RESIDUAL_LIMIT:
        raise ReferenceBlocked(
            f'MFEM transient implicit residual {implicit_residual} exceeds fixed '
            f'{SOLVER_RESIDUAL_LIMIT} qualification limit'
        )

    raw_samples = payload.get('samples')
    if not isinstance(raw_samples, list) or len(raw_samples) != expected_count:
        raise ReferenceBlocked('raw MFEM sample array is incomplete')

    pressures: list[float] = []
    source_samples: list[float] = []
    for index, item in enumerate(raw_samples):
        if not isinstance(item, dict) or int(item.get('index', -1)) != index:
            raise ReferenceBlocked('raw MFEM sample index sequence is not exact')
        expected_time = index * expected_dt
        if not _float_close(float(item.get('time_s', math.nan)), expected_time):
            raise ReferenceBlocked('raw MFEM sample timestamp differs from solver-native grid')
        pressure = float(item.get('pressure_pa', math.nan))
        q = float(item.get('source_volume_velocity_m3_s', math.nan))
        if not math.isfinite(pressure) or not math.isfinite(q):
            raise ReferenceBlocked('raw MFEM pressure/source record contains non-finite samples')
        expected_q = float(source.amplitude) if index == 0 else 0.0
        if q != expected_q:
            raise ReferenceBlocked(
                'raw MFEM physical source record is not q[0]=amplitude, q[n>0]=0'
            )
        pressures.append(pressure)
        source_samples.append(q)

    return pressures, source_samples


def _direct_dtft_transfer(
    fixture,
    *,
    dt_s: float,
    pressures: list[float],
    source_samples: list[float],
) -> list[dict[str, float]]:
    if len(pressures) != len(source_samples):
        raise ReferenceBlocked('pressure and source records use different sample counts')

    transfers: list[dict[str, float]] = []
    for frequency_hz in _expected_frequency_grid(fixture):
        rotation = cmath.exp(1j * 2.0 * math.pi * frequency_hz * dt_s)
        phase = 1.0 + 0.0j
        pressure_sum = 0.0 + 0.0j
        source_sum = 0.0 + 0.0j
        for pressure, source in zip(pressures, source_samples):
            pressure_sum += pressure * phase
            source_sum += source * phase
            phase *= rotation

        pressure_spectrum = dt_s * pressure_sum
        source_spectrum = dt_s * source_sum
        if (
            not math.isfinite(source_spectrum.real)
            or not math.isfinite(source_spectrum.imag)
            or abs(source_spectrum) <= 1.0e-30
        ):
            raise ReferenceBlocked(
                f'R100A-4 source spectrum is non-finite/zero at {frequency_hz} Hz'
            )
        transfer = pressure_spectrum / source_spectrum
        if not math.isfinite(transfer.real) or not math.isfinite(transfer.imag):
            raise ReferenceBlocked(
                f'finite-record P_T/Q_T is non-finite at {frequency_hz} Hz'
            )
        transfers.append(
            {
                'frequency_hz': frequency_hz,
                'real_pa_per_m3_s': transfer.real,
                'imag_pa_per_m3_s': transfer.imag,
                'magnitude_db_re_1_pa_per_m3_s': (
                    20.0 * math.log10(max(abs(transfer), 1.0e-300))
                ),
                'phase_deg': math.degrees(math.atan2(transfer.imag, transfer.real)),
            }
        )
    return transfers


def _transfer_complex(sample: dict[str, float]) -> complex:
    return complex(
        float(sample['real_pa_per_m3_s']),
        float(sample['imag_pa_per_m3_s']),
    )


def _wrapped_phase_delta_deg(first: float, second: float) -> float:
    return abs((first - second + 180.0) % 360.0 - 180.0)


def _pair_metrics(
    coarse: dict[str, object],
    fine: dict[str, object],
    *,
    magnitude_mask_db: float,
    phase_mask_db: float,
) -> dict[str, object]:
    coarse_samples = coarse['transfer_samples']
    fine_samples = fine['transfer_samples']
    if not isinstance(coarse_samples, list) or not isinstance(fine_samples, list):
        raise ReferenceBlocked('refinement level is missing transfer samples')
    if len(coarse_samples) != len(fine_samples):
        raise ReferenceBlocked('refinement levels use different scored frequency counts')

    magnitude_abs: list[float] = []
    magnitude_rel: list[float] = []
    phase_error: list[float] = []
    squared_diff = 0.0
    squared_reference = 0.0

    for coarse_sample, fine_sample in zip(coarse_samples, fine_samples):
        if not _float_close(
            float(coarse_sample['frequency_hz']),
            float(fine_sample['frequency_hz']),
        ):
            raise ReferenceBlocked('refinement transfer grids differ')
        coarse_value = _transfer_complex(coarse_sample)
        fine_value = _transfer_complex(fine_sample)
        coarse_magnitude = abs(coarse_value)
        fine_magnitude = abs(fine_value)
        coarse_db = 20.0 * math.log10(max(coarse_magnitude, 1.0e-300))
        fine_db = 20.0 * math.log10(max(fine_magnitude, 1.0e-300))

        delta = coarse_value - fine_value
        squared_diff += delta.real * delta.real + delta.imag * delta.imag
        squared_reference += (
            fine_value.real * fine_value.real + fine_value.imag * fine_value.imag
        )

        if fine_db >= magnitude_mask_db:
            magnitude_abs.append(abs(coarse_db - fine_db))
            magnitude_rel.append(
                abs(coarse_magnitude - fine_magnitude) / max(fine_magnitude, 1.0e-300)
            )
        if fine_db >= phase_mask_db and coarse_magnitude > 0.0 and fine_magnitude > 0.0:
            phase_error.append(
                _wrapped_phase_delta_deg(
                    math.degrees(math.atan2(coarse_value.imag, coarse_value.real)),
                    math.degrees(math.atan2(fine_value.imag, fine_value.real)),
                )
            )

    if not magnitude_abs or not phase_error:
        raise ReferenceBlocked('R100A null masks removed all refinement-comparison samples')

    return {
        'coarse_level_id': coarse['level_id'],
        'fine_level_id': fine['level_id'],
        'coarse_order': coarse['order'],
        'fine_order': fine['order'],
        'coarse_dt_s': coarse['dt_s'],
        'fine_dt_s': fine['dt_s'],
        'magnitude_max_abs_db': max(magnitude_abs),
        'magnitude_max_relative': max(magnitude_rel),
        'phase_max_error_deg': max(phase_error),
        'complex_rms_relative': math.sqrt(
            squared_diff / max(squared_reference, 1.0e-300)
        ),
        'magnitude_sample_count': len(magnitude_abs),
        'phase_sample_count': len(phase_error),
    }


def _reference_evidence(fixture, levels: list[dict[str, object]]):
    observable_by_id = {item.observable_id: item for item in fixture.observables}
    magnitude = observable_by_id['lroom-fr']
    phase = observable_by_id['lroom-phase']

    pair_metrics = [
        _pair_metrics(
            levels[index],
            levels[index + 1],
            magnitude_mask_db=float(magnitude.tolerance.null_mask_below_db),
            phase_mask_db=float(phase.tolerance.null_mask_below_db),
        )
        for index in range(len(levels) - 1)
    ]
    rms_sequence = [float(item['complex_rms_relative']) for item in pair_metrics]
    monotonic_rms = all(
        rms_sequence[index] > rms_sequence[index + 1]
        for index in range(len(rms_sequence) - 1)
    )
    final = pair_metrics[-1]

    magnitude_abs_limit = (
        float(magnitude.tolerance.absolute) * REFERENCE_QUALIFICATION_FRACTION
    )
    magnitude_relative_limit = (
        float(magnitude.tolerance.relative) * REFERENCE_QUALIFICATION_FRACTION
    )
    phase_limit = float(phase.tolerance.phase_deg) * REFERENCE_QUALIFICATION_FRACTION

    magnitude_violations: list[str] = []
    phase_violations: list[str] = []
    if not monotonic_rms:
        message = 'complex RMS coupled p/dt refinement error is not strictly decreasing'
        magnitude_violations.append(message)
        phase_violations.append(message)
    if float(final['magnitude_max_abs_db']) > magnitude_abs_limit:
        magnitude_violations.append(
            f'final magnitude delta {final["magnitude_max_abs_db"]} dB exceeds fixed '
            f'independent-reference qualification {magnitude_abs_limit} dB'
        )
    if float(final['magnitude_max_relative']) > magnitude_relative_limit:
        magnitude_violations.append(
            f'final relative magnitude delta {final["magnitude_max_relative"]} exceeds fixed '
            f'independent-reference qualification {magnitude_relative_limit}'
        )
    if float(final['phase_max_error_deg']) > phase_limit:
        phase_violations.append(
            f'final phase delta {final["phase_max_error_deg"]} deg exceeds fixed '
            f'independent-reference qualification {phase_limit} deg'
        )

    magnitude_evidence = BakeoffObservableEvidence(
        observable_id='lroom-fr',
        status='fail' if magnitude_violations else 'pass',
        summary=(
            '; '.join(magnitude_violations)
            if magnitude_violations
            else 'Independent transient MFEM finite-record magnitude reference converged.'
        ),
        absolute_error=float(final['magnitude_max_abs_db']),
        relative_error=float(final['magnitude_max_relative']),
    )
    phase_evidence = BakeoffObservableEvidence(
        observable_id='lroom-phase',
        status='fail' if phase_violations else 'pass',
        summary=(
            '; '.join(phase_violations)
            if phase_violations
            else 'Independent transient MFEM finite-record phase reference converged.'
        ),
        phase_error_deg=float(final['phase_max_error_deg']),
    )

    for expected, evidence in (
        (magnitude, magnitude_evidence),
        (phase, phase_evidence),
    ):
        if evidence.status == 'pass':
            violations = observable_tolerance_violations(expected, evidence)
            if violations:
                raise ReferenceBlocked(
                    'reference qualification passed but central R100B tolerance authority '
                    'rejected it: ' + '; '.join(violations)
                )

    qualification = {
        'refinement_plan_frozen_before_results': True,
        'refinement_kind': 'coupled_h1_p_order_and_solver_native_dt',
        'qualification_fraction_of_frozen_tolerance': REFERENCE_QUALIFICATION_FRACTION,
        'solver_residual_limit': SOLVER_RESIDUAL_LIMIT,
        'frozen_candidate_tolerances_unchanged': {
            'magnitude_absolute_db': float(magnitude.tolerance.absolute),
            'magnitude_relative': float(magnitude.tolerance.relative),
            'phase_deg': float(phase.tolerance.phase_deg),
        },
        'reference_qualification_limits': {
            'magnitude_absolute_db': magnitude_abs_limit,
            'magnitude_relative': magnitude_relative_limit,
            'phase_deg': phase_limit,
        },
        'complex_rms_relative_strictly_decreasing': monotonic_rms,
        'complex_rms_relative_sequence': rms_sequence,
    }
    return (magnitude_evidence, phase_evidence), pair_metrics, qualification


def _frequency_key(frequency_hz: float) -> str:
    return f'f={frequency_hz:.12g}Hz'


def _raw_reference_observation(
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
    qualified: bool,
) -> RawFixtureObservation:
    magnitude_samples: list[RawObservationSample] = []
    phase_samples: list[RawObservationSample] = []

    transfer_samples = finest['transfer_samples']
    if not isinstance(transfer_samples, list):
        raise ReferenceBlocked('finest level has no transfer samples')
    for item in transfer_samples:
        frequency = float(item['frequency_hz'])
        key = _frequency_key(frequency)
        magnitude_samples.append(
            RawObservationSample(
                sample_key=key,
                frequency_hz=frequency,
                scalar_value=float(item['magnitude_db_re_1_pa_per_m3_s']),
            )
        )
        phase_samples.append(
            RawObservationSample(
                sample_key=key,
                frequency_hz=frequency,
                scalar_value=float(item['phase_deg']),
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
                diagnostics=(
                    'Direct dt-weighted P_T/Q_T on the R100A-4 scored grid.',
                    f'independent_reference_qualified={qualified}',
                ),
            ),
            RawObservableObservation(
                observable_id='lroom-phase',
                kind='transfer_phase_deg',
                unit='deg',
                samples=tuple(phase_samples),
                diagnostics=(
                    'Phase from the same direct complex P_T/Q_T values.',
                    f'independent_reference_qualified={qualified}',
                ),
            ),
        ),
        diagnostics=(
            'Physical pressure numerator p=rho*d(phi)/dt is recorded before DTFT.',
            'Physical pre-scaling source record q[0]=1, q[n>0]=0 is the DTFT denominator.',
            'Analysis kernel is exp(+i*2*pi*f*n*dt); no FFT, interpolation, window, filter, '
            'or zero padding is used.',
            'This object is central-evaluator-compatible raw observation evidence; its '
            f'qualification state is {qualified}.',
        ),
    )


def _execute_level(
    fixture,
    *,
    executable: Path,
    work_dir: Path,
    level: dict[str, object],
) -> tuple[dict[str, object], float]:
    level_id = str(level['level_id'])
    raw_path = work_dir / f'{level_id}.json'
    source = fixture.sources[0]
    receiver = fixture.receivers[0]
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
        '--observation-time', str(fixture.comparison.observation_time_s),
        '--sample-rate-hz', str(level['sample_rate_hz']),
        '--order', str(level['order']),
        '--uniform-refinements', '0',
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
        raise ReferenceBlocked(
            f'MFEM transient level {level_id} exited {process.returncode}: {stdout[-6000:]}'
        )
    if not raw_path.is_file():
        raise ReferenceBlocked(f'MFEM transient level {level_id} did not produce raw output')

    payload = json.loads(raw_path.read_text(encoding='utf-8'))
    pressures, source_samples = _validate_raw_level(fixture, payload, level)
    dt_s = float(payload['dt_s'])
    transfer_samples = _direct_dtft_transfer(
        fixture,
        dt_s=dt_s,
        pressures=pressures,
        source_samples=source_samples,
    )
    result = {
        'level_id': level_id,
        'order': int(level['order']),
        'sample_rate_hz': int(level['sample_rate_hz']),
        'dt_s': dt_s,
        'sample_count': int(payload['sample_count']),
        'last_sample_time_s': float(payload['last_sample_time_s']),
        'elements': int(payload['elements']),
        'ndofs': int(payload['ndofs']),
        'assemble_s': float(payload['assemble_s']),
        'solve_s': float(payload['solve_s']),
        'source_mass_relative_residual': float(payload['source_mass_relative_residual']),
        'max_implicit_relative_residual': float(payload['max_implicit_relative_residual']),
        'max_implicit_iterations': int(payload['max_implicit_iterations']),
        'raw_output_path': raw_path.as_posix(),
        'raw_output_sha256': _sha256_file(raw_path),
        'raw_output_mb': raw_path.stat().st_size / (1024.0 * 1024.0),
        'transfer_samples': transfer_samples,
        'raw_solver_output': payload,
        'stdout_tail': stdout[-6000:],
    }
    return result, peak_ram_mb


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
    fixture = _fixture(benchmark)
    _validate_authority(benchmark, fixture)
    candidate = _candidate(candidates)
    if _git_head(mfem_root) != candidate.source_commit_sha:
        raise ReferenceBlocked('MFEM checkout does not match pinned candidate source commit')
    if not executable.is_file():
        raise ReferenceBlocked(f'MFEM transient executable is missing: {executable}')

    work_dir.mkdir(parents=True, exist_ok=True)
    htdt_git = _htdt_git_provenance()
    post_started = time.perf_counter()

    levels: list[dict[str, object]] = []
    peak_ram_mb = 0.0
    for level in REFINEMENT_LEVELS:
        result, level_peak_ram_mb = _execute_level(
            fixture,
            executable=executable,
            work_dir=work_dir,
            level=level,
        )
        levels.append(result)
        peak_ram_mb = max(peak_ram_mb, level_peak_ram_mb)

    observable_evidence, convergence, qualification = _reference_evidence(fixture, levels)
    qualified = all(item.status == 'pass' for item in observable_evidence)

    assembly_s = sum(float(item['assemble_s']) for item in levels)
    solve_s = sum(float(item['solve_s']) for item in levels)
    disk_mb = _directory_size_mb(work_dir)
    raw_output_mb = sum(float(item['raw_output_mb']) for item in levels)
    backend_version = str(levels[-1]['raw_solver_output']['mfem_version'])

    provisional_postprocess_s = time.perf_counter() - post_started - solve_s
    provisional_postprocess_s = max(0.0, provisional_postprocess_s)
    raw_observation = _raw_reference_observation(
        fixture,
        levels[-1],
        evidence_ref=evidence_ref,
        backend_version=backend_version,
        compile_s=assembly_s,
        solve_s=solve_s,
        postprocess_s=provisional_postprocess_s,
        peak_ram_mb=peak_ram_mb,
        disk_mb=disk_mb,
        output_mb=raw_output_mb,
        qualified=qualified,
    )
    postprocess_s = max(0.0, time.perf_counter() - post_started - solve_s)
    raw_observation = raw_observation.model_copy(update={'postprocess_s': postprocess_s})

    fixture_status = 'pass' if qualified else 'fail'
    fixture_evidence = BakeoffFixtureEvidence(
        fixture_id=fixture.fixture_id,
        status=fixture_status,
        evidence_ref=evidence_ref,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        backend_version=backend_version,
        precision='float64',
        compile_s=assembly_s,
        solve_s=solve_s,
        postprocess_s=postprocess_s,
        peak_ram_mb=peak_ram_mb,
        disk_mb=disk_mb,
        output_mb=raw_output_mb,
        observables=observable_evidence,
        diagnostics=(
            'Exact five-cell L-prism; natural rigid Neumann boundary on every exterior face.',
            'Transient velocity-potential FEM with physical pressure p=rho*d(phi)/dt.',
            'Causal physical source q[0]=1 m3/s and q[n>0]=0 is recorded before '
            'solver-internal c^2*dt mass-kick scaling.',
            'Newmark beta=0.25 gamma=0.5; no algorithmic damping/window/filter/zero-padding.',
            'Refinement plan was frozen before this workflow observed transient results.',
            'No PFFDTD observation and no PR #154 harmonic finest trace participates.',
        ),
    )

    run = BakeoffRun(
        run_id=f'mfem-concave-finite-record-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(fixture.resource_budget.cpu_thread_budget),
        fixture_evidence=(fixture_evidence,),
        hard_gates=_hard_gates(evidence_ref, reproducible=True),
        notes=(
            'R100A-4 independent finite-record reference attempt only.',
            'PASS means the independent reference convergence qualification succeeded; '
            'FAIL preserves the non-converged sequence without promoting its finest trace.',
            'No production solver selection and no R110 claim is made.',
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)

    cache_path = build_dir / 'CMakeCache.txt'
    executable_size_mb = executable.stat().st_size / (1024.0 * 1024.0)
    source_path = Path(__file__).resolve()
    cpp_path = (
        source_path.parent.parent
        / 'benchmarks'
        / 'acoustics'
        / 'mfem_probe'
        / 'concave_finite_record.cpp'
    )
    details: dict[str, object] = {
        'mfem_source_commit_sha': candidate.source_commit_sha,
        'htdt_source_commit_sha': htdt_git['pr_head_commit_sha'],
        'htdt_checkout_commit_sha': htdt_git['checkout_commit_sha'],
        'htdt_pr_head_commit_sha': htdt_git['pr_head_commit_sha'],
        'native_build_s': native_build_s,
        'dependency_versions': _runtime_versions(),
        'probe_script_sha256': _sha256_file(source_path),
        'probe_cpp_sha256': _sha256_file(cpp_path) if cpp_path.is_file() else None,
        'probe_executable_sha256': _sha256_file(executable),
        'probe_executable_mb': executable_size_mb,
        'cmake_cache_sha256': _sha256_file(cache_path) if cache_path.is_file() else None,
        'authority': {
            'fixture': fixture.model_dump(mode='json'),
            'finite_record_contract': (
                fixture.comparison.finite_record_transfer.model_dump(mode='json')
            ),
        },
        'source_mapping': {
            'physical_source_record': 'q[0]=source.amplitude; q[n>0]=0',
            'solver_internal_mapping': 'phi_t(0+)=c^2*dt*M^-1*b*q[0]',
            'governing_semidiscrete_equation': 'M*phi_tt+c^2*K*phi=c^2*b*q',
            'physical_pressure': 'p=rho*d(phi)/dt',
            'denominator_uses_pre_scaling_physical_q': True,
        },
        'analysis': {
            'record_interval': '[0,T)',
            'source_t0_is_sample_zero': True,
            'dtft_kernel': 'exp(+i*2*pi*f*n*dt)',
            'dtft_measure': 'dt_weighted_sum',
            'transfer': 'P_T/Q_T',
            'frequency_evaluation': 'direct_scored_frequency_dtft',
            'window': 'none',
            'filter': 'none',
            'zero_padding': 'none',
        },
        'refinement_plan': list(REFINEMENT_LEVELS),
        'levels': levels,
        'convergence': convergence,
        'qualification': qualification,
        'qualified_reference_observation': (
            raw_observation.model_dump(mode='json') if qualified else None
        ),
        'raw_finest_unqualified_observation': (
            None if qualified else raw_observation.model_dump(mode='json')
        ),
        'central_evaluator_ready': qualified,
        'resource_evidence': {
            'native_build_s': native_build_s,
            'fem_assembly_s': assembly_s,
            'solve_s': solve_s,
            'postprocess_s': postprocess_s,
            'peak_process_tree_ram_mb': peak_ram_mb,
            'work_disk_mb': disk_mb,
            'raw_output_mb': raw_output_mb,
            'probe_executable_mb': executable_size_mb,
        },
    }
    return details, run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run R100A-4 MFEM independent finite-record concave reference'
    )
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--candidates', required=True, type=Path)
    parser.add_argument('--mfem-root', required=True, type=Path)
    parser.add_argument('--build-dir', required=True, type=Path)
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
    fixture = _fixture(benchmark)
    _validate_authority(benchmark, fixture)
    evidence_ref = f'artifact:{args.output.as_posix()}'

    if args.blocked_reason:
        reason = str(args.blocked_reason)
        run = _blocked_run(benchmark, candidates, evidence_ref, reason)
        payload = {
            'schema_version': ARTIFACT_SCHEMA,
            'finite_record_reference_outcome': 'blocked',
            'workflow_result_is_reference_result': False,
            'r100a_manifest_id': benchmark.manifest_id,
            'r100a_semantic_hash': benchmark.semantic_hash(),
            'candidate_manifest_hash': candidates.semantic_hash(),
            'details': {
                'error': reason,
                'authority_change_required': False,
                'note': (
                    'The current R100A-4 quantity is representable by MFEM; this BLOCKED '
                    'state records setup/build/runtime inability rather than substituting a '
                    'steady-state Helmholtz quantity.'
                ),
            },
            'bakeoff_run': run.model_dump(mode='json'),
        }
    else:
        if args.executable is None:
            raise SystemExit('--executable is required unless --blocked-reason is used')
        try:
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
            outcome = next(
                item.status for item in run.fixture_evidence if item.fixture_id == FIXTURE_ID
            )
            payload = {
                'schema_version': ARTIFACT_SCHEMA,
                'finite_record_reference_outcome': outcome,
                'workflow_result_is_reference_result': False,
                'r100a_manifest_id': benchmark.manifest_id,
                'r100a_semantic_hash': benchmark.semantic_hash(),
                'candidate_manifest_hash': candidates.semantic_hash(),
                'details': details,
                'bakeoff_run': run.model_dump(mode='json'),
            }
        except ReferenceBlocked as exc:
            reason = f'{type(exc).__name__}: {exc}'
            run = _blocked_run(benchmark, candidates, evidence_ref, reason)
            payload = {
                'schema_version': ARTIFACT_SCHEMA,
                'finite_record_reference_outcome': 'blocked',
                'workflow_result_is_reference_result': False,
                'r100a_manifest_id': benchmark.manifest_id,
                'r100a_semantic_hash': benchmark.semantic_hash(),
                'candidate_manifest_hash': candidates.semantic_hash(),
                'details': {
                    'error': reason,
                    'authority_change_required': False,
                    'note': (
                        'MFEM can represent the R100A-4 transient quantity in principle; '
                        'this run failed a fixed technical qualification and is retained as '
                        'BLOCKED evidence. No harmonic substitute is used.'
                    ),
                },
                'bakeoff_run': run.model_dump(mode='json'),
            }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )

    summary = {
        'finite_record_reference_outcome': payload['finite_record_reference_outcome'],
        'r100a_semantic_hash': payload['r100a_semantic_hash'],
        'candidate_manifest_hash': payload['candidate_manifest_hash'],
        'output': str(args.output),
    }
    details = payload.get('details')
    if isinstance(details, dict):
        for key in ('qualification', 'resource_evidence', 'error'):
            if key in details:
                summary[key] = details[key]
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
