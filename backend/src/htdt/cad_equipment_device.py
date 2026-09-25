"""General equipment device adapter / observed-state framework (#726).

One bounded integration contract for live equipment (AVR/processor, DSP,
display/projector, media source, other controllable theater hardware) —
separate from the calibration-specific adapter owned by #609, which consumes
this layer's binding/observation semantics rather than duplicating them.

Authority boundary (per the issue contract):

- a :class:`DeviceTargetBinding` is *explicit*: an adapter binds one
  installed equipment instance to one concrete endpoint — never a fuzzy
  fallback to another device, and never a credential value (only a secret
  *reference name*);
- a :class:`DeviceCapabilitySnapshot` (what the device *can* do) and an
  :class:`ObservedDeviceState` (what the device *currently reports*) are
  separate immutable authorities — a new observation never rewrites an old
  one;
- :class:`DeviceActionAck` is a command acknowledgment, NOT verified
  state — verification is only :func:`verify_action_outcome` comparing the
  requested fields against a subsequent observation;
- unsupported fields are explicit (``'unsupported'``/``'unknown'``), never
  guessed defaults.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Protocol, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


DEVICE_FRAMEWORK_SCHEMA_VERSION = 1
DEVICE_FRAMEWORK_AUTHORITY_VERSION = 'equipment-device-framework-1'


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


DeviceKind = Literal[
    'avr',
    'processor',
    'dsp',
    'display',
    'projector',
    'media_source',
    'pdu',
    'other',
]

CapabilityState = Literal['supported', 'unsupported', 'unknown']
ObservedFieldState = Literal['observed', 'unsupported', 'unknown']


class DeviceFrameworkError(ValueError):
    """A device-framework record violated its contract."""


# ----------------------------------------------------------------------
# Binding

class DeviceTargetBinding(BaseModel):
    """Explicit binding of one installed equipment instance to one endpoint.

    ``endpoint`` is the adapter-specific target locator (an IP/host:port, a
    serial path, a fixture id). ``credential_ref`` is only the *name* of a
    secret in the local secret store — credentials never live inside the
    project authority.
    """

    model_config = ConfigDict(frozen=True)

    binding_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    installed_equipment_ref: str = Field(min_length=1)
    device_kind: DeviceKind
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    credential_ref: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_binding(self) -> 'DeviceTargetBinding':
        if self.credential_ref is not None and any(
            marker in self.credential_ref.lower()
            for marker in ('pass', 'token', 'secret_value', 'key=')
        ):
            raise ValueError(
                'credential_ref is a secret *name*, never a value'
            )
        if self.binding_sha256 != _hash(self.semantic_payload()):
            raise ValueError('DeviceTargetBinding hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'binding_id': self.binding_id,
            'document_id': self.document_id,
            'installed_equipment_ref': self.installed_equipment_ref,
            'device_kind': self.device_kind,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'endpoint': self.endpoint,
            'credential_ref': self.credential_ref,
            'created_at_utc': self.created_at_utc,
        }


# ----------------------------------------------------------------------
# Capability snapshot (what the device can do — immutable)

class DeviceCapabilityEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: str = Field(min_length=1)
    state: CapabilityState
    detail: str | None = None


class DeviceCapabilitySnapshot(BaseModel):
    """Immutable record of an adapter's capability probe for one binding."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    probed_at_utc: str = Field(min_length=1)
    capabilities: tuple[DeviceCapabilityEntry, ...]
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_snapshot(self) -> 'DeviceCapabilitySnapshot':
        keys = [item.capability for item in self.capabilities]
        if len(keys) != len(set(keys)):
            raise ValueError('capability ids must be unique in a snapshot')
        if self.snapshot_sha256 != _hash(self.semantic_payload()):
            raise ValueError('DeviceCapabilitySnapshot hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'snapshot_id': self.snapshot_id,
            'binding_sha256': self.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'probed_at_utc': self.probed_at_utc,
            'capabilities': [
                item.model_dump(mode='json') for item in self.capabilities
            ],
        }

    def capability_state(self, capability: str) -> CapabilityState:
        """Lookup one capability; absent means explicitly ``unknown``."""
        for item in self.capabilities:
            if item.capability == capability:
                return item.state
        return 'unknown'


# ----------------------------------------------------------------------
# Observed state (what the device reports now — immutable evidence)

class ObservedDeviceField(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str = Field(min_length=1)
    state: ObservedFieldState
    value: str | None = None

    @model_validator(mode='after')
    def valid_field(self) -> 'ObservedDeviceField':
        if self.state != 'observed' and self.value is not None:
            raise ValueError(
                'unsupported/unknown fields cannot carry a value'
            )
        if self.state == 'observed' and self.value is None:
            raise ValueError('an observed field must carry its value')
        return self


class ObservedDeviceState(BaseModel):
    """Append-only evidence of one device's reported state.

    ``raw_source_sha256`` pins the raw response bytes when the adapter
    produces them; ``limitations`` records known observation gaps.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    firmware_version: str | None = Field(default=None, min_length=1)
    observed_at_utc: str = Field(min_length=1)
    fields: tuple[ObservedDeviceField, ...]
    raw_source_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    limitations: tuple[str, ...] = ()
    observation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_observation(self) -> 'ObservedDeviceState':
        keys = [item.field for item in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError('observed field keys must be unique')
        if self.observation_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ObservedDeviceState hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'observation_id': self.observation_id,
            'binding_sha256': self.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'firmware_version': self.firmware_version,
            'observed_at_utc': self.observed_at_utc,
            'fields': [item.model_dump(mode='json') for item in self.fields],
            'raw_source_sha256': self.raw_source_sha256,
            'limitations': list(self.limitations),
        }

    def field(self, name: str) -> ObservedDeviceField | None:
        for item in self.fields:
            if item.field == name:
                return item
        return None


# ----------------------------------------------------------------------
# Actions (requested -> materialized -> ACK -> verified)

class DeviceActionRequest(BaseModel):
    """Domain-level requested change, before materialization."""

    model_config = ConfigDict(frozen=True)

    request: tuple[tuple[str, str], ...]
    reason: str | None = None

    @model_validator(mode='after')
    def valid_request(self) -> 'DeviceActionRequest':
        keys = [key for key, _ in self.request]
        if len(keys) != len(set(keys)):
            raise ValueError('requested field keys must be unique')
        return self


class DeviceCommand(BaseModel):
    """One adapter-materialized device command."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(min_length=1)
    command: str = Field(min_length=1)
    argument: str | None = None


class ProposedDeviceAction(BaseModel):
    """A request materialized into exact device commands — pre-apply."""

    model_config = ConfigDict(frozen=True)

    action_id: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    requested: tuple[tuple[str, str], ...]
    commands: tuple[DeviceCommand, ...]
    #: Requested fields the adapter cannot express — surfaced before apply,
    #: never silently dropped.
    unsupported: tuple[str, ...] = ()
    requires_operator_confirm: bool = True
    planned_at_utc: str = Field(min_length=1)
    action_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_action(self) -> 'ProposedDeviceAction':
        keys = [key for key, _ in self.requested]
        if len(keys) != len(set(keys)):
            raise ValueError('requested field keys must be unique')
        if self.action_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ProposedDeviceAction hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'action_id': self.action_id,
            'binding_sha256': self.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'requested': [list(pair) for pair in self.requested],
            'commands': [item.model_dump(mode='json') for item in self.commands],
            'unsupported': list(self.unsupported),
            'requires_operator_confirm': self.requires_operator_confirm,
            'planned_at_utc': self.planned_at_utc,
        }


class DeviceActionAck(BaseModel):
    """A command acknowledgment.

    ``accepted=True`` means the device accepted the command bytes — it says
    nothing about effective state until a subsequent observation proves it.
    """

    model_config = ConfigDict(frozen=True)

    ack_id: str = Field(min_length=1)
    action_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    acked_at_utc: str = Field(min_length=1)
    accepted: bool
    detail: str | None = None
    operator_confirmed: bool = False
    ack_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_ack(self) -> 'DeviceActionAck':
        if self.ack_sha256 != _hash(self.semantic_payload()):
            raise ValueError('DeviceActionAck hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'ack_id': self.ack_id,
            'action_sha256': self.action_sha256,
            'acked_at_utc': self.acked_at_utc,
            'accepted': self.accepted,
            'detail': self.detail,
            'operator_confirmed': self.operator_confirmed,
        }


ActionOutcome = Literal['verified', 'ack_only', 'not_accepted', 'field_mismatch']


def verify_action_outcome(
    action: ProposedDeviceAction,
    ack: DeviceActionAck | None,
    observation: ObservedDeviceState | None,
) -> ActionOutcome:
    """Classify an action's outcome without ever equating ACK with proof.

    ``verified`` requires the acknowledgment *and* a subsequent observation
    reporting every requested field at the requested value.
    """
    if ack is None or not ack.accepted:
        return 'not_accepted'
    if ack.action_sha256 != action.action_sha256:
        raise ValueError('acknowledgment does not belong to this action')
    if observation is None:
        return 'ack_only'
    if observation.binding_sha256 != action.binding_sha256:
        raise ValueError('observation does not belong to this binding')
    for field, expected in action.requested:
        observed = observation.field(field)
        if observed is None or observed.state != 'observed':
            return 'field_mismatch'
        if observed.value != expected:
            return 'field_mismatch'
    return 'verified'


# ----------------------------------------------------------------------
# Adapter contract

class EquipmentDeviceAdapter(Protocol):
    """One bounded integration contract per supported device family.

    ``plan_action``/``apply_action``/``read_back`` are optional — adapters
    that only observe raise :class:`NotImplementedError` or return ``None``
    and must report those capabilities as unsupported in
    ``probe_capabilities``.
    """

    adapter_id: str
    adapter_version: str
    supported_device_kind: DeviceKind

    def discover_targets(self) -> tuple[str, ...]:
        """Endpoints the adapter can see; empty when discovery is unsupported."""

    def probe_capabilities(
        self, binding: DeviceTargetBinding
    ) -> DeviceCapabilitySnapshot:
        ...

    def observe_state(self, binding: DeviceTargetBinding) -> ObservedDeviceState:
        ...

    def plan_action(
        self,
        binding: DeviceTargetBinding,
        request: DeviceActionRequest,
    ) -> ProposedDeviceAction | None:
        ...

    def apply_action(
        self,
        binding: DeviceTargetBinding,
        action: ProposedDeviceAction,
        *,
        operator_confirmed: bool,
    ) -> DeviceActionAck | None:
        ...

    def read_back(
        self, binding: DeviceTargetBinding
    ) -> ObservedDeviceState | None:
        ...


# ----------------------------------------------------------------------
# Deterministic fixture adapter (framework reference implementation)

class FixtureDeviceAdapter:
    """In-memory deterministic adapter for tests and offline demos.

    The fixture pretends to be one device per endpoint with a fixed
    capability map and mutable state, so the whole lifecycle — discover,
    bind, probe, observe, plan, apply, read-back, verify — is exercisable
    without hardware. It proves the *contract*, never a real device.
    """

    adapter_id = 'htdt-fixture-device'
    adapter_version = '1'

    def __init__(
        self,
        *,
        supported_device_kind: DeviceKind = 'avr',
        targets: dict[str, dict[str, str]] | None = None,
        capabilities: tuple[DeviceCapabilityEntry, ...] = (
            DeviceCapabilityEntry(capability='volume', state='supported'),
            DeviceCapabilityEntry(capability='input', state='supported'),
            DeviceCapabilityEntry(capability='peq', state='unsupported'),
        ),
    ) -> None:
        self.supported_device_kind = supported_device_kind
        self._state: dict[str, dict[str, str]] = {
            endpoint: dict(fields) for endpoint, fields in (targets or {}).items()
        }
        self._capabilities = capabilities

    def discover_targets(self) -> tuple[str, ...]:
        return tuple(sorted(self._state))

    def _require_target(self, binding: DeviceTargetBinding) -> dict[str, str]:
        if binding.adapter_id != self.adapter_id:
            raise DeviceFrameworkError(
                'binding belongs to a different adapter'
            )
        if binding.device_kind != self.supported_device_kind:
            raise DeviceFrameworkError(
                'binding device kind is not supported by this adapter'
            )
        target = self._state.get(binding.endpoint)
        if target is None:
            raise DeviceFrameworkError(
                f'endpoint {binding.endpoint} is not bound to this adapter'
            )
        return target

    def probe_capabilities(
        self, binding: DeviceTargetBinding
    ) -> DeviceCapabilitySnapshot:
        self._require_target(binding)
        payload: dict[str, Any] = {
            'snapshot_id': str(uuid4()),
            'binding_sha256': binding.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'probed_at_utc': binding.created_at_utc,
            'capabilities': tuple(self._capabilities),
        }
        provisional = DeviceCapabilitySnapshot.model_construct(
            **payload, snapshot_sha256='0' * 64
        )
        return DeviceCapabilitySnapshot(
            **payload, snapshot_sha256=_hash(provisional.semantic_payload())
        )

    def observe_state(self, binding: DeviceTargetBinding) -> ObservedDeviceState:
        target = self._require_target(binding)
        supported = {
            item.capability
            for item in self._capabilities
            if item.state == 'supported'
        }
        fields = tuple(
            ObservedDeviceField(
                field=name,
                state='observed' if name in supported else 'unsupported',
                value=value if name in supported else None,
            )
            for name, value in sorted(target.items())
        )
        raw = _canonical_json(target).encode('utf-8')
        payload: dict[str, Any] = {
            'observation_id': str(uuid4()),
            'binding_sha256': binding.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'firmware_version': target.get('firmware'),
            'observed_at_utc': binding.created_at_utc,
            'fields': fields,
            'raw_source_sha256': sha256(raw).hexdigest(),
            'limitations': (),
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
        self._require_target(binding)
        supported = {
            item.capability
            for item in self._capabilities
            if item.state == 'supported'
        }
        commands = tuple(
            DeviceCommand(field=field, command='set', argument=value)
            for field, value in request.request
            if field in supported
        )
        unsupported = tuple(
            field for field, _ in request.request if field not in supported
        )
        payload: dict[str, Any] = {
            'action_id': str(uuid4()),
            'binding_sha256': binding.binding_sha256,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'requested': tuple(request.request),
            'commands': commands,
            'unsupported': unsupported,
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
        self._require_target(binding)
        if action.binding_sha256 != binding.binding_sha256:
            raise DeviceFrameworkError('action belongs to another binding')
        accepted = operator_confirmed
        if accepted:
            target = self._state[binding.endpoint]
            for command in action.commands:
                target[command.field] = command.argument or ''
        payload: dict[str, Any] = {
            'ack_id': str(uuid4()),
            'action_sha256': action.action_sha256,
            'acked_at_utc': binding.created_at_utc,
            'accepted': accepted,
            'detail': None if accepted else 'operator did not confirm',
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


def build_device_target_binding(
    *,
    document_id: str,
    installed_equipment_ref: str,
    device_kind: DeviceKind,
    adapter_id: str,
    adapter_version: str,
    endpoint: str,
    created_at_utc: str,
    credential_ref: str | None = None,
    binding_id: str | None = None,
) -> DeviceTargetBinding:
    payload: dict[str, Any] = {
        'binding_id': binding_id or str(uuid4()),
        'document_id': document_id,
        'installed_equipment_ref': installed_equipment_ref,
        'device_kind': device_kind,
        'adapter_id': adapter_id,
        'adapter_version': adapter_version,
        'endpoint': endpoint,
        'credential_ref': credential_ref,
        'created_at_utc': created_at_utc,
    }
    provisional = DeviceTargetBinding.model_construct(
        **payload, binding_sha256='0' * 64
    )
    return DeviceTargetBinding(
        **payload, binding_sha256=_hash(provisional.semantic_payload())
    )


__all__ = [
    'ActionOutcome',
    'CapabilityState',
    'DEVICE_FRAMEWORK_AUTHORITY_VERSION',
    'DEVICE_FRAMEWORK_SCHEMA_VERSION',
    'DeviceActionAck',
    'DeviceActionRequest',
    'DeviceCapabilityEntry',
    'DeviceCapabilitySnapshot',
    'DeviceCommand',
    'DeviceFrameworkError',
    'DeviceKind',
    'DeviceTargetBinding',
    'EquipmentDeviceAdapter',
    'FixtureDeviceAdapter',
    'ObservedDeviceField',
    'ObservedDeviceState',
    'ObservedFieldState',
    'ProposedDeviceAction',
    'build_device_target_binding',
    'verify_action_outcome',
]
