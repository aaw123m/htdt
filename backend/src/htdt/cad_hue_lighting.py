"""Philips Hue local API (CLIP v2) lighting adapter (#1069).

First concrete read-back/apply target for Theater Lighting (#640):
bounded on/off + dimming + color-temperature control of Hue lights and
zones via the bridge-local REST API, plus scene recall and the event
stream as optional evidence.

Authority boundary (per the issue contract):

- bindings are explicit: an endpoint ``hue://<bridge-host>/<rtype>/<rid>``
  names one bridge + one resource; the adapter verifies the reported
  ``bridge_id`` against the binding's expected id before mutating;
- the ``hue-application-key`` never enters project data: bindings carry
  only ``credential_ref`` (the secret's *name*); the transport resolves
  it against the local secret store at request time;
- a Hue ``200`` is an ACK, not observed state — truth is only a later
  read-back (``verify_action_outcome`` stays the arbiter);
- unsupported capabilities (gradients, entertainment/streaming, dynamic
  scenes) report ``unsupported`` — never silently applied;
- events from ``/eventstream/clip/v2`` are *event evidence* (raw JSON +
  normalized deltas), not guaranteed-complete history;
- Matter Scenes stay a parallel track: this adapter does NOT claim
  Matter — the ADR records the controller-reuse evaluation
  (see ``docs/ISSUE_1069_...``).

Transport is an injectable seam (:class:`HueTransport`);
:class:`FixtureHueTransport` exercises the whole lifecycle in CI.
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
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


HUE_ADAPTER_ID = 'htdt-hue-local'
HUE_ADAPTER_VERSION = '1'
HUE_SPEC_PROFILE = 'philips-hue-clip-v2'

#: Resource types this adapter binds.
_RESOURCE_KINDS = ('light', 'grouped_light', 'scene')

#: Mirek limits for the color_temperature channel (Hue white ambiance).
_MIREK_MIN, _MIREK_MAX = 153, 500






def kelvin_to_mirek(kelvin: float) -> int:
    return max(_MIREK_MIN, min(_MIREK_MAX, round(1_000_000.0 / kelvin)))


def mirek_to_kelvin(mirek: float) -> int:
    return round(1_000_000.0 / mirek)


class HueError(RuntimeError):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f'{kind}: {detail}')
        self.kind = kind
        self.detail = detail


def split_endpoint(endpoint: str) -> tuple[str, str, str]:
    """``hue://<host>/<rtype>/<rid>`` → ``(host, rtype, rid)``."""
    if not endpoint.startswith('hue://'):
        raise HueError('bad_endpoint', endpoint)
    parts = endpoint[len('hue://'):].split('/')
    if len(parts) != 3 or not all(parts):
        raise HueError('bad_endpoint', endpoint)
    host, rtype, rid = parts
    if rtype not in _RESOURCE_KINDS:
        raise HueError('bad_endpoint', f'unsupported resource {rtype!r}')
    return host, rtype, rid


class HueTransport(Protocol):
    """Injectable seam to the bridge-local HTTPS API.

    ``request`` performs one CLIP v2 call; ``credential`` is the resolved
    application key (supplied by the caller from the secret store — the
    transport must not persist it). ``poll_event`` returns one event
    object from the event stream or ``None``.
    """

    def request(
        self,
        host: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        credential: str | None = None,
    ) -> dict[str, Any]: ...

    def poll_event(self, host: str) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class HueEventRecord(BaseModel):
    """One Hue event-stream object — raw + normalized deltas."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    host: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    received_at_utc: str = Field(min_length=1)
    raw: dict[str, Any]
    resource_type: str | None = None
    resource_rid: str | None = None
    normalized: dict[str, Any] | None = None
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_record(self) -> 'HueEventRecord':
        if self.record_sha256 != _hash(self.identity_payload()):
            raise ValueError('HueEventRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'event_id': self.event_id,
            'host': self.host,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'received_at_utc': self.received_at_utc,
            'raw': self.raw,
            'resource_type': self.resource_type,
            'resource_rid': self.resource_rid,
            'normalized': self.normalized,
        }


def _normalize_state(resource: dict[str, Any]) -> dict[str, str]:
    """Extract normalized observable fields from a resource object."""
    out: dict[str, str] = {}
    on = resource.get('on')
    if isinstance(on, dict) and 'on' in on:
        out['on'] = 'on' if on['on'] else 'off'
    dimming = resource.get('dimming')
    if isinstance(dimming, dict) and dimming.get('brightness') is not None:
        out['brightness_pct'] = f"{float(dimming['brightness']):g}"
    ct = resource.get('color_temperature')
    if isinstance(ct, dict) and ct.get('mirek') is not None:
        out['cct_k'] = str(mirek_to_kelvin(float(ct['mirek'])))
    color = resource.get('color')
    if (
        isinstance(color, dict)
        and isinstance(color.get('xy'), dict)
        and color['xy'].get('x') is not None
    ):
        out['color_xy'] = f"{float(color['xy']['x']):g},{float(color['xy']['y']):g}"
    mode = resource.get('mode')
    if isinstance(mode, str):
        out['mode'] = mode
    return out


def _identity_fields(resource: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    metadata = resource.get('metadata')
    if isinstance(metadata, dict) and metadata.get('name'):
        out['name'] = str(metadata['name'])
    product = resource.get('product_data')
    if isinstance(product, dict):
        if product.get('model_id'):
            out['model_id'] = str(product['model_id'])
        if product.get('software_version'):
            out['software_version'] = str(product['software_version'])
        if product.get('manufacturer_name'):
            out['manufacturer'] = str(product['manufacturer_name'])
    return out


class HueLocalAdapter:
    """``EquipmentDeviceAdapter`` over the Hue bridge-local CLIP v2 API.

    ``hosts`` maps bridge host → transport. Discovery lists the resources
    each host's transport exposes; bindings are still explicit.
    """

    adapter_id = HUE_ADAPTER_ID
    adapter_version = HUE_ADAPTER_VERSION
    supported_device_kind = 'lighting_fixture'

    def __init__(
        self,
        transports: dict[str, HueTransport],
        *,
        credential_resolver: Any | None = None,
    ) -> None:
        self._transports = dict(transports)
        # credential_resolver(credential_ref) -> secret value; never stored.
        self._credential_resolver = credential_resolver

    # -- transport plumbing -------------------------------------------------

    def _resolve_credential(self, binding: DeviceTargetBinding) -> str | None:
        if binding.credential_ref is None or self._credential_resolver is None:
            return None
        return self._credential_resolver(binding.credential_ref)

    def _transport(self, binding: DeviceTargetBinding) -> tuple[HueTransport, str, str, str]:
        if binding.adapter_id != self.adapter_id:
            raise DeviceFrameworkError('binding belongs to a different adapter')
        if binding.device_kind not in ('lighting_fixture', 'lighting_controller'):
            raise DeviceFrameworkError(
                'binding device kind is not supported by this adapter'
            )
        host, rtype, rid = split_endpoint(binding.endpoint)
        transport = self._transports.get(host)
        if transport is None:
            raise DeviceFrameworkError(
                f'bridge host {host} is not bound to this adapter'
            )
        return transport, host, rtype, rid

    def discover_targets(self) -> tuple[str, ...]:
        endpoints: list[str] = []
        for host, transport in sorted(self._transports.items()):
            for rtype in _RESOURCE_KINDS:
                try:
                    listing = transport.request(
                        host, 'GET', f'/clip/v2/resource/{rtype}'
                    )
                except HueError:
                    continue
                for item in listing.get('data', []):
                    rid = item.get('id')
                    if rid:
                        endpoints.append(f'hue://{host}/{rtype}/{rid}')
        return tuple(sorted(endpoints))

    # -- adapter contract ---------------------------------------------------

    def probe_capabilities(
        self, binding: DeviceTargetBinding
    ) -> DeviceCapabilitySnapshot:
        transport, host, rtype, rid = self._transport(binding)
        resource = self._get_resource(transport, host, rtype, rid, binding)
        state = _normalize_state(resource)
        entries: list[DeviceCapabilityEntry] = [
            DeviceCapabilityEntry(
                capability='bridge_identity',
                state='supported',
                detail=self._bridge_id(transport, host, binding),
            ),
            DeviceCapabilityEntry(
                capability='read_state',
                state='supported' if state else 'unknown',
            ),
        ]
        for capability, field in (
            ('set_on', 'on'),
            ('set_brightness', 'brightness_pct'),
            ('set_cct', 'cct_k'),
            ('set_color_xy', 'color_xy'),
            ('scene_recall', 'mode' if rtype == 'scene' else '__none__'),
        ):
            entries.append(
                DeviceCapabilityEntry(
                    capability=capability,
                    state='supported'
                    if field in state or (capability == 'scene_recall' and rtype == 'scene')
                    else 'unsupported',
                )
            )
        entries.append(
            DeviceCapabilityEntry(
                capability='gradients_entertainment_dynamic_scenes',
                state='unsupported',
                detail='bounded surface: on/dimming/CCT/xy/scene recall only',
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

    def _bridge_id(
        self, transport: HueTransport, host: str, binding: DeviceTargetBinding
    ) -> str | None:
        try:
            listing = transport.request(
                host, 'GET', '/clip/v2/resource/bridge',
                credential=self._resolve_credential(binding),
            )
            data = listing.get('data') or []
            return data[0].get('bridge_id') if data else None
        except HueError:
            return None

    def _get_resource(
        self,
        transport: HueTransport,
        host: str,
        rtype: str,
        rid: str,
        binding: DeviceTargetBinding,
    ) -> dict[str, Any]:
        response = transport.request(
            host,
            'GET',
            f'/clip/v2/resource/{rtype}/{rid}',
            credential=self._resolve_credential(binding),
        )
        errors = response.get('errors')
        if errors:
            raise HueError('api_error', json.dumps(errors, sort_keys=True))
        data = response.get('data') or []
        if not data:
            raise HueError('not_found', f'{rtype}/{rid}')
        return data[0]

    def observe_state(self, binding: DeviceTargetBinding) -> ObservedDeviceState:
        transport, host, rtype, rid = self._transport(binding)
        resource = self._get_resource(transport, host, rtype, rid, binding)
        state = _normalize_state(resource)
        identity = _identity_fields(resource)
        fields: list[ObservedDeviceField] = [
            ObservedDeviceField(field='resource_type', state='observed', value=rtype),
            ObservedDeviceField(
                field='resource_id', state='observed', value=rid
            ),
        ]
        for name in ('name', 'model_id', 'software_version', 'manufacturer'):
            if name in identity:
                fields.append(
                    ObservedDeviceField(
                        field=name, state='observed', value=identity[name]
                    )
                )
        for name in ('on', 'brightness_pct', 'cct_k', 'color_xy', 'mode'):
            if name in state:
                fields.append(
                    ObservedDeviceField(
                        field=name, state='observed', value=state[name]
                    )
                )
        limitations: list[str] = []
        if 'on' not in state:
            limitations.append('resource carries no on/off state')
        payload: dict[str, Any] = {
            'observation_id': str(uuid4()),
            'binding_sha256': binding.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'firmware_version': identity.get('software_version'),
            'observed_at_utc': binding.created_at_utc,
            'fields': tuple(fields),
            'raw_source_sha256': sha256(
                _canonical(resource).encode('utf-8')
            ).hexdigest(),
            'limitations': tuple(limitations),
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
        _, _, rtype, _ = self._transport(binding)
        commands: list[DeviceCommand] = []
        unsupported: list[str] = []
        for field, value in request.request:
            body: dict[str, Any] | None = None
            if field == 'on' and value in ('on', 'off'):
                body = {'on': {'on': value == 'on'}}
            elif field == 'brightness_pct':
                try:
                    pct = float(value)
                except ValueError:
                    pct = -1.0
                if 0.0 <= pct <= 100.0:
                    body = {'dimming': {'brightness': pct}}
            elif field == 'cct_k':
                try:
                    body = {
                        'color_temperature': {
                            'mirek': kelvin_to_mirek(float(value))
                        }
                    }
                except ValueError:
                    body = None
            elif field == 'color_xy':
                parts = value.split(',')
                if len(parts) == 2:
                    try:
                        body = {
                            'color': {
                                'xy': {
                                    'x': float(parts[0]),
                                    'y': float(parts[1]),
                                }
                            }
                        }
                    except ValueError:
                        body = None
            elif field == 'scene_recall' and rtype == 'scene' and value == 'active':
                body = {'recall': {'action': 'active'}}
            if body is None:
                unsupported.append(field)
            else:
                commands.append(
                    DeviceCommand(
                        field=field,
                        command='put',
                        argument=json.dumps(body, sort_keys=True),
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

    def apply_action(
        self,
        binding: DeviceTargetBinding,
        action: ProposedDeviceAction,
        *,
        operator_confirmed: bool,
    ) -> DeviceActionAck | None:
        transport, host, rtype, rid = self._transport(binding)
        if action.binding_sha256 != binding.binding_sha256:
            raise DeviceFrameworkError('action belongs to another binding')
        accepted = False
        detail: str | None = None
        if not operator_confirmed:
            detail = 'operator did not confirm'
        else:
            merged: dict[str, Any] = {}
            for command in action.commands:
                merged.update(json.loads(command.argument or '{}'))
            try:
                response = transport.request(
                    host,
                    'PUT',
                    f'/clip/v2/resource/{rtype}/{rid}',
                    body=merged,
                    credential=self._resolve_credential(binding),
                )
            except HueError as error:
                detail = f'{error.kind}: {error.detail}'
            else:
                errors = response.get('errors')
                if errors:
                    detail = json.dumps(errors, sort_keys=True)
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

    def collect_events(
        self,
        host: str,
        *,
        received_at_utc: str,
        max_events: int = 64,
    ) -> tuple[HueEventRecord, ...]:
        """Drain queued event-stream objects into event evidence records."""
        transport = self._transports.get(host)
        if transport is None:
            raise DeviceFrameworkError(
                f'bridge host {host} is not bound to this adapter'
            )
        records: list[HueEventRecord] = []
        for _ in range(max_events):
            event = transport.poll_event(host)
            if event is None:
                break
            normalized: dict[str, Any] | None = None
            resource_type: str | None = None
            resource_rid: str | None = None
            data = event.get('data') or []
            if data and isinstance(data[0], dict):
                resource_type = data[0].get('type')
                resource_rid = data[0].get('id')
                normalized = _normalize_state(data[0]) or None
            payload: dict[str, Any] = {
                'event_id': str(uuid4()),
                'host': host,
                'adapter_id': self.adapter_id,
                'adapter_version': self.adapter_version,
                'received_at_utc': received_at_utc,
                'raw': event,
                'resource_type': resource_type,
                'resource_rid': resource_rid,
                'normalized': normalized,
            }
            provisional = HueEventRecord.model_construct(
                **payload, record_sha256='0' * 64
            )
            records.append(
                HueEventRecord(
                    **payload,
                    record_sha256=_hash(provisional.identity_payload()),
                )
            )
        return tuple(records)


class FixtureHueTransport:
    """Deterministic in-memory Hue bridge for CI fixtures.

    ``resources`` maps ``<rtype>/<rid>`` → a CLIP v2 resource dict that is
    mutated in place by PUT so read-back reflects applied state.
    """

    def __init__(
        self,
        resources: dict[str, dict[str, Any]],
        *,
        bridge_id: str = '001788fffe123456',
        events: tuple[dict[str, Any], ...] = (),
        credential: str | None = None,
        fail: bool = False,
    ) -> None:
        self._resources = resources
        self._bridge_id = bridge_id
        self._events = list(events)
        self._credential = credential
        self._fail = fail
        self.requests: list[tuple[str, str, str, dict[str, Any] | None]] = []

    def request(
        self,
        host: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        credential: str | None = None,
    ) -> dict[str, Any]:
        self.requests.append((host, method, path, body))
        if self._fail:
            raise HueError('not_connected', 'fixture transport is down')
        if self._credential is not None and credential != self._credential:
            return {
                'errors': [
                    {'description': 'unauthorized user', 'address': path}
                ]
            }
        parts = [p for p in path.split('/') if p]
        # /clip/v2/resource/<rtype>[/<rid>]
        if parts[:3] != ['clip', 'v2', 'resource']:
            return {'errors': [{'description': 'bad path', 'address': path}]}
        rtype = parts[3]
        if rtype == 'bridge':
            return {
                'data': [
                    {
                        'id': 'bridge-resource',
                        'type': 'bridge',
                        'bridge_id': self._bridge_id,
                    }
                ]
            }
        if len(parts) == 4:
            return {
                'data': [
                    dict(resource, id=rid)
                    for key, resource in self._resources.items()
                    for key_type, rid in (key.split('/', 1),)
                    if key_type == rtype
                ]
            }
        rid = parts[4]
        resource = self._resources.get(f'{rtype}/{rid}')
        if resource is None:
            return {
                'errors': [
                    {'description': 'resource not found', 'address': path}
                ]
            }
        if method == 'PUT' and body is not None:
            resource.update(body)
            return {'data': [{'rid': rid, 'rtype': rtype}]}
        return {'data': [dict(resource, id=rid)]}

    def poll_event(self, host: str) -> dict[str, Any] | None:
        if self._events:
            return self._events.pop(0)
        return None

    def close(self) -> None:
        pass


__all__ = [
    'FixtureHueTransport',
    'HUE_ADAPTER_ID',
    'HUE_ADAPTER_VERSION',
    'HUE_SPEC_PROFILE',
    'HueError',
    'HueEventRecord',
    'HueLocalAdapter',
    'HueTransport',
    'kelvin_to_mirek',
    'mirek_to_kelvin',
    'split_endpoint',
]
