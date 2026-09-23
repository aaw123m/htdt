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
from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest


CANDIDATE_ID = 'mfem-v4.10-d964264'
PORTAL_FIXTURE_ID = 'wave-portal-split-room-v1'
PEER_FIXTURE_ID = 'wave-rectangular-convergence-v1'
ADAPTER_ID = 'htdt-r100b-mfem-portal-continuity'
ADAPTER_VERSION = '1'
ARTIFACT_SCHEMA = 'r100b-mfem-portal-continuity-artifact-1'
ORDER = 3
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
            'GitHub-hosted Windows MFEM v4.10 serial H1 Portal-continuity cross-fixture probe.'
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
                        'Pinned MFEM source commit, exact R100A-2/candidate hashes, Portal and '
                        'peer fixture authority, conforming topology, raw transfer samples and '
                        'resource/source provenance are recorded.'
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
                        'MFEM Portal-continuity cross-fixture semantics.'
                    ),
                )
            )
    return tuple(result)


def _expected_box_vertices(
    *,
    prefix: str,
    xmin: float,
    xmax: float,
) -> dict[str, tuple[float, float, float]]:
    return {
        f'{prefix}-v000': (xmin, 0.0, 0.0),
        f'{prefix}-v100': (xmax, 0.0, 0.0),
        f'{prefix}-v110': (xmax, 4.0, 0.0),
        f'{prefix}-v010': (xmin, 4.0, 0.0),
        f'{prefix}-v001': (xmin, 0.0, 2.5),
        f'{prefix}-v101': (xmax, 0.0, 2.5),
        f'{prefix}-v111': (xmax, 4.0, 2.5),
        f'{prefix}-v011': (xmin, 4.0, 2.5),
    }


def _validate_common_authority(portal, peer) -> None:
    if portal.fixture_id != PORTAL_FIXTURE_ID or peer.fixture_id != PEER_FIXTURE_ID:
        raise ValueError('wrong Portal/peer fixture authority')
    if tuple(portal.required_capabilities) != ('wave_rigid', 'portal_continuity'):
        raise ValueError('Portal required capability authority changed')
    if tuple(peer.required_capabilities) != ('wave_rigid',):
        raise ValueError('peer required capability authority changed')
    if portal.obstacles or portal.terminations or peer.obstacles or peer.portals or peer.terminations:
        raise ValueError('Portal/peer topology authority changed')

    if len(portal.regions) != 2 or {item.region_id for item in portal.regions} != {'left', 'right'}:
        raise ValueError('Portal fixture must contain frozen left/right regions')
    if len(peer.regions) != 1 or peer.regions[0].region_id != 'room':
        raise ValueError('peer fixture must contain frozen room region')

    portal_regions = {item.region_id: item for item in portal.regions}
    actual_left = {
        vertex.vertex_id: _position_tuple(vertex.position)
        for vertex in portal_regions['left'].vertices
    }
    actual_right = {
        vertex.vertex_id: _position_tuple(vertex.position)
        for vertex in portal_regions['right'].vertices
    }
    if actual_left != _expected_box_vertices(prefix='left', xmin=0.0, xmax=3.0):
        raise ValueError('Portal left-region geometry changed')
    if actual_right != _expected_box_vertices(prefix='right', xmin=3.0, xmax=6.0):
        raise ValueError('Portal right-region geometry changed')

    actual_peer = {
        vertex.vertex_id: _position_tuple(vertex.position)
        for vertex in peer.regions[0].vertices
    }
    if actual_peer != _expected_box_vertices(prefix='room', xmin=0.0, xmax=6.0):
        raise ValueError('peer room geometry changed')

    if len(portal.portals) != 1:
        raise ValueError('Portal fixture must contain exactly one Portal')
    aperture = portal.portals[0]
    expected_aperture = (
        (3.0, 0.0, 0.0),
        (3.0, 4.0, 0.0),
        (3.0, 4.0, 2.5),
        (3.0, 0.0, 2.5),
    )
    actual_aperture = tuple(_position_tuple(item) for item in aperture.aperture)
    if (
        aperture.portal_id != 'portal-mid'
        or aperture.region_a_id != 'left'
        or aperture.region_b_id != 'right'
        or aperture.continuity_model != 'pressure_velocity_continuity'
        or actual_aperture != expected_aperture
    ):
        raise ValueError('Portal aperture/continuity authority changed')

    boundary_by_id = {item.boundary_id: item.material_id for item in portal.boundaries}
    if boundary_by_id != {
        'b-rigid': 'rigid',
        'b-interface': 'portal-interface-placeholder',
    }:
        raise ValueError('Portal boundary mapping changed')
    material_by_id = {item.material_id: item for item in portal.materials}
    if material_by_id['rigid'].wave_model != 'rigid':
        raise ValueError('Portal rigid material authority changed')
    if material_by_id['portal-interface-placeholder'].wave_model != 'unsupported':
        raise ValueError('Portal interface placeholder must remain non-material authority')
    interface_faces = [
        face
        for region in portal.regions
        for face in region.faces
        if face.boundary_id == 'b-interface'
    ]
    if {item.face_id for item in interface_faces} != {'left-xmax', 'right-xmin'}:
        raise ValueError('Portal interface face authority changed')

    for fixture in (portal, peer):
        if len(fixture.sources) != 1 or len(fixture.receivers) != 1:
            raise ValueError('Portal/peer requires one source and receiver')
        source = fixture.sources[0]
        receiver = fixture.receivers[0]
        if (
            source.source_id != 'src'
            or _position_tuple(source.position) != (1.0, 1.0, 1.0)
            or source.normalization != 'volume_velocity_m3_s'
            or float(source.amplitude) != 1.0
            or float(source.phase_deg) != 0.0
            or source.directivity != 'omnidirectional'
        ):
            raise ValueError(f'{fixture.fixture_id} source authority changed')
        if (
            receiver.receiver_id != 'rx'
            or _position_tuple(receiver.position) != (5.0, 3.0, 1.0)
            or receiver.calibration_state != 'ideal_flat'
            or receiver.timing_reference != 'source_t0'
        ):
            raise ValueError(f'{fixture.fixture_id} receiver authority changed')
        environment = fixture.environment
        if (
            float(environment.density_kg_m3) != 1.2
            or float(environment.sound_speed_m_s) != 343.0
        ):
            raise ValueError(f'{fixture.fixture_id} environment authority changed')
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
            raise ValueError(f'{fixture.fixture_id} comparison authority changed')

    portal_comparison = portal.comparison
    if (
        portal_comparison.time_step_s != (1.0 / 6000.0)
        or portal_comparison.observation_time_s != 2.0
        or portal_comparison.finite_record_transfer is not None
    ):
        raise ValueError('Portal time-domain comparison authority changed')

    peer_comparison = peer.comparison
    peer_transfer = peer_comparison.finite_record_transfer
    expected_peer_transfer = {
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
    if (
        peer_comparison.time_step_s is not None
        or peer_comparison.observation_time_s != 2.0
        or peer_transfer is None
        or peer_transfer.model_dump(mode='json') != expected_peer_transfer
    ):
        raise ValueError('peer finite-record comparison authority changed')

    if portal.sources[0].region_id != 'left' or portal.receivers[0].region_id != 'right':
        raise ValueError('Portal source/receiver region binding changed')
    if peer.sources[0].region_id != 'room' or peer.receivers[0].region_id != 'room':
        raise ValueError('peer source/receiver region binding changed')

    observable_by_id = {item.observable_id: item for item in portal.observables}
    if set(observable_by_id) != {'portal-fr-invariance', 'portal-phase-invariance'}:
        raise ValueError('Portal observable authority changed')
    if (
        observable_by_id['portal-fr-invariance'].kind != 'transfer_magnitude_db'
        or observable_by_id['portal-fr-invariance'].reference_kind != 'cross_fixture'
        or observable_by_id['portal-fr-invariance'].peer_fixture_id != PEER_FIXTURE_ID
    ):
        raise ValueError('Portal magnitude observable authority changed')
    if (
        observable_by_id['portal-phase-invariance'].kind != 'transfer_phase_deg'
        or observable_by_id['portal-phase-invariance'].reference_kind != 'cross_fixture'
        or observable_by_id['portal-phase-invariance'].peer_fixture_id != PEER_FIXTURE_ID
    ):
        raise ValueError('Portal phase observable authority changed')


def _complex(sample: dict[str, object]) -> complex:
    return complex(
        float(sample['pressure_real_pa']),
        float(sample['pressure_imag_pa']),
    )


def _wrapped_phase_deg(value: complex) -> float:
    return math.degrees(math.atan2(value.imag, value.real))


def _wrapped_phase_delta_deg(first: float, second: float) -> float:
    return abs((first - second + 180.0) % 360.0 - 180.0)


def _expected_frequency_grid(fixture) -> list[float]:
    grid = fixture.comparison.frequency_grid
    count = int(round((float(grid.stop_hz) - float(grid.start_hz)) / float(grid.step_hz))) + 1
    return [float(grid.start_hz) + index * float(grid.step_hz) for index in range(count)]


def _validate_case(
    case: dict[str, object],
    *,
    case_id: str,
    expected_attributes: list[int],
    expected_grid: list[float],
) -> None:
    if case.get('case_id') != case_id:
        raise ValueError(f'raw MFEM Portal output missing case {case_id}')
    if int(case.get('order', 0)) != ORDER:
        raise ValueError('MFEM Portal polynomial order changed')
    if int(case.get('elements', 0)) != 2:
        raise ValueError('MFEM Portal/peer compiled representation must contain exactly two hexes')
    if int(case.get('boundary_faces', -1)) != 10:
        raise ValueError('MFEM Portal/peer compiled representation must have 10 exterior faces')
    if int(case.get('total_faces', -1)) != 11:
        raise ValueError('MFEM Portal/peer compiled representation must have 11 total faces')
    if int(case.get('internal_faces', -1)) != 1:
        raise ValueError('MFEM Portal/peer compiled representation must have one shared internal face')
    if [int(item) for item in case.get('element_attributes', [])] != expected_attributes:
        raise ValueError(f'MFEM {case_id} region attributes changed')

    samples = case.get('samples')
    if not isinstance(samples, list) or len(samples) != len(expected_grid):
        raise ValueError(f'MFEM {case_id} did not return the complete frozen frequency grid')
    for item, expected_frequency in zip(samples, expected_grid, strict=True):
        actual_frequency = float(item['frequency_hz'])
        if abs(actual_frequency - expected_frequency) > max(
            1.0e-9, abs(expected_frequency) * 1.0e-12
        ):
            raise ValueError(
                f'MFEM {case_id} frequency mismatch: {actual_frequency} != {expected_frequency}'
            )
        residual = float(item['relative_residual'])
        if residual > SOLVER_RESIDUAL_LIMIT:
            raise ValueError(
                f'MFEM {case_id} residual {residual} exceeds fixed '
                f'{SOLVER_RESIDUAL_LIMIT} qualification limit'
            )


def _validate_raw(portal_fixture, raw: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    if raw.get('schema_version') != 'r100b-mfem-portal-transfer-raw-1':
        raise ValueError('unexpected MFEM Portal raw schema')
    if raw.get('portal_interface_boundary_condition') != 'none-internal-shared-face':
        raise ValueError('MFEM Portal compiler introduced an artificial interface boundary')
    if raw.get('exterior_boundary_model') != 'natural-neumann-rigid':
        raise ValueError('MFEM Portal exterior boundary semantics changed')
    if raw.get('fourier_sign') != portal_fixture.comparison.fourier_sign:
        raise ValueError('MFEM Portal Fourier convention changed')
    if float(raw.get('density_kg_m3', -1.0)) != float(portal_fixture.environment.density_kg_m3):
        raise ValueError('MFEM Portal density changed')
    if float(raw.get('sound_speed_m_s', -1.0)) != float(portal_fixture.environment.sound_speed_m_s):
        raise ValueError('MFEM Portal sound speed changed')

    cases = raw.get('cases')
    if not isinstance(cases, list) or len(cases) != 2:
        raise ValueError('MFEM Portal raw output must contain peer and Portal cases')
    case_by_id = {str(item.get('case_id')): item for item in cases}
    if set(case_by_id) != {'peer-one-region', 'portal-two-region'}:
        raise ValueError('MFEM Portal raw case identities changed')

    expected_grid = _expected_frequency_grid(portal_fixture)
    peer = case_by_id['peer-one-region']
    portal = case_by_id['portal-two-region']
    _validate_case(
        peer,
        case_id='peer-one-region',
        expected_attributes=[1],
        expected_grid=expected_grid,
    )
    _validate_case(
        portal,
        case_id='portal-two-region',
        expected_attributes=[1, 2],
        expected_grid=expected_grid,
    )
    if int(peer['ndofs']) != int(portal['ndofs']):
        raise ValueError('peer and Portal discretizations have different DOF counts')
    return peer, portal


def _evaluate_cross_fixture(
    portal_fixture,
    peer_case: dict[str, object],
    portal_case: dict[str, object],
) -> tuple[tuple[BakeoffObservableEvidence, ...], dict[str, object]]:
    expected_by_id = {item.observable_id: item for item in portal_fixture.observables}
    magnitude_expected = expected_by_id['portal-fr-invariance']
    phase_expected = expected_by_id['portal-phase-invariance']

    magnitude_abs: list[float] = []
    magnitude_rel: list[float] = []
    phase_errors: list[float] = []
    complex_diff_sq = 0.0
    peer_sq = 0.0

    for peer_sample, portal_sample in zip(
        peer_case['samples'], portal_case['samples'], strict=True
    ):
        peer_pressure = _complex(peer_sample)
        portal_pressure = _complex(portal_sample)
        peer_magnitude = abs(peer_pressure)
        portal_magnitude = abs(portal_pressure)
        peer_db = 20.0 * math.log10(max(peer_magnitude, 1.0e-300))
        portal_db = 20.0 * math.log10(max(portal_magnitude, 1.0e-300))

        difference = portal_pressure - peer_pressure
        complex_diff_sq += difference.real * difference.real + difference.imag * difference.imag
        peer_sq += peer_pressure.real * peer_pressure.real + peer_pressure.imag * peer_pressure.imag

        if peer_db >= float(magnitude_expected.tolerance.null_mask_below_db):
            magnitude_abs.append(abs(portal_db - peer_db))
            magnitude_rel.append(
                abs(portal_magnitude - peer_magnitude) / max(peer_magnitude, 1.0e-300)
            )
        if (
            peer_db >= float(phase_expected.tolerance.null_mask_below_db)
            and peer_magnitude > 0.0
            and portal_magnitude > 0.0
        ):
            phase_errors.append(
                _wrapped_phase_delta_deg(
                    _wrapped_phase_deg(portal_pressure),
                    _wrapped_phase_deg(peer_pressure),
                )
            )

    if not magnitude_abs or not phase_errors:
        raise ValueError('Portal null masks removed all cross-fixture comparison samples')

    magnitude_metrics = {
        'absolute_error': max(magnitude_abs),
        'relative_error': max(magnitude_rel),
    }
    phase_metric = max(phase_errors)

    magnitude_probe = BakeoffObservableEvidence(
        observable_id='portal-fr-invariance',
        status='pass',
        summary='MFEM conforming Portal/peer cross-fixture transfer magnitude comparison.',
        absolute_error=magnitude_metrics['absolute_error'],
        relative_error=magnitude_metrics['relative_error'],
    )
    phase_probe = BakeoffObservableEvidence(
        observable_id='portal-phase-invariance',
        status='pass',
        summary='MFEM conforming Portal/peer cross-fixture transfer phase comparison.',
        phase_error_deg=phase_metric,
    )

    magnitude_violations = observable_tolerance_violations(
        magnitude_expected, magnitude_probe
    )
    phase_violations = observable_tolerance_violations(phase_expected, phase_probe)

    magnitude_evidence = magnitude_probe.model_copy(
        update={
            'status': 'fail' if magnitude_violations else 'pass',
            'summary': (
                '; '.join(magnitude_violations)
                if magnitude_violations
                else (
                    'Portal semantic partition preserves MFEM transfer magnitude under the '
                    'frozen R100A-2 cross-fixture tolerance.'
                )
            ),
        }
    )
    phase_evidence = phase_probe.model_copy(
        update={
            'status': 'fail' if phase_violations else 'pass',
            'summary': (
                '; '.join(phase_violations)
                if phase_violations
                else (
                    'Portal semantic partition preserves MFEM transfer phase under the '
                    'frozen R100A-2 cross-fixture tolerance.'
                )
            ),
        }
    )

    metrics = {
        'magnitude_max_abs_db': magnitude_metrics['absolute_error'],
        'magnitude_max_relative': magnitude_metrics['relative_error'],
        'phase_max_error_deg': phase_metric,
        'complex_rms_relative': math.sqrt(
            complex_diff_sq / max(peer_sq, 1.0e-300)
        ),
        'magnitude_compared_sample_count': len(magnitude_abs),
        'phase_compared_sample_count': len(phase_errors),
    }
    return (magnitude_evidence, phase_evidence), metrics


def _blocked_fixture(
    *,
    backend_version: str,
    reason: str,
) -> BakeoffFixtureEvidence:
    return BakeoffFixtureEvidence(
        fixture_id=PORTAL_FIXTURE_ID,
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
    portal_fixture = _fixture(benchmark, PORTAL_FIXTURE_ID)
    peer_fixture = _fixture(benchmark, PEER_FIXTURE_ID)
    _validate_common_authority(portal_fixture, peer_fixture)

    if 'portal_continuity' not in candidate.probe_capabilities:
        raise RuntimeError('MFEM candidate manifest does not authorize portal_continuity probe')

    actual_head = _git_head(mfem_root)
    if actual_head != candidate.source_commit_sha:
        raise RuntimeError(
            f'MFEM checkout mismatch: expected {candidate.source_commit_sha}, got {actual_head}'
        )
    if not executable.is_file():
        raise RuntimeError(f'MFEM Portal probe executable is missing: {executable}')

    work_dir.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / 'mfem_portal_transfer_raw.json'
    source = portal_fixture.sources[0]
    receiver = portal_fixture.receivers[0]
    grid = portal_fixture.comparison.frequency_grid

    command = [
        str(executable),
        '--density', str(portal_fixture.environment.density_kg_m3),
        '--sound-speed', str(portal_fixture.environment.sound_speed_m_s),
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
        '--order', str(ORDER),
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
            f'MFEM Portal probe executable exited {process.returncode}: {stdout[-6000:]}'
        )
    if not raw_path.is_file():
        raise RuntimeError('MFEM Portal probe did not produce raw JSON output')

    raw = json.loads(raw_path.read_text(encoding='utf-8'))
    peer_case, portal_case = _validate_raw(portal_fixture, raw)
    backend_version = str(raw.get('mfem_version', '4.10'))
    compile_s = float(peer_case['assemble_s']) + float(portal_case['assemble_s'])
    solve_s = float(peer_case['solve_s']) + float(portal_case['solve_s'])
    disk_mb = _directory_size_mb(work_dir)
    output_mb = raw_path.stat().st_size / (1024.0 * 1024.0)

    post_started = time.perf_counter()
    observables, metrics = _evaluate_cross_fixture(
        portal_fixture, peer_case, portal_case
    )
    postprocess_s = time.perf_counter() - post_started
    status = 'pass' if all(item.status == 'pass' for item in observables) else 'fail'

    fixture_evidence = BakeoffFixtureEvidence(
        fixture_id=portal_fixture.fixture_id,
        status=status,
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
        observables=observables,
        diagnostics=(
            'Peer and Portal use the same two-hexahedron conforming mesh partition at x=3m.',
            'Portal case uses element attributes [1,2]; peer uses [1].',
            'The shared x=3m face is an internal mesh face with no boundary condition.',
            'H1 continuity supplies pressure continuity; weak-form flux continuity is unchanged.',
        ),
    )

    run = BakeoffRun(
        run_id=f'mfem-portal-continuity-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(portal_fixture.resource_budget.cpu_thread_budget),
        fixture_evidence=(fixture_evidence,),
        hard_gates=_hard_gates(evidence_ref, reproducible=True),
        notes=(
            'Portal continuity is compiled as a conforming shared internal face, not a material boundary.',
            'Frozen 2 s / 1/6000 s timing authority is validated; the MFEM comparison itself is the authorized steady-state 20–300 Hz complex transfer grid.',
            'Probe capability authorizes execution only; fixture PASS/FAIL is separate evidence.',
            'This slice does not select a production solver or satisfy candidate-wide hard gates.',
        ),
    )
    validate_bakeoff_run(benchmark, candidates, run)

    root = Path(__file__).resolve().parents[1]
    cmake_cache = build_dir / 'CMakeCache.txt'
    details = {
        'raw': raw,
        'cross_fixture_metrics': metrics,
        'resource_evidence': {
            'native_build_s': native_build_s,
            'fem_assembly_s': compile_s,
            'solve_s': solve_s,
            'postprocess_s': postprocess_s,
            'peak_ram_mb': peak_ram_mb,
            'work_disk_mb': disk_mb,
            'raw_output_mb': output_mb,
            'probe_executable_mb': executable.stat().st_size / (1024.0 * 1024.0),
        },
        'topology_evidence': {
            'peer_elements': int(peer_case['elements']),
            'portal_elements': int(portal_case['elements']),
            'peer_element_attributes': peer_case['element_attributes'],
            'portal_element_attributes': portal_case['element_attributes'],
            'boundary_faces': int(portal_case['boundary_faces']),
            'total_faces': int(portal_case['total_faces']),
            'internal_faces': int(portal_case['internal_faces']),
            'portal_interface_boundary_condition': raw['portal_interface_boundary_condition'],
            'peer_and_portal_ndofs_equal': int(peer_case['ndofs']) == int(portal_case['ndofs']),
        },
        'source_provenance': {
            **_htdt_git_provenance(),
            'mfem_source_commit_sha': actual_head,
            'probe_source_sha256': _sha256_file(
                root / 'benchmarks' / 'acoustics' / 'mfem_probe' / 'portal_transfer.cpp'
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
    portal_fixture = _fixture(benchmark, PORTAL_FIXTURE_ID)
    run = BakeoffRun(
        run_id=f'mfem-portal-continuity-blocked-{os.environ.get("GITHUB_RUN_ID", "manual")}',
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_source_commit_sha=candidate.source_commit_sha,
        platform=_platform(portal_fixture.resource_budget.cpu_thread_budget),
        fixture_evidence=(
            _blocked_fixture(backend_version='unavailable', reason=reason),
        ),
        hard_gates=_hard_gates(evidence_ref, reproducible=False),
        notes=(
            'Workflow completion and Portal fixture result are separate authorities.',
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
        'probe': 'mfem-portal-continuity-cross-fixture',
        'workflow_result_is_fixture_result': False,
        'portal_fixture_outcome': run.fixture_evidence[0].status,
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
        description='Run the R100B MFEM Portal continuity cross-fixture gate'
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
    portal_fixture = _fixture(benchmark, PORTAL_FIXTURE_ID)
    peer_fixture = _fixture(benchmark, PEER_FIXTURE_ID)
    _validate_common_authority(portal_fixture, peer_fixture)
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
                'portal_fixture_outcome': run.fixture_evidence[0].status,
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
