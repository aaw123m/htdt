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
    R130DGeneral3DValidationPlan,
    compare_complex_transfer,
    load_validation_plan,
    save_evidence,
    semantic_hash,
    validate_exact_binding,
    validate_refinement_schedule,
    validation_decision,
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
    estimated_dense_bytes = 6 * ndofs * ndofs * 8
    if estimated_dense_bytes > (
        plan.resource_ceiling.max_reference_peak_ram_mb * 1024.0 * 1024.0
    ):
        raise ValidationBlocked(
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
        raise ValidationBlocked(
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
        raise ValidationBlocked(
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
    rss_final_mb = process.memory_info().rss / (1024.0 * 1024.0)
    checkpoint_rss_max_mb = max(rss_before_mb, rss_after_eigen_mb, rss_final_mb)
    if checkpoint_rss_max_mb > plan.resource_ceiling.max_reference_peak_ram_mb:
        raise ValidationBlocked(
            'MFEM observed checkpoint RSS exceeds predeclared RAM ceiling'
        )

    transfer_pairs = _complex_pairs(transfer)
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
        'frequency_hz': list(plan.physical_quantity.frequency_hz),
        'transfer_pa_per_m3_s': transfer_pairs,
        'transfer_sha256': semantic_hash(transfer_pairs),
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
    _restore_pinned_pffdtd_checkout(fixture['executor'])
    result = executor.execute(
        dispatch_binding_id=dispatch.binding_id,
        configuration=configuration,
        semantic_geometry_ref=semantic_ref,
        compiled_geometry_ref=compiled_ref,
        rigid_boundary_physics_ref=rigid_boundary_ref,
    )
    evidence = _result_evidence(fixture, result)
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
        'boundary_mask_logical_sha256': evidence['boundary_mask_logical_sha256'],
        'cart_grid_logical_sha256': evidence['cart_grid_logical_sha256'],
        'boundary_node_count': evidence['boundary_node_count'],
        'resource_estimate_ref': evidence['resource_estimate_ref'],
        'resource_estimate': evidence['resource_estimate'],
        'timings_s': evidence['timings_s'],
        'raw_solver_asset_sha256': evidence['raw_solver_asset_sha256'],
        'frequency_hz': list(plan.physical_quantity.frequency_hz),
        'absolute_pressure_pa': _complex_pairs(pressure),
        'exact_excitation_q_m3_s': _complex_pairs(q),
        'transfer_pa_per_m3_s': transfer_pairs,
        'transfer_sha256': semantic_hash(transfer_pairs),
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


def _blocked_payload(
    plan: R130DGeneral3DValidationPlan,
    *,
    repository_head: str,
    reason: str,
    mfem_build_s: float | None,
) -> dict[str, Any]:
    decision = validation_decision(
        execution_status='BLOCKED',
        reference_metrics=None,
        pffdtd_metrics=None,
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
        'execution_error': reason,
        'reference_build_s': mfem_build_s,
        'decision': decision,
        'scope': {
            'validated_fixture': None,
            'concave_state': 'CONCAVE_NOT_VALIDATED',
            'multi_region_state': 'MULTI_REGION_NOT_VALIDATED',
            'portal_state': 'PORTAL_NOT_VALIDATED',
            'production_solver_selected': False,
        },
        'rdc_calls': 0,
        'htdt_capture_changed': False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run R130D independent sloped-polyhedron validation'
    )
    parser.add_argument('--plan', required=True, type=Path)
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
    repository_head = os.environ.get('HTDT_PR_HEAD_SHA', '').strip().lower()
    if not repository_head:
        repository_head = _git_head(Path(__file__).resolve().parents[1])

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

        pffdtd_executor = PffdtdPolyhedralCandidateWaveExecutor(
            base_executor=fixture['executor'],
            containment_tolerance_m=1.0e-9,
        )
        pffdtd_levels = [
            _run_pffdtd_level(
                plan,
                fixture=fixture,
                executor=pffdtd_executor,
                semantic_ref=semantic_ref,
                compiled_ref=compiled_ref,
                rigid_boundary_ref=rigid_boundary_ref,
                ppw=ppw,
            )
            for ppw in plan.pffdtd.points_per_wavelength
        ]

        reference_levels = [
            _run_reference_level(
                plan,
                executable=args.mfem_executable,
                work_root=args.work_root,
                refinement=refinement,
                expected_elements=expected_elements,
            )
            for refinement, expected_elements in zip(
                plan.independent_reference.uniform_refinements,
                plan.independent_reference.expected_element_counts,
            )
        ]

        frequencies = plan.physical_quantity.frequency_hz
        reference_pair_metrics = []
        for coarse, fine in zip(reference_levels, reference_levels[1:]):
            metrics = compare_complex_transfer(
                reference=fine['transfer_pa_per_m3_s'],
                candidate=coarse['transfer_pa_per_m3_s'],
                frequency_hz=frequencies,
                magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
            )
            reference_pair_metrics.append(
                {
                    'coarse_refinement': coarse['refinement'],
                    'fine_refinement': fine['refinement'],
                    'metrics': _metric_dict(metrics),
                }
            )

        pffdtd_pair_metrics = []
        for coarse, fine in zip(pffdtd_levels, pffdtd_levels[1:]):
            metrics = compare_complex_transfer(
                reference=fine['transfer_pa_per_m3_s'],
                candidate=coarse['transfer_pa_per_m3_s'],
                frequency_hz=frequencies,
                magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
            )
            pffdtd_pair_metrics.append(
                {
                    'coarse_points_per_wavelength': coarse['points_per_wavelength'],
                    'fine_points_per_wavelength': fine['points_per_wavelength'],
                    'metrics': _metric_dict(metrics),
                }
            )

        cross_metrics = compare_complex_transfer(
            reference=reference_levels[-1]['transfer_pa_per_m3_s'],
            candidate=pffdtd_levels[-1]['transfer_pa_per_m3_s'],
            frequency_hz=frequencies,
            magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
        )
        reference_final = compare_complex_transfer(
            reference=reference_levels[-1]['transfer_pa_per_m3_s'],
            candidate=reference_levels[-2]['transfer_pa_per_m3_s'],
            frequency_hz=frequencies,
            magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
        )
        pffdtd_final = compare_complex_transfer(
            reference=pffdtd_levels[-1]['transfer_pa_per_m3_s'],
            candidate=pffdtd_levels[-2]['transfer_pa_per_m3_s'],
            frequency_hz=frequencies,
            magnitude_mask_relative_db=plan.acceptance.magnitude_mask_relative_db,
        )
        decision = validation_decision(
            execution_status='PASS',
            reference_metrics=reference_final,
            pffdtd_metrics=pffdtd_final,
            cross_solver_metrics=cross_metrics,
            plan=plan,
        )
        accepted_frequencies = _accepted_frequencies(plan, cross_metrics)

        payload = {
            'schema_version': EVIDENCE_SCHEMA,
            'plan_id': plan.plan_id,
            'plan_sha256': plan.plan_sha256(),
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
            'cross_solver_fine_fine_metrics': _metric_dict(cross_metrics),
            'frequency_acceptance': accepted_frequencies,
            'decision': decision,
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
    except (
        ValidationBlocked,
        CandidateWaveExecutionError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        payload = _blocked_payload(
            plan,
            repository_head=repository_head,
            reason=f'{type(exc).__name__}: {exc}',
            mfem_build_s=args.mfem_build_s,
        )
        payload['traceback_tail'] = traceback.format_exc()[-6000:]
        save_evidence(args.output, payload)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
