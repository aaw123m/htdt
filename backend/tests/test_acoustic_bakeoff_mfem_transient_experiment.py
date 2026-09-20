from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.acoustic_bakeoff_mfem_transient_experiment import (
    ExperimentAttemptResult,
    MfemTransientExperimentPlan,
    ModalReferenceMetrics,
    PairMetrics,
    evaluate_transient_experiment,
    load_experiment_plan,
    validate_exact_authority_binding,
    validate_modal_reference_system_identity,
)


PLAN_PATH = Path('benchmarks/acoustics/r100b_mfem_transient_experiment_plan.json')


def _plan() -> MfemTransientExperimentPlan:
    return load_experiment_plan(PLAN_PATH)


def _attempts(status: str = 'COMPLETED'):
    plan = _plan()
    return tuple(
        ExperimentAttemptResult(
            attempt_id=spec.attempt_id,
            status=status,
            reason_code='completed' if status == 'COMPLETED' else 'synthetic_solve_failure',
            reason='completed' if status == 'COMPLETED' else 'solve failed',
            sample_rate_hz=spec.sample_rate_hz,
            output_interval_s=(1.0 / spec.sample_rate_hz if status == 'COMPLETED' else None),
            substeps_per_output_interval=(
                plan.integrator.substeps_per_output_interval if status == 'COMPLETED' else None
            ),
            internal_step_s=(
                1.0
                / (spec.sample_rate_hz * plan.integrator.substeps_per_output_interval)
                if status == 'COMPLETED'
                else None
            ),
            internal_step_count=(
                (2 * spec.sample_rate_hz - 1)
                * plan.integrator.substeps_per_output_interval
                if status == 'COMPLETED'
                else None
            ),
            numerical_identity_sha256=(f'{index + 1:064x}' if status == 'COMPLETED' else None),
            pressure_record_sha256=(f'{index + 11:064x}' if status == 'COMPLETED' else None),
            sample_count=(2 * spec.sample_rate_hz if status == 'COMPLETED' else None),
            solve_s=(1.0 if status == 'COMPLETED' else None),
            factorization_s=(0.1 if status == 'COMPLETED' else None),
            peak_ram_mb=50.0,
            output_mb=(1.0 if status == 'COMPLETED' else None),
            mass_solve_relative_residual=(1e-14 if status == 'COMPLETED' else None),
            max_checked_step_relative_residual=(1e-14 if status == 'COMPLETED' else None),
            checked_step_residual_count=(10 if status == 'COMPLETED' else None),
            denominator_nnz=(100 if status == 'COMPLETED' else None),
            lu_nnz=(200 if status == 'COMPLETED' else None),
            factor_storage_mb=(1.0 if status == 'COMPLETED' else None),
            factorization_identity_sha256=(
                f'{index + 21:064x}' if status == 'COMPLETED' else None
            ),
            sparse_candidate_path=True,
            dense_eigendecomposition_used=False,
            factorization_reused=True,
        )
        for index, spec in enumerate(plan.attempts)
    )


def _pairs(
    *,
    first_rms: float = 0.02,
    final_rms: float = 0.005,
    final_abs_db: float = 0.2,
    final_relative: float = 0.02,
    final_phase: float = 2.0,
):
    return (
        PairMetrics(
            coarse_attempt_id='transient-6000',
            fine_attempt_id='transient-9000',
            magnitude_max_abs_db=0.4,
            magnitude_max_relative=0.04,
            phase_max_error_deg=4.0,
            complex_rms_relative=first_rms,
            magnitude_sample_count=281,
            phase_sample_count=281,
        ),
        PairMetrics(
            coarse_attempt_id='transient-9000',
            fine_attempt_id='transient-12000',
            magnitude_max_abs_db=final_abs_db,
            magnitude_max_relative=final_relative,
            phase_max_error_deg=final_phase,
            complex_rms_relative=final_rms,
            magnitude_sample_count=281,
            phase_sample_count=281,
        ),
    )


def _modal_metrics(*, relative: float = 0.01):
    return tuple(
        ModalReferenceMetrics(
            attempt_id=f'transient-{rate}',
            modal_attempt_id=f'modal-{rate}',
            sample_rate_hz=rate,
            magnitude_max_abs_db=0.1,
            magnitude_max_relative=relative,
            phase_max_error_deg=1.0,
            complex_rms_relative=0.001,
            magnitude_sample_count=281,
            phase_sample_count=281,
        )
        for rate in (6000, 9000, 12000)
    )


def test_exact_frozen_plan_identity_and_integrator_contract() -> None:
    plan = _plan()
    assert plan.schema_version == 'r100b-mfem-transient-experiment-plan-2'
    assert plan.plan_id == 'r100b-mfem-gl2-exact-two-halfsteps-current-authority-2026-09-20'
    assert plan.spatial_system.model_dump(mode='json') == {
        'h1_order': 2,
        'uniform_refinements': 1,
        'expected_elements': 40,
        'expected_ndofs': 525,
        'geometry': 'exact-five-hex-l-prism',
        'boundary_model': 'natural-neumann-rigid',
        'primary_field': 'velocity_potential_phi',
        'governing_equation': 'M*phi_tt+Kc2*phi=c^2*b*q',
        'mass_assembly': 'MFEM MassIntegrator',
        'stiffness_assembly': 'MFEM DiffusionIntegrator(c^2)',
        'source_functional': 'MFEM DomainLFIntegrator(DeltaCoefficient)',
        'receiver_functional': 'MFEM DomainLFIntegrator(DeltaCoefficient)',
    }
    assert plan.integrator.algorithm_id == 'gauss-legendre-2stage-pade22-linear'
    assert plan.integrator.order == 4
    assert plan.integrator.substeps_per_output_interval == 2
    assert plan.integrator.substep_policy == (
        'fixed equal GL2 substeps per output interval; no adaptive stepping'
    )
    assert plan.integrator.residual_relative_tolerance == 1e-10
    assert plan.integrator.candidate_matrix_policy == (
        'sparse CSR/CSC only; no dense inverse; no eigendecomposition'
    )
    assert [(item.attempt_id, item.sample_rate_hz) for item in plan.attempts] == [
        ('transient-6000', 6000),
        ('transient-9000', 9000),
        ('transient-12000', 12000),
    ]
    assert plan.modal_reference.pull_request == 277
    assert plan.modal_reference.production_execution_candidate is False
    assert plan.numerical_contract.magnitude_absolute_tolerance_db == 0.75
    assert plan.numerical_contract.magnitude_relative_tolerance == 0.05
    assert plan.numerical_contract.phase_tolerance_deg == 8.0


def test_parameter_substitution_is_rejected() -> None:
    payload = _plan().model_dump(mode='json')
    payload['attempts'][1]['sample_rate_hz'] = 8000
    with pytest.raises(ValidationError, match='attempts are frozen'):
        MfemTransientExperimentPlan.model_validate(payload)


def test_stale_r100a_and_mfem_binding_is_rejected() -> None:
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
    with pytest.raises(ValueError, match='candidate_source_commit_sha binding mismatch'):
        validate_exact_authority_binding(
            plan,
            r100a_manifest_id=plan.authority.r100a_manifest_id,
            r100a_semantic_hash=plan.authority.r100a_semantic_hash,
            candidate_manifest_hash=plan.authority.candidate_manifest_hash,
            candidate_id=plan.authority.candidate_id,
            candidate_semantic_hash=plan.authority.candidate_semantic_hash,
            candidate_source_commit_sha='0' * 40,
        )


def test_integrator_configuration_identity_is_frozen() -> None:
    payload = _plan().model_dump(mode='json')
    payload['integrator']['residual_relative_tolerance'] = 1e-8
    with pytest.raises(ValidationError, match='residual tolerance is frozen'):
        MfemTransientExperimentPlan.model_validate(payload)
    payload = _plan().model_dump(mode='json')
    payload['integrator']['algorithm_id'] = 'newmark'
    with pytest.raises(ValidationError):
        MfemTransientExperimentPlan.model_validate(payload)


def test_deterministic_replay_has_identical_decision_identity() -> None:
    first = evaluate_transient_experiment(
        _plan(),
        attempt_results=_attempts(),
        pair_metrics=_pairs(),
        modal_reference_metrics=_modal_metrics(),
    )
    second = evaluate_transient_experiment(
        _plan(),
        attempt_results=_attempts(),
        pair_metrics=_pairs(),
        modal_reference_metrics=_modal_metrics(),
    )
    assert first == second
    assert first.deterministic_report_identity_sha256 == second.deterministic_report_identity_sha256
    assert first.outcome == 'PASS'
    assert first.production_adoption_decision == 'NO_GO'


def test_numerical_solve_failure_fails_closed() -> None:
    attempts = list(_attempts())
    attempts[1] = _attempts(status='FAILED')[1]
    decision = evaluate_transient_experiment(
        _plan(),
        attempt_results=tuple(attempts),
        pair_metrics=(),
        modal_reference_metrics=(),
    )
    assert decision.outcome == 'BLOCKED'
    assert decision.numerical_convergence_status == 'BLOCKED'
    assert decision.modal_reference_agreement_status == 'BLOCKED'
    assert decision.current_r100a_tolerance_status == 'BLOCKED'
    assert decision.production_suitability_status == 'BLOCKED'
    assert decision.production_adoption_decision == 'NO_GO'


def test_pass_cannot_relax_predeclared_current_tolerance() -> None:
    decision = evaluate_transient_experiment(
        _plan(),
        attempt_results=_attempts(),
        pair_metrics=_pairs(final_relative=0.0500001),
        modal_reference_metrics=_modal_metrics(),
    )
    assert decision.outcome == 'FAIL'
    assert decision.current_r100a_tolerance_status == 'FAIL'
    assert 'magnitude_relative_tolerance_exceeded' in decision.violations

    payload = _plan().model_dump(mode='json')
    payload['numerical_contract']['magnitude_relative_tolerance'] = 0.06
    with pytest.raises(ValidationError):
        MfemTransientExperimentPlan.model_validate(payload)


def test_modal_reference_mismatch_is_detected() -> None:
    plan = _plan()
    validate_modal_reference_system_identity(
        plan, plan.modal_reference.expected_system_numeric_identity_sha256
    )
    with pytest.raises(ValueError, match='modal reference semidiscrete system mismatch'):
        validate_modal_reference_system_identity(plan, '0' * 64)


def test_modal_reference_agreement_gate_is_independent() -> None:
    decision = evaluate_transient_experiment(
        _plan(),
        attempt_results=_attempts(),
        pair_metrics=_pairs(),
        modal_reference_metrics=_modal_metrics(relative=0.051),
    )
    assert decision.numerical_convergence_status == 'PASS'
    assert decision.current_r100a_tolerance_status == 'PASS'
    assert decision.modal_reference_agreement_status == 'FAIL'
    assert decision.outcome == 'FAIL'
    assert decision.production_adoption_decision == 'NO_GO'


def test_dense_or_nonreused_candidate_path_fails_production_suitability() -> None:
    attempts = list(_attempts())
    attempts[0] = attempts[0].model_copy(
        update={'dense_eigendecomposition_used': True, 'factorization_reused': False}
    )
    decision = evaluate_transient_experiment(
        _plan(),
        attempt_results=tuple(attempts),
        pair_metrics=_pairs(),
        modal_reference_metrics=_modal_metrics(),
    )
    assert decision.production_suitability_status == 'FAIL'
    assert decision.outcome == 'FAIL'
    assert 'production_transient_execution_contract_not_met' in decision.violations



def test_exact_two_halfstep_and_output_sampling_authority_is_enforced() -> None:
    plan = _plan()
    attempts = _attempts()
    for spec, result in zip(plan.attempts, attempts):
        output_interval_s = 1.0 / spec.sample_rate_hz
        assert result.sample_count == 2 * spec.sample_rate_hz
        assert result.output_interval_s == output_interval_s
        assert result.substeps_per_output_interval == 2
        assert result.internal_step_s == output_interval_s / 2.0
        assert result.internal_step_count == (result.sample_count - 1) * 2

    tampered = list(attempts)
    tampered[0] = tampered[0].model_copy(update={'substeps_per_output_interval': 1})
    with pytest.raises(ValueError, match='substep count differs'):
        evaluate_transient_experiment(
            plan,
            attempt_results=tuple(tampered),
            pair_metrics=_pairs(),
            modal_reference_metrics=_modal_metrics(),
        )


def test_invalid_substep_configuration_fails_closed() -> None:
    payload = _plan().model_dump(mode='json')
    payload['integrator']['substeps_per_output_interval'] = 3
    with pytest.raises(ValidationError):
        MfemTransientExperimentPlan.model_validate(payload)

    payload = _plan().model_dump(mode='json')
    payload['integrator']['substeps_per_output_interval'] = 1
    with pytest.raises(ValidationError, match='requires exactly 2'):
        MfemTransientExperimentPlan.model_validate(payload)


def test_pr281_single_step_plan_contract_remains_parseable() -> None:
    payload = _plan().model_dump(mode='json')
    payload['schema_version'] = 'r100b-mfem-transient-experiment-plan-1'
    payload['plan_id'] = 'r100b-mfem-low-dispersion-transient-current-authority-2026-09-20'
    payload['integrator']['substeps_per_output_interval'] = 1
    legacy = MfemTransientExperimentPlan.model_validate(payload)
    assert legacy.integrator.substeps_per_output_interval == 1
    assert legacy.schema_version == 'r100b-mfem-transient-experiment-plan-1'
