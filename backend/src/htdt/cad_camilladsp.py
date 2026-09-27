"""CamillaDSP open-DSP interoperability (#1072).

Two bounded surfaces, both free of proprietary vendor lock-in:

1. **Config import** — :func:`build_camilladsp_artifact` parses a
   CamillaDSP YAML (or JSON) configuration into the shared
   :class:`ImportedCalibrationArtifact` contract (#808). Filters
   (Biquad PEQ, Gain, Delay), pipeline channel attribution and
   external-file references (Conv/Fir impulse responses) normalize into
   the same fields as the Equalizer APO importer; mixer routing,
   processors and unrecognized filter types stay opaque — imported
   configuration evidence is never claimed as applied truth.

2. **Live adapter** — :class:`CamillaDSPAdapter` implements the
   ``EquipmentDeviceAdapter`` contract (#726) over the documented
   WebSocket JSON API (``GetVersion``, ``GetConfigJson``, ``GetState``,
   ``ValidateConfigJson``, ``SetConfigJson``, ``PatchConfigJson``,
   ``Reload``). Apply is gated on ``operator_confirmed`` *and* a
   successful ``ValidateConfigJson``; ``SetConfigJson`` returning
   ``Ok`` is an ACK only — truth is a later ``read_back`` comparing the
   canonical config. The transport is an injectable seam
   (:class:`CamillaDSPTransport`), so no websocket dependency is added
   to the backend; :class:`FixtureCamillaDSPTransport` drives CI.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Protocol
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
from .cad_external_calibration import (
    ChannelMappingEntry,
    ImportedCalibrationArtifact,
    ImportedChannelSettings,
    ImportedFilterBand,
    IncludeDependency,
    OpaqueArtifactSection,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


CAMILLADSP_ADAPTER_ID = 'htdt-camilladsp'
CAMILLADSP_ADAPTER_VERSION = '1'
CAMILLADSP_IMPORTER_ID = 'htdt-camilladsp-importer'
CAMILLADSP_IMPORTER_VERSION = '1'
CAMILLADSP_FORMAT_ID = 'camilladsp-config'

_ARTIFACT_PREFIX = 'imported-calibration:'

#: CamillaDSP Biquad ``parameters.type`` -> ImportedFilterBand type.
_BIQUAD_MAP = {
    'Peaking': 'peaking',
    'Highshelf': 'high_shelf',
    'Lowshelf': 'low_shelf',
    'Highpass': 'high_pass',
    'Lowpass': 'low_pass',
    'Notch': 'notch',
    'Bandpass': 'band_pass',
    'Allpass': 'all_pass',
    'HighpassFO': 'high_pass',
    'LowpassFO': 'low_pass',
}

_DELAY_UNITS = {'ms': 1e-3, 'us': 1e-6, 's': 1.0}


class CamillaDSPError(RuntimeError):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f'{kind}: {detail}')
        self.kind = kind
        self.detail = detail






#: CamillaDSP configs are small hand-written files; a hard input bound keeps
#: YAML alias expansion and parser work proportional to honest sources.
MAX_CAMILLADSP_CONFIG_BYTES = 8 * 1024 * 1024


def load_camilladsp_config(
    source_bytes: bytes,
    *,
    max_bytes: int = MAX_CAMILLADSP_CONFIG_BYTES,
) -> dict[str, Any]:
    """Parse a CamillaDSP config — JSON first, YAML otherwise."""
    if len(source_bytes) > max_bytes:
        raise CamillaDSPError(
            'config_too_large',
            f'CamillaDSP config is {len(source_bytes)} bytes '
            f'(limit {max_bytes})',
        )
    text = source_bytes.decode('utf-8-sig', errors='replace')
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as error:  # pragma: no cover - dep is pinned
            raise CamillaDSPError(
                'missing_dependency',
                'YAML config sources require PyYAML',
            ) from error
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise CamillaDSPError(
                'malformed_config', str(error)
            ) from error
        except RecursionError as error:
            raise CamillaDSPError(
                'malformed_config',
                'YAML nesting exceeds the parser depth limit',
            ) from error
    except RecursionError as error:
        raise CamillaDSPError(
            'malformed_config',
            'JSON nesting exceeds the parser depth limit',
        ) from error
    if not isinstance(parsed, dict):
        raise CamillaDSPError(
            'malformed_config', 'CamillaDSP config must be a mapping'
        )
    return parsed


def _section_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _import_filter(
    name: str,
    spec: Any,
    *,
    opaque: list[OpaqueArtifactSection],
    file_deps: list[IncludeDependency],
) -> tuple[str, dict[str, Any] | None, float | None, float | None]:
    """Normalize one CamillaDSP filter.

    Returns ``(category, band_fields, gain_db, delay_s)`` where category
    is one of ``peq``/``gain``/``delay``/``opaque``.
    """
    if not isinstance(spec, dict):
        opaque.append(
            OpaqueArtifactSection(
                kind='malformed_line',
                raw_text=_section_text(spec),
                reason=f'filter {name!r} is not a mapping',
            )
        )
        return 'opaque', None, None, None
    ftype = spec.get('type')
    params = spec.get('parameters') or {}
    raw = _section_text(spec)
    if ftype == 'Biquad':
        subtype = params.get('type')
        mapped = _BIQUAD_MAP.get(subtype)
        if mapped is None:
            opaque.append(
                OpaqueArtifactSection(
                    kind='unsupported_filter_type',
                    raw_text=raw,
                    reason=f'biquad type {subtype!r} is outside the imported subset',
                )
            )
            return 'opaque', None, None, None
        fields: dict[str, Any] = {
            'filter_type': mapped,
            'enabled': bool(params.get('enabled', True)),
            'frequency_hz': params.get('freq') or params.get('freq_act'),
            'gain_db': params.get('gain'),
            'q': params.get('q') or params.get('q_act'),
            'bandwidth_oct': params.get('bandwidth'),
        }
        return 'peq', fields, None, None
    if ftype == 'Gain':
        try:
            return 'gain', None, float(params.get('gain', 0.0)), None
        except (TypeError, ValueError):
            opaque.append(
                OpaqueArtifactSection(
                    kind='malformed_line',
                    raw_text=raw,
                    reason=f'gain filter {name!r} has a non-numeric gain',
                )
            )
            return 'opaque', None, None, None
    if ftype == 'Delay':
        unit = str(params.get('unit', 'ms')).lower()
        try:
            value = float(params.get('delay'))
        except (TypeError, ValueError):
            value = None
        if value is not None and unit in _DELAY_UNITS:
            return 'delay', None, None, value * _DELAY_UNITS[unit]
        opaque.append(
            OpaqueArtifactSection(
                kind='unsupported_command',
                raw_text=raw,
                reason='delay in samples/mm needs the source sample rate or speed of sound; retained opaque',
            )
        )
        return 'opaque', None, None, None
    if ftype in ('Conv', 'Fir'):
        filename = params.get('filename')
        if isinstance(filename, str) and filename:
            file_deps.append(
                IncludeDependency(
                    include_path=filename,
                    resolved=False,
                    diagnostics=(
                        'convolution/FIR file referenced; content not bundled in config',
                    ),
                )
            )
        opaque.append(
            OpaqueArtifactSection(
                kind='external_file_reference',
                raw_text=raw,
                reason=f'{ftype} filter {name!r} references an external impulse response',
            )
        )
        return 'opaque', None, None, None
    opaque.append(
        OpaqueArtifactSection(
            kind='unsupported_filter_type',
            raw_text=raw,
            reason=f'filter type {ftype!r} is outside the imported subset',
        )
    )
    return 'opaque', None, None, None


def build_camilladsp_artifact(
    source_bytes: bytes,
    *,
    source_filename: str,
    imported_at_utc: str,
    artifact_id: str,
    channel_map: dict[str, str] | None = None,
    diagnostics_extra: tuple[str, ...] = (),
) -> ImportedCalibrationArtifact:
    """Parse a CamillaDSP YAML/JSON config into imported-config evidence.

    Channel buckets key on ``playback:N`` (pipeline ``channel:`` indices);
    ``channel_map`` binds those labels to HTDT channel ids — unmapped
    labels stay unmapped.
    """
    config = load_camilladsp_config(source_bytes)
    source_sha = sha256(source_bytes).hexdigest()
    diagnostics: list[str] = list(diagnostics_extra)
    opaque: list[OpaqueArtifactSection] = []
    file_deps: list[IncludeDependency] = []

    filters = config.get('filters') or {}
    devices = config.get('devices') or {}
    mixers = config.get('mixers') or {}
    processors = config.get('processors') or {}
    pipeline = config.get('pipeline') or []

    playback = devices.get('playback') or {}
    n_channels = playback.get('channels')
    channels: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def bucket_for(index: int) -> dict[str, Any]:
        label = f'playback:{index}'
        if label not in channels:
            channels[label] = {
                'preamp_db': None,
                'delay_s': None,
                'peq': [],
            }
            order.append(label)
        return channels[label]

    if isinstance(n_channels, int):
        for index in range(n_channels):
            bucket_for(index)

    for name, spec in mixers.items() if isinstance(mixers, dict) else []:
        opaque.append(
            OpaqueArtifactSection(
                kind='unsupported_command',
                raw_text=_section_text(spec),
                reason=f'mixer {name!r} routing is not modeled in the import subset',
            )
        )
    for name, spec in (
        processors.items() if isinstance(processors, dict) else []
    ):
        opaque.append(
            OpaqueArtifactSection(
                kind='unsupported_command',
                raw_text=_section_text(spec),
                reason=f'processor {name!r} is not modeled in the import subset',
            )
        )

    band_counter = 0
    for step in pipeline if isinstance(pipeline, list) else []:
        if not isinstance(step, dict):
            opaque.append(
                OpaqueArtifactSection(
                    kind='malformed_line',
                    raw_text=_section_text(step),
                    reason='pipeline step is not a mapping',
                )
            )
            continue
        step_type = step.get('type')
        if step_type != 'Filter':
            opaque.append(
                OpaqueArtifactSection(
                    kind='unsupported_command',
                    raw_text=_section_text(step),
                    reason=f'pipeline step type {step_type!r} is not modeled',
                )
            )
            continue
        channel_index = step.get('channel')
        if not isinstance(channel_index, int):
            opaque.append(
                OpaqueArtifactSection(
                    kind='malformed_line',
                    raw_text=_section_text(step),
                    reason='filter pipeline step lacks a channel index',
                )
            )
            continue
        bucket = bucket_for(channel_index)
        for filter_name in step.get('names') or []:
            category, fields, gain_db, delay_s = _import_filter(
                filter_name,
                filters.get(filter_name),
                opaque=opaque,
                file_deps=file_deps,
            )
            if category == 'peq' and fields is not None:
                bucket['peq'].append(
                    (
                        f'{filter_name}',
                        band_counter,
                        fields,
                    )
                )
                band_counter += 1
            elif category == 'gain' and gain_db is not None:
                bucket['preamp_db'] = (
                    (bucket['preamp_db'] or 0.0) + gain_db
                )
            elif category == 'delay' and delay_s is not None:
                bucket['delay_s'] = (bucket['delay_s'] or 0.0) + delay_s

    channel_map = channel_map or {}
    mapping: list[ChannelMappingEntry] = []
    normalized: list[ImportedChannelSettings] = []
    for label in order:
        bucket = channels[label]
        mapped = channel_map.get(label)
        mapping.append(
            ChannelMappingEntry(
                channel_label=label,
                htdt_channel_id=mapped,
                state='mapped' if mapped is not None else 'unmapped',
            )
        )
        if mapped is None:
            diagnostics.append(
                f'channel {label} has no HTDT mapping'
            )
        normalized.append(
            ImportedChannelSettings(
                channel_label=label,
                preamp_db=bucket['preamp_db'],
                delay_s=bucket['delay_s'],
                peq=tuple(
                    ImportedFilterBand(
                        band_index=index,
                        filter_type=fields['filter_type'],
                        enabled=fields['enabled'],
                        frequency_hz=fields['frequency_hz'],
                        gain_db=fields['gain_db'],
                        q=fields['q'],
                        bandwidth_oct=fields['bandwidth_oct'],
                        raw_text=raw_name,
                    )
                    for index, (raw_name, _n, fields) in enumerate(
                        bucket['peq']
                    )
                ),
            )
        )

    payload: dict[str, Any] = {
        'artifact_id': artifact_id,
        'state': 'imported_configuration',
        'producer_tool': 'camilladsp',
        'format_id': CAMILLADSP_FORMAT_ID,
        'format_version': 'unknown',
        'importer_id': CAMILLADSP_IMPORTER_ID,
        'importer_version': CAMILLADSP_IMPORTER_VERSION,
        'source_filename': source_filename,
        'source_sha256': source_sha,
        'imported_at_utc': imported_at_utc,
        'device_context': json.dumps(devices, sort_keys=True, allow_nan=False)
        if devices
        else None,
        'global_preamp_db': None,
        'channel_mapping': tuple(mapping),
        'channels': tuple(normalized),
        'opaque_sections': tuple(opaque),
        'include_dependencies': (),
        'file_dependencies': tuple(file_deps),
        'diagnostics': tuple(diagnostics),
    }
    provisional = ImportedCalibrationArtifact.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return ImportedCalibrationArtifact.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


class CamillaDSPTransport(Protocol):
    """Injectable seam to the CamillaDSP WebSocket JSON API.

    ``request`` sends one command object (e.g. ``{"GetState": null}``)
    and returns the decoded response ``{"<Cmd>": {"result": ...,
    "value": ...}}``. A real implementation owns the ws:// connection;
    authentication is transport-level and never enters project data.
    """

    def request(self, command: dict[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None: ...


def _result_value(
    response: dict[str, Any], command: str
) -> tuple[bool, Any]:
    """Unpack ``{"Cmd": {"result": "Ok"|"Error", "value": ...}}``."""
    envelope = response.get(command)
    if not isinstance(envelope, dict):
        raise CamillaDSPError(
            'malformed_response', f'no {command} key in response'
        )
    if envelope.get('result') == 'Error':
        return False, envelope.get('value')
    if envelope.get('result') != 'Ok':
        raise CamillaDSPError(
            'malformed_response', f'{command} result={envelope.get("result")!r}'
        )
    return True, envelope.get('value')


class CamillaDSPAdapter:
    """``EquipmentDeviceAdapter`` over the CamillaDSP WebSocket API."""

    adapter_id = CAMILLADSP_ADAPTER_ID
    adapter_version = CAMILLADSP_ADAPTER_VERSION
    supported_device_kind = 'dsp'

    def __init__(self, transports: dict[str, CamillaDSPTransport]) -> None:
        #: endpoint 'camilladsp://host:port' -> transport
        self._transports = dict(transports)

    def discover_targets(self) -> tuple[str, ...]:
        return tuple(sorted(self._transports))

    def _transport(self, binding: DeviceTargetBinding) -> CamillaDSPTransport:
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

    def _call(
        self,
        transport: CamillaDSPTransport,
        command: str,
        argument: Any = None,
    ) -> Any:
        """One CamillaDSP command; raises CamillaDSPError on Error."""
        response = transport.request({command: argument})
        ok, value = _result_value(response, command)
        if not ok:
            raise CamillaDSPError('device_error', f'{command}: {value!r}')
        return value

    def probe_capabilities(
        self, binding: DeviceTargetBinding
    ) -> DeviceCapabilitySnapshot:
        transport = self._transport(binding)
        entries: list[DeviceCapabilityEntry] = []
        for capability, command in (
            ('version', 'GetVersion'),
            ('config_json', 'GetConfigJson'),
            ('state', 'GetState'),
        ):
            try:
                self._call(transport, command)
            except CamillaDSPError as error:
                entries.append(
                    DeviceCapabilityEntry(
                        capability=command.lower(),
                        state='unknown',
                        detail=f'{error.kind}: {error.detail}',
                    )
                )
            else:
                entries.append(
                    DeviceCapabilityEntry(
                        capability=command.lower(),
                        state='supported',
                    )
                )
        entries.append(
            DeviceCapabilityEntry(
                capability='config_mutation',
                state='supported',
                detail='ValidateConfigJson-gated SetConfigJson/PatchConfigJson + Reload',
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

    def _canonical_config(self, transport: CamillaDSPTransport) -> str:
        raw = self._call(transport, 'GetConfigJson')
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return _canonical(parsed)

    def observe_state(self, binding: DeviceTargetBinding) -> ObservedDeviceState:
        transport = self._transport(binding)
        fields: list[ObservedDeviceField] = []
        limitations: list[str] = []
        version: str | None = None
        try:
            version = self._call(transport, 'GetVersion')
            fields.append(
                ObservedDeviceField(
                    field='camilladsp_version',
                    state='observed',
                    value=str(version),
                )
            )
        except CamillaDSPError as error:
            fields.append(
                ObservedDeviceField(field='camilladsp_version', state='unknown')
            )
            limitations.append(f'GetVersion: {error.kind}')
        try:
            state = self._call(transport, 'GetState')
            fields.append(
                ObservedDeviceField(
                    field='state', state='observed', value=str(state)
                )
            )
        except CamillaDSPError as error:
            fields.append(ObservedDeviceField(field='state', state='unknown'))
            limitations.append(f'GetState: {error.kind}')
        try:
            fields.append(
                ObservedDeviceField(
                    field='config_json',
                    state='observed',
                    value=self._canonical_config(transport),
                )
            )
        except CamillaDSPError as error:
            fields.append(
                ObservedDeviceField(field='config_json', state='unknown')
            )
            limitations.append(f'GetConfigJson: {error.kind}')
        payload: dict[str, Any] = {
            'observation_id': str(uuid4()),
            'binding_sha256': binding.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'firmware_version': str(version) if version else None,
            'observed_at_utc': binding.created_at_utc,
            'fields': tuple(fields),
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
            if field == 'config_json':
                try:
                    canonical = _canonical(json.loads(value))
                except (json.JSONDecodeError, TypeError):
                    unsupported.append(field)
                    continue
                commands.append(
                    DeviceCommand(
                        field=field,
                        command='SetConfigJson',
                        argument=canonical,
                    )
                )
            elif field == 'reload' and value == 'true':
                commands.append(
                    DeviceCommand(field=field, command='Reload')
                )
            else:
                unsupported.append(field)
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
                    if command.command == 'SetConfigJson':
                        # Validation gate: the device must accept the
                        # config's own schema check before it is set.
                        self._call(
                            transport,
                            'ValidateConfigJson',
                            command.argument,
                        )
                    self._call(
                        transport, command.command, command.argument
                    )
            except CamillaDSPError as error:
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
        provisional = DeviceActionAck.model_construct(
            **payload, ack_sha256='0' * 64
        )
        return DeviceActionAck(
            **payload, ack_sha256=_hash(provisional.semantic_payload())
        )

    def read_back(
        self, binding: DeviceTargetBinding
    ) -> ObservedDeviceState | None:
        return self.observe_state(binding)


class FixtureCamillaDSPTransport:
    """Deterministic in-memory CamillaDSP daemon for CI fixtures."""

    def __init__(
        self,
        *,
        version: str = '3.0.0',
        state: str = 'Running',
        config: dict[str, Any] | None = None,
        reject_config: str | None = None,
        fail: bool = False,
    ) -> None:
        self._version = version
        self._state = state
        self._config = config if config is not None else {'devices': {}}
        self._reject_config = reject_config
        self._fail = fail
        self.requests: list[dict[str, Any]] = []

    def request(self, command: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(command)
        if self._fail:
            raise CamillaDSPError('not_connected', 'fixture transport is down')
        if len(command) != 1:
            raise CamillaDSPError('malformed_response', 'one key expected')
        name, argument = next(iter(command.items()))
        if name == 'GetVersion':
            return {name: {'result': 'Ok', 'value': self._version}}
        if name == 'GetState':
            return {name: {'result': 'Ok', 'value': self._state}}
        if name in ('GetConfigJson', 'GetConfig'):
            return {
                name: {'result': 'Ok', 'value': json.dumps(self._config)}
            }
        if name in ('ValidateConfigJson', 'ValidateConfig'):
            if self._reject_config is not None:
                return {
                    name: {'result': 'Error', 'value': self._reject_config}
                }
            return {name: {'result': 'Ok', 'value': None}}
        if name in ('SetConfigJson', 'SetConfig', 'PatchConfigJson', 'PatchConfig'):
            if self._reject_config is not None:
                return {
                    name: {'result': 'Error', 'value': self._reject_config}
                }
            try:
                self._config = json.loads(argument)
            except (json.JSONDecodeError, TypeError):
                return {
                    name: {
                        'result': 'Error',
                        'value': 'config is not valid JSON',
                    }
                }
            return {name: {'result': 'Ok', 'value': None}}
        if name == 'Reload':
            return {name: {'result': 'Ok', 'value': None}}
        return {name: {'result': 'Error', 'value': 'unknown command'}}

    def close(self) -> None:
        pass


__all__ = [
    'CAMILLADSP_ADAPTER_ID',
    'CAMILLADSP_ADAPTER_VERSION',
    'CAMILLADSP_FORMAT_ID',
    'CAMILLADSP_IMPORTER_ID',
    'CAMILLADSP_IMPORTER_VERSION',
    'CamillaDSPAdapter',
    'CamillaDSPError',
    'CamillaDSPTransport',
    'FixtureCamillaDSPTransport',
    'build_camilladsp_artifact',
    'load_camilladsp_config',
]
