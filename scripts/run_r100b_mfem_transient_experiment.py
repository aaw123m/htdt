from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import time

import numpy as np
import psutil
import scipy
from scipy import sparse
from scipy.sparse import linalg as sparse_linalg

from htdt.acoustic_bakeoff import load_bakeoff_candidate_manifest
from htdt.acoustic_bakeoff_mfem_transient_experiment import (
    ExperimentAttemptResult,
    MfemTransientExperimentPlan,
    ModalReferenceMetrics,
    PairMetrics,
    evaluate_transient_experiment,
    load_experiment_plan,
    semantic_hash,
    validate_modal_reference_system_identity,
)
from htdt.acoustic_bakeoff_readiness import candidate_semantic_hash
from htdt.acoustic_benchmark import canonical_benchmark_json, load_acoustic_benchmark_manifest

import run_r100b_mfem_finite_record_reference as baseline
import run_r100b_mfem_modal_experiment as modal_runner


ARTIFACT_SCHEMA = 'r100b-mfem-transient-experiment-artifact-2'
RAW_SCHEMA = 'r100b-mfem-transient-finite-record-raw-2'


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
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file()) / (
        1024.0 * 1024.0
    )


def _fixture_hash(fixture) -> str:
    return sha256(
        canonical_benchmark_json(fixture.model_dump(mode='json')).encode('utf-8')
    ).hexdigest()


def _csr_matrix(payload: dict[str, object], ndofs: int, name: str) -> sparse.csr_matrix:
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
    matrix = sparse.csr_matrix(
        (values, column_indices, row_offsets),
        shape=(ndofs, ndofs),
        dtype=np.float64,
    )
    matrix.sum_duplicates()
    matrix.sort_indices()
    return matrix


def _symmetry_max_abs(matrix: sparse.csr_matrix) -> float:
    diff = matrix - matrix.T
    return 0.0 if diff.nnz == 0 else float(np.max(np.abs(diff.data)))


def _validate_sparse_system(
    plan: MfemTransientExperimentPlan,
    fixture,
    path: Path,
) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    spatial_plan = plan.spatial_system
    exact = (
        ('schema_version', payload.get('schema_version'), modal_runner.SYSTEM_SCHEMA),
        ('fixture_id', payload.get('fixture_id'), plan.authority.fixture_id),
        ('geometry', payload.get('geometry'), spatial_plan.geometry),
        ('boundary_model', payload.get('boundary_model'), spatial_plan.boundary_model),
        ('primary_field', payload.get('primary_field'), spatial_plan.primary_field),
        ('governing_equation', payload.get('governing_equation'), spatial_plan.governing_equation),
        ('mass_assembly', payload.get('mass_assembly'), spatial_plan.mass_assembly),
        ('stiffness_assembly', payload.get('stiffness_assembly'), spatial_plan.stiffness_assembly),
        ('source_functional_assembly', payload.get('source_functional_assembly'), spatial_plan.source_functional),
        ('receiver_functional_assembly', payload.get('receiver_functional_assembly'), spatial_plan.receiver_functional),
        ('matrix_format', payload.get('matrix_format'), 'csr_full'),
    )
    for name, actual, expected in exact:
        if actual != expected:
            raise ExperimentBlocked(f'semidiscrete system {name} mismatch: {actual} != {expected}')

    for name, expected in (
        ('order', spatial_plan.h1_order),
        ('uniform_refinements', spatial_plan.uniform_refinements),
        ('elements', spatial_plan.expected_elements),
        ('ndofs', spatial_plan.expected_ndofs),
    ):
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

    ndofs = spatial_plan.expected_ndofs
    mass = _csr_matrix(payload['mass_matrix'], ndofs, 'mass_matrix')
    stiffness = _csr_matrix(payload['stiffness_c2_matrix'], ndofs, 'stiffness_c2_matrix')
    source = np.asarray(payload.get('source_functional'), dtype=np.float64)
    receiver = np.asarray(payload.get('receiver_functional'), dtype=np.float64)
    if source.shape != (ndofs,) or receiver.shape != (ndofs,):
        raise ExperimentBlocked('source/receiver functionals differ from frozen ndofs')
    if not np.all(np.isfinite(source)) or not np.all(np.isfinite(receiver)):
        raise ExperimentBlocked('source/receiver functionals contain non-finite values')

    mass_symmetry = _symmetry_max_abs(mass)
    stiffness_symmetry = _symmetry_max_abs(stiffness)
    if mass_symmetry > 1e-12 or stiffness_symmetry > 1e-8:
        raise ExperimentBlocked(
            f'exported sparse matrices are not symmetric: M={mass_symmetry}, K={stiffness_symmetry}'
        )

    numeric_identity = semantic_hash(
        {
            'schema_version': modal_runner.SYSTEM_SCHEMA,
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
    try:
        validate_modal_reference_system_identity(plan, numeric_identity)
    except ValueError as exc:
        raise ExperimentBlocked(str(exc)) from exc

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


def _sparse_numeric_hash(matrix: sparse.spmatrix) -> str:
    csr = matrix.tocsr(copy=True)
    csr.sum_duplicates()
    csr.sort_indices()
    digest = sha256()
    digest.update(np.asarray(csr.shape, dtype=np.int64).tobytes())
    digest.update(csr.indptr.astype(np.int64, copy=False).tobytes())
    digest.update(csr.indices.astype(np.int64, copy=False).tobytes())
    digest.update(csr.data.astype(np.float64, copy=False).tobytes())
    return digest.hexdigest()


def _factor_storage_mb(lu) -> float:
    total = 0
    for matrix in (lu.L.tocsr(), lu.U.tocsr()):
        total += matrix.data.nbytes + matrix.indices.nbytes + matrix.indptr.nbytes
    total += np.asarray(lu.perm_r).nbytes + np.asarray(lu.perm_c).nbytes
    return total / (1024.0 * 1024.0)


def _factorize(matrix: sparse.spmatrix, plan: MfemTransientExperimentPlan):
    cfg = plan.integrator
    return sparse_linalg.splu(
        matrix.tocsc(),
        permc_spec=cfg.permutation,
        diag_pivot_thresh=cfg.diagonal_pivot_threshold,
        options={
            'Equil': cfg.equilibration,
            'IterRefine': cfg.iterative_refinement,
        },
    )


def _prepare_mass_factorization(
    plan: MfemTransientExperimentPlan,
    system: dict[str, object],
) -> tuple[object, dict[str, object]]:
    if scipy.__version__ != plan.integrator.scipy_version:
        raise ExperimentBlocked(
            f'SciPy version mismatch: {scipy.__version__} != {plan.integrator.scipy_version}'
        )
    if np.__version__ != plan.integrator.numpy_version:
        raise ExperimentBlocked(
            f'NumPy version mismatch: {np.__version__} != {plan.integrator.numpy_version}'
        )
    started = time.perf_counter()
    try:
        lu = _factorize(system['mass'], plan)
    except Exception as exc:
        raise ExperimentBlocked(f'sparse mass factorization failed: {type(exc).__name__}: {exc}') from exc
    factor_s = time.perf_counter() - started
    evidence = {
        'solver': plan.integrator.linear_solver,
        'permutation': plan.integrator.permutation,
        'diagonal_pivot_threshold': plan.integrator.diagonal_pivot_threshold,
        'equilibration': plan.integrator.equilibration,
        'iterative_refinement': plan.integrator.iterative_refinement,
        'factorization_s': factor_s,
        'matrix_nnz': int(system['mass'].nnz),
        'lu_nnz': int(lu.L.nnz + lu.U.nnz),
        'factor_storage_mb': _factor_storage_mb(lu),
        'reuse_policy': plan.integrator.mass_factorization,
        'matrix_numeric_sha256': _sparse_numeric_hash(system['mass']),
    }
    evidence['factorization_identity_sha256'] = semantic_hash(evidence)
    return lu, evidence


def _build_pade_blocks(
    mass: sparse.csr_matrix,
    stiffness: sparse.csr_matrix,
    dt_s: float,
) -> tuple[sparse.csc_matrix, sparse.csr_matrix]:
    a0 = mass - (dt_s * dt_s / 12.0) * stiffness
    denominator = sparse.bmat(
        [
            [a0, (-0.5 * dt_s) * mass],
            [(0.5 * dt_s) * stiffness, a0],
        ],
        format='csc',
        dtype=np.float64,
    )
    numerator = sparse.bmat(
        [
            [a0, (0.5 * dt_s) * mass],
            [(-0.5 * dt_s) * stiffness, a0],
        ],
        format='csr',
        dtype=np.float64,
    )
    return denominator, numerator


def _pressure_hash(pressures: np.ndarray) -> str:
    values = np.asarray(pressures, dtype='<f8')
    return sha256(values.tobytes(order='C')).hexdigest()


def _run_transient_attempt(
    plan: MfemTransientExperimentPlan,
    fixture,
    system: dict[str, object],
    mass_lu,
    spec,
    work_dir: Path,
) -> tuple[ExperimentAttemptResult, dict[str, object]]:
    cfg = plan.integrator
    dt_s = 1.0 / float(spec.sample_rate_hz)
    substeps_per_output_interval = cfg.substeps_per_output_interval
    internal_step_s = dt_s / float(substeps_per_output_interval)
    sample_count = int(round(float(fixture.comparison.observation_time_s) * spec.sample_rate_hz))
    expected_internal_step_count = (sample_count - 1) * substeps_per_output_interval
    sound_speed = float(fixture.environment.sound_speed_m_s)
    density = float(fixture.environment.density_kg_m3)
    source_amplitude = float(fixture.sources[0].amplitude)
    mass = system['mass']
    stiffness = system['stiffness']
    source = system['source']
    receiver = system['receiver']

    rhs0 = (sound_speed * sound_speed * dt_s * source_amplitude) * source
    try:
        v0 = mass_lu.solve(rhs0)
    except Exception as exc:
        raise ExperimentBlocked(f'initial sparse mass solve failed: {type(exc).__name__}: {exc}') from exc
    mass_residual = float(
        np.linalg.norm(mass @ v0 - rhs0) / max(float(np.linalg.norm(rhs0)), 1e-30)
    )
    if not math.isfinite(mass_residual) or mass_residual > cfg.residual_relative_tolerance:
        raise ExperimentBlocked(
            f'initial sparse mass solve residual {mass_residual} exceeds frozen tolerance'
        )

    denominator, numerator = _build_pade_blocks(mass, stiffness, internal_step_s)
    denominator_hash = _sparse_numeric_hash(denominator)
    factor_started = time.perf_counter()
    try:
        step_lu = _factorize(denominator, plan)
    except Exception as exc:
        raise ExperimentBlocked(
            f'Padé denominator sparse factorization failed: {type(exc).__name__}: {exc}'
        ) from exc
    factorization_s = time.perf_counter() - factor_started
    factor_evidence = {
        'system_numeric_identity_sha256': system['numeric_identity_sha256'],
        'integrator_configuration_sha256': plan.integrator_configuration_hash(),
        'attempt_id': spec.attempt_id,
        'sample_rate_hz': spec.sample_rate_hz,
        'output_interval_s': dt_s,
        'substeps_per_output_interval': substeps_per_output_interval,
        'internal_step_s': internal_step_s,
        'internal_step_fraction_of_output_interval': f'1/{substeps_per_output_interval}',
        'denominator_sparse_numeric_sha256': denominator_hash,
        'denominator_shape': list(denominator.shape),
        'denominator_nnz': int(denominator.nnz),
        'lu_nnz': int(step_lu.L.nnz + step_lu.U.nnz),
        'solver': cfg.linear_solver,
        'permutation': cfg.permutation,
        'diagonal_pivot_threshold': cfg.diagonal_pivot_threshold,
        'equilibration': cfg.equilibration,
        'iterative_refinement': cfg.iterative_refinement,
        'reuse_policy': cfg.step_factorization,
    }
    factorization_identity = semantic_hash(factor_evidence)

    state = np.empty(2 * mass.shape[0], dtype=np.float64)
    state[: mass.shape[0]] = 0.0
    state[mass.shape[0] :] = v0
    pressures = np.empty(sample_count, dtype=np.float64)
    max_residual = 0.0
    checked_residuals = 0
    internal_step_count = 0

    solve_started = time.perf_counter()
    for index in range(sample_count):
        pressures[index] = density * float(receiver @ state[mass.shape[0] :])
        if index + 1 == sample_count:
            break
        for substep_index in range(substeps_per_output_interval):
            rhs = numerator @ state
            try:
                next_state = step_lu.solve(rhs)
            except Exception as exc:
                raise ExperimentBlocked(
                    'Padé sparse internal step solve failed at '
                    f'output={index} substep={substep_index}: {type(exc).__name__}: {exc}'
                ) from exc
            if not np.all(np.isfinite(next_state)):
                raise ExperimentBlocked(
                    f'Padé sparse internal step produced non-finite state at '
                    f'output={index} substep={substep_index}'
                )
            internal_step_count += 1
            if (
                internal_step_count % cfg.residual_check_interval_steps == 0
                or internal_step_count == expected_internal_step_count
            ):
                residual = denominator @ next_state - rhs
                relative = float(
                    np.linalg.norm(residual) / max(float(np.linalg.norm(rhs)), 1e-30)
                )
                if not math.isfinite(relative):
                    raise ExperimentBlocked('Padé sparse internal-step residual is non-finite')
                max_residual = max(max_residual, relative)
                checked_residuals += 1
                if relative > cfg.residual_relative_tolerance:
                    raise ExperimentBlocked(
                        f'Padé sparse internal-step residual {relative} exceeds frozen tolerance'
                    )
            state = next_state
    solve_s = time.perf_counter() - solve_started
    if internal_step_count != expected_internal_step_count:
        raise ExperimentBlocked(
            f'exact substep contract violated: {internal_step_count} != '
            f'{expected_internal_step_count}'
        )

    if not np.all(np.isfinite(pressures)):
        raise ExperimentBlocked('transient pressure record contains non-finite samples')

    source_samples = np.zeros(sample_count, dtype=np.float64)
    source_samples[0] = source_amplitude
    transfer_samples = baseline._direct_dtft_transfer(
        fixture,
        dt_s=dt_s,
        pressures=pressures.tolist(),
        source_samples=source_samples.tolist(),
    )
    pressure_record_sha256 = _pressure_hash(pressures)
    numerical_identity = semantic_hash(
        {
            'system_numeric_identity_sha256': system['numeric_identity_sha256'],
            'integrator_configuration_sha256': plan.integrator_configuration_hash(),
            'factorization_identity_sha256': factorization_identity,
            'attempt_id': spec.attempt_id,
            'sample_rate_hz': spec.sample_rate_hz,
            'output_interval_s': dt_s,
            'substeps_per_output_interval': substeps_per_output_interval,
            'internal_step_s': internal_step_s,
            'internal_step_count': internal_step_count,
            'pressure_record_sha256': pressure_record_sha256,
        }
    )

    raw_path = work_dir / f'{spec.attempt_id}.json'
    raw = {
        'schema_version': RAW_SCHEMA,
        'fixture_id': fixture.fixture_id,
        'system_numeric_identity_sha256': system['numeric_identity_sha256'],
        'integrator_configuration_sha256': plan.integrator_configuration_hash(),
        'algorithm_id': cfg.algorithm_id,
        'algorithm_version': cfg.algorithm_version,
        'order': cfg.order,
        'propagation_form': cfg.propagation_form,
        'dissipation_model': cfg.dissipation_model,
        'candidate_matrix_policy': cfg.candidate_matrix_policy,
        'source_mapping': 'phi_t(0+)=c^2*dt*M^-1*b*q[0]',
        'pressure_mapping': 'p(t)=rho*r.T*phi_t(t)',
        'sample_rate_hz': spec.sample_rate_hz,
        'dt_s': dt_s,
        'output_interval_s': dt_s,
        'substeps_per_output_interval': substeps_per_output_interval,
        'substep_policy': cfg.substep_policy,
        'internal_step_s': internal_step_s,
        'internal_step_fraction_of_output_interval': f'1/{substeps_per_output_interval}',
        'internal_step_count': internal_step_count,
        'record_interval': plan.numerical_contract.record_interval,
        'observation_time_s': float(fixture.comparison.observation_time_s),
        'sample_count': sample_count,
        'last_sample_time_s': (sample_count - 1) * dt_s,
        'source_normalization': fixture.sources[0].normalization,
        'window': 'none',
        'filter': 'none',
        'zero_padding': 'none',
        'mass_solve_relative_residual': mass_residual,
        'max_checked_step_relative_residual': max_residual,
        'checked_step_residual_count': checked_residuals,
        'factorization': {
            **factor_evidence,
            'factorization_identity_sha256': factorization_identity,
            'factorization_s': factorization_s,
            'factor_storage_mb': _factor_storage_mb(step_lu),
        },
        'solve_s': solve_s,
        'pressure_record_sha256': pressure_record_sha256,
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
    total_s = factorization_s + solve_s
    status = 'COMPLETED'
    reason_code = 'completed'
    reason = 'sparse fourth-order non-dissipative transient attempt completed'
    if total_s > plan.resource_ceiling.max_solve_s_per_attempt:
        status = 'BLOCKED'
        reason_code = 'solve_resource_ceiling'
        reason = f'factorization+stepping {total_s} s exceeds frozen solve ceiling'
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
        sample_rate_hz=spec.sample_rate_hz,
        output_interval_s=dt_s,
        substeps_per_output_interval=substeps_per_output_interval,
        internal_step_s=internal_step_s,
        internal_step_count=internal_step_count,
        numerical_identity_sha256=numerical_identity if status == 'COMPLETED' else None,
        pressure_record_sha256=pressure_record_sha256 if status == 'COMPLETED' else None,
        sample_count=sample_count,
        solve_s=solve_s,
        factorization_s=factorization_s,
        peak_ram_mb=rss_mb,
        output_mb=output_mb,
        mass_solve_relative_residual=mass_residual,
        max_checked_step_relative_residual=max_residual,
        checked_step_residual_count=checked_residuals,
        denominator_nnz=int(denominator.nnz),
        lu_nnz=int(step_lu.L.nnz + step_lu.U.nnz),
        factor_storage_mb=_factor_storage_mb(step_lu),
        factorization_identity_sha256=factorization_identity,
        sparse_candidate_path=True,
        dense_eigendecomposition_used=False,
        factorization_reused=True,
    )
    return result, {
        'raw_output_path': raw_path.as_posix(),
        'raw_output_sha256': _sha256_file(raw_path),
        'dt_s': dt_s,
        'output_interval_s': dt_s,
        'substeps_per_output_interval': substeps_per_output_interval,
        'internal_step_s': internal_step_s,
        'internal_step_count': internal_step_count,
        'transfer_samples': transfer_samples,
        'pressure_record_sha256': pressure_record_sha256,
        'factorization': raw['factorization'],
    }


def _adjacent_pair_metrics(
    plan: MfemTransientExperimentPlan,
    details: dict[str, dict[str, object]],
) -> tuple[PairMetrics, ...]:
    pairs: list[PairMetrics] = []
    ids = [item.attempt_id for item in plan.attempts]
    for coarse_id, fine_id in zip(ids, ids[1:]):
        if coarse_id not in details or fine_id not in details:
            continue
        raw = baseline._pair_metrics(
            {
                'level_id': coarse_id,
                'order': plan.spatial_system.h1_order,
                'dt_s': details[coarse_id]['dt_s'],
                'transfer_samples': details[coarse_id]['transfer_samples'],
            },
            {
                'level_id': fine_id,
                'order': plan.spatial_system.h1_order,
                'dt_s': details[fine_id]['dt_s'],
                'transfer_samples': details[fine_id]['transfer_samples'],
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


def _frequency_deviation(candidate_samples, modal_samples) -> list[dict[str, float]]:
    result: list[dict[str, float]] = []
    for candidate, reference in zip(candidate_samples, modal_samples):
        if not baseline._float_close(
            float(candidate['frequency_hz']), float(reference['frequency_hz'])
        ):
            raise ExperimentBlocked('candidate/modal frequency grids differ')
        c = baseline._transfer_complex(candidate)
        r = baseline._transfer_complex(reference)
        rmag = abs(r)
        result.append(
            {
                'frequency_hz': float(reference['frequency_hz']),
                'magnitude_delta_db': float(
                    candidate['magnitude_db_re_1_pa_per_m3_s']
                    - reference['magnitude_db_re_1_pa_per_m3_s']
                ),
                'magnitude_relative': abs(abs(c) - rmag) / max(rmag, 1e-300),
                'phase_error_deg': baseline._wrapped_phase_delta_deg(
                    float(candidate['phase_deg']), float(reference['phase_deg'])
                ),
                'complex_relative': abs(c - r) / max(rmag, 1e-300),
            }
        )
    return result


def _modal_reference_metrics(
    plan: MfemTransientExperimentPlan,
    fixture,
    candidate_details: dict[str, dict[str, object]],
    modal_details: dict[int, dict[str, object]],
) -> tuple[ModalReferenceMetrics, ...]:
    metrics: list[ModalReferenceMetrics] = []
    for spec in plan.attempts:
        if spec.attempt_id not in candidate_details or spec.sample_rate_hz not in modal_details:
            continue
        candidate = candidate_details[spec.attempt_id]
        reference = modal_details[spec.sample_rate_hz]
        raw = baseline._pair_metrics(
            {
                'level_id': spec.attempt_id,
                'order': plan.spatial_system.h1_order,
                'dt_s': candidate['dt_s'],
                'transfer_samples': candidate['transfer_samples'],
            },
            {
                'level_id': reference['attempt_id'],
                'order': plan.spatial_system.h1_order,
                'dt_s': reference['dt_s'],
                'transfer_samples': reference['transfer_samples'],
            },
            magnitude_mask_db=plan.numerical_contract.magnitude_null_mask_below_db,
            phase_mask_db=plan.numerical_contract.phase_null_mask_below_db,
        )
        candidate['modal_reference_frequency_deviation'] = _frequency_deviation(
            candidate['transfer_samples'], reference['transfer_samples']
        )
        metrics.append(
            ModalReferenceMetrics(
                attempt_id=spec.attempt_id,
                modal_attempt_id=str(reference['attempt_id']),
                sample_rate_hz=spec.sample_rate_hz,
                magnitude_max_abs_db=float(raw['magnitude_max_abs_db']),
                magnitude_max_relative=float(raw['magnitude_max_relative']),
                phase_max_error_deg=float(raw['phase_max_error_deg']),
                complex_rms_relative=float(raw['complex_rms_relative']),
                magnitude_sample_count=int(raw['magnitude_sample_count']),
                phase_sample_count=int(raw['phase_sample_count']),
            )
        )
    return tuple(metrics)


def _validate_historical_modal_reference(
    plan: MfemTransientExperimentPlan,
    modal_plan_path: Path,
    modal_evidence_path: Path,
    actual_system_identity: str,
) -> dict[str, object]:
    if _sha256_file(modal_plan_path) != plan.modal_reference.modal_plan_file_sha256:
        raise ExperimentBlocked('PR #277 modal plan file hash differs from frozen reference binding')
    evidence = json.loads(modal_evidence_path.read_text(encoding='utf-8'))
    if int(evidence.get('pull_request', -1)) != plan.modal_reference.pull_request:
        raise ExperimentBlocked('modal reference evidence does not identify PR #277')
    evidence_system = (
        evidence.get('authority', {}).get('semidiscrete_system_numeric_identity_sha256')
    )
    if evidence_system != plan.modal_reference.expected_system_numeric_identity_sha256:
        raise ExperimentBlocked('historical modal evidence system identity differs from frozen plan')
    if actual_system_identity != evidence_system:
        raise ExperimentBlocked('current sparse system does not match historical PR #277 system')
    if evidence.get('experiment_outcome') != 'PASS':
        raise ExperimentBlocked('historical PR #277 modal evidence is not PASS')
    return evidence


def _recompute_modal_reference(
    plan: MfemTransientExperimentPlan,
    fixture,
    benchmark,
    candidates,
    system_path: Path,
    modal_plan_path: Path,
    work_dir: Path,
) -> tuple[dict[int, dict[str, object]], dict[str, object]]:
    modal_plan = modal_runner.load_experiment_plan(modal_plan_path)
    modal_fixture, _ = modal_runner._validate_plan_against_current_authority(
        modal_plan, benchmark, candidates
    )
    if modal_fixture.fixture_id != fixture.fixture_id:
        raise ExperimentBlocked('modal reference fixture differs from transient fixture')
    dense_system = modal_runner._validate_system(modal_plan, fixture, system_path)
    modal = modal_runner._modal_decomposition(modal_plan, dense_system)
    reference_dir = work_dir / 'diagnostic_modal_reference'
    reference_dir.mkdir(parents=True, exist_ok=True)
    details: dict[int, dict[str, object]] = {}
    for spec in modal_plan.attempts:
        result, item = modal_runner._reconstruct_attempt(
            modal_plan, fixture, dense_system, modal, spec, reference_dir
        )
        if result.status != 'COMPLETED':
            raise ExperimentBlocked(
                f'diagnostic modal reference {spec.attempt_id} did not complete: {result.reason}'
            )
        details[spec.sample_rate_hz] = {
            'attempt_id': spec.attempt_id,
            'dt_s': item['dt_s'],
            'transfer_samples': item['transfer_samples'],
            'raw_output_path': item['raw_output_path'],
            'raw_output_sha256': item['raw_output_sha256'],
        }
    evidence = {
        key: value for key, value in modal.items() if key not in {'eigenvalues', 'eigenvectors'}
    }
    evidence['system_numeric_identity_sha256'] = dense_system['numeric_identity_sha256']
    evidence['role'] = 'diagnostic_reference_only_not_production_candidate'
    return details, evidence


def _blocked_attempts(plan: MfemTransientExperimentPlan, reason_code: str, reason: str):
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
        description='Run the frozen R100B sparse low-dispersion transient experiment'
    )
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--candidates', required=True, type=Path)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--modal-plan', required=True, type=Path)
    parser.add_argument('--modal-evidence', required=True, type=Path)
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
    fixture, candidate = modal_runner._validate_plan_against_current_authority(
        plan, benchmark, candidates
    )
    fixture_semantic_hash = _fixture_hash(fixture)

    if baseline._git_head(args.mfem_root) != candidate.source_commit_sha:
        raise SystemExit('MFEM checkout does not match exact candidate source commit')

    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    attempt_results: tuple[ExperimentAttemptResult, ...]
    attempt_details: dict[str, dict[str, object]] = {}
    system_evidence: dict[str, object] = {}
    mass_factorization_evidence: dict[str, object] = {}
    modal_reference_evidence: dict[str, object] = {}
    modal_reference_details: dict[int, dict[str, object]] = {}
    historical_modal_evidence: dict[str, object] = {}
    setup_stdout_tail = ''

    if args.blocked_reason:
        attempt_results = _blocked_attempts(plan, 'experiment_setup_blocked', args.blocked_reason)
    elif args.executable is None or not args.executable.is_file():
        attempt_results = _blocked_attempts(
            plan, 'experiment_setup_blocked', 'MFEM semidiscrete export executable missing'
        )
    else:
        try:
            system_path, setup_stdout_tail = modal_runner._run_system_export(
                plan, fixture, args.executable, args.work_dir
            )
            system = _validate_sparse_system(plan, fixture, system_path)
            historical_modal_evidence = _validate_historical_modal_reference(
                plan,
                args.modal_plan,
                args.modal_evidence,
                system['numeric_identity_sha256'],
            )
            system_evidence = {
                'raw_path': system_path.as_posix(),
                'raw_sha256': _sha256_file(system_path),
                'numeric_identity_sha256': system['numeric_identity_sha256'],
                'mass_symmetry_max_abs': system['mass_symmetry_max_abs'],
                'stiffness_symmetry_max_abs': system['stiffness_symmetry_max_abs'],
                'mass_nnz': int(system['mass'].nnz),
                'stiffness_nnz': int(system['stiffness'].nnz),
                'ndofs': plan.spatial_system.expected_ndofs,
                'elements': plan.spatial_system.expected_elements,
                'candidate_matrix_policy': plan.integrator.candidate_matrix_policy,
            }
            mass_lu, mass_factorization_evidence = _prepare_mass_factorization(plan, system)
            results: list[ExperimentAttemptResult] = []
            for spec in plan.attempts:
                try:
                    result, details = _run_transient_attempt(
                        plan, fixture, system, mass_lu, spec, args.work_dir
                    )
                except ExperimentBlocked as exc:
                    result = ExperimentAttemptResult(
                        attempt_id=spec.attempt_id,
                        status='BLOCKED',
                        reason_code='transient_solve_blocked',
                        reason=str(exc),
                        sample_rate_hz=spec.sample_rate_hz,
                    )
                    details = {}
                results.append(result)
                if result.status == 'COMPLETED':
                    attempt_details[spec.attempt_id] = details
            attempt_results = tuple(results)

            if all(item.status == 'COMPLETED' for item in attempt_results):
                modal_reference_details, modal_reference_evidence = _recompute_modal_reference(
                    plan,
                    fixture,
                    benchmark,
                    candidates,
                    system_path,
                    args.modal_plan,
                    args.work_dir,
                )
                if (
                    modal_reference_evidence.get('system_numeric_identity_sha256')
                    != system['numeric_identity_sha256']
                ):
                    raise ExperimentBlocked(
                        'recomputed modal reference used a different semidiscrete system'
                    )
        except ExperimentBlocked as exc:
            attempt_results = _blocked_attempts(plan, 'transient_system_blocked', str(exc))
            attempt_details = {}
            modal_reference_details = {}

    pair_metrics = _adjacent_pair_metrics(plan, attempt_details)
    modal_metrics = _modal_reference_metrics(
        plan, fixture, attempt_details, modal_reference_details
    )
    decision = evaluate_transient_experiment(
        plan,
        attempt_results=attempt_results,
        pair_metrics=pair_metrics,
        modal_reference_metrics=modal_metrics,
    )

    htdt = baseline._htdt_git_provenance()
    task_start_main_sha = os.environ.get('HTDT_TASK_START_MAIN_SHA', '').strip().lower()
    workflow_base_sha = os.environ.get('HTDT_PR_BASE_SHA', '').strip().lower()
    for provenance_name, provenance_sha in (
        ('HTDT_TASK_START_MAIN_SHA', task_start_main_sha),
        ('HTDT_PR_BASE_SHA', workflow_base_sha),
    ):
        if len(provenance_sha) != 40 or any(
            character not in '0123456789abcdef' for character in provenance_sha
        ):
            raise SystemExit(f'{provenance_name} must be an exact 40-character Git SHA')

    report = {
        'schema_version': ARTIFACT_SCHEMA,
        'workflow_execution_status': 'PASS',
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
        'integrator_configuration_sha256': plan.integrator_configuration_hash(),
        'output_grid_configuration_sha256': plan.output_grid_configuration_hash(),
        'source_provenance': {
            'task_start_main_sha': task_start_main_sha,
            'workflow_base_sha': workflow_base_sha,
            'pr_head_sha': htdt['pr_head_commit_sha'],
            'checkout_sha': htdt['checkout_commit_sha'],
            'github_run_id': os.environ.get('GITHUB_RUN_ID'),
            'github_run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
        },
        'htdt_source_commit_sha': htdt['pr_head_commit_sha'],
        'htdt_checkout_commit_sha': htdt['checkout_commit_sha'],
        'mfem_checkout_commit_sha': baseline._git_head(args.mfem_root),
        'native_build_s': args.native_build_s,
        'system_evidence': system_evidence,
        'mass_factorization_evidence': mass_factorization_evidence,
        'attempt_details': attempt_details,
        'diagnostic_modal_reference': {
            'historical_evidence_path': args.modal_evidence.as_posix(),
            'historical_pr': plan.modal_reference.pull_request,
            'historical_selected_conclusion': historical_modal_evidence.get('selected_conclusion'),
            'recomputed_evidence': modal_reference_evidence,
            'attempt_details': modal_reference_details,
            'production_execution_candidate': False,
            'candidate_requires_modal_reference': False,
        },
        'decision': decision.model_dump(mode='json'),
        'resource_evidence': {
            'native_build_s': args.native_build_s,
            'experiment_work_disk_mb': _directory_size_mb(args.work_dir),
            'observed_process_rss_mb': psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0),
            'mass_factorization_s': mass_factorization_evidence.get('factorization_s'),
            'total_transient_factorization_s': sum(
                item.factorization_s or 0.0 for item in attempt_results
            ),
            'total_transient_stepping_s': sum(item.solve_s or 0.0 for item in attempt_results),
            'total_internal_steps': sum(item.internal_step_count or 0 for item in attempt_results),
            'substeps_per_output_interval': plan.integrator.substeps_per_output_interval,
            'nominal_internal_step_multiplier_vs_pr281': float(
                plan.integrator.substeps_per_output_interval
            ),
            'factorization_reuse': (
                'one sparse Padé denominator factorization per output rate, reused for '
                'every internal GL2 substep'
            ),
            'resource_ceiling': plan.resource_ceiling.model_dump(mode='json'),
        },
        'production_readiness': {
            'limited_integrator_suitability_status': decision.production_suitability_status,
            'candidate_wide_decision_before': 'NO_GO',
            'candidate_wide_decision_after': decision.production_adoption_decision,
            'production_solver_selected': False,
            'readiness_input_promoted': False,
            'reason': (
                'This slice evaluates only transient-integrator execution suitability. '
                'Candidate-wide R100B adoption authority and other required evidence are not bypassed.'
            ),
        },
        'next_minimum_experiment': (
            'If the transient candidate passes, keep this exact integrator/system and run the '
            'smallest remaining candidate-wide R100B production gate that can change adoption; '
            'do not relax tolerances or retry h2 as a substitute. If it fails or blocks, isolate '
            'only the recorded transient numerical/resource blocker.'
        ),
        'hard_rule_attestation': {
            'r100a_authority_changed': False,
            'r100a_tolerance_changed': False,
            'spatial_p2_h1_system_changed': False,
            'output_sampling_contract_changed': False,
            'substeps_per_output_interval': plan.integrator.substeps_per_output_interval,
            'adaptive_stepping_used': False,
            'dense_inverse_generated': False,
            'candidate_dense_eigendecomposition_used': False,
            'diagnostic_reference_dense_eigendecomposition_used': bool(modal_reference_evidence),
            'best_trace_selection_used': False,
            'nonconverged_result_promoted': False,
            'production_solver_selected': False,
            'htdt_capture_changed': False,
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
        'workflow_execution_status': report['workflow_execution_status'],
        'outcome': decision.outcome,
        'numerical_convergence_status': decision.numerical_convergence_status,
        'modal_reference_agreement_status': decision.modal_reference_agreement_status,
        'current_r100a_tolerance_status': decision.current_r100a_tolerance_status,
        'production_suitability_status': decision.production_suitability_status,
        'production_adoption_decision': decision.production_adoption_decision,
        'deterministic_report_identity_sha256': decision.deterministic_report_identity_sha256,
        'system_numeric_identity_sha256': system_evidence.get('numeric_identity_sha256'),
        'pair_metrics': [item.model_dump(mode='json') for item in pair_metrics],
        'modal_reference_metrics': [item.model_dump(mode='json') for item in modal_metrics],
        'resource_evidence': report['resource_evidence'],
        'violations': list(decision.violations),
    }
    print(
        'R100B_MFEM_TRANSIENT_SUMMARY='
        + json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    )
    print(f'R100B_MFEM_TRANSIENT_REPORT={args.output.as_posix()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
