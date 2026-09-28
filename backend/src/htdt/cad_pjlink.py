"""PJLink projector/display adapter (#1082, slices PJA10-PJA40).

First vendor-neutral live adapter for projectors/displays, built on the
JBMIA PJLink Class 2 v2.10 specification (2024-02-29). PJLink provides
standardized power/input/status commands; it does NOT carry lens memory,
picture calibration, HDR mode or other vendor-specific authority — those
remain manufacturer-specific (#1164) and are never fabricated here.

Authority boundary (per the issue contract):

- every binding preserves the PJLink class, reported model, manufacturer,
  software version and endpoint — ``PJLink Class 2`` never implies every
  Class 2 command works; command-level support is probed per command;
- standard response categories stay exact: ``ERR1`` unsupported command,
  ``ERR2`` invalid parameter, ``ERR3`` temporarily unavailable,
  ``ERR4`` device failure, ``ERRA`` authentication — an unavailable
  command during standby is never normalized into "offline";
- a PJLink ``OK`` is an ACK, not observed state — effective state is only
  an :class:`ObservedDeviceState` produced by a later read-back;
- Class 2 spontaneous notifications are optional event evidence: raw
  text, receive time, exact binding and a normalized event are stored —
  never treated as guaranteed/complete truth;
- authentication follows the documented PJLink path only (MD5 of
  seed+password); credentials stay in the local secret store via
  ``credential_ref`` and never enter project data, diagnostics or
  fixtures;
- the JBMIA compatible-product list is a discovery/prequalification hint,
  never ``HARDWARE_VERIFIED`` — tiers stay DOCUMENTED_ONLY /
  FIXTURE_VERIFIED / HARDWARE_VERIFIED (#792).

The transport is a narrow injectable seam (:class:`PJLinkTransport`), so
the whole lifecycle is exercised by :class:`FixturePJLinkTransport` in CI
without hardware; a real TCP transport lands with the hardware slice.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment_device import (
    DeviceActionAck,
    DeviceActionRequest,
    DeviceCapabilityEntry,
    DeviceCapabilitySnapshot,
    DeviceCommand,
    DeviceFrameworkError,
    DeviceTargetBinding,
    ObservedDeviceField,
    ObservedDeviceState,
    ProposedDeviceAction,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash, canonicalize_payload


PJLINK_ADAPTER_ID = 'htdt-pjlink'
PJLINK_ADAPTER_VERSION = '1'
#: The specification profile this adapter normalizes against.
PJLINK_SPEC_PROFILE = 'jbmia-pjlink-class2-2.10'

PJLinkErrorKind = Literal[
    'unsupported_command',
    'invalid_parameter',
    'unavailable',
    'device_failure',
    'auth_failed',
    'malformed_response',
    'timeout',
    'not_connected',
]

_ERROR_MAP = {
    'ERR1': 'unsupported_command',
    'ERR2': 'invalid_parameter',
    'ERR3': 'unavailable',
    'ERR4': 'device_failure',
    'ERRA': 'auth_failed',
}

#: Power code -> normalized state. Transitional states are preserved
#: exactly so callers never hammer a warming/cooling projector.
_PJLINK_POWER = {
    '0000': 'standby',
    '0001': 'on',
    '0002': 'cooling',
    '0003': 'warming',
}

#: AV mute codes (spec): 10/11 video off/on, 20/21 audio off/on,
#: 30/31 both off/on.
_PJLINK_AVMUTE = {
    '10': 'video_unmuted',
    '11': 'video_muted',
    '20': 'audio_unmuted',
    '21': 'audio_muted',
    '30': 'av_unmuted',
    '31': 'av_muted',
}

#: First digit of an input code = input kind, second digit = number.
_PJLINK_INPUT_KIND = {
    '1': 'rgb',
    '2': 'video',
    '3': 'digital',
    '4': 'storage',
    '5': 'network',
}

#: ERST is six positions: fan, lamp, temperature, cover, filter, other;
#: each 0=ok 1=warning 2=error.
_ERST_POSITIONS = ('fan', 'lamp', 'temperature', 'cover', 'filter', 'other')
_ERST_STATE = {'0': 'ok', '1': 'warning', '2': 'error'}

#: Commands probed by probe_capabilities/observe_state. Class-2-only
#: commands are expected to be unsupported on class 1 devices.
_PROBE_COMMANDS = (
    'CLSS',
    'INF1',
    'INF2',
    'INFO',
    'SNUM',
    'SVER',
    'POWR',
    'INPT',
    'AVMT',
    'ERST',
    'LAMP',
    'FILT',
    'INST',
    'IRES',
    'RRES',
    'FRZ',
)

_MUTABLE_FIELDS = ('power', 'input', 'av_mute', 'freeze')






class PJLinkError(RuntimeError):
    """A PJLink exchange failed with an exact protocol category."""

    def __init__(self, kind: PJLinkErrorKind, detail: str) -> None:
        super().__init__(f'{kind}: {detail}')
        self.kind = kind
        self.detail = detail


def encode_query(command: str, device_class: str = '1') -> str:
    """Render a PJLink query line, e.g. ``%1POWR=?``."""
    return f'%{device_class}{command}=?'


def encode_set(command: str, value: str, device_class: str = '1') -> str:
    return f'%{device_class}{command}={value}'


def parse_response_line(line: str) -> tuple[str, str, str]:
    """Parse ``%<class><CMD>=<value>`` → ``(class, cmd, value)``.

    Raises :class:`PJLinkError` ``malformed_response`` on any deviation —
    the adapter never guesses at a damaged frame.
    """
    text = line.strip()
    if not text.startswith('%') or '=' not in text:
        raise PJLinkError('malformed_response', f'bad frame: {line!r}')
    if len(text) < 7:
        raise PJLinkError('malformed_response', f'short frame: {line!r}')
    device_class = text[1]
    command = text[2:6]
    value = text[7:]
    if not device_class.isdigit() or not command.isalnum():
        raise PJLinkError('malformed_response', f'bad frame: {line!r}')
    return device_class, command.upper(), value


def pjlink_auth_digest(seed: str, password: str) -> str:
    """Documented PJLink authentication: MD5 of seed+password.

    ``password`` must come from the local secret store (the binding's
    ``credential_ref``); it is never persisted by this module.
    """
    import hashlib

    return hashlib.md5((seed + password).encode('utf-8')).hexdigest()


def normalize_power(value: str) -> str | None:
    return _PJLINK_POWER.get(value)


def normalize_av_mute(value: str) -> str | None:
    return _PJLINK_AVMUTE.get(value)


def normalize_input(value: str) -> str | None:
    """``31`` → ``digital_1``; unmapped layouts stay ``None``."""
    if len(value) < 2 or not value.isdigit():
        return None
    kind = _PJLINK_INPUT_KIND.get(value[0])
    if kind is None:
        return None
    return f'{kind}_{value[1:]}'


def normalize_error_status(value: str) -> dict[str, str] | None:
    """Six-digit ERST bitmap → per-subsystem state."""
    if len(value) != 6 or not value.isdigit():
        return None
    result: dict[str, str] = {}
    for position, digit in zip(_ERST_POSITIONS, value):
        result[position] = _ERST_STATE.get(digit, 'unknown')
    return result


def normalize_lamp(value: str) -> tuple[dict[str, Any], ...]:
    """LAMP response: space-separated ``<hours> <on/off>`` pairs."""
    lamps: list[dict[str, Any]] = []
    tokens = value.split()
    for index in range(0, len(tokens) - 1, 2):
        hours, state = tokens[index], tokens[index + 1]
        try:
            lamps.append(
                {
                    'index': len(lamps),
                    'hours': int(hours),
                    'on': state == '1' if state in ('0', '1') else state,
                }
            )
        except ValueError:
            lamps.append({'index': len(lamps), 'hours_raw': hours, 'state': state})
    return tuple(lamps)


class PJLinkTransport(Protocol):
    """Injectable wire seam — a real implementation owns TCP :4352.

    ``banner()`` returns the greeting the device sends on connect
    (``PJLINK 0`` = no auth, ``PJLINK 1 <seed>`` = auth required).
    ``exchange()`` sends one rendered request line and returns the raw
    response line. ``poll_notification()`` returns one raw Class-2
    notification line or ``None``.
    """

    def banner(self) -> str: ...

    def exchange(self, request: str) -> str: ...

    def poll_notification(self) -> str | None: ...

    def close(self) -> None: ...


class PJLinkNotificationRecord(BaseModel):
    """One spontaneous Class 2 notification — optional event evidence."""

    model_config = ConfigDict(frozen=True)

    notification_id: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    received_at_utc: str = Field(min_length=1)
    raw_text: str = Field(min_length=1)
    command: str | None = None
    raw_value: str | None = None
    normalized_field: str | None = None
    normalized_value: str | None = None
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_record(self) -> 'PJLinkNotificationRecord':
        if self.record_sha256 != _hash(self.identity_payload()):
            raise ValueError('PJLinkNotificationRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'notification_id': self.notification_id,
            'binding_sha256': self.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'received_at_utc': self.received_at_utc,
            'raw_text': self.raw_text,
            'command': self.command,
            'raw_value': self.raw_value,
            'normalized_field': self.normalized_field,
            'normalized_value': self.normalized_value,
        }


class PJLinkDeviceAdapter:
    """``EquipmentDeviceAdapter`` implementation for PJLink devices.

    Read-first: identity/state queries are the primary surface; bounded
    mutations (power/input/AV-mute/freeze) require the usual
    ``operator_confirmed`` gate and produce an ACK whose truth is only
    established by a later ``read_back``.
    """

    adapter_id = PJLINK_ADAPTER_ID
    adapter_version = PJLINK_ADAPTER_VERSION
    supported_device_kind = 'projector'

    def __init__(self, transports: dict[str, PJLinkTransport]) -> None:
        #: endpoint -> transport. Discovery is explicit: the caller maps
        #: endpoints to transports; the adapter never binds the first
        #: discovered device on its own.
        self._transports = dict(transports)

    def discover_targets(self) -> tuple[str, ...]:
        return tuple(sorted(self._transports))

    def _transport(self, binding: DeviceTargetBinding) -> PJLinkTransport:
        if binding.adapter_id != self.adapter_id:
            raise DeviceFrameworkError('binding belongs to a different adapter')
        if binding.device_kind != self.supported_device_kind:
            raise DeviceFrameworkError(
                'binding device kind is not supported by this adapter'
            )
        transport = self._transports.get(binding.endpoint)
        if transport is None:
            raise DeviceFrameworkError(
                f'endpoint {binding.endpoint} is not bound to this adapter'
            )
        return transport

    def _query(
        self, transport: PJLinkTransport, command: str, device_class: str = '1'
    ) -> str:
        """One request/response with exact error categories preserved."""
        raw = transport.exchange(encode_query(command, device_class))
        _, response_command, value = parse_response_line(raw)
        if response_command != command:
            raise PJLinkError(
                'malformed_response',
                f'response command {response_command} != request {command}',
            )
        if value in _ERROR_MAP:
            raise PJLinkError(_ERROR_MAP[value], f'{command} -> {value}')
        return value

    def probe_capabilities(
        self, binding: DeviceTargetBinding
    ) -> DeviceCapabilitySnapshot:
        transport = self._transport(binding)
        banner = transport.banner()
        if banner.startswith('PJLINK 1'):
            auth_state = (
                'required'
                if binding.credential_ref is not None
                else 'required_missing_credential'
            )
        else:
            auth_state = 'none'
        entries: list[DeviceCapabilityEntry] = [
            DeviceCapabilityEntry(
                capability='authentication', state='supported'
                if auth_state != 'required_missing_credential'
                else 'unknown',
                detail=f'banner={banner!r}',
            )
        ]
        probed_class = '1'
        for command in _PROBE_COMMANDS:
            for device_class in (probed_class, '2'):
                try:
                    value = self._query(transport, command, device_class)
                except PJLinkError as error:
                    if error.kind == 'unsupported_command' and device_class == '1':
                        continue
                    entries.append(
                        DeviceCapabilityEntry(
                            capability=command.lower(),
                            state='unsupported'
                            if error.kind == 'unsupported_command'
                            else 'unknown',
                            detail=error.detail,
                        )
                    )
                    break
                else:
                    entries.append(
                        DeviceCapabilityEntry(
                            capability=command.lower(),
                            state='supported',
                            detail=f'class{device_class}',
                        )
                    )
                    if command == 'CLSS':
                        probed_class = value if value.isdigit() else '1'
                    break
        entries.append(
            DeviceCapabilityEntry(
                capability='power_mutation',
                state='supported'
                if any(e.capability == 'powr' and e.state == 'supported'
                       for e in entries)
                else 'unknown',
                detail='bounded power/input/mute mutations only',
            )
        )
        payload: dict[str, Any] = {
            'snapshot_id': str(uuid4()),
            'binding_sha256': binding.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'probed_at_utc': binding.created_at_utc,
            'capabilities': tuple(entries),
        }
        provisional = DeviceCapabilitySnapshot.model_construct(
            **payload, snapshot_sha256='0' * 64
        )
        return DeviceCapabilitySnapshot(
            **payload, snapshot_sha256=_hash(provisional.semantic_payload())
        )

    def observe_state(self, binding: DeviceTargetBinding) -> ObservedDeviceState:
        transport = self._transport(binding)
        fields: list[ObservedDeviceField] = []
        limitations: list[str] = []
        raw: dict[str, str] = {}
        firmware: str | None = None
        device_class = '1'

        try:
            device_class = self._query(transport, 'CLSS')
        except PJLinkError:
            device_class = '1'
        raw['CLSS'] = device_class
        fields.append(
            ObservedDeviceField(
                field='pjlink_class', state='observed', value=device_class
            )
        )

        identity_map = {
            'INF1': 'manufacturer',
            'INF2': 'model',
            'INFO': 'device_name',
            'SNUM': 'serial_number',
            'SVER': 'software_version',
        }
        for command, field_name in identity_map.items():
            try:
                value = self._query(transport, command, device_class)
            except PJLinkError as error:
                fields.append(
                    ObservedDeviceField(
                        field=field_name,
                        state='unsupported'
                        if error.kind == 'unsupported_command'
                        else 'unknown',
                    )
                )
                limitations.append(f'{command}: {error.kind}')
                continue
            raw[command] = value
            fields.append(
                ObservedDeviceField(
                    field=field_name, state='observed', value=value
                )
            )
            if field_name == 'software_version':
                firmware = value

        state_queries = {
            'POWR': ('power', normalize_power),
            'INPT': ('input', normalize_input),
            'AVMT': ('av_mute', normalize_av_mute),
            'IRES': ('input_resolution', lambda v: v or None),
            'RRES': ('recommended_resolution', lambda v: v or None),
            'FRZ': ('freeze', lambda v: {'0': 'off', '1': 'on'}.get(v)),
        }
        for command, (field_name, normalize) in state_queries.items():
            try:
                value = self._query(transport, command, device_class)
            except PJLinkError as error:
                fields.append(
                    ObservedDeviceField(
                        field=field_name,
                        state='unsupported'
                        if error.kind == 'unsupported_command'
                        else 'unknown',
                    )
                )
                limitations.append(f'{command}: {error.kind}')
                continue
            raw[command] = value
            normalized = normalize(value)
            if normalized is None:
                fields.append(
                    ObservedDeviceField(
                        field=field_name, state='observed', value=value
                    )
                )
                limitations.append(
                    f'{command}: unrecognized value {value!r} kept raw'
                )
            else:
                fields.append(
                    ObservedDeviceField(
                        field=field_name, state='observed', value=normalized
                    )
                )

        try:
            erst = self._query(transport, 'ERST', device_class)
            raw['ERST'] = erst
            decoded = normalize_error_status(erst)
            if decoded is None:
                limitations.append(f'ERST: malformed value {erst!r}')
                fields.append(
                    ObservedDeviceField(
                        field='error_status', state='observed', value=erst
                    )
                )
            else:
                flagged = {
                    k: v for k, v in decoded.items() if v != 'ok'
                }
                fields.append(
                    ObservedDeviceField(
                        field='error_status',
                        state='observed',
                        value='ok' if not flagged else json.dumps(flagged, sort_keys=True),
                    )
                )
        except PJLinkError as error:
            fields.append(
                ObservedDeviceField(
                    field='error_status',
                    state='unsupported'
                    if error.kind == 'unsupported_command'
                    else 'unknown',
                )
            )
            limitations.append(f'ERST: {error.kind}')

        for command, field_name in (('LAMP', 'lamp'), ('FILT', 'filter')):
            try:
                value = self._query(transport, command, device_class)
            except PJLinkError as error:
                fields.append(
                    ObservedDeviceField(
                        field=field_name,
                        state='unsupported'
                        if error.kind == 'unsupported_command'
                        else 'unknown',
                    )
                )
                limitations.append(f'{command}: {error.kind}')
                continue
            raw[command] = value
            fields.append(
                ObservedDeviceField(
                    field=field_name,
                    state='observed',
                    value=json.dumps(
                        normalize_lamp(value), sort_keys=True
                    ),
                )
            )

        try:
            inst = self._query(transport, 'INST', device_class)
            raw['INST'] = inst
            inputs = [
                normalize_input(code) or code
                for code in inst.split()
                if code
            ]
            fields.append(
                ObservedDeviceField(
                    field='available_inputs',
                    state='observed',
                    value=','.join(inputs),
                )
            )
        except PJLinkError as error:
            fields.append(
                ObservedDeviceField(
                    field='available_inputs',
                    state='unsupported'
                    if error.kind == 'unsupported_command'
                    else 'unknown',
                )
            )
            limitations.append(f'INST: {error.kind}')

        payload: dict[str, Any] = {
            'observation_id': str(uuid4()),
            'binding_sha256': binding.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'firmware_version': firmware,
            'observed_at_utc': binding.created_at_utc,
            'fields': tuple(fields),
            'raw_source_sha256': sha256(
                _canonical(raw).encode('utf-8')
            ).hexdigest(),
            'limitations': tuple(sorted(set(limitations))),
        }
        provisional = ObservedDeviceState.model_construct(
            **payload, observation_sha256='0' * 64
        )
        return ObservedDeviceState(
            **payload, observation_sha256=_hash(provisional.semantic_payload())
        )

    def plan_action(
        self,
        binding: DeviceTargetBinding,
        request: DeviceActionRequest,
    ) -> ProposedDeviceAction | None:
        self._transport(binding)
        commands: list[DeviceCommand] = []
        unsupported: list[str] = []
        for field, value in request.request:
            command: str | None = None
            argument: str | None = None
            if field == 'power' and value in ('on', 'standby'):
                command, argument = 'POWR', '1' if value == 'on' else '0'
            elif field == 'input':
                code = self._input_code(value)
                if code is None:
                    unsupported.append(field)
                else:
                    command, argument = 'INPT', code
            elif field == 'av_mute':
                argument = {
                    'video_muted': '11',
                    'video_unmuted': '10',
                    'audio_muted': '21',
                    'audio_unmuted': '20',
                    'av_muted': '31',
                    'av_unmuted': '30',
                }.get(value)
                if argument is None:
                    unsupported.append(field)
                else:
                    command = 'AVMT'
            elif field == 'freeze' and value in ('on', 'off'):
                command, argument = 'FRZ', '1' if value == 'on' else '0'
            if command is None and field not in unsupported:
                unsupported.append(field)
            elif command is not None:
                commands.append(
                    DeviceCommand(
                        field=field, command=command, argument=argument
                    )
                )
        payload: dict[str, Any] = {
            'action_id': str(uuid4()),
            'binding_sha256': binding.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'requested': tuple(request.request),
            'commands': tuple(commands),
            'unsupported': tuple(sorted(set(unsupported))),
            'requires_operator_confirm': True,
            'planned_at_utc': binding.created_at_utc,
        }
        provisional = ProposedDeviceAction.model_construct(
            **payload, action_sha256='0' * 64
        )
        return ProposedDeviceAction(
            **payload, action_sha256=_hash(provisional.semantic_payload())
        )

    @staticmethod
    def _input_code(value: str) -> str | None:
        """``digital_1``/raw ``31`` → PJLink input code."""
        if value.isdigit() and len(value) >= 2:
            return value
        for digit, kind in _PJLINK_INPUT_KIND.items():
            if value.startswith(kind + '_') and value[len(kind) + 1:].isdigit():
                return digit + value[len(kind) + 1:]
        return None

    def apply_action(
        self,
        binding: DeviceTargetBinding,
        action: ProposedDeviceAction,
        *,
        operator_confirmed: bool,
    ) -> DeviceActionAck | None:
        transport = self._transport(binding)
        if action.binding_sha256 != binding.binding_sha256:
            raise DeviceFrameworkError('action belongs to another binding')
        accepted = False
        detail: str | None = None
        if not operator_confirmed:
            detail = 'operator did not confirm'
        else:
            try:
                for command in action.commands:
                    raw = transport.exchange(
                        encode_set(command.command, command.argument or '')
                    )
                    _, response_command, value = parse_response_line(raw)
                    if response_command != command.command:
                        raise PJLinkError(
                            'malformed_response',
                            f'response {response_command} != '
                            f'{command.command}',
                        )
                    if value != 'OK':
                        kind = _ERROR_MAP.get(value, 'device_failure')
                        raise PJLinkError(kind, f'{command.command} -> {value}')
            except PJLinkError as error:
                accepted = False
                detail = f'{command.field}: {error.kind} ({error.detail})'
            else:
                accepted = True
        payload: dict[str, Any] = {
            'ack_id': str(uuid4()),
            'action_sha256': action.action_sha256,
            'acked_at_utc': binding.created_at_utc,
            'accepted': accepted,
            'detail': detail,
            'operator_confirmed': operator_confirmed,
        }
        provisional = DeviceActionAck.model_construct(**canonicalize_payload(DeviceActionAck, dict(
            **payload, ack_sha256='0' * 64
        )))
        return DeviceActionAck(
            **payload, ack_sha256=_hash(provisional.semantic_payload())
        )

    def read_back(
        self, binding: DeviceTargetBinding
    ) -> ObservedDeviceState | None:
        return self.observe_state(binding)

    def collect_notifications(
        self,
        binding: DeviceTargetBinding,
        *,
        received_at_utc: str,
        max_events: int = 64,
    ) -> tuple[PJLinkNotificationRecord, ...]:
        """Drain spontaneous Class-2 notifications as raw+normalized events.

        Notifications are best-effort evidence — never a complete history.
        """
        transport = self._transport(binding)
        records: list[PJLinkNotificationRecord] = []
        for _ in range(max_events):
            line = transport.poll_notification()
            if line is None:
                break
            command: str | None = None
            raw_value: str | None = None
            normalized_field: str | None = None
            normalized_value: str | None = None
            try:
                _, command, raw_value = parse_response_line(line)
            except PJLinkError:
                pass
            if command == 'POWR' and raw_value is not None:
                normalized_field = 'power'
                normalized_value = normalize_power(raw_value)
            elif command == 'INPT' and raw_value is not None:
                normalized_field = 'input'
                normalized_value = normalize_input(raw_value)
            elif command == 'AVMT' and raw_value is not None:
                normalized_field = 'av_mute'
                normalized_value = normalize_av_mute(raw_value)
            payload: dict[str, Any] = {
                'notification_id': str(uuid4()),
                'binding_sha256': binding.binding_sha256,
                'adapter_id': self.adapter_id,
                'adapter_version': self.adapter_version,
                'received_at_utc': received_at_utc,
                'raw_text': line.strip(),
                'command': command,
                'raw_value': raw_value,
                'normalized_field': normalized_field,
                'normalized_value': normalized_value,
            }
            provisional = PJLinkNotificationRecord.model_construct(
                **payload, record_sha256='0' * 64
            )
            records.append(
                PJLinkNotificationRecord(
                    **payload,
                    record_sha256=_hash(provisional.identity_payload()),
                )
            )
        return tuple(records)


class FixturePJLinkTransport:
    """Deterministic in-memory PJLink device for CI fixtures.

    ``responses`` maps ``<CMD>`` → response value (``OK``, ``ERRn``, or a
    data value); ``banner_text`` controls the auth greeting; ``mutate`` is
    applied on set commands so read-back reflects applied state;
    ``notifications`` is a FIFO of raw notification lines.
    """

    def __init__(
        self,
        *,
        responses: dict[str, str],
        banner_text: str = 'PJLINK 0',
        mutable: bool = True,
        notifications: tuple[str, ...] = (),
        fail: bool = False,
    ) -> None:
        self._responses = dict(responses)
        self._banner = banner_text
        self._mutable = mutable
        self._notifications = list(notifications)
        self._fail = fail
        self.exchanges: list[str] = []

    def banner(self) -> str:
        return self._banner

    def exchange(self, request: str) -> str:
        self.exchanges.append(request)
        if self._fail:
            raise PJLinkError('not_connected', 'fixture transport is down')
        body = request.strip().lstrip('%')
        device_class, rest = body[0], body[1:]
        command, _, value = rest.partition('=')
        command = command.upper()
        if value == '?':
            stored = self._responses.get(command)
            if stored is None:
                return f'%{device_class}{command}=ERR1'
            return f'%{device_class}{command}={stored}'
        if self._mutable:
            # POWR set uses '0'/'1' but the query reports '0000'-'0003' —
            # the fixture mirrors that so read-back stays honest.
            self._responses[command] = (
                value.zfill(4) if command == 'POWR' else value
            )
        return f'%{device_class}{command}=OK'

    def poll_notification(self) -> str | None:
        if self._notifications:
            return self._notifications.pop(0)
        return None

    def close(self) -> None:
        pass


__all__ = [
    'FixturePJLinkTransport',
    'PJLINK_ADAPTER_ID',
    'PJLINK_ADAPTER_VERSION',
    'PJLINK_SPEC_PROFILE',
    'PJLinkDeviceAdapter',
    'PJLinkError',
    'PJLinkErrorKind',
    'PJLinkNotificationRecord',
    'PJLinkTransport',
    'encode_query',
    'encode_set',
    'normalize_av_mute',
    'normalize_error_status',
    'normalize_input',
    'normalize_lamp',
    'normalize_power',
    'parse_response_line',
    'pjlink_auth_digest',
]
