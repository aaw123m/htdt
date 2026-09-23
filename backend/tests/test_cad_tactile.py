"""Tactile transducer system tests (#638)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_bass_management import CrossoverSpec, FrequencyBand
from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_tactile import (
    RattleFinding,
    TactileActuatorDefinition,
    TactileMeasurement,
    build_tactile_actuator_definition,
    build_tactile_attachment_binding,
    build_tactile_processing_profile,
    check_tactile_electrical,
    evaluate_tactile_system,
    tactile_system_status,
)


def _provenance():
    return (
        EquipmentDataProvenance(
            evidence_kind='manufacturer',
            source_name='Example Tactile Co.',
            source_version='2026.1',
            source_reference='BST-1 datasheet',
            source_sha256='5' * 64,
        ),
    )


def _actuator(**overrides):
    kwargs = dict(
        definition_id='bst-1',
        version='1',
        manufacturer='Example Tactile Co.',
        model='BST-1',
        tech_class='piston',
        impedance_ohm=4.0,
        power_handling_w=100.0,
        operating_band=FrequencyBand(low_hz=5.0, high_hz=200.0),
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return build_tactile_actuator_definition(**kwargs)


def _binding(**overrides):
    kwargs = dict(
        binding_id='bind-seat-1',
        actuator_instance_id='inst-bst-1',
        target_entity_id='seat-front',
        target_kind='seat',
        attachment_point='seat base rear',
        orientation='vertical',
        install_state='mounted',
        actuator_definition_sha256=_actuator().definition_sha256,
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return build_tactile_attachment_binding(**kwargs)


def _processing(**overrides):
    kwargs = dict(
        profile_id='tactile-bus-1',
        version='1',
        source_bus='sub_out_1_parallel',
        amplifier_channel_ref='amp-ch-3',
        high_pass=CrossoverSpec(frequency_hz=10.0),
        low_pass=CrossoverSpec(
            frequency_hz=40.0, slope_db_per_octave=24.0,
            filter_family='butterworth',
        ),
        gain_db=-3.0,
        delay_ms=4.0,
        polarity='normal',
        limiter_policy='soft-clip at 80W',
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return build_tactile_processing_profile(**kwargs)


def test_actuator_definition_contract():
    actuator = _actuator()
    assert actuator.tech_class == 'piston'
    assert actuator.operating_band.high_hz == 200.0
    payload = actuator.model_dump(mode='python')
    payload['tech_class'] = 'voice_coil'
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        TactileActuatorDefinition(**payload)
    with pytest.raises(ValueError, match='supplied together'):
        _actuator(model=None)


def test_binding_targets_only_attachable_kinds():
    binding = _binding(target_kind='seat')
    assert binding.binding_sha256
    # N:M — many bindings may share a target or an instance
    second = _binding(
        binding_id='bind-seat-2',
        actuator_instance_id='inst-bst-2',
        target_entity_id='seat-front',
    )
    assert second.target_entity_id == binding.target_entity_id


def test_processing_is_not_a_sub_crossover_copy():
    profile = _processing()
    # tactile-specific fields exist independently
    assert profile.low_pass.frequency_hz == 40.0
    assert profile.polarity == 'normal'
    assert profile.delay_ms == 4.0


def test_accelerometer_requires_axis_and_unit():
    with pytest.raises(ValueError, match='axis and unit'):
        TactileMeasurement(
            measurement_id='m1',
            binding_id='bind-seat-1',
            method='accelerometer',
            measured_at_utc='2026-09-23T03:00:00+00:00',
        )
    ok = TactileMeasurement(
        measurement_id='m2',
        binding_id='bind-seat-1',
        method='accelerometer',
        measured_at_utc='2026-09-23T03:00:00+00:00',
        axis='z',
        value=0.4,
        unit='g',
        sensor_ref='accel-usb-1',
        calibration_ref='cal-2026-08',
    )
    assert ok.axis == 'z'


def test_electrical_check_bounded():
    checks = check_tactile_electrical(
        actuator=_actuator(),
        amplifier_min_load_ohm=4.0,
        amplifier_max_power_w=80.0,
    )
    statuses = {c.check: c.status for c in checks}
    assert statuses['impedance_compatibility'] == 'PASS'
    assert statuses['power_headroom'] == 'PASS'

    bad = check_tactile_electrical(
        actuator=_actuator(impedance_ohm=2.0),
        amplifier_min_load_ohm=4.0,
        amplifier_max_power_w=None,
    )
    statuses = {c.check: c.status for c in bad}
    assert statuses['impedance_compatibility'] == 'FAIL'
    assert statuses['power_headroom'] == 'UNKNOWN'


def test_system_evaluation_target_kinds_and_capability():
    measurements = (
        TactileMeasurement(
            measurement_id='m1',
            binding_id='bind-seat-1',
            method='accelerometer',
            measured_at_utc='2026-09-23T03:00:00+00:00',
            axis='z',
            value=0.4,
            unit='g',
            sensor_ref='accel-usb-1',
        ),
    )
    evaluation = evaluate_tactile_system(
        bindings=(_binding(),),
        processing=(_processing(),),
        measurements=measurements,
        entity_kinds={'seat-front': 'seat'},
    )
    assert evaluation.acoustic_solver_status == 'NOT_APPLICABLE'
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['attachment_target'] == 'PASS'
    assert checks['processing_source_recorded'] == 'PASS'
    assert checks['capability_state'] == 'PASS'
    assert evaluation.evaluation_id.startswith('tse-')


def test_system_evaluation_rejects_non_attachable_target():
    evaluation = evaluate_tactile_system(
        bindings=(
            _binding(
                binding_id='bad-bind',
                target_entity_id='speaker-c',
                target_kind='other',
            ),
        ),
        entity_kinds={'speaker-c': 'speaker'},
    )
    checks = {c.check: c.status for c in evaluation.checks}
    assert checks['attachment_target'] == 'FAIL'
    assert checks['capability_state'] == 'UNKNOWN'
    assert tactile_system_status(evaluation) == 'FAIL'


def test_rattle_finding_is_separate_evidence():
    finding = RattleFinding(
        finding_id='rattle-1',
        entity_id='seat-front',
        observed_at_utc='2026-09-23T03:10:00+00:00',
        description='cupholder rattles at 32Hz sweep',
        severity='medium',
        related_binding_id='bind-seat-1',
    )
    # recorded as its own artifact — nothing folds it into acoustics
    assert finding.severity == 'medium'
