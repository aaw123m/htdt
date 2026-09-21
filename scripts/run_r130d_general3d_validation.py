from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
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
from htdt.acoustic_pffdtd_polyhedral_geometry import (
    PffdtdPolyhedralCandidateWaveExecutor,
    register_r120b_polyhedral_authorities,
)
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
    compare_complex_transfer,
    load_target_window_diagnostic_plan,
    load_validation_plan,
    native_window_left_rectangle_transfer,
    save_evidence,
    semantic_hash,
    target_window_sampling_metadata,
    target_window_clipped_left_rectangle_transfer,
    validate_exact_binding,
    validate_physical_observable_contract,
    validate_refinement_schedule,
    validation_decision_v2,
)

from run_r130a_candidate_wave_execution import _fixture as build_r130a_fixture
from run_r130d_polyhedral_candidate_wave_execution import (
    _make_polyhedron,
    _restore_pinned_pffdtd_checkout,
    _result_evidence,
    _sloped_same_bbox_fixture,
)


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
    command = [
        str(executable),
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
    ]
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


def _run_pffdtd_level(
    plan: R130DGeneral3DValidationPlan,
    *,
    fixture: dict[str, Any],
    executor: PffdtdPolyhedralCandidateWaveExecutor,
    semantic_ref,
    compiled_ref,
    rigid_boundary_ref,
    ppw: float,
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
        'sampling_metadata': sampling_metadata,
        'diagnostic_raw_trace': {
            'sim_outs_sha256': _sha256_file(raw_output_path),
            'comms_out_sha256': _sha256_file(comms_path),
            'pressure_trace_sha256': semantic_hash(
                [float(x) for x in pressure_trace]
            ),
            'source_trace_sha256': semantic_hash(
                [float(x) for x in source_trace]
            ),
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
    parser.add_argument('--pr286-summary', required=True, type=Path)
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
