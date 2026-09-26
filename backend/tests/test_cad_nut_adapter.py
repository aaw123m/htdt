"""#1083 NUT (RFC 9271) telemetry adapter tests — fixture upsd, no daemon."""

from __future__ import annotations

import pytest

from htdt.cad_nut_adapter import (
    FixtureNUTTransport,
    NUTError,
    NUTTelemetryAdapter,
    parse_list_var,
)
from htdt.cad_power_thermal_telemetry import (
    build_power_thermal_telemetry_session,
)


NOW = '2026-09-25T00:00:00+00:00'

UPS_VARS = {
    'device.mfr': 'Example Power',
    'device.model': 'EX-1500',
    'ups.status': 'OL',
    'ups.load': '27',
    'ups.realpower': '320.0',
    'ups.power': '480.0',
    'output.voltage': '120.1',
    'input.voltage': '119.8',
    'battery.charge': '100',
    'battery.runtime': '1840',
    'driver.name': 'usbhid-ups',
}


def _adapter(vars_=None, **kw):
    transport = FixtureNUTTransport(
        {'ups1': ('Main UPS', dict(vars_ if vars_ is not None else UPS_VARS))},
        **kw,
    )
    return NUTTelemetryAdapter(transport), transport


def test_list_devices():
    adapter, _ = _adapter()
    assert adapter.list_devices() == (('ups1', 'Main UPS'),)


def test_dump_device_hashes_raw_variables():
    adapter, _ = _adapter()
    record = adapter.dump_device('ups1')
    assert record.ups_name == 'ups1'
    assert record.variables['battery.charge'] == '100'
    assert record.driver == 'usbhid-ups'
    assert len(record.raw_sha256) == 64
    assert len(record.record_sha256) == 64


def test_observe_produces_power_battery_evidence():
    adapter, _ = _adapter()
    records, powers, events = adapter.observe(
        session_id='sess-1', observed_at_utc=NOW
    )
    assert len(records) == 1 and len(powers) == 1
    obs = powers[0]
    assert obs.subject_kind == 'ups'
    assert obs.instrument_source == 'ups_reported'
    assert obs.observed_state == 'active'
    assert obs.real_power_w == 320.0
    assert obs.apparent_power_va == 480.0
    assert obs.voltage_v == 120.1
    assert obs.battery_charge_pct == 100.0
    assert obs.battery_runtime_s == 1840.0
    assert obs.provenance[0].source_sha256 == records[0].raw_sha256
    # Nominal OL status produces no event.
    assert events == ()


def test_on_battery_status_yields_event_and_state_label():
    vars_ = dict(UPS_VARS)
    vars_['ups.status'] = 'OB LB'
    adapter, _ = _adapter(vars_)
    _, powers, events = adapter.observe(
        session_id='sess-1', observed_at_utc=NOW
    )
    obs = powers[0]
    assert obs.observed_state == 'vendor_defined'
    assert obs.state_label == 'OB LB'
    kinds = {event.kind for event in events}
    assert kinds == {'ups_transfer', 'vendor_defined'}
    labels = {event.event_label for event in events}
    assert 'on_battery' in labels and 'low_battery' in labels


def test_pdu_branch_subject_stays_pdu_sourced():
    adapter, _ = _adapter()
    _, powers, _ = adapter.observe(
        session_id='sess-1',
        observed_at_utc=NOW,
        subject_kind='pdu_branch',
        subject_ref_template='pdu-branch:{name}',
    )
    assert powers[0].subject_kind == 'pdu_branch'
    assert powers[0].instrument_source == 'pdu_reported'
    assert powers[0].subject_id == 'pdu-branch:ups1'


def test_errors_surface_exactly():
    adapter, _ = _adapter()
    with pytest.raises(NUTError) as error:
        adapter.dump_device('ghost')
    assert error.value.kind == 'protocol_error'

    down, _ = _adapter(fail=True)
    with pytest.raises(NUTError) as error2:
        down.list_devices()
    assert error2.value.kind == 'not_connected'


def test_parse_list_var_requires_framing():
    with pytest.raises(NUTError):
        parse_list_var(('VAR ups1 x.y "1"',), 'ups1')


def test_observations_compose_into_telemetry_session():
    adapter, _ = _adapter()
    _, powers, events = adapter.observe(
        session_id='sess-1', observed_at_utc=NOW
    )
    session = build_power_thermal_telemetry_session(
        session_id='sess-1',
        power_observations=powers,
        power_events=events,
    )
    assert session.semantic_sha256
    assert len(session.power_observations) == 1
