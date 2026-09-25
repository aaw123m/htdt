"""Operating power & thermal telemetry tests (#1049)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_power_thermal_telemetry import (
    OperatingPowerObservation,
    PowerEventObservation,
    PowerThermalTelemetrySession,
    ThermalObservation,
    build_power_thermal_telemetry_session,
    evaluate_telemetry_readiness,
)


def _provenance(digit: str = '2'):
    return (
        EquipmentDataProvenance(
            evidence_kind='measured',
            source_name='rack PDU meter',
            source_version='1',
            source_reference='telemetry run 4',
            source_sha256=digit * 64,
        ),
    )


def _power(**overrides):
    kwargs = dict(
        observation_id='po-1',
        session_id='sess-1',
        subject_id='avr-1',
        subject_kind='equipment',
        observed_state='playback',
        instrument_id='pdu-branch-3',
        instrument_source='pdu_reported',
        observed_at='2026-09-25T20:00:00Z',
        interval_s=60.0,
        voltage_v=120.1,
        current_a=1.2,
        real_power_w=130.0,
        apparent_power_va=144.0,
        energy_wh=2200.0,
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return OperatingPowerObservation(**kwargs)


def _thermal(**overrides):
    kwargs = dict(
        observation_id='to-1',
        session_id='sess-1',
        sensor_id='sensor-rack-exhaust',
        location='rack_exhaust',
        subject_id='rack-1',
        observed_at='2026-09-25T20:00:00Z',
        temperature_c=33.5,
        provenance=_provenance('4'),
    )
    kwargs.update(overrides)
    return ThermalObservation(**kwargs)


def test_observation_immutability_and_wva_consistency():
    observation = _power()
    with pytest.raises(ValidationError, match='inconsistent'):
        _power(real_power_w=130.0, apparent_power_va=100.0,
               power_factor=0.5)
    # pf consistent with W/VA passes
    ok = _power(power_factor=0.9)
    assert ok.power_factor == 0.9


def test_vendor_defined_states_and_events_need_labels():
    with pytest.raises(ValidationError, match='state_label'):
        _power(observed_state='vendor_defined')
    event = PowerEventObservation(
        event_id='ev-1',
        session_id='sess-1',
        subject_id='ups-1',
        kind='ups_transfer',
        observed_at='2026-09-25T20:01:00Z',
        detail='transferred to battery for 2 s',
        provenance=_provenance('5'),
    )
    assert event.kind == 'ups_transfer'
    with pytest.raises(ValidationError, match='event_label'):
        PowerEventObservation(
            event_id='ev-2', session_id='sess-1', subject_id='ups-1',
            kind='vendor_defined', observed_at='t0',
        )


def test_session_bundles_matching_observations():
    session = build_power_thermal_telemetry_session(
        session_id='sess-1',
        power_observations=(_power(),),
        thermal_observations=(_thermal(),),
        power_events=(),
        provenance=_provenance('6'),
    )
    assert session.semantic_sha256
    payload = session.model_dump(mode='python')
    payload['version'] = '2'
    with pytest.raises(ValidationError, match='semantic hash'):
        PowerThermalTelemetrySession(**payload)
    # foreign session ids cannot be smuggled into a bundle
    with pytest.raises(ValidationError, match='match the bundle'):
        build_power_thermal_telemetry_session(
            session_id='sess-1',
            power_observations=(_power(session_id='other'),),
        )


def test_readiness_passes_on_complete_session():
    session = build_power_thermal_telemetry_session(
        session_id='sess-1',
        power_observations=(_power(),),
        thermal_observations=(_thermal(),),
    )
    report = evaluate_telemetry_readiness(session=session)
    checks = {c.check: c.status for c in report.checks}
    assert checks['observations_present'] == 'PASS'
    assert checks['states_recorded'] == 'PASS'
    assert checks['instruments_recorded'] == 'PASS'
    assert checks['thermal_locations_bound'] == 'PASS'
    assert checks['distinct_from_planning'] == 'PASS'


def test_unknown_state_and_location_stay_unknown():
    session = build_power_thermal_telemetry_session(
        session_id='sess-1',
        power_observations=(
            _power(observed_state='unknown',
                   instrument_source='unknown'),
        ),
        thermal_observations=(_thermal(location='unknown'),),
    )
    report = evaluate_telemetry_readiness(session=session)
    checks = {c.check: c.status for c in report.checks}
    assert checks['observations_present'] == 'PASS'
    assert checks['states_recorded'] == 'UNKNOWN'
    assert checks['instruments_recorded'] == 'UNKNOWN'
    assert checks['thermal_locations_bound'] == 'UNKNOWN'


def test_empty_session_unknown():
    session = build_power_thermal_telemetry_session(session_id='sess-e')
    report = evaluate_telemetry_readiness(session=session)
    checks = {c.check: c.status for c in report.checks}
    assert checks['observations_present'] == 'UNKNOWN'


def test_thermal_location_semantics():
    with pytest.raises(ValidationError, match='plausible'):
        _thermal(temperature_c=400.0)
    with pytest.raises(ValidationError, match='label'):
        _thermal(location='custom')
    ok = _thermal(location='custom', location_label='amp heatsink fin')
    assert ok.location == 'custom'
