from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time
import traceback
from typing import Any

import numpy as np
import psutil
import scipy
from scipy import linalg

from htdt.acoustic_pffdtd_adapter import (
    finite_record_pressure_transfer as pffdtd_finite_record_pressure_transfer,
    pffdtd_velocity_potential_to_pressure_trace,
    recombine_pffdtd_receiver_traces,
)
from htdt.acoustic_pffdtd_polyhedral_geometry import register_r120b_polyhedral_authorities
from htdt.cad_acoustic_solver_adapter import bind_prediction_request_to_solver_adapter
from htdt.cad_candidate_wave_execution import (
    CandidateResourceConfiguration,
    CandidateWaveExecutionError,
    build_pffdtd_candidate_configuration,
)
from htdt.r130d_general3d_validation import (
    EVIDENCE_SCHEMA,
    ObservableContractMismatch,
    R130DGeneral3DValidationPlan,
    analytic_complex_harmonic_spectrum,
    analytic_sampled_complex_harmonic_left_rectangle_spectrum,
    assess_refinement_series,
    classify_dense_frequency_neighborhood,
    classify_frequency_neighborhood,
    classify_spatial_representation_trend,
    compare_complex_transfer,
    connected_air_domain_node_metrics,
    dense_frequency_grid,
    interpolation_stencil_diagnostic,
    load_dense_frequency_diagnostic_plan,
    load_spatial_representation_diagnostic_plan,
    load_target_window_diagnostic_plan,
    load_validation_plan,
    native_window_left_rectangle_transfer,
    normalized_complex_difference,
    plane_distance_metrics,
    save_evidence,
    semantic_hash,
    target_window_sampling_metadata,
    target_window_clipped_left_rectangle_transfer,
    validate_dense_frequency_diagnostic_binding,
    validate_exact_binding,
    validate_physical_observable_contract,
    validate_refinement_schedule,
    validate_spatial_representation_diagnostic_binding,
    validation_decision_v2,
)

from run_r130a_candidate_wave_execution import _fixture as build_r130a_fixture
from run_r130d_polyhedral_candidate_wave_execution import (
    _make_polyhedron,
    _restore_pinned_pffdtd_checkout,
    _result_evidence,
    _sloped_same_bbox_fixture,
)
from htdt.acoustics.services.acoustic_pffdtd_polyhedral_executor import PffdtdPolyhedralCandidateWaveExecutor


SYSTEM_SCHEMA = 'htdt.r130d.mfem-sloped-system-1'
QUANTITY = 'finite_record_complex_acoustic_pressure_per_volume_velocity'
UNIT = 'Pa/(m3/s)'
PHASOR = 'exp(-i*omega*t)'
ANALYSIS_KERNEL = 'exp(+i*omega*t)'


class ValidationBlocked(RuntimeError):
    pass


class ResourceBlocked(ValidationBlocked):
    pass


def _observable_contract(
    plan: R130DGeneral3DValidationPlan,
    *,
    geometry_sha256: str,
) -> dict[str, Any]:
    return {
        'quantity': plan.physical_quantity.quantity,
        'unit': plan.physical_quantity.unit,
        'source_position_m': plan.fixture.source_position_m,
        'receiver_position_m': plan.fixture.receiver_position_m,
        'source_convention': plan.physical_quantity.source_contract,
        'pressure_normalization': 'point acoustic pressure p=rho*d(phi)/dt',
        'excitation_normalization': plan.physical_quantity.source_normalization,
        'phasor_convention': plan.physical_quantity.phasor_convention,
        'analysis_fourier_kernel': plan.physical_quantity.analysis_fourier_kernel,
        'record_duration_s': plan.physical_quantity.duration_s,
        'record_interval': plan.physical_quantity.record_interval,
        'window_function': plan.physical_quantity.window_function,
        'frequency_hz': plan.physical_quantity.frequency_hz,
        'sound_speed_m_s': plan.fixture.sound_speed_m_s,
        'density_kg_m3': plan.fixture.density_kg_m3,
        'boundary_condition': plan.fixture.boundary_model,
        'geometry_sha256': geometry_sha256,
        'geometry_units': plan.physical_quantity.geometry_units,
    }


def _mesh_characteristic_size_m(
    plan: R130DGeneral3DValidationPlan,
    refinement: int,
) -> float:
    vertices = plan.fixture.vertices_m
    maximum = 0.0
    for tet in plan.fixture.base_tetrahedra:
        for offset, first in enumerate(tet):
            for second in tet[offset + 1:]:
                a = vertices[first]
                b = vertices[second]
                edge = math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))
                maximum = max(maximum, edge)
    return maximum / float(2 ** refinement)


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head(root: Path) -> str:
    completed = subprocess.run(
        ['git', '-C', str(root), 'rev-parse', 'HEAD'],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip().lower()


_WINDOWS_DRIVE_PATH_RE = re.compile(
    r'^(?P<drive>[A-Za-z]):[\\/](?P<rest>.*)$'
)


def _wsl_path(value: str) -> str:
    """Translate a Windows drive-letter path to its WSL mount form."""
    match = _WINDOWS_DRIVE_PATH_RE.match(value)
    if match is None:
        return value
    return (
        '/mnt/'
        + match.group('drive').lower()
        + '/'
        + match.group('rest').replace('\\', '/')
    )


def _mfem_execution_mode(executable: Path) -> str:
    """How the pinned MFEM adapter is invoked on this platform."""
    if os.name != 'nt' or executable.suffix.lower() == '.exe':
        return 'native'
    return 'wsl'


def _mfem_executable_invocation(
    executable: Path, args: list[str]
) -> list[str]:
    """Invocation prefix for the pinned MFEM adapter executable.

    POSIX runs the adapter directly; a native Windows ``.exe`` also runs
    directly. The pinned MFEM toolchain has no supported native Windows
    build, so a Linux ELF adapter built under WSL is invoked through
    ``wsl.exe`` with drive-letter paths translated to ``/mnt/<drive>/``.
    The execution is the real pinned MFEM build either way — the bridge
    only carries the process boundary.
    """
    if _mfem_execution_mode(executable) == 'native':
        return [str(executable), *args]
    return [
        'wsl.exe',
        '-e',
        _wsl_path(str(executable)),
        *[_wsl_path(arg) for arg in args],
    ]


def _complex_pairs(values: np.ndarray) -> list[list[float]]:
    array = np.asarray(values, dtype=np.complex128)
    return [[float(item.real), float(item.imag)] for item in array]


def _validate_target_window_diagnostic_binding(
    plan: R130DGeneral3DValidationPlan,
    diagnostic: dict[str, Any],
) -> None:
    checks = (
        (
            'parent_general3d_plan_sha256',
            diagnostic.get('parent_general3d_plan_sha256'),
            plan.plan_sha256(),
        ),
        (
            'target_duration_s',
            float(diagnostic.get('target_duration_s', math.nan)),
            float(plan.physical_quantity.duration_s),
        ),
        (
            'frequency_hz',
            tuple(float(x) for x in diagnostic.get('frequency_hz', ())),
            tuple(float(x) for x in plan.physical_quantity.frequency_hz),
        ),
        (
            'mfem_refinements',
            tuple(int(x) for x in diagnostic.get('series', {}).get('mfem_refinements', ())),
            tuple(int(x) for x in plan.independent_reference.uniform_refinements),
        ),
        (
            'pffdtd_ppw',
            tuple(float(x) for x in diagnostic.get('series', {}).get('pffdtd_ppw', ())),
            tuple(float(x) for x in plan.pffdtd.points_per_wavelength),
        ),
    )
    for label, actual, expected in checks:
        if actual != expected:
            raise ValidationBlocked(
                f'target-window diagnostic binding mismatch for {label}: '
                f'{actual!r} != {expected!r}'
            )

    thresholds = diagnostic.get('acceptance_thresholds_unchanged', {})
    if thresholds.get('mfem') != {
        'complex_rms_relative_max': 0.05,
        'magnitude_max_relative': 0.08,
        'phase_max_deg': 5.0,
    }:
        raise ValidationBlocked('diagnostic MFEM thresholds changed from PR #286')
    if thresholds.get('pffdtd') != {
        'complex_rms_relative_max': 0.2,
        'magnitude_max_relative': 0.25,
        'phase_max_deg': 15.0,
    }:
        raise ValidationBlocked('diagnostic PFFDTD thresholds changed from PR #286')
    if float(thresholds.get('magnitude_mask_relative_db', math.nan)) != -50.0:
        raise ValidationBlocked('diagnostic magnitude mask changed from PR #286')
    decision = diagnostic.get('decision_semantics', {})
    if not (
        decision.get('diagnostic_only') is True
        and decision.get('canonical_observable_replaced') is False
        and decision.get('cross_solver_unblocked_by_diagnostic_only') is False
        and decision.get('general_3d_validation_promoted_by_diagnostic_only') is False
    ):
        raise ValidationBlocked('diagnostic decision semantics are not fail-closed')


def _run_observation_operator_fixture(
    diagnostic: dict[str, Any],
) -> dict[str, Any]:
    spec = diagnostic['analytic_fixture']
    frequencies = np.asarray(diagnostic['frequency_hz'], dtype=np.float64)
    duration = float(diagnostic['target_duration_s'])
    p_amp = complex(*[float(x) for x in spec['pressure_amplitude']])
    q_amp = complex(*[float(x) for x in spec['source_amplitude']])
    p_hz = float(spec['pressure_harmonic_hz'])
    q_hz = float(spec['source_harmonic_hz'])
    exact_p = analytic_complex_harmonic_spectrum(
        amplitude=p_amp,
        harmonic_frequency_hz=p_hz,
        analysis_frequency_hz=frequencies,
        duration_s=duration,
    )
    exact_q = analytic_complex_harmonic_spectrum(
        amplitude=q_amp,
        harmonic_frequency_hz=q_hz,
        analysis_frequency_hz=frequencies,
        duration_s=duration,
    )
    exact_transfer = exact_p / exact_q
    cases = []
    errors = []
    for dt_s in (float(x) for x in spec['dt_s']):
        sample_count = int(math.ceil(duration / dt_s))
        ratio = duration / dt_s
        if math.isclose(ratio, round(ratio), rel_tol=0.0, abs_tol=1.0e-12):
            raise ValidationBlocked(
                'analytic fixture requires non-integer target_duration/dt'
            )
        times = np.arange(sample_count, dtype=np.float64) * dt_s
        pressure = p_amp * np.exp(-2j * np.pi * p_hz * times)
        source = q_amp * np.exp(-2j * np.pi * q_hz * times)
        native = native_window_left_rectangle_transfer(
            pressure,
            source,
            dt_s=dt_s,
            frequency_hz=frequencies,
        )
        native_exact_p = analytic_sampled_complex_harmonic_left_rectangle_spectrum(
            amplitude=p_amp,
            harmonic_frequency_hz=p_hz,
            analysis_frequency_hz=frequencies,
            dt_s=dt_s,
            sample_count=sample_count,
        )
        native_exact_q = analytic_sampled_complex_harmonic_left_rectangle_spectrum(
            amplitude=q_amp,
            harmonic_frequency_hz=q_hz,
            analysis_frequency_hz=frequencies,
            dt_s=dt_s,
            sample_count=sample_count,
        )
        native_exact = native_exact_p / native_exact_q
        native_relative_error = float(
            np.linalg.norm(native - native_exact)
            / max(float(np.linalg.norm(native_exact)), np.finfo(np.float64).tiny)
        )
        if native_relative_error > 5.0e-13:
            raise ValidationBlocked(
                'native-window harmonic fixture differs from independent '
                f'geometric-series authority: {native_relative_error}'
            )

        aligned = target_window_clipped_left_rectangle_transfer(
            pressure,
            source,
            dt_s=dt_s,
            target_duration_s=duration,
            frequency_hz=frequencies,
        )
        relative_error = float(
            np.linalg.norm(aligned - exact_transfer)
            / max(float(np.linalg.norm(exact_transfer)), np.finfo(np.float64).tiny)
        )
        errors.append(relative_error)
        cases.append(
            {
                'dt_s': dt_s,
                'sample_count': sample_count,
                'n_dt_s': sample_count * dt_s,
                'n_dt_minus_target_s': sample_count * dt_s - duration,
                'native_window_transfer': _complex_pairs(native),
                'analytic_native_window_transfer': _complex_pairs(native_exact),
                'native_window_relative_error': native_relative_error,
                'aligned_transfer': _complex_pairs(aligned),
                'analytic_target_window_transfer': _complex_pairs(exact_transfer),
                'aligned_target_relative_error': relative_error,
            }
        )
    monotone = all(
        following < previous
        for previous, following in zip(errors, errors[1:])
    )
    finest_limit = 0.11
    passed = bool(monotone and errors[-1] < finest_limit)
    if not passed:
        raise ValidationBlocked(
            'target-window analytic fixture did not satisfy frozen convergence gate'
        )
    return {
        'state': 'PASS',
        'fixture_id': spec['fixture_id'],
        'analytic_target_transfer': _complex_pairs(exact_transfer),
        'native_window_extractor_validation': 'PASS',
        'cases': cases,
        'relative_error_strictly_decreasing': monotone,
        'finest_relative_error_max': finest_limit,
        'finest_relative_error': errors[-1],
    }


def _finite_record_transfer(
    pressure: np.ndarray,
    source: np.ndarray,
    *,
    dt_s: float,
    frequency_hz: np.ndarray,
) -> np.ndarray:
    pressure = np.asarray(pressure, dtype=np.float64)
    source = np.asarray(source, dtype=np.float64)
    frequencies = np.asarray(frequency_hz, dtype=np.float64)
    if pressure.ndim != 1 or source.ndim != 1 or pressure.shape != source.shape:
        raise ValidationBlocked('MFEM finite-record pressure/source shape mismatch')
    times = np.arange(pressure.size, dtype=np.float64) * float(dt_s)
    kernel = np.exp(+2j * np.pi * frequencies[:, None] * times[None, :])
    p_spectrum = float(dt_s) * (kernel @ pressure)
    q_spectrum = float(dt_s) * (kernel @ source)
    floor = np.finfo(np.float64).eps * max(
        1.0, float(np.max(np.abs(q_spectrum)))
    )
    if np.any(np.abs(q_spectrum) <= floor):
        raise ValidationBlocked('MFEM finite-record source spectrum is zero')
    transfer = p_spectrum / q_spectrum
    if not (
        np.all(np.isfinite(transfer.real))
        and np.all(np.isfinite(transfer.imag))
    ):
        raise ValidationBlocked('MFEM transfer contains non-finite values')
    return transfer


def _csr_dense(payload: dict[str, Any], ndofs: int, label: str) -> np.ndarray:
    try:
        rows = int(payload['rows'])
        cols = int(payload['cols'])
        nnz = int(payload['nnz'])
        offsets = np.asarray(payload['row_offsets'], dtype=np.int64)
        columns = np.asarray(payload['column_indices'], dtype=np.int64)
        values = np.asarray(payload['values'], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationBlocked(f'{label} CSR payload is invalid') from exc
    if rows != ndofs or cols != ndofs:
        raise ValidationBlocked(f'{label} dimensions differ from MFEM ndofs')
    if offsets.shape != (ndofs + 1,) or columns.shape != (nnz,) or values.shape != (nnz,):
        raise ValidationBlocked(f'{label} CSR array sizes are inconsistent')
    if offsets[0] != 0 or offsets[-1] != nnz or np.any(np.diff(offsets) < 0):
        raise ValidationBlocked(f'{label} CSR offsets are invalid')
    if np.any(columns < 0) or np.any(columns >= ndofs) or not np.all(np.isfinite(values)):
        raise ValidationBlocked(f'{label} CSR contents are invalid')

    dense = np.zeros((ndofs, ndofs), dtype=np.float64)
    for row in range(ndofs):
        start = int(offsets[row])
        stop = int(offsets[row + 1])
        dense[row, columns[start:stop]] += values[start:stop]
    return dense


def _validate_system(
    plan: R130DGeneral3DValidationPlan,
    path: Path,
    *,
    refinement: int,
    expected_elements: int,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    exact = (
        ('schema_version', payload.get('schema_version'), SYSTEM_SCHEMA),
        ('fixture_id', payload.get('fixture_id'), plan.fixture.fixture_id),
        ('boundary_model', payload.get('boundary_model'), 'natural_neumann_rigid'),
        ('primary_field', payload.get('primary_field'), 'velocity_potential_phi'),
        ('mass_assembly', payload.get('mass_assembly'), plan.independent_reference.mass_assembly),
        ('stiffness_assembly', payload.get('stiffness_assembly'), plan.independent_reference.stiffness_assembly),
        ('source_functional_assembly', payload.get('source_functional_assembly'), plan.independent_reference.source_functional),
        ('receiver_functional_assembly', payload.get('receiver_functional_assembly'), plan.independent_reference.receiver_functional),
        ('source_normalization', payload.get('source_normalization'), 'volume_velocity_m3_s'),
        (
            'governing_equation',
            payload.get('governing_equation'),
            'M*phi_tt+Kc2*phi=c^2*b*q',
        ),
    )
    for name, actual, expected in exact:
        if actual != expected:
            raise ValidationBlocked(
                f'MFEM system {name} mismatch: {actual!r} != {expected!r}'
            )
    if int(payload.get('uniform_refinements', -1)) != refinement:
        raise ValidationBlocked('MFEM system refinement differs from plan')
    if int(payload.get('elements', -1)) != expected_elements:
        raise ValidationBlocked('MFEM system element count differs from plan')
    if int(payload.get('order', -1)) != plan.independent_reference.polynomial_order:
        raise ValidationBlocked('MFEM polynomial order differs from plan')
    if payload.get('base_tetrahedra') != [
        list(item) for item in plan.fixture.base_tetrahedra
    ]:
        raise ValidationBlocked('MFEM base tetrahedralization differs from plan')
    if not math.isclose(
        float(payload.get('base_volume_m3', math.nan)),
        plan.fixture.base_tetrahedralization_volume_m3,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise ValidationBlocked('MFEM base tetrahedralization volume differs from plan')

    for name, actual, expected in (
        ('density_kg_m3', payload.get('density_kg_m3'), plan.fixture.density_kg_m3),
        ('sound_speed_m_s', payload.get('sound_speed_m_s'), plan.fixture.sound_speed_m_s),
    ):
        if not math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1.0e-12):
            raise ValidationBlocked(f'MFEM {name} differs from plan')
    for name, expected in (
        ('source_position_m', plan.fixture.source_position_m),
        ('receiver_position_m', plan.fixture.receiver_position_m),
    ):
        actual = payload.get(name)
        if (
            not isinstance(actual, list)
            or len(actual) != 3
            or any(
                not math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=1.0e-12)
                for a, b in zip(actual, expected)
            )
        ):
            raise ValidationBlocked(f'MFEM {name} differs from plan')

    ndofs = int(payload.get('ndofs', 0))
    if ndofs <= 0:
        raise ValidationBlocked('MFEM ndofs is invalid')
    expected_dofs = plan.independent_reference.expected_dofs
    if expected_dofs is not None:
        index = plan.independent_reference.uniform_refinements.index(refinement)
        if ndofs != expected_dofs[index]:
            raise ValidationBlocked(
                f'MFEM DOF count differs from frozen plan: {ndofs} != '
                f'{expected_dofs[index]}'
            )
    estimated_dense_bytes = 6 * ndofs * ndofs * 8
    if estimated_dense_bytes > (
        plan.resource_ceiling.max_reference_peak_ram_mb * 1024.0 * 1024.0
    ):
        raise ResourceBlocked(
            'MFEM dense modal working-set estimate exceeds predeclared RAM ceiling'
        )

    mass = _csr_dense(payload['mass_matrix'], ndofs, 'mass_matrix')
    stiffness = _csr_dense(payload['stiffness_c2_matrix'], ndofs, 'stiffness_c2_matrix')
    source = np.asarray(payload.get('source_functional'), dtype=np.float64)
    receiver = np.asarray(payload.get('receiver_functional'), dtype=np.float64)
    if source.shape != (ndofs,) or receiver.shape != (ndofs,):
        raise ValidationBlocked('MFEM source/receiver functional size mismatch')
    if not np.all(np.isfinite(source)) or not np.all(np.isfinite(receiver)):
        raise ValidationBlocked('MFEM source/receiver functional is non-finite')
    if not (np.linalg.norm(source) > 0.0 and np.linalg.norm(receiver) > 0.0):
        raise ValidationBlocked('MFEM source/receiver functional is empty')

    mass_symmetry = float(np.max(np.abs(mass - mass.T)))
    stiffness_symmetry = float(np.max(np.abs(stiffness - stiffness.T)))
    if mass_symmetry > 1.0e-11 or stiffness_symmetry > 1.0e-6:
        raise ValidationBlocked(
            f'MFEM matrices are not sufficiently symmetric: M={mass_symmetry}, K={stiffness_symmetry}'
        )
    return {
        'payload': payload,
        'ndofs': ndofs,
        'mass': mass,
        'stiffness': stiffness,
        'source': source,
        'receiver': receiver,
        'mass_symmetry_max_abs': mass_symmetry,
        'stiffness_symmetry_max_abs': stiffness_symmetry,
        'estimated_dense_modal_bytes': estimated_dense_bytes,
    }


def _run_reference_level(
    plan: R130DGeneral3DValidationPlan,
    *,
    executable: Path,
    work_root: Path,
    refinement: int,
    expected_elements: int,
) -> dict[str, Any]:
    level_dir = work_root / f'mfem-r{refinement}'
    level_dir.mkdir(parents=True, exist_ok=True)
    system_path = level_dir / 'system.json'
    command = _mfem_executable_invocation(
        executable,
        [
            '--density', str(plan.fixture.density_kg_m3),
            '--sound-speed', str(plan.fixture.sound_speed_m_s),
            '--source-x', str(plan.fixture.source_position_m[0]),
            '--source-y', str(plan.fixture.source_position_m[1]),
            '--source-z', str(plan.fixture.source_position_m[2]),
            '--receiver-x', str(plan.fixture.receiver_position_m[0]),
            '--receiver-y', str(plan.fixture.receiver_position_m[1]),
            '--receiver-z', str(plan.fixture.receiver_position_m[2]),
            '--order', str(plan.independent_reference.polynomial_order),
            '--uniform-refinements', str(refinement),
            '--output', str(system_path),
        ],
    )
    export_started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=plan.resource_ceiling.max_reference_wall_seconds_per_level,
        )
    except subprocess.TimeoutExpired as exc:
        raise ResourceBlocked(
            f'MFEM system export refinement {refinement} exceeded wall ceiling'
        ) from exc
    export_s = time.perf_counter() - export_started
    if completed.returncode != 0:
        raise ValidationBlocked(
            f'MFEM system export refinement {refinement} failed: {completed.stdout[-4000:]}'
        )
    if not system_path.is_file():
        raise ValidationBlocked('MFEM system export completed without output')

    system = _validate_system(
        plan,
        system_path,
        refinement=refinement,
        expected_elements=expected_elements,
    )
    mass = system['mass']
    stiffness = system['stiffness']
    process = psutil.Process()
    rss_before_mb = process.memory_info().rss / (1024.0 * 1024.0)

    eigen_started = time.perf_counter()
    try:
        eigenvalues, eigenvectors = linalg.eigh(
            stiffness,
            mass,
            type=1,
            driver='gvd',
            check_finite=True,
            overwrite_a=False,
            overwrite_b=False,
        )
    except Exception as exc:
        raise ValidationBlocked(
            f'MFEM generalized eigen solve failed at refinement {refinement}: {type(exc).__name__}: {exc}'
        ) from exc
    eigen_s = time.perf_counter() - eigen_started
    if eigen_s > plan.resource_ceiling.max_reference_wall_seconds_per_level:
        raise ResourceBlocked(
            f'MFEM eigen solve refinement {refinement} exceeded wall ceiling'
        )
    rss_after_eigen_mb = process.memory_info().rss / (1024.0 * 1024.0)

    if not (
        np.all(np.isfinite(eigenvalues))
        and np.all(np.isfinite(eigenvectors))
    ):
        raise ValidationBlocked('MFEM eigen decomposition is non-finite')
    max_abs_lambda = max(1.0, float(np.max(np.abs(eigenvalues))))
    negative_threshold = 1.0e-10 * max_abs_lambda
    min_lambda = float(np.min(eigenvalues))
    if min_lambda < -negative_threshold:
        raise ValidationBlocked(
            f'MFEM generalized eigenproblem has material negative lambda {min_lambda}'
        )
    clamped = np.maximum(eigenvalues, 0.0)

    gram = eigenvectors.T @ mass @ eigenvectors
    orth_error = float(np.max(np.abs(gram - np.eye(system['ndofs']))))
    if orth_error > plan.independent_reference.mass_orthonormality_max_abs_tolerance:
        raise ValidationBlocked(
            f'MFEM mass orthonormality {orth_error} exceeds plan tolerance'
        )
    kv = stiffness @ eigenvectors
    mvl = (mass @ eigenvectors) * eigenvalues[np.newaxis, :]
    eigen_residual = float(
        np.linalg.norm(kv - mvl, ord='fro')
        / max(
            float(np.linalg.norm(kv, ord='fro')),
            float(np.linalg.norm(mvl, ord='fro')),
            1.0,
        )
    )
    if eigen_residual > (
        plan.independent_reference.generalized_eigen_residual_relative_tolerance
    ):
        raise ValidationBlocked(
            f'MFEM generalized eigen residual {eigen_residual} exceeds plan tolerance'
        )

    sample_rate_hz = plan.independent_reference.modal_sample_rate_hz
    dt_s = 1.0 / float(sample_rate_hz)
    sample_count_f = plan.physical_quantity.duration_s * sample_rate_hz
    sample_count = int(round(sample_count_f))
    if not math.isclose(sample_count_f, sample_count, rel_tol=0.0, abs_tol=1.0e-12):
        raise ValidationBlocked('MFEM duration is not an integer number of samples')

    rhs = (
        plan.fixture.sound_speed_m_s
        * plan.fixture.sound_speed_m_s
        * dt_s
        * system['source']
    )
    try:
        phi_t0 = linalg.solve(
            mass,
            rhs,
            assume_a='pos',
            check_finite=True,
            overwrite_a=False,
            overwrite_b=False,
        )
    except Exception as exc:
        raise ValidationBlocked(
            f'MFEM initial mass solve failed: {type(exc).__name__}: {exc}'
        ) from exc
    source_mass_residual = float(
        np.linalg.norm(mass @ phi_t0 - rhs)
        / max(float(np.linalg.norm(rhs)), 1.0e-30)
    )
    if source_mass_residual > 1.0e-10:
        raise ValidationBlocked(
            f'MFEM source mass residual {source_mass_residual} exceeds controlled tolerance'
        )

    modal_velocity = eigenvectors.T @ (mass @ phi_t0)
    receiver_projection = system['receiver'] @ eigenvectors
    amplitudes = (
        plan.fixture.density_kg_m3
        * receiver_projection
        * modal_velocity
    )
    omega = np.sqrt(clamped)

    reconstruct_started = time.perf_counter()
    pressure = np.empty(sample_count, dtype=np.float64)
    chunk_size = 1024
    for start in range(0, sample_count, chunk_size):
        stop = min(start + chunk_size, sample_count)
        times = np.arange(start, stop, dtype=np.float64) * dt_s
        pressure[start:stop] = np.cos(
            omega[:, None] * times[None, :]
        ).T @ amplitudes
    reconstruct_s = time.perf_counter() - reconstruct_started
    source_trace = np.zeros(sample_count, dtype=np.float64)
    source_trace[0] = 1.0
    frequencies = np.asarray(plan.physical_quantity.frequency_hz, dtype=np.float64)
    transfer = _finite_record_transfer(
        pressure,
        source_trace,
        dt_s=dt_s,
        frequency_hz=frequencies,
    )
    aligned_transfer = target_window_clipped_left_rectangle_transfer(
        pressure,
        source_trace,
        dt_s=dt_s,
        target_duration_s=plan.physical_quantity.duration_s,
        frequency_hz=frequencies,
    )
    sampling_metadata = target_window_sampling_metadata(
        solver='MFEM',
        requested_duration_s=plan.physical_quantity.duration_s,
        dt_s=dt_s,
        sample_count=sample_count,
        frequency_hz=frequencies,
        source_sampling=(
            'unit discrete volume-velocity impulse q[0]=1, q[n>0]=0 on '
            'the MFEM modal sample grid'
        ),
        pressure_sampling=(
            'full-basis modal pressure samples rho*r^T*phi_t at t_n=n*dt'
        ),
    )
    rss_final_mb = process.memory_info().rss / (1024.0 * 1024.0)
    checkpoint_rss_max_mb = max(rss_before_mb, rss_after_eigen_mb, rss_final_mb)
    if checkpoint_rss_max_mb > plan.resource_ceiling.max_reference_peak_ram_mb:
        raise ResourceBlocked(
            'MFEM observed checkpoint RSS exceeds predeclared RAM ceiling'
        )

    transfer_pairs = _complex_pairs(transfer)
    aligned_transfer_pairs = _complex_pairs(aligned_transfer)
    canonical_aligned_delta = compare_complex_transfer(
        reference=transfer_pairs,
        candidate=aligned_transfer_pairs,
        frequency_hz=frequencies,
        magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
    )
    return {
        'refinement': refinement,
        'mesh_identity_sha256': plan.reference_mesh_sha256(refinement),
        'system_file_sha256': _sha256_file(system_path),
        'system_numeric_identity_sha256': semantic_hash(
            {
                'refinement': refinement,
                'elements': expected_elements,
                'ndofs': system['ndofs'],
                'mass_matrix': system['payload']['mass_matrix'],
                'stiffness_c2_matrix': system['payload']['stiffness_c2_matrix'],
                'source_functional': system['payload']['source_functional'],
                'receiver_functional': system['payload']['receiver_functional'],
            }
        ),
        'elements': expected_elements,
        'dofs': system['ndofs'],
        'polynomial_order': plan.independent_reference.polynomial_order,
        'mesh_characteristic_size_m': _mesh_characteristic_size_m(
            plan, refinement
        ),
        'geometry_fixture_sha256': plan.fixture_sha256(),
        'source_representation': (
            'MFEM DomainLFIntegrator(DeltaCoefficient) point functional; '
            'Mesh::FindPoints selects one containing element'
        ),
        'receiver_representation': (
            'MFEM DomainLFIntegrator(DeltaCoefficient) H1 point functional; '
            'continuous trace is single-valued on the audited internal facet'
        ),
        'frequency_hz': list(plan.physical_quantity.frequency_hz),
        'transfer_pa_per_m3_s': transfer_pairs,
        'transfer_sha256': semantic_hash(transfer_pairs),
        'aligned_diagnostic_transfer_pa_per_m3_s': aligned_transfer_pairs,
        'aligned_diagnostic_transfer_sha256': semantic_hash(aligned_transfer_pairs),
        'canonical_aligned_delta': _metric_dict(canonical_aligned_delta),
        'sampling_metadata': sampling_metadata,
        'source_mass_relative_residual': source_mass_residual,
        'mass_symmetry_max_abs': system['mass_symmetry_max_abs'],
        'stiffness_symmetry_max_abs': system['stiffness_symmetry_max_abs'],
        'mass_orthonormality_max_abs_error': orth_error,
        'generalized_eigen_residual_relative': eigen_residual,
        'raw_min_eigenvalue': min_lambda,
        'clamped_negative_eigenvalue_count': int(np.count_nonzero(eigenvalues < 0.0)),
        'min_frequency_hz': float(np.sqrt(clamped[0]) / (2.0 * np.pi)),
        'max_frequency_hz': float(np.sqrt(clamped[-1]) / (2.0 * np.pi)),
        'sample_rate_hz': sample_rate_hz,
        'dt_s': dt_s,
        'sample_count': sample_count,
        'timings_s': {
            'system_export': export_s,
            'generalized_eigen': eigen_s,
            'modal_reconstruction': reconstruct_s,
        },
        'resource': {
            'estimated_dense_modal_bytes': system['estimated_dense_modal_bytes'],
            'checkpoint_rss_max_mb': checkpoint_rss_max_mb,
        },
        'system_export_stdout_tail': completed.stdout[-2000:],
    }


def _create_pffdtd_dispatch(
    fixture: dict[str, Any],
    *,
    configuration,
):
    fixture['store'].put_exact_json(
        configuration.as_external_ref(),
        configuration.semantic_payload(),
    )
    dispatch = bind_prediction_request_to_solver_adapter(
        snapshot=fixture['snapshot'],
        request=fixture['request'],
        adapter=fixture['descriptor'],
        solver_configuration_ref=configuration.as_external_ref(),
        numerical_fidelity_policy=fixture['fidelity_policy'],
    )
    if dispatch.state != 'READY':
        raise ValidationBlocked(
            f'PFFDTD validation dispatch is {dispatch.state}, expected READY'
        )
    fixture['dispatch_repository'].save_dispatch(dispatch)
    return dispatch


def _point_in_triangle_3d(
    point: np.ndarray,
    a: np.ndarray,
    b: np.ndarray,
    c: np.ndarray,
    *,
    tolerance: float,
) -> bool:
    v0 = b - a
    v1 = c - a
    v2 = point - a
    d00 = float(np.dot(v0, v0))
    d01 = float(np.dot(v0, v1))
    d11 = float(np.dot(v1, v1))
    d20 = float(np.dot(v2, v0))
    d21 = float(np.dot(v2, v1))
    denominator = d00 * d11 - d01 * d01
    if abs(denominator) <= 1.0e-24:
        raise ValidationBlocked('exact sloped face triangulation is degenerate')
    v = (d11 * d20 - d01 * d21) / denominator
    w = (d00 * d21 - d01 * d20) / denominator
    u = 1.0 - v - w
    return (
        u >= -tolerance
        and v >= -tolerance
        and w >= -tolerance
        and u <= 1.0 + tolerance
        and v <= 1.0 + tolerance
        and w <= 1.0 + tolerance
    )


def _read_pffdtd_spatial_representation_diagnostic(
    plan: R130DGeneral3DValidationPlan,
    diagnostic: dict[str, Any],
    *,
    sim_dir: Path,
) -> dict[str, Any]:
    try:
        import h5py
    except Exception as exc:
        raise ValidationBlocked(
            'h5py is required for PFFDTD spatial diagnostic extraction'
        ) from exc

    cart_path = sim_dir / 'cart_grid.h5'
    voxel_path = sim_dir / 'vox_out.h5'
    comms_path = sim_dir / 'comms_out.h5'
    for asset in (cart_path, voxel_path, comms_path):
        if not asset.is_file():
            raise ValidationBlocked(
                f'PFFDTD spatial diagnostic asset is missing: {asset.name}'
            )

    try:
        with h5py.File(cart_path, 'r') as handle:
            xv = np.asarray(handle['xv'][...], dtype=np.float64)
            yv = np.asarray(handle['yv'][...], dtype=np.float64)
            zv = np.asarray(handle['zv'][...], dtype=np.float64)
            h = float(handle['h'][()])
        with h5py.File(voxel_path, 'r') as handle:
            bn_ixyz = np.asarray(handle['bn_ixyz'][...], dtype=np.int64)
            adj_bn = np.asarray(handle['adj_bn'][...], dtype=bool)
            voxel_dims = (
                int(handle['Nx'][()]),
                int(handle['Ny'][()]),
                int(handle['Nz'][()]),
            )
        with h5py.File(comms_path, 'r') as handle:
            in_ixyz = np.asarray(handle['in_ixyz'][...], dtype=np.int64).reshape(-1)
            in_sigs = np.asarray(handle['in_sigs'][...], dtype=np.float64)
            out_ixyz = np.asarray(handle['out_ixyz'][...], dtype=np.int64).reshape(-1)
            out_alpha = np.asarray(handle['out_alpha'][...], dtype=np.float64)
    except Exception as exc:
        raise ValidationBlocked(
            f'PFFDTD spatial diagnostic HDF5 read failed: {type(exc).__name__}: {exc}'
        ) from exc

    dims = (int(xv.size), int(yv.size), int(zv.size))
    if dims != voxel_dims:
        raise ValidationBlocked(
            f'PFFDTD cart/voxel dimensions disagree: {dims} != {voxel_dims}'
        )
    if adj_bn.shape != (bn_ixyz.size, 6):
        raise ValidationBlocked(
            f'PFFDTD Cartesian boundary adjacency has unexpected shape {adj_bn.shape}'
        )
    if in_ixyz.shape != (8,) or in_sigs.ndim != 2 or in_sigs.shape[0] != 8:
        raise ValidationBlocked(
            'PFFDTD source authority is not the frozen eight-node trilinear stencil'
        )
    if out_ixyz.shape != (8,) or out_alpha.shape != (1, 8):
        raise ValidationBlocked(
            'PFFDTD receiver authority is not the frozen one-receiver eight-node stencil'
        )

    source_first = np.asarray(in_sigs[:, 0], dtype=np.float64)
    source_sum = float(np.sum(source_first))
    if (
        not np.all(np.isfinite(source_first))
        or not math.isfinite(source_sum)
        or abs(source_sum) <= np.finfo(np.float64).tiny
    ):
        raise ValidationBlocked('PFFDTD source first-sample stencil cannot be normalized')
    source_weights = source_first / source_sum
    source_stencil = interpolation_stencil_diagnostic(
        xv=xv,
        yv=yv,
        zv=zv,
        linear_indices=in_ixyz,
        weights=source_weights,
        exact_position_m=plan.fixture.source_position_m,
        grid_spacing_m=h,
    )
    receiver_stencil = interpolation_stencil_diagnostic(
        xv=xv,
        yv=yv,
        zv=zv,
        linear_indices=out_ixyz,
        weights=out_alpha[0],
        exact_position_m=plan.fixture.receiver_position_m,
        grid_spacing_m=h,
    )
    for label, stencil in (
        ('source', source_stencil),
        ('receiver', receiver_stencil),
    ):
        if not math.isclose(
            float(stencil['weight_sum']), 1.0, rel_tol=0.0, abs_tol=1.0e-12
        ):
            raise ValidationBlocked(f'PFFDTD {label} stencil weights do not sum to one')
        if float(stencil['reconstruction_error_m']) > max(1.0e-12, h * 1.0e-10):
            raise ValidationBlocked(
                f'PFFDTD {label} stencil does not reconstruct exact coordinate'
            )

    directions = diagnostic['spatial_representation']['cartesian_neighbor_order']
    domain = connected_air_domain_node_metrics(
        dimensions=dims,
        boundary_linear_indices=bn_ixyz,
        boundary_adjacency=adj_bn,
        source_linear_indices=in_ixyz,
        neighbor_directions=directions,
    )
    reached_mask = np.asarray(domain.pop('reachable_mask'), dtype=bool)
    exact_volume = float(plan.fixture.base_tetrahedralization_volume_m3)
    discrete_volume = float(domain['reachable_air_node_count']) * h ** 3
    relative_volume_error = (discrete_volume - exact_volume) / exact_volume

    face_map = {key: indices for key, indices in plan.fixture.faces}
    sloped_key = str(diagnostic['spatial_representation']['sloped_surface_key'])
    if sloped_key not in face_map:
        raise ValidationBlocked(f'unknown frozen sloped surface key: {sloped_key}')
    face_indices = tuple(int(x) for x in face_map[sloped_key])
    if len(face_indices) != 4:
        raise ValidationBlocked('frozen sloped surface must be the R120B quad')
    vertices = np.asarray(plan.fixture.vertices_m, dtype=np.float64)
    face_points = vertices[np.asarray(face_indices, dtype=np.int64)]
    plane_point = face_points[0]
    normal_raw = np.cross(face_points[1] - plane_point, face_points[2] - plane_point)
    normal_norm = float(np.linalg.norm(normal_raw))
    if normal_norm <= 0.0:
        raise ValidationBlocked('frozen sloped surface plane is degenerate')
    plane_normal = normal_raw / normal_norm
    plane_d = -float(np.dot(plane_normal, plane_point))

    ny = dims[1]
    nz = dims[2]
    yz = ny * nz
    reverse = (1, 0, 3, 2, 5, 4)
    direction_tuples = tuple(tuple(int(v) for v in row) for row in directions)
    boundary_row = {int(index): row for row, index in enumerate(bn_ixyz)}

    def coords(index: int) -> tuple[int, int, int]:
        ix = index // yz
        rem = index % yz
        return ix, rem // nz, rem % nz

    def linear(ix: int, iy: int, iz: int) -> int:
        return ix * yz + iy * nz + iz

    def position(index: int) -> np.ndarray:
        ix, iy, iz = coords(index)
        return np.asarray((xv[ix], yv[iy], zv[iz]), dtype=np.float64)

    blocked_edges: set[tuple[int, int]] = set()
    for row, current_value in enumerate(bn_ixyz):
        current = int(current_value)
        ix, iy, iz = coords(current)
        for direction_index, (dx, dy, dz) in enumerate(direction_tuples):
            if bool(adj_bn[row, direction_index]):
                continue
            nx, ny_, nz_ = ix + dx, iy + dy, iz + dz
            if not (0 <= nx < dims[0] and 0 <= ny_ < dims[1] and 0 <= nz_ < dims[2]):
                continue
            neighbor = linear(nx, ny_, nz_)
            pair = (min(current, neighbor), max(current, neighbor))
            blocked_edges.add(pair)
            neighbor_row = boundary_row.get(neighbor)
            if (
                neighbor_row is not None
                and bool(adj_bn[neighbor_row, reverse[direction_index]])
            ):
                raise ValidationBlocked(
                    'PFFDTD boundary adjacency is asymmetric across a blocked edge'
                )

    plane_tolerance = max(1.0e-12, h * 1.0e-9)
    bary_tolerance = max(1.0e-12, h * 1.0e-9)
    tri_a = (face_points[0], face_points[1], face_points[2])
    tri_b = (face_points[0], face_points[2], face_points[3])
    samples: list[list[float]] = []
    for first, second in sorted(blocked_edges):
        p0 = position(first)
        p1 = position(second)
        s0 = float(np.dot(plane_normal, p0) + plane_d)
        s1 = float(np.dot(plane_normal, p1) + plane_d)
        denominator = s0 - s1
        if abs(denominator) <= plane_tolerance:
            continue
        t = s0 / denominator
        if t < -1.0e-12 or t > 1.0 + 1.0e-12:
            continue
        intersection = p0 + t * (p1 - p0)
        if not (
            _point_in_triangle_3d(
                intersection, *tri_a, tolerance=bary_tolerance
            )
            or _point_in_triangle_3d(
                intersection, *tri_b, tolerance=bary_tolerance
            )
        ):
            continue
        samples.append([float(x) for x in (0.5 * (p0 + p1))])

    if not samples:
        raise ValidationBlocked(
            'PFFDTD spatial diagnostic found no blocked-adjacency samples on sloped surface'
        )
    distance = plane_distance_metrics(
        samples,
        plane_point_m=plane_point,
        plane_unit_normal=plane_normal,
        grid_spacing_m=h,
    )

    geometry_core = {
        'representation_kind': diagnostic['spatial_representation']['representation_kind'],
        'grid_spacing_m': h,
        'grid_dimensions': list(dims),
        'grid_origin_m': [float(xv[0]), float(yv[0]), float(zv[0])],
        'grid_axes_m': {
            'x': [float(x) for x in xv],
            'y': [float(x) for x in yv],
            'z': [float(x) for x in zv],
        },
        'boundary_linear_indices': [int(x) for x in bn_ixyz],
        'boundary_adjacency': adj_bn.astype(np.uint8).tolist(),
        'cart_grid_file_sha256': _sha256_file(cart_path),
        'voxel_mask_file_sha256': _sha256_file(voxel_path),
    }
    return {
        'representation_definition': diagnostic['spatial_representation']['representation_kind'],
        'grid_spacing_m': h,
        'grid_dimensions': list(dims),
        'exact_polyhedron_volume_m3': exact_volume,
        'discrete_air_domain_volume_estimate_m3': discrete_volume,
        'relative_volume_error': relative_volume_error,
        'absolute_relative_volume_error': abs(relative_volume_error),
        **domain,
        'geometry_representation_hash': semantic_hash(geometry_core),
        'pffdtd_cart_grid_asset_file_sha256': geometry_core[
            'cart_grid_file_sha256'
        ],
        'pffdtd_geometry_mask_asset_file_sha256': geometry_core[
            'voxel_mask_file_sha256'
        ],
        'sloped_surface_key': sloped_key,
        'exact_plane_equation_unit_normal': {
            'a': float(plane_normal[0]),
            'b': float(plane_normal[1]),
            'c': float(plane_normal[2]),
            'd': plane_d,
        },
        'staircase_sample_definition': diagnostic['spatial_representation']['sloped_staircase_sample_definition'],
        **distance,
        'source_stencil': source_stencil,
        'receiver_stencil': receiver_stencil,
    }


def _validate_pr295_canonical_reproduction(
    summary_path: Path,
    *,
    reference_levels: list[dict[str, Any]],
    pffdtd_levels: list[dict[str, Any]],
    max_abs_tolerance: float,
) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    expected_mfem = {
        int(item['refinement']): np.asarray(item['canonical'], dtype=np.float64)
        for item in summary.get('outputs', {}).get('mfem', ())
    }
    expected_pffdtd = {
        float(item['points_per_wavelength']): np.asarray(
            item['canonical'], dtype=np.float64
        )
        for item in summary.get('outputs', {}).get('pffdtd', ())
    }
    if set(expected_mfem) != {1, 2, 3} or set(expected_pffdtd) != {8.0, 10.0, 12.0}:
        raise ValidationBlocked('PR #295 canonical baseline schedule is incomplete')

    tolerance = float(max_abs_tolerance)
    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise ValidationBlocked('PR #295 reproduction tolerance is invalid')
    details: dict[str, list[dict[str, Any]]] = {'mfem': [], 'pffdtd': []}
    maximum = 0.0
    for level in reference_levels:
        key = int(level['refinement'])
        actual = np.asarray(level['transfer_pa_per_m3_s'], dtype=np.float64)
        error = float(np.max(np.abs(actual - expected_mfem[key])))
        maximum = max(maximum, error)
        if error > tolerance:
            raise ValidationBlocked(
                f'MFEM refinement {key} did not reproduce PR #295 canonical transfer: '
                f'max_abs_component_error={error} > {tolerance}'
            )
        details['mfem'].append(
            {'refinement': key, 'max_abs_complex_component_error': error}
        )
    for level in pffdtd_levels:
        key = float(level['points_per_wavelength'])
        actual = np.asarray(level['transfer_pa_per_m3_s'], dtype=np.float64)
        error = float(np.max(np.abs(actual - expected_pffdtd[key])))
        maximum = max(maximum, error)
        if error > tolerance:
            raise ValidationBlocked(
                f'PFFDTD {key:g} PPW did not reproduce PR #295 canonical transfer: '
                f'max_abs_component_error={error} > {tolerance}'
            )
        details['pffdtd'].append(
            {'points_per_wavelength': key, 'max_abs_complex_component_error': error}
        )
    return {
        'state': 'PASS',
        'baseline_authoritative_run_id': summary.get('source', {}).get('workflow_run_id'),
        'baseline_artifact_id': summary.get('source', {}).get('artifact_id'),
        'baseline_artifact_digest_sha256': summary.get('source', {}).get(
            'artifact_digest_sha256'
        ),
        'max_abs_complex_component_error_all_six_levels': maximum,
        'max_abs_complex_component_tolerance': tolerance,
        **details,
    }


def _run_pffdtd_level(
    plan: R130DGeneral3DValidationPlan,
    *,
    fixture: dict[str, Any],
    executor: PffdtdPolyhedralCandidateWaveExecutor,
    semantic_ref,
    compiled_ref,
    rigid_boundary_ref,
    ppw: float,
    spatial_diagnostic: dict[str, Any],
    dense_diagnostic: dict[str, Any],
) -> dict[str, Any]:
    base = fixture['configuration']
    configuration = build_pffdtd_candidate_configuration(
        expected_pffdtd_commit_sha=plan.pffdtd.source_commit_sha,
        fmax_hz=plan.pffdtd.fmax_hz,
        points_per_wavelength=float(ppw),
        duration_s=plan.physical_quantity.duration_s,
        frequency_samples_hz=plan.physical_quantity.frequency_hz,
        density_kg_m3=plan.fixture.density_kg_m3,
        density_authority_ref=base.density_authority_ref,
        relative_humidity_percent=float(base.relative_humidity_percent),
        humidity_authority_ref=base.humidity_authority_ref,
        resource=CandidateResourceConfiguration(
            solver_threads=plan.pffdtd.solver_threads,
            setup_processes=plan.pffdtd.setup_processes,
            max_grid_cells=plan.pffdtd.max_grid_cells,
            max_time_steps=plan.pffdtd.max_time_steps,
            max_output_bytes=plan.pffdtd.max_output_bytes,
            max_solver_wall_seconds=plan.pffdtd.max_solver_wall_seconds,
        ),
    )
    dispatch = _create_pffdtd_dispatch(fixture, configuration=configuration)
    execution_authority, _, _ = executor.compile_input(
        dispatch_binding_id=dispatch.binding_id,
        configuration=configuration,
        semantic_geometry_ref=semantic_ref,
        compiled_geometry_ref=compiled_ref,
        rigid_boundary_physics_ref=rigid_boundary_ref,
    )
    _restore_pinned_pffdtd_checkout(fixture['executor'])
    result = executor.execute(
        dispatch_binding_id=dispatch.binding_id,
        configuration=configuration,
        semantic_geometry_ref=semantic_ref,
        compiled_geometry_ref=compiled_ref,
        rigid_boundary_physics_ref=rigid_boundary_ref,
    )
    evidence = _result_evidence(fixture, result)
    artifact = fixture['store'].read_payload(
        result.artifacts[0].artifact_authority
    )
    provenance = fixture['store'].read_payload(result.execution_provenance_ref)
    representation = artifact.get('complex_representation', {})
    time_sampling = artifact.get('time_sampling', {})
    contract_checks = (
        ('artifact units', artifact.get('units'), 'Pa'),
        (
            'phasor convention',
            representation.get('phasor_convention'),
            plan.physical_quantity.phasor_convention,
        ),
        (
            'Fourier kernel',
            representation.get('analysis_fourier_kernel'),
            plan.physical_quantity.analysis_fourier_kernel,
        ),
        (
            'record interval',
            time_sampling.get('finite_record_interval'),
            plan.physical_quantity.record_interval,
        ),
        (
            'frequency axis',
            tuple(float(x) for x in artifact.get('frequency_axis_hz', ())),
            plan.physical_quantity.frequency_hz,
        ),
    )
    for label, actual, expected in contract_checks:
        if actual != expected:
            raise ObservableContractMismatch(
                f'PFFDTD {label} mismatch: {actual!r} != {expected!r}'
            )
    structured_configuration_checks = (
        (
            'source injection mapping',
            configuration.source_injection_mapping,
            'unit_discrete_volume_velocity_impulse_for_transfer_then_exact_Q_spectrum',
        ),
        (
            'pressure conversion',
            configuration.pressure_conversion,
            'p=rho*d(phi)/dt_second_order',
        ),
        (
            'transfer definition',
            configuration.transfer_definition,
            'finite_record_direct_dtft_P_over_Q_exp_plus_iwt',
        ),
    )
    for label, actual, expected in structured_configuration_checks:
        if actual != expected:
            raise ObservableContractMismatch(
                f'PFFDTD {label} mismatch: {actual!r} != {expected!r}'
            )
    source_authority = artifact.get('source_authority', {})
    if source_authority.get('wave_excitation_sha256') != fixture['excitation'].semantic_sha256:
        raise ObservableContractMismatch(
            'PFFDTD artifact does not bind the exact wave excitation authority'
        )
    if not math.isclose(
        float(time_sampling.get('requested_duration_s', math.nan)),
        plan.physical_quantity.duration_s,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise ObservableContractMismatch('PFFDTD record duration differs from plan')
    if not math.isclose(
        float(provenance.get('sound_speed_m_s', math.nan)),
        plan.fixture.sound_speed_m_s,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise ObservableContractMismatch('PFFDTD sound speed differs from plan')
    if not math.isclose(
        float(configuration.density_kg_m3),
        plan.fixture.density_kg_m3,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise ObservableContractMismatch('PFFDTD density differs from plan')

    real = np.asarray(evidence['pressure_real_pa'][0], dtype=np.float64)
    imag = np.asarray(evidence['pressure_imag_pa'][0], dtype=np.float64)
    pressure = real + 1j * imag
    excitation_by_frequency = {
        float(item.frequency_hz): complex(
            float(item.real_m3_s), float(item.imag_m3_s)
        )
        for item in fixture['excitation'].samples
    }
    q = np.asarray(
        [
            excitation_by_frequency[float(frequency)]
            for frequency in plan.physical_quantity.frequency_hz
        ],
        dtype=np.complex128,
    )
    if np.any(np.abs(q) <= 0.0):
        raise ValidationBlocked('PFFDTD exact excitation has zero comparison sample')
    transfer = pressure / q
    if not (
        np.all(np.isfinite(transfer.real))
        and np.all(np.isfinite(transfer.imag))
    ):
        raise ValidationBlocked('PFFDTD normalized transfer is non-finite')
    transfer_pairs = _complex_pairs(transfer)
    time_step_s = float(provenance['time_step_s'])
    time_step_count = int(provenance['time_step_count'])

    run_dir = (
        executor.base_executor.work_root
        / execution_authority.semantic_sha256
        / 'sim'
    )
    raw_output_path = run_dir / 'sim_outs.h5'
    comms_path = run_dir / 'comms_out.h5'
    if not raw_output_path.is_file() or not comms_path.is_file():
        raise ValidationBlocked(
            'PFFDTD raw output/comms assets are missing for target-window diagnosis'
        )
    try:
        import h5py

        with h5py.File(raw_output_path, 'r') as handle:
            raw_grid = np.asarray(handle['u_out'][...], dtype=np.float64)
        with h5py.File(comms_path, 'r') as handle:
            out_alpha = np.asarray(handle['out_alpha'][...], dtype=np.float64)
            raw_nt = int(handle['Nt'][()])
    except Exception as exc:
        raise ValidationBlocked(
            f'PFFDTD raw diagnostic trace load failed: {type(exc).__name__}: {exc}'
        ) from exc
    if raw_nt != time_step_count:
        raise ValidationBlocked(
            'PFFDTD raw diagnostic Nt differs from execution provenance'
        )
    receiver_potential = recombine_pffdtd_receiver_traces(
        raw_grid,
        out_alpha,
        receiver_count=1,
        nt=time_step_count,
    )[0]
    pressure_trace = pffdtd_velocity_potential_to_pressure_trace(
        receiver_potential,
        time_step_s=time_step_s,
        density_kg_m3=plan.fixture.density_kg_m3,
    )
    source_trace = np.zeros(time_step_count, dtype=np.float64)
    source_trace[0] = 1.0
    canonical_from_raw = pffdtd_finite_record_pressure_transfer(
        pressure_trace,
        source_trace,
        time_step_s=time_step_s,
        frequency_hz=np.asarray(
            plan.physical_quantity.frequency_hz, dtype=np.float64
        ),
    )
    canonical_raw_error = float(np.max(np.abs(canonical_from_raw - transfer)))
    if not np.allclose(
        canonical_from_raw,
        transfer,
        rtol=1.0e-12,
        atol=1.0e-12,
    ):
        raise ValidationBlocked(
            'PFFDTD canonical transfer does not reproduce from persisted raw trace: '
            f'max_abs={canonical_raw_error}'
        )
    aligned_transfer = target_window_clipped_left_rectangle_transfer(
        pressure_trace,
        source_trace,
        dt_s=time_step_s,
        target_duration_s=plan.physical_quantity.duration_s,
        frequency_hz=np.asarray(
            plan.physical_quantity.frequency_hz, dtype=np.float64
        ),
    )
    aligned_transfer_pairs = _complex_pairs(aligned_transfer)
    canonical_aligned_delta = compare_complex_transfer(
        reference=transfer_pairs,
        candidate=aligned_transfer_pairs,
        frequency_hz=plan.physical_quantity.frequency_hz,
        magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
    )
    diagnostic_frequencies = np.asarray(
        spatial_diagnostic['frequency_neighborhood']['diagnostic_frequency_hz'],
        dtype=np.float64,
    )
    neighborhood_transfer = pffdtd_finite_record_pressure_transfer(
        pressure_trace,
        source_trace,
        time_step_s=time_step_s,
        frequency_hz=diagnostic_frequencies,
    )
    dense_frequencies = np.asarray(
        dense_diagnostic['dense_frequency_neighborhood'][
            'diagnostic_frequency_hz'
        ],
        dtype=np.float64,
    )
    dense_transfer = pffdtd_finite_record_pressure_transfer(
        pressure_trace,
        source_trace,
        time_step_s=time_step_s,
        frequency_hz=dense_frequencies,
    )
    pressure_trace_sha256 = semantic_hash([float(x) for x in pressure_trace])
    source_trace_sha256 = semantic_hash([float(x) for x in source_trace])
    run76_binding = dense_diagnostic['run76_record_binding']
    run76_pin = {
        float(item['points_per_wavelength']): item
        for item in run76_binding['levels']
    }[float(ppw)]
    run76_trace_binding = (
        run76_binding['identical_label']
        if (
            pressure_trace_sha256 == run76_pin['pressure_trace_sha256']
            and source_trace_sha256 == run76_pin['source_trace_sha256']
        )
        else run76_binding['nonidentical_label']
    )
    spatial_metrics = _read_pffdtd_spatial_representation_diagnostic(
        plan,
        spatial_diagnostic,
        sim_dir=run_dir,
    )
    spatial_metrics['pffdtd_cart_grid_logical_sha256'] = evidence[
        'cart_grid_logical_sha256'
    ]
    spatial_metrics['pffdtd_geometry_mask_logical_sha256'] = evidence[
        'boundary_mask_logical_sha256'
    ]
    if not math.isclose(
        float(spatial_metrics['grid_spacing_m']),
        float(evidence['grid_spacing_m']),
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise ValidationBlocked(
            'PFFDTD spatial diagnostic grid spacing differs from executed-grid evidence'
        )
    if tuple(int(x) for x in spatial_metrics['grid_dimensions']) != tuple(
        int(x) for x in evidence['grid_dimensions']
    ):
        raise ValidationBlocked(
            'PFFDTD spatial diagnostic dimensions differ from executed-grid evidence'
        )
    sampling_metadata = target_window_sampling_metadata(
        solver='PFFDTD',
        requested_duration_s=plan.physical_quantity.duration_s,
        dt_s=time_step_s,
        sample_count=time_step_count,
        frequency_hz=plan.physical_quantity.frequency_hz,
        source_sampling=(
            'unit discrete volume-velocity impulse q[0]=1, q[n>0]=0 from '
            'the exact PFFDTD candidate source mapping'
        ),
        pressure_sampling=(
            'native recombined PFFDTD receiver potential converted by the '
            'existing second-order p=rho*d(phi)/dt pressure mapping'
        ),
    )
    grid_spacing_m = float(evidence['grid_spacing_m'])
    courant_c_dt_over_h = (
        plan.fixture.sound_speed_m_s * time_step_s / grid_spacing_m
    )
    grid_cell_count = math.prod(int(x) for x in evidence['grid_dimensions'])
    return {
        'points_per_wavelength': float(ppw),
        'configuration_ref': configuration.as_external_ref().model_dump(mode='json'),
        'dispatch_id': dispatch.binding_id,
        'dispatch_sha256': dispatch.semantic_sha256,
        'result_id': evidence['result_id'],
        'result_sha256': evidence['result_sha256'],
        'artifact_ref': evidence['artifact_ref'],
        'candidate_execution_input_sha256': evidence['candidate_execution_input_sha256'],
        'solver_geometry_ref': evidence['solver_geometry_ref'],
        'executed_grid_ref': evidence['executed_grid_ref'],
        'grid_origin_m': evidence['grid_origin_m'],
        'grid_spacing_m': evidence['grid_spacing_m'],
        'grid_dimensions': evidence['grid_dimensions'],
        'grid_cell_count': grid_cell_count,
        'active_cell_count': grid_cell_count,
        'active_cell_count_semantics': (
            'allocated Cartesian update grid; boundary_node_count is reported '
            'separately from the executed voxel authority'
        ),
        'boundary_mask_logical_sha256': evidence['boundary_mask_logical_sha256'],
        'cart_grid_logical_sha256': evidence['cart_grid_logical_sha256'],
        'boundary_node_count': evidence['boundary_node_count'],
        'resource_estimate_ref': evidence['resource_estimate_ref'],
        'resource_estimate': evidence['resource_estimate'],
        'timings_s': evidence['timings_s'],
        'runtime_identity': provenance.get('runtime_identity'),
        'time_step_s': time_step_s,
        'time_step_count': time_step_count,
        'record_last_sample_time_s': (time_step_count - 1) * time_step_s,
        'record_next_sample_time_s': time_step_count * time_step_s,
        'courant_c_dt_over_h': courant_c_dt_over_h,
        'cfl_fraction_of_3d_cartesian_limit': (
            courant_c_dt_over_h * math.sqrt(3.0)
        ),
        'source_representation': (
            'PFFDTD SimComms eight-node trilinear interpolation at exact '
            'physical source xyz'
        ),
        'receiver_representation': (
            'PFFDTD SimComms eight-node trilinear interpolation/recombination '
            'at exact physical receiver xyz'
        ),
        'raw_solver_asset_sha256': evidence['raw_solver_asset_sha256'],
        'artifact_pressure_reference_note': artifact.get('reference'),
        'source_injection_mapping': configuration.source_injection_mapping,
        'pressure_conversion_mapping': configuration.pressure_conversion,
        'transfer_definition': configuration.transfer_definition,
        'frequency_hz': list(plan.physical_quantity.frequency_hz),
        'absolute_pressure_pa': _complex_pairs(pressure),
        'exact_excitation_q_m3_s': _complex_pairs(q),
        'transfer_pa_per_m3_s': transfer_pairs,
        'transfer_sha256': semantic_hash(transfer_pairs),
        'aligned_diagnostic_transfer_pa_per_m3_s': aligned_transfer_pairs,
        'aligned_diagnostic_transfer_sha256': semantic_hash(
            aligned_transfer_pairs
        ),
        'canonical_aligned_delta': _metric_dict(canonical_aligned_delta),
        'canonical_recomputed_from_raw_max_abs_error': canonical_raw_error,
        'diagnostic_frequency_hz': [float(x) for x in diagnostic_frequencies],
        'diagnostic_neighborhood_transfer_pa_per_m3_s': _complex_pairs(
            neighborhood_transfer
        ),
        'diagnostic_neighborhood_transfer_sha256': semantic_hash(
            _complex_pairs(neighborhood_transfer)
        ),
        'dense_diagnostic_frequency_hz': [
            float(x) for x in dense_frequencies
        ],
        'dense_neighborhood_transfer_pa_per_m3_s': _complex_pairs(
            dense_transfer
        ),
        'dense_neighborhood_transfer_sha256': semantic_hash(
            _complex_pairs(dense_transfer)
        ),
        'run76_trace_binding': run76_trace_binding,
        'spatial_representation_diagnostic': {
            **{
                key: value
                for key, value in spatial_metrics.items()
                if key not in ('source_stencil', 'receiver_stencil')
            },
            'pffdtd_cart_grid_logical_sha256': evidence[
                'cart_grid_logical_sha256'
            ],
            'pffdtd_boundary_mask_logical_sha256': evidence[
                'boundary_mask_logical_sha256'
            ],
        },
        'source_interpolation_stencil': spatial_metrics['source_stencil'],
        'receiver_interpolation_stencil': spatial_metrics['receiver_stencil'],
        'sampling_metadata': sampling_metadata,
        'diagnostic_raw_trace': {
            'sim_outs_sha256': _sha256_file(raw_output_path),
            'comms_out_sha256': _sha256_file(comms_path),
            'pressure_trace_sha256': pressure_trace_sha256,
            'source_trace_sha256': source_trace_sha256,
        },
    }


def _metric_dict(metric) -> dict[str, Any]:
    return metric.model_dump(mode='json')


def _accepted_frequencies(plan, cross_metrics) -> list[dict[str, Any]]:
    threshold = plan.acceptance.cross_solver_fine_fine
    output = []
    for item in cross_metrics.frequency_metrics:
        accepted = bool(
            item['masked_in']
            and item['magnitude_relative'] <= threshold.magnitude_max_relative
            and (
                threshold.magnitude_max_db is None
                or item['magnitude_db'] <= threshold.magnitude_max_db
            )
            and item['phase_deg'] <= threshold.phase_max_deg
        )
        output.append(
            {
                **item,
                'status': 'ACCEPTED' if accepted else 'REJECTED',
            }
        )
    return output


def _validate_pr286_canonical_reproduction(
    summary_path: Path,
    *,
    reference_levels: list[dict[str, Any]],
    pffdtd_levels: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    expected_mfem = {
        int(item['refinement']): item
        for item in summary.get('mfem_levels', ())
    }
    expected_pffdtd = {
        float(item['points_per_wavelength']): item
        for item in summary.get('pffdtd_levels', ())
    }
    if set(expected_mfem) != {1, 2, 3}:
        raise ValidationBlocked('PR #286 MFEM baseline schedule is incomplete')
    if set(expected_pffdtd) != {8.0, 10.0, 12.0}:
        raise ValidationBlocked('PR #286 PFFDTD baseline schedule is incomplete')

    details = {'mfem': [], 'pffdtd': []}
    for level in reference_levels:
        refinement = int(level['refinement'])
        actual = np.asarray(level['transfer_pa_per_m3_s'], dtype=np.float64)
        expected = np.asarray(
            expected_mfem[refinement]['transfer_pa_per_m3_s'],
            dtype=np.float64,
        )
        max_abs = float(np.max(np.abs(actual - expected)))
        if not np.allclose(actual, expected, rtol=1.0e-9, atol=1.0e-9):
            raise ValidationBlocked(
                f'MFEM refinement {refinement} did not reproduce PR #286 '
                f'canonical transfer: max_abs={max_abs}'
            )
        details['mfem'].append(
            {'refinement': refinement, 'max_abs_complex_component_error': max_abs}
        )
    for level in pffdtd_levels:
        ppw = float(level['points_per_wavelength'])
        actual = np.asarray(level['transfer_pa_per_m3_s'], dtype=np.float64)
        expected = np.asarray(
            expected_pffdtd[ppw]['transfer_pa_per_m3_s'],
            dtype=np.float64,
        )
        max_abs = float(np.max(np.abs(actual - expected)))
        if not np.allclose(actual, expected, rtol=1.0e-9, atol=1.0e-9):
            raise ValidationBlocked(
                f'PFFDTD {ppw:g} PPW did not reproduce PR #286 canonical '
                f'transfer: max_abs={max_abs}'
            )
        details['pffdtd'].append(
            {'points_per_wavelength': ppw, 'max_abs_complex_component_error': max_abs}
        )
    return {
        'state': 'PASS',
        'baseline_schema_version': summary.get('schema_version'),
        'baseline_workflow_run_id': summary.get('source', {}).get('workflow_run_id'),
        'baseline_artifact_digest_sha256': summary.get('source', {}).get(
            'artifact_digest_sha256'
        ),
        **details,
    }


def _pffdtd_nonmonotonicity_diagnosis(
    canonical_pairs: list[dict[str, Any]],
    aligned_pairs: list[dict[str, Any]],
    diagnostic: dict[str, Any],
) -> dict[str, Any]:
    if len(canonical_pairs) != 2 or len(aligned_pairs) != 2:
        raise ValidationBlocked(
            'PFFDTD non-monotonicity diagnosis requires exactly two adjacent pairs'
        )
    canonical = [
        float(item['metrics']['complex_rms_relative'])
        for item in canonical_pairs
    ]
    aligned = [
        float(item['metrics']['complex_rms_relative'])
        for item in aligned_pairs
    ]
    canonical_ratio = canonical[1] / max(canonical[0], np.finfo(np.float64).tiny)
    aligned_ratio = aligned[1] / max(aligned[0], np.finfo(np.float64).tiny)
    canonical_excess = max(canonical_ratio - 1.0, 0.0)
    aligned_excess = max(aligned_ratio - 1.0, 0.0)
    threshold = float(
        diagnostic['diagnosis_classification']['substantial_reduction_fraction']
    )
    if aligned[1] <= aligned[0]:
        classification = 'ALIGNED_MONOTONIC'
        reduction_fraction = 1.0
    else:
        reduction_fraction = (
            (canonical_excess - aligned_excess) / canonical_excess
            if canonical_excess > 0.0
            else 0.0
        )
        if reduction_fraction >= threshold:
            classification = (
                'ALIGNED_NON_MONOTONICITY_SUBSTANTIALLY_REDUCED'
            )
        else:
            classification = 'ALIGNED_NON_MONOTONICITY_REMAINS'
    return {
        'classification': classification,
        'canonical_adjacent_complex_rms': canonical,
        'aligned_adjacent_complex_rms': aligned,
        'canonical_worsening_ratio': canonical_ratio,
        'aligned_worsening_ratio': aligned_ratio,
        'canonical_worsening_excess': canonical_excess,
        'aligned_worsening_excess': aligned_excess,
        'worsening_excess_reduction_fraction': reduction_fraction,
        'substantial_reduction_threshold_fraction': threshold,
    }


def _blocked_payload(
    plan: R130DGeneral3DValidationPlan,
    *,
    repository_head: str,
    reason: str,
    mfem_build_s: float | None,
    failure_semantic: str = 'EXECUTION_FAILED',
    resource_state: str = 'NOT_RESOURCE_BLOCKED',
) -> dict[str, Any]:
    contract_mismatch = failure_semantic == 'CONTRACT_MISMATCH'
    decision = validation_decision_v2(
        execution_state='PASS' if contract_mismatch else 'EXECUTION_FAILED',
        contract_state='CONTRACT_MISMATCH' if contract_mismatch else 'MATCH',
        reference_assessment=None,
        pffdtd_assessment=None,
        cross_solver_metrics=None,
        plan=plan,
    )
    return {
        'schema_version': EVIDENCE_SCHEMA,
        'plan_id': plan.plan_id,
        'plan_sha256': plan.plan_sha256(),
        'repository_head': repository_head,
        'fixture_id': plan.fixture.fixture_id,
        'fixture_sha256': plan.fixture_sha256(),
        'pffdtd_implementation_sha': plan.pffdtd.source_commit_sha,
        'independent_solver': plan.independent_reference.solver,
        'independent_solver_sha': plan.independent_reference.source_commit_sha,
        'physical_quantity': plan.physical_quantity.model_dump(mode='json'),
        'independence': {
            'reference_spatial_discretization': plan.independent_reference.spatial_discretization,
            'pffdtd_voxel_or_triangle_intersection_reuse': False,
        },
        'failure_semantic': failure_semantic,
        'resource_state': resource_state,
        'execution_error': reason,
        'reference_build_s': mfem_build_s,
        'decision': decision,
        'scope': {
            'validated_fixture': None,
            'general_3d_validation_state': 'NOT_VALIDATED',
            'concave_state': 'CONCAVE_NOT_VALIDATED',
            'multi_region_state': 'MULTI_REGION_NOT_VALIDATED',
            'portal_state': 'PORTAL_NOT_VALIDATED',
            'production_solver_selected': False,
            'owned_room_evidence': False,
            'gpu_validated': False,
        },
        'runtime': {
            'python': platform.python_version(),
            'platform': platform.platform(),
            'numpy': np.__version__,
            'scipy': scipy.__version__,
            'logical_cpus': os.cpu_count(),
        },
        'resource_ceiling': plan.resource_ceiling.model_dump(mode='json'),
        'rdc_calls': 0,
        'htdt_capture_changed': False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run R130D independent sloped-polyhedron validation'
    )
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--diagnostic-plan', required=True, type=Path)
    parser.add_argument('--spatial-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--dense-diagnostic-plan', required=True, type=Path)
    parser.add_argument('--pr286-summary', required=True, type=Path)
    parser.add_argument('--pr295-summary', required=True, type=Path)
    parser.add_argument('--mfem-root', type=Path)
    parser.add_argument('--mfem-executable', type=Path)
    parser.add_argument('--pffdtd-root', type=Path)
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--mfem-build-s', type=float)
    parser.add_argument('--blocked-reason')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    plan = load_validation_plan(args.plan)
    diagnostic = load_target_window_diagnostic_plan(args.diagnostic_plan)
    _validate_target_window_diagnostic_binding(plan, diagnostic)
    spatial_diagnostic = load_spatial_representation_diagnostic_plan(
        args.spatial_diagnostic_plan
    )
    validate_spatial_representation_diagnostic_binding(plan, spatial_diagnostic)
    dense_diagnostic = load_dense_frequency_diagnostic_plan(
        args.dense_diagnostic_plan
    )
    validate_dense_frequency_diagnostic_binding(plan, dense_diagnostic)
    observation_operator_fixture = _run_observation_operator_fixture(diagnostic)
    repository_head = os.environ.get('HTDT_PR_HEAD_SHA', '').strip().lower()
    if not repository_head:
        repository_head = _git_head(Path(__file__).resolve().parents[1])
    pffdtd_levels: list[dict[str, Any]] = []
    reference_levels: list[dict[str, Any]] = []

    if args.blocked_reason:
        save_evidence(
            args.output,
            _blocked_payload(
                plan,
                repository_head=repository_head,
                reason=args.blocked_reason,
                mfem_build_s=args.mfem_build_s,
            ),
        )
        return 0

    if args.mfem_root is None or args.mfem_executable is None or args.pffdtd_root is None:
        raise ValueError('MFEM root/executable and PFFDTD root are required')
    if args.work_root.exists():
        shutil.rmtree(args.work_root)
    args.work_root.mkdir(parents=True)

    try:
        validate_refinement_schedule(
            plan,
            reference_refinements=plan.independent_reference.uniform_refinements,
            pffdtd_points_per_wavelength=plan.pffdtd.points_per_wavelength,
        )
        if np.__version__ != plan.independent_reference.modal_numpy_version:
            raise ValidationBlocked(
                f'NumPy version mismatch: {np.__version__} != {plan.independent_reference.modal_numpy_version}'
            )
        if scipy.__version__ != plan.independent_reference.modal_scipy_version:
            raise ValidationBlocked(
                f'SciPy version mismatch: {scipy.__version__} != {plan.independent_reference.modal_scipy_version}'
            )
        if _git_head(args.mfem_root) != plan.independent_reference.source_commit_sha:
            raise ValidationBlocked('MFEM checkout does not match exact source commit')
        if _git_head(args.pffdtd_root) != plan.pffdtd.source_commit_sha:
            raise ValidationBlocked('PFFDTD checkout does not match exact source commit')

        helper_vertices, helper_faces = _sloped_same_bbox_fixture()
        validate_exact_binding(
            plan,
            vertices_m=helper_vertices,
            faces=helper_faces,
            source_position_m=plan.fixture.source_position_m,
            receiver_position_m=plan.fixture.receiver_position_m,
            quantity=QUANTITY,
            unit=UNIT,
            phasor_convention=PHASOR,
            analysis_fourier_kernel=ANALYSIS_KERNEL,
            pffdtd_source_commit_sha=plan.pffdtd.source_commit_sha,
            independent_source_commit_sha=plan.independent_reference.source_commit_sha,
        )

        fixture = build_r130a_fixture(
            args.work_root / 'pffdtd-fixture',
            args.pffdtd_root,
            boundary_mode='rigid',
            fixture_id='r130d-general3d-independent-validation-v1',
        )
        source_point = fixture['source'].source_acoustic_reference_world_position
        source_position = (
            float(source_point.x_m),
            float(source_point.y_m),
            float(source_point.z_m),
        )
        receiver_point = fixture['snapshot'].receivers[0].world_position
        receiver_position = (
            float(receiver_point.x_m),
            float(receiver_point.y_m),
            float(receiver_point.z_m),
        )
        if source_position != plan.fixture.source_position_m:
            raise ValidationBlocked('PFFDTD source position differs from validation plan')
        if receiver_position != plan.fixture.receiver_position_m:
            raise ValidationBlocked('PFFDTD receiver position differs from validation plan')

        snapshot = fixture['snapshot']
        binding = snapshot.surface_boundary_configuration[0]
        material_ref = binding.material_authority
        rigid_boundary_ref = binding.boundary_physics_authority
        if material_ref is None or rigid_boundary_ref is None:
            raise ValidationBlocked('rigid fixture boundary authority is missing')
        semantic, compiled = _make_polyhedron(
            snapshot=snapshot,
            source_key=plan.fixture.source_key,
            vertices=plan.fixture.vertices_m,
            faces=plan.fixture.faces,
            material_ref=material_ref,
        )
        semantic_ref, compiled_ref = register_r120b_polyhedral_authorities(
            fixture['store'],
            semantic=semantic,
            compiled=compiled,
        )
        compiled_vertices = tuple(
            sorted(tuple(float(value) for value in vertex.point()) for vertex in compiled.vertices)
        )
        planned_vertices = tuple(sorted(plan.fixture.vertices_m))
        if compiled_vertices != planned_vertices:
            raise ValidationBlocked(
                'compiled R120B vertex set differs from validation fixture'
            )
        rigid_boundary = fixture['store'].read_payload(rigid_boundary_ref)
        if rigid_boundary.get('model') != 'rigid_zero_normal_velocity':
            raise ObservableContractMismatch(
                'PFFDTD boundary authority is not rigid zero-normal-velocity'
            )

        expected_contract = _observable_contract(
            plan, geometry_sha256=plan.fixture_sha256()
        )
        mfem_contract = dict(expected_contract)
        pffdtd_contract = dict(expected_contract)
        validate_physical_observable_contract(
            expected=expected_contract, actual=mfem_contract
        )
        validate_physical_observable_contract(
            expected=expected_contract, actual=pffdtd_contract
        )

        pffdtd_executor = PffdtdPolyhedralCandidateWaveExecutor(
            base_executor=fixture['executor'],
            containment_tolerance_m=1.0e-9,
        )
        for ppw in plan.pffdtd.points_per_wavelength:
            pffdtd_levels.append(
                _run_pffdtd_level(
                    plan,
                    fixture=fixture,
                    executor=pffdtd_executor,
                    semantic_ref=semantic_ref,
                    compiled_ref=compiled_ref,
                    rigid_boundary_ref=rigid_boundary_ref,
                    ppw=ppw,
                    spatial_diagnostic=spatial_diagnostic,
                    dense_diagnostic=dense_diagnostic,
                )
            )

        for refinement, expected_elements in zip(
            plan.independent_reference.uniform_refinements,
            plan.independent_reference.expected_element_counts,
        ):
            reference_levels.append(
                _run_reference_level(
                    plan,
                    executable=args.mfem_executable,
                    work_root=args.work_root,
                    refinement=refinement,
                    expected_elements=expected_elements,
                )
            )

        frequencies = plan.physical_quantity.frequency_hz
        mfem_valid_max_hz = min(
            float(level['max_frequency_hz']) for level in reference_levels
        )
        valid_overlap_max_hz = min(
            float(plan.pffdtd.fmax_hz),
            mfem_valid_max_hz,
        )
        if any(float(frequency) > valid_overlap_max_hz for frequency in frequencies):
            raise ValidationBlocked(
                'predeclared comparison bin lies outside the overlapping '
                'MFEM/PFFDTD numerical band'
            )
        valid_overlap_band = {
            'minimum_hz': 0.0,
            'maximum_hz': valid_overlap_max_hz,
            'compared_frequency_hz': [float(x) for x in frequencies],
            'pffdtd_validity_basis': (
                'all compared bins are <= configured PFFDTD fmax_hz; '
                'PPW is specified at fmax'
            ),
            'mfem_validity_basis': (
                'all compared bins are <= the smallest executed full-basis '
                'MFEM maximum modal frequency'
            ),
        }
        canonical_reproduction = _validate_pr286_canonical_reproduction(
            args.pr286_summary,
            reference_levels=reference_levels,
            pffdtd_levels=pffdtd_levels,
        )
        canonical_pr295_reproduction = _validate_pr295_canonical_reproduction(
            args.pr295_summary,
            reference_levels=reference_levels,
            pffdtd_levels=pffdtd_levels,
            max_abs_tolerance=float(
                spatial_diagnostic['canonical_reproduction'][
                    'max_abs_complex_component_tolerance'
                ]
            ),
        )

        diagnostic_frequency_hz = tuple(
            float(x)
            for x in spatial_diagnostic['frequency_neighborhood'][
                'diagnostic_frequency_hz'
            ]
        )
        level_by_ppw = {
            float(level['points_per_wavelength']): np.asarray(
                [
                    complex(float(pair[0]), float(pair[1]))
                    for pair in level['diagnostic_neighborhood_transfer_pa_per_m3_s']
                ],
                dtype=np.complex128,
            )
            for level in pffdtd_levels
        }
        if set(level_by_ppw) != {8.0, 10.0, 12.0}:
            raise ValidationBlocked('diagnostic neighborhood lacks exact 8/10/12 levels')
        fixed_floor = float(
            spatial_diagnostic['frequency_neighborhood']['fixed_floor']
        )
        d_8_10 = [
            normalized_complex_difference(
                level_by_ppw[8.0][index],
                level_by_ppw[10.0][index],
                fixed_floor=fixed_floor,
            )
            for index in range(len(diagnostic_frequency_hz))
        ]
        d_10_12 = [
            normalized_complex_difference(
                level_by_ppw[10.0][index],
                level_by_ppw[12.0][index],
                fixed_floor=fixed_floor,
            )
            for index in range(len(diagnostic_frequency_hz))
        ]
        neighborhood_classification = classify_frequency_neighborhood(
            d_8_10, d_10_12
        )
        spatial_levels = [
            {
                'points_per_wavelength': level['points_per_wavelength'],
                **level['spatial_representation_diagnostic'],
            }
            for level in pffdtd_levels
        ]
        spatial_classification = classify_spatial_representation_trend(
            spatial_levels
        )

        dense_block = dense_diagnostic['dense_frequency_neighborhood']
        dense_frequency_hz = tuple(
            float(x) for x in dense_block['diagnostic_frequency_hz']
        )
        dense_level_by_ppw = {
            float(level['points_per_wavelength']): np.asarray(
                [
                    complex(float(pair[0]), float(pair[1]))
                    for pair in level[
                        'dense_neighborhood_transfer_pa_per_m3_s'
                    ]
                ],
                dtype=np.complex128,
            )
            for level in pffdtd_levels
        }
        if set(dense_level_by_ppw) != {8.0, 10.0, 12.0}:
            raise ValidationBlocked('dense neighborhood lacks exact 8/10/12 levels')
        dense_fixed_floor = float(dense_block['fixed_floor'])
        dense_d_8_10 = [
            normalized_complex_difference(
                dense_level_by_ppw[8.0][index],
                dense_level_by_ppw[10.0][index],
                fixed_floor=dense_fixed_floor,
            )
            for index in range(len(dense_frequency_hz))
        ]
        dense_d_10_12 = [
            normalized_complex_difference(
                dense_level_by_ppw[10.0][index],
                dense_level_by_ppw[12.0][index],
                fixed_floor=dense_fixed_floor,
            )
            for index in range(len(dense_frequency_hz))
        ]
        run76_binding = dense_diagnostic['run76_record_binding']
        identical_label = run76_binding['identical_label']
        run76_record_binding_state = (
            identical_label
            if all(
                level['run76_trace_binding'] == identical_label
                for level in pffdtd_levels
            )
            else run76_binding['nonidentical_label']
        )
        dense_classification = classify_dense_frequency_neighborhood(
            dense_d_8_10, dense_d_10_12
        )
        dense_evaluation_state = 'EVALUATED'
        if run76_record_binding_state != identical_label:
            dense_evaluation_state = 'NOT_EVALUATED_RECORD_BINDING_MISMATCH'
            dense_classification = {
                **dense_classification,
                'classification': dense_block['classification']['not_evaluated'],
            }
        dense_band_of: dict[float, str] = {}
        for band in dense_block['bands']:
            for frequency in dense_frequency_grid([band]):
                dense_band_of[frequency] = str(band['band_id'])
        dense_gap = [b - a for a, b in zip(dense_d_8_10, dense_d_10_12)]
        dense_argmax_index = max(
            range(len(dense_gap)), key=lambda index: dense_gap[index]
        )

        reference_pair_metrics = []
        reference_aligned_pair_metrics = []
        for coarse, fine in zip(reference_levels, reference_levels[1:]):
            canonical_metrics = compare_complex_transfer(
                reference=fine['transfer_pa_per_m3_s'],
                candidate=coarse['transfer_pa_per_m3_s'],
                frequency_hz=frequencies,
                magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
            )
            aligned_metrics = compare_complex_transfer(
                reference=fine['aligned_diagnostic_transfer_pa_per_m3_s'],
                candidate=coarse['aligned_diagnostic_transfer_pa_per_m3_s'],
                frequency_hz=frequencies,
                magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
            )
            reference_pair_metrics.append(
                {
                    'coarse_refinement': coarse['refinement'],
                    'fine_refinement': fine['refinement'],
                    'metrics': _metric_dict(canonical_metrics),
                    'metrics_obj': canonical_metrics,
                }
            )
            reference_aligned_pair_metrics.append(
                {
                    'coarse_refinement': coarse['refinement'],
                    'fine_refinement': fine['refinement'],
                    'metrics': _metric_dict(aligned_metrics),
                    'metrics_obj': aligned_metrics,
                }
            )

        pffdtd_pair_metrics = []
        pffdtd_aligned_pair_metrics = []
        for coarse, fine in zip(pffdtd_levels, pffdtd_levels[1:]):
            canonical_metrics = compare_complex_transfer(
                reference=fine['transfer_pa_per_m3_s'],
                candidate=coarse['transfer_pa_per_m3_s'],
                frequency_hz=frequencies,
                magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
            )
            aligned_metrics = compare_complex_transfer(
                reference=fine['aligned_diagnostic_transfer_pa_per_m3_s'],
                candidate=coarse['aligned_diagnostic_transfer_pa_per_m3_s'],
                frequency_hz=frequencies,
                magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
            )
            pffdtd_pair_metrics.append(
                {
                    'coarse_points_per_wavelength': coarse['points_per_wavelength'],
                    'fine_points_per_wavelength': fine['points_per_wavelength'],
                    'metrics': _metric_dict(canonical_metrics),
                    'metrics_obj': canonical_metrics,
                }
            )
            pffdtd_aligned_pair_metrics.append(
                {
                    'coarse_points_per_wavelength': coarse['points_per_wavelength'],
                    'fine_points_per_wavelength': fine['points_per_wavelength'],
                    'metrics': _metric_dict(aligned_metrics),
                    'metrics_obj': aligned_metrics,
                }
            )

        reference_assessment = assess_refinement_series(
            tuple(item['metrics_obj'] for item in reference_pair_metrics),
            plan.acceptance.reference_self_convergence,
        )
        pffdtd_assessment = assess_refinement_series(
            tuple(item['metrics_obj'] for item in pffdtd_pair_metrics),
            plan.acceptance.pffdtd_self_convergence,
        )
        reference_aligned_assessment = assess_refinement_series(
            tuple(item['metrics_obj'] for item in reference_aligned_pair_metrics),
            plan.acceptance.reference_self_convergence,
        )
        pffdtd_aligned_assessment = assess_refinement_series(
            tuple(item['metrics_obj'] for item in pffdtd_aligned_pair_metrics),
            plan.acceptance.pffdtd_self_convergence,
        )

        pffdtd_sampling_diagnosis = _pffdtd_nonmonotonicity_diagnosis(
            pffdtd_pair_metrics,
            pffdtd_aligned_pair_metrics,
            diagnostic,
        )

        cross_eligible = bool(
            reference_assessment.state == 'SELF_CONVERGENCE_PASS'
            and pffdtd_assessment.state == 'SELF_CONVERGENCE_PASS'
        )
        cross_metrics = None
        accepted_frequencies: list[dict[str, Any]] = []
        if cross_eligible:
            cross_metrics = compare_complex_transfer(
                reference=reference_levels[-1]['transfer_pa_per_m3_s'],
                candidate=pffdtd_levels[-1]['transfer_pa_per_m3_s'],
                frequency_hz=frequencies,
                magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
            )
            accepted_frequencies = _accepted_frequencies(plan, cross_metrics)

        decision = validation_decision_v2(
            execution_state='PASS',
            contract_state='MATCH',
            reference_assessment=reference_assessment,
            pffdtd_assessment=pffdtd_assessment,
            cross_solver_metrics=cross_metrics,
            plan=plan,
        )

        for collection in (
            reference_pair_metrics,
            pffdtd_pair_metrics,
            reference_aligned_pair_metrics,
            pffdtd_aligned_pair_metrics,
        ):
            for item in collection:
                item.pop('metrics_obj', None)

        payload = {
            'schema_version': EVIDENCE_SCHEMA,
            'plan_id': plan.plan_id,
            'plan_sha256': plan.plan_sha256(),
            'diagnostic_plan': {
                'diagnostic_id': diagnostic['diagnostic_id'],
                'schema_version': diagnostic['schema_version'],
                'semantic_sha256': semantic_hash(diagnostic),
                'target_duration_s': diagnostic['target_duration_s'],
                'operator': diagnostic['operator'],
                'decision_semantics': diagnostic['decision_semantics'],
            },
            'diagnostic_observation_operator_validation': (
                observation_operator_fixture
            ),
            'canonical_pr286_reproduction': canonical_reproduction,
            'spatial_diagnostic_plan': {
                'diagnostic_id': spatial_diagnostic['diagnostic_id'],
                'schema_version': spatial_diagnostic['schema_version'],
                'semantic_sha256': semantic_hash(spatial_diagnostic),
                'decision_semantics': spatial_diagnostic['decision_semantics'],
            },
            'dense_diagnostic_plan': {
                'diagnostic_id': dense_diagnostic['diagnostic_id'],
                'schema_version': dense_diagnostic['schema_version'],
                'semantic_sha256': semantic_hash(dense_diagnostic),
                'decision_semantics': dense_diagnostic['decision_semantics'],
            },
            'canonical_pr295_reproduction': canonical_pr295_reproduction,
            'spatial_representation_trend': {
                'levels': spatial_levels,
                **spatial_classification,
            },
            'interpolation_stencil_diagnostic': {
                'levels': [
                    {
                        'points_per_wavelength': level['points_per_wavelength'],
                        'source': level['source_interpolation_stencil'],
                        'receiver': level['receiver_interpolation_stencil'],
                    }
                    for level in pffdtd_levels
                ],
                'source_receiver_positions_unchanged': True,
                'solver_semantics_changed': False,
            },
            'frequency_neighborhood_diagnostic': {
                'frequency_hz': list(diagnostic_frequency_hz),
                'canonical_scored_frequency_hz': [40.0, 80.0],
                'diagnostic_only_frequency_hz': [39.0, 41.0, 79.0, 81.0],
                'normalized_complex_difference_formula': spatial_diagnostic[
                    'frequency_neighborhood'
                ]['normalized_complex_difference_formula'],
                'fixed_floor': fixed_floor,
                'levels': [
                    {
                        'points_per_wavelength': level['points_per_wavelength'],
                        'transfer_pa_per_m3_s': level[
                            'diagnostic_neighborhood_transfer_pa_per_m3_s'
                        ],
                        'transfer_sha256': level[
                            'diagnostic_neighborhood_transfer_sha256'
                        ],
                    }
                    for level in pffdtd_levels
                ],
                'd_8_10': d_8_10,
                'd_10_12': d_10_12,
                'per_frequency': [
                    {
                        'frequency_hz': frequency,
                        'd_8_10': d_8_10[index],
                        'd_10_12': d_10_12[index],
                        'worsening': bool(d_10_12[index] > d_8_10[index]),
                    }
                    for index, frequency in enumerate(diagnostic_frequency_hz)
                ],
                **neighborhood_classification,
                'diagnostic_only': True,
                'canonical_acceptance_inclusion': False,
            },
            'dense_frequency_neighborhood_diagnostic': {
                'frequency_hz': list(dense_frequency_hz),
                'canonical_scored_frequency_hz': list(
                    dense_block['canonical_scored_frequency_hz']
                ),
                'diagnostic_only_frequency_hz': list(
                    dense_block['diagnostic_only_frequency_hz']
                ),
                'normalized_complex_difference_formula': dense_block[
                    'normalized_complex_difference_formula'
                ],
                'fixed_floor': dense_fixed_floor,
                'levels': [
                    {
                        'points_per_wavelength': level['points_per_wavelength'],
                        'transfer_pa_per_m3_s': level[
                            'dense_neighborhood_transfer_pa_per_m3_s'
                        ],
                        'transfer_sha256': level[
                            'dense_neighborhood_transfer_sha256'
                        ],
                        'run76_trace_binding': level['run76_trace_binding'],
                    }
                    for level in pffdtd_levels
                ],
                'd_8_10': dense_d_8_10,
                'd_10_12': dense_d_10_12,
                'per_frequency': [
                    {
                        'frequency_hz': frequency,
                        'band_id': dense_band_of[frequency],
                        'd_8_10': dense_d_8_10[index],
                        'd_10_12': dense_d_10_12[index],
                        'worsening': bool(
                            dense_d_10_12[index] > dense_d_8_10[index]
                        ),
                    }
                    for index, frequency in enumerate(dense_frequency_hz)
                ],
                'band_worsening_counts': {
                    str(band['band_id']): sum(
                        1
                        for index, frequency in enumerate(dense_frequency_hz)
                        if dense_band_of[frequency] == band['band_id']
                        and dense_d_10_12[index] > dense_d_8_10[index]
                    )
                    for band in dense_block['bands']
                },
                'argmax_worsening_gap_frequency_hz': float(
                    dense_frequency_hz[dense_argmax_index]
                ),
                'run76_record_binding': run76_record_binding_state,
                'evaluation_state': dense_evaluation_state,
                **dense_classification,
                'diagnostic_only': True,
                'canonical_acceptance_inclusion': False,
            },
            'repository_head': repository_head,
            'fixture_id': plan.fixture.fixture_id,
            'fixture_sha256': plan.fixture_sha256(),
            'exact_r120b_geometry_refs': {
                'semantic': semantic_ref.model_dump(mode='json'),
                'compiled': compiled_ref.model_dump(mode='json'),
            },
            'source_receiver_binding': {
                'source_entity_id': fixture['source'].source_entity_id,
                'source_position_m': list(plan.fixture.source_position_m),
                'receiver_id': fixture['snapshot'].receivers[0].receiver_id,
                'receiver_position_m': list(plan.fixture.receiver_position_m),
                'physical_boundary_clearance_claim': 'both points are strictly inside the exact polyhedron and away from its physical boundary; R130D exact containment is re-evaluated for every PFFDTD level',
                'reference_mesh_note': 'receiver lies on a conforming internal tetrahedral facet in the audited base partition; continuous H1 point evaluation is single-valued. Any empty/invalid MFEM functional is BLOCKED.',
            },
            'physical_quantity': plan.physical_quantity.model_dump(mode='json'),
            'normalization': {
                'pffdtd': 'stored absolute pressure divided by exact complex Q(f) from AcousticWaveExcitationAuthority',
                'mfem': 'unit discrete volume-velocity impulse transformed with the same dt-weighted exp(+i*omega*t) finite-record kernel',
                'fitted_amplitude_scale': False,
                'fitted_phase_rotation': False,
                'frequency_shift': False,
            },
            'pffdtd_implementation_sha': plan.pffdtd.source_commit_sha,
            'independent_solver': plan.independent_reference.solver,
            'independent_solver_sha': plan.independent_reference.source_commit_sha,
            'independence': {
                'reference_formulation': plan.independent_reference.formulation,
                'reference_spatial_discretization': plan.independent_reference.spatial_discretization,
                'reference_base_tetrahedra': [list(item) for item in plan.fixture.base_tetrahedra],
                'pffdtd_voxel_or_triangle_intersection_reuse': False,
                'shared_code_limited_to_validation_orchestration_and_common_physical_quantity_contract': True,
            },
            'reference_configuration': plan.independent_reference.model_dump(mode='json'),
            'pffdtd_configuration': plan.pffdtd.model_dump(mode='json'),
            'acceptance': plan.acceptance.model_dump(mode='json'),
            'reference_build_s': args.mfem_build_s,
            'reference_levels': reference_levels,
            'pffdtd_levels': pffdtd_levels,
            'reference_pair_metrics': reference_pair_metrics,
            'pffdtd_pair_metrics': pffdtd_pair_metrics,
            'reference_aligned_diagnostic_pair_metrics': (
                reference_aligned_pair_metrics
            ),
            'pffdtd_aligned_diagnostic_pair_metrics': (
                pffdtd_aligned_pair_metrics
            ),
            'reference_self_convergence': reference_assessment.model_dump(mode='json'),
            'pffdtd_self_convergence': pffdtd_assessment.model_dump(mode='json'),
            'reference_aligned_diagnostic_self_convergence': (
                reference_aligned_assessment.model_dump(mode='json')
            ),
            'pffdtd_aligned_diagnostic_self_convergence': (
                pffdtd_aligned_assessment.model_dump(mode='json')
            ),
            'pffdtd_sampling_window_diagnosis': pffdtd_sampling_diagnosis,
            'contract_audit': {
                'state': 'MATCH',
                'expected': expected_contract,
                'mfem': mfem_contract,
                'pffdtd': pffdtd_contract,
                'source_receiver_representation_note': (
                    'Physical point locations are identical. MFEM H1 delta '
                    'functional and PFFDTD trilinear interpolation are distinct '
                    'numerical representations and are recorded, not fitted.'
                ),
            },
            'cross_solver_eligible': cross_eligible,
            'aligned_diagnostic_cross_solver_eligible': False,
            'aligned_diagnostic_cross_solver_nonclaim': (
                'Diagnostic observation-operator results cannot unblock the '
                'canonical cross-solver gate in this slice.'
            ),
            'cross_solver_fine_fine_metrics': (
                None if cross_metrics is None else _metric_dict(cross_metrics)
            ),
            'frequency_acceptance': accepted_frequencies,
            'valid_overlapping_numerical_band': valid_overlap_band,
            'decision': decision,
            'decision_semantics': {
                'solver_execution': decision['execution_state'],
                'canonical_observable_contract': decision['contract_state'],
                'diagnostic_observation_operator_validation': (
                    observation_operator_fixture['state']
                ),
                'canonical_reference_self_convergence': (
                    reference_assessment.state
                ),
                'canonical_pffdtd_self_convergence': pffdtd_assessment.state,
                'canonical_pr295_reproduction': canonical_pr295_reproduction['state'],
                'spatial_representation_trend': spatial_classification['classification'],
                'frequency_neighborhood_sensitivity': (
                    neighborhood_classification['classification']
                ),
                'dense_frequency_neighborhood_sensitivity': (
                    dense_classification['classification']
                ),
                'dense_frequency_record_binding': run76_record_binding_state,
                'interpolation_stencil_diagnostic': 'RECORDED',
                'aligned_reference_self_convergence': (
                    reference_aligned_assessment.state
                ),
                'aligned_pffdtd_self_convergence': (
                    pffdtd_aligned_assessment.state
                ),
                'cross_solver_eligibility': (
                    'ELIGIBLE' if cross_eligible else 'CROSS_SOLVER_BLOCKED'
                ),
                'general_3d_validation_state': (
                    decision['general_3d_validation_state']
                ),
                'diagnostic_does_not_promote_validation': True,
            },
            'scope': {
                'validated_fixture': (
                    plan.fixture.fixture_id
                    if decision['fixture_validation_result'] == 'PASS'
                    else None
                ),
                'general_3d_validation_state': decision['general_3d_validation_state'],
                'concave_state': 'CONCAVE_NOT_VALIDATED',
                'multi_region_state': 'MULTI_REGION_NOT_VALIDATED',
                'portal_state': 'PORTAL_NOT_VALIDATED',
                'production_solver_selected': False,
                'owned_room_evidence': False,
                'gpu_validated': False,
            },
            'runtime': {
                'python': platform.python_version(),
                'platform': platform.platform(),
                'numpy': np.__version__,
                'scipy': scipy.__version__,
                'logical_cpus': os.cpu_count(),
            },
            'resource_ceiling': plan.resource_ceiling.model_dump(mode='json'),
            'rdc_calls': 0,
            'htdt_capture_changed': False,
        }
        save_evidence(args.output, payload)
        return 0
    except ObservableContractMismatch as exc:
        payload = _blocked_payload(
            plan,
            repository_head=repository_head,
            reason=f'{type(exc).__name__}: {exc}',
            mfem_build_s=args.mfem_build_s,
            failure_semantic='CONTRACT_MISMATCH',
        )
        payload['partial_reference_levels'] = reference_levels
        payload['partial_pffdtd_levels'] = pffdtd_levels
        payload['traceback_tail'] = traceback.format_exc()[-6000:]
        save_evidence(args.output, payload)
        return 0
    except ResourceBlocked as exc:
        payload = _blocked_payload(
            plan,
            repository_head=repository_head,
            reason=f'{type(exc).__name__}: {exc}',
            mfem_build_s=args.mfem_build_s,
            failure_semantic='EXECUTION_FAILED',
            resource_state='RESOURCE_BLOCKED',
        )
        payload['partial_reference_levels'] = reference_levels
        payload['partial_pffdtd_levels'] = pffdtd_levels
        payload['traceback_tail'] = traceback.format_exc()[-6000:]
        save_evidence(args.output, payload)
        return 0
    except CandidateWaveExecutionError as exc:
        message = str(exc).lower()
        is_resource = any(
            marker in message
            for marker in (
                'bounded resource contract',
                'bounded wall-time contract',
                'exceeds bounded',
                'time steps exceed',
                'grid exceeds',
                'raw output exceeds',
            )
        )
        is_contract = any(
            marker in message
            for marker in (
                'differs from exact htdt authority',
                'mapping differs',
                'authority is incompatible',
            )
        )
        payload = _blocked_payload(
            plan,
            repository_head=repository_head,
            reason=f'{type(exc).__name__}: {exc}',
            mfem_build_s=args.mfem_build_s,
            failure_semantic=(
                'CONTRACT_MISMATCH'
                if is_contract
                else 'EXECUTION_FAILED'
            ),
            resource_state=(
                'RESOURCE_BLOCKED' if is_resource else 'NOT_RESOURCE_BLOCKED'
            ),
        )
        payload['partial_reference_levels'] = reference_levels
        payload['partial_pffdtd_levels'] = pffdtd_levels
        payload['traceback_tail'] = traceback.format_exc()[-6000:]
        save_evidence(args.output, payload)
        return 0
    except (
        ValidationBlocked,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        payload = _blocked_payload(
            plan,
            repository_head=repository_head,
            reason=f'{type(exc).__name__}: {exc}',
            mfem_build_s=args.mfem_build_s,
            failure_semantic='EXECUTION_FAILED',
        )
        payload['partial_reference_levels'] = reference_levels
        payload['partial_pffdtd_levels'] = pffdtd_levels
        payload['traceback_tail'] = traceback.format_exc()[-6000:]
        save_evidence(args.output, payload)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
