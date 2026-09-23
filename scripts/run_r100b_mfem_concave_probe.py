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
CONCAVE_FIXTURE_ID = 'wave-concave-l-room-v1'
IMPEDANCE_FIXTURE_ID = 'wave-normal-incidence-impedance-v1'
CONCAVE_ADAPTER_ID = 'htdt-r100b-mfem-concave-transfer'
IMPEDANCE_ADAPTER_ID = 'htdt-r100b-mfem-impedance-capability'
ADAPTER_VERSION = '1'
ARTIFACT_SCHEMA = 'r100b-mfem-concave-reference-artifact-1'
REFERENCE_QUALIFICATION_FRACTION = 0.25
ORDER_MIN = 2
ORDER_MAX = 5
SOLVER_RESIDUAL_LIMIT = 1.0e-8


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


def _fixture(benchmark, fixture_id: str):
    return next(item for item in benchmark.fixtures if item.fixture_id == fixture_id)


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
    is_sha = (
        len(event_head) == 40
        and all(character in '0123456789abcdef' for character in event_head)
    )
    if is_sha:
        pr_head = event_head
    else:
        # Fallback for local/manual execution where the workflow event SHA is
        # not injected. pull_request checkout may be a synthetic merge commit.
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
            'GitHub-hosted Windows MFEM v4.10 serial H1 FEM independent-reference probe. '
            'The concave fixture is solved on an exact conforming L-prism volume mesh.'
        ),
    )


def _expected_concave_vertices() -> dict[str, tuple[float, float, float]]:
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


def _validate_concave_authority(fixture) -> None:
    if fixture.fixture_id != CONCAVE_FIXTURE_ID:
        raise ValueError('wrong concave fixture')
    if fixture.required_capabilities != ('wave_rigid',):
        raise ValueError('concave fixture capability authority changed')
    if len(fixture.regions) != 1 or fixture.regions[0].region_id != 'lroom':
        raise ValueError('concave fixture must contain exactly frozen lroom region')
    if fixture.obstacles or fixture.portals or fixture.terminations:
        raise ValueError('concave fixture topology authority changed')

    region = fixture.regions[0]
    actual_vertices = {
        item.vertex_id: _position_tuple(item.position)
        for item in region.vertices
    }
    if actual_vertices != _expected_concave_vertices():
        raise ValueError('concave fixture vertices differ from frozen R100A-2 authority')

    expected_faces = {
        'lroom-bottom': (
            ('lroom-b0', 'lroom-b1', 'lroom-b2', 'lroom-b3',
             'lroom-b4', 'lroom-b5', 'lroom-b6', 'lroom-b7'),
            'b-rigid',
        ),
        'lroom-top': (
            ('lroom-t7', 'lroom-t6', 'lroom-t5', 'lroom-t4',
             'lroom-t3', 'lroom-t2', 'lroom-t1', 'lroom-t0'),
            'b-rigid',
        ),
    }
    for index in range(8):
        expected_faces[f'lroom-side{index}'] = (
            (
                f'lroom-b{index}',
                f'lroom-b{(index + 1) % 8}',
                f'lroom-t{(index + 1) % 8}',
                f'lroom-t{index}',
            ),
            'b-rigid',
        )
    actual_faces = {
        item.face_id: (tuple(item.vertex_ids), item.boundary_id)
        for item in region.faces
    }
    if actual_faces != expected_faces:
        raise ValueError('concave fixture faces differ from frozen R100A-2 authority')

    if len(fixture.materials) != 1:
        raise ValueError('concave fixture material authority changed')
    material = fixture.materials[0]
    if material.material_id != 'rigid' or material.wave_model != 'rigid':
        raise ValueError('concave fixture requires explicit rigid material')
    if material.specific_impedance:
        raise ValueError('concave rigid material must not carry impedance samples')
    if len(fixture.boundaries) != 1:
        raise ValueError('concave fixture boundary authority changed')
    boundary = fixture.boundaries[0]
    if boundary.boundary_id != 'b-rigid' or boundary.material_id != 'rigid':
        raise ValueError('concave fixture requires b-rigid -> rigid')

    if len(fixture.sources) != 1 or len(fixture.receivers) != 1:
        raise ValueError('concave fixture requires one frozen source and receiver')
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
        or receiver.calibration_state != 'ideal_flat'
        or receiver.timing_reference != 'source_t0'
    ):
        raise ValueError('concave receiver authority changed')

    environment = fixture.environment
    if float(environment.density_kg_m3) != 1.2 or float(environment.sound_speed_m_s) != 343.0:
        raise ValueError('concave density/sound-speed authority changed')

    comparison = fixture.comparison
    if (
        comparison.fourier_sign != 'exp(-i*omega*t)'
        or comparison.time_zero_reference != 'source_excitation_t0'
        or comparison.floating_point != 'float64'
        or comparison.interpolation != 'linear_complex'
        or comparison.window != 'none'
        or comparison.filter != 'none'
        or comparison.frequency_grid.kind != 'uniform'
        or float(comparison.frequency_grid.start_hz) != 20.0
        or float(comparison.frequency_grid.stop_hz) != 300.0
        or float(comparison.frequency_grid.step_hz) != 1.0
    ):
        raise ValueError('concave frequency/Fourier/comparison authority changed')

    observable_by_id = {item.observable_id: item for item in fixture.observables}
    if set(observable_by_id) != {'lroom-fr', 'lroom-phase'}:
        raise ValueError('concave observable authority changed')
    if (
        observable_by_id['lroom-fr'].kind != 'transfer_magnitude_db'
        or observable_by_id['lroom-fr'].reference_kind != 'independent_solver'
        or observable_by_id['lroom-fr'].samples
    ):
        raise ValueError('concave magnitude observable authority changed')
    if (
        observable_by_id['lroom-phase'].kind != 'transfer_phase_deg'
        or observable_by_id['lroom-phase'].reference_kind != 'independent_solver'
        or observable_by_id['lroom-phase'].samples
    ):
        raise ValueError('concave phase observable authority changed')


def _validate_impedance_authority(fixture) -> dict[str, object]:
    if fixture.fixture_id != IMPEDANCE_FIXTURE_ID:
        raise ValueError('wrong impedance fixture')
    if set(fixture.required_capabilities) != {'wave_rigid', 'wave_impedance'}:
        raise ValueError('impedance fixture capability authority changed')
    if fixture.obstacles or fixture.portals or fixture.terminations:
        raise ValueError('impedance fixture topology authority changed')
    if len(fixture.regions) != 1 or fixture.regions[0].region_id != 'room':
        raise ValueError('impedance fixture room authority changed')

    material_by_id = {item.material_id: item for item in fixture.materials}
    if set(material_by_id) != {'rigid', 'z-2z0'}:
        raise ValueError('impedance fixture material ids changed')
    impedance = material_by_id['z-2z0']
    if impedance.wave_model != 'specific_impedance_table':
        raise ValueError('impedance fixture no longer carries explicit specific impedance')
    expected_table = (
        (100.0, 823.2, 0.0),
        (200.0, 823.2, 0.0),
        (300.0, 823.2, 0.0),
    )
    actual_table = tuple(
        (
            float(item.frequency_hz),
            float(item.resistance_pa_s_m),
            float(item.reactance_pa_s_m),
        )
        for item in impedance.specific_impedance
    )
    if actual_table != expected_table:
        raise ValueError('explicit impedance table differs from frozen R100A-2 authority')

    boundary_by_id = {item.boundary_id: item.material_id for item in fixture.boundaries}
    if boundary_by_id != {'b-rigid': 'rigid', 'b-impedance': 'z-2z0'}:
        raise ValueError('impedance boundary/material authority changed')
    face_boundaries = {
        item.face_id: item.boundary_id
        for item in fixture.regions[0].faces
    }
    if face_boundaries.get('room-xmax') != 'b-impedance':
        raise ValueError('impedance face is not the frozen room-xmax face')
    if any(
        boundary_id != 'b-rigid'
        for face_id, boundary_id in face_boundaries.items()
        if face_id != 'room-xmax'
    ):
        raise ValueError('non-xmax impedance fixture face is not rigid')

    environment = fixture.environment
    rho = float(environment.density_kg_m3)
    sound_speed = float(environment.sound_speed_m_s)
    if rho != 1.2 or sound_speed != 343.0:
        raise ValueError('impedance density/sound-speed authority changed')
    characteristic_impedance = rho * sound_speed
    resistance = expected_table[0][1]
    normalized_impedance = resistance / characteristic_impedance
    admittance = 1.0 / resistance
    normalized_admittance = characteristic_impedance / resistance

    comparison = fixture.comparison
    if (
        comparison.fourier_sign != 'exp(-i*omega*t)'
        or comparison.frequency_grid.kind != 'explicit'
        or tuple(float(item) for item in comparison.frequency_grid.values_hz)
        != (100.0, 200.0, 300.0)
    ):
        raise ValueError('impedance frequency/Fourier authority changed')

    if len(fixture.observables) != 1:
        raise ValueError('impedance observable authority changed')
    observable = fixture.observables[0]
    if (
        observable.observable_id != 'reflection-r'
        or observable.kind != 'complex_reflection_coefficient'
        or observable.reference_kind != 'closed_form'
    ):
        raise ValueError('impedance reflection observable authority changed')

    return {
        'fixture_id': fixture.fixture_id,
        'explicit_specific_impedance_pa_s_m': {
            'resistance': resistance,
            'reactance': 0.0,
            'frequencies_hz': [100.0, 200.0, 300.0],
        },
        'density_kg_m3': rho,
        'sound_speed_m_s': sound_speed,
        'characteristic_impedance_pa_s_m': characteristic_impedance,
        'normalized_impedance': normalized_impedance,
        'specific_admittance_m_pa_s': admittance,
        'normalized_admittance': normalized_admittance,
        'mfem_robin_capability_under_frozen_fourier_sign': (
            'For exp(-i*omega*t), p=Z*u_n and grad(p)=i*omega*rho*u imply '
            'd(p)/dn=i*omega*rho*p/Z. MFEM can represent the resulting complex '
            'boundary mass term exactly for the frozen constant real Z.'
        ),
        'reflection_reference_extraction_status': 'blocked',
        'block_reason': (
            'The frozen fixture defines a point volume-velocity source in a finite room but asks '
            'for incident/reflected complex reflection coefficient R. An independent MFEM spatial '
            'R result would require an incident/reflected decomposition, plane-wave excitation, '
            'or another extraction authority that R100A-2 does not freeze. Computing R directly '
            'from Z would only repeat the closed form and is not independent numerical evidence.'
        ),
        'scalar_absorption_used': False,
    }


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
                        'Pinned MFEM source commit, frozen R100A-2 semantic hash, exact '
                        'concave geometry/source/receiver/environment, discretization, '
                        'raw transfer samples, convergence and resource evidence are recorded.'
                    ),
                )
            )
        else:
            result.append(
                BakeoffHardGateEvidence(
                    category=category,
                    status='not_run',
                    summary=(
                        'Candidate-wide hard gate remains open. This independent-reference slice '
                        'does not select a production solver or complete candidate-wide R100B gates.'
                    ),
                )
            )
    return tuple(result)


def _blocked_fixture(
    fixture_id: str,
    *,
    adapter_id: str,
    backend_version: str,
    reason: str,
) -> BakeoffFixtureEvidence:
    return BakeoffFixtureEvidence(
        fixture_id=fixture_id,
        status='blocked',
        adapter_id=adapter_id,
        adapter_version=ADAPTER_VERSION,
        backend_version=backend_version,
        precision='float64',
        diagnostics=(reason,),
    )


def _blocked_run(
    benchmark,
    candidates,
    evidence_ref: str,
    reason: str,
    impedance_reason: str,
) -> BakeoffRun:
    candidate = _candidate(candidates)
    concave = _fixture(benchmark, CONCAVE_FIXTURE_ID)
    run = BakeoffRun(
        run_id=f'mfem-concave-reference-blocked-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(concave.resource_budget.cpu_thread_budget),
        fixture_evidence=(
            _blocked_fixture(
                CONCAVE_FIXTURE_ID,
                adapter_id=CONCAVE_ADAPTER_ID,
                backend_version='unavailable',
                reason=reason,
            ),
            _blocked_fixture(
                IMPEDANCE_FIXTURE_ID,
                adapter_id=IMPEDANCE_ADAPTER_ID,
                backend_version='unavailable',
                reason=impedance_reason,
            ),
        ),
        hard_gates=_hard_gates(evidence_ref, reproducible=False),
        notes=(
            'Workflow completion and numerical-reference status are separate authorities.',
            reason,
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)
    return run


def _complex(sample: dict[str, object]) -> complex:
    return complex(
        float(sample['pressure_real_pa']),
        float(sample['pressure_imag_pa']),
    )


def _wrapped_phase_deg(value: complex) -> float:
    return math.degrees(math.atan2(value.imag, value.real))


def _wrapped_phase_delta_deg(first: float, second: float) -> float:
    return abs((first - second + 180.0) % 360.0 - 180.0)


def _frequency_key(frequency_hz: float) -> str:
    return f'f={frequency_hz:.12g}Hz'


def _expected_frequency_grid(fixture) -> list[float]:
    grid = fixture.comparison.frequency_grid
    count = int(round((float(grid.stop_hz) - float(grid.start_hz)) / float(grid.step_hz))) + 1
    return [float(grid.start_hz) + index * float(grid.step_hz) for index in range(count)]


def _validate_raw_orders(fixture, raw_payload: dict[str, object]) -> list[dict[str, object]]:
    if raw_payload.get('schema_version') != 'r100b-mfem-concave-transfer-raw-1':
        raise ValueError('unexpected MFEM concave raw schema')
    if raw_payload.get('fourier_sign') != fixture.comparison.fourier_sign:
        raise ValueError('raw MFEM Fourier convention differs from authority')
    if float(raw_payload.get('density_kg_m3', -1.0)) != float(fixture.environment.density_kg_m3):
        raise ValueError('raw MFEM density differs from authority')
    if float(raw_payload.get('sound_speed_m_s', -1.0)) != float(fixture.environment.sound_speed_m_s):
        raise ValueError('raw MFEM sound speed differs from authority')

    orders = raw_payload.get('orders')
    if not isinstance(orders, list):
        raise ValueError('raw MFEM output is missing p-refinement orders')
    expected_orders = list(range(ORDER_MIN, ORDER_MAX + 1))
    actual_orders = [int(item['order']) for item in orders]
    if actual_orders != expected_orders:
        raise ValueError(f'expected p-refinement orders {expected_orders}, got {actual_orders}')

    expected_grid = _expected_frequency_grid(fixture)
    for level in orders:
        if int(level.get('elements', 0)) != 5:
            raise ValueError('MFEM concave reference must preserve the exact five-cell L-prism mesh')
        samples = level.get('samples')
        if not isinstance(samples, list) or len(samples) != len(expected_grid):
            raise ValueError('MFEM concave reference did not return the complete frozen grid')
        actual_grid = [float(item['frequency_hz']) for item in samples]
        for actual, expected in zip(actual_grid, expected_grid):
            if abs(actual - expected) > max(1.0e-9, abs(expected) * 1.0e-12):
                raise ValueError(f'raw frequency grid mismatch: {actual} != {expected}')
        maximum_residual = max(float(item['relative_residual']) for item in samples)
        if maximum_residual > SOLVER_RESIDUAL_LIMIT:
            raise ValueError(
                f'MFEM linear solve residual {maximum_residual} exceeds fixed '
                f'{SOLVER_RESIDUAL_LIMIT} reference qualification limit'
            )
    return orders


def _pair_metrics(
    coarse: dict[str, object],
    fine: dict[str, object],
    *,
    magnitude_mask_db: float,
    phase_mask_db: float,
) -> dict[str, float | int]:
    coarse_samples = coarse['samples']
    fine_samples = fine['samples']
    if len(coarse_samples) != len(fine_samples):
        raise ValueError('p-refinement levels use different sample counts')

    magnitude_abs: list[float] = []
    magnitude_rel: list[float] = []
    phase_error: list[float] = []
    squared_diff = 0.0
    squared_reference = 0.0
    for coarse_sample, fine_sample in zip(coarse_samples, fine_samples):
        coarse_p = _complex(coarse_sample)
        fine_p = _complex(fine_sample)
        fine_magnitude = abs(fine_p)
        coarse_magnitude = abs(coarse_p)
        fine_db = 20.0 * math.log10(max(fine_magnitude, 1.0e-300))
        coarse_db = 20.0 * math.log10(max(coarse_magnitude, 1.0e-300))

        diff = coarse_p - fine_p
        squared_diff += diff.real * diff.real + diff.imag * diff.imag
        squared_reference += fine_p.real * fine_p.real + fine_p.imag * fine_p.imag

        if fine_db >= magnitude_mask_db:
            delta_db = abs(coarse_db - fine_db)
            magnitude_abs.append(delta_db)
            magnitude_rel.append(
                abs(coarse_magnitude - fine_magnitude) / max(fine_magnitude, 1.0e-300)
            )

        if fine_db >= phase_mask_db and coarse_magnitude > 0.0 and fine_magnitude > 0.0:
            phase_error.append(
                _wrapped_phase_delta_deg(
                    _wrapped_phase_deg(coarse_p),
                    _wrapped_phase_deg(fine_p),
                )
            )

    if not magnitude_abs or not phase_error:
        raise ValueError('null masks removed every convergence sample')

    rms_relative = math.sqrt(squared_diff / max(squared_reference, 1.0e-300))
    return {
        'coarse_order': int(coarse['order']),
        'fine_order': int(fine['order']),
        'magnitude_max_abs_db': max(magnitude_abs),
        'magnitude_max_relative': max(magnitude_rel),
        'phase_max_error_deg': max(phase_error),
        'complex_rms_relative': rms_relative,
        'magnitude_sample_count': len(magnitude_abs),
        'phase_sample_count': len(phase_error),
    }


def _reference_evidence(
    fixture,
    orders: list[dict[str, object]],
) -> tuple[tuple[BakeoffObservableEvidence, ...], list[dict[str, float | int]], dict[str, object]]:
    observable_by_id = {item.observable_id: item for item in fixture.observables}
    magnitude = observable_by_id['lroom-fr']
    phase = observable_by_id['lroom-phase']

    pair_metrics = [
        _pair_metrics(
            orders[index],
            orders[index + 1],
            magnitude_mask_db=float(magnitude.tolerance.null_mask_below_db),
            phase_mask_db=float(phase.tolerance.null_mask_below_db),
        )
        for index in range(len(orders) - 1)
    ]
    rms_sequence = [float(item['complex_rms_relative']) for item in pair_metrics]
    monotonic_rms = all(
        rms_sequence[index] > rms_sequence[index + 1]
        for index in range(len(rms_sequence) - 1)
    )
    final = pair_metrics[-1]

    mag_abs_limit = float(magnitude.tolerance.absolute) * REFERENCE_QUALIFICATION_FRACTION
    mag_rel_limit = float(magnitude.tolerance.relative) * REFERENCE_QUALIFICATION_FRACTION
    phase_limit = float(phase.tolerance.phase_deg) * REFERENCE_QUALIFICATION_FRACTION

    magnitude_violations: list[str] = []
    if not monotonic_rms:
        magnitude_violations.append(
            'complex RMS p-refinement error is not strictly decreasing'
        )
    if float(final['magnitude_max_abs_db']) > mag_abs_limit:
        magnitude_violations.append(
            f'final p-refinement magnitude delta {final["magnitude_max_abs_db"]} dB '
            f'exceeds fixed independent-reference qualification {mag_abs_limit} dB'
        )
    if float(final['magnitude_max_relative']) > mag_rel_limit:
        magnitude_violations.append(
            f'final p-refinement relative magnitude delta {final["magnitude_max_relative"]} '
            f'exceeds fixed independent-reference qualification {mag_rel_limit}'
        )

    phase_violations: list[str] = []
    if not monotonic_rms:
        phase_violations.append(
            'complex RMS p-refinement error is not strictly decreasing'
        )
    if float(final['phase_max_error_deg']) > phase_limit:
        phase_violations.append(
            f'final p-refinement phase delta {final["phase_max_error_deg"]} deg '
            f'exceeds fixed independent-reference qualification {phase_limit} deg'
        )

    magnitude_evidence = BakeoffObservableEvidence(
        observable_id='lroom-fr',
        status='fail' if magnitude_violations else 'pass',
        summary=(
            '; '.join(magnitude_violations)
            if magnitude_violations
            else (
                'Exact-geometry MFEM p-refinement qualified the independent magnitude '
                f'reference at {REFERENCE_QUALIFICATION_FRACTION:g}x the frozen '
                'candidate tolerance.'
            )
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
            else (
                'Exact-geometry MFEM p-refinement qualified the independent phase '
                f'reference at {REFERENCE_QUALIFICATION_FRACTION:g}x the frozen '
                'candidate tolerance.'
            )
        ),
        phase_error_deg=float(final['phase_max_error_deg']),
    )

    # Central R100B tolerance authority still owns pass/fail admissibility.
    for expected, evidence in (
        (magnitude, magnitude_evidence),
        (phase, phase_evidence),
    ):
        if evidence.status == 'pass':
            central_violations = observable_tolerance_violations(expected, evidence)
            if central_violations:
                raise ValueError(
                    'reference qualification passed but central R100B evaluator rejected it: '
                    + '; '.join(central_violations)
                )

    qualification = {
        'qualification_fraction_of_frozen_tolerance': REFERENCE_QUALIFICATION_FRACTION,
        'frozen_candidate_tolerances_unchanged': {
            'magnitude_absolute_db': float(magnitude.tolerance.absolute),
            'magnitude_relative': float(magnitude.tolerance.relative),
            'phase_deg': float(phase.tolerance.phase_deg),
        },
        'reference_qualification_limits': {
            'magnitude_absolute_db': mag_abs_limit,
            'magnitude_relative': mag_rel_limit,
            'phase_deg': phase_limit,
        },
        'complex_rms_relative_strictly_decreasing': monotonic_rms,
        'complex_rms_relative_sequence': rms_sequence,
    }
    return (magnitude_evidence, phase_evidence), pair_metrics, qualification


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
) -> RawFixtureObservation:
    magnitude_samples: list[RawObservationSample] = []
    phase_samples: list[RawObservationSample] = []
    source_amplitude = float(fixture.sources[0].amplitude)

    for item in finest['samples']:
        frequency = float(item['frequency_hz'])
        pressure = _complex(item)
        transfer = pressure / source_amplitude
        magnitude_db = 20.0 * math.log10(max(abs(transfer), 1.0e-300))
        phase_deg = _wrapped_phase_deg(transfer)
        key = _frequency_key(frequency)
        magnitude_samples.append(
            RawObservationSample(
                sample_key=key,
                frequency_hz=frequency,
                scalar_value=magnitude_db,
            )
        )
        phase_samples.append(
            RawObservationSample(
                sample_key=key,
                frequency_hz=frequency,
                scalar_value=phase_deg,
            )
        )

    return RawFixtureObservation(
        fixture_id=fixture.fixture_id,
        evidence_ref=evidence_ref,
        adapter_id=CONCAVE_ADAPTER_ID,
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
                unit='dB',
                samples=tuple(magnitude_samples),
                diagnostics=(
                    'Raw finest-order MFEM pressure is converted to 20*log10(|p/Q|).',
                ),
            ),
            RawObservableObservation(
                observable_id='lroom-phase',
                kind='transfer_phase_deg',
                unit='deg',
                samples=tuple(phase_samples),
                diagnostics=(
                    'Phase is atan2(Im(p/Q), Re(p/Q)) under exp(-i*omega*t).',
                ),
            ),
        ),
        diagnostics=(
            'This is evaluator-ready raw reference evidence. R100A-2 intentionally has no '
            'embedded expected samples for the independent-solver concave fixture, so '
            'evaluate_sampled_fixture is not invoked against a fabricated authority.',
        ),
    )


def _execute(
    benchmark,
    candidates,
    mfem_root: Path,
    executable: Path,
    build_dir: Path,
    work_dir: Path,
    evidence_ref: str,
    native_build_s: float | None,
) -> tuple[dict[str, object], BakeoffRun]:
    candidate = _candidate(candidates)
    concave = _fixture(benchmark, CONCAVE_FIXTURE_ID)
    impedance = _fixture(benchmark, IMPEDANCE_FIXTURE_ID)
    _validate_concave_authority(concave)
    impedance_capability = _validate_impedance_authority(impedance)
    htdt_git = _htdt_git_provenance()

    actual_head = _git_head(mfem_root)
    if actual_head != candidate.source_commit_sha:
        raise RuntimeError(
            f'MFEM checkout mismatch: expected {candidate.source_commit_sha}, got {actual_head}'
        )
    if not executable.is_file():
        raise RuntimeError(f'MFEM concave probe executable is missing: {executable}')

    work_dir.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / 'mfem_concave_transfer_raw.json'

    source = concave.sources[0]
    receiver = concave.receivers[0]
    grid = concave.comparison.frequency_grid
    command = [
        str(executable),
        '--density', str(concave.environment.density_kg_m3),
        '--sound-speed', str(concave.environment.sound_speed_m_s),
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
    stdout, _ = process.communicate()
    peak_ram_mb = monitor.stop()
    if process.returncode != 0:
        raise RuntimeError(
            f'MFEM concave probe executable exited {process.returncode}: {stdout[-6000:]}'
        )
    if not raw_path.is_file():
        raise RuntimeError('MFEM concave probe did not produce raw JSON output')

    raw_payload = json.loads(raw_path.read_text(encoding='utf-8'))
    orders = _validate_raw_orders(concave, raw_payload)
    backend_version = str(raw_payload.get('mfem_version', '4.10'))
    compile_s = sum(float(item['assemble_s']) for item in orders)
    solve_s = sum(float(item['solve_s']) for item in orders)
    disk_mb = _directory_size_mb(work_dir)
    output_mb = raw_path.stat().st_size / (1024.0 * 1024.0)

    post_started = time.perf_counter()
    observable_evidence, convergence, qualification = _reference_evidence(concave, orders)
    provisional_postprocess_s = time.perf_counter() - post_started

    raw_observation = _raw_reference_observation(
        concave,
        orders[-1],
        evidence_ref=evidence_ref,
        backend_version=backend_version,
        compile_s=compile_s,
        solve_s=solve_s,
        postprocess_s=provisional_postprocess_s,
        peak_ram_mb=peak_ram_mb,
        disk_mb=disk_mb,
        output_mb=output_mb,
    )
    postprocess_s = time.perf_counter() - post_started
    raw_observation = raw_observation.model_copy(update={'postprocess_s': postprocess_s})

    concave_status = (
        'pass'
        if all(item.status == 'pass' for item in observable_evidence)
        else 'fail'
    )
    concave_evidence = BakeoffFixtureEvidence(
        fixture_id=concave.fixture_id,
        status=concave_status,
        evidence_ref=evidence_ref,
        adapter_id=CONCAVE_ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        backend_version=backend_version,
        precision='float64',
        compile_s=compile_s,
        solve_s=solve_s,
        postprocess_s=postprocess_s,
        peak_ram_mb=peak_ram_mb,
        disk_mb=disk_mb,
        output_mb=output_mb,
        observables=observable_evidence,
        diagnostics=(
            'Exact frozen L-prism geometry represented by five conforming hexahedra; '
            'the concave notch is absent from the mesh rather than filled.',
            'p_refinement_orders=' + ','.join(
                str(order) for order in range(ORDER_MIN, ORDER_MAX + 1)
            ),
            'Natural H1 boundary condition is rigid Neumann on every exterior face.',
            'No candidate trace participates in MFEM reference qualification.',
        ),
    )

    impedance_evidence = _blocked_fixture(
        impedance.fixture_id,
        adapter_id=IMPEDANCE_ADAPTER_ID,
        backend_version=backend_version,
        reason=str(impedance_capability['block_reason']),
    )

    run = BakeoffRun(
        run_id=f'mfem-concave-reference-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(concave.resource_budget.cpu_thread_budget),
        fixture_evidence=(concave_evidence, impedance_evidence),
        hard_gates=_hard_gates(evidence_ref, reproducible=True),
        notes=(
            'Independent MFEM numerical reference evidence; not a production solver selection.',
            'PFFDTD PR #151 evidence is intentionally not consumed by this numerical solve.',
            'Impedance Robin representation capability is distinct from the blocked '
            'frozen-fixture reflection-coefficient extraction authority.',
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)

    cache_path = build_dir / 'CMakeCache.txt'
    source_path = Path(__file__).resolve()
    executable_size_mb = executable.stat().st_size / (1024.0 * 1024.0)
    details = {
        'mfem_source_commit_sha': actual_head,
        'htdt_source_commit_sha': htdt_git['pr_head_commit_sha'],
        'htdt_checkout_commit_sha': htdt_git['checkout_commit_sha'],
        'htdt_pr_head_commit_sha': htdt_git['pr_head_commit_sha'],
        'native_build_s': native_build_s,
        'mfem_build_configuration': {
            'generator': 'Visual Studio x64 from dedicated GitHub Actions workflow',
            'build_type': 'Release',
            'target': 'r100b_mfem_concave_transfer',
            'MFEM_USE_MPI': False,
            'MFEM_USE_METIS': False,
            'MFEM_USE_LAPACK': False,
            'MFEM_USE_OPENMP': False,
            'MFEM_USE_ZLIB': False,
            'MFEM_USE_EXCEPTIONS': True,
            'BUILD_SHARED_LIBS': False,
            'cmake_cache_sha256': _sha256_file(cache_path) if cache_path.is_file() else None,
        },
        'dependency_versions': _runtime_versions(),
        'probe_source_sha256': _sha256_file(source_path),
        'probe_executable_sha256': _sha256_file(executable),
        'probe_executable_mb': executable_size_mb,
        'work_dir_mb': disk_mb,
        'authority': {
            'concave_fixture': concave.model_dump(mode='json'),
            'impedance_fixture': impedance.model_dump(mode='json'),
            'impedance_capability': impedance_capability,
        },
        'discretization': {
            'family': 'conforming H1 finite elements',
            'geometry_mesh': '5 exact axis-aligned hexahedra tiling only the frozen L-prism volume',
            'mesh_elements_per_level': [int(item['elements']) for item in orders],
            'polynomial_orders': [int(item['order']) for item in orders],
            'true_dofs': [int(item['ndofs']) for item in orders],
            'refinement_kind': 'p-refinement on fixed exact geometry',
            'linear_operator': 'K-k^2 M',
            'preconditioner': 'diagonal Jacobi applied to positive K+k^2 M',
            'iterative_solver': 'MFEM serial MINRES',
            'relative_tolerance': 1.0e-10,
            'qualification_residual_limit': SOLVER_RESIDUAL_LIMIT,
            'max_iterations': 8000,
        },
        'convergence': convergence,
        'qualification': qualification,
        'raw_solver_output': raw_payload,
        'raw_reference_observation': raw_observation.model_dump(mode='json'),
        'resource_evidence': {
            'native_build_s': native_build_s,
            'fem_assembly_s': compile_s,
            'solve_s': solve_s,
            'postprocess_s': postprocess_s,
            'peak_ram_mb': peak_ram_mb,
            'work_disk_mb': disk_mb,
            'raw_output_mb': output_mb,
            'probe_executable_mb': executable_size_mb,
        },
        'stdout_tail': stdout[-6000:],
    }
    return details, run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run R100B MFEM concave independent numerical-reference probe'
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
    evidence_ref = f'artifact:{args.output.as_posix()}'

    concave = _fixture(benchmark, CONCAVE_FIXTURE_ID)
    impedance = _fixture(benchmark, IMPEDANCE_FIXTURE_ID)
    _validate_concave_authority(concave)
    impedance_capability = _validate_impedance_authority(impedance)
    impedance_reason = str(impedance_capability['block_reason'])

    if args.blocked_reason:
        run = _blocked_run(
            benchmark,
            candidates,
            evidence_ref,
            args.blocked_reason,
            impedance_reason,
        )
        payload = {
            'schema_version': ARTIFACT_SCHEMA,
            'concave_reference_outcome': 'blocked',
            'impedance_reference_outcome': 'blocked',
            'workflow_result_is_reference_result': False,
            'r100a_manifest_id': benchmark.manifest_id,
            'r100a_semantic_hash': benchmark.semantic_hash(),
            'candidate_manifest_hash': candidates.semantic_hash(),
            'details': {
                'error': args.blocked_reason,
                'impedance_capability': impedance_capability,
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
                args.mfem_root,
                args.executable,
                args.build_dir,
                args.work_dir,
                evidence_ref,
                args.build_s,
            )
            concave_outcome = next(
                item.status
                for item in run.fixture_evidence
                if item.fixture_id == CONCAVE_FIXTURE_ID
            )
            impedance_outcome = next(
                item.status
                for item in run.fixture_evidence
                if item.fixture_id == IMPEDANCE_FIXTURE_ID
            )
            payload = {
                'schema_version': ARTIFACT_SCHEMA,
                'concave_reference_outcome': concave_outcome,
                'impedance_reference_outcome': impedance_outcome,
                'workflow_result_is_reference_result': False,
                'r100a_manifest_id': benchmark.manifest_id,
                'r100a_semantic_hash': benchmark.semantic_hash(),
                'candidate_manifest_hash': candidates.semantic_hash(),
                'details': details,
                'bakeoff_run': run.model_dump(mode='json'),
            }
        except Exception as exc:
            reason = f'{type(exc).__name__}: {exc}'
            run = _blocked_run(
                benchmark,
                candidates,
                evidence_ref,
                reason,
                impedance_reason,
            )
            payload = {
                'schema_version': ARTIFACT_SCHEMA,
                'concave_reference_outcome': 'blocked',
                'impedance_reference_outcome': 'blocked',
                'workflow_result_is_reference_result': False,
                'r100a_manifest_id': benchmark.manifest_id,
                'r100a_semantic_hash': benchmark.semantic_hash(),
                'candidate_manifest_hash': candidates.semantic_hash(),
                'details': {
                    'error': reason,
                    'impedance_capability': impedance_capability,
                },
                'bakeoff_run': run.model_dump(mode='json'),
            }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )

    summary = {
        'concave_reference_outcome': payload['concave_reference_outcome'],
        'impedance_reference_outcome': payload['impedance_reference_outcome'],
        'output': str(args.output),
    }
    details = payload.get('details')
    if isinstance(details, dict) and isinstance(details.get('convergence'), list):
        summary['convergence'] = details['convergence']
        summary['qualification'] = details.get('qualification')
        summary['resource_evidence'] = details.get('resource_evidence')
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
