from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from htdt.acoustic_pffdtd_adapter import finite_record_pressure_transfer
from htdt.canonical_json import canonical_sha256
from htdt.r130d_general3d_validation import (
    EVIDENCE_SCHEMA,
    JOINT_TRANSLATION_CANONICAL_CELL_ID,
    JOINT_TRANSLATION_CELL_IDS,
    JOINT_TRANSLATION_OFFSET_CELLS,
    RECEIVER_POSITION_CANONICAL_CELL_ID,
    RECEIVER_POSITION_CELL_IDS,
    RECEIVER_POSITION_OFFSET_CELLS,
    SOURCE_POSITION_CANONICAL_CELL_ID,
    SOURCE_POSITION_CELL_IDS,
    SOURCE_POSITION_OFFSET_CELLS,
    STENCIL_SENSITIVITY_CANONICAL_CELL_ID,
    STENCIL_SENSITIVITY_VARIANT_IDS,
    TIME_GATE_BAND_CELL_IDS,
    TIME_GATE_CANONICAL_CELL_ID,
    TIME_GATE_CELL_IDS,
    TIME_GATE_PARTITION_CELL_IDS,
    TIME_GATE_PREFIX_CELL_IDS,
    TIME_GATE_TAIL_CELL_IDS,
    VOXEL_STAIRCASE_SENSITIVITY_CANONICAL_CELL_ID,
    VOXEL_STAIRCASE_SENSITIVITY_VARIANT_IDS,
    PairMetrics,
    ObservableContractMismatch,
    analytic_complex_harmonic_spectrum,
    apply_time_gate,
    analytic_sampled_complex_harmonic_left_rectangle_spectrum,
    assess_refinement_series,
    classify_dense_frequency_neighborhood,
    classify_frequency_neighborhood,
    classify_joint_translation_sensitivity,
    classify_receiver_position_sensitivity,
    classify_source_position_sensitivity,
    classify_spatial_representation_trend,
    classify_stencil_sensitivity,
    classify_time_gate_localization,
    classify_voxel_staircase_sensitivity,
    compare_complex_transfer,
    connected_air_domain_node_metrics,
    dense_frequency_grid,
    interpolation_stencil_diagnostic,
    joint_translation_offset_stencil,
    load_dense_frequency_diagnostic_plan,
    load_evidence,
    load_joint_translation_sensitivity_diagnostic_plan,
    load_receiver_position_sensitivity_diagnostic_plan,
    load_source_position_sensitivity_diagnostic_plan,
    load_spatial_representation_diagnostic_plan,
    load_stencil_sensitivity_diagnostic_plan,
    load_target_window_diagnostic_plan,
    load_time_gate_localization_diagnostic_plan,
    load_validation_plan,
    load_voxel_staircase_sensitivity_diagnostic_plan,
    native_window_left_rectangle_spectrum,
    native_window_left_rectangle_transfer,
    normalized_complex_difference,
    plane_distance_metrics,
    receiver_position_offset_stencil,
    save_evidence,
    source_position_offset_stencil,
    stencil_variant_weights,
    target_window_sampling_metadata,
    target_window_clipped_left_rectangle_spectrum,
    target_window_clipped_left_rectangle_transfer,
    validate_dense_frequency_diagnostic_binding,
    validate_exact_binding,
    validate_joint_translation_sensitivity_diagnostic_binding,
    validate_physical_observable_contract,
    validate_receiver_position_sensitivity_diagnostic_binding,
    validate_refinement_schedule,
    validate_source_position_sensitivity_diagnostic_binding,
    validate_spatial_representation_diagnostic_binding,
    validate_stencil_sensitivity_diagnostic_binding,
    validate_time_gate_localization_diagnostic_binding,
    validate_voxel_staircase_sensitivity_diagnostic_binding,
    validation_decision,
    validation_decision_v2,
    voxel_staircase_boundary_variant,
)


PLAN = Path(__file__).parents[2] / 'benchmarks' / 'acoustics' / 'r130d_general3d_validation_plan.json'
DIAGNOSTIC_PLAN = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_target_window_diagnostic_plan.json'
)

SPATIAL_DIAGNOSTIC_PLAN = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_spatial_representation_diagnostic_plan.json'
)
DENSE_DIAGNOSTIC_PLAN = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_dense_frequency_diagnostic_plan.json'
)
STENCIL_DIAGNOSTIC_PLAN = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_stencil_sensitivity_diagnostic_plan.json'
)
VOXEL_DIAGNOSTIC_PLAN = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_voxel_staircase_sensitivity_diagnostic_plan.json'
)
TIME_GATE_DIAGNOSTIC_PLAN = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_time_gate_localization_diagnostic_plan.json'
)
RECEIVER_DIAGNOSTIC_PLAN = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_receiver_position_sensitivity_diagnostic_plan.json'
)
SOURCE_DIAGNOSTIC_PLAN = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_source_position_sensitivity_diagnostic_plan.json'
)
JOINT_DIAGNOSTIC_PLAN = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_joint_translation_sensitivity_diagnostic_plan.json'
)
RUN76_EVIDENCE = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_spatial_representation_diagnostic_run76_evidence.json'
)
PR295_SUMMARY = (
    Path(__file__).parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r130d_target_window_diagnostic_run62_summary.json'
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
        'htdt.r130d.target_window_clipped_left_rectangle'
    )
    assert diagnostic['decision_semantics']['diagnostic_only'] is True
    assert (
        diagnostic['decision_semantics']['canonical_observable_replaced']
        is False
    )


def test_target_window_clipped_left_rectangle_known_complex_harmonic_converges_for_noninteger_t_over_dt():
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

        aligned_pressure = target_window_clipped_left_rectangle_spectrum(
            pressure,
            dt_s=dt_s,
            target_duration_s=target_duration_s,
            frequency_hz=frequencies,
        )
        aligned = target_window_clipped_left_rectangle_transfer(
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


def test_native_window_generalization_matches_existing_real_extractor():
    dt_s = 0.0037
    frequencies = np.asarray([40.0, 80.0], dtype=np.float64)
    times = np.arange(71, dtype=np.float64) * dt_s
    pressure = 1.3 * np.cos(2.0 * np.pi * 31.0 * times + 0.2)
    source = np.zeros(times.size, dtype=np.float64)
    source[0] = 1.0
    existing = finite_record_pressure_transfer(
        pressure,
        source,
        time_step_s=dt_s,
        frequency_hz=frequencies,
    )
    generalized = native_window_left_rectangle_transfer(
        pressure,
        source,
        dt_s=dt_s,
        frequency_hz=frequencies,
    )
    assert np.allclose(existing, generalized, rtol=0.0, atol=1.0e-13)


def test_native_window_complex_harmonic_matches_independent_geometric_series():
    dt_s = 0.007
    target_duration_s = 0.25
    sample_count = int(np.ceil(target_duration_s / dt_s))
    frequencies = np.asarray([40.0, 80.0], dtype=np.float64)
    amplitude = 2.1 + 0.4j
    harmonic_hz = 53.0
    times = np.arange(sample_count, dtype=np.float64) * dt_s
    samples = amplitude * np.exp(-2j * np.pi * harmonic_hz * times)
    numerical = native_window_left_rectangle_spectrum(
        samples,
        dt_s=dt_s,
        frequency_hz=frequencies,
    )
    analytic = analytic_sampled_complex_harmonic_left_rectangle_spectrum(
        amplitude=amplitude,
        harmonic_frequency_hz=harmonic_hz,
        analysis_frequency_hz=frequencies,
        dt_s=dt_s,
        sample_count=sample_count,
    )
    assert np.allclose(numerical, analytic, rtol=2.0e-13, atol=2.0e-13)


def test_spatial_diagnostic_plan_hash_binding_and_canonical_contract_are_frozen():
    plan = _plan()
    diagnostic = load_spatial_representation_diagnostic_plan(
        SPATIAL_DIAGNOSTIC_PLAN
    )
    validate_spatial_representation_diagnostic_binding(plan, diagnostic)
    assert diagnostic['task_start_main_sha'] == (
        '9e6066259ec58c093e9a7587550ccf907f28402f'
    )
    assert diagnostic['frozen_solver_contract']['pffdtd_ppw'] == [
        8.0, 10.0, 12.0
    ]
    assert diagnostic['frozen_solver_contract']['canonical_frequency_hz'] == [
        40.0, 80.0
    ]
    assert diagnostic['frequency_neighborhood']['diagnostic_frequency_hz'] == [
        39.0, 40.0, 41.0, 79.0, 80.0, 81.0
    ]
    assert diagnostic['frequency_neighborhood']['canonical_acceptance_inclusion'] is False
    assert diagnostic['frozen_solver_contract']['magnitude_mask_relative_db'] == -50.0
    assert diagnostic['frozen_solver_contract']['pffdtd_self_convergence_thresholds'] == {
        'complex_rms_relative_max': 0.2,
        'magnitude_max_relative': 0.25,
        'phase_max_deg': 15.0,
    }


def test_spatial_diagnostic_binding_rejects_stale_geometry_identity():
    plan = _plan()
    diagnostic = load_spatial_representation_diagnostic_plan(
        SPATIAL_DIAGNOSTIC_PLAN
    )
    stale = json.loads(json.dumps(diagnostic))
    stale['frozen_solver_contract']['fixture_id'] = 'stale-fixture'
    with pytest.raises(ValueError, match='fixture id'):
        validate_spatial_representation_diagnostic_binding(plan, stale)


def test_node_adjacency_air_domain_synthetic_split_is_counted_from_source_component():
    dims = (3, 3, 3)
    boundary = []
    adjacency = []
    for ix in (1, 2):
        for iy in range(3):
            for iz in range(3):
                boundary.append(ix * 9 + iy * 3 + iz)
                row = [True] * 6
                if ix == 1:
                    row[0] = False
                else:
                    row[1] = False
                adjacency.append(row)
    metrics = connected_air_domain_node_metrics(
        dimensions=dims,
        boundary_linear_indices=boundary,
        boundary_adjacency=adjacency,
        source_linear_indices=[0],
        neighbor_directions=[
            [1, 0, 0], [-1, 0, 0], [0, 1, 0],
            [0, -1, 0], [0, 0, 1], [0, 0, -1],
        ],
    )
    assert metrics['active_node_count'] == 27
    assert metrics['reachable_air_node_count'] == 18
    assert metrics['boundary_node_count'] == 9
    assert metrics['interior_node_count'] == 9
    assert metrics['exterior_or_disconnected_node_count'] == 9


def test_exact_plane_distance_metric_preserves_physical_and_over_h_units():
    metrics = plane_distance_metrics(
        [[0.0, 0.0, 0.1], [1.0, 0.0, -0.1], [0.0, 1.0, 0.2]],
        plane_point_m=[0.0, 0.0, 0.0],
        plane_unit_normal=[0.0, 0.0, 1.0],
        grid_spacing_m=0.5,
    )
    assert metrics['sloped_signed_normal_distance_m'] == pytest.approx(
        [0.1, -0.1, 0.2]
    )
    assert metrics['sloped_rms_abs_normal_distance_m'] == pytest.approx(
        np.sqrt(0.02)
    )
    assert metrics['sloped_max_abs_normal_distance_m'] == pytest.approx(0.2)
    assert metrics['sloped_rms_abs_normal_distance_over_h'] == pytest.approx(
        2.0 * np.sqrt(0.02)
    )
    assert metrics['sloped_max_abs_normal_distance_over_h'] == pytest.approx(0.4)


def test_trilinear_stencil_weight_sum_and_coordinate_reconstruction():
    target = np.asarray([0.25, 0.5, 0.75], dtype=np.float64)
    linear_indices = [7, 3, 5, 6, 1, 2, 4, 0]
    positions = [
        (1, 1, 1), (0, 1, 1), (1, 0, 1), (1, 1, 0),
        (0, 0, 1), (0, 1, 0), (1, 0, 0), (0, 0, 0),
    ]
    weights = [
        float(np.prod([
            target[axis] if node[axis] else 1.0 - target[axis]
            for axis in range(3)
        ]))
        for node in positions
    ]
    diagnostic = interpolation_stencil_diagnostic(
        xv=[0.0, 1.0],
        yv=[0.0, 1.0],
        zv=[0.0, 1.0],
        linear_indices=linear_indices,
        weights=weights,
        exact_position_m=target,
        grid_spacing_m=1.0,
    )
    assert diagnostic['weight_sum'] == pytest.approx(1.0)
    assert diagnostic['fractional_cell_coordinate'] == pytest.approx(target)
    assert diagnostic['reconstructed_coordinate_m'] == pytest.approx(target)
    assert diagnostic['reconstruction_error_m'] < 1.0e-15
    assert len(diagnostic['stencil_sha256']) == 64


def test_normalized_complex_difference_uses_frozen_symmetric_floor_formula():
    assert normalized_complex_difference(
        1.0 + 0.0j, 2.0 + 0.0j, fixed_floor=1.0e-12
    ) == pytest.approx(0.5)
    assert normalized_complex_difference(
        0.0j, 0.0j, fixed_floor=1.0e-12
    ) == 0.0


@pytest.mark.parametrize(
    ('worsening_count', 'expected'),
    [
        (0, 'NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS'),
        (2, 'NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS'),
        (3, 'MIXED_NEIGHBORHOOD_SENSITIVITY'),
        (4, 'NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD'),
        (6, 'NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD'),
    ],
)
def test_frequency_neighborhood_classifier_frozen_boundaries(
    worsening_count, expected
):
    d_8_10 = [1.0] * 6
    d_10_12 = [2.0 if index < worsening_count else 0.5 for index in range(6)]
    result = classify_frequency_neighborhood(d_8_10, d_10_12)
    assert result['worsening_count'] == worsening_count
    assert result['classification'] == expected


def test_spatial_classifier_uses_physical_volume_and_plane_rms_ten_to_twelve():
    monotonic = [
        {'points_per_wavelength': 8.0, 'relative_volume_error': 0.2,
         'sloped_rms_abs_normal_distance_m': 0.2},
        {'points_per_wavelength': 10.0, 'relative_volume_error': -0.1,
         'sloped_rms_abs_normal_distance_m': 0.1},
        {'points_per_wavelength': 12.0, 'relative_volume_error': 0.1,
         'sloped_rms_abs_normal_distance_m': 0.1},
    ]
    assert classify_spatial_representation_trend(monotonic)['classification'] == (
        'SPATIAL_REPRESENTATION_MONOTONIC'
    )
    volume_worse = json.loads(json.dumps(monotonic))
    volume_worse[2]['relative_volume_error'] = 0.100001
    assert classify_spatial_representation_trend(volume_worse)['classification'] == (
        'SPATIAL_REPRESENTATION_NON_MONOTONIC'
    )
    plane_worse = json.loads(json.dumps(monotonic))
    plane_worse[2]['sloped_rms_abs_normal_distance_m'] = 0.100001
    assert classify_spatial_representation_trend(plane_worse)['classification'] == (
        'SPATIAL_REPRESENTATION_NON_MONOTONIC'
    )


def test_pr295_canonical_baseline_binds_all_six_levels_and_failed_states():
    summary = json.loads(PR295_SUMMARY.read_text(encoding='utf-8'))
    assert [item['refinement'] for item in summary['outputs']['mfem']] == [1, 2, 3]
    assert [
        item['points_per_wavelength'] for item in summary['outputs']['pffdtd']
    ] == [8, 10, 12]
    assert all(len(item['canonical']) == 2 for item in summary['outputs']['pffdtd'])
    assert summary['canonical_pr286_reproduction']['max_abs_complex_component_error_all_six_levels'] == 0
    assert summary['decision']['canonical_reference_self_convergence'] == (
        'SELF_CONVERGENCE_FAILED'
    )
    assert summary['decision']['canonical_pffdtd_self_convergence'] == (
        'SELF_CONVERGENCE_FAILED'
    )
    assert summary['decision']['cross_solver_eligibility'] == 'CROSS_SOLVER_BLOCKED'
    assert summary['decision']['general_3d_validation_state'] == 'NOT_VALIDATED'


def test_diagnostic_frequency_set_cannot_enter_canonical_acceptance():
    plan = _plan()
    diagnostic = load_spatial_representation_diagnostic_plan(
        SPATIAL_DIAGNOSTIC_PLAN
    )
    assert tuple(plan.physical_quantity.frequency_hz) == (40.0, 80.0)
    assert tuple(
        diagnostic['frequency_neighborhood']['canonical_scored_frequency_hz']
    ) == (40.0, 80.0)
    assert set(
        diagnostic['frequency_neighborhood']['diagnostic_only_frequency_hz']
    ).isdisjoint(plan.physical_quantity.frequency_hz)
    assert diagnostic['frequency_neighborhood']['canonical_acceptance_inclusion'] is False


def test_dense_diagnostic_plan_hash_binding_and_canonical_contract_are_frozen():
    plan = _plan()
    diagnostic = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    validate_dense_frequency_diagnostic_binding(plan, diagnostic)
    assert diagnostic['task_start_main_sha'] == (
        'b6577df1109ad786040d8ae1d3719d2b5cd3456d'
    )
    assert diagnostic['frozen_solver_contract']['pffdtd_ppw'] == [8.0, 10.0, 12.0]
    assert diagnostic['frozen_solver_contract']['canonical_frequency_hz'] == [
        40.0, 80.0
    ]
    dense = diagnostic['dense_frequency_neighborhood']
    grid = dense_frequency_grid(dense['bands'])
    assert tuple(dense['diagnostic_frequency_hz']) == grid
    assert len(grid) == 34
    assert grid[0] == 36.0 and grid[16] == 44.0
    assert grid[17] == 76.0 and grid[33] == 84.0
    assert grid[8] == 40.0 and grid[25] == 80.0
    assert dense['canonical_scored_frequency_hz'] == [40.0, 80.0]
    assert dense['fixed_floor'] == 1.0e-12
    assert dense['pairs'] == ['8_to_10', '10_to_12']
    assert dense['classification']['localized_max_count'] == 11
    assert dense['classification']['persists_min_count'] == 23
    assert dense['canonical_acceptance_inclusion'] is False
    binding = diagnostic['run76_record_binding']
    assert [item['points_per_wavelength'] for item in binding['levels']] == [
        8.0, 10.0, 12.0
    ]
    assert binding['identical_label'] == 'RUN76_TRACE_IDENTICAL'


def test_dense_run76_pins_match_committed_run76_evidence():
    diagnostic = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    evidence = json.loads(RUN76_EVIDENCE.read_text(encoding='utf-8'))
    provenance = {
        float(item['ppw']): item for item in evidence['pffdtd_provenance']
    }
    for level in diagnostic['run76_record_binding']['levels']:
        record = provenance[float(level['points_per_wavelength'])]
        assert level['pressure_trace_sha256'] == record['pressure_trace_sha256']
        assert level['source_trace_sha256'] == record['source_trace_sha256']


def test_dense_diagnostic_binding_rejects_mutations():
    plan = _plan()
    diagnostic = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    stale = json.loads(json.dumps(diagnostic))
    stale['frozen_solver_contract']['fixture_id'] = 'stale-fixture'
    with pytest.raises(ValueError, match='fixture id'):
        validate_dense_frequency_diagnostic_binding(plan, stale)
    promoted = json.loads(json.dumps(diagnostic))
    promoted['dense_frequency_neighborhood'][
        'canonical_acceptance_inclusion'
    ] = True
    with pytest.raises(ValueError, match='canonical acceptance'):
        validate_dense_frequency_diagnostic_binding(plan, promoted)
    drift = json.loads(json.dumps(diagnostic))
    drift['dense_frequency_neighborhood']['diagnostic_frequency_hz'][8] = 41.0
    with pytest.raises(ValueError, match='band grid'):
        validate_dense_frequency_diagnostic_binding(plan, drift)
    rewritten = json.loads(json.dumps(diagnostic))
    rewritten['forbidden_changes']['post_result_frequency_selection'] = True
    with pytest.raises(ValueError, match='forbidden-change'):
        validate_dense_frequency_diagnostic_binding(plan, rewritten)
    stale_parent = json.loads(json.dumps(diagnostic))
    stale_parent['parent_spatial_representation_diagnostic'][
        'semantic_sha256'
    ] = '0' * 64
    with pytest.raises(ValueError, match='spatial diagnostic sha256'):
        validate_dense_frequency_diagnostic_binding(plan, stale_parent)


def test_dense_frequency_grid_expands_inclusive_bands():
    grid = dense_frequency_grid([
        {
            'band_id': 'a',
            'canonical_center_hz': 40.0,
            'start_hz': 36.0,
            'stop_hz': 44.0,
            'step_hz': 0.5,
        },
        {
            'band_id': 'b',
            'canonical_center_hz': 80.0,
            'start_hz': 76.0,
            'stop_hz': 84.0,
            'step_hz': 0.5,
        },
    ])
    assert len(grid) == 34
    with pytest.raises(ValueError, match='evenly'):
        dense_frequency_grid([{
            'band_id': 'x',
            'canonical_center_hz': 40.0,
            'start_hz': 36.0,
            'stop_hz': 44.1,
            'step_hz': 0.5,
        }])
    with pytest.raises(ValueError, match='canonical center'):
        dense_frequency_grid([{
            'band_id': 'x',
            'canonical_center_hz': 40.25,
            'start_hz': 36.0,
            'stop_hz': 44.0,
            'step_hz': 0.5,
        }])
    with pytest.raises(ValueError, match='repeat'):
        dense_frequency_grid([
            {
                'band_id': 'x',
                'canonical_center_hz': 40.0,
                'start_hz': 36.0,
                'stop_hz': 44.0,
                'step_hz': 4.0,
            },
            {
                'band_id': 'y',
                'canonical_center_hz': 40.0,
                'start_hz': 40.0,
                'stop_hz': 48.0,
                'step_hz': 4.0,
            },
        ])


@pytest.mark.parametrize(
    ('worsening_count', 'expected'),
    [
        (0, 'DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS'),
        (11, 'DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS'),
        (12, 'DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY'),
        (22, 'DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY'),
        (23, 'DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD'),
        (34, 'DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD'),
    ],
)
def test_dense_frequency_classifier_frozen_boundaries(
    worsening_count, expected
):
    d_8_10 = [1.0] * 34
    d_10_12 = [2.0 if index < worsening_count else 0.5 for index in range(34)]
    result = classify_dense_frequency_neighborhood(d_8_10, d_10_12)
    assert result['worsening_count'] == worsening_count
    assert result['classification'] == expected
    assert result['worsening_by_frequency'][:worsening_count] == [True] * (
        worsening_count
    )


def test_dense_diagnostic_sweep_output_is_deterministic_and_hash_bound():
    diagnostic = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    dense = diagnostic['dense_frequency_neighborhood']
    grid = dense_frequency_grid(dense['bands'])
    floor = float(dense['fixed_floor'])

    def sweep():
        level_by_ppw = {
            8.0: [complex(1.0 + index, -0.5 * index) for index, _ in enumerate(grid)],
            10.0: [complex(1.1 + index, -0.5 * index) for index, _ in enumerate(grid)],
            12.0: [complex(1.2 + index, -0.5 * index) for index, _ in enumerate(grid)],
        }
        d_8_10 = [
            normalized_complex_difference(
                level_by_ppw[8.0][i], level_by_ppw[10.0][i], fixed_floor=floor
            )
            for i in range(len(grid))
        ]
        d_10_12 = [
            normalized_complex_difference(
                level_by_ppw[10.0][i], level_by_ppw[12.0][i], fixed_floor=floor
            )
            for i in range(len(grid))
        ]
        classification = classify_dense_frequency_neighborhood(d_8_10, d_10_12)
        return {
            'frequency_hz': list(grid),
            'd_8_10': d_8_10,
            'd_10_12': d_10_12,
            **classification,
        }

    first = sweep()
    second = sweep()
    assert first == second
    assert canonical_sha256(first) == canonical_sha256(second)


def test_dense_diagnostic_cannot_enter_canonical_acceptance():
    plan = _plan()
    diagnostic = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    dense = diagnostic['dense_frequency_neighborhood']
    assert tuple(plan.physical_quantity.frequency_hz) == (40.0, 80.0)
    assert plan.plan_sha256() == (
        '5c753073a88d6705ee2962aa387006d4c563f90f0249b722dcf309a8155f62ec'
    )
    assert tuple(dense['canonical_scored_frequency_hz']) == (40.0, 80.0)
    assert set(
        dense['diagnostic_only_frequency_hz']
    ).isdisjoint(plan.physical_quantity.frequency_hz)
    assert dense['canonical_acceptance_inclusion'] is False


def test_stencil_diagnostic_plan_hash_binding_and_canonical_contract_are_frozen():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_stencil_sensitivity_diagnostic_plan(STENCIL_DIAGNOSTIC_PLAN)
    validate_stencil_sensitivity_diagnostic_binding(plan, diagnostic, dense)
    assert diagnostic['task_start_main_sha'] == (
        '6ee4f4a71954df10aa6ab02096662c9db695025b'
    )
    assert diagnostic['frozen_solver_contract']['pffdtd_ppw'] == [8.0, 10.0, 12.0]
    stencil = diagnostic['stencil_sensitivity']
    assert [
        item['variant_id'] for item in stencil['stencil_variants']
    ] == list(STENCIL_SENSITIVITY_VARIANT_IDS)
    matrix = stencil['cell_matrix']
    assert matrix['source_variants'] == list(STENCIL_SENSITIVITY_VARIANT_IDS)
    assert matrix['receiver_variants'] == list(STENCIL_SENSITIVITY_VARIANT_IDS)
    assert matrix['canonical_cell_id'] == STENCIL_SENSITIVITY_CANONICAL_CELL_ID
    evaluation = stencil['evaluation']
    dense_block = dense['dense_frequency_neighborhood']
    assert evaluation['normalized_complex_difference_formula'] == dense_block[
        'normalized_complex_difference_formula'
    ]
    assert evaluation['fixed_floor'] == dense_block['fixed_floor']
    assert evaluation['pairs'] == dense_block['pairs']
    assert evaluation['per_cell_classification'][
        'localized_max_count'
    ] == dense_block['classification']['localized_max_count']
    assert evaluation['per_cell_classification'][
        'persists_min_count'
    ] == dense_block['classification']['persists_min_count']
    assert evaluation['canonical_acceptance_inclusion'] is False


def test_stencil_run76_pins_match_dense_authority():
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_stencil_sensitivity_diagnostic_plan(STENCIL_DIAGNOSTIC_PLAN)
    dense_levels = {
        float(item['points_per_wavelength']): item
        for item in dense['run76_record_binding']['levels']
    }
    for level in diagnostic['run76_record_binding']['levels']:
        dense_level = dense_levels[float(level['points_per_wavelength'])]
        assert level['pressure_trace_sha256'] == dense_level[
            'pressure_trace_sha256'
        ]
        assert level['source_trace_sha256'] == dense_level['source_trace_sha256']


def test_stencil_diagnostic_binding_rejects_mutations():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_stencil_sensitivity_diagnostic_plan(STENCIL_DIAGNOSTIC_PLAN)
    stale = json.loads(json.dumps(diagnostic))
    stale['frozen_solver_contract']['fixture_id'] = 'stale-fixture'
    with pytest.raises(ValueError, match='fixture id'):
        validate_stencil_sensitivity_diagnostic_binding(plan, stale, dense)
    promoted = json.loads(json.dumps(diagnostic))
    promoted['stencil_sensitivity']['evaluation'][
        'canonical_acceptance_inclusion'
    ] = True
    with pytest.raises(ValueError, match='canonical acceptance'):
        validate_stencil_sensitivity_diagnostic_binding(plan, promoted, dense)
    drift = json.loads(json.dumps(diagnostic))
    drift['stencil_sensitivity']['stencil_variants'][1]['variant_id'] = 'bilinear'
    with pytest.raises(ValueError, match='variants are not the frozen set'):
        validate_stencil_sensitivity_diagnostic_binding(plan, drift, dense)
    drift_cell = json.loads(json.dumps(diagnostic))
    drift_cell['stencil_sensitivity']['cell_matrix'][
        'canonical_cell_id'
    ] = 'nearest_node|canonical_trilinear'
    with pytest.raises(ValueError, match='canonical cell'):
        validate_stencil_sensitivity_diagnostic_binding(plan, drift_cell, dense)
    drift_metric = json.loads(json.dumps(diagnostic))
    drift_metric['stencil_sensitivity']['evaluation']['fixed_floor'] = 1.0e-9
    with pytest.raises(ValueError, match='fixed floor'):
        validate_stencil_sensitivity_diagnostic_binding(plan, drift_metric, dense)
    rewritten = json.loads(json.dumps(diagnostic))
    rewritten['forbidden_changes']['source_receiver_movement'] = True
    with pytest.raises(ValueError, match='forbidden-change'):
        validate_stencil_sensitivity_diagnostic_binding(plan, rewritten, dense)
    stale_parent = json.loads(json.dumps(diagnostic))
    stale_parent['parent_dense_frequency_diagnostic']['semantic_sha256'] = (
        '0' * 64
    )
    with pytest.raises(ValueError, match='dense diagnostic sha256'):
        validate_stencil_sensitivity_diagnostic_binding(plan, stale_parent, dense)
    drift_pin = json.loads(json.dumps(diagnostic))
    drift_pin['run76_record_binding']['levels'][0]['pressure_trace_sha256'] = (
        '0' * 64
    )
    with pytest.raises(ValueError, match='dense authority'):
        validate_stencil_sensitivity_diagnostic_binding(plan, drift_pin, dense)


def test_stencil_variant_weights_reproduce_canonical_and_normalize():
    canonical = [0.3, 0.1, 0.2, 0.4, 0.05, 0.25, 0.15, -0.45]
    canonical = [x / sum(canonical) for x in canonical]
    positions = [
        [1.0, 2.0, 2.0],
        [1.5, 2.0, 2.0],
        [1.0, 2.5, 2.0],
        [1.5, 2.5, 2.0],
        [1.0, 2.0, 2.5],
        [1.5, 2.0, 2.5],
        [1.0, 2.5, 2.5],
        [1.5, 2.5, 2.5],
    ]
    target = [1.4, 2.05, 2.05]
    copied = stencil_variant_weights(
        variant_id='canonical_trilinear',
        canonical_weights=canonical,
        node_positions_m=positions,
        exact_position_m=target,
    )
    assert np.array_equal(copied, np.asarray(canonical))
    copied[0] = 99.0
    assert canonical[0] != 99.0
    nearest = stencil_variant_weights(
        variant_id='nearest_node',
        canonical_weights=canonical,
        node_positions_m=positions,
        exact_position_m=target,
    )
    assert int(np.argmax(nearest)) == 1
    assert nearest.sum() == pytest.approx(1.0)
    uniform = stencil_variant_weights(
        variant_id='uniform_eight_node',
        canonical_weights=canonical,
        node_positions_m=positions,
        exact_position_m=target,
    )
    assert np.allclose(uniform, 0.125)
    with pytest.raises(ValueError, match='unknown stencil weight variant'):
        stencil_variant_weights(
            variant_id='cubic',
            canonical_weights=canonical,
            node_positions_m=positions,
            exact_position_m=target,
        )
    with pytest.raises(ValueError, match='node positions'):
        stencil_variant_weights(
            variant_id='nearest_node',
            canonical_weights=canonical,
            node_positions_m=positions[:-1],
            exact_position_m=target,
        )


def test_stencil_variant_nearest_node_tie_breaks_to_lowest_row():
    positions = [[float(index), 0.0, 0.0] for index in range(8)]
    weights = stencil_variant_weights(
        variant_id='nearest_node',
        canonical_weights=[0.125] * 8,
        node_positions_m=positions,
        exact_position_m=[2.5, 0.0, 0.0],
    )
    assert int(np.argmax(weights)) == 2
    assert weights.sum() == pytest.approx(1.0)


@pytest.mark.parametrize(
    ('cell_vectors', 'cell_labels', 'expected'),
    [
        (
            [[True, False, True, False]] * 8,
            ['DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY'] * 8,
            'STENCIL_WORSENING_PATTERN_INVARIANT',
        ),
        (
            [[True, False, True, False]] * 7 + [[False, False, True, False]],
            ['DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY'] * 8,
            'STENCIL_WORSENING_PATTERN_SHIFTED',
        ),
        (
            [[True, False, True, False]] * 7 + [[False, False, True, False]],
            ['DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY'] * 7
            + ['DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS'],
            'STENCIL_WORSENING_PATTERN_RECLASSIFIED',
        ),
    ],
)
def test_stencil_classifier_frozen_labels(cell_vectors, cell_labels, expected):
    canonical_vector = [True, False, True, False]
    cells = [
        {
            'cell_id': f'cell{index}',
            'worsening_by_frequency': vector,
            'classification': cell_labels[index],
        }
        for index, vector in enumerate(cell_vectors)
    ]
    result = classify_stencil_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification='DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY',
        noncanonical_cells=cells,
    )
    assert result['classification'] == expected
    assert result['canonical_cell_id'] == STENCIL_SENSITIVITY_CANONICAL_CELL_ID
    if expected == 'STENCIL_WORSENING_PATTERN_INVARIANT':
        assert result['identical_vector_cell_count'] == 8
        assert all(
            distance == 0
            for distance in result['hamming_distance_by_cell'].values()
        )
    if expected == 'STENCIL_WORSENING_PATTERN_RECLASSIFIED':
        assert result['reclassified_cell_ids'] == ['cell7']


def test_stencil_classifier_is_fail_closed_on_inputs():
    canonical_vector = [True, False, True, False]
    with pytest.raises(ValueError, match='control'):
        classify_stencil_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification='X',
            noncanonical_cells=[
                {
                    'cell_id': STENCIL_SENSITIVITY_CANONICAL_CELL_ID,
                    'worsening_by_frequency': canonical_vector,
                    'classification': 'X',
                }
            ],
        )
    with pytest.raises(ValueError, match='length mismatch'):
        classify_stencil_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification='X',
            noncanonical_cells=[
                {
                    'cell_id': 'short',
                    'worsening_by_frequency': [True],
                    'classification': 'X',
                }
            ],
        )


def test_stencil_diagnostic_cannot_enter_canonical_acceptance():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_stencil_sensitivity_diagnostic_plan(STENCIL_DIAGNOSTIC_PLAN)
    validate_stencil_sensitivity_diagnostic_binding(plan, diagnostic, dense)
    assert tuple(plan.physical_quantity.frequency_hz) == (40.0, 80.0)
    assert diagnostic['stencil_sensitivity']['evaluation'][
        'canonical_acceptance_inclusion'
    ] is False
    assert diagnostic['decision_semantics']['diagnostic_only'] is True
    assert diagnostic['decision_semantics'][
        'cross_solver_unblocked_by_diagnostic'
    ] is False


def test_voxel_diagnostic_plan_hash_binding_and_canonical_contract_are_frozen():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_voxel_staircase_sensitivity_diagnostic_plan(
        VOXEL_DIAGNOSTIC_PLAN
    )
    validate_voxel_staircase_sensitivity_diagnostic_binding(
        plan, diagnostic, dense
    )
    assert diagnostic['task_start_main_sha'] == (
        '1131897041951af001ce16cf33220db923afc3b3'
    )
    assert diagnostic['frozen_solver_contract']['pffdtd_ppw'] == [8.0, 10.0, 12.0]
    voxel = diagnostic['voxel_staircase_sensitivity']
    assert [
        item['variant_id'] for item in voxel['boundary_variants']
    ] == list(VOXEL_STAIRCASE_SENSITIVITY_VARIANT_IDS)
    cell_axis = voxel['cell_axis']
    assert cell_axis['cells'] == list(VOXEL_STAIRCASE_SENSITIVITY_VARIANT_IDS)
    assert cell_axis['canonical_cell_id'] == (
        VOXEL_STAIRCASE_SENSITIVITY_CANONICAL_CELL_ID
    )
    evaluation = voxel['evaluation']
    dense_block = dense['dense_frequency_neighborhood']
    assert evaluation['normalized_complex_difference_formula'] == dense_block[
        'normalized_complex_difference_formula'
    ]
    assert evaluation['fixed_floor'] == dense_block['fixed_floor']
    assert evaluation['pairs'] == dense_block['pairs']
    assert evaluation['per_cell_classification'][
        'localized_max_count'
    ] == dense_block['classification']['localized_max_count']
    assert evaluation['per_cell_classification'][
        'persists_min_count'
    ] == dense_block['classification']['persists_min_count']
    assert evaluation['canonical_acceptance_inclusion'] is False


def test_voxel_run76_pins_match_dense_authority():
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_voxel_staircase_sensitivity_diagnostic_plan(
        VOXEL_DIAGNOSTIC_PLAN
    )
    dense_levels = {
        float(item['points_per_wavelength']): item
        for item in dense['run76_record_binding']['levels']
    }
    for level in diagnostic['run76_record_binding']['levels']:
        dense_level = dense_levels[float(level['points_per_wavelength'])]
        assert level['pressure_trace_sha256'] == dense_level[
            'pressure_trace_sha256'
        ]
        assert level['source_trace_sha256'] == dense_level['source_trace_sha256']


def test_voxel_diagnostic_binding_rejects_mutations():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_voxel_staircase_sensitivity_diagnostic_plan(
        VOXEL_DIAGNOSTIC_PLAN
    )
    stale = json.loads(json.dumps(diagnostic))
    stale['frozen_solver_contract']['fixture_id'] = 'stale-fixture'
    with pytest.raises(ValueError, match='fixture id'):
        validate_voxel_staircase_sensitivity_diagnostic_binding(
            plan, stale, dense
        )
    promoted = json.loads(json.dumps(diagnostic))
    promoted['voxel_staircase_sensitivity']['evaluation'][
        'canonical_acceptance_inclusion'
    ] = True
    with pytest.raises(ValueError, match='canonical acceptance'):
        validate_voxel_staircase_sensitivity_diagnostic_binding(
            plan, promoted, dense
        )
    drift = json.loads(json.dumps(diagnostic))
    drift['voxel_staircase_sensitivity']['boundary_variants'][1][
        'variant_id'
    ] = 'refined_twice'
    with pytest.raises(ValueError, match='variants are not the frozen set'):
        validate_voxel_staircase_sensitivity_diagnostic_binding(
            plan, drift, dense
        )
    drift_cell = json.loads(json.dumps(diagnostic))
    drift_cell['voxel_staircase_sensitivity']['cell_axis'][
        'canonical_cell_id'
    ] = 'open_boundary_as_air'
    with pytest.raises(ValueError, match='canonical cell'):
        validate_voxel_staircase_sensitivity_diagnostic_binding(
            plan, drift_cell, dense
        )
    drift_metric = json.loads(json.dumps(diagnostic))
    drift_metric['voxel_staircase_sensitivity']['evaluation'][
        'fixed_floor'
    ] = 1.0e-9
    with pytest.raises(ValueError, match='fixed floor'):
        validate_voxel_staircase_sensitivity_diagnostic_binding(
            plan, drift_metric, dense
        )
    rewritten = json.loads(json.dumps(diagnostic))
    rewritten['forbidden_changes']['source_receiver_movement'] = True
    with pytest.raises(ValueError, match='forbidden-change'):
        validate_voxel_staircase_sensitivity_diagnostic_binding(
            plan, rewritten, dense
        )
    stale_parent = json.loads(json.dumps(diagnostic))
    stale_parent['parent_dense_frequency_diagnostic']['semantic_sha256'] = (
        '0' * 64
    )
    with pytest.raises(ValueError, match='dense diagnostic sha256'):
        validate_voxel_staircase_sensitivity_diagnostic_binding(
            plan, stale_parent, dense
        )
    stale_stencil = json.loads(json.dumps(diagnostic))
    stale_stencil['parent_stencil_sensitivity_diagnostic'][
        'semantic_sha256'
    ] = '0' * 64
    with pytest.raises(ValueError, match='stencil diagnostic sha256'):
        validate_voxel_staircase_sensitivity_diagnostic_binding(
            plan, stale_stencil, dense
        )
    drift_pin = json.loads(json.dumps(diagnostic))
    drift_pin['run76_record_binding']['levels'][0]['pressure_trace_sha256'] = (
        '0' * 64
    )
    with pytest.raises(ValueError, match='dense authority'):
        validate_voxel_staircase_sensitivity_diagnostic_binding(
            plan, drift_pin, dense
        )


def test_voxel_boundary_variant_reproduces_canonical():
    bn = np.asarray([50, 70], dtype=np.int64)
    adj = np.asarray(
        [
            [True, True, True, True, True, False],
            [False, True, True, True, True, True],
        ],
        dtype=bool,
    )
    result = voxel_staircase_boundary_variant(
        variant_id='canonical_voxelization',
        dimensions=(6, 5, 4),
        boundary_linear_indices=bn,
        boundary_adjacency=adj,
    )
    assert np.array_equal(result['bn_ixyz'], bn)
    assert np.array_equal(result['adj_bn'], adj)
    assert np.array_equal(
        result['canonical_row_indices'], np.arange(2, dtype=np.int64)
    )
    report = result['report']
    assert report['boundary_node_count'] == 2
    assert report['appended_boundary_node_count'] == 0
    assert report['dropped_boundary_node_count'] == 0
    result['bn_ixyz'][0] = 99
    assert bn[0] != 99


def test_voxel_boundary_variant_rules():
    dims = (6, 5, 4)
    bn = np.asarray([50, 70, 31], dtype=np.int64)
    adj = np.asarray(
        [
            [True, True, True, True, True, False],
            [False, True, True, True, True, True],
            [False, False, False, False, False, False],
        ],
        dtype=bool,
    )
    dropped = voxel_staircase_boundary_variant(
        variant_id='near_boundary_nodes_as_air',
        dimensions=dims,
        boundary_linear_indices=bn,
        boundary_adjacency=adj,
    )
    assert np.array_equal(dropped['bn_ixyz'], np.asarray([50, 70]))
    assert np.array_equal(
        dropped['canonical_row_indices'], np.asarray([0, 1])
    )
    assert dropped['report']['dropped_boundary_node_count'] == 1
    opened = voxel_staircase_boundary_variant(
        variant_id='open_boundary_as_air',
        dimensions=dims,
        boundary_linear_indices=bn,
        boundary_adjacency=adj,
    )
    assert np.all(opened['adj_bn'])
    assert np.array_equal(opened['bn_ixyz'], bn)
    blocked = voxel_staircase_boundary_variant(
        variant_id='fully_blocked_boundary',
        dimensions=dims,
        boundary_linear_indices=bn,
        boundary_adjacency=adj,
    )
    assert not np.any(blocked['adj_bn'])
    assert np.array_equal(blocked['bn_ixyz'], bn)
    with pytest.raises(ValueError, match='unknown voxel-staircase variant'):
        voxel_staircase_boundary_variant(
            variant_id='refined_twice',
            dimensions=dims,
            boundary_linear_indices=bn,
            boundary_adjacency=adj,
        )
    with pytest.raises(ValueError, match='Nb,6'):
        voxel_staircase_boundary_variant(
            variant_id='canonical_voxelization',
            dimensions=dims,
            boundary_linear_indices=bn,
            boundary_adjacency=adj[:, :5],
        )


def test_voxel_boundary_variant_dilation_appends_open_neighbors():
    dims = (6, 5, 4)
    bn = np.asarray([50, 70], dtype=np.int64)
    adj = np.asarray(
        [
            [True, True, True, True, True, False],
            [False, True, True, True, True, True],
        ],
        dtype=bool,
    )
    result = voxel_staircase_boundary_variant(
        variant_id='dilated_boundary_layer',
        dimensions=dims,
        boundary_linear_indices=bn,
        boundary_adjacency=adj,
    )
    # node 50 at (2,2,2): open neighbors +x=70 (already boundary), -x=30,
    # +y=54, -y=46, +z=51 (halo shell iz=3=Nz-1, excluded); -z blocked.
    # node 70 at (3,2,2): +x blocked, -x=50 (boundary), +y=74, -y=66,
    # +z=71 (halo shell, excluded), -z=69.
    appended = sorted(set(int(x) for x in result['bn_ixyz']) - {50, 70})
    assert appended == [30, 46, 54, 66, 69, 74]
    assert result['report']['appended_boundary_node_count'] == 6
    rows = {int(v): i for i, v in enumerate(result['bn_ixyz'])}
    for index in (30, 46, 54):
        assert np.array_equal(result['adj_bn'][rows[index]], adj[0])
    for index in (66, 69, 74):
        assert np.array_equal(result['adj_bn'][rows[index]], adj[1])
    assert np.array_equal(
        result['canonical_row_indices'], np.asarray([0, 1])
    )


@pytest.mark.parametrize(
    ('cell_vectors', 'cell_labels', 'expected'),
    [
        (
            [[True, False, True, False]] * 4,
            ['DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY'] * 4,
            'VOXEL_STAIRCASE_WORSENING_PATTERN_INVARIANT',
        ),
        (
            [[True, False, True, False]] * 3 + [[False, False, True, False]],
            ['DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY'] * 4,
            'VOXEL_STAIRCASE_WORSENING_PATTERN_SHIFTED',
        ),
        (
            [[True, False, True, False]] * 3 + [[False, False, True, False]],
            ['DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY'] * 3
            + ['DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS'],
            'VOXEL_STAIRCASE_WORSENING_PATTERN_RECLASSIFIED',
        ),
    ],
)
def test_voxel_classifier_frozen_labels(cell_vectors, cell_labels, expected):
    canonical_vector = [True, False, True, False]
    cells = [
        {
            'cell_id': f'cell{index}',
            'worsening_by_frequency': vector,
            'classification': cell_labels[index],
        }
        for index, vector in enumerate(cell_vectors)
    ]
    result = classify_voxel_staircase_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification='DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY',
        noncanonical_cells=cells,
    )
    assert result['classification'] == expected
    assert result['canonical_cell_id'] == (
        VOXEL_STAIRCASE_SENSITIVITY_CANONICAL_CELL_ID
    )
    if expected == 'VOXEL_STAIRCASE_WORSENING_PATTERN_INVARIANT':
        assert result['identical_vector_cell_count'] == 4
        assert all(
            distance == 0
            for distance in result['hamming_distance_by_cell'].values()
        )
    if expected == 'VOXEL_STAIRCASE_WORSENING_PATTERN_RECLASSIFIED':
        assert result['reclassified_cell_ids'] == ['cell3']


def test_voxel_classifier_is_fail_closed_on_inputs():
    canonical_vector = [True, False, True, False]
    with pytest.raises(ValueError, match='control'):
        classify_voxel_staircase_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification='X',
            noncanonical_cells=[
                {
                    'cell_id': VOXEL_STAIRCASE_SENSITIVITY_CANONICAL_CELL_ID,
                    'worsening_by_frequency': canonical_vector,
                    'classification': 'X',
                }
            ],
        )
    with pytest.raises(ValueError, match='length mismatch'):
        classify_voxel_staircase_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification='X',
            noncanonical_cells=[
                {
                    'cell_id': 'short',
                    'worsening_by_frequency': [True],
                    'classification': 'X',
                }
            ],
        )


def test_voxel_diagnostic_cannot_enter_canonical_acceptance():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_voxel_staircase_sensitivity_diagnostic_plan(
        VOXEL_DIAGNOSTIC_PLAN
    )
    validate_voxel_staircase_sensitivity_diagnostic_binding(
        plan, diagnostic, dense
    )
    assert tuple(plan.physical_quantity.frequency_hz) == (40.0, 80.0)
    assert diagnostic['voxel_staircase_sensitivity']['evaluation'][
        'canonical_acceptance_inclusion'
    ] is False
    assert diagnostic['decision_semantics']['diagnostic_only'] is True
    assert diagnostic['decision_semantics'][
        'cross_solver_unblocked_by_diagnostic'
    ] is False




def test_time_gate_diagnostic_plan_hash_binding_and_canonical_contract_are_frozen():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_time_gate_localization_diagnostic_plan(
        TIME_GATE_DIAGNOSTIC_PLAN
    )
    validate_time_gate_localization_diagnostic_binding(
        plan, diagnostic, dense
    )
    assert diagnostic['task_start_main_sha'] == (
        'ede5eff8be6b4bef552e52c78582df19287d0d3c'
    )
    assert diagnostic['frozen_solver_contract']['pffdtd_ppw'] == [
        8.0, 10.0, 12.0,
    ]
    assert diagnostic['parent_voxel_staircase_sensitivity_diagnostic'][
        'semantic_sha256'
    ] == canonical_sha256(
        load_voxel_staircase_sensitivity_diagnostic_plan(VOXEL_DIAGNOSTIC_PLAN)
    )
    time_gate = diagnostic['time_gate_localization']
    gates = time_gate['gates']
    assert [item['gate_id'] for item in gates] == list(TIME_GATE_CELL_IDS)
    assert [bool(item.get('control')) for item in gates] == [True] + [
        False
    ] * (len(TIME_GATE_CELL_IDS) - 1)
    duration = float(plan.physical_quantity.duration_s)
    assert gates[0]['interval_s'] == [0.0, duration]
    for item in gates[1:]:
        t_start, t_end = item['interval_s']
        assert 0.0 <= t_start < t_end <= duration
        if item['kind'] == 'prefix':
            assert t_start == 0.0
        if item['kind'] == 'tail':
            assert t_end == duration
    cell_axis = time_gate['cell_axis']
    assert cell_axis['cells'] == list(TIME_GATE_CELL_IDS)
    assert cell_axis['canonical_cell_id'] == TIME_GATE_CANONICAL_CELL_ID
    evaluation = time_gate['evaluation']
    dense_block = dense['dense_frequency_neighborhood']
    assert evaluation['normalized_complex_difference_formula'] == dense_block[
        'normalized_complex_difference_formula'
    ]
    assert evaluation['fixed_floor'] == dense_block['fixed_floor']
    assert evaluation['pairs'] == dense_block['pairs']
    assert evaluation['per_cell_classification'][
        'localized_max_count'
    ] == dense_block['classification']['localized_max_count']
    assert evaluation['per_cell_classification'][
        'persists_min_count'
    ] == dense_block['classification']['persists_min_count']
    assert evaluation['partition_identity_check']['partition_cells'] == list(
        TIME_GATE_PARTITION_CELL_IDS
    )
    assert evaluation['canonical_acceptance_inclusion'] is False


def test_time_gate_run76_pins_match_dense_authority():
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_time_gate_localization_diagnostic_plan(
        TIME_GATE_DIAGNOSTIC_PLAN
    )
    dense_levels = {
        float(item['points_per_wavelength']): item
        for item in dense['run76_record_binding']['levels']
    }
    for level in diagnostic['run76_record_binding']['levels']:
        dense_level = dense_levels[float(level['points_per_wavelength'])]
        assert level['pressure_trace_sha256'] == dense_level[
            'pressure_trace_sha256'
        ]
        assert level['source_trace_sha256'] == dense_level['source_trace_sha256']


def test_time_gate_diagnostic_binding_rejects_mutations():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_time_gate_localization_diagnostic_plan(
        TIME_GATE_DIAGNOSTIC_PLAN
    )
    stale = json.loads(json.dumps(diagnostic))
    stale['frozen_solver_contract']['fixture_id'] = 'stale-fixture'
    with pytest.raises(ValueError, match='fixture id'):
        validate_time_gate_localization_diagnostic_binding(plan, stale, dense)
    promoted = json.loads(json.dumps(diagnostic))
    promoted['time_gate_localization']['evaluation'][
        'canonical_acceptance_inclusion'
    ] = True
    with pytest.raises(ValueError, match='canonical acceptance'):
        validate_time_gate_localization_diagnostic_binding(
            plan, promoted, dense
        )
    drift = json.loads(json.dumps(diagnostic))
    drift['time_gate_localization']['gates'][1]['gate_id'] = 'prefix_12ms'
    with pytest.raises(ValueError, match='not the frozen set'):
        validate_time_gate_localization_diagnostic_binding(plan, drift, dense)
    drift_interval = json.loads(json.dumps(diagnostic))
    drift_interval['time_gate_localization']['gates'][1]['interval_s'] = [
        0.0,
        0.30,
    ]
    with pytest.raises(ValueError, match='outside the frozen record'):
        validate_time_gate_localization_diagnostic_binding(
            plan, drift_interval, dense
        )
    drift_partition = json.loads(json.dumps(diagnostic))
    drift_partition['time_gate_localization']['gates'][9]['interval_s'] = [
        0.050,
        0.140,
    ]
    with pytest.raises(ValueError, match='tile'):
        validate_time_gate_localization_diagnostic_binding(
            plan, drift_partition, dense
        )
    drift_cell = json.loads(json.dumps(diagnostic))
    drift_cell['time_gate_localization']['cell_axis'][
        'canonical_cell_id'
    ] = 'prefix_50ms'
    with pytest.raises(ValueError, match='canonical cell'):
        validate_time_gate_localization_diagnostic_binding(
            plan, drift_cell, dense
        )
    drift_metric = json.loads(json.dumps(diagnostic))
    drift_metric['time_gate_localization']['evaluation']['fixed_floor'] = (
        1.0e-9
    )
    with pytest.raises(ValueError, match='fixed floor'):
        validate_time_gate_localization_diagnostic_binding(
            plan, drift_metric, dense
        )
    drift_labels = json.loads(json.dumps(diagnostic))
    drift_labels['time_gate_localization']['evaluation']['classification'][
        'broadband'
    ] = 'RENAMED'
    with pytest.raises(ValueError, match='classification labels'):
        validate_time_gate_localization_diagnostic_binding(
            plan, drift_labels, dense
        )
    rewritten = json.loads(json.dumps(diagnostic))
    rewritten['forbidden_changes']['time_window_replacement'] = True
    with pytest.raises(ValueError, match='forbidden-change'):
        validate_time_gate_localization_diagnostic_binding(
            plan, rewritten, dense
        )
    stale_parent = json.loads(json.dumps(diagnostic))
    stale_parent['parent_voxel_staircase_sensitivity_diagnostic'][
        'semantic_sha256'
    ] = '0' * 64
    with pytest.raises(ValueError, match='voxel-staircase diagnostic sha256'):
        validate_time_gate_localization_diagnostic_binding(
            plan, stale_parent, dense
        )
    drift_pin = json.loads(json.dumps(diagnostic))
    drift_pin['run76_record_binding']['levels'][0]['pressure_trace_sha256'] = (
        '0' * 64
    )
    with pytest.raises(ValueError, match='dense authority'):
        validate_time_gate_localization_diagnostic_binding(
            plan, drift_pin, dense
        )


def test_apply_time_gate_masks_outside_half_open_interval():
    ts = 0.001
    record = np.arange(20, dtype=np.float64) + 1.0
    gated = apply_time_gate(record, time_step_s=ts, interval_s=[0.005, 0.012])
    expected = np.zeros(20, dtype=np.float64)
    expected[5:12] = record[5:12]
    assert np.array_equal(gated, expected)
    assert record[0] == 1.0
    prefix = apply_time_gate(record, time_step_s=ts, interval_s=[0.0, 0.004])
    assert np.array_equal(prefix[:4], record[:4])
    assert not np.any(prefix[4:])
    tail = apply_time_gate(record, time_step_s=ts, interval_s=[0.004, 0.020])
    assert not np.any(tail[:4])
    assert np.array_equal(tail[4:], record[4:])


def test_apply_time_gate_partition_decomposes_transfer_additively():
    rng = np.random.default_rng(20261003)
    ts = 0.0005
    record = rng.standard_normal(500)
    source = np.zeros(record.size)
    source[0] = 1.0
    frequencies = np.asarray([36.0, 40.5, 77.0], dtype=np.float64)
    full = finite_record_pressure_transfer(
        record, source, time_step_s=ts, frequency_hz=frequencies
    )
    partition_sum = sum(
        finite_record_pressure_transfer(
            apply_time_gate(record, time_step_s=ts, interval_s=interval),
            source,
            time_step_s=ts,
            frequency_hz=frequencies,
        )
        for interval in ([0.0, 0.05], [0.05, 0.15], [0.15, 0.25])
    )
    assert np.max(np.abs(partition_sum - full)) <= 1.0e-12


def test_apply_time_gate_is_fail_closed_on_inputs():
    record = np.ones(10, dtype=np.float64)
    with pytest.raises(ValueError, match='outside the record'):
        apply_time_gate(record, time_step_s=0.001, interval_s=[0.0, 0.02])
    with pytest.raises(ValueError, match='selects no record samples'):
        apply_time_gate(record, time_step_s=0.001, interval_s=[0.0055, 0.0059])
    with pytest.raises(ValueError, match='finite and positive'):
        apply_time_gate(record, time_step_s=0.0, interval_s=[0.0, 0.01])
    with pytest.raises(ValueError, match='must be finite'):
        apply_time_gate(
            np.full(10, np.nan), time_step_s=0.001, interval_s=[0.0, 0.01]
        )


_LOCALIZED = 'DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS'
_MIXED = 'DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY'


def _time_gate_cells(vectors, labels):
    cell_ids = [
        cell_id
        for cell_id in TIME_GATE_CELL_IDS
        if cell_id != TIME_GATE_CANONICAL_CELL_ID
    ]
    return [
        {
            'cell_id': cell_id,
            'worsening_by_frequency': vectors[index],
            'classification': labels[index],
        }
        for index, cell_id in enumerate(cell_ids)
    ]


def test_time_gate_classifier_frozen_labels():
    canonical_vector = [True, False, True, False]
    identical = [canonical_vector] * 10
    shifted = [[False, True, True, False]] * 10
    result = classify_time_gate_localization(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_time_gate_cells(identical, [_MIXED] * 10),
    )
    assert result['classification'] == 'TIME_GATE_WORSENING_PATTERN_INVARIANT'
    assert result['identical_vector_cell_count'] == 10

    # Every tail localized (count <= 11) and one prefix reproduces the
    # canonical vector -> early-carried.
    vectors = [shifted[0]] * 10
    labels = [_MIXED] * 4 + [_LOCALIZED] * 6
    vectors[0] = canonical_vector
    result = classify_time_gate_localization(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_time_gate_cells(vectors, labels),
    )
    assert result['classification'] == 'TIME_GATE_WORSENING_EARLY_CARRIED'
    assert result['prefix_exact_reproduction_cell_ids'] == ['prefix_10ms']
    assert result['all_tail_cells_localized'] is True

    # Every prefix localized and one tail reproduces the canonical vector
    # -> late-carried.
    vectors = [shifted[0]] * 10
    labels = [_LOCALIZED] * 4 + [_MIXED] * 6
    vectors[4] = canonical_vector
    result = classify_time_gate_localization(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_time_gate_cells(vectors, labels),
    )
    assert result['classification'] == 'TIME_GATE_WORSENING_LATE_CARRIED'
    assert result['tail_exact_reproduction_cell_ids'] == ['tail_10ms']
    assert result['all_prefix_cells_localized'] is True

    # At least one prefix carries and at least one tail carries -> broadband.
    vectors = [shifted[0]] * 10
    labels = [_MIXED] * 10
    result = classify_time_gate_localization(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_time_gate_cells(vectors, labels),
    )
    assert result['classification'] == 'TIME_GATE_WORSENING_BROADBAND'
    assert result['prefix_carrying_cell_ids'] == list(TIME_GATE_PREFIX_CELL_IDS)
    assert result['tail_carrying_cell_ids'] == list(TIME_GATE_TAIL_CELL_IDS)

    # Tails all localized but no prefix reproduces the canonical vector
    # -> mixed.
    vectors = [shifted[0]] * 10
    labels = [_MIXED] * 4 + [_LOCALIZED] * 6
    result = classify_time_gate_localization(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_time_gate_cells(vectors, labels),
    )
    assert result['classification'] == 'TIME_GATE_WORSENING_PATTERN_MIXED'


def test_time_gate_classifier_is_fail_closed_on_inputs():
    canonical_vector = [True, False, True, False]
    cells = _time_gate_cells([canonical_vector] * 10, [_MIXED] * 10)
    with pytest.raises(ValueError, match='control'):
        classify_time_gate_localization(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=[
                {
                    'cell_id': TIME_GATE_CANONICAL_CELL_ID,
                    'worsening_by_frequency': canonical_vector,
                    'classification': _MIXED,
                }
            ],
        )
    with pytest.raises(ValueError, match='length mismatch'):
        classify_time_gate_localization(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=[
                {**cells[0], 'worsening_by_frequency': [True]}
            ]
            + cells[1:],
        )
    with pytest.raises(ValueError, match='frozen non-control cells'):
        classify_time_gate_localization(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=cells[:-1],
        )


def test_time_gate_diagnostic_cannot_enter_canonical_acceptance():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_time_gate_localization_diagnostic_plan(
        TIME_GATE_DIAGNOSTIC_PLAN
    )
    validate_time_gate_localization_diagnostic_binding(
        plan, diagnostic, dense
    )
    assert tuple(plan.physical_quantity.frequency_hz) == (40.0, 80.0)
    assert diagnostic['time_gate_localization']['evaluation'][
        'canonical_acceptance_inclusion'
    ] is False
    assert diagnostic['decision_semantics']['diagnostic_only'] is True
    assert diagnostic['decision_semantics'][
        'cross_solver_unblocked_by_diagnostic'
    ] is False




def test_receiver_position_diagnostic_plan_hash_binding_and_canonical_contract_are_frozen():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_receiver_position_sensitivity_diagnostic_plan(
        RECEIVER_DIAGNOSTIC_PLAN
    )
    validate_receiver_position_sensitivity_diagnostic_binding(
        plan, diagnostic, dense
    )
    assert diagnostic['task_start_main_sha'] == (
        '98da71aea3958f0ed0e6729c45ad5eff0c44c92f'
    )
    assert diagnostic['frozen_solver_contract']['pffdtd_ppw'] == [
        8.0, 10.0, 12.0,
    ]
    assert diagnostic['parent_time_gate_localization_diagnostic'][
        'semantic_sha256'
    ] == canonical_sha256(
        load_time_gate_localization_diagnostic_plan(TIME_GATE_DIAGNOSTIC_PLAN)
    )
    receiver = diagnostic['receiver_position_sensitivity']
    offsets = receiver['receiver_offsets']
    assert [item['cell_id'] for item in offsets] == list(
        RECEIVER_POSITION_CELL_IDS
    )
    assert [bool(item.get('control')) for item in offsets] == [True] + [
        False
    ] * (len(RECEIVER_POSITION_CELL_IDS) - 1)
    for item in offsets:
        assert item['offset_cells'] == list(
            RECEIVER_POSITION_OFFSET_CELLS[item['cell_id']]
        )
    cell_axis = receiver['cell_axis']
    assert cell_axis['cells'] == list(RECEIVER_POSITION_CELL_IDS)
    assert cell_axis['canonical_cell_id'] == (
        RECEIVER_POSITION_CANONICAL_CELL_ID
    )
    evaluation = receiver['evaluation']
    dense_block = dense['dense_frequency_neighborhood']
    assert evaluation['normalized_complex_difference_formula'] == dense_block[
        'normalized_complex_difference_formula'
    ]
    assert evaluation['fixed_floor'] == dense_block['fixed_floor']
    assert evaluation['pairs'] == dense_block['pairs']
    assert evaluation['per_cell_classification'][
        'localized_max_count'
    ] == dense_block['classification']['localized_max_count']
    assert evaluation['per_cell_classification'][
        'persists_min_count'
    ] == dense_block['classification']['persists_min_count']
    assert evaluation['canonical_acceptance_inclusion'] is False


def test_receiver_position_run76_pins_match_dense_authority():
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_receiver_position_sensitivity_diagnostic_plan(
        RECEIVER_DIAGNOSTIC_PLAN
    )
    dense_levels = {
        float(item['points_per_wavelength']): item
        for item in dense['run76_record_binding']['levels']
    }
    for level in diagnostic['run76_record_binding']['levels']:
        dense_level = dense_levels[float(level['points_per_wavelength'])]
        assert level['pressure_trace_sha256'] == dense_level[
            'pressure_trace_sha256'
        ]
        assert level['source_trace_sha256'] == dense_level['source_trace_sha256']


def test_receiver_position_diagnostic_binding_rejects_mutations():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_receiver_position_sensitivity_diagnostic_plan(
        RECEIVER_DIAGNOSTIC_PLAN
    )
    stale = json.loads(json.dumps(diagnostic))
    stale['frozen_solver_contract']['fixture_id'] = 'stale-fixture'
    with pytest.raises(ValueError, match='fixture id'):
        validate_receiver_position_sensitivity_diagnostic_binding(
            plan, stale, dense
        )
    promoted = json.loads(json.dumps(diagnostic))
    promoted['receiver_position_sensitivity']['evaluation'][
        'canonical_acceptance_inclusion'
    ] = True
    with pytest.raises(ValueError, match='canonical acceptance'):
        validate_receiver_position_sensitivity_diagnostic_binding(
            plan, promoted, dense
        )
    drift = json.loads(json.dumps(diagnostic))
    drift['receiver_position_sensitivity']['receiver_offsets'][1][
        'cell_id'
    ] = 'x_minus_3'
    with pytest.raises(ValueError, match='not the frozen set'):
        validate_receiver_position_sensitivity_diagnostic_binding(
            plan, drift, dense
        )
    drift_offset = json.loads(json.dumps(diagnostic))
    drift_offset['receiver_position_sensitivity']['receiver_offsets'][1][
        'offset_cells'
    ] = [-3, 0, 0]
    with pytest.raises(ValueError, match='whole-cell offset'):
        validate_receiver_position_sensitivity_diagnostic_binding(
            plan, drift_offset, dense
        )
    drift_cell = json.loads(json.dumps(diagnostic))
    drift_cell['receiver_position_sensitivity']['cell_axis'][
        'canonical_cell_id'
    ] = 'x_plus_1'
    with pytest.raises(ValueError, match='canonical cell'):
        validate_receiver_position_sensitivity_diagnostic_binding(
            plan, drift_cell, dense
        )
    drift_metric = json.loads(json.dumps(diagnostic))
    drift_metric['receiver_position_sensitivity']['evaluation'][
        'fixed_floor'
    ] = 1.0e-9
    with pytest.raises(ValueError, match='fixed floor'):
        validate_receiver_position_sensitivity_diagnostic_binding(
            plan, drift_metric, dense
        )
    drift_labels = json.loads(json.dumps(diagnostic))
    drift_labels['receiver_position_sensitivity']['evaluation'][
        'classification'
    ]['room_global'] = 'RENAMED'
    with pytest.raises(ValueError, match='classification labels'):
        validate_receiver_position_sensitivity_diagnostic_binding(
            plan, drift_labels, dense
        )
    rewritten = json.loads(json.dumps(diagnostic))
    rewritten['forbidden_changes']['source_movement'] = True
    with pytest.raises(ValueError, match='forbidden-change'):
        validate_receiver_position_sensitivity_diagnostic_binding(
            plan, rewritten, dense
        )
    stale_parent = json.loads(json.dumps(diagnostic))
    stale_parent['parent_time_gate_localization_diagnostic'][
        'semantic_sha256'
    ] = '0' * 64
    with pytest.raises(ValueError, match='time-gate diagnostic sha256'):
        validate_receiver_position_sensitivity_diagnostic_binding(
            plan, stale_parent, dense
        )
    drift_pin = json.loads(json.dumps(diagnostic))
    drift_pin['run76_record_binding']['levels'][0]['pressure_trace_sha256'] = (
        '0' * 64
    )
    with pytest.raises(ValueError, match='dense authority'):
        validate_receiver_position_sensitivity_diagnostic_binding(
            plan, drift_pin, dense
        )


def _receiver_stencil_fixture(base=(2, 2, 2)):
    h = 0.5
    axes = tuple(np.arange(6, dtype=np.float64) * h for _ in range(3))
    dims = (6, 6, 6)
    coords = [
        (base[0] + dx, base[1] + dy, base[2] + dz)
        for dz in (0, 1)
        for dy in (0, 1)
        for dx in (0, 1)
    ]
    ny, nz = dims[1], dims[2]
    linear = [ix * ny * nz + iy * nz + iz for ix, iy, iz in coords]
    position = np.asarray(base, dtype=np.float64) * h + np.asarray(
        [0.15, 0.20, 0.25], dtype=np.float64
    )
    fractional = position / h - np.asarray(base, dtype=np.float64)
    weights = [
        float(
            (1.0 - fractional[0] if dx == 0 else fractional[0])
            * (1.0 - fractional[1] if dy == 0 else fractional[1])
            * (1.0 - fractional[2] if dz == 0 else fractional[2])
        )
        for dx, dy, dz in (
            (x - base[0], y - base[1], z - base[2]) for x, y, z in coords
        )
    ]
    return axes, dims, h, np.asarray(linear), np.asarray(weights), position


def test_receiver_position_offset_stencil_translates_canonical_nodes():
    axes, dims, h, linear, weights, position = _receiver_stencil_fixture()
    moved = receiver_position_offset_stencil(
        cell_id='x_plus_1',
        xv=axes[0],
        yv=axes[1],
        zv=axes[2],
        canonical_linear_indices=linear,
        canonical_weights=weights,
        grid_spacing_m=h,
        canonical_position_m=position,
    )
    expected = linear + np.int64(dims[1] * dims[2])
    assert moved['moved_linear_indices'] == [int(x) for x in expected]
    assert moved['moved_position_m'] == [
        float(position[0] + h), float(position[1]), float(position[2]),
    ]
    assert moved['interpolation_weights'] == [float(x) for x in weights]
    assert moved['reconstruction_error_m'] <= 1.0e-9
    assert len(moved['stencil_sha256']) == 64
    canonical = receiver_position_offset_stencil(
        cell_id='canonical_position',
        xv=axes[0],
        yv=axes[1],
        zv=axes[2],
        canonical_linear_indices=linear,
        canonical_weights=weights,
        grid_spacing_m=h,
        canonical_position_m=position,
    )
    assert canonical['moved_linear_indices'] == [int(x) for x in linear]
    assert canonical['moved_position_m'] == [float(x) for x in position]


def test_receiver_position_offset_stencil_is_fail_closed():
    axes, dims, h, linear, weights, position = _receiver_stencil_fixture()
    with pytest.raises(ValueError, match='unknown receiver-position'):
        receiver_position_offset_stencil(
            cell_id='x_minus_3',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_linear_indices=linear,
            canonical_weights=weights,
            grid_spacing_m=h,
            canonical_position_m=position,
        )
    # z_minus_2 lands the moved cell on the iz=0 absorbing plane.
    with pytest.raises(ValueError, match='absorbing-layer plane'):
        receiver_position_offset_stencil(
            cell_id='z_minus_2',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_linear_indices=linear,
            canonical_weights=weights,
            grid_spacing_m=h,
            canonical_position_m=position,
        )
    # From an edge-adjacent canonical cell, x_plus_2 leaves the grid.
    edge_axes, edge_dims, edge_h, edge_linear, edge_weights, edge_position = (
        _receiver_stencil_fixture(base=(4, 2, 2))
    )
    with pytest.raises(ValueError, match='leaves the grid'):
        receiver_position_offset_stencil(
            cell_id='x_plus_2',
            xv=edge_axes[0],
            yv=edge_axes[1],
            zv=edge_axes[2],
            canonical_linear_indices=edge_linear,
            canonical_weights=edge_weights,
            grid_spacing_m=edge_h,
            canonical_position_m=edge_position,
        )
    wrong_weights = np.asarray(weights, dtype=np.float64)
    wrong_weights[0] += 0.4
    with pytest.raises(ValueError, match='does not interpolate'):
        receiver_position_offset_stencil(
            cell_id='z_minus_1',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_linear_indices=linear,
            canonical_weights=wrong_weights,
            grid_spacing_m=h,
            canonical_position_m=position,
        )
    with pytest.raises(ValueError, match='exactly eight nodes'):
        receiver_position_offset_stencil(
            cell_id='x_plus_1',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_linear_indices=linear[:7],
            canonical_weights=weights[:7],
            grid_spacing_m=h,
            canonical_position_m=position,
        )


def _receiver_cells(vectors, labels):
    cell_ids = [
        cell_id
        for cell_id in RECEIVER_POSITION_CELL_IDS
        if cell_id != RECEIVER_POSITION_CANONICAL_CELL_ID
    ]
    return [
        {
            'cell_id': cell_id,
            'worsening_by_frequency': vectors[index],
            'classification': labels[index],
        }
        for index, cell_id in enumerate(cell_ids)
    ]


def test_receiver_position_classifier_frozen_labels():
    canonical_vector = [True, False, True, False]
    identical = [canonical_vector] * 12
    result = classify_receiver_position_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_receiver_cells(identical, [_MIXED] * 12),
    )
    assert result['classification'] == (
        'RECEIVER_POSITION_WORSENING_PATTERN_INVARIANT'
    )
    assert result['identical_vector_cell_count'] == 12

    # Any moved cell localized (worsening gone) -> position-local.
    shifted = [[False, True, True, False]] * 12
    labels = [_MIXED] * 12
    labels[3] = _LOCALIZED
    labels[7] = _LOCALIZED
    result = classify_receiver_position_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_receiver_cells(shifted, labels),
    )
    assert result['classification'] == (
        'RECEIVER_POSITION_WORSENING_POSITION_LOCAL'
    )
    assert set(result['localized_cell_ids']) == {'x_plus_2', 'y_plus_2'}

    # Every moved cell carries the worsening but vectors shift ->
    # room-global.
    result = classify_receiver_position_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_receiver_cells(shifted, [_MIXED] * 12),
    )
    assert result['classification'] == (
        'RECEIVER_POSITION_WORSENING_ROOM_GLOBAL'
    )
    assert result['shifted_cell_ids'] == [
        cell_id
        for cell_id in RECEIVER_POSITION_CELL_IDS
        if cell_id != RECEIVER_POSITION_CANONICAL_CELL_ID
    ]
    assert result['localized_cell_ids'] == []

    # Hamming distances are recorded per moved cell.
    vectors = [canonical_vector] * 12
    vectors[0] = shifted[0]
    result = classify_receiver_position_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_receiver_cells(vectors, [_MIXED] * 12),
    )
    assert result['hamming_distance_by_cell']['x_minus_1'] == 2
    assert result['hamming_distance_by_cell']['x_plus_1'] == 0


def test_receiver_position_classifier_is_fail_closed_on_inputs():
    canonical_vector = [True, False, True, False]
    cells = _receiver_cells([canonical_vector] * 12, [_MIXED] * 12)
    with pytest.raises(ValueError, match='control'):
        classify_receiver_position_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=[
                {
                    'cell_id': RECEIVER_POSITION_CANONICAL_CELL_ID,
                    'worsening_by_frequency': canonical_vector,
                    'classification': _MIXED,
                }
            ],
        )
    with pytest.raises(ValueError, match='length mismatch'):
        classify_receiver_position_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=[
                {**cells[0], 'worsening_by_frequency': [True]}
            ]
            + cells[1:],
        )
    with pytest.raises(ValueError, match='frozen non-control cells'):
        classify_receiver_position_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=cells[:-1],
        )
    with pytest.raises(ValueError, match='not in the frozen set'):
        classify_receiver_position_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=[
                {**cells[0], 'cell_id': 'x_minus_3'}
            ]
            + cells[1:],
        )


def test_receiver_position_diagnostic_cannot_enter_canonical_acceptance():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_receiver_position_sensitivity_diagnostic_plan(
        RECEIVER_DIAGNOSTIC_PLAN
    )
    validate_receiver_position_sensitivity_diagnostic_binding(
        plan, diagnostic, dense
    )
    assert tuple(plan.physical_quantity.frequency_hz) == (40.0, 80.0)
    assert diagnostic['receiver_position_sensitivity']['evaluation'][
        'canonical_acceptance_inclusion'
    ] is False
    assert diagnostic['decision_semantics']['diagnostic_only'] is True
    assert diagnostic['decision_semantics'][
        'cross_solver_unblocked_by_diagnostic'
    ] is False




def test_source_position_diagnostic_plan_hash_binding_and_canonical_contract_are_frozen():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_source_position_sensitivity_diagnostic_plan(
        SOURCE_DIAGNOSTIC_PLAN
    )
    validate_source_position_sensitivity_diagnostic_binding(
        plan, diagnostic, dense
    )
    assert diagnostic['task_start_main_sha'] == (
        'aecf6842cabe91535781534156390b2ee247e844'
    )
    assert diagnostic['frozen_solver_contract']['pffdtd_ppw'] == [
        8.0, 10.0, 12.0,
    ]
    assert diagnostic['parent_receiver_position_sensitivity_diagnostic'][
        'semantic_sha256'
    ] == canonical_sha256(
        load_receiver_position_sensitivity_diagnostic_plan(
            RECEIVER_DIAGNOSTIC_PLAN
        )
    )
    source = diagnostic['source_position_sensitivity']
    offsets = source['source_offsets']
    assert [item['cell_id'] for item in offsets] == list(
        SOURCE_POSITION_CELL_IDS
    )
    assert [bool(item.get('control')) for item in offsets] == [True] + [
        False
    ] * (len(SOURCE_POSITION_CELL_IDS) - 1)
    for item in offsets:
        assert item['offset_cells'] == list(
            SOURCE_POSITION_OFFSET_CELLS[item['cell_id']]
        )
    cell_axis = source['cell_axis']
    assert cell_axis['cells'] == list(SOURCE_POSITION_CELL_IDS)
    assert cell_axis['canonical_cell_id'] == (
        SOURCE_POSITION_CANONICAL_CELL_ID
    )
    evaluation = source['evaluation']
    dense_block = dense['dense_frequency_neighborhood']
    assert evaluation['normalized_complex_difference_formula'] == dense_block[
        'normalized_complex_difference_formula'
    ]
    assert evaluation['fixed_floor'] == dense_block['fixed_floor']
    assert evaluation['pairs'] == dense_block['pairs']
    assert evaluation['per_cell_classification'][
        'localized_max_count'
    ] == dense_block['classification']['localized_max_count']
    assert evaluation['per_cell_classification'][
        'persists_min_count'
    ] == dense_block['classification']['persists_min_count']
    assert evaluation['canonical_acceptance_inclusion'] is False


def test_source_position_run76_pins_match_dense_authority():
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_source_position_sensitivity_diagnostic_plan(
        SOURCE_DIAGNOSTIC_PLAN
    )
    dense_levels = {
        float(item['points_per_wavelength']): item
        for item in dense['run76_record_binding']['levels']
    }
    for level in diagnostic['run76_record_binding']['levels']:
        dense_level = dense_levels[float(level['points_per_wavelength'])]
        assert level['pressure_trace_sha256'] == dense_level[
            'pressure_trace_sha256'
        ]
        assert level['source_trace_sha256'] == dense_level['source_trace_sha256']


def test_source_position_diagnostic_binding_rejects_mutations():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_source_position_sensitivity_diagnostic_plan(
        SOURCE_DIAGNOSTIC_PLAN
    )
    stale = json.loads(json.dumps(diagnostic))
    stale['frozen_solver_contract']['fixture_id'] = 'stale-fixture'
    with pytest.raises(ValueError, match='fixture id'):
        validate_source_position_sensitivity_diagnostic_binding(
            plan, stale, dense
        )
    promoted = json.loads(json.dumps(diagnostic))
    promoted['source_position_sensitivity']['evaluation'][
        'canonical_acceptance_inclusion'
    ] = True
    with pytest.raises(ValueError, match='canonical acceptance'):
        validate_source_position_sensitivity_diagnostic_binding(
            plan, promoted, dense
        )
    drift = json.loads(json.dumps(diagnostic))
    drift['source_position_sensitivity']['source_offsets'][1][
        'cell_id'
    ] = 'x_minus_3'
    with pytest.raises(ValueError, match='not the frozen set'):
        validate_source_position_sensitivity_diagnostic_binding(
            plan, drift, dense
        )
    drift_offset = json.loads(json.dumps(diagnostic))
    drift_offset['source_position_sensitivity']['source_offsets'][1][
        'offset_cells'
    ] = [-3, 0, 0]
    with pytest.raises(ValueError, match='whole-cell offset'):
        validate_source_position_sensitivity_diagnostic_binding(
            plan, drift_offset, dense
        )
    drift_cell = json.loads(json.dumps(diagnostic))
    drift_cell['source_position_sensitivity']['cell_axis'][
        'canonical_cell_id'
    ] = 'x_plus_1'
    with pytest.raises(ValueError, match='canonical cell'):
        validate_source_position_sensitivity_diagnostic_binding(
            plan, drift_cell, dense
        )
    drift_metric = json.loads(json.dumps(diagnostic))
    drift_metric['source_position_sensitivity']['evaluation'][
        'fixed_floor'
    ] = 1.0e-9
    with pytest.raises(ValueError, match='fixed floor'):
        validate_source_position_sensitivity_diagnostic_binding(
            plan, drift_metric, dense
        )
    drift_labels = json.loads(json.dumps(diagnostic))
    drift_labels['source_position_sensitivity']['evaluation'][
        'classification'
    ]['receiver_local'] = 'RENAMED'
    with pytest.raises(ValueError, match='classification labels'):
        validate_source_position_sensitivity_diagnostic_binding(
            plan, drift_labels, dense
        )
    rewritten = json.loads(json.dumps(diagnostic))
    rewritten['forbidden_changes']['receiver_movement'] = True
    with pytest.raises(ValueError, match='forbidden-change'):
        validate_source_position_sensitivity_diagnostic_binding(
            plan, rewritten, dense
        )
    stale_parent = json.loads(json.dumps(diagnostic))
    stale_parent['parent_receiver_position_sensitivity_diagnostic'][
        'semantic_sha256'
    ] = '0' * 64
    with pytest.raises(ValueError, match='receiver-position diagnostic sha256'):
        validate_source_position_sensitivity_diagnostic_binding(
            plan, stale_parent, dense
        )
    drift_pin = json.loads(json.dumps(diagnostic))
    drift_pin['run76_record_binding']['levels'][0]['pressure_trace_sha256'] = (
        '0' * 64
    )
    with pytest.raises(ValueError, match='dense authority'):
        validate_source_position_sensitivity_diagnostic_binding(
            plan, drift_pin, dense
        )


def _source_stencil_fixture(base=(2, 2, 2)):
    h = 0.5
    axes = tuple(np.arange(6, dtype=np.float64) * h for _ in range(3))
    dims = (6, 6, 6)
    coords = [
        (base[0] + dx, base[1] + dy, base[2] + dz)
        for dz in (0, 1)
        for dy in (0, 1)
        for dx in (0, 1)
    ]
    ny, nz = dims[1], dims[2]
    linear = [ix * ny * nz + iy * nz + iz for ix, iy, iz in coords]
    position = np.asarray(base, dtype=np.float64) * h + np.asarray(
        [0.15, 0.20, 0.25], dtype=np.float64
    )
    fractional = position / h - np.asarray(base, dtype=np.float64)
    weights = [
        float(
            (1.0 - fractional[0] if dx == 0 else fractional[0])
            * (1.0 - fractional[1] if dy == 0 else fractional[1])
            * (1.0 - fractional[2] if dz == 0 else fractional[2])
        )
        for dx, dy, dz in (
            (x - base[0], y - base[1], z - base[2]) for x, y, z in coords
        )
    ]
    return axes, dims, h, np.asarray(linear), np.asarray(weights), position


def test_source_position_offset_stencil_translates_canonical_nodes():
    axes, dims, h, linear, weights, position = _source_stencil_fixture()
    moved = source_position_offset_stencil(
        cell_id='x_plus_1',
        xv=axes[0],
        yv=axes[1],
        zv=axes[2],
        canonical_linear_indices=linear,
        canonical_weights=weights,
        grid_spacing_m=h,
        canonical_position_m=position,
    )
    expected = linear + np.int64(dims[1] * dims[2])
    assert moved['moved_linear_indices'] == [int(x) for x in expected]
    assert moved['moved_position_m'] == [
        float(position[0] + h), float(position[1]), float(position[2]),
    ]
    assert moved['interpolation_weights'] == [float(x) for x in weights]
    assert moved['reconstruction_error_m'] <= 1.0e-9
    assert len(moved['stencil_sha256']) == 64
    canonical = source_position_offset_stencil(
        cell_id='canonical_position',
        xv=axes[0],
        yv=axes[1],
        zv=axes[2],
        canonical_linear_indices=linear,
        canonical_weights=weights,
        grid_spacing_m=h,
        canonical_position_m=position,
    )
    assert canonical['moved_linear_indices'] == [int(x) for x in linear]
    assert canonical['moved_position_m'] == [float(x) for x in position]


def test_source_position_offset_stencil_is_fail_closed():
    axes, dims, h, linear, weights, position = _source_stencil_fixture()
    with pytest.raises(ValueError, match='unknown source-position'):
        source_position_offset_stencil(
            cell_id='x_minus_3',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_linear_indices=linear,
            canonical_weights=weights,
            grid_spacing_m=h,
            canonical_position_m=position,
        )
    # z_minus_2 lands the moved cell on the iz=0 absorbing plane.
    with pytest.raises(ValueError, match='absorbing-layer plane'):
        source_position_offset_stencil(
            cell_id='z_minus_2',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_linear_indices=linear,
            canonical_weights=weights,
            grid_spacing_m=h,
            canonical_position_m=position,
        )
    # From an edge-adjacent canonical cell, x_plus_2 leaves the grid.
    edge_axes, edge_dims, edge_h, edge_linear, edge_weights, edge_position = (
        _source_stencil_fixture(base=(4, 2, 2))
    )
    with pytest.raises(ValueError, match='leaves the grid'):
        source_position_offset_stencil(
            cell_id='x_plus_2',
            xv=edge_axes[0],
            yv=edge_axes[1],
            zv=edge_axes[2],
            canonical_linear_indices=edge_linear,
            canonical_weights=edge_weights,
            grid_spacing_m=edge_h,
            canonical_position_m=edge_position,
        )
    wrong_weights = np.asarray(weights, dtype=np.float64)
    wrong_weights[0] += 0.4
    with pytest.raises(ValueError, match='does not interpolate'):
        source_position_offset_stencil(
            cell_id='z_minus_1',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_linear_indices=linear,
            canonical_weights=wrong_weights,
            grid_spacing_m=h,
            canonical_position_m=position,
        )
    with pytest.raises(ValueError, match='exactly eight nodes'):
        source_position_offset_stencil(
            cell_id='x_plus_1',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_linear_indices=linear[:7],
            canonical_weights=weights[:7],
            grid_spacing_m=h,
            canonical_position_m=position,
        )


def _source_cells(vectors, labels):
    cell_ids = [
        cell_id
        for cell_id in SOURCE_POSITION_CELL_IDS
        if cell_id != SOURCE_POSITION_CANONICAL_CELL_ID
    ]
    return [
        {
            'cell_id': cell_id,
            'worsening_by_frequency': vectors[index],
            'classification': labels[index],
        }
        for index, cell_id in enumerate(cell_ids)
    ]


def test_source_position_classifier_frozen_labels():
    canonical_vector = [True, False, True, False]
    identical = [canonical_vector] * 12
    result = classify_source_position_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_source_cells(identical, [_MIXED] * 12),
    )
    assert result['classification'] == (
        'SOURCE_POSITION_WORSENING_PATTERN_INVARIANT'
    )
    assert result['identical_vector_cell_count'] == 12

    # Any moved cell localized (worsening gone) -> position-local.
    shifted = [[False, True, True, False]] * 12
    labels = [_MIXED] * 12
    labels[3] = _LOCALIZED
    labels[7] = _LOCALIZED
    result = classify_source_position_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_source_cells(shifted, labels),
    )
    assert result['classification'] == (
        'SOURCE_POSITION_WORSENING_POSITION_LOCAL'
    )
    assert set(result['localized_cell_ids']) == {'x_plus_2', 'y_plus_2'}

    # Every moved cell carries the worsening but vectors shift ->
    # receiver-local.
    result = classify_source_position_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_source_cells(shifted, [_MIXED] * 12),
    )
    assert result['classification'] == (
        'SOURCE_POSITION_WORSENING_RECEIVER_LOCAL'
    )
    assert result['shifted_cell_ids'] == [
        cell_id
        for cell_id in SOURCE_POSITION_CELL_IDS
        if cell_id != SOURCE_POSITION_CANONICAL_CELL_ID
    ]
    assert result['localized_cell_ids'] == []

    # Hamming distances are recorded per moved cell.
    vectors = [canonical_vector] * 12
    vectors[0] = shifted[0]
    result = classify_source_position_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_source_cells(vectors, [_MIXED] * 12),
    )
    assert result['hamming_distance_by_cell']['x_minus_1'] == 2
    assert result['hamming_distance_by_cell']['x_plus_1'] == 0


def test_source_position_classifier_is_fail_closed_on_inputs():
    canonical_vector = [True, False, True, False]
    cells = _source_cells([canonical_vector] * 12, [_MIXED] * 12)
    with pytest.raises(ValueError, match='control'):
        classify_source_position_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=[
                {
                    'cell_id': SOURCE_POSITION_CANONICAL_CELL_ID,
                    'worsening_by_frequency': canonical_vector,
                    'classification': _MIXED,
                }
            ],
        )
    with pytest.raises(ValueError, match='length mismatch'):
        classify_source_position_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=[
                {**cells[0], 'worsening_by_frequency': [True]}
            ]
            + cells[1:],
        )
    with pytest.raises(ValueError, match='frozen non-control cells'):
        classify_source_position_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=cells[:-1],
        )
    with pytest.raises(ValueError, match='not in the frozen set'):
        classify_source_position_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=[
                {**cells[0], 'cell_id': 'x_minus_3'}
            ]
            + cells[1:],
        )


def test_source_position_diagnostic_cannot_enter_canonical_acceptance():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_source_position_sensitivity_diagnostic_plan(
        SOURCE_DIAGNOSTIC_PLAN
    )
    validate_source_position_sensitivity_diagnostic_binding(
        plan, diagnostic, dense
    )
    assert tuple(plan.physical_quantity.frequency_hz) == (40.0, 80.0)
    assert diagnostic['source_position_sensitivity']['evaluation'][
        'canonical_acceptance_inclusion'
    ] is False
    assert diagnostic['decision_semantics']['diagnostic_only'] is True
    assert diagnostic['decision_semantics'][
        'cross_solver_unblocked_by_diagnostic'
    ] is False



def test_joint_translation_diagnostic_plan_hash_binding_and_canonical_contract_are_frozen():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_joint_translation_sensitivity_diagnostic_plan(
        JOINT_DIAGNOSTIC_PLAN
    )
    validate_joint_translation_sensitivity_diagnostic_binding(
        plan, diagnostic, dense
    )
    assert diagnostic['task_start_main_sha'] == (
        '9c6d1582d5919f26d24e78757660b230aa6eeab1'
    )
    assert diagnostic['frozen_solver_contract']['pffdtd_ppw'] == [
        8.0, 10.0, 12.0,
    ]
    assert diagnostic['parent_source_position_sensitivity_diagnostic'][
        'semantic_sha256'
    ] == canonical_sha256(
        load_source_position_sensitivity_diagnostic_plan(
            SOURCE_DIAGNOSTIC_PLAN
        )
    )
    joint = diagnostic['joint_translation_sensitivity']
    offsets = joint['joint_offsets']
    assert [item['cell_id'] for item in offsets] == list(
        JOINT_TRANSLATION_CELL_IDS
    )
    assert [bool(item.get('control')) for item in offsets] == [True] + [
        False
    ] * (len(JOINT_TRANSLATION_CELL_IDS) - 1)
    for item in offsets:
        assert item['offset_cells'] == list(
            JOINT_TRANSLATION_OFFSET_CELLS[item['cell_id']]
        )
    cell_axis = joint['cell_axis']
    assert cell_axis['cells'] == list(JOINT_TRANSLATION_CELL_IDS)
    assert cell_axis['canonical_cell_id'] == (
        JOINT_TRANSLATION_CANONICAL_CELL_ID
    )
    evaluation = joint['evaluation']
    dense_block = dense['dense_frequency_neighborhood']
    assert evaluation['normalized_complex_difference_formula'] == dense_block[
        'normalized_complex_difference_formula'
    ]
    assert evaluation['fixed_floor'] == dense_block['fixed_floor']
    assert evaluation['pairs'] == dense_block['pairs']
    assert evaluation['per_cell_classification'][
        'localized_max_count'
    ] == dense_block['classification']['localized_max_count']
    assert evaluation['per_cell_classification'][
        'persists_min_count'
    ] == dense_block['classification']['persists_min_count']
    assert evaluation['canonical_acceptance_inclusion'] is False


def test_joint_translation_run76_pins_match_dense_authority():
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_joint_translation_sensitivity_diagnostic_plan(
        JOINT_DIAGNOSTIC_PLAN
    )
    dense_levels = {
        float(item['points_per_wavelength']): item
        for item in dense['run76_record_binding']['levels']
    }
    for level in diagnostic['run76_record_binding']['levels']:
        dense_level = dense_levels[float(level['points_per_wavelength'])]
        assert level['pressure_trace_sha256'] == dense_level[
            'pressure_trace_sha256'
        ]
        assert level['source_trace_sha256'] == dense_level['source_trace_sha256']


def test_joint_translation_diagnostic_binding_rejects_mutations():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_joint_translation_sensitivity_diagnostic_plan(
        JOINT_DIAGNOSTIC_PLAN
    )
    stale = json.loads(json.dumps(diagnostic))
    stale['frozen_solver_contract']['fixture_id'] = 'stale-fixture'
    with pytest.raises(ValueError, match='fixture id'):
        validate_joint_translation_sensitivity_diagnostic_binding(
            plan, stale, dense
        )
    promoted = json.loads(json.dumps(diagnostic))
    promoted['joint_translation_sensitivity']['evaluation'][
        'canonical_acceptance_inclusion'
    ] = True
    with pytest.raises(ValueError, match='canonical acceptance'):
        validate_joint_translation_sensitivity_diagnostic_binding(
            plan, promoted, dense
        )
    drift = json.loads(json.dumps(diagnostic))
    drift['joint_translation_sensitivity']['joint_offsets'][1][
        'cell_id'
    ] = 'x_minus_3'
    with pytest.raises(ValueError, match='not the frozen set'):
        validate_joint_translation_sensitivity_diagnostic_binding(
            plan, drift, dense
        )
    drift_offset = json.loads(json.dumps(diagnostic))
    drift_offset['joint_translation_sensitivity']['joint_offsets'][1][
        'offset_cells'
    ] = [-3, 0, 0]
    with pytest.raises(ValueError, match='whole-cell offset'):
        validate_joint_translation_sensitivity_diagnostic_binding(
            plan, drift_offset, dense
        )
    drift_cell = json.loads(json.dumps(diagnostic))
    drift_cell['joint_translation_sensitivity']['cell_axis'][
        'canonical_cell_id'
    ] = 'x_plus_1'
    with pytest.raises(ValueError, match='canonical cell'):
        validate_joint_translation_sensitivity_diagnostic_binding(
            plan, drift_cell, dense
        )
    drift_metric = json.loads(json.dumps(diagnostic))
    drift_metric['joint_translation_sensitivity']['evaluation'][
        'fixed_floor'
    ] = 1.0e-9
    with pytest.raises(ValueError, match='fixed floor'):
        validate_joint_translation_sensitivity_diagnostic_binding(
            plan, drift_metric, dense
        )
    drift_labels = json.loads(json.dumps(diagnostic))
    drift_labels['joint_translation_sensitivity']['evaluation'][
        'classification'
    ]['shifted'] = 'RENAMED'
    with pytest.raises(ValueError, match='classification labels'):
        validate_joint_translation_sensitivity_diagnostic_binding(
            plan, drift_labels, dense
        )
    rewritten = json.loads(json.dumps(diagnostic))
    rewritten['forbidden_changes']['receiver_movement'] = True
    with pytest.raises(ValueError, match='forbidden-change'):
        validate_joint_translation_sensitivity_diagnostic_binding(
            plan, rewritten, dense
        )
    stale_parent = json.loads(json.dumps(diagnostic))
    stale_parent['parent_source_position_sensitivity_diagnostic'][
        'semantic_sha256'
    ] = '0' * 64
    with pytest.raises(ValueError, match='source-position diagnostic sha256'):
        validate_joint_translation_sensitivity_diagnostic_binding(
            plan, stale_parent, dense
        )
    drift_pin = json.loads(json.dumps(diagnostic))
    drift_pin['run76_record_binding']['levels'][0]['pressure_trace_sha256'] = (
        '0' * 64
    )
    with pytest.raises(ValueError, match='dense authority'):
        validate_joint_translation_sensitivity_diagnostic_binding(
            plan, drift_pin, dense
        )


def _joint_stencil_fixture():
    # Source trilinear cell at base (2,2,2), receiver at base (3,2,2):
    # the pair separation is exactly one cell along +x, mirroring the
    # canonical fixture's 1 m x-axis separation at any level's grid.
    s_axes, dims, h, s_linear, weights, s_position = _source_stencil_fixture(
        base=(2, 2, 2)
    )
    _, _, _, r_linear, _, r_position = _source_stencil_fixture(base=(3, 2, 2))
    return s_axes, dims, h, s_linear, r_linear, weights, s_position, r_position


def test_joint_translation_offset_stencil_translates_both_stencils():
    axes, dims, h, s_linear, r_linear, weights, s_position, r_position = (
        _joint_stencil_fixture()
    )
    moved = joint_translation_offset_stencil(
        cell_id='y_plus_1',
        xv=axes[0],
        yv=axes[1],
        zv=axes[2],
        canonical_source_linear_indices=s_linear,
        canonical_source_weights=weights,
        canonical_receiver_linear_indices=r_linear,
        canonical_receiver_weights=weights,
        grid_spacing_m=h,
        canonical_source_position_m=s_position,
        canonical_receiver_position_m=r_position,
    )
    expected = np.int64(dims[2])
    assert moved['moved_source_linear_indices'] == [
        int(x) for x in s_linear + expected
    ]
    assert moved['moved_receiver_linear_indices'] == [
        int(x) for x in r_linear + expected
    ]
    assert moved['moved_source_position_m'] == [
        float(s_position[0]), float(s_position[1] + h), float(s_position[2]),
    ]
    assert moved['moved_receiver_position_m'] == [
        float(r_position[0]), float(r_position[1] + h), float(r_position[2]),
    ]
    assert moved['source_interpolation_weights'] == [
        float(x) for x in weights
    ]
    assert moved['receiver_interpolation_weights'] == [
        float(x) for x in weights
    ]
    assert moved['source_reconstruction_error_m'] <= 1.0e-9
    assert moved['receiver_reconstruction_error_m'] <= 1.0e-9
    assert moved['separation_preservation_error_m'] <= 1.0e-12
    canonical_separation = np.asarray(s_position) - np.asarray(r_position)
    assert moved['pair_separation_m'] == [
        float(x) for x in canonical_separation
    ]
    assert len(moved['stencil_sha256']) == 64
    canonical = joint_translation_offset_stencil(
        cell_id='canonical_position',
        xv=axes[0],
        yv=axes[1],
        zv=axes[2],
        canonical_source_linear_indices=s_linear,
        canonical_source_weights=weights,
        canonical_receiver_linear_indices=r_linear,
        canonical_receiver_weights=weights,
        grid_spacing_m=h,
        canonical_source_position_m=s_position,
        canonical_receiver_position_m=r_position,
    )
    assert canonical['moved_source_linear_indices'] == [
        int(x) for x in s_linear
    ]
    assert canonical['moved_receiver_linear_indices'] == [
        int(x) for x in r_linear
    ]
    assert canonical['moved_source_position_m'] == [
        float(x) for x in s_position
    ]
    assert canonical['moved_receiver_position_m'] == [
        float(x) for x in r_position
    ]


def test_joint_translation_offset_stencil_is_fail_closed():
    axes, dims, h, s_linear, r_linear, weights, s_position, r_position = (
        _joint_stencil_fixture()
    )
    with pytest.raises(ValueError, match='unknown joint-translation'):
        joint_translation_offset_stencil(
            cell_id='x_minus_3',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_source_linear_indices=s_linear,
            canonical_source_weights=weights,
            canonical_receiver_linear_indices=r_linear,
            canonical_receiver_weights=weights,
            grid_spacing_m=h,
            canonical_source_position_m=s_position,
            canonical_receiver_position_m=r_position,
        )
    # z_minus_2 lands the moved cells on the iz=0 absorbing plane.
    with pytest.raises(ValueError, match='absorbing-layer plane'):
        joint_translation_offset_stencil(
            cell_id='z_minus_2',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_source_linear_indices=s_linear,
            canonical_source_weights=weights,
            canonical_receiver_linear_indices=r_linear,
            canonical_receiver_weights=weights,
            grid_spacing_m=h,
            canonical_source_position_m=s_position,
            canonical_receiver_position_m=r_position,
        )
    # x_plus_2 pushes the receiver cell (base ix=3) past the grid edge.
    with pytest.raises(ValueError, match='leaves the grid'):
        joint_translation_offset_stencil(
            cell_id='x_plus_2',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_source_linear_indices=s_linear,
            canonical_source_weights=weights,
            canonical_receiver_linear_indices=r_linear,
            canonical_receiver_weights=weights,
            grid_spacing_m=h,
            canonical_source_position_m=s_position,
            canonical_receiver_position_m=r_position,
        )
    wrong_weights = np.asarray(weights, dtype=np.float64)
    wrong_weights[0] += 0.4
    with pytest.raises(ValueError, match='does not interpolate'):
        joint_translation_offset_stencil(
            cell_id='z_minus_1',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_source_linear_indices=s_linear,
            canonical_source_weights=wrong_weights,
            canonical_receiver_linear_indices=r_linear,
            canonical_receiver_weights=weights,
            grid_spacing_m=h,
            canonical_source_position_m=s_position,
            canonical_receiver_position_m=r_position,
        )
    with pytest.raises(ValueError, match='does not interpolate'):
        joint_translation_offset_stencil(
            cell_id='z_minus_1',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_source_linear_indices=s_linear,
            canonical_source_weights=weights,
            canonical_receiver_linear_indices=r_linear,
            canonical_receiver_weights=wrong_weights,
            grid_spacing_m=h,
            canonical_source_position_m=s_position,
            canonical_receiver_position_m=r_position,
        )
    with pytest.raises(ValueError, match='exactly eight nodes'):
        joint_translation_offset_stencil(
            cell_id='x_plus_1',
            xv=axes[0],
            yv=axes[1],
            zv=axes[2],
            canonical_source_linear_indices=s_linear[:7],
            canonical_source_weights=weights[:7],
            canonical_receiver_linear_indices=r_linear,
            canonical_receiver_weights=weights,
            grid_spacing_m=h,
            canonical_source_position_m=s_position,
            canonical_receiver_position_m=r_position,
        )


def _joint_cells(vectors, labels):
    cell_ids = [
        cell_id
        for cell_id in JOINT_TRANSLATION_CELL_IDS
        if cell_id != JOINT_TRANSLATION_CANONICAL_CELL_ID
    ]
    return [
        {
            'cell_id': cell_id,
            'worsening_by_frequency': vectors[index],
            'classification': labels[index],
        }
        for index, cell_id in enumerate(cell_ids)
    ]


def test_joint_translation_classifier_frozen_labels():
    canonical_vector = [True, False, True, False]
    identical = [canonical_vector] * 12
    result = classify_joint_translation_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_joint_cells(identical, [_MIXED] * 12),
    )
    assert result['classification'] == (
        'JOINT_TRANSLATION_WORSENING_RELATIVE_GEOMETRY_INVARIANT'
    )
    assert result['identical_vector_cell_count'] == 12

    # Any moved cell localized (worsening gone) -> absolute-position bound.
    shifted = [[False, True, True, False]] * 12
    labels = [_MIXED] * 12
    labels[3] = _LOCALIZED
    labels[7] = _LOCALIZED
    result = classify_joint_translation_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_joint_cells(shifted, labels),
    )
    assert result['classification'] == (
        'JOINT_TRANSLATION_WORSENING_ABSOLUTE_POSITION_BOUND'
    )
    assert set(result['localized_cell_ids']) == {'x_plus_2', 'y_plus_2'}

    # Every moved pair still carries the worsening but vectors shift ->
    # pattern shifted.
    result = classify_joint_translation_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_joint_cells(shifted, [_MIXED] * 12),
    )
    assert result['classification'] == (
        'JOINT_TRANSLATION_WORSENING_PATTERN_SHIFTED'
    )
    assert result['shifted_cell_ids'] == [
        cell_id
        for cell_id in JOINT_TRANSLATION_CELL_IDS
        if cell_id != JOINT_TRANSLATION_CANONICAL_CELL_ID
    ]
    assert result['localized_cell_ids'] == []

    # Hamming distances are recorded per moved cell.
    vectors = [canonical_vector] * 12
    vectors[0] = shifted[0]
    result = classify_joint_translation_sensitivity(
        canonical_worsening_by_frequency=canonical_vector,
        canonical_classification=_MIXED,
        noncanonical_cells=_joint_cells(vectors, [_MIXED] * 12),
    )
    assert result['hamming_distance_by_cell']['x_minus_1'] == 2
    assert result['hamming_distance_by_cell']['x_plus_1'] == 0


def test_joint_translation_classifier_is_fail_closed_on_inputs():
    canonical_vector = [True, False, True, False]
    cells = _joint_cells([canonical_vector] * 12, [_MIXED] * 12)
    with pytest.raises(ValueError, match='control'):
        classify_joint_translation_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=[
                {
                    'cell_id': JOINT_TRANSLATION_CANONICAL_CELL_ID,
                    'worsening_by_frequency': canonical_vector,
                    'classification': _MIXED,
                }
            ],
        )
    with pytest.raises(ValueError, match='length mismatch'):
        classify_joint_translation_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=[
                {**cells[0], 'worsening_by_frequency': [True]}
            ]
            + cells[1:],
        )
    with pytest.raises(ValueError, match='frozen non-control cells'):
        classify_joint_translation_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=cells[:-1],
        )
    with pytest.raises(ValueError, match='not in the frozen set'):
        classify_joint_translation_sensitivity(
            canonical_worsening_by_frequency=canonical_vector,
            canonical_classification=_MIXED,
            noncanonical_cells=[
                {**cells[0], 'cell_id': 'x_minus_3'}
            ]
            + cells[1:],
        )


def test_joint_translation_diagnostic_cannot_enter_canonical_acceptance():
    plan = _plan()
    dense = load_dense_frequency_diagnostic_plan(DENSE_DIAGNOSTIC_PLAN)
    diagnostic = load_joint_translation_sensitivity_diagnostic_plan(
        JOINT_DIAGNOSTIC_PLAN
    )
    validate_joint_translation_sensitivity_diagnostic_binding(
        plan, diagnostic, dense
    )
    assert tuple(plan.physical_quantity.frequency_hz) == (40.0, 80.0)
    assert diagnostic['joint_translation_sensitivity']['evaluation'][
        'canonical_acceptance_inclusion'
    ] is False
    assert diagnostic['decision_semantics']['diagnostic_only'] is True
    assert diagnostic['decision_semantics'][
        'cross_solver_unblocked_by_diagnostic'
    ] is False
