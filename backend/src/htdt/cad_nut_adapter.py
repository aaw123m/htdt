"""NUT (Network UPS Tools) telemetry adapter (#1083).

Read-only power/battery/event evidence for UPS units and NUT-managed PDU
outlets, built on the NUT network protocol as documented by RFC 9271
(tcp/3493 line protocol: ``GET VAR``, ``LIST VAR``, ``LIST UPS``,
``GET UPSDESC``, ``VER``, ``NETVER``).

Authority boundary (per the issue contract):

- read-only: the adapter issues only ``GET``/``LIST``/``VER`` commands —
  ``SETRHS``/``INSTCMD``/login commands are never materialized;
- every observation keeps the raw variable dump (hashed) plus exact
  provenance — a NUT-reported number is ``instrument_source='ups_reported'``
  (or ``pdu_reported`` for outlet subjects), never silently relabeled as a
  calibrated meter;
- unknown variables and unmapped status tokens stay raw in
  ``state_label``/``detail`` — never dropped, never guessed;
- battery evidence (``battery.charge``/``battery.runtime``) lands on the
  observation's battery fields, not in prose;
- status tokens map to :class:`PowerEventObservation` evidence records —
  they describe what the UPS reported, not a diagnosis of the mains.

The transport is a narrow injectable seam (:class:`NUTTransport`) so the
whole lifecycle is exercised by :class:`FixtureNUTTransport` in CI without
a live ``upsd``; a real socket transport lands with the hardware slice.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_power_thermal_telemetry import (
    OperatingPowerObservation,
    PowerEventObservation,
)


NUT_ADAPTER_ID = 'htdt-nut'
NUT_ADAPTER_VERSION = '1'
#: Protocol profile this adapter normalizes against.
NUT_PROTOCOL_PROFILE = 'rfc9271-nut'

#: NUT status tokens -> (event kind, human label). ``OL`` is the nominal
#: state and produces no event; everything else is preserved evidence.
_STATUS_EVENT_MAP: dict[str, tuple[str, str]] = {
    'OB': ('ups_transfer', 'on_battery'),
    'LB': ('vendor_defined', 'low_battery'),
    'RB': ('vendor_defined', 'replace_battery'),
    'OVER': ('vendor_defined', 'overload'),
    'TRIM': ('vendor_defined', 'trim'),
    'BOOST': ('vendor_defined', 'boost'),
    'BYPASS': ('vendor_defined', 'bypass'),
    'CAL': ('vendor_defined', 'calibration'),
    'OFF': ('vendor_defined', 'off'),
    'FSD': ('vendor_defined', 'forced_shutdown'),
    'ALARM': ('vendor_defined', 'alarm'),
    'WAIT': ('vendor_defined', 'wait'),
    'NOCOMM': ('vendor_defined', 'no_communication'),
    'SD': ('vendor_defined', 'shutdown'),
    'TEST': ('vendor_defined', 'test'),
}

#: Tokens that keep the load path fed — everything else is a
#: vendor-defined/abnormal state carried via ``state_label``.
_STATE_MAP = {
    'OL': 'active',
    'OFF': 'disconnected',
}

NutErrorKind = Literal[
    'protocol_error',
    'device_unavailable',
    'not_connected',
    'malformed_response',
]


class NUTError(RuntimeError):
    def __init__(self, kind: NutErrorKind, detail: str) -> None:
        super().__init__(f'{kind}: {detail}')
        self.kind = kind
        self.detail = detail


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict[str, Any]) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def parse_quoted(text: str) -> str:
    """Strip the RFC 9271 double-quoted value wrapper."""
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
        return stripped[1:-1]
    return stripped


class NUTTransport(Protocol):
    """Injectable wire seam — a real implementation owns tcp/3493."""

    def exchange(self, request: str) -> tuple[str, ...]:
        """Send one request line; return all response lines verbatim."""
        ...

    def close(self) -> None: ...


class NUTDeviceRecord(BaseModel):
    """One UPS/PDU device's raw NUT dump, hashed — the audit anchor."""

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
    ups_name: str = Field(min_length=1)
    description: str | None = None
    driver: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)
    server_version: str | None = None
    protocol_version: str | None = None
    raw_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_record(self) -> 'NUTDeviceRecord':
        if self.raw_sha256 != sha256(
            _canonical(self.variables).encode('utf-8')
        ).hexdigest():
            raise ValueError('NUTDeviceRecord raw hash mismatch')
        if self.record_sha256 != _hash(self.identity_payload()):
            raise ValueError('NUTDeviceRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'record_id': self.record_id,
            'ups_name': self.ups_name,
            'description': self.description,
            'driver': self.driver,
            'variables': self.variables,
            'server_version': self.server_version,
            'protocol_version': self.protocol_version,
            'raw_sha256': self.raw_sha256,
        }


def parse_list_var(lines: tuple[str, ...], ups_name: str) -> dict[str, str]:
    """Parse a ``LIST VAR`` block into a variable map.

    Raises ``NUTError`` on protocol errors or missing BEGIN/END markers —
    a partial dump is never silently accepted.
    """
    variables: dict[str, str] = {}
    began = False
    ended = False
    for line in lines:
        text = line.strip()
        if text.startswith('ERR '):
            raise NUTError('protocol_error', text)
        if text == f'BEGIN LIST VAR {ups_name}':
            began = True
            continue
        if text == f'END LIST VAR {ups_name}':
            ended = True
            break
        if text.startswith('VAR '):
            parts = text.split(' ', 3)
            if len(parts) == 4 and parts[1] == ups_name:
                variables[parts[2]] = parse_quoted(parts[3])
    if not began:
        raise NUTError('malformed_response', 'missing BEGIN LIST VAR')
    if not ended:
        raise NUTError('malformed_response', 'missing END LIST VAR')
    return variables


def _float_value(
    variables: dict[str, str], *names: str
) -> float | None:
    for name in names:
        raw = variables.get(name)
        if raw is None:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return None


def _status_tokens(variables: dict[str, str]) -> tuple[str, ...]:
    raw = variables.get('ups.status', '')
    return tuple(token for token in raw.split() if token)


def _observed_state(tokens: tuple[str, ...]) -> tuple[str, str | None]:
    """Map status tokens to ObservedOperatingState + raw label."""
    if not tokens:
        return 'unknown', None
    for token in tokens:
        if token in _STATE_MAP:
            return _STATE_MAP[token], ' '.join(tokens)
    return 'vendor_defined', ' '.join(tokens)


def _provenance(
    record: NUTDeviceRecord, subject_ref: str
) -> tuple[EquipmentDataProvenance, ...]:
    return (
        EquipmentDataProvenance(
            evidence_kind='measured',
            source_name='nut-upsd',
            source_version=record.server_version or 'unknown',
            source_reference=f'{subject_ref} via {NUT_PROTOCOL_PROFILE}',
            source_sha256=record.raw_sha256,
        ),
    )


class NUTTelemetryAdapter:
    """Read-only RFC 9271 client producing #1049 telemetry evidence.

    ``observe`` performs ``LIST UPS``/``LIST VAR``/``VER``/``NETVER``
    against the transport and returns one :class:`NUTDeviceRecord` per UPS
    plus derived :class:`OperatingPowerObservation` /
    :class:`PowerEventObservation` records bound to ``session_id``.
    """

    adapter_id = NUT_ADAPTER_ID
    adapter_version = NUT_ADAPTER_VERSION

    def __init__(self, transport: NUTTransport) -> None:
        self._transport = transport

    def list_devices(self) -> tuple[tuple[str, str], ...]:
        """``LIST UPS`` → ``(name, description)`` pairs."""
        lines = self._transport.exchange('LIST UPS')
        devices: list[tuple[str, str]] = []
        began = False
        for line in lines:
            text = line.strip()
            if text.startswith('ERR '):
                raise NUTError('protocol_error', text)
            if text == 'BEGIN LIST UPS':
                began = True
                continue
            if text == 'END LIST UPS':
                break
            if text.startswith('UPS '):
                parts = text.split(' ', 2)
                if len(parts) == 3:
                    devices.append((parts[1], parse_quoted(parts[2])))
        if not began:
            raise NUTError('malformed_response', 'missing BEGIN LIST UPS')
        return tuple(devices)

    def server_versions(self) -> tuple[str | None, str | None]:
        """``VER`` and ``NETVER`` — daemon + protocol versions."""
        ver = self._transport.exchange('VER')
        netver = self._transport.exchange('NETVER')
        return (
            ver[0].strip() if ver else None,
            netver[0].strip() if netver else None,
        )

    def dump_device(
        self,
        ups_name: str,
        *,
        server_version: str | None = None,
        protocol_version: str | None = None,
        description: str | None = None,
    ) -> NUTDeviceRecord:
        """``LIST VAR`` for one UPS → raw hashed record."""
        lines = self._transport.exchange(f'LIST VAR {ups_name}')
        variables = parse_list_var(lines, ups_name)
        payload: dict[str, Any] = {
            'record_id': str(uuid4()),
            'ups_name': ups_name,
            'description': description,
            'driver': variables.get('driver.name'),
            'variables': variables,
            'server_version': server_version,
            'protocol_version': protocol_version,
            'raw_sha256': sha256(
                _canonical(variables).encode('utf-8')
            ).hexdigest(),
        }
        provisional = NUTDeviceRecord.model_construct(
            **payload, record_sha256='0' * 64
        )
        return NUTDeviceRecord(
            **payload, record_sha256=_hash(provisional.identity_payload())
        )

    def observe(
        self,
        *,
        session_id: str,
        subject_ref_template: str = 'ups:{name}',
        observed_at_utc: str,
        subject_kind: Literal['ups', 'pdu_branch', 'equipment'] = 'ups',
        ups_names: tuple[str, ...] | None = None,
    ) -> tuple[
        tuple[NUTDeviceRecord, ...],
        tuple[OperatingPowerObservation, ...],
        tuple[PowerEventObservation, ...],
    ]:
        """Collect power/battery/event evidence for each listed UPS.

        ``subject_kind`` marks whether the subject is a UPS proper or a
        NUT-managed PDU branch; the instrument source follows the kind so
        a PDU outlet is never mislabeled as a UPS.
        """
        server_version, protocol_version = self.server_versions()
        descriptions = dict(self.list_devices())
        names = ups_names or tuple(descriptions)
        records: list[NUTDeviceRecord] = []
        powers: list[OperatingPowerObservation] = []
        events: list[PowerEventObservation] = []
        for name in names:
            record = self.dump_device(
                name,
                server_version=server_version,
                protocol_version=protocol_version,
                description=descriptions.get(name),
            )
            records.append(record)
            variables = record.variables
            subject_id = subject_ref_template.format(name=name)
            instrument_source = (
                'pdu_reported'
                if subject_kind == 'pdu_branch'
                else 'ups_reported'
            )
            tokens = _status_tokens(variables)
            observed_state, state_label = _observed_state(tokens)
            powers.append(
                OperatingPowerObservation(
                    observation_id=str(uuid4()),
                    session_id=session_id,
                    subject_id=subject_id,
                    subject_kind=subject_kind,
                    observed_state=observed_state,  # type: ignore[arg-type]
                    state_label=state_label,
                    instrument_id=f'nut:{name}@{record.driver or "unknown"}',
                    instrument_source=instrument_source,
                    observed_at=observed_at_utc,
                    voltage_v=_float_value(
                        variables, 'output.voltage', 'input.voltage'
                    ),
                    current_a=_float_value(
                        variables, 'output.current', 'input.current'
                    ),
                    real_power_w=_float_value(
                        variables, 'ups.realpower', 'output.realpower'
                    ),
                    apparent_power_va=_float_value(
                        variables, 'ups.power', 'output.power'
                    ),
                    battery_charge_pct=_float_value(
                        variables, 'battery.charge'
                    ),
                    battery_runtime_s=_float_value(
                        variables, 'battery.runtime'
                    ),
                    provenance=_provenance(record, subject_id),
                )
            )
            for token in tokens:
                mapped = _STATUS_EVENT_MAP.get(token)
                if mapped is None:
                    if token in _STATE_MAP or token == 'OL':
                        continue
                    mapped = ('unknown', token)
                kind, label = mapped
                events.append(
                    PowerEventObservation(
                        event_id=str(uuid4()),
                        session_id=session_id,
                        subject_id=subject_id,
                        kind=kind,  # type: ignore[arg-type]
                        event_label=label,
                        observed_at=observed_at_utc,
                        detail=f'ups.status={record.variables.get("ups.status")!r}',
                        provenance=_provenance(record, subject_id),
                    )
                )
        return tuple(records), tuple(powers), tuple(events)


class FixtureNUTTransport:
    """Deterministic in-memory ``upsd`` for CI fixtures.

    ``devices`` maps ups name → (description, variables). Replies follow
    the RFC 9271 line format exactly, including ``ERR`` lines for unknown
    devices/commands.
    """

    def __init__(
        self,
        devices: dict[str, tuple[str, dict[str, str]]],
        *,
        server_version: str = '2.8.0',
        protocol_version: str = '1.3',
        fail: bool = False,
    ) -> None:
        self._devices = {
            name: (desc, dict(variables))
            for name, (desc, variables) in devices.items()
        }
        self._server_version = server_version
        self._protocol_version = protocol_version
        self._fail = fail
        self.requests: list[str] = []

    def exchange(self, request: str) -> tuple[str, ...]:
        self.requests.append(request)
        if self._fail:
            raise NUTError('not_connected', 'fixture transport is down')
        parts = request.strip().split()
        verb = parts[0].upper() if parts else ''
        if verb == 'VER':
            return (self._server_version,)
        if verb == 'NETVER':
            return (self._protocol_version,)
        if verb == 'LIST' and len(parts) >= 2 and parts[1].upper() == 'UPS':
            lines = ['BEGIN LIST UPS']
            for name, (description, _) in sorted(self._devices.items()):
                lines.append(f'UPS {name} "{description}"')
            lines.append('END LIST UPS')
            return tuple(lines)
        if verb == 'LIST' and len(parts) >= 3 and parts[1].upper() == 'VAR':
            name = parts[2]
            device = self._devices.get(name)
            if device is None:
                return ('ERR UNKNOWN-UPS',)
            lines = [f'BEGIN LIST VAR {name}']
            for var_name, value in sorted(device[1].items()):
                lines.append(f'VAR {name} {var_name} "{value}"')
            lines.append(f'END LIST VAR {name}')
            return tuple(lines)
        if verb == 'GET' and len(parts) >= 4 and parts[1].upper() == 'VAR':
            name, var_name = parts[2], parts[3]
            device = self._devices.get(name)
            if device is None:
                return ('ERR UNKNOWN-UPS',)
            value = device[1].get(var_name)
            if value is None:
                return ('ERR VAR-NOT-SUPPORTED',)
            return (f'VAR {name} {var_name} "{value}"',)
        return ('ERR UNKNOWN-COMMAND',)

    def close(self) -> None:
        pass


__all__ = [
    'FixtureNUTTransport',
    'NUT_ADAPTER_ID',
    'NUT_ADAPTER_VERSION',
    'NUT_PROTOCOL_PROFILE',
    'NUTDeviceRecord',
    'NUTError',
    'NUTErrorKind',
    'NUTTelemetryAdapter',
    'NUTTransport',
    'parse_list_var',
    'parse_quoted',
]
