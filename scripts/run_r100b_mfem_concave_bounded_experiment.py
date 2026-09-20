from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import psutil

from htdt.acoustic_bakeoff_mfem_concave_experiment import (
    ExperimentAttemptResult,
    MfemConcaveExperimentPlan,
    PairMetrics,
    evaluate_bounded_experiment,
    load_experiment_plan,
    semantic_hash,
    validate_exact_authority_binding,
)
from htdt.acoustic_bakeoff_readiness import candidate_semantic_hash
from htdt.acoustic_benchmark import canonical_benchmark_json, load_acoustic_benchmark_manifest
from htdt.acoustic_bakeoff import load_bakeoff_candidate_manifest

import run_r100b_mfem_finite_record_reference as baseline


ARTIFACT_SCHEMA = 'r100b-mfem-concave-bounded-experiment-artifact-1'
RAW_SCHEMA = 'r100b-mfem-concave-finite-record-raw-1'


class AttemptStructuralFailure(RuntimeError):
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
    total = sum(item.stat().st_size for item in path.rglob('*') if item.is_file())
    return total / (1024.0 * 1024.0)


def _fixture_hash(fixture) -> str:
    return sha256(
        canonical_benchmark_json(fixture.model_dump(mode='json')).encode('utf-8')
    ).hexdigest()


def _validate_plan_against_current_authority(
    plan: MfemConcaveExperimentPlan,
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

    if plan.solver_configuration.source_commit_sha != candidate.source_commit_sha:
        raise ValueError('solver configuration source commit differs from candidate binding')

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
        (
            'record_interval',
            contract.record_interval,
            comparison.finite_record_transfer.record_interval,
        ),
    )
    for name, actual, expected in exact_checks:
        if actual != expected:
            raise ValueError(f'plan {name} differs from current R100A: {actual} != {expected}')

    numeric_checks = (
        ('observation_time_s', contract.observation_time_s, comparison.observation_time_s),
        ('frequency_start_hz', contract.frequency_start_hz, comparison.frequency_grid.start_hz),
        ('frequency_stop_hz', contract.frequency_stop_hz, comparison.frequency_grid.stop_hz),
        ('frequency_step_hz', contract.frequency_step_hz, comparison.frequency_grid.step_hz),
        (
            'magnitude_null_mask_below_db',
            contract.magnitude_null_mask_below_db,
            magnitude.tolerance.null_mask_below_db,
        ),
        (
            'phase_null_mask_below_db',
            contract.phase_null_mask_below_db,
            phase.tolerance.null_mask_below_db,
        ),
        (
            'magnitude_absolute_tolerance_db',
            contract.magnitude_absolute_tolerance_db,
            magnitude.tolerance.absolute,
        ),
        (
            'magnitude_relative_tolerance',
            contract.magnitude_relative_tolerance,
            magnitude.tolerance.relative,
        ),
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
        (
            'max_solve_s_per_attempt',
            plan.resource_ceiling.max_solve_s_per_attempt,
            budget.max_solve_s,
        ),
        (
            'max_output_mb_per_attempt',
            plan.resource_ceiling.max_output_mb_per_attempt,
            budget.max_output_mb,
        ),
    )
    for name, actual, expected in resource_checks:
        if not baseline._float_close(float(actual), float(expected)):
            raise ValueError(f'plan resource {name} differs from current R100A budget')

    return fixture, candidate


def _validate_raw_attempt(fixture, payload: dict[str, object], spec):
    if payload.get('schema_version') != RAW_SCHEMA:
        raise AttemptStructuralFailure('unexpected raw schema')
    if payload.get('fixture_id') != fixture.fixture_id:
        raise AttemptStructuralFailure('raw fixture id differs from current authority')
    if payload.get('geometry') != 'exact-five-hex-l-prism':
        raise AttemptStructuralFailure('raw geometry is not exact five-hex L-prism')
    if payload.get('boundary_model') != 'natural-neumann-rigid':
        raise AttemptStructuralFailure('raw boundary model is not rigid natural Neumann')
    if payload.get('primary_field') != 'velocity_potential_phi':
        raise AttemptStructuralFailure('raw primary field changed')
    if payload.get('pressure_conversion') != 'p=rho*d(phi)/dt':
        raise AttemptStructuralFailure('raw pressure conversion changed')
    if payload.get('time_integrator') != 'Newmark(beta=0.25,gamma=0.5)':
        raise AttemptStructuralFailure('raw time integrator changed')
    if payload.get('algorithmic_damping') != 'none':
        raise AttemptStructuralFailure('algorithmic damping is forbidden')
    if (
        payload.get('window') != 'none'
        or payload.get('filter') != 'none'
        or payload.get('zero_padding') != 'none'
    ):
        raise AttemptStructuralFailure('window/filter/zero-padding is forbidden')

    if int(payload.get('order', -1)) != spec.order:
        raise AttemptStructuralFailure('raw H1 order differs from predeclared attempt')
    if int(payload.get('uniform_refinements', -1)) != spec.uniform_refinements:
        raise AttemptStructuralFailure('raw h-refinement differs from predeclared attempt')
    expected_elements = 5 * (8 ** spec.uniform_refinements)
    if int(payload.get('elements', -1)) != expected_elements:
        raise AttemptStructuralFailure(
            f'raw element count does not match exact uniform refinement: '
            f'{payload.get("elements")} != {expected_elements}'
        )
    if int(payload.get('sample_rate_hz', -1)) != spec.sample_rate_hz:
        raise AttemptStructuralFailure('raw sample rate differs from predeclared attempt')

    source = fixture.sources[0]
    receiver = fixture.receivers[0]
    expected_dt = 1.0 / spec.sample_rate_hz
    observation_time = float(fixture.comparison.observation_time_s)
    expected_count = int(round(observation_time / expected_dt))
    expected_last = (expected_count - 1) * expected_dt
    for name, expected in (
        ('density_kg_m3', float(fixture.environment.density_kg_m3)),
        ('sound_speed_m_s', float(fixture.environment.sound_speed_m_s)),
        ('source_amplitude_m3_s', float(source.amplitude)),
        ('source_t0_s', 0.0),
        ('observation_time_s', observation_time),
        ('dt_s', expected_dt),
        ('last_sample_time_s', expected_last),
    ):
        try:
            actual = float(payload[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise AttemptStructuralFailure(f'raw output is missing valid {name}') from exc
        if not baseline._float_close(actual, expected):
            raise AttemptStructuralFailure(f'raw {name} differs from current authority/plan')

    if int(payload.get('sample_count', -1)) != expected_count:
        raise AttemptStructuralFailure('raw record does not contain exact [0,T) samples')
    if payload.get('record_interval') != 'half_open_0_T':
        raise AttemptStructuralFailure('raw record interval changed')
    if payload.get('source_normalization') != source.normalization:
        raise AttemptStructuralFailure('raw source normalization changed')
    if payload.get('sample_zero_state') != 'after_source_t0_kick_before_first_homogeneous_step':
        raise AttemptStructuralFailure('raw source t0/sample-zero mapping changed')

    for name, expected in (
        ('source_position_m', list(baseline._position_tuple(source.position))),
        ('receiver_position_m', list(baseline._position_tuple(receiver.position))),
    ):
        actual = payload.get(name)
        if not isinstance(actual, list) or len(actual) != 3:
            raise AttemptStructuralFailure(f'raw output is missing {name}')
        if any(
            not baseline._float_close(float(a), float(e))
            for a, e in zip(actual, expected)
        ):
            raise AttemptStructuralFailure(f'raw {name} differs from current authority')

    raw_samples = payload.get('samples')
    if not isinstance(raw_samples, list) or len(raw_samples) != expected_count:
        raise AttemptStructuralFailure('raw sample array is incomplete')

    pressures: list[float] = []
    source_samples: list[float] = []
    for index, item in enumerate(raw_samples):
        if not isinstance(item, dict) or int(item.get('index', -1)) != index:
            raise AttemptStructuralFailure('raw sample index sequence is not exact')
        if not baseline._float_close(float(item.get('time_s', math.nan)), index * expected_dt):
            raise AttemptStructuralFailure('raw sample timestamp differs from solver-native grid')
        pressure = float(item.get('pressure_pa', math.nan))
        q = float(item.get('source_volume_velocity_m3_s', math.nan))
        if not math.isfinite(pressure) or not math.isfinite(q):
            raise AttemptStructuralFailure('raw pressure/source contains non-finite samples')
        expected_q = float(source.amplitude) if index == 0 else 0.0
        if q != expected_q:
            raise AttemptStructuralFailure('raw physical source is not q[0]=amplitude, q[n>0]=0')
        pressures.append(pressure)
        source_samples.append(q)

    return pressures, source_samples


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    try:
        root = psutil.Process(process.pid)
        for child in root.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
        try:
            root.kill()
        except psutil.Error:
            pass
    except psutil.Error:
        try:
            process.kill()
        except OSError:
            pass


def _run_attempt(
    plan: MfemConcaveExperimentPlan,
    fixture,
    executable: Path,
    work_dir: Path,
    spec,
    *,
    fixture_semantic_hash: str,
):
    raw_path = work_dir / f'{spec.attempt_id}.json'
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
        '--sample-rate-hz', str(spec.sample_rate_hz),
        '--order', str(spec.order),
        '--uniform-refinements', str(spec.uniform_refinements),
        '--output', str(raw_path),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    monitor = baseline.ProcessPeakRssMonitor(process)
    monitor.start()
    timed_out = False
    try:
        stdout, _ = process.communicate(timeout=plan.resource_ceiling.subprocess_wall_timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(process)
        stdout, _ = process.communicate()
    peak_ram_mb = monitor.stop()

    if timed_out:
        result = ExperimentAttemptResult(
            attempt_id=spec.attempt_id,
            status='BLOCKED',
            reason_code='resource_wall_timeout',
            reason=(
                f'process exceeded predeclared wall timeout '
                f'{plan.resource_ceiling.subprocess_wall_timeout_s} s'
            ),
            peak_ram_mb=peak_ram_mb,
        )
        return result, {'stdout_tail': stdout[-6000:], 'raw_output_path': None}

    if process.returncode != 0:
        result = ExperimentAttemptResult(
            attempt_id=spec.attempt_id,
            status='FAILED',
            reason_code='solver_process_failure',
            reason=f'MFEM process exited with code {process.returncode}',
            peak_ram_mb=peak_ram_mb,
        )
        return result, {'stdout_tail': stdout[-6000:], 'raw_output_path': None}

    if not raw_path.is_file():
        result = ExperimentAttemptResult(
            attempt_id=spec.attempt_id,
            status='FAILED',
            reason_code='missing_raw_output',
            reason='MFEM process completed without raw output',
            peak_ram_mb=peak_ram_mb,
        )
        return result, {'stdout_tail': stdout[-6000:], 'raw_output_path': None}

    try:
        payload = json.loads(raw_path.read_text(encoding='utf-8'))
        pressures, source_samples = _validate_raw_attempt(fixture, payload, spec)
        transfer_samples = baseline._direct_dtft_transfer(
            fixture,
            dt_s=float(payload['dt_s']),
            pressures=pressures,
            source_samples=source_samples,
        )
    except Exception as exc:
        result = ExperimentAttemptResult(
            attempt_id=spec.attempt_id,
            status='FAILED',
            reason_code='raw_contract_failure',
            reason=f'{type(exc).__name__}: {exc}',
            peak_ram_mb=peak_ram_mb,
            output_mb=raw_path.stat().st_size / (1024.0 * 1024.0),
        )
        return result, {
            'stdout_tail': stdout[-6000:],
            'raw_output_path': raw_path.as_posix(),
            'raw_output_sha256': _sha256_file(raw_path),
        }

    source_residual = float(payload.get('source_mass_relative_residual', math.inf))
    implicit_residual = float(payload.get('max_implicit_relative_residual', math.inf))
    solve_s = float(payload['solve_s'])
    output_mb = raw_path.stat().st_size / (1024.0 * 1024.0)

    status = 'COMPLETED'
    reason_code = 'completed'
    reason = 'attempt completed within frozen numerical and resource ceilings'
    residual_limit = plan.solver_configuration.qualification_residual_limit
    if not math.isfinite(source_residual) or source_residual > residual_limit:
        status = 'BLOCKED'
        reason_code = 'source_mass_residual_ceiling'
        reason = f'source mass residual {source_residual} exceeds {residual_limit}'
    elif not math.isfinite(implicit_residual) or implicit_residual > residual_limit:
        status = 'BLOCKED'
        reason_code = 'implicit_residual_ceiling'
        reason = f'implicit residual {implicit_residual} exceeds {residual_limit}'
    elif solve_s > plan.resource_ceiling.max_solve_s_per_attempt:
        status = 'BLOCKED'
        reason_code = 'solve_resource_ceiling'
        reason = (
            f'solve {solve_s} s exceeds '
            f'{plan.resource_ceiling.max_solve_s_per_attempt} s'
        )
    elif peak_ram_mb > plan.resource_ceiling.ram_budget_mb:
        status = 'BLOCKED'
        reason_code = 'ram_resource_ceiling'
        reason = (
            f'peak RAM {peak_ram_mb} MiB exceeds '
            f'{plan.resource_ceiling.ram_budget_mb} MiB'
        )
    elif output_mb > plan.resource_ceiling.max_output_mb_per_attempt:
        status = 'BLOCKED'
        reason_code = 'output_resource_ceiling'
        reason = (
            f'raw output {output_mb} MiB exceeds '
            f'{plan.resource_ceiling.max_output_mb_per_attempt} MiB'
        )

    numerical_identity = semantic_hash(
        {
            'fixture_semantic_hash': fixture_semantic_hash,
            'solver_configuration_sha256': plan.solver_configuration_hash(),
            'attempt_spec': spec.model_dump(mode='json'),
            'raw_contract': {
                key: payload[key]
                for key in (
                    'schema_version',
                    'fixture_id',
                    'geometry',
                    'boundary_model',
                    'primary_field',
                    'governing_equation',
                    'pressure_conversion',
                    'source_injection',
                    'sample_zero_state',
                    'time_integrator',
                    'algorithmic_damping',
                    'window',
                    'filter',
                    'zero_padding',
                    'order',
                    'uniform_refinements',
                    'elements',
                    'ndofs',
                    'density_kg_m3',
                    'sound_speed_m_s',
                    'source_position_m',
                    'receiver_position_m',
                    'source_amplitude_m3_s',
                    'source_normalization',
                    'source_t0_s',
                    'record_interval',
                    'observation_time_s',
                    'sample_rate_hz',
                    'dt_s',
                    'sample_count',
                    'last_sample_time_s',
                )
            },
            'pressure_samples_pa': pressures,
            'source_samples_m3_s': source_samples,
            'transfer_samples': transfer_samples,
        }
    )
    result = ExperimentAttemptResult(
        attempt_id=spec.attempt_id,
        status=status,
        reason_code=reason_code,
        reason=reason,
        numerical_identity_sha256=numerical_identity,
        elements=int(payload['elements']),
        ndofs=int(payload['ndofs']),
        solve_s=solve_s,
        peak_ram_mb=peak_ram_mb,
        output_mb=output_mb,
        source_mass_relative_residual=source_residual,
        max_implicit_relative_residual=implicit_residual,
    )
    details = {
        'stdout_tail': stdout[-6000:],
        'raw_output_path': raw_path.as_posix(),
        'raw_output_sha256': _sha256_file(raw_path),
        'assemble_s': float(payload['assemble_s']),
        'sample_count': int(payload['sample_count']),
        'dt_s': float(payload['dt_s']),
        'max_implicit_iterations': int(payload['max_implicit_iterations']),
        'transfer_samples': transfer_samples,
    }
    return result, details


def _pair_metrics_from_attempts(
    plan: MfemConcaveExperimentPlan,
    completed_details: dict[str, dict[str, object]],
) -> tuple[PairMetrics, ...]:
    by_id = {item.attempt_id: item for item in plan.attempts}
    pairs: list[PairMetrics] = []
    seen: set[tuple[str, str]] = set()
    for track in (plan.time_track, plan.space_track):
        for coarse_id, fine_id in zip(track, track[1:]):
            key = (coarse_id, fine_id)
            if key in seen:
                continue
            seen.add(key)
            coarse_details = completed_details.get(coarse_id)
            fine_details = completed_details.get(fine_id)
            if not coarse_details or not fine_details:
                continue
            coarse_spec = by_id[coarse_id]
            fine_spec = by_id[fine_id]
            raw = baseline._pair_metrics(
                {
                    'level_id': coarse_id,
                    'order': coarse_spec.order,
                    'dt_s': coarse_details['dt_s'],
                    'transfer_samples': coarse_details['transfer_samples'],
                },
                {
                    'level_id': fine_id,
                    'order': fine_spec.order,
                    'dt_s': fine_details['dt_s'],
                    'transfer_samples': fine_details['transfer_samples'],
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


def _blocked_attempts(plan: MfemConcaveExperimentPlan, reason: str):
    return tuple(
        ExperimentAttemptResult(
            attempt_id=spec.attempt_id,
            status='BLOCKED',
            reason_code='experiment_setup_blocked',
            reason=reason,
        )
        for spec in plan.attempts
    )


def _summary_payload(report: dict[str, object]) -> dict[str, object]:
    decision = report['decision']
    return {
        'outcome': decision['outcome'],
        'promoted_attempt_id': decision['promoted_attempt_id'],
        'deterministic_report_identity_sha256': (
            decision['deterministic_report_identity_sha256']
        ),
        'r100a_semantic_hash': report['authority_binding']['r100a_semantic_hash'],
        'fixture_semantic_hash': report['authority_binding']['fixture_semantic_hash'],
        'candidate_manifest_hash': report['authority_binding']['candidate_manifest_hash'],
        'candidate_semantic_hash': report['authority_binding']['candidate_semantic_hash'],
        'candidate_source_commit_sha': report['authority_binding']['candidate_source_commit_sha'],
        'solver_configuration_sha256': report['solver_configuration_sha256'],
        'mesh_refinement_configuration_sha256': (
            report['mesh_refinement_configuration_sha256']
        ),
        'attempt_results': decision['attempt_results'],
        'pair_metrics': decision['pair_metrics'],
        'track_evaluations': decision['track_evaluations'],
        'conclusion_codes': decision['conclusion_codes'],
        'resource_evidence': report['resource_evidence'],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Run the bounded current-authority MFEM concave numerical experiment'
    )
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--candidates', required=True, type=Path)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--mfem-root', required=True, type=Path)
    parser.add_argument('--build-dir', required=True, type=Path)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--native-build-s', type=float)
    parser.add_argument('--blocked-reason')
    args = parser.parse_args(argv)

    plan = load_experiment_plan(args.plan)
    benchmark = load_acoustic_benchmark_manifest(args.manifest)
    candidates = load_bakeoff_candidate_manifest(args.candidates)
    fixture, candidate = _validate_plan_against_current_authority(
        plan, benchmark, candidates
    )
    fixture_semantic_hash = _fixture_hash(fixture)

    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    if baseline._git_head(args.mfem_root) != candidate.source_commit_sha:
        raise SystemExit('MFEM checkout does not match exact candidate source commit')

    attempt_results: list[ExperimentAttemptResult] = []
    attempt_details: dict[str, dict[str, object]] = {}

    if args.blocked_reason:
        attempt_results.extend(_blocked_attempts(plan, str(args.blocked_reason)))
    else:
        if args.executable is None or not args.executable.is_file():
            attempt_results.extend(_blocked_attempts(plan, 'MFEM experiment executable missing'))
        else:
            for spec in plan.attempts:
                result, details = _run_attempt(
                    plan,
                    fixture,
                    args.executable,
                    args.work_dir,
                    spec,
                    fixture_semantic_hash=fixture_semantic_hash,
                )
                attempt_results.append(result)
                attempt_details[spec.attempt_id] = details

    total_disk_mb = _directory_size_mb(args.work_dir)
    if (
        total_disk_mb > plan.resource_ceiling.disk_budget_mb
        and all(item.status == 'COMPLETED' for item in attempt_results)
    ):
        last = attempt_results[-1]
        attempt_results[-1] = last.model_copy(
            update={
                'status': 'BLOCKED',
                'reason_code': 'disk_resource_ceiling',
                'reason': (
                    f'experiment work disk {total_disk_mb} MiB exceeds '
                    f'{plan.resource_ceiling.disk_budget_mb} MiB'
                ),
            }
        )

    completed_details = {
        item.attempt_id: attempt_details[item.attempt_id]
        for item in attempt_results
        if item.status == 'COMPLETED' and item.attempt_id in attempt_details
    }
    pair_metrics = _pair_metrics_from_attempts(plan, completed_details)
    decision = evaluate_bounded_experiment(
        plan,
        attempt_results=tuple(attempt_results),
        pair_metrics=pair_metrics,
    )

    htdt = baseline._htdt_git_provenance()
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
        'solver_configuration_sha256': plan.solver_configuration_hash(),
        'mesh_refinement_configuration_sha256': plan.mesh_refinement_configuration_hash(),
        'htdt_source_commit_sha': htdt['pr_head_commit_sha'],
        'htdt_checkout_commit_sha': htdt['checkout_commit_sha'],
        'mfem_checkout_commit_sha': baseline._git_head(args.mfem_root),
        'native_build_s': args.native_build_s,
        'decision': decision.model_dump(mode='json'),
        'attempt_details': attempt_details,
        'resource_evidence': {
            'native_build_s': args.native_build_s,
            'experiment_work_disk_mb': total_disk_mb,
            'peak_attempt_ram_mb': max(
                (item.peak_ram_mb or 0.0 for item in attempt_results),
                default=0.0,
            ),
            'total_attempt_solve_s': sum(
                item.solve_s or 0.0 for item in attempt_results
            ),
            'resource_ceiling': plan.resource_ceiling.model_dump(mode='json'),
        },
        'promotion': {
            'finest_trace_promoted': decision.promoted_attempt_id is not None,
            'promoted_attempt_id': decision.promoted_attempt_id,
            'best_trace_selected': False,
            'nonconverged_trace_promoted': False,
        },
        'hard_rule_attestation': {
            'r100a_tolerance_changed': False,
            'r100a_fixture_changed': False,
            'r100a_observable_changed': False,
            'pffdtd_used_as_reference': False,
            'best_trace_selection_used': False,
            'hidden_scoring_used': False,
            'production_solver_selected': False,
            'rdc_used': False,
        },
        'runtime': {
            'python_version': platform.python_version(),
            'psutil_version': psutil.__version__,
        },
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )

    summary = _summary_payload(report)
    print(
        'R100B_MFEM_CONCAVE_BOUNDED_SUMMARY='
        + json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    )
    print(f'R100B_MFEM_CONCAVE_BOUNDED_REPORT={args.output.as_posix()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
