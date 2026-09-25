"""#1084 CEC event-observation adapter tests — fixture frames, no dongle."""

from __future__ import annotations

import pytest

from htdt.cad_cec_adapter import (
    CECObservationAdapter,
    CECError,
    FixtureCECTransport,
    normalize_cec_frame,
    parse_cec_frame,
)


NOW = '2026-09-25T00:00:00+00:00'

# TV broadcasting ACTIVE_SOURCE for physical address 1.0.0.0
FRAMES = (
    '0f:82:10:00',
    '0f:80:22:00:10:00',
    '50:90:00',
    'garbage line',
)


def _adapter(frames=FRAMES, **kw):
    transport = FixtureCECTransport(frames, **kw)
    return CECObservationAdapter({'fixture:bus0': transport}), transport


def test_parse_frame_header_and_payload():
    src, dst, opcode, payload = parse_cec_frame('0f:82:10:00')
    assert src == 0 and dst == 15 and opcode == 0x82
    assert payload == bytes([0x10, 0x00])
    src2, dst2, opcode2, _ = parse_cec_frame('>> 50:90:00')
    assert src2 == 5 and dst2 == 0 and opcode2 == 0x90
    with pytest.raises(CECError):
        parse_cec_frame('not hex')


def test_normalize_known_opcodes():
    kind, label, detail = normalize_cec_frame(0, 15, 0x82, bytes([0x10, 0x00]))
    assert kind == 'active_source'
    assert label == 'ACTIVE_SOURCE'
    assert detail['active_physical_address'] == '1.0.0.0'
    kind2, _, detail2 = normalize_cec_frame(5, 0, 0x90, bytes([0x01]))
    assert kind2 == 'power_status_report'
    assert detail2['reported_power_status'] == 'standby'
    kind3, label3, _ = normalize_cec_frame(4, 15, 0xC0, b'')
    assert kind3 == 'unknown'
    assert label3 == 'OPCODE_0xC0'


def test_collect_keeps_raw_and_normalized():
    adapter, _ = _adapter()
    records = adapter.collect('fixture:bus0', received_at_utc=NOW)
    assert len(records) == 4
    active = records[0]
    assert active.event_kind == 'active_source'
    assert active.raw_text == '0f:82:10:00'
    assert active.source_address == 0
    assert active.destination_address == 15
    assert active.normalized['source'] == 'tv'
    assert active.normalized['destination'] == 'broadcast'
    standby = records[2]
    assert standby.event_kind == 'power_status_report'
    assert standby.normalized['reported_power_status'] == 'on'
    malformed = records[3]
    assert malformed.event_kind == 'unknown'
    assert malformed.event_label == 'malformed_frame'
    assert malformed.raw_text == 'garbage line'
    assert all(len(r.record_sha256) == 64 for r in records)


def test_unbound_endpoint_raises():
    adapter, _ = _adapter()
    with pytest.raises(CECError):
        adapter.collect('fixture:elsewhere', received_at_utc=NOW)


def test_observe_only_transport_rejects_tx():
    _, transport = _adapter()
    with pytest.raises(CECError) as error:
        transport.tx('0f:36')
    assert error.value.kind == 'transmit_unavailable'


def test_event_record_is_frozen_evidence():
    adapter, _ = _adapter()
    records = adapter.collect('fixture:bus0', received_at_utc=NOW)
    first = records[0]
    with pytest.raises(Exception):
        first.raw_text = 'mutated'
