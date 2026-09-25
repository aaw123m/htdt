"""Device adapter compatibility matrix and conformance harness (#792).

Answers exactly which HTDT operations are supported and verified on a
device model + firmware + adapter version — never a generic
manufacturer-level "supported" badge.

- ``DeviceCompatibilityMatrix`` rows pin
  (adapter_id/version, manufacturer, model, firmware, transport,
  capability, direction, verification tier, interface provenance, last
  qualification evidence). Unknown firmware never inherits a VERIFIED
  tier — it resolves to ``NEEDS_REQUALIFICATION``.
- ``run_conformance_harness`` executes a deterministic contract over
  sanitized fixtures (capability parsing, materialization,
  quantization surfacing, binding gating, operator confirmation,
  read-back normalization). Fixture verification is a *separate tier*
  from hardware verification — a user-contributed fixture can raise
  ``FIXTURE_VERIFIED`` but never ``HARDWARE_VERIFIED``.
- ``DeviceQualificationRecord`` persists per-firmware hardware
  qualification evidence; a later firmware update creates a new row,
  never overwrites old evidence.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_calibration import CadCalibrationExportSnapshot
from .cad_device_adapter import (
    AdapterCapabilityError,
    AdapterDeviceBinding,
    CalibrationAdapterService,
    DeviceBindingMismatchError,
)


NormalizedDeviceCapability = Literal[
    'discover',
    'bind_exact_target',
    'firmware_model_readback',
    'power',
    'input_select',
    'mute',
    'master_volume',
    'decoder_mode',
    'speaker_configuration',
    'channel_output_mapping',
    'bass_management',
    'crossover',
    'sub_routing',
    'trim_gain',
    'distance_delay',
    'polarity',
    'peq',
    'filter_count_type',
    'target_profile',
    'command_ack',
    'readback',
    'exact_setting_comparison',
    'drift_detection',
]

CapabilityTier = Literal[
    'UNSUPPORTED',
    'DOCUMENTED_ONLY',
    'SOFTWARE_IMPLEMENTED',
    'FIXTURE_VERIFIED',
    'HARDWARE_VERIFIED',
    'HARDWARE_READBACK_VERIFIED',
    'UNKNOWN',
]

InterfaceProvenance = Literal[
    'official_public_api',
    'official_custom_install_api',
    'documented_file_format',
    'supported_local_config',
    'user_attested_manual',
    'experimental_undocumented',
]

_CAPABILITY_DIRECTIONS = ('read', 'write', 'export')
_TIER_ORDER = (
    'UNSUPPORTED',
    'DOCUMENTED_ONLY',
    'SOFTWARE_IMPLEMENTED',
    'FIXTURE_VERIFIED',
    'HARDWARE_VERIFIED',
    'HARDWARE_READBACK_VERIFIED',
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict[str, Any]) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


class DeviceCompatibilityRow(BaseModel):
    """One (adapter, device, firmware, transport, capability, direction)
    cell — qualification is always firmware-scoped."""

    model_config = ConfigDict(frozen=True)

    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    manufacturer: str = Field(min_length=1)
    model: str = Field(min_length=1)
    firmware_version: str = Field(min_length=1)
    transport: str = Field(min_length=1)
    capability: NormalizedDeviceCapability
    direction: Literal['read', 'write', 'export']
    tier: CapabilityTier
    interface_provenance: InterfaceProvenance
    qualification_evidence_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    qualified_at_utc: str | None = None
    notes: str = ''

    @model_validator(mode='after')
    def hardware_tiers_need_evidence(self) -> 'DeviceCompatibilityRow':
        if self.tier in ('HARDWARE_VERIFIED', 'HARDWARE_READBACK_VERIFIED'):
            if self.qualification_evidence_sha256 is None:
                raise ValueError(
                    'hardware verification tiers require a '
                    'DeviceQualificationRecord evidence hash'
                )
        return self


class DeviceCompatibilityMatrix(BaseModel):
    """Sealed set of compatibility rows (#792 §1-2)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    matrix_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    rows: tuple[DeviceCompatibilityRow, ...]
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_matrix(self) -> 'DeviceCompatibilityMatrix':
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('compatibility matrix hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'matrix_id': self.matrix_id,
            'created_at_utc': self.created_at_utc,
            'rows': [row.model_dump(mode='json') for row in self.rows],
        }

    def lookup(
        self,
        *,
        adapter_id: str,
        model: str,
        firmware_version: str,
        capability: NormalizedDeviceCapability,
        direction: str,
    ) -> DeviceCompatibilityRow | None:
        for row in self.rows:
            if (
                row.adapter_id == adapter_id
                and row.model == model
                and row.firmware_version == firmware_version
                and row.capability == capability
                and row.direction == direction
            ):
                return row
        return None

    def model_rows(
        self, *, adapter_id: str, model: str
    ) -> tuple[DeviceCompatibilityRow, ...]:
        return tuple(
            row
            for row in self.rows
            if row.adapter_id == adapter_id and row.model == model
        )


class FirmwareCapabilityResolution(BaseModel):
    """Resolution of one capability cell against an observed firmware
    version (#792 §9 — firmware drift)."""

    model_config = ConfigDict(frozen=True)

    state: Literal[
        'UNSUPPORTED',
        'DOCUMENTED_ONLY',
        'SOFTWARE_IMPLEMENTED',
        'FIXTURE_VERIFIED',
        'HARDWARE_VERIFIED',
        'HARDWARE_READBACK_VERIFIED',
        'UNKNOWN',
        'NEEDS_REQUALIFICATION',
    ]
    row: DeviceCompatibilityRow | None
    note: str = ''


def resolve_firmware_capability(
    matrix: DeviceCompatibilityMatrix,
    *,
    adapter_id: str,
    model: str,
    observed_firmware: str,
    capability: NormalizedDeviceCapability,
    direction: str,
) -> FirmwareCapabilityResolution:
    """Resolve exact qualification state for one capability on observed
    firmware. A capability qualified on different firmware returns
    ``NEEDS_REQUALIFICATION`` — read-only probing may continue where the
    adapter itself proves it, writes stay policy-gated."""
    row = matrix.lookup(
        adapter_id=adapter_id,
        model=model,
        firmware_version=observed_firmware,
        capability=capability,
        direction=direction,
    )
    if row is not None:
        return FirmwareCapabilityResolution(state=row.tier, row=row)
    siblings = [
        r
        for r in matrix.model_rows(adapter_id=adapter_id, model=model)
        if r.capability == capability and r.direction == direction
    ]
    if siblings:
        known = ', '.join(sorted({r.firmware_version for r in siblings}))
        return FirmwareCapabilityResolution(
            state='NEEDS_REQUALIFICATION',
            row=None,
            note=(
                f'adapter implemented; capability qualified on firmware '
                f'{known}, observed {observed_firmware}'
            ),
        )
    return FirmwareCapabilityResolution(
        state='UNKNOWN',
        row=None,
        note='no qualification row for this model/capability',
    )


class DeviceQualificationRecord(BaseModel):
    """Real-device qualification evidence (#792 §5). Immutable; a later
    firmware update creates a new record, never overwrites this one."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    record_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    firmware_version: str = Field(min_length=1)
    app_version: str = Field(min_length=1)
    connection_mode: str = Field(min_length=1)
    capability_results: tuple[tuple[str, str, str], ...]
    raw_evidence_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    qualified_at_utc: str = Field(min_length=1)
    notes: str = ''
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_record(self) -> 'DeviceQualificationRecord':
        if self.record_sha256 != _hash(self.identity_payload()):
            raise ValueError('qualification record hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'record_id': self.record_id,
            'adapter_id': self.adapter_id,
            'adapter_version': self.adapter_version,
            'model': self.model,
            'firmware_version': self.firmware_version,
            'app_version': self.app_version,
            'connection_mode': self.connection_mode,
            'capability_results': [
                list(item) for item in self.capability_results
            ],
            'raw_evidence_sha256': self.raw_evidence_sha256,
            'qualified_at_utc': self.qualified_at_utc,
            'notes': self.notes,
        }


class ConformanceCase(BaseModel):
    """One deterministic contract check over a sanitized fixture
    (#792 §3-4). Fixtures hold legal/non-secret content only —
    documented payloads, generated mock responses, expected states."""

    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    capability: NormalizedDeviceCapability
    direction: Literal['read', 'write', 'export']
    operation: Literal[
        'capability_report',
        'materialize_deterministic',
        'unsupported_stage_blocked',
        'binding_mismatch_blocked',
        'operator_confirmation_required',
        'readback_normalize',
        'unsupported_items_surfaced',
    ]
    export: CadCalibrationExportSnapshot | None = None
    binding: AdapterDeviceBinding | None = None
    wrong_binding: AdapterDeviceBinding | None = None
    expected: dict[str, Any] = Field(default_factory=dict)


class ConformanceCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    direction: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    status: Literal['PASS', 'FAIL', 'UNKNOWN']
    detail: str = ''


class AdapterConformanceReport(BaseModel):
    """Per-case results of a harness run — fixture tier only, never
    hardware verification."""

    model_config = ConfigDict(frozen=True)

    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    verification_tier: Literal['FIXTURE_VERIFIED'] = 'FIXTURE_VERIFIED'
    generated_at_utc: str = Field(min_length=1)
    results: tuple[ConformanceCaseResult, ...]

    @property
    def passed(self) -> bool:
        return all(r.status == 'PASS' for r in self.results)


def run_conformance_harness(
    adapter: Any,
    cases: tuple[ConformanceCase, ...],
    *,
    generated_at_utc: str,
    calibration_repository: Any = None,
) -> AdapterConformanceReport:
    """Run the deterministic conformance contract against one
    ``CalibrationDeviceAdapter`` over sanitized fixtures (#792 §3).

    ``UNKNOWN`` marks a case the harness could not evaluate (missing
    fixture inputs) — never counted as a pass. ``calibration_repository``
    supplies the persisted calibration authority observed-state cases
    (``readback_normalize``) require under the #865 contract.
    """
    service = CalibrationAdapterService(
        adapter, calibration_repository=calibration_repository
    )
    results: list[ConformanceCaseResult] = []

    for case in cases:
        status: Literal['PASS', 'FAIL', 'UNKNOWN'] = 'UNKNOWN'
        detail = ''
        try:
            if case.operation == 'capability_report':
                if not case.expected:
                    detail = 'expected capability subset is required'
                else:
                    report = service.capability()
                    actual = {
                        'adapter_kind': report.adapter_kind,
                        'supports_apply': report.supports_apply,
                        'supports_read_back': report.supports_read_back,
                        'supports_materialization': (
                            report.supports_materialization
                        ),
                    }
                    mismatches = {
                        k: (v, actual.get(k))
                        for k, v in case.expected.items()
                        if actual.get(k) != v
                    }
                    if mismatches:
                        status = 'FAIL'
                        detail = f'capability mismatch: {mismatches}'
                    else:
                        status = 'PASS'

            elif case.operation == 'materialize_deterministic':
                if case.export is None or case.binding is None:
                    detail = 'export and binding fixtures required'
                else:
                    first = service.materialize_export(
                        case.export, case.binding, created_at_utc='case-time'
                    )
                    second = service.materialize_export(
                        case.export, case.binding, created_at_utc='case-time'
                    )
                    if first.payload_text != second.payload_text:
                        status = 'FAIL'
                        detail = 'payload bytes differ between identical runs'
                    else:
                        status = 'PASS'

            elif case.operation == 'unsupported_stage_blocked':
                stage = case.expected.get('stage', 'apply')
                if stage == 'apply':
                    if case.export is None or case.binding is None:
                        detail = 'export and binding fixtures required'
                    else:
                        materialization = adapter.materialize(
                            case.export,
                            case.binding,
                            created_at_utc='case-time',
                        )
                        try:
                            service.apply_materialization(
                                materialization,
                                case.binding,
                                operator_confirmed=True,
                                applied_at_utc='case-time',
                            )
                            status = 'FAIL'
                            detail = 'apply succeeded although unsupported'
                        except AdapterCapabilityError:
                            status = 'PASS'
                elif stage == 'read_back':
                    if case.binding is None:
                        detail = 'binding fixture required'
                    else:
                        try:
                            adapter.read_back(
                                case.binding, observed_at_utc='case-time'
                            )
                            status = 'FAIL'
                            detail = 'read_back succeeded although unsupported'
                        except AdapterCapabilityError:
                            status = 'PASS'
                else:
                    detail = f'unknown stage {stage!r}'

            elif case.operation == 'binding_mismatch_blocked':
                if case.export is None or case.wrong_binding is None:
                    detail = 'export and wrong_binding fixtures required'
                else:
                    try:
                        service.materialize_export(
                            case.export,
                            case.wrong_binding,
                            created_at_utc='case-time',
                        )
                        status = 'FAIL'
                        detail = 'materialization accepted a foreign binding'
                    except DeviceBindingMismatchError:
                        status = 'PASS'

            elif case.operation == 'operator_confirmation_required':
                if case.export is None or case.binding is None:
                    detail = 'export and binding fixtures required'
                else:
                    capability = service.capability()
                    if not capability.supports_apply:
                        status = 'UNKNOWN'
                        detail = 'adapter does not support apply at all'
                    else:
                        materialization = service.materialize_export(
                            case.export,
                            case.binding,
                            created_at_utc='case-time',
                        )
                        try:
                            service.apply_materialization(
                                materialization,
                                case.binding,
                                operator_confirmed=False,
                                applied_at_utc='case-time',
                            )
                            status = 'FAIL'
                            detail = 'apply allowed without operator confirm'
                        except PermissionError:
                            status = 'PASS'

            elif case.operation == 'readback_normalize':
                if case.binding is None or case.export is None:
                    detail = 'binding and export fixtures required'
                else:
                    snapshot = service.read_back_snapshot(
                        case.binding,
                        case.export,
                        document_id=case.expected.get(
                            'document_id', 'conformance-doc'
                        ),
                        observed_at_utc='case-time',
                    )
                    expected_channels = case.expected.get('channel_ids')
                    actual_ids = [
                        c.channel_id for c in snapshot.observed_channels
                    ]
                    if expected_channels is not None and sorted(
                        expected_channels
                    ) != sorted(actual_ids):
                        status = 'FAIL'
                        detail = (
                            f'read-back channels {actual_ids} != '
                            f'{expected_channels}'
                        )
                    elif not snapshot.observed_channels:
                        status = 'FAIL'
                        detail = 'read-back produced no channels'
                    else:
                        status = 'PASS'

            elif case.operation == 'unsupported_items_surfaced':
                if case.export is None or case.binding is None:
                    detail = 'export and binding fixtures required'
                else:
                    materialization = service.materialize_export(
                        case.export, case.binding, created_at_utc='case-time'
                    )
                    minimum = case.expected.get('min_unsupported_items', 1)
                    if len(materialization.unsupported_items) >= minimum:
                        status = 'PASS'
                    else:
                        status = 'FAIL'
                        detail = (
                            f'unsupported items {len(materialization.unsupported_items)} '
                            f'< expected {minimum}'
                        )
            else:
                detail = f'unknown operation {case.operation!r}'
        except Exception as exc:  # harness records, never crashes
            status = 'FAIL'
            detail = f'{type(exc).__name__}: {exc}'

        results.append(
            ConformanceCaseResult(
                case_id=case.case_id,
                capability=case.capability,
                direction=case.direction,
                operation=case.operation,
                status=status,
                detail=detail,
            )
        )

    capability = service.capability()
    return AdapterConformanceReport(
        adapter_id=capability.adapter_id,
        adapter_version=capability.adapter_version,
        generated_at_utc=generated_at_utc,
        results=tuple(results),
    )


__all__ = [
    'AdapterConformanceReport',
    'CapabilityTier',
    'ConformanceCase',
    'ConformanceCaseResult',
    'DeviceCompatibilityMatrix',
    'DeviceCompatibilityRow',
    'DeviceQualificationRecord',
    'FirmwareCapabilityResolution',
    'InterfaceProvenance',
    'NormalizedDeviceCapability',
    'resolve_firmware_capability',
    'run_conformance_harness',
]
