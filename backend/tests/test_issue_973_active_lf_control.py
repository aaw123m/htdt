"""Issue #973: active low-frequency MIMO control authority — transfer-matrix
plans, vendor-opaque bindings, support/localization/latency guardrails."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_active_lf_control import (
    ActiveLowFrequencyControlPlan,
    ControlMatrixEntry,
    VendorControllerBinding,
    build_control_plan,
    evaluate_support_guardrails,
)

_H = 'e' * 64


def _entry(input_group='LFE', output_group='SUB1', **overrides):
    kwargs = dict(
        input_group=input_group,
        output_group=output_group,
        gain_db=-4.0,
        delay_s=0.005,
        filter_kind='fir',
        filter_ref='fir-1',
        valid_band_hz=(20.0, 120.0),
        latency_s=0.012,
    )
    kwargs.update(overrides)
    return ControlMatrixEntry(**kwargs)


def _plan(**overrides):
    kwargs = dict(
        plan_id='alfc-1',
        schema_version='alfc_v1',
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_H,
        logical_input_groups=('FL', 'FR', 'LFE'),
        physical_output_groups=('FL', 'FR', 'SUB1', 'SUB2'),
        control_band_hz=(20.0, 150.0),
        representation='transfer_matrix',
        matrix=(
            _entry('LFE', 'SUB1'),
            _entry('LFE', 'SUB2', gain_db=-6.0),
            _entry('FL', 'SUB1', gain_db=-12.0),
        ),
        objectives=('support_speaker', 'decay_control'),
        support_band_upper_limit_hz=150.0,
        source_capability_refs=('cap-sub1', 'cap-sub2'),
        total_latency_s=0.018,
        lifecycle='proposed',
        design_producer='htdt',
        design_version='1',
        created_at_utc='2026-09-25T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_control_plan(**kwargs)


def test_plan_is_sealed():
    plan = _plan()
    assert len(plan.plan_sha256) == 64
    ActiveLowFrequencyControlPlan.model_validate(plan.model_dump(mode='json'))
    payload = plan.model_dump(mode='json')
    payload['control_band_hz'] = [10.0, 150.0]
    with pytest.raises(ValidationError, match='hash mismatch'):
        ActiveLowFrequencyControlPlan.model_validate(payload)


def test_matrix_entries_must_reference_declared_groups():
    with pytest.raises(ValidationError, match='declared logical input'):
        _plan(matrix=(_entry('UNKNOWN_IN', 'SUB1'),))
    with pytest.raises(ValidationError, match='declared physical output'):
        _plan(matrix=(_entry('LFE', 'GARBAGE'),))


def test_enabled_path_needs_effect():
    with pytest.raises(ValidationError, match='transfer quantity'):
        ControlMatrixEntry(input_group='LFE', output_group='SUB1')


def test_entry_band_cannot_exceed_plan_band():
    with pytest.raises(ValidationError, match='control band'):
        _plan(matrix=(_entry(valid_band_hz=(20.0, 400.0)),))


def test_vendor_opaque_requires_binding():
    with pytest.raises(ValidationError, match='vendor_binding'):
        _plan(representation='vendor_opaque', matrix=())
    plan = _plan(
        representation='vendor_opaque',
        matrix=(),
        vendor_binding=VendorControllerBinding(
            vendor='Dirac', product='ART', profile_name='p1'
        ),
    )
    assert plan.representation == 'vendor_opaque'


def test_applied_state_requires_representation():
    with pytest.raises(ValidationError, match='real representation'):
        _plan(lifecycle='applied', representation='unknown')


def test_support_guardrail_blocks_overdriving_band():
    plan = _plan(
        support_band_upper_limit_hz=120.0,
        matrix=(_entry(valid_band_hz=(20.0, 150.0)),),
    )
    findings = evaluate_support_guardrails(
        plan, source_capability_declared=True
    )
    blocking = [f for f in findings if f.blocking]
    assert any(f.kind == 'support_band_exceeds_localization_limit' for f in blocking)


def test_missing_capability_is_explicit_limitation():
    plan = _plan()
    findings = evaluate_support_guardrails(plan, source_capability_declared=False)
    assert any(f.kind == 'missing_source_capability' for f in findings)


def test_latency_must_be_disclosed():
    plan = _plan(total_latency_s=None)
    findings = evaluate_support_guardrails(plan, source_capability_declared=True)
    assert any(f.kind == 'latency_undisclosed' for f in findings)
