from __future__ import annotations

import json
from pathlib import Path

import pytest

from htdt.r130d_general3d_validation import (
    EVIDENCE_SCHEMA,
    PairMetrics,
    compare_complex_transfer,
    load_evidence,
    load_validation_plan,
    save_evidence,
    validate_exact_binding,
    validate_refinement_schedule,
    validation_decision,
)


PLAN = Path(__file__).parents[2] / 'benchmarks' / 'acoustics' / 'r130d_general3d_validation_plan.json'


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
    assert plan.reference_mesh_sha256(0) == plan.reference_mesh_sha256(0)
    assert plan.reference_mesh_sha256(0) != plan.reference_mesh_sha256(1)
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
        reference_refinements=(0, 1, 2),
        pffdtd_points_per_wavelength=(6.0, 8.0, 10.0),
    )
    with pytest.raises(ValueError, match='reference refinement schedule'):
        validate_refinement_schedule(
            plan,
            reference_refinements=(0, 2),
            pffdtd_points_per_wavelength=(6.0, 8.0, 10.0),
        )
    with pytest.raises(ValueError, match='PFFDTD refinement schedule'):
        validate_refinement_schedule(
            plan,
            reference_refinements=(0, 1, 2),
            pffdtd_points_per_wavelength=(6.0, 8.0, 12.0),
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
