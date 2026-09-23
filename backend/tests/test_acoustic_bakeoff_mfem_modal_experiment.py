from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.acoustic_bakeoff_mfem_modal_experiment import (
    ExperimentAttemptResult,
    MfemModalExperimentPlan,
    PairMetrics,
    evaluate_modal_experiment,
    load_experiment_plan,
    validate_exact_authority_binding,
)


PLAN_PATH = Path('benchmarks/acoustics/r100b_mfem_modal_experiment_plan.json')


def _plan() -> MfemModalExperimentPlan:
    return load_experiment_plan(PLAN_PATH)


def _attempts(status: str = 'COMPLETED'):
    plan = _plan()
    return tuple(
        ExperimentAttemptResult(
            attempt_id=spec.attempt_id,
            status=status,
            reason_code='completed' if status == 'COMPLETED' else 'synthetic_block',
            reason='completed' if status == 'COMPLETED' else 'blocked',
            numerical_identity_sha256=(
                f'{index + 1:064x}' if status == 'COMPLETED' else None
            ),
            sample_rate_hz=spec.sample_rate_hz,
            sample_count=2 * spec.sample_rate_hz if status == 'COMPLETED' else None,
            solve_s=1.0 if status == 'COMPLETED' else None,
            peak_ram_mb=10.0,
            output_mb=1.0 if status == 'COMPLETED' else None,
            source_mass_relative_residual=1e-13 if status == 'COMPLETED' else None,
        )
        for index, spec in enumerate(plan.attempts)
    )


def _pairs(*, final_abs_db: float = 0.2, final_relative: float = 0.02,
           final_phase: float = 3.0, first_rms: float = 0.2,
           final_rms: float = 0.1):
    return (
        PairMetrics(
            coarse_attempt_id='modal-6000',
            fine_attempt_id='modal-9000',
            magnitude_max_abs_db=0.3,
            magnitude_max_relative=0.03,
            phase_max_error_deg=4.0,
            complex_rms_relative=first_rms,
            magnitude_sample_count=281,
            phase_sample_count=281,
        ),
        PairMetrics(
            coarse_attempt_id='modal-9000',
            fine_attempt_id='modal-12000',
            magnitude_max_abs_db=final_abs_db,
            magnitude_max_relative=final_relative,
            phase_max_error_deg=final_phase,
            complex_rms_relative=final_rms,
            magnitude_sample_count=281,
            phase_sample_count=281,
        ),
    )


def test_plan_identity_and_exact_fixed_spatial_modal_output_grid() -> None:
    plan = _plan()
    assert plan.schema_version == 'r100b-mfem-modal-experiment-plan-1'
    assert plan.spatial_system.h1_order == 2
    assert plan.spatial_system.uniform_refinements == 1
    assert plan.spatial_system.expected_elements == 40
    assert plan.spatial_system.expected_ndofs == 525
    assert plan.modal_configuration.basis_size == 525
    assert plan.modal_configuration.truncation_rule == (
        'none; retain all 525 generalized eigenpairs'
    )
    assert [(item.attempt_id, item.sample_rate_hz) for item in plan.attempts] == [
        ('modal-6000', 6000),
        ('modal-9000', 9000),
        ('modal-12000', 12000),
    ]
    assert plan.numerical_contract.magnitude_absolute_tolerance_db == 0.75
    assert plan.numerical_contract.magnitude_relative_tolerance == 0.05
    assert plan.numerical_contract.phase_tolerance_deg == 8.0


def test_parameter_substitution_is_rejected() -> None:
    payload = _plan().model_dump(mode='json')
    payload['attempts'][1]['sample_rate_hz'] = 8000
    with pytest.raises(ValidationError, match='attempts are frozen'):
        MfemModalExperimentPlan.model_validate(payload)


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


def test_pass_requires_decreasing_adjacent_error_and_current_tolerance() -> None:
    decision = evaluate_modal_experiment(
        _plan(),
        attempt_results=_attempts(),
        pair_metrics=_pairs(),
    )
    assert decision.outcome == 'PASS'
    assert decision.promoted_attempt_id is None
    assert decision.complex_rms_relative_strictly_decreasing is True
    assert decision.final_pair_within_current_tolerance is True


def test_nonconverged_result_cannot_be_promoted() -> None:
    decision = evaluate_modal_experiment(
        _plan(),
        attempt_results=_attempts(),
        pair_metrics=_pairs(final_abs_db=2.0, final_rms=0.3),
    )
    assert decision.outcome == 'FAIL'
    assert decision.promoted_attempt_id is None
    assert 'modal_time_track_nonconverged' in decision.conclusion_codes
    assert 'magnitude_absolute_tolerance_exceeded' in decision.violations


def test_incomplete_predeclared_attempt_is_blocked() -> None:
    attempts = list(_attempts())
    attempts[2] = attempts[2].model_copy(
        update={
            'status': 'BLOCKED',
            'reason_code': 'resource_wall_timeout',
            'reason': 'timeout',
            'numerical_identity_sha256': None,
            'sample_count': None,
        }
    )
    decision = evaluate_modal_experiment(
        _plan(),
        attempt_results=tuple(attempts),
        pair_metrics=(),
    )
    assert decision.outcome == 'BLOCKED'
    assert decision.promoted_attempt_id is None
    assert 'attempt_blocked:resource_wall_timeout' in decision.conclusion_codes


def test_decision_serialization_reopens_without_semantic_change(tmp_path: Path) -> None:
    decision = evaluate_modal_experiment(
        _plan(),
        attempt_results=_attempts(),
        pair_metrics=_pairs(),
    )
    path = tmp_path / 'decision.json'
    path.write_text(decision.model_dump_json(indent=2), encoding='utf-8')
    reopened = type(decision).model_validate_json(path.read_text(encoding='utf-8'))
    assert reopened == decision
    assert (
        reopened.deterministic_report_identity_sha256
        == decision.deterministic_report_identity_sha256
    )
