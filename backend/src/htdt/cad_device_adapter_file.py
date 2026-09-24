"""Offline file adapter for calibration settings (#609 Stage A).

``FileCalibrationAdapter`` is the reference implementation of the
:class:`CalibrationDeviceAdapter` contract for devices that consume a
settings file the operator loads out-of-band (USB, vendor app, web UI):

- ``materialize`` renders the exported channel settings into a canonical
  JSON payload written into a target directory, and surfaces every
  pre-apply limitation (unmapped routing, quantization) in the record;
- ``apply`` is *not* supported: writing a file never mutates a device, so
  the capability is declared false rather than faked;
- ``read_back`` reads a device-captured JSON file the operator places at
  ``<root>/readback.json`` — the operator transports it, the adapter only
  parses and binds it.

Deterministic: same export + same binding ⇒ identical payload bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .cad_calibration import CadCalibrationExportSnapshot, CadExportedChannelSettings
from .cad_device_adapter import (
    AdapterCapabilityError,
    AdapterCapabilityReport,
    AdapterDeviceBinding,
    DeviceApplyAck,
    MaterializedCalibrationSettings,
    _hash,
)


FILE_ADAPTER_ID = 'htdt-file-adapter'
FILE_ADAPTER_VERSION = '1'
FILE_READBACK_NAME = 'readback.json'


class _ReadbackFile(BaseModel):
    """The exact shape the device-side read-back file must carry."""

    model_config = ConfigDict(frozen=True)

    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    channels: tuple[CadExportedChannelSettings, ...]


class FileCalibrationAdapter:
    """Offline file adapter — deterministic fixture for the contract."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def capability(self) -> AdapterCapabilityReport:
        return AdapterCapabilityReport(
            adapter_id=FILE_ADAPTER_ID,
            adapter_version=FILE_ADAPTER_VERSION,
            adapter_kind='offline_file',
            device_family='generic-file-target',
            supports_apply=False,
            supports_read_back=True,
            supports_materialization=True,
            notes=(
                'Offline file transport; live mutation is not supported.',
                'Read-back uses an operator-placed device capture file.',
            ),
        )

    def materialize(
        self,
        export: CadCalibrationExportSnapshot,
        binding: AdapterDeviceBinding,
        *,
        created_at_utc: str,
    ) -> MaterializedCalibrationSettings:
        routed_channels = {entry[0] for entry in binding.routing}
        unsupported = tuple(
            f'{channel.channel_id}: no routing on bound device'
            for channel in export.channels
            if channel.channel_id not in routed_channels
        )
        payload_text = json.dumps(
            {
                'adapter_id': FILE_ADAPTER_ID,
                'adapter_version': FILE_ADAPTER_VERSION,
                'binding': binding.semantic_payload(),
                'export_id': export.export_id,
                'exported_settings_semantic_sha256': (
                    export.exported_settings_semantic_sha256
                ),
                'sample_rate_hz': export.sample_rate_hz,
                'channels': [
                    item.model_dump(mode='json') for item in export.channels
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ) + '\n'
        materialization_id = 'mat:' + _hash(
            {
                'adapter_id': FILE_ADAPTER_ID,
                'binding_sha256': binding.binding_sha256,
                'export_sha256': export.exported_settings_semantic_sha256,
            }
        )[:32]
        payload: dict[str, Any] = {
            'materialization_id': materialization_id,
            'created_at_utc': created_at_utc,
            'adapter_id': FILE_ADAPTER_ID,
            'adapter_version': FILE_ADAPTER_VERSION,
            'export_id': export.export_id,
            'exported_settings_semantic_sha256': (
                export.exported_settings_semantic_sha256
            ),
            'binding_id': binding.binding_id,
            'binding_sha256': binding.binding_sha256,
            'payload_text': payload_text,
            'channel_settings': export.channels,
            'quantization_applied': export.quantization_applied,
            'quantization_notes': export.quantization_notes,
            'unsupported_items': unsupported,
        }
        provisional = MaterializedCalibrationSettings.model_construct(
            **payload, materialization_sha256='0' * 64
        )
        materialization = MaterializedCalibrationSettings(
            **payload,
            materialization_sha256=_hash(provisional.semantic_payload()),
        )
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / f'{materialization.materialization_id}.json').write_text(
            payload_text, encoding='utf-8'
        )
        return materialization

    def apply(
        self,
        materialization: MaterializedCalibrationSettings,
        binding: AdapterDeviceBinding,
        *,
        operator_confirmed: bool,
        applied_at_utc: str,
    ) -> DeviceApplyAck:
        raise AdapterCapabilityError(
            f'{FILE_ADAPTER_ID} cannot apply settings to a device; '
            'load the materialized file with vendor tooling'
        )

    def read_back(
        self,
        binding: AdapterDeviceBinding,
        *,
        observed_at_utc: str,
    ) -> tuple[CadExportedChannelSettings, ...]:
        path = self._root / FILE_READBACK_NAME
        if not path.is_file():
            raise AdapterCapabilityError(
                f'no read-back capture at {path}; place a device-captured '
                f'{FILE_READBACK_NAME} there'
            )
        try:
            file = _ReadbackFile.model_validate_json(
                path.read_text(encoding='utf-8')
            )
        except ValidationError as exc:
            raise AdapterCapabilityError(
                f'read-back file {path} does not match the capture schema'
            ) from exc
        if file.binding_sha256 != binding.binding_sha256:
            raise AdapterCapabilityError(
                'read-back capture is bound to a different device identity/'
                'version/routing — refusing to reinterpret it'
            )
        return file.channels


__all__ = [
    'FILE_ADAPTER_ID',
    'FILE_ADAPTER_VERSION',
    'FILE_READBACK_NAME',
    'FileCalibrationAdapter',
]
