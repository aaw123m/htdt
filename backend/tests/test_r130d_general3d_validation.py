from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from htdt.r130d_general3d_validation import (
    EVIDENCE_SCHEMA,
    PairMetrics,
    ObservableContractMismatch,
    analytic_complex_harmonic_spectrum,
    assess_refinement_series,
    compare_complex_transfer,
    load_evidence,
    load_target_window_diagnostic_plan,
    load_validation_plan,
    save_evidence,
    target_window_sampling_metadata,
    target_window_zoh_spectrum,
    target_window_zoh_transfer,
    validate_exact_binding,
    validate_physical_observable_contract,
    validate_refinement_schedule,
    validation_decision,
    validation_decision_v2,
)


PLAN = Path(__file__).parents[2] / 'benchmarks' / 'acoustics' / 'r130d_general3d_validation_plan.json'
DIAGNOSTIC_PLAN = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_target_window_diagnostic_plan.json'
)


def _plan():
    return load_validation_plan(PLAN)


def _pass_metrics() -> PairMetrics:
    return PairMetrics(
        complex_rms_relative=0.01,
        magnitude_max_relative=0.01,
        magnitude_max_db=0.1,
        phase_max_deg=1.0,
        mask_floor=1.0e-9,
        compared_frequency_count=2,
        frequency_metrics=(
            {
                'frequency_hz': 40.0,
                'masked_in': True,
                'magnitude_relative': 0.01,
                'magnitude_db': 0.1,
                'phase_deg': 1.0,
                'complex_relative': 0.01,
            },
            {
                'frequency_hz': 80.0,
                'masked_in': True,
                'magnitude_relative': 0.01,
                'magnitude_db': 0.1,
                'phase_deg': 1.0,
                'complex_relative': 0.01,
            },
        ),
    )


def test_exact_fixture_and_reference_mesh_identity_are_deterministic():
    plan = _plan()
    assert plan.fixture.source_key == 'sloped'
    assert plan.fixture.vertices_m[6] == (4.0, 4.0, 3.0)
    assert plan.fixture.base_tetrahedralization_volume_m3 == 56.0
    assert len(plan.fixture.base_tetrahedra) == 6
    assert plan.reference_mesh_sha256(1) == plan.reference_mesh_sha256(1)
    assert plan.reference_mesh_sha256(1) != plan.reference_mesh_sha256(2)
    assert len(plan.fixture_sha256()) == 64


def test_exact_binding_rejects_stale_geometry_and_source_receiver_mismatch():
    plan = _plan()
    kwargs = dict(
        vertices_m=plan.fixture.vertices_m,
        faces=plan.fixture.faces,
        source_position_m=plan.fixture.source_position_m,
        receiver_position_m=plan.fixture.receiver_position_m,
        quantity=plan.physical_quantity.quantity,
        unit=plan.physical_quantity.unit,
        phasor_convention=plan.physical_quantity.phasor_convention,
        analysis_fourier_kernel=plan.physical_quantity.analysis_fourier_kernel,
        pffdtd_source_commit_sha=plan.pffdtd.source_commit_sha,
        independent_source_commit_sha=plan.independent_reference.source_commit_sha,
    )
    validate_exact_binding(plan, **kwargs)

    stale = list(plan.fixture.vertices_m)
    stale[6] = (4.0, 4.0, 3.1)
    with pytest.raises(ValueError, match='vertices'):
        validate_exact_binding(plan, **{**kwargs, 'vertices_m': stale})
    with pytest.raises(ValueError, match='source_position_m'):
        validate_exact_binding(
            plan,
            **{**kwargs, 'source_position_m': (1.6, 2.0, 2.0)},
        )
    with pytest.raises(ValueError, match='receiver_position_m'):
        validate_exact_binding(
            plan,
            **{**kwargs, 'receiver_position_m': (2.6, 2.0, 2.0)},
        )


def test_quantity_and_convention_mismatch_fail_closed():
    plan = _plan()
    base = dict(
        vertices_m=plan.fixture.vertices_m,
        faces=plan.fixture.faces,
        source_position_m=plan.fixture.source_position_m,
        receiver_position_m=plan.fixture.receiver_position_m,
        quantity=plan.physical_quantity.quantity,
        unit=plan.physical_quantity.unit,
        phasor_convention=plan.physical_quantity.phasor_convention,
        analysis_fourier_kernel=plan.physical_quantity.analysis_fourier_kernel,
        pffdtd_source_commit_sha=plan.pffdtd.source_commit_sha,
        independent_source_commit_sha=plan.independent_reference.source_commit_sha,
    )
    with pytest.raises(ValueError, match='quantity'):
        validate_exact_binding(plan, **{**base, 'quantity': 'complex_pressure'})
    with pytest.raises(ValueError, match='unit'):
        validate_exact_binding(plan, **{**base, 'unit': 'Pa'})
    with pytest.raises(ValueError, match='phasor_convention'):
        validate_exact_binding(
            plan, **{**base, 'phasor_convention': 'exp(+i*omega*t)'}
        )
    with pytest.raises(ValueError, match='analysis_fourier_kernel'):
        validate_exact_binding(
            plan, **{**base, 'analysis_fourier_kernel': 'exp(-i*omega*t)'}
        )


def test_predeclared_refinement_schedule_is_exact():
    plan = _plan()
    validate_refinement_schedule(
        plan,
        reference_refinements=(1, 2, 3),
        pffdtd_points_per_wavelength=(8.0, 10.0, 12.0),
    )
    with pytest.raises(ValueError, match='reference refinement schedule'):
        validate_refinement_schedule(
            plan,
            reference_refinements=(1, 3),
            pffdtd_points_per_wavelength=(8.0, 10.0, 12.0),
        )
    with pytest.raises(ValueError, match='PFFDTD refinement schedule'):
        validate_refinement_schedule(
            plan,
            reference_refinements=(1, 2, 3),
            pffdtd_points_per_wavelength=(8.0, 10.0, 14.0),
        )


def test_complex_metrics_and_convergence_decision_semantics():
    plan = _plan()
    metrics = compare_complex_transfer(
        reference=((1.0, 0.0), (0.0, 2.0)),
        candidate=((1.01, 0.0), (0.0, 1.98)),
        frequency_hz=(40.0, 80.0),
        magnitude_mask_relative_db=-50.0,
    )
    assert metrics.complex_rms_relative < 0.02
    decision = validation_decision(
        execution_status='PASS',
        reference_metrics=_pass_metrics(),
        pffdtd_metrics=_pass_metrics(),
        cross_solver_metrics=_pass_metrics(),
        plan=plan,
    )
    assert decision['fixture_validation_result'] == 'PASS'
    assert decision['general_3d_validation_state'] == 'VALIDATED_BOUNDED_SLOPED_FIXTURE'


def test_nonconverged_reference_blocks_cross_solver_pass():
    plan = _plan()
    failed = _pass_metrics().model_copy(
        update={'complex_rms_relative': 0.5}
    )
    decision = validation_decision(
        execution_status='PASS',
        reference_metrics=failed,
        pffdtd_metrics=_pass_metrics(),
        cross_solver_metrics=_pass_metrics(),
        plan=plan,
    )
    assert decision['reference_self_convergence_status'] == 'FAIL'
    assert decision['cross_solver_agreement_status'] == 'BLOCKED'
    assert decision['fixture_validation_result'] == 'FAIL'
    assert decision['general_3d_validation_state'] == 'NOT_VALIDATED'


def test_blocked_execution_is_not_physics_fail_or_pass():
    plan = _plan()
    decision = validation_decision(
        execution_status='BLOCKED',
        reference_metrics=None,
        pffdtd_metrics=None,
        cross_solver_metrics=None,
        plan=plan,
    )
    assert decision['reference_build_execution_status'] == 'BLOCKED'
    assert decision['fixture_validation_result'] == 'BLOCKED'
    assert decision['general_3d_validation_state'] == 'NOT_VALIDATED'


def test_evidence_persistence_reopen_and_tamper_detection(tmp_path: Path):
    path = tmp_path / 'evidence.json'
    payload = {
        'schema_version': EVIDENCE_SCHEMA,
        'plan_id': _plan().plan_id,
        'fixture_validation_result': 'BLOCKED',
        'general_3d_validation_state': 'NOT_VALIDATED',
    }
    digest = save_evidence(path, payload)
    assert len(digest) == 64
    assert load_evidence(path) == payload

    document = json.loads(path.read_text(encoding='utf-8'))
    document['payload']['fixture_validation_result'] = 'PASS'
    path.write_text(json.dumps(document), encoding='utf-8')
    with pytest.raises(ValueError, match='modified'):
        load_evidence(path)

def _physical_contract(plan):
    return {
        'quantity': plan.physical_quantity.quantity,
        'unit': plan.physical_quantity.unit,
        'source_position_m': plan.fixture.source_position_m,
        'receiver_position_m': plan.fixture.receiver_position_m,
        'source_convention': plan.physical_quantity.source_contract,
        'pressure_normalization': plan.physical_quantity.pressure_conversion,
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
        'geometry_sha256': plan.fixture_sha256(),
        'geometry_units': plan.physical_quantity.geometry_units,
    }


def test_physical_observable_contract_rejects_normalization_and_geometry_mismatch():
    plan = _plan()
    expected = _physical_contract(plan)
    validate_physical_observable_contract(expected=expected, actual=dict(expected))

    bad_normalization = dict(expected)
    bad_normalization['excitation_normalization'] = 'arbitrary fitted scale'
    with pytest.raises(ObservableContractMismatch, match='excitation_normalization'):
        validate_physical_observable_contract(
            expected=expected,
            actual=bad_normalization,
        )

    bad_geometry = dict(expected)
    bad_geometry['geometry_sha256'] = '0' * 64
    with pytest.raises(ObservableContractMismatch, match='geometry_sha256'):
        validate_physical_observable_contract(expected=expected, actual=bad_geometry)


def test_self_convergence_requires_ordered_three_level_trend_and_blocks_cross_solver():
    plan = _plan()
    coarse = _pass_metrics().model_copy(
        update={
            'complex_rms_relative': 0.04,
            'magnitude_max_relative': 0.06,
            'phase_max_deg': 4.0,
        }
    )
    fine = _pass_metrics().model_copy(
        update={
            'complex_rms_relative': 0.02,
            'magnitude_max_relative': 0.03,
            'phase_max_deg': 2.0,
        }
    )
    reference = assess_refinement_series(
        (coarse, fine),
        plan.acceptance.reference_self_convergence,
    )
    assert reference.state == 'SELF_CONVERGENCE_PASS'

    non_decreasing = _pass_metrics().model_copy(
        update={
            'complex_rms_relative': 0.03,
            'magnitude_max_relative': 0.04,
            'phase_max_deg': 3.0,
        }
    )
    pffdtd = assess_refinement_series(
        (fine, non_decreasing),
        plan.acceptance.pffdtd_self_convergence,
    )
    assert pffdtd.state == 'SELF_CONVERGENCE_FAILED'
    decision = validation_decision_v2(
        execution_state='PASS',
        contract_state='MATCH',
        reference_assessment=reference,
        pffdtd_assessment=pffdtd,
        cross_solver_metrics=None,
        plan=plan,
    )
    assert decision['cross_solver_state'] == 'CROSS_SOLVER_BLOCKED'
    assert decision['validation_state'] == 'NOT_VALIDATED'


def test_missing_adjacent_level_fails_closed():
    plan = _plan()
    with pytest.raises(ValueError, match='at least two adjacent comparisons'):
        assess_refinement_series(
            (_pass_metrics(),),
            plan.acceptance.reference_self_convergence,
        )


def test_cross_solver_is_evaluated_only_after_both_self_convergence_pass():
    plan = _plan()
    first = _pass_metrics().model_copy(
        update={
            'complex_rms_relative': 0.04,
            'magnitude_max_relative': 0.06,
            'phase_max_deg': 4.0,
        }
    )
    second = _pass_metrics().model_copy(
        update={
            'complex_rms_relative': 0.01,
            'magnitude_max_relative': 0.02,
            'phase_max_deg': 1.0,
        }
    )
    reference = assess_refinement_series(
        (first, second), plan.acceptance.reference_self_convergence
    )
    pffdtd = assess_refinement_series(
        (first, second), plan.acceptance.pffdtd_self_convergence
    )
    with pytest.raises(ValueError, match='cross-solver metrics are required'):
        validation_decision_v2(
            execution_state='PASS',
            contract_state='MATCH',
            reference_assessment=reference,
            pffdtd_assessment=pffdtd,
            cross_solver_metrics=None,
            plan=plan,
        )


def test_contract_mismatch_blocks_cross_solver_even_with_converged_series():
    plan = _plan()
    first = _pass_metrics().model_copy(
        update={
            'complex_rms_relative': 0.04,
            'magnitude_max_relative': 0.06,
            'phase_max_deg': 4.0,
        }
    )
    second = _pass_metrics().model_copy(
        update={
            'complex_rms_relative': 0.01,
            'magnitude_max_relative': 0.02,
            'phase_max_deg': 1.0,
        }
    )
    reference = assess_refinement_series(
        (first, second), plan.acceptance.reference_self_convergence
    )
    pffdtd = assess_refinement_series(
        (first, second), plan.acceptance.pffdtd_self_convergence
    )
    decision = validation_decision_v2(
        execution_state='PASS',
        contract_state='CONTRACT_MISMATCH',
        reference_assessment=reference,
        pffdtd_assessment=pffdtd,
        cross_solver_metrics=_pass_metrics(),
        plan=plan,
    )
    assert decision['execution_state'] == 'PASS'
    assert decision['contract_state'] == 'CONTRACT_MISMATCH'
    assert decision['cross_solver_state'] == 'CROSS_SOLVER_BLOCKED'
    assert decision['validation_state'] == 'NOT_VALIDATED'


def test_pr282_summary_fixture_and_solver_identity_remain_compatible():
    plan = _plan()
    path = (
        Path(__file__).parents[2]
        / 'benchmarks'
        / 'acoustics'
        / 'r130d_general3d_validation_run7_summary.json'
    )
    previous = json.loads(path.read_text(encoding='utf-8'))
    assert previous['fixture_id'] == plan.fixture.fixture_id
    assert previous['fixture_sha256'] == plan.fixture_sha256()
    assert previous['pffdtd_implementation_sha'] == plan.pffdtd.source_commit_sha
    assert previous['independent_solver_sha'] == (
        plan.independent_reference.source_commit_sha
    )
    assert previous['decision']['general_3d_validation_state'] == 'NOT_VALIDATED'


def test_new_plan_is_record_coherent_and_refinement_ordered():
    plan = _plan()
    assert plan.physical_quantity.duration_s == 0.25
    assert all(
        float(frequency) * plan.physical_quantity.duration_s
        == round(float(frequency) * plan.physical_quantity.duration_s)
        for frequency in plan.physical_quantity.frequency_hz
    )
    assert plan.independent_reference.uniform_refinements == (1, 2, 3)
    assert plan.independent_reference.expected_element_counts == (48, 384, 3072)
    assert plan.independent_reference.expected_dofs == (125, 729, 4913)
    assert plan.pffdtd.points_per_wavelength == (8.0, 10.0, 12.0)




def test_target_window_diagnostic_plan_is_frozen_and_hash_bound():
    diagnostic = load_target_window_diagnostic_plan(DIAGNOSTIC_PLAN)
    assert diagnostic['target_duration_s'] == 0.25
    assert diagnostic['frequency_hz'] == [40.0, 80.0]
    assert diagnostic['series']['mfem_refinements'] == [1, 2, 3]
    assert diagnostic['series']['pffdtd_ppw'] == [8.0, 10.0, 12.0]
    assert diagnostic['operator']['operator_id'] == (
        'htdt.r130d.target_window_zoh_exp_integral'
    )
    assert diagnostic['decision_semantics']['diagnostic_only'] is True
    assert (
        diagnostic['decision_semantics']['canonical_observable_replaced']
        is False
    )


def test_target_window_zoh_known_complex_harmonic_converges_for_noninteger_t_over_dt():
    target_duration_s = 0.25
    frequencies = np.asarray([40.0, 80.0], dtype=np.float64)
    pressure_amplitude = 2.1 + 0.4j
    source_amplitude = 0.7 - 0.2j
    pressure_harmonic_hz = 53.0
    source_harmonic_hz = 17.0

    exact_pressure = analytic_complex_harmonic_spectrum(
        amplitude=pressure_amplitude,
        harmonic_frequency_hz=pressure_harmonic_hz,
        analysis_frequency_hz=frequencies,
        duration_s=target_duration_s,
    )
    exact_source = analytic_complex_harmonic_spectrum(
        amplitude=source_amplitude,
        harmonic_frequency_hz=source_harmonic_hz,
        analysis_frequency_hz=frequencies,
        duration_s=target_duration_s,
    )
    exact_transfer = exact_pressure / exact_source

    errors = []
    spectrum_errors = []
    for dt_s in (0.007, 0.0035, 0.00175, 0.000875):
        sample_count = int(np.ceil(target_duration_s / dt_s))
        assert not np.isclose(
            target_duration_s / dt_s,
            round(target_duration_s / dt_s),
            rtol=0.0,
            atol=1.0e-12,
        )
        times = np.arange(sample_count, dtype=np.float64) * dt_s
        pressure = pressure_amplitude * np.exp(
            -2j * np.pi * pressure_harmonic_hz * times
        )
        source = source_amplitude * np.exp(
            -2j * np.pi * source_harmonic_hz * times
        )

        aligned_pressure = target_window_zoh_spectrum(
            pressure,
            dt_s=dt_s,
            target_duration_s=target_duration_s,
            frequency_hz=frequencies,
        )
        aligned = target_window_zoh_transfer(
            pressure,
            source,
            dt_s=dt_s,
            target_duration_s=target_duration_s,
            frequency_hz=frequencies,
        )
        spectrum_errors.append(
            float(
                np.linalg.norm(aligned_pressure - exact_pressure)
                / np.linalg.norm(exact_pressure)
            )
        )
        errors.append(
            float(
                np.linalg.norm(aligned - exact_transfer)
                / np.linalg.norm(exact_transfer)
            )
        )

    assert all(
        following < previous
        for previous, following in zip(errors, errors[1:])
    )
    assert all(
        following < previous
        for previous, following in zip(
            spectrum_errors, spectrum_errors[1:]
        )
    )
    assert errors[-1] < 0.11
    assert spectrum_errors[-1] < spectrum_errors[0]


def test_target_window_sampling_metadata_exposes_native_overrun_exactly():
    metadata = target_window_sampling_metadata(
        solver='PFFDTD',
        requested_duration_s=0.25,
        dt_s=0.0007209661486505452,
        sample_count=347,
        frequency_hz=(40.0, 80.0),
        source_sampling='unit discrete impulse q[0]=1',
        pressure_sampling='native pressure trace',
    )
    assert metadata['actual_first_sample_time_s'] == 0.0
    assert metadata['actual_last_sample_time_s'] == pytest.approx(
        0.24945428743308865
    )
    assert metadata['actual_n_dt_s'] == pytest.approx(
        0.2501752535817392
    )
    assert metadata['n_dt_minus_requested_duration_s'] == pytest.approx(
        0.0001752535817392
    )
    assert metadata['target_effective_integration_interval_s'] == [0.0, 0.25]
    assert metadata['analysis_fourier_kernel'] == 'exp(+i*omega*t)'
