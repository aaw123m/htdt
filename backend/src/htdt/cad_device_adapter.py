"""Calibration device adapter framework (#609).

An adapter connects a supported device family to the calibration
authority's *output formats* — it may materialize an export into a
device-native payload, optionally apply settings live, and optionally read
the effective settings back. An adapter never defines what a
CalibrationPlan means and never becomes evidence authority.

Authority boundary (per the issue contract):

- every read/write goes through the explicit
  :class:`CalibrationDeviceAdapter` interface — capability, materialize,
  optional apply, optional read-back;
- anything the device cannot represent (unsupported filters, quantized
  values) is surfaced *before* Apply via
  :class:`MaterializedCalibrationSettings` (quantization notes, unsupported
  items, operator-viewable payload) — never silently after;
- a live mutation requires ``operator_confirmed=True`` and produces a
  :class:`DeviceApplyAck` — acknowledgment is NOT a read-back;
- the effective installed state is a separate authority:
  :class:`EffectiveAppliedSettingsSnapshot` pins the device identity and
  carries *observed* settings (adapter read-back or operator entry), never
  the exported payload restated;
- when the device drifts from an observed snapshot, a new observation is
  recorded — drift never edits a prior snapshot;
- device identity + version + routing are bound into every record, so a
  snapshot can never be silently reinterpreted on a different unit;
- credentials and device secrets live outside project authority — the
  adapter contract never stores them in the project database.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_calibration import CadCalibrationExportSnapshot, CadExportedChannelSettings
from .cad_calibration_workflow import AppliedSettingsDeviation


ADAPTER_SCHEMA_VERSION = 1
ADAPTER_AUTHORITY_VERSION = 'calibration-device-adapter-1'

AdapterKind = Literal['offline_file', 'network_api', 'simulated']

ObservedSettingsSource = Literal['read_back', 'operator_entered']


class AdapterCapabilityError(RuntimeError):
    """The adapter does not support the requested operation."""


class DeviceBindingMismatchError(ValueError):
    """A record is bound to a different device identity/version/routing."""


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


class AdapterDeviceBinding(BaseModel):
    """The exact device a materialization/observation is bound to.

    Identity = family + model + serial + firmware + the routing map the
    settings were written for. Two different units or firmware generations
    are different bindings — records never drift between them.
    """

    model_config = ConfigDict(frozen=True)

    binding_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    device_family: str = Field(min_length=1)
    device_model: str = Field(min_length=1)
    device_serial: str = Field(min_length=1)
    firmware_version: str = Field(min_length=1)
    #: channel_id -> physical output on this device.
    routing: tuple[tuple[str, str], ...] = ()
    bound_at_utc: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_binding(self) -> 'AdapterDeviceBinding':
        channel_ids = [entry[0] for entry in self.routing]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError('routing channel ids must be unique')
        outputs = [entry[1] for entry in self.routing]
        if len(outputs) != len(set(outputs)):
            raise ValueError('routing outputs must be unique')
        if self.binding_sha256 != _hash(self.semantic_payload()):
            raise ValueError('AdapterDeviceBinding hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'binding_id': self.binding_id,
            'adapter_id': self.adapter_id,
            'device_family': self.device_family,
            'device_model': self.device_model,
            'device_serial': self.device_serial,
            'firmware_version': self.firmware_version,
            'routing': [list(entry) for entry in self.routing],
            'bound_at_utc': self.bound_at_utc,
        }


def build_device_binding(
    *,
    adapter_id: str,
    device_family: str,
    device_model: str,
    device_serial: str,
    firmware_version: str,
    routing: tuple[tuple[str, str], ...],
    bound_at_utc: str,
    binding_id: str | None = None,
) -> AdapterDeviceBinding:
    payload: dict[str, Any] = {
        'binding_id': binding_id or str(uuid4()),
        'adapter_id': adapter_id,
        'device_family': device_family,
        'device_model': device_model,
        'device_serial': device_serial,
        'firmware_version': firmware_version,
        'routing': tuple(routing),
        'bound_at_utc': bound_at_utc,
    }
    provisional = AdapterDeviceBinding.model_construct(
        **payload, binding_sha256='0' * 64
    )
    return AdapterDeviceBinding(
        **payload,
        binding_sha256=_hash(provisional.semantic_payload()),
    )


class AdapterCapabilityReport(BaseModel):
    """What this adapter supports — the only way callers may decide."""

    model_config = ConfigDict(frozen=True)

    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    adapter_kind: AdapterKind
    device_family: str = Field(min_length=1)
    supports_apply: bool
    supports_read_back: bool
    #: Whether the adapter can materialize a payload for operator handling.
    supports_materialization: bool = True
    notes: tuple[str, ...] = ()


class MaterializedCalibrationSettings(BaseModel):
    """Device-native payload plus the operator-visible preview.

    Everything the device will receive — and everything it cannot honor
    (quantized values, dropped fields, unsupported filters) — is visible on
    this record *before* any apply happens.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ADAPTER_SCHEMA_VERSION
    materialization_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    export_id: str = Field(min_length=1)
    exported_settings_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    binding_id: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    #: The exact device-native payload text (file content, packet bytes
    #: rendered, ...). Opaque to the framework; the adapter defines it.
    payload_text: str
    #: Per-channel settings as they will land (post-quantization).
    channel_settings: tuple[CadExportedChannelSettings, ...]
    quantization_applied: bool
    quantization_notes: tuple[str, ...] = ()
    #: Device-side limits detected during materialization — fields the
    #: device will ignore or clamp, surfaced before apply.
    unsupported_items: tuple[str, ...] = ()
    materialization_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_materialization(self) -> 'MaterializedCalibrationSettings':
        if self.materialization_sha256 != _hash(self.semantic_payload()):
            raise ValueError('MaterializedCalibrationSettings hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'materialization_id': self.materialization_id,
            'created_at_utc': self.created_at_utc,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'export_id': self.export_id,
            'exported_settings_semantic_sha256': (
                self.exported_settings_semantic_sha256
            ),
            'binding_id': self.binding_id,
            'binding_sha256': self.binding_sha256,
            'payload_text': self.payload_text,
            'channel_settings': [
                item.model_dump(mode='json') for item in self.channel_settings
            ],
            'quantization_applied': self.quantization_applied,
            'quantization_notes': list(self.quantization_notes),
            'unsupported_items': list(self.unsupported_items),
        }


class DeviceApplyAck(BaseModel):
    """The adapter's acknowledgment that a write was issued.

    An ack is *not* evidence the settings are installed — the device's
    effective state is only ever established by an
    :class:`EffectiveAppliedSettingsSnapshot`.
    """

    model_config = ConfigDict(frozen=True)

    ack_id: str = Field(min_length=1)
    materialization_id: str = Field(min_length=1)
    acked_at_utc: str = Field(min_length=1)
    state: Literal['acknowledged'] = 'acknowledged'
    device_note: str | None = None


class EffectiveAppliedSettingsSnapshot(BaseModel):
    """Observed installed state on one bound device — separate authority.

    Derived strictly from an adapter read-back or an explicit operator
    entry; never produced by copying the export. ``deviations`` lists only
    the fields that differ from the pinned export (``export ⊕ deviations``
    = effective state), matching ``CadAppliedSettingsRecord`` semantics.
    """

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    export_id: str = Field(min_length=1)
    exported_settings_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    observed_channels: tuple[CadExportedChannelSettings, ...]
    observed_at_utc: str = Field(min_length=1)
    source: ObservedSettingsSource
    deviations: tuple[AppliedSettingsDeviation, ...] = ()
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_snapshot(self) -> 'EffectiveAppliedSettingsSnapshot':
        channel_ids = [item.channel_id for item in self.observed_channels]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError('observed channels must be unique')
        channel_field = tuple(
            (item.channel_id, item.field_name) for item in self.deviations
        )
        if len(channel_field) != len(set(channel_field)):
            raise ValueError('deviations must be unique per channel/field')
        if self.snapshot_sha256 != _hash(self.semantic_payload()):
            raise ValueError('EffectiveAppliedSettingsSnapshot hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'snapshot_id': self.snapshot_id,
            'document_id': self.document_id,
            'binding_sha256': self.binding_sha256,
            'export_id': self.export_id,
            'exported_settings_semantic_sha256': (
                self.exported_settings_semantic_sha256
            ),
            'observed_channels': [
                item.model_dump(mode='json') for item in self.observed_channels
            ],
            'observed_at_utc': self.observed_at_utc,
            'source': self.source,
            'deviations': [item.model_dump(mode='json') for item in self.deviations],
        }


def diff_observed_vs_exported(
    export: CadCalibrationExportSnapshot,
    observed_channels: tuple[CadExportedChannelSettings, ...],
) -> tuple[AppliedSettingsDeviation, ...]:
    """Field-level drift between the pinned export and observed settings."""

    deviations: list[AppliedSettingsDeviation] = []
    observed_by_id = {item.channel_id: item for item in observed_channels}
    for exported in export.channels:
        observed = observed_by_id.get(exported.channel_id)
        if observed is None:
            deviations.append(
                AppliedSettingsDeviation(
                    channel_id=exported.channel_id,
                    field_name='routing',
                    exported_value_repr='present',
                    applied_value_repr='missing',
                    reason='channel not observed on device',
                )
            )
            continue
        for field_name, exported_value, observed_value in (
            ('gain_db', exported.gain_db, observed.gain_db),
            ('delay_s', exported.delay_s, observed.delay_s),
            ('polarity', exported.polarity, observed.polarity),
        ):
            if exported_value != observed_value:
                deviations.append(
                    AppliedSettingsDeviation(
                        channel_id=exported.channel_id,
                        field_name=field_name,  # type: ignore[arg-type]
                        exported_value_repr=repr(exported_value),
                        applied_value_repr=repr(observed_value),
                    )
                )
        if exported.peq != observed.peq:
            deviations.append(
                AppliedSettingsDeviation(
                    channel_id=exported.channel_id,
                    field_name='peq',
                    exported_value_repr=repr(
                        [f.model_dump(mode='json') for f in exported.peq]
                    ),
                    applied_value_repr=repr(
                        [f.model_dump(mode='json') for f in observed.peq]
                    ),
                )
            )
        if exported.crossovers != observed.crossovers:
            deviations.append(
                AppliedSettingsDeviation(
                    channel_id=exported.channel_id,
                    field_name='crossover',
                    exported_value_repr=repr(
                        [c.model_dump(mode='json') for c in exported.crossovers]
                    ),
                    applied_value_repr=repr(
                        [c.model_dump(mode='json') for c in observed.crossovers]
                    ),
                )
            )
        if exported.routing != observed.routing:
            deviations.append(
                AppliedSettingsDeviation(
                    channel_id=exported.channel_id,
                    field_name='routing',
                    exported_value_repr=repr(list(exported.routing)),
                    applied_value_repr=repr(list(observed.routing)),
                )
            )
    for observed in observed_channels:
        if observed.channel_id not in {e.channel_id for e in export.channels}:
            deviations.append(
                AppliedSettingsDeviation(
                    channel_id=observed.channel_id,
                    field_name='routing',
                    exported_value_repr='absent',
                    applied_value_repr='present',
                    reason='channel observed on device but not exported',
                )
            )
    return tuple(deviations)


def build_observation(
    *,
    document_id: str,
    binding: AdapterDeviceBinding,
    export: CadCalibrationExportSnapshot,
    observed_channels: tuple[CadExportedChannelSettings, ...],
    observed_at_utc: str,
    source: ObservedSettingsSource,
    snapshot_id: str | None = None,
) -> EffectiveAppliedSettingsSnapshot:
    """Record one observed effective state — drift becomes a new record."""

    deviations = diff_observed_vs_exported(export, observed_channels)
    payload: dict[str, Any] = {
        'snapshot_id': snapshot_id or str(uuid4()),
        'document_id': document_id,
        'binding_sha256': binding.binding_sha256,
        'export_id': export.export_id,
        'exported_settings_semantic_sha256': (
            export.exported_settings_semantic_sha256
        ),
        'observed_channels': tuple(observed_channels),
        'observed_at_utc': observed_at_utc,
        'source': source,
        'deviations': deviations,
    }
    provisional = EffectiveAppliedSettingsSnapshot.model_construct(
        **payload, snapshot_sha256='0' * 64
    )
    return EffectiveAppliedSettingsSnapshot(
        **payload,
        snapshot_sha256=_hash(provisional.semantic_payload()),
    )


class CalibrationDeviceAdapter(Protocol):
    """The adapter contract — the only legal path to a device.

    ``supports_apply``/``supports_read_back`` declare optional stages; an
    unsupported stage raises :class:`AdapterCapabilityError`, never silently
    skips.
    """

    def capability(self) -> AdapterCapabilityReport: ...

    def materialize(
        self,
        export: CadCalibrationExportSnapshot,
        binding: AdapterDeviceBinding,
        *,
        created_at_utc: str,
    ) -> MaterializedCalibrationSettings: ...

    def apply(
        self,
        materialization: MaterializedCalibrationSettings,
        binding: AdapterDeviceBinding,
        *,
        operator_confirmed: bool,
        applied_at_utc: str,
    ) -> DeviceApplyAck: ...

    def read_back(
        self,
        binding: AdapterDeviceBinding,
        *,
        observed_at_utc: str,
    ) -> tuple[CadExportedChannelSettings, ...]: ...


def _assert_binding(adapter_id: str, binding: AdapterDeviceBinding) -> None:
    if binding.adapter_id != adapter_id:
        raise DeviceBindingMismatchError(
            f'binding is for adapter {binding.adapter_id}, not {adapter_id}'
        )


class CalibrationAdapterService:
    """Framework-level orchestration over a CalibrationDeviceAdapter.

    Enforces the contract centrally: capability gating, binding checks,
    and the explicit operator-confirmation requirement for live mutation.
    """

    def __init__(self, adapter: CalibrationDeviceAdapter) -> None:
        self.adapter = adapter
        self._capability = adapter.capability()

    def capability(self) -> AdapterCapabilityReport:
        return self._capability

    def materialize_export(
        self,
        export: CadCalibrationExportSnapshot,
        binding: AdapterDeviceBinding,
        *,
        created_at_utc: str,
    ) -> MaterializedCalibrationSettings:
        """Produce the device payload + preview — the pre-Apply surface."""

        _assert_binding(self._capability.adapter_id, binding)
        if not self._capability.supports_materialization:
            raise AdapterCapabilityError(
                f'{self._capability.adapter_id} cannot materialize settings'
            )
        return self.adapter.materialize(
            export, binding, created_at_utc=created_at_utc
        )

    def apply_materialization(
        self,
        materialization: MaterializedCalibrationSettings,
        binding: AdapterDeviceBinding,
        *,
        operator_confirmed: bool,
        applied_at_utc: str,
    ) -> DeviceApplyAck:
        """Issue a live write. Requires capability + explicit confirmation.

        Returns only an acknowledgment — the effective installed state must
        be established by ``read_back``/operator observation, never by this
        call.
        """

        _assert_binding(self._capability.adapter_id, binding)
        if binding.binding_sha256 != materialization.binding_sha256:
            raise DeviceBindingMismatchError(
                'materialization is bound to a different device'
            )
        if not self._capability.supports_apply:
            raise AdapterCapabilityError(
                f'{self._capability.adapter_id} cannot apply settings'
            )
        if not operator_confirmed:
            raise PermissionError(
                'applying calibration settings requires explicit operator '
                'confirmation'
            )
        return self.adapter.apply(
            materialization,
            binding,
            operator_confirmed=operator_confirmed,
            applied_at_utc=applied_at_utc,
        )

    def read_back_snapshot(
        self,
        binding: AdapterDeviceBinding,
        export: CadCalibrationExportSnapshot,
        *,
        document_id: str,
        observed_at_utc: str,
    ) -> EffectiveAppliedSettingsSnapshot:
        """Observe the effective installed state on the bound device."""

        _assert_binding(self._capability.adapter_id, binding)
        if not self._capability.supports_read_back:
            raise AdapterCapabilityError(
                f'{self._capability.adapter_id} cannot read back settings'
            )
        channels = self.adapter.read_back(binding, observed_at_utc=observed_at_utc)
        return build_observation(
            document_id=document_id,
            binding=binding,
            export=export,
            observed_channels=channels,
            observed_at_utc=observed_at_utc,
            source='read_back',
        )

    def record_operator_snapshot(
        self,
        binding: AdapterDeviceBinding,
        export: CadCalibrationExportSnapshot,
        *,
        document_id: str,
        observed_channels: tuple[CadExportedChannelSettings, ...],
        observed_at_utc: str,
    ) -> EffectiveAppliedSettingsSnapshot:
        """Explicit operator-entered read-back — same authority shape."""

        _assert_binding(self._capability.adapter_id, binding)
        return build_observation(
            document_id=document_id,
            binding=binding,
            export=export,
            observed_channels=observed_channels,
            observed_at_utc=observed_at_utc,
            source='operator_entered',
        )


__all__ = [
    'ADAPTER_AUTHORITY_VERSION',
    'ADAPTER_SCHEMA_VERSION',
    'AdapterCapabilityError',
    'AdapterCapabilityReport',
    'AdapterDeviceBinding',
    'AdapterKind',
    'CalibrationAdapterService',
    'CalibrationDeviceAdapter',
    'DeviceApplyAck',
    'DeviceBindingMismatchError',
    'EffectiveAppliedSettingsSnapshot',
    'MaterializedCalibrationSettings',
    'ObservedSettingsSource',
    'build_device_binding',
    'build_observation',
    'diff_observed_vs_exported',
]
