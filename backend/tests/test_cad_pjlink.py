"""#1082 PJLink adapter tests — fixture transport, no hardware."""

from __future__ import annotations

import pytest

from htdt.cad_equipment_device import (
    DeviceActionRequest,
    DeviceFrameworkError,
    build_device_target_binding,
    verify_action_outcome,
)
from htdt.cad_pjlink import (
    PJLinkDeviceAdapter,
    PJLinkError,
    FixturePJLinkTransport,
    normalize_error_status,
    normalize_input,
    normalize_power,
    parse_response_line,
    pjlink_auth_digest,
)


NOW = '2026-09-25T00:00:00+00:00'

FULL_RESPONSES = {
    'CLSS': '2',
    'INF1': 'JBMIA-Brand',
    'INF2': 'FX-500',
    'INFO': 'Theater PJ',
    'SNUM': 'SN0000',
    'SVER': '1.2.3',
    'POWR': '0001',
    'INPT': '31',
    'AVMT': '30',
    'ERST': '000000',
    'LAMP': '120 1',
    'FILT': '300',
    'INST': '31 32 11',
    'IRES': '3840x2160',
    'RRES': '3840x2160',
    'FRZ': '0',
}


def _binding(endpoint='pjlink://192.0.2.10:4352', **over):
    payload = {
        'document_id': 'doc-1',
        'installed_equipment_ref': 'inst-pj-1',
        'device_kind': 'projector',
        'adapter_id': 'htdt-pjlink',
        'adapter_version': '1',
        'endpoint': endpoint,
        'created_at_utc': NOW,
    }
    payload.update(over)
    return build_device_target_binding(**payload)


def _adapter(responses=None, **kw):
    transport = FixturePJLinkTransport(
        responses=responses if responses is not None else dict(FULL_RESPONSES),
        **kw,
    )
    return PJLinkDeviceAdapter({'pjlink://192.0.2.10:4352': transport}), transport


def test_frame_codec_roundtrip():
    assert parse_response_line('%1POWR=0001') == ('1', 'POWR', '0001')
    assert parse_response_line('%2ERST=000000') == ('2', 'ERST', '000000')
    with pytest.raises(PJLinkError):
        parse_response_line('garbage')


def test_normalizers():
    assert normalize_power('0001') == 'on'
    assert normalize_power('0003') == 'warming'
    assert normalize_input('31') == 'digital_1'
    assert normalize_input('52') == 'network_2'
    assert normalize_input('zz') is None
    assert normalize_error_status('000000') == {
        'fan': 'ok', 'lamp': 'ok', 'temperature': 'ok',
        'cover': 'ok', 'filter': 'ok', 'other': 'ok',
    }
    assert normalize_error_status('020000')['lamp'] == 'error'


def test_auth_digest_documented_scheme():
    import hashlib
    assert pjlink_auth_digest('SEED', 'pw') == hashlib.md5(b'SEEDpw').hexdigest()


def test_discovery_and_binding_is_explicit():
    adapter, _ = _adapter()
    assert adapter.discover_targets() == ('pjlink://192.0.2.10:4352',)
    other = _binding(endpoint='pjlink://192.0.2.99:4352')
    with pytest.raises(DeviceFrameworkError):
        adapter.observe_state(other)


def test_probe_reports_command_level_capability():
    adapter, _ = _adapter()
    snapshot = adapter.probe_capabilities(_binding())
    assert snapshot.capability_state('powr') == 'supported'
    assert snapshot.capability_state('clss') == 'supported'
    assert snapshot.capability_state('power_mutation') == 'supported'
    # Class-1-only fixture: drop CLSS to simulate
    adapter2, _ = _adapter(responses={
        k: v for k, v in FULL_RESPONSES.items() if k != 'CLSS'
    })
    snap2 = adapter2.probe_capabilities(_binding())
    assert snap2.capability_state('clss') == 'unsupported'


def test_observe_state_normalizes_documented_fields():
    adapter, _ = _adapter()
    state = adapter.observe_state(_binding())
    assert state.field('power').value == 'on'
    assert state.field('input').value == 'digital_1'
    assert state.field('av_mute').value == 'av_unmuted'
    assert state.field('manufacturer').value == 'JBMIA-Brand'
    assert state.field('model').value == 'FX-500'
    assert state.field('software_version').value == '1.2.3'
    assert state.firmware_version == '1.2.3'
    assert state.field('error_status').value == 'ok'
    assert 'digital_1' in state.field('available_inputs').value
    assert state.raw_source_sha256 is not None


def test_unavailable_command_is_not_offline():
    """ERR3 during standby stays `unavailable`, never normalized to
    offline (#1082 §7)."""
    adapter, _ = _adapter(responses={'POWR': 'ERR3'})
    state = adapter.observe_state(_binding())
    assert state.field('power').state == 'unknown'
    assert any('POWR: unavailable' in lim for lim in state.limitations)


def test_apply_requires_confirmation_and_readback_verifies():
    adapter, transport = _adapter()
    binding = _binding()
    request = DeviceActionRequest(
        request=(('power', 'standby'), ('input', 'digital_2'))
    )
    action = adapter.plan_action(binding, request)
    assert action is not None
    assert action.unsupported == ()
    assert [c.command for c in action.commands] == ['POWR', 'INPT']

    ack = adapter.apply_action(binding, action, operator_confirmed=False)
    assert ack is not None and not ack.accepted
    assert verify_action_outcome(action, ack, None) == 'not_accepted'

    ack = adapter.apply_action(binding, action, operator_confirmed=True)
    assert ack is not None and ack.accepted
    # Fixture mutates responses on set; INPT=32 -> digital_2.
    assert transport._responses['INPT'] == '32'
    assert transport._responses['POWR'] == '0000'
    readback = adapter.read_back(binding)
    assert verify_action_outcome(action, ack, readback) == 'verified'


def test_apply_error_is_ack_rejection_not_silent():
    adapter, transport = _adapter()
    binding = _binding()
    original = transport.exchange

    def guarded(request: str) -> str:
        if request.strip().endswith('=?'):
            return original(request)
        return '%1POWR=ERR4'

    transport.exchange = guarded  # type: ignore[method-assign]
    request = DeviceActionRequest(request=(('power', 'standby'),))
    action = adapter.plan_action(binding, request)
    ack = adapter.apply_action(binding, action, operator_confirmed=True)
    assert ack is not None and not ack.accepted
    assert 'device_failure' in (ack.detail or '')


def test_notifications_are_raw_plus_normalized_events():
    adapter, _ = _adapter(
        notifications=('%2POWR=0002', '%2INPT=32', 'garbage'),
    )
    records = adapter.collect_notifications(
        _binding(), received_at_utc=NOW
    )
    assert len(records) == 3
    assert records[0].command == 'POWR'
    assert records[0].normalized_value == 'cooling'
    assert records[1].normalized_value == 'digital_2'
    assert records[2].command is None  # malformed kept raw
    assert all(r.record_sha256 for r in records)
