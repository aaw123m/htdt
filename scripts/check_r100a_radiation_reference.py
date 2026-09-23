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

from htdt.acoustic_benchmark import (
    canonical_benchmark_json,
    load_acoustic_benchmark_manifest,
)


FIXTURE_ID = 'wave-explicit-radiation-termination-v1'
MAGNITUDE_OBSERVABLE_ID = 'termination-fr'
COMPLEX_OBSERVABLE_ID = 'termination-complex-pressure'
COARSE_TRUNCATION = 8
REFERENCE_TRUNCATION = 12
RMS_RELATIVE_LIMIT = 1.0e-9
MAX_POINT_RELATIVE_LIMIT = 1.0e-8
REFERENCE_RELATIVE_TOLERANCE = 1.0e-12
REFERENCE_ABSOLUTE_TOLERANCE = 1.0e-12
ARTIFACT_SCHEMA = 'r100a3-radiation-reference-artifact-1'


class ProcessPeakRssMonitor:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self.peak_bytes = 0
        try:
            import psutil
        except ImportError:
            self._psutil = None
            self._process = None
        else:
            self._psutil = psutil
            self._process = psutil.Process(os.getpid())

    def _sample_once(self) -> bool:
        if self._psutil is None or self._process is None:
            return False
        try:
            self.peak_bytes = max(
                self.peak_bytes,
                self._process.memory_info().rss,
            )
        except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
            return False
        return True

    def _sample(self) -> None:
        if not self._sample_once():
            return
        while not self._stop.wait(0.005):
            if not self._sample_once():
                return

    def start(self) -> None:
        self._sample_once()
        self._thread.start()

    def stop(self) -> float | None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        if self.peak_bytes <= 0:
            return None
        return self.peak_bytes / (1024.0 * 1024.0)


def _fixture(manifest):
    return next(item for item in manifest.fixtures if item.fixture_id == FIXTURE_ID)


def _position(position) -> tuple[float, float, float]:
    return (float(position.x_m), float(position.y_m), float(position.z_m))


def _mode_value(index: int, coordinate: float, length: float) -> float:
    if index == 0:
        return 1.0 / math.sqrt(length)
    return math.sqrt(2.0 / length) * math.cos(index * math.pi * coordinate / length)


def _axial_green(
    *,
    k: float,
    transverse_wavenumber_sq: float,
    x_m: float,
    source_x_m: float,
    length_x_m: float,
) -> complex:
    q = cmath.sqrt(complex(k * k - transverse_wavenumber_sq, 0.0))
    x_min = min(x_m, source_x_m)
    x_max = max(x_m, source_x_m)

    if abs(q) < 1.0e-10:
        u = complex(1.0, 0.0)
        v = complex(1.0, -k * (length_x_m - x_max))
        wronskian = complex(0.0, k)
    else:
        u = cmath.cos(q * x_min)
        distance_from_termination = length_x_m - x_max
        v = (
            cmath.cos(q * distance_from_termination)
            - 1j * (k / q) * cmath.sin(q * distance_from_termination)
        )
        wronskian = (
            q * cmath.sin(q * length_x_m)
            + 1j * k * cmath.cos(q * length_x_m)
        )
    return u * v / wronskian


def _pressure(fixture, frequency_hz: float, truncation: int) -> complex:
    source = fixture.sources[0]
    receiver = fixture.receivers[0]
    rho = float(fixture.environment.density_kg_m3)
    sound_speed = float(fixture.environment.sound_speed_m_s)
    omega = 2.0 * math.pi * frequency_hz
    k = omega / sound_speed

    length_x = 6.0
    length_y = 4.0
    length_z = 2.5
    source_x, source_y, source_z = _position(source.position)
    receiver_x, receiver_y, receiver_z = _position(receiver.position)

    modal_sum = complex(0.0, 0.0)
    for m in range(truncation + 1):
        y_weight = (
            _mode_value(m, source_y, length_y)
            * _mode_value(m, receiver_y, length_y)
        )
        if abs(y_weight) < 1.0e-16:
            continue
        alpha_sq = (m * math.pi / length_y) ** 2

        for n in range(truncation + 1):
            z_weight = (
                _mode_value(n, source_z, length_z)
                * _mode_value(n, receiver_z, length_z)
            )
            if abs(z_weight) < 1.0e-16:
                continue
            transverse_sq = alpha_sq + (n * math.pi / length_z) ** 2
            modal_sum += (
                y_weight
                * z_weight
                * _axial_green(
                    k=k,
                    transverse_wavenumber_sq=transverse_sq,
                    x_m=receiver_x,
                    source_x_m=source_x,
                    length_x_m=length_x,
                )
            )

    return (
        1j
        * omega
        * rho
        * float(source.amplitude)
        * modal_sum
    )


def _frequency_grid(fixture) -> tuple[float, ...]:
    grid = fixture.comparison.frequency_grid
    if (
        grid.kind != 'uniform'
        or float(grid.start_hz) != 20.0
        or float(grid.stop_hz) != 300.0
        or float(grid.step_hz) != 1.0
    ):
        raise ValueError('radiation reference requires the frozen 20-300 Hz / 1 Hz grid')
    count = int(round((float(grid.stop_hz) - float(grid.start_hz)) / float(grid.step_hz))) + 1
    return tuple(float(grid.start_hz) + index * float(grid.step_hz) for index in range(count))


def _validate_authority(manifest, fixture) -> None:
    expected_revision = {'r100a-3': 3, 'r100a-4': 4}.get(manifest.schema_version)
    if expected_revision is None or manifest.revision != expected_revision:
        raise ValueError('radiation reference requires R100A-3/4 with matching revision')
    if tuple(fixture.required_capabilities) != (
        'wave_rigid',
        'wave_radiation_termination',
    ):
        raise ValueError('radiation fixture capability authority changed')
    if len(fixture.regions) != 1 or fixture.regions[0].region_id != 'room':
        raise ValueError('radiation fixture region authority changed')
    region = fixture.regions[0]
    expected_vertices = {
        'room-v000': (0.0, 0.0, 0.0),
        'room-v100': (6.0, 0.0, 0.0),
        'room-v110': (6.0, 4.0, 0.0),
        'room-v010': (0.0, 4.0, 0.0),
        'room-v001': (0.0, 0.0, 2.5),
        'room-v101': (6.0, 0.0, 2.5),
        'room-v111': (6.0, 4.0, 2.5),
        'room-v011': (0.0, 4.0, 2.5),
    }
    actual_vertices = {
        item.vertex_id: _position(item.position) for item in region.vertices
    }
    if actual_vertices != expected_vertices:
        raise ValueError('radiation fixture room geometry authority changed')

    expected_face_boundaries = {
        'room-zmin': 'b-rigid',
        'room-zmax': 'b-rigid',
        'room-ymin': 'b-rigid',
        'room-ymax': 'b-rigid',
        'room-xmin': 'b-rigid',
        'room-xmax': 'b-interface',
    }
    actual_face_boundaries = {
        item.face_id: item.boundary_id for item in region.faces
    }
    if actual_face_boundaries != expected_face_boundaries:
        raise ValueError('radiation fixture face/boundary authority changed')

    material_by_id = {item.material_id: item for item in fixture.materials}
    boundary_by_id = {item.boundary_id: item for item in fixture.boundaries}
    rigid_binding = boundary_by_id.get('b-rigid')
    interface_binding = boundary_by_id.get('b-interface')
    if (
        rigid_binding is None
        or interface_binding is None
        or material_by_id[rigid_binding.material_id].wave_model != 'rigid'
        or material_by_id[interface_binding.material_id].wave_model != 'unsupported'
    ):
        raise ValueError('radiation fixture material/boundary mapping authority changed')

    if len(fixture.terminations) != 1:
        raise ValueError('radiation fixture must have exactly one termination')
    termination = fixture.terminations[0]
    if (
        termination.termination_id != 'open-right'
        or termination.region_id != 'room'
        or termination.kind != 'radiation'
        or termination.boundary_id != 'b-interface'
        or termination.radiation_model != 'local_first_order_outgoing'
        or termination.normal_convention != 'outward_from_region'
        or termination.characteristic_impedance_model != 'rho_c_from_environment'
        or termination.pressure_velocity_equation != 'p_eq_rho_c_u_n'
        or termination.wavenumber_equation != 'k_eq_omega_over_c'
        or termination.helmholtz_robin_equation != 'dp_dn_minus_i_k_p_eq_0'
    ):
        raise ValueError('radiation termination mathematical authority changed')

    expected_aperture = (
        (6.0, 0.0, 0.0),
        (6.0, 0.0, 2.5),
        (6.0, 4.0, 2.5),
        (6.0, 4.0, 0.0),
    )
    if tuple(_position(item) for item in termination.aperture) != expected_aperture:
        raise ValueError('radiation termination aperture authority changed')
    if float(fixture.environment.density_kg_m3) != 1.2:
        raise ValueError('radiation reference density authority changed')
    if float(fixture.environment.sound_speed_m_s) != 343.0:
        raise ValueError('radiation reference sound-speed authority changed')

    comparison = fixture.comparison
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
        or float(comparison.observation_time_s) != 2.0
    ):
        raise ValueError('radiation comparison authority changed')

    if len(fixture.sources) != 1 or len(fixture.receivers) != 1:
        raise ValueError('radiation reference requires exactly one source and receiver')
    source = fixture.sources[0]
    receiver = fixture.receivers[0]
    if (
        _position(source.position) != (1.0, 2.0, 1.0)
        or source.normalization != 'volume_velocity_m3_s'
        or float(source.amplitude) != 1.0
        or float(source.phase_deg) != 0.0
        or source.directivity != 'omnidirectional'
        or _position(receiver.position) != (5.0, 2.0, 1.0)
        or receiver.calibration_state != 'ideal_flat'
        or receiver.calibration_profile_id is not None
        or receiver.timing_reference != 'source_t0'
    ):
        raise ValueError('radiation source/receiver authority changed')


def _observable(fixture, observable_id: str):
    return next(item for item in fixture.observables if item.observable_id == observable_id)


def _reference_payload(frequencies: tuple[float, ...], pressures: tuple[complex, ...]) -> dict[str, object]:
    return {
        'fixture_id': FIXTURE_ID,
        'model': 'rectangular-neumann-transverse-modal-green-function-v1',
        'radiation_model': 'local_first_order_outgoing',
        'fourier_sign': 'exp(-i*omega*t)',
        'normal_convention': 'outward_from_region',
        'pressure_velocity_equation': 'p_eq_rho_c_u_n',
        'helmholtz_robin_equation': 'dp_dn_minus_i_k_p_eq_0',
        'transfer_magnitude_definition': '20*log10(|P/Q|/(1 Pa/(m3/s)))',
        'transfer_magnitude_unit': 'dB re 1 Pa/(m3/s)',
        'truncation': REFERENCE_TRUNCATION,
        'samples': [
            {
                'frequency_hz': frequency_hz,
                'pressure_real_pa': pressure.real,
                'pressure_imag_pa': pressure.imag,
            }
            for frequency_hz, pressure in zip(frequencies, pressures, strict=True)
        ],
    }


def _compare_manifest_samples(fixture, frequencies: tuple[float, ...], pressures: tuple[complex, ...]) -> None:
    magnitude = _observable(fixture, MAGNITUDE_OBSERVABLE_ID)
    complex_pressure = _observable(fixture, COMPLEX_OBSERVABLE_ID)
    if magnitude.reference_kind != 'analytical' or complex_pressure.reference_kind != 'analytical':
        raise ValueError('radiation reference observables must remain analytical authority')
    if magnitude.unit != 'dB re 1 Pa/(m3/s)':
        raise ValueError('radiation transfer-magnitude dB reference unit changed')
    if complex_pressure.unit != 'Pa':
        raise ValueError('radiation complex-pressure unit changed')
    if len(magnitude.samples) != len(frequencies) or len(complex_pressure.samples) != len(frequencies):
        raise ValueError('radiation reference samples do not cover the complete frozen grid')

    magnitude_by_key = {item.sample_key: item for item in magnitude.samples}
    complex_by_key = {item.sample_key: item for item in complex_pressure.samples}
    source_volume_velocity_m3_s = float(fixture.sources[0].amplitude)
    for frequency_hz, pressure in zip(frequencies, pressures, strict=True):
        integer_hz = int(round(frequency_hz))
        magnitude_sample = magnitude_by_key.get(f'mag@{integer_hz}Hz')
        complex_sample = complex_by_key.get(f'P@{integer_hz}Hz')
        if magnitude_sample is None or complex_sample is None:
            raise ValueError(f'missing radiation reference sample at {frequency_hz} Hz')
        transfer_magnitude = abs(pressure) / source_volume_velocity_m3_s
        expected_db = 20.0 * math.log10(transfer_magnitude / 1.0)
        if not math.isclose(
            float(magnitude_sample.scalar_value),
            expected_db,
            rel_tol=REFERENCE_RELATIVE_TOLERANCE,
            abs_tol=REFERENCE_ABSOLUTE_TOLERANCE,
        ):
            raise ValueError(f'radiation magnitude sample drift at {frequency_hz} Hz')
        if not math.isclose(
            float(complex_sample.real_value),
            pressure.real,
            rel_tol=REFERENCE_RELATIVE_TOLERANCE,
            abs_tol=REFERENCE_ABSOLUTE_TOLERANCE,
        ) or not math.isclose(
            float(complex_sample.imag_value),
            pressure.imag,
            rel_tol=REFERENCE_RELATIVE_TOLERANCE,
            abs_tol=REFERENCE_ABSOLUTE_TOLERANCE,
        ):
            raise ValueError(f'radiation complex-pressure sample drift at {frequency_hz} Hz')


def check_reference(manifest_path: Path) -> dict[str, object]:
    manifest = load_acoustic_benchmark_manifest(manifest_path)
    fixture = _fixture(manifest)
    _validate_authority(manifest, fixture)
    frequencies = _frequency_grid(fixture)

    coarse = tuple(_pressure(fixture, frequency_hz, COARSE_TRUNCATION) for frequency_hz in frequencies)
    reference = tuple(
        _pressure(fixture, frequency_hz, REFERENCE_TRUNCATION)
        for frequency_hz in frequencies
    )
    numerator = sum(abs(fine - coarse_value) ** 2 for coarse_value, fine in zip(coarse, reference, strict=True))
    denominator = sum(abs(fine) ** 2 for fine in reference)
    rms_relative = math.sqrt(numerator / max(denominator, 1.0e-300))
    max_point_relative = max(
        abs(fine - coarse_value) / max(abs(fine), 1.0e-300)
        for coarse_value, fine in zip(coarse, reference, strict=True)
    )
    if rms_relative > RMS_RELATIVE_LIMIT or max_point_relative > MAX_POINT_RELATIVE_LIMIT:
        raise ValueError(
            'radiation modal reference did not converge under frozen truncation check: '
            f'rms_relative={rms_relative} max_point_relative={max_point_relative}'
        )

    _compare_manifest_samples(fixture, frequencies, reference)
    payload = _reference_payload(frequencies, reference)
    reference_sha256 = sha256(
        canonical_benchmark_json(payload).encode('utf-8')
    ).hexdigest()
    return {
        'status': 'pass',
        'manifest_id': manifest.manifest_id,
        'r100a_semantic_hash': manifest.semantic_hash(),
        'fixture_id': FIXTURE_ID,
        'radiation_model': fixture.terminations[0].radiation_model,
        'coarse_truncation': COARSE_TRUNCATION,
        'reference_truncation': REFERENCE_TRUNCATION,
        'complex_rms_relative_coarse_to_reference': rms_relative,
        'max_point_relative_coarse_to_reference': max_point_relative,
        'frequency_sample_count': len(frequencies),
        'reference_payload_sha256': reference_sha256,
    }


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


def _runtime_versions() -> dict[str, str]:
    result = {'python': platform.python_version()}
    for name in ('pydantic', 'psutil'):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = 'not-installed'
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Validate the frozen R100A-3 local-radiation semi-analytical reference'
    )
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    monitor = ProcessPeakRssMonitor()
    monitor.start()
    started = time.perf_counter()
    summary = check_reference(args.manifest)
    reference_s = time.perf_counter() - started
    peak_rss_mb = monitor.stop()

    if args.output is not None:
        manifest = load_acoustic_benchmark_manifest(args.manifest)
        fixture = _fixture(manifest)
        frequencies = _frequency_grid(fixture)
        pressures = tuple(
            _pressure(fixture, frequency_hz, REFERENCE_TRUNCATION)
            for frequency_hz in frequencies
        )
        reference_payload = _reference_payload(frequencies, pressures)
        raw_reference_bytes = len(
            canonical_benchmark_json(reference_payload).encode('utf-8')
        )
        artifact = {
            'schema_version': ARTIFACT_SCHEMA,
            'workflow_result_is_reference_result': False,
            'reference_outcome': 'pass',
            **summary,
            'raw_reference': reference_payload,
            'resource_evidence': {
                'reference_compute_s': reference_s,
                'peak_rss_mb': peak_rss_mb,
                'raw_reference_mb': raw_reference_bytes / (1024.0 * 1024.0),
            },
            'source_provenance': {
                **_htdt_git_provenance(),
                'manifest_file_sha256': _sha256_file(args.manifest),
                'checker_source_sha256': _sha256_file(Path(__file__).resolve()),
                'runtime_versions': _runtime_versions(),
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
        )

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
