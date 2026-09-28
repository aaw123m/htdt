"""HDMI-CEC event-observation adapter (#1084).

Bounded, read-only normalization of HDMI CEC bus traffic sourced from a
libCEC instance. CEC evidence answers "what did the bus observe?" — power
rallies, source routing changes, standby storms — it is never treated as
handshake truth: a ``REPORT_POWER_STATUS`` frame is a claim by one device,
and device *truth* still comes from that device's own adapter read-back
(e.g. PJLink #1082, vendor AVR APIs).

Authority boundary (per the issue contract):

- observe-only by default: the transport seam delivers raw frames;
  transmit (``tx``) is a separate explicit grant and never required for
  evidence collection;
- every event keeps raw bytes, receive time, bus endpoint and the exact
  normalized interpretation — unmapped opcodes stay ``unknown`` with the
  raw payload intact, never guessed;
- libCEC is GPL — integration stays out-of-process (adapter consumes a
  frame stream, e.g. ``cec-client`` traffic output); no libCEC symbols are
  linked into HTDT;
- no discovery/EDID magic: endpoints are explicit bindings, and address
  claims from the bus are recorded, not trusted.

:class:`FixtureCECTransport` feeds deterministic frame lines so the whole
normalization path is exercised in CI without a CEC dongle.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash, canonicalize_payload


CEC_ADAPTER_ID = 'htdt-cec'
CEC_ADAPTER_VERSION = '1'
#: HDMI CEC bus profile — opcode names follow CEC 1.4 / libCEC traffic log
#: conventions; the subset below is the bounded normalization surface.
CEC_SPEC_PROFILE = 'hdmi-cec-1.4-libcec-traffic'

CECEventKind = Literal[
    'active_source',
    'routing_change',
    'inactive_source',
    'standby',
    'power_status_report',
    'physical_address',
    'cec_version',
    'device_vendor_id',
    'osd_name',
    'user_control',
    'system_audio_mode',
    'vendor_command',
    'unknown',
]

#: Opcode byte -> (event kind, human label). Only this subset normalizes;
#: anything else records as ``unknown`` with raw bytes.
_OPCODE_MAP: dict[int, tuple[CECEventKind, str]] = {
    0x82: ('active_source', 'ACTIVE_SOURCE'),
    0x80: ('routing_change', 'ROUTING_CHANGE'),
    0x9D: ('inactive_source', 'INACTIVE_SOURCE'),
    0x36: ('standby', 'STANDBY'),
    0x90: ('power_status_report', 'REPORT_POWER_STATUS'),
    0x8F: ('power_status_report', 'GIVE_DEVICE_POWER_STATUS'),
    0x84: ('physical_address', 'REPORT_PHYSICAL_ADDRESS'),
    0x85: ('active_source', 'REQUEST_ACTIVE_SOURCE'),
    0x9E: ('cec_version', 'CEC_VERSION'),
    0x9F: ('cec_version', 'GET_CEC_VERSION'),
    0x87: ('device_vendor_id', 'DEVICE_VENDOR_ID'),
    0x89: ('vendor_command', 'VENDOR_COMMAND'),
    0x47: ('osd_name', 'SET_OSD_NAME'),
    0x46: ('osd_name', 'GIVE_OSD_NAME'),
    0x44: ('user_control', 'USER_CONTROL_PRESSED'),
    0x45: ('user_control', 'USER_CONTROL_RELEASED'),
    0x70: ('system_audio_mode', 'SYSTEM_AUDIO_MODE_STATUS'),
    0x72: ('system_audio_mode', 'SET_SYSTEM_AUDIO_MODE'),
    0x7A: ('system_audio_mode', 'SYSTEM_AUDIO_MODE_REQUEST'),
}

#: REPORT_POWER_STATUS operand values.
_POWER_STATUS = {
    0x00: 'on',
    0x01: 'standby',
    0x02: 'standby_to_on',
    0x03: 'on_to_standby',
}

#: Coarse logical-address labels; the bus's own claims are recorded,
#: never trusted beyond the label.
_ADDRESS_LABEL = {
    0: 'tv',
    5: 'audio_system',
    15: 'broadcast',
}


class CECError(RuntimeError):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f'{kind}: {detail}')
        self.kind = kind
        self.detail = detail






def address_label(address: int) -> str:
    return _ADDRESS_LABEL.get(address, f'device_{address}')


def parse_cec_frame(
    line: str,
) -> tuple[int, int, int, bytes]:
    """Parse a libCEC-style raw frame ``<src><dst>:<b0>:<b1>...``.

    Accepts ``>>``/``<<`` prefixes and whitespace; returns
    ``(source, destination, opcode, payload)``. Raises ``CECError``
    ``malformed_frame`` on deviation — damaged frames are never guessed.
    """
    text = line.strip()
    for prefix in ('>>', '<<'):
        if text.startswith(prefix):
            text = text[2:].strip()
    try:
        bytes_ = bytes.fromhex(text.replace(':', ' '))
    except ValueError as error:
        raise CECError('malformed_frame', f'{line!r}: {error}') from error
    if not bytes_:
        raise CECError('malformed_frame', f'{line!r}: empty')
    header = bytes_[0]
    source, destination = header >> 4, header & 0x0F
    opcode = bytes_[1] if len(bytes_) > 1 else 0xFF
    return source, destination, opcode, bytes_[2:]


def normalize_cec_frame(
    source: int, destination: int, opcode: int, payload: bytes
) -> tuple[CECEventKind, str, dict[str, Any]]:
    """Map one frame to (kind, label, normalized detail).

    Unknown opcodes keep ``unknown`` + raw payload — bounded by design.
    """
    kind, label = _OPCODE_MAP.get(opcode, ('unknown', f'OPCODE_0x{opcode:02X}'))
    detail: dict[str, Any] = {
        'opcode': f'0x{opcode:02X}',
        'source': address_label(source),
        'destination': address_label(destination),
        'payload_hex': payload.hex(),
    }
    if opcode == 0x82 and len(payload) >= 2:
        detail['active_physical_address'] = (
            f'{payload[0] >> 4}.{payload[0] & 0xF}.'
            f'{payload[1] >> 4}.{payload[1] & 0xF}'
        )
    elif opcode == 0x80 and len(payload) >= 4:
        detail['from_physical_address'] = (
            f'{payload[0] >> 4}.{payload[0] & 0xF}.'
            f'{payload[1] >> 4}.{payload[1] & 0xF}'
        )
        detail['to_physical_address'] = (
            f'{payload[2] >> 4}.{payload[2] & 0xF}.'
            f'{payload[3] >> 4}.{payload[3] & 0xF}'
        )
    elif opcode == 0x90 and payload:
        detail['reported_power_status'] = _POWER_STATUS.get(
            payload[0], 'unknown'
        )
        detail['reported_power_status_raw'] = f'0x{payload[0]:02X}'
    elif opcode == 0x84 and len(payload) >= 3:
        detail['physical_address'] = (
            f'{payload[0] >> 4}.{payload[0] & 0xF}.'
            f'{payload[1] >> 4}.{payload[1] & 0xF}'
        )
        detail['device_type'] = payload[2]
    elif opcode in (0x9E, 0x9F) and payload:
        detail['cec_version'] = f'0x{payload[0]:02X}'
    elif opcode == 0x87 and len(payload) >= 3:
        detail['vendor_id'] = f'{payload[0]:02X}{payload[1]:02X}{payload[2]:02X}'
    elif opcode == 0x47 and payload:
        try:
            detail['osd_name'] = payload.decode('ascii', errors='replace')
        except ValueError:
            detail['osd_name'] = None
    elif opcode in (0x44, 0x45) and payload:
        detail['ui_command'] = f'0x{payload[0]:02X}'
    return kind, label, detail


class CECTransport(Protocol):
    """Injectable seam to a frame source (e.g. libCEC traffic output).

    ``poll_frame`` returns one raw frame line or ``None``; ``tx`` sends a
    raw frame — a separate explicit grant that evidence-only adapters do
    not need. Implementations that cannot transmit raise ``CECError``
    ``transmit_unavailable``.
    """

    def poll_frame(self) -> str | None: ...

    def tx(self, raw_hex: str) -> None: ...

    def close(self) -> None: ...


class CECEventRecord(BaseModel):
    """One observed CEC bus event — raw + normalized, self-hashed."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    bus_endpoint: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    received_at_utc: str = Field(min_length=1)
    raw_text: str = Field(min_length=1)
    source_address: int | None = Field(default=None, ge=0, le=15)
    destination_address: int | None = Field(default=None, ge=0, le=15)
    opcode: int | None = Field(default=None, ge=0, le=255)
    event_kind: CECEventKind = 'unknown'
    event_label: str | None = None
    normalized: dict[str, Any] | None = None
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_record(self) -> 'CECEventRecord':
        if self.record_sha256 != _hash(self.identity_payload()):
            raise ValueError('CECEventRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'event_id': self.event_id,
            'bus_endpoint': self.bus_endpoint,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'received_at_utc': self.received_at_utc,
            'raw_text': self.raw_text,
            'source_address': self.source_address,
            'destination_address': self.destination_address,
            'opcode': self.opcode,
            'event_kind': self.event_kind,
            'event_label': self.event_label,
            'normalized': self.normalized,
        }


class CECObservationAdapter:
    """Collects CEC bus frames into :class:`CECEventRecord` evidence.

    Deliberately not an ``EquipmentDeviceAdapter``: CEC observation is a
    bus-level evidence stream, not a per-device control contract.
    """

    adapter_id = CEC_ADAPTER_ID
    adapter_version = CEC_ADAPTER_VERSION

    def __init__(self, transports: dict[str, CECTransport]) -> None:
        #: bus endpoint -> transport (e.g. 'cec:/dev/cec0', 'fixture:0')
        self._transports = dict(transports)

    def discover_targets(self) -> tuple[str, ...]:
        return tuple(sorted(self._transports))

    def collect(
        self,
        bus_endpoint: str,
        *,
        received_at_utc: str,
        max_frames: int = 256,
    ) -> tuple[CECEventRecord, ...]:
        transport = self._transports.get(bus_endpoint)
        if transport is None:
            raise CECError(
                'unbound_endpoint',
                f'bus endpoint {bus_endpoint} is not bound to this adapter',
            )
        records: list[CECEventRecord] = []
        for _ in range(max_frames):
            line = transport.poll_frame()
            if line is None:
                break
            source: int | None = None
            destination: int | None = None
            opcode: int | None = None
            kind: CECEventKind = 'unknown'
            label: str | None = None
            normalized: dict[str, Any] | None = None
            try:
                source, destination, opcode, payload = parse_cec_frame(line)
            except CECError:
                label = 'malformed_frame'
            else:
                kind, label, normalized = normalize_cec_frame(
                    source, destination, opcode, payload
                )
            payload_dict: dict[str, Any] = {
                'event_id': str(uuid4()),
                'bus_endpoint': bus_endpoint,
                'adapter_id': self.adapter_id,
                'adapter_version': self.adapter_version,
                'received_at_utc': received_at_utc,
                'raw_text': line.strip(),
                'source_address': source,
                'destination_address': destination,
                'opcode': opcode,
                'event_kind': kind,
                'event_label': label,
                'normalized': normalized,
            }
            provisional = CECEventRecord.model_construct(**canonicalize_payload(CECEventRecord, dict(
                **payload_dict, record_sha256='0' * 64
            )))
            records.append(
                CECEventRecord(
                    **payload_dict,
                    record_sha256=_hash(provisional.identity_payload()),
                )
            )
        return tuple(records)


class FixtureCECTransport:
    """Deterministic FIFO of raw CEC frames for CI fixtures."""

    def __init__(
        self,
        frames: tuple[str, ...] = (),
        *,
        allow_tx: bool = False,
        fail: bool = False,
    ) -> None:
        self._frames = list(frames)
        self._allow_tx = allow_tx
        self._fail = fail
        self.transmitted: list[str] = []

    def poll_frame(self) -> str | None:
        if self._fail:
            raise CECError('not_connected', 'fixture transport is down')
        if self._frames:
            return self._frames.pop(0)
        return None

    def tx(self, raw_hex: str) -> None:
        if not self._allow_tx:
            raise CECError(
                'transmit_unavailable',
                'fixture transport is observe-only',
            )
        self.transmitted.append(raw_hex)

    def close(self) -> None:
        pass


__all__ = [
    'CEC_ADAPTER_ID',
    'CEC_ADAPTER_VERSION',
    'CEC_SPEC_PROFILE',
    'CECEventKind',
    'CECEventRecord',
    'CECError',
    'CECObservationAdapter',
    'CECTransport',
    'FixtureCECTransport',
    'address_label',
    'normalize_cec_frame',
    'parse_cec_frame',
]
