from __future__ import annotations

from pathlib import Path

import pytest

from htdt.acoustic_bakeoff_mfem_concave_experiment import (
    ExperimentAttemptResult,
    PairMetrics,
    evaluate_bounded_experiment,
    load_experiment_plan,
    validate_exact_authority_binding,
)


PLAN_PATH = (
    Path(__file__).resolve().parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r100b_mfem_concave_experiment_plan.json'
)


def _plan():
    return load_experiment_plan(PLAN_PATH)


def _attempts(*, status: str = 'COMPLETED', solve_s: float = 1.0):
    plan = _plan()
    return tuple(
        ExperimentAttemptResult(
            attempt_id=spec.attempt_id,
            status=status,
            reason_code='completed' if status == 'COMPLETED' else 'synthetic_failure',
            reason='completed' if status == 'COMPLETED' else 'synthetic failure',
            numerical_identity_sha256=(
                f'{index + 1:064x}' if status == 'COMPLETED' else None
            ),
            elements=5 * (8 ** spec.uniform_refinements),
            ndofs=100 + index,
            solve_s=solve_s,
            peak_ram_mb=10.0,
            output_mb=1.0,
            source_mass_relative_residual=1e-12,
            max_implicit_relative_residual=1e-12,
        )
        for index, spec in enumerate(plan.attempts)
    )


def _passing_pairs():
    plan = _plan()
    return (
        PairMetrics(
            coarse_attempt_id=plan.time_track[0],
            fine_attempt_id=plan.time_track[1],
            magnitude_max_abs_db=0.30,
            magnitude_max_relative=0.03,
            phase_max_error_deg=4.0,
            complex_rms_relative=0.20,
            magnitude_sample_count=281,
            phase_sample_count=281,
        ),
        PairMetrics(
            coarse_attempt_id=plan.time_track[1],
            fine_attempt_id=plan.time_track[2],
            magnitude_max_abs_db=0.20,
            magnitude_max_relative=0.02,
            phase_max_error_deg=3.0,
            complex_rms_relative=0.10,
            magnitude_sample_count=281,
            phase_sample_count=281,
        ),
        PairMetrics(
            coarse_attempt_id=plan.space_track[0],
            fine_attempt_id=plan.space_track[1],
            magnitude_max_abs_db=0.40,
            magnitude_max_relative=0.04,
            phase_max_error_deg=5.0,
            complex_rms_relative=0.25,
            magnitude_sample_count=281,
            phase_sample_count=281,
        ),
        PairMetrics(
            coarse_attempt_id=plan.space_track[1],
            fine_attempt_id=plan.space_track[2],
            magnitude_max_abs_db=0.20,
            magnitude_max_relative=0.02,
            phase_max_error_deg=3.0,
            complex_rms_relative=0.12,
            magnitude_sample_count=281,
            phase_sample_count=281,
        ),
    )


def test_exact_authority_binding_rejects_stale_r100a() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match='r100a_semantic_hash binding mismatch'):
        validate_exact_authority_binding(
            plan,
            r100a_manifest_id=plan.authority.r100a_manifest_id,
            r100a_semantic_hash='0' * 64,
            candidate_manifest_hash=plan.authority.candidate_manifest_hash,
            candidate_id=plan.authority.candidate_id,
            candidate_semantic_hash=plan.authority.candidate_semantic_hash,
            candidate_source_commit_sha=plan.authority.candidate_source_commit_sha,
        )


def test_failed_attempt_is_preserved_and_blocks_promotion() -> None:
    plan = _plan()
    attempts = list(_attempts())
    attempts[1] = attempts[1].model_copy(
        update={
            'status': 'FAILED',
            'reason_code': 'solver_process_failure',
            'reason': 'solver failed',
            'numerical_identity_sha256': None,
        }
    )
    decision = evaluate_bounded_experiment(
        plan,
        attempt_results=tuple(attempts),
        pair_metrics=(),
    )
    assert decision.outcome == 'BLOCKED'
    assert decision.promoted_attempt_id is None
    assert decision.attempt_results[1].status == 'FAILED'
    assert 'attempt_failed:solver_process_failure' in decision.conclusion_codes


def test_nonconverged_track_cannot_promote_finest_trace() -> None:
    plan = _plan()
    pairs = list(_passing_pairs())
    pairs[1] = pairs[1].model_copy(
        update={
            'magnitude_max_abs_db': 10.0,
            'complex_rms_relative': 0.30,
        }
    )
    decision = evaluate_bounded_experiment(
        plan,
        attempt_results=_attempts(),
        pair_metrics=tuple(pairs),
    )
    assert decision.outcome == 'FAIL'
    assert decision.promoted_attempt_id is None
    time_track = next(item for item in decision.track_evaluations if item.track_id == 'time')
    assert time_track.status == 'FAIL'


def test_both_tracks_must_pass_before_exact_finest_space_attempt_is_promoted() -> None:
    plan = _plan()
    decision = evaluate_bounded_experiment(
        plan,
        attempt_results=_attempts(),
        pair_metrics=_passing_pairs(),
    )
    assert decision.outcome == 'PASS'
    assert decision.promoted_attempt_id == plan.space_track[-1]


def test_deterministic_report_identity_excludes_incidental_runtime_measurements() -> None:
    plan = _plan()
    first = evaluate_bounded_experiment(
        plan,
        attempt_results=_attempts(solve_s=1.0),
        pair_metrics=_passing_pairs(),
    )
    second = evaluate_bounded_experiment(
        plan,
        attempt_results=_attempts(solve_s=99.0),
        pair_metrics=_passing_pairs(),
    )
    assert (
        first.deterministic_report_identity_sha256
        == second.deterministic_report_identity_sha256
    )
