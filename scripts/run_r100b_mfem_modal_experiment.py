from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import time

import numpy as np
import psutil
import scipy
from scipy import linalg

from htdt.acoustic_bakeoff import load_bakeoff_candidate_manifest
from htdt.acoustic_bakeoff_mfem_modal_experiment import (
    ExperimentAttemptResult,
    MfemModalExperimentPlan,
    PairMetrics,
    evaluate_modal_experiment,
    load_experiment_plan,
    semantic_hash,
    validate_exact_authority_binding,
)
from htdt.acoustic_bakeoff_readiness import candidate_semantic_hash
from htdt.acoustic_benchmark import (
    canonical_benchmark_json,
    load_acoustic_benchmark_manifest,
)

import run_r100b_mfem_finite_record_reference as baseline


ARTIFACT_SCHEMA = 'r100b-mfem-modal-experiment-artifact-1'
SYSTEM_SCHEMA = 'r100b-mfem-concave-semidiscrete-system-1'
RAW_SCHEMA = 'r100b-mfem-modal-finite-record-raw-1'


class ExperimentBlocked(RuntimeError):
    pass


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
        item.stat().st_size for item in path.rglob('*') if item.is_file()
    ) / (1024.0 * 1024.0)


def _fixture_hash(fixture) -> str:
    return sha256(
        canonical_benchmark_json(fixture.model_dump(mode='json')).encode('utf-8')
    ).hexdigest()


def _validate_plan_against_current_authority(
    plan: MfemModalExperimentPlan,
    benchmark,
    candidates,
):
    fixture_by_id = {item.fixture_id: item for item in benchmark.fixtures}
    fixture = fixture_by_id.get(plan.authority.fixture_id)
    if fixture is None:
        raise ValueError('planned concave fixture is absent from current R100A')
    candidate_by_id = {item.candidate_id: item for item in candidates.candidates}
    candidate = candidate_by_id.get(plan.authority.candidate_id)
    if candidate is None:
        raise ValueError('planned MFEM candidate is absent from current candidate manifest')

    validate_exact_authority_binding(
        plan,
        r100a_manifest_id=benchmark.manifest_id,
        r100a_semantic_hash=benchmark.semantic_hash(),
        candidate_manifest_hash=candidates.semantic_hash(),
        candidate_id=candidate.candidate_id,
        candidate_semantic_hash=candidate_semantic_hash(candidate),
        candidate_source_commit_sha=candidate.source_commit_sha,
    )

    source = fixture.sources[0]
    comparison = fixture.comparison
    magnitude = next(item for item in fixture.observables if item.observable_id == 'lroom-fr')
    phase = next(item for item in fixture.observables if item.observable_id == 'lroom-phase')
    contract = plan.numerical_contract

    exact_checks = (
        ('source_normalization', contract.source_normalization, source.normalization),
        ('coordinate_system', contract.coordinate_system, comparison.coordinate_system),
        ('interpolation', contract.interpolation, comparison.interpolation),
        ('fourier_sign', contract.fourier_sign, comparison.fourier_sign),
        (
            'finite_record_dtft_kernel',
            contract.finite_record_dtft_kernel,
            comparison.finite_record_transfer.dtft_kernel,
        ),
        ('record_interval', contract.record_interval, comparison.finite_record_transfer.record_interval),
    )
    for name, actual, expected in exact_checks:
        if actual != expected:
            raise ValueError(f'plan {name} differs from current R100A: {actual} != {expected}')

    numeric_checks = (
        ('observation_time_s', contract.observation_time_s, comparison.observation_time_s),
        ('frequency_start_hz', contract.frequency_start_hz, comparison.frequency_grid.start_hz),
        ('frequency_stop_hz', contract.frequency_stop_hz, comparison.frequency_grid.stop_hz),
        ('frequency_step_hz', contract.frequency_step_hz, comparison.frequency_grid.step_hz),
        ('magnitude_null_mask_below_db', contract.magnitude_null_mask_below_db, magnitude.tolerance.null_mask_below_db),
        ('phase_null_mask_below_db', contract.phase_null_mask_below_db, phase.tolerance.null_mask_below_db),
        ('magnitude_absolute_tolerance_db', contract.magnitude_absolute_tolerance_db, magnitude.tolerance.absolute),
        ('magnitude_relative_tolerance', contract.magnitude_relative_tolerance, magnitude.tolerance.relative),
        ('phase_tolerance_deg', contract.phase_tolerance_deg, phase.tolerance.phase_deg),
    )
    for name, actual, expected in numeric_checks:
        if not baseline._float_close(float(actual), float(expected)):
            raise ValueError(f'plan {name} differs from current R100A: {actual} != {expected}')

    budget = fixture.resource_budget
    resource_checks = (
        ('cpu_thread_budget', plan.resource_ceiling.cpu_thread_budget, budget.cpu_thread_budget),
        ('ram_budget_mb', plan.resource_ceiling.ram_budget_mb, budget.ram_budget_mb),
        ('disk_budget_mb', plan.resource_ceiling.disk_budget_mb, budget.disk_budget_mb),
        ('max_solve_s_per_attempt', plan.resource_ceiling.max_solve_s_per_attempt, budget.max_solve_s),
        ('max_output_mb_per_attempt', plan.resource_ceiling.max_output_mb_per_attempt, budget.max_output_mb),
    )
    for name, actual, expected in resource_checks:
        if not baseline._float_close(float(actual), float(expected)):
            raise ValueError(f'plan resource {name} differs from current R100A budget')

    return fixture, candidate


def _run_system_export(
    plan: MfemModalExperimentPlan,
    fixture,
    executable: Path,
    work_dir: Path,
) -> tuple[Path, str]:
    system_path = work_dir / 'semidiscrete_system.json'
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
        '--sample-rate-hz', str(plan.attempts[-1].sample_rate_hz),
        '--order', str(plan.spatial_system.h1_order),
        '--uniform-refinements', str(plan.spatial_system.uniform_refinements),
        '--system-output', str(system_path),
        '--assemble-only',
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=plan.resource_ceiling.subprocess_wall_timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExperimentBlocked(
            f'semidiscrete system export exceeded {plan.resource_ceiling.subprocess_wall_timeout_s} s'
        ) from exc
    if completed.returncode != 0:
        raise ExperimentBlocked(
            f'semidiscrete system export failed with code {completed.returncode}: '
            f'{completed.stdout[-3000:]}'
        )
    if not system_path.is_file():
        raise ExperimentBlocked('semidiscrete system export completed without output')
    return system_path, completed.stdout[-6000:]


def _csr_dense(payload: dict[str, object], ndofs: int, name: str) -> np.ndarray:
    try:
        rows = int(payload['rows'])
        cols = int(payload['cols'])
        nnz = int(payload['nnz'])
        row_offsets = np.asarray(payload['row_offsets'], dtype=np.int64)
        column_indices = np.asarray(payload['column_indices'], dtype=np.int64)
        values = np.asarray(payload['values'], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as exc:
        raise ExperimentBlocked(f'{name} CSR payload is invalid') from exc
    if rows != ndofs or cols != ndofs:
        raise ExperimentBlocked(f'{name} dimensions differ from frozen ndofs')
    if row_offsets.shape != (ndofs + 1,) or column_indices.shape != (nnz,) or values.shape != (nnz,):
        raise ExperimentBlocked(f'{name} CSR array sizes are inconsistent')
    if row_offsets[0] != 0 or row_offsets[-1] != nnz or np.any(np.diff(row_offsets) < 0):
        raise ExperimentBlocked(f'{name} CSR row offsets are invalid')
    if np.any(column_indices < 0) or np.any(column_indices >= ndofs):
        raise ExperimentBlocked(f'{name} CSR column indices are invalid')
    if not np.all(np.isfinite(values)):
        raise ExperimentBlocked(f'{name} contains non-finite coefficients')

    dense = np.zeros((ndofs, ndofs), dtype=np.float64)
    for row in range(ndofs):
        start = int(row_offsets[row])
        stop = int(row_offsets[row + 1])
        dense[row, column_indices[start:stop]] += values[start:stop]
    return dense


def _validate_system(
    plan: MfemModalExperimentPlan,
    fixture,
    path: Path,
) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    spatial = plan.spatial_system
    exact = (
        ('schema_version', payload.get('schema_version'), SYSTEM_SCHEMA),
        ('fixture_id', payload.get('fixture_id'), plan.authority.fixture_id),
        ('geometry', payload.get('geometry'), spatial.geometry),
        ('boundary_model', payload.get('boundary_model'), spatial.boundary_model),
        ('primary_field', payload.get('primary_field'), spatial.primary_field),
        ('governing_equation', payload.get('governing_equation'), spatial.governing_equation),
        ('mass_assembly', payload.get('mass_assembly'), spatial.mass_assembly),
        ('stiffness_assembly', payload.get('stiffness_assembly'), spatial.stiffness_assembly),
        ('source_functional_assembly', payload.get('source_functional_assembly'), spatial.source_functional),
        ('receiver_functional_assembly', payload.get('receiver_functional_assembly'), spatial.receiver_functional),
        ('matrix_format', payload.get('matrix_format'), 'csr_full'),
    )
    for name, actual, expected in exact:
        if actual != expected:
            raise ExperimentBlocked(f'semidiscrete system {name} mismatch: {actual} != {expected}')

    integer_checks = (
        ('order', spatial.h1_order),
        ('uniform_refinements', spatial.uniform_refinements),
        ('elements', spatial.expected_elements),
        ('ndofs', spatial.expected_ndofs),
    )
    for name, expected in integer_checks:
        if int(payload.get(name, -1)) != int(expected):
            raise ExperimentBlocked(f'semidiscrete system {name} differs from frozen p2/h1 binding')

    for name, expected in (
        ('density_kg_m3', float(fixture.environment.density_kg_m3)),
        ('sound_speed_m_s', float(fixture.environment.sound_speed_m_s)),
    ):
        if not baseline._float_close(float(payload.get(name, math.nan)), expected):
            raise ExperimentBlocked(f'semidiscrete system {name} differs from R100A')

    for name, position in (
        ('source_position_m', fixture.sources[0].position),
        ('receiver_position_m', fixture.receivers[0].position),
    ):
        actual = payload.get(name)
        expected = list(baseline._position_tuple(position))
        if not isinstance(actual, list) or len(actual) != 3:
            raise ExperimentBlocked(f'semidiscrete system {name} missing')
        if any(
            not baseline._float_close(float(a), float(e))
            for a, e in zip(actual, expected)
        ):
            raise ExperimentBlocked(f'semidiscrete system {name} differs from R100A')

    if payload.get('source_normalization') != fixture.sources[0].normalization:
        raise ExperimentBlocked('semidiscrete source normalization differs from R100A')

    ndofs = spatial.expected_ndofs
    mass = _csr_dense(payload['mass_matrix'], ndofs, 'mass_matrix')
    stiffness = _csr_dense(payload['stiffness_c2_matrix'], ndofs, 'stiffness_c2_matrix')
    source = np.asarray(payload.get('source_functional'), dtype=np.float64)
    receiver = np.asarray(payload.get('receiver_functional'), dtype=np.float64)
    if source.shape != (ndofs,) or receiver.shape != (ndofs,):
        raise ExperimentBlocked('source/receiver functionals differ from frozen ndofs')
    if not np.all(np.isfinite(source)) or not np.all(np.isfinite(receiver)):
        raise ExperimentBlocked('source/receiver functionals contain non-finite values')

    mass_symmetry = float(np.max(np.abs(mass - mass.T)))
    stiffness_symmetry = float(np.max(np.abs(stiffness - stiffness.T)))
    if mass_symmetry > 1e-12 or stiffness_symmetry > 1e-8:
        raise ExperimentBlocked(
            f'exported semidiscrete matrices are not symmetric: M={mass_symmetry}, K={stiffness_symmetry}'
        )

    numeric_identity = semantic_hash(
        {
            'schema_version': SYSTEM_SCHEMA,
            'mfem_version': payload.get('mfem_version'),
            'order': payload['order'],
            'uniform_refinements': payload['uniform_refinements'],
            'elements': payload['elements'],
            'ndofs': payload['ndofs'],
            'density_kg_m3': payload['density_kg_m3'],
            'sound_speed_m_s': payload['sound_speed_m_s'],
            'source_position_m': payload['source_position_m'],
            'receiver_position_m': payload['receiver_position_m'],
            'mass_matrix': payload['mass_matrix'],
            'stiffness_c2_matrix': payload['stiffness_c2_matrix'],
            'source_functional': payload['source_functional'],
            'receiver_functional': payload['receiver_functional'],
        }
    )
    return {
        'payload': payload,
        'mass': mass,
        'stiffness': stiffness,
        'source': source,
        'receiver': receiver,
        'numeric_identity_sha256': numeric_identity,
        'mass_symmetry_max_abs': mass_symmetry,
        'stiffness_symmetry_max_abs': stiffness_symmetry,
    }


def _modal_decomposition(
    plan: MfemModalExperimentPlan,
    system: dict[str, object],
) -> dict[str, object]:
    if scipy.__version__ != plan.modal_configuration.eigen_library_version:
        raise ExperimentBlocked(
            f'SciPy version mismatch: {scipy.__version__} != '
            f'{plan.modal_configuration.eigen_library_version}'
        )
    if np.__version__ != plan.modal_configuration.numpy_version:
        raise ExperimentBlocked(
            f'NumPy version mismatch: {np.__version__} != '
            f'{plan.modal_configuration.numpy_version}'
        )

    mass = system['mass']
    stiffness = system['stiffness']
    started = time.perf_counter()
    try:
        eigenvalues, eigenvectors = linalg.eigh(
            stiffness,
            mass,
            type=1,
            driver=plan.modal_configuration.generalized_eigh_driver,
            check_finite=True,
            overwrite_a=False,
            overwrite_b=False,
        )
    except Exception as exc:
        raise ExperimentBlocked(f'generalized eigen solve failed: {type(exc).__name__}: {exc}') from exc
    eigen_solve_s = time.perf_counter() - started

    if eigenvalues.shape != (plan.modal_configuration.basis_size,):
        raise ExperimentBlocked('generalized eigen solve did not return the full frozen basis')
    if eigenvectors.shape != (
        plan.modal_configuration.basis_size,
        plan.modal_configuration.basis_size,
    ):
        raise ExperimentBlocked('generalized eigenvector matrix does not match full basis')
    if not np.all(np.isfinite(eigenvalues)) or not np.all(np.isfinite(eigenvectors)):
        raise ExperimentBlocked('generalized eigen decomposition contains non-finite values')

    max_abs_lambda = float(np.max(np.abs(eigenvalues)))
    negative_threshold = (
        plan.modal_configuration.negative_eigenvalue_relative_tolerance
        * max(1.0, max_abs_lambda)
    )
    min_lambda = float(np.min(eigenvalues))
    if min_lambda < -negative_threshold:
        raise ExperimentBlocked(
            f'generalized eigenproblem has materially negative lambda {min_lambda} '
            f'below {-negative_threshold}'
        )
    clamped = np.maximum(eigenvalues, 0.0)

    gram = eigenvectors.T @ mass @ eigenvectors
    orth_error = float(np.max(np.abs(gram - np.eye(gram.shape[0]))))
    if orth_error > plan.modal_configuration.mass_orthonormality_max_abs_tolerance:
        raise ExperimentBlocked(
            f'mass-normalized eigenbasis error {orth_error} exceeds frozen tolerance'
        )

    kv = stiffness @ eigenvectors
    mvl = (mass @ eigenvectors) * eigenvalues[np.newaxis, :]
    residual = float(
        np.linalg.norm(kv - mvl, ord='fro')
        / max(
            float(np.linalg.norm(kv, ord='fro')),
            float(np.linalg.norm(mvl, ord='fro')),
            1.0,
        )
    )
    if residual > plan.modal_configuration.generalized_eigen_residual_relative_tolerance:
        raise ExperimentBlocked(
            f'generalized eigen residual {residual} exceeds frozen tolerance'
        )

    return {
        'eigenvalues': clamped,
        'eigenvectors': eigenvectors,
        'raw_min_eigenvalue': min_lambda,
        'raw_max_eigenvalue': float(np.max(eigenvalues)),
        'negative_clamp_threshold': negative_threshold,
        'clamped_negative_count': int(np.count_nonzero(eigenvalues < 0.0)),
        'mass_orthonormality_max_abs_error': orth_error,
        'generalized_eigen_residual_relative': residual,
        'eigen_solve_s': eigen_solve_s,
        'basis_size': int(eigenvalues.size),
        'retained_basis_size': int(eigenvalues.size),
        'min_frequency_hz': float(np.sqrt(clamped[0]) / (2.0 * np.pi)),
        'max_frequency_hz': float(np.sqrt(clamped[-1]) / (2.0 * np.pi)),
    }


def _reconstruct_attempt(
    plan: MfemModalExperimentPlan,
    fixture,
    system: dict[str, object],
    modal: dict[str, object],
    spec,
    work_dir: Path,
) -> tuple[ExperimentAttemptResult, dict[str, object]]:
    dt_s = 1.0 / float(spec.sample_rate_hz)
    sample_count = int(round(float(fixture.comparison.observation_time_s) * spec.sample_rate_hz))
    source_amplitude = float(fixture.sources[0].amplitude)
    sound_speed = float(fixture.environment.sound_speed_m_s)
    density = float(fixture.environment.density_kg_m3)
    mass = system['mass']
    source = system['source']
    receiver = system['receiver']
    eigenvalues = modal['eigenvalues']
    eigenvectors = modal['eigenvectors']

    started = time.perf_counter()
    rhs = (sound_speed * sound_speed * dt_s * source_amplitude) * source
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
        raise ExperimentBlocked(f'initial mass solve failed: {type(exc).__name__}: {exc}') from exc
    mass_residual = float(
        np.linalg.norm(mass @ phi_t0 - rhs)
        / max(float(np.linalg.norm(rhs)), 1e-30)
    )
    if mass_residual > 1e-10:
        raise ExperimentBlocked(
            f'initial mass solve residual {mass_residual} exceeds controlled modal tolerance'
        )

    modal_velocity = eigenvectors.T @ (mass @ phi_t0)
    receiver_projection = receiver @ eigenvectors
    amplitudes = density * receiver_projection * modal_velocity
    omega = np.sqrt(eigenvalues)

    pressures = np.empty(sample_count, dtype=np.float64)
    chunk_size = 1024
    for start in range(0, sample_count, chunk_size):
        stop = min(start + chunk_size, sample_count)
        times = np.arange(start, stop, dtype=np.float64) * dt_s
        pressures[start:stop] = np.cos(np.outer(times, omega)) @ amplitudes
    if not np.all(np.isfinite(pressures)):
        raise ExperimentBlocked('modal reconstruction produced non-finite pressure samples')
    solve_s = time.perf_counter() - started

    source_samples = np.zeros(sample_count, dtype=np.float64)
    source_samples[0] = source_amplitude
    transfer_samples = baseline._direct_dtft_transfer(
        fixture,
        dt_s=dt_s,
        pressures=pressures.tolist(),
        source_samples=source_samples.tolist(),
    )

    deterministic_payload = {
        'system_numeric_identity_sha256': system['numeric_identity_sha256'],
        'modal_configuration_sha256': plan.modal_configuration_hash(),
        'attempt_id': spec.attempt_id,
        'sample_rate_hz': spec.sample_rate_hz,
        'dt_s': dt_s,
        'pressure_pa': pressures.tolist(),
        'source_volume_velocity_m3_s': source_samples.tolist(),
    }
    numerical_identity = semantic_hash(deterministic_payload)

    raw_path = work_dir / f'{spec.attempt_id}.json'
    raw = {
        'schema_version': RAW_SCHEMA,
        'fixture_id': fixture.fixture_id,
        'system_numeric_identity_sha256': system['numeric_identity_sha256'],
        'modal_configuration_sha256': plan.modal_configuration_hash(),
        'time_evolution': 'exact_full_basis_modal_cosine',
        'generalized_eigenproblem': plan.modal_configuration.generalized_eigenproblem,
        'mass_normalization': plan.modal_configuration.mass_normalization,
        'basis_size': plan.modal_configuration.basis_size,
        'retained_basis_size': modal['retained_basis_size'],
        'truncation_rule': plan.modal_configuration.truncation_rule,
        'source_projection': plan.modal_configuration.source_projection,
        'receiver_projection': plan.modal_configuration.receiver_projection,
        'initial_conditions': plan.modal_configuration.initial_conditions,
        'reconstruction': plan.modal_configuration.reconstruction,
        'sample_rate_hz': spec.sample_rate_hz,
        'dt_s': dt_s,
        'record_interval': plan.numerical_contract.record_interval,
        'observation_time_s': float(fixture.comparison.observation_time_s),
        'sample_count': sample_count,
        'last_sample_time_s': (sample_count - 1) * dt_s,
        'source_normalization': fixture.sources[0].normalization,
        'source_t0_s': 0.0,
        'source_mapping': 'phi_t(0+)=c^2*dt*M^-1*b*q[0]',
        'window': 'none',
        'filter': 'none',
        'zero_padding': 'none',
        'source_mass_relative_residual': mass_residual,
        'solve_s': solve_s,
        'numerical_identity_sha256': numerical_identity,
        'samples': [
            {
                'index': index,
                'time_s': index * dt_s,
                'pressure_pa': float(pressures[index]),
                'source_volume_velocity_m3_s': float(source_samples[index]),
            }
            for index in range(sample_count)
        ],
    }
    raw_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    output_mb = raw_path.stat().st_size / (1024.0 * 1024.0)
    rss_mb = psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0)

    status = 'COMPLETED'
    reason_code = 'completed'
    reason = 'full-basis modal attempt completed under frozen authority'
    if solve_s > plan.resource_ceiling.max_solve_s_per_attempt:
        status = 'BLOCKED'
        reason_code = 'solve_resource_ceiling'
        reason = f'modal reconstruction {solve_s} s exceeds frozen solve ceiling'
    elif output_mb > plan.resource_ceiling.max_output_mb_per_attempt:
        status = 'BLOCKED'
        reason_code = 'output_resource_ceiling'
        reason = f'raw output {output_mb} MiB exceeds frozen output ceiling'
    elif rss_mb > plan.resource_ceiling.ram_budget_mb:
        status = 'BLOCKED'
        reason_code = 'ram_resource_ceiling'
        reason = f'observed process RSS {rss_mb} MiB exceeds frozen RAM ceiling'

    result = ExperimentAttemptResult(
        attempt_id=spec.attempt_id,
        status=status,
        reason_code=reason_code,
        reason=reason,
        numerical_identity_sha256=numerical_identity if status == 'COMPLETED' else None,
        sample_rate_hz=spec.sample_rate_hz,
        sample_count=sample_count,
        solve_s=solve_s,
        peak_ram_mb=rss_mb,
        output_mb=output_mb,
        source_mass_relative_residual=mass_residual,
    )
    details = {
        'raw_output_path': raw_path.as_posix(),
        'raw_output_sha256': _sha256_file(raw_path),
        'dt_s': dt_s,
        'transfer_samples': transfer_samples,
    }
    return result, details


def _pair_metrics(
    plan: MfemModalExperimentPlan,
    fixture,
    details: dict[str, dict[str, object]],
) -> tuple[PairMetrics, ...]:
    pairs: list[PairMetrics] = []
    ids = [item.attempt_id for item in plan.attempts]
    by_id = {item.attempt_id: item for item in plan.attempts}
    for coarse_id, fine_id in zip(ids, ids[1:]):
        if coarse_id not in details or fine_id not in details:
            continue
        coarse = details[coarse_id]
        fine = details[fine_id]
        raw = baseline._compare_convergence_pair(
            fixture,
            {
                'level_id': coarse_id,
                'order': plan.spatial_system.h1_order,
                'dt_s': coarse['dt_s'],
                'transfer_samples': coarse['transfer_samples'],
            },
            {
                'level_id': fine_id,
                'order': plan.spatial_system.h1_order,
                'dt_s': fine['dt_s'],
                'transfer_samples': fine['transfer_samples'],
            },
            magnitude_mask_db=plan.numerical_contract.magnitude_null_mask_below_db,
            phase_mask_db=plan.numerical_contract.phase_null_mask_below_db,
        )
        pairs.append(
            PairMetrics(
                coarse_attempt_id=coarse_id,
                fine_attempt_id=fine_id,
                magnitude_max_abs_db=float(raw['magnitude_max_abs_db']),
                magnitude_max_relative=float(raw['magnitude_max_relative']),
                phase_max_error_deg=float(raw['phase_max_error_deg']),
                complex_rms_relative=float(raw['complex_rms_relative']),
                magnitude_sample_count=int(raw['magnitude_sample_count']),
                phase_sample_count=int(raw['phase_sample_count']),
            )
        )
    return tuple(pairs)


def _blocked_attempts(plan: MfemModalExperimentPlan, reason_code: str, reason: str):
    return tuple(
        ExperimentAttemptResult(
            attempt_id=spec.attempt_id,
            status='BLOCKED',
            reason_code=reason_code,
            reason=reason,
            sample_rate_hz=spec.sample_rate_hz,
        )
        for spec in plan.attempts
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Run the frozen R100B MFEM full-basis modal time-evolution experiment'
    )
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--candidates', required=True, type=Path)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--mfem-root', required=True, type=Path)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--native-build-s', type=float)
    parser.add_argument('--blocked-reason')
    args = parser.parse_args(argv)

    plan = load_experiment_plan(args.plan)
    benchmark = load_acoustic_benchmark_manifest(args.manifest)
    candidates = load_bakeoff_candidate_manifest(args.candidates)
    fixture, candidate = _validate_plan_against_current_authority(plan, benchmark, candidates)
    fixture_semantic_hash = _fixture_hash(fixture)

    if baseline._git_head(args.mfem_root) != candidate.source_commit_sha:
        raise SystemExit('MFEM checkout does not match exact candidate source commit')

    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    attempt_results: tuple[ExperimentAttemptResult, ...]
    attempt_details: dict[str, dict[str, object]] = {}
    system_evidence: dict[str, object] = {}
    modal_evidence: dict[str, object] = {}
    setup_stdout_tail = ''

    if args.blocked_reason:
        attempt_results = _blocked_attempts(
            plan, 'experiment_setup_blocked', str(args.blocked_reason)
        )
    elif args.executable is None or not args.executable.is_file():
        attempt_results = _blocked_attempts(
            plan, 'experiment_setup_blocked', 'MFEM finite-record executable missing'
        )
    else:
        try:
            system_path, setup_stdout_tail = _run_system_export(
                plan, fixture, args.executable, args.work_dir
            )
            system = _validate_system(plan, fixture, system_path)
            system_evidence = {
                'raw_path': system_path.as_posix(),
                'raw_sha256': _sha256_file(system_path),
                'numeric_identity_sha256': system['numeric_identity_sha256'],
                'mass_symmetry_max_abs': system['mass_symmetry_max_abs'],
                'stiffness_symmetry_max_abs': system['stiffness_symmetry_max_abs'],
                'ndofs': plan.spatial_system.expected_ndofs,
                'elements': plan.spatial_system.expected_elements,
            }
            modal = _modal_decomposition(plan, system)
            modal_evidence = {
                key: value
                for key, value in modal.items()
                if key not in {'eigenvalues', 'eigenvectors'}
            }
            results: list[ExperimentAttemptResult] = []
            for spec in plan.attempts:
                try:
                    result, details = _reconstruct_attempt(
                        plan, fixture, system, modal, spec, args.work_dir
                    )
                except ExperimentBlocked as exc:
                    result = ExperimentAttemptResult(
                        attempt_id=spec.attempt_id,
                        status='BLOCKED',
                        reason_code='modal_reconstruction_blocked',
                        reason=str(exc),
                        sample_rate_hz=spec.sample_rate_hz,
                    )
                    details = {}
                results.append(result)
                if result.status == 'COMPLETED':
                    attempt_details[spec.attempt_id] = details
            attempt_results = tuple(results)
        except ExperimentBlocked as exc:
            attempt_results = _blocked_attempts(
                plan, 'modal_system_blocked', str(exc)
            )

    pair_metrics = _pair_metrics(plan, fixture, attempt_details)
    decision = evaluate_modal_experiment(
        plan,
        attempt_results=attempt_results,
        pair_metrics=pair_metrics,
    )
    total_disk_mb = _directory_size_mb(args.work_dir)
    htdt = baseline._htdt_git_provenance()

    if decision.outcome == 'PASS':
        next_experiment = (
            'Newmark temporal evolution is now isolated as a major candidate cause. '
            'Test a production-suitable non-dissipative transient integrator against this '
            'full-basis modal reference without changing R100A.'
        )
    elif decision.outcome == 'FAIL':
        next_experiment = (
            'Newmark alone is not sufficient to explain the sample-rate failure. Preserve '
            'the exact p2/h1 modal system and next isolate the sample-rate-dependent source '
            'kick / finite-record source normalization before changing the spatial delta '
            'source/receiver representation or retrying h2.'
        )
    else:
        next_experiment = (
            'Preserve the blocked configuration and remove only the recorded resource/library/'
            'numerical blocker; do not substitute a modal count, cutoff, mesh, or output grid.'
        )

    report = {
        'schema_version': ARTIFACT_SCHEMA,
        'plan': plan.model_dump(mode='json'),
        'plan_file_sha256': _sha256_file(args.plan),
        'authority_binding': {
            'r100a_manifest_id': benchmark.manifest_id,
            'r100a_semantic_hash': benchmark.semantic_hash(),
            'fixture_id': fixture.fixture_id,
            'fixture_semantic_hash': fixture_semantic_hash,
            'candidate_manifest_hash': candidates.semantic_hash(),
            'candidate_id': candidate.candidate_id,
            'candidate_semantic_hash': candidate_semantic_hash(candidate),
            'candidate_source_commit_sha': candidate.source_commit_sha,
        },
        'spatial_system_configuration_sha256': plan.spatial_system_configuration_hash(),
        'modal_configuration_sha256': plan.modal_configuration_hash(),
        'output_grid_configuration_sha256': plan.output_grid_configuration_hash(),
        'htdt_source_commit_sha': htdt['pr_head_commit_sha'],
        'htdt_checkout_commit_sha': htdt['checkout_commit_sha'],
        'mfem_checkout_commit_sha': baseline._git_head(args.mfem_root),
        'native_build_s': args.native_build_s,
        'system_evidence': system_evidence,
        'modal_evidence': modal_evidence,
        'setup_stdout_tail': setup_stdout_tail,
        'decision': decision.model_dump(mode='json'),
        'attempt_details': attempt_details,
        'resource_evidence': {
            'native_build_s': args.native_build_s,
            'experiment_work_disk_mb': total_disk_mb,
            'observed_process_rss_mb': psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0),
            'total_modal_reconstruction_s': sum(
                item.solve_s or 0.0 for item in attempt_results
            ),
            'resource_ceiling': plan.resource_ceiling.model_dump(mode='json'),
        },
        'production_readiness': {
            'decision_before': 'NO_GO',
            'decision_after': 'NO_GO',
            'production_solver_selected': False,
            'readiness_input_promoted': False,
            'reason': (
                'This diagnostic experiment cannot select or promote a production solver. '
                'A modal PASS only isolates Newmark as a candidate cause; FAIL/BLOCKED retains '
                'the existing negative evidence.'
            ),
        },
        'next_minimum_experiment': next_experiment,
        'hard_rule_attestation': {
            'r100a_authority_changed': False,
            'r100a_tolerance_changed': False,
            'spatial_p2_h1_system_changed_between_output_grids': False,
            'modal_count_selected_after_results': False,
            'modal_truncation_used': False,
            'best_trace_selection_used': False,
            'nonconverged_result_promoted': False,
            'production_solver_selected': False,
            'rdc_used': False,
        },
        'runtime': {
            'python_version': platform.python_version(),
            'numpy_version': np.__version__,
            'scipy_version': scipy.__version__,
            'psutil_version': psutil.__version__,
        },
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )

    summary = {
        'outcome': decision.outcome,
        'deterministic_report_identity_sha256': decision.deterministic_report_identity_sha256,
        'system_numeric_identity_sha256': system_evidence.get('numeric_identity_sha256'),
        'modal_evidence': modal_evidence,
        'pair_metrics': [item.model_dump(mode='json') for item in decision.pair_metrics],
        'violations': list(decision.violations),
        'resource_evidence': report['resource_evidence'],
        'next_minimum_experiment': next_experiment,
    }
    print(
        'R100B_MFEM_MODAL_SUMMARY='
        + json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    )
    print(f'R100B_MFEM_MODAL_REPORT={args.output.as_posix()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
