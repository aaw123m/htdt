"""Device configuration snapshot / restore authority (issue #592).

A commissioned system is only reproducible if the actual device
configuration and firmware state can be identified, backed up where
supported, restored, compared and re-verified. This module owns the
sealed records that make that state auditable:

- :class:`DeviceConfigurationSnapshot` — the sealed "what / when / from
  which version" record for one installed equipment instance. Identity
  pins (manufacturer / model / hardware revision / serial / firmware /
  module-license state / active preset slot) are individually optional —
  absent data stays UNKNOWN, never a guessed default. The snapshot embeds
  the observed setting fields plus a ``state_content_sha256`` over the
  normalized identity+field set, and links the evidence it was taken from
  (readback observation, vendor backup artifact, applied request).

- :class:`DeviceKnownGoodBaseline` — promotion of a verified snapshot to
  the immutable commissioned reference. Later changes create descendant
  snapshots; the baseline record is never edited.

- :class:`DeviceFirmwareTransition` — old→new firmware identity with
  pre/post state, migration result, rollback status and the explicit
  re-verification domain set. Firmware identity inside historical
  commissioning evidence is never rewritten.

- :class:`ConfigurationRestoreRecord` — a restore event with the
  compatibility checks, post-restore readback/diff and an honest verdict
  (``restored_exact_observed_state`` requires an identical semantic diff
  against the pinned reference — a vendor "Load OK" is never enough).

- :class:`ReplacementDeviceAssessment` — per-family portability classes
  for the same-model-new-serial service case; device-bound and
  license-bound content never silently transfers.

- :func:`evaluate_pre_update_gate` — the readiness checklist before a
  managed firmware/config update (backup captured, rollback documented,
  release notes reviewed…). False declarations block; unknowns stay
  UNKNOWN.

Composition with existing authorities:

- ``cad_equipment_device`` (#726): ``ObservedDeviceState`` /
  ``ObservedDeviceField`` are the raw observation evidence a snapshot
  pins; the snapshot is the durable configuration identity built on them.
- ``cad_device_backup`` (#1055): ``DeviceConfigurationBackupArtifact``
  carries the opaque vendor backup; ``evaluate_restore_compatibility``
  supplies the compatibility checks a restore record must embed.

Provenance DAG (issue §14): every snapshot names ``parent_snapshot_ids``
and a ``transition_kind`` edge label, so the lineage factory-default →
calibration → known-good → experiment/restore is append-only and
hash-linked, never a screenshot collection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_device_backup import RestoreCheckResult
from .cad_equipment_device import ObservedDeviceField
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


_SHA256 = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


# ---------------------------------------------------------------------------
# Taxonomy (#592 §2, §11, §10, §7, §12)
# ---------------------------------------------------------------------------

DeviceStateEvidenceClass = Literal[
    'device_readback',
    'device_export_backup',
    'htdt_applied_request',
    'user_recorded',
    'screenshot_documented',
    'inferred_from_measurement',
    'unknown',
]
"""Where the snapshot's claimed state comes from (#592 §2). A setting
HTDT *requested* is not proof the device applied it; an exported backup
is not proof of the active runtime state — the class keeps those
distinct."""

DeviceTransitionKind = Literal[
    'factory_default',
    'initial_configuration',
    'calibration',
    'manual_tuning',
    'known_good_promotion',
    'experiment',
    'firmware_transition',
    'restore',
    'rollback',
    'service',
    'other',
]
"""Lineage edge label in the configuration provenance DAG (#592 §14)."""

DeviceSensitivityClass = Literal[
    'may_contain_credentials',
    'no_credentials_expected',
    'unknown',
]
"""Secrets boundary (#592 §12): reports may expose status/hash, never
raw sensitive payload."""

DevicePortabilityClass = Literal[
    'portable_to_same_model',
    'portable_with_firmware_constraint',
    'device_instance_bound',
    'license_bound',
    'measurement_reuse_conditional',
    'not_portable',
    'unknown',
]
"""Whether a setting/artifact transfers to replacement hardware
(#592 §11). Matching model names never imply portability."""

FirmwareMigrationResult = Literal[
    'unchanged',
    'migrated',
    'partially_migrated',
    'reset_to_defaults',
    'unknown',
]
"""What the device did to the configuration across a firmware update."""

RollbackStatus = Literal[
    'available',
    'unavailable',
    'performed',
    'unknown',
]

RestoreResultStatus = Literal[
    'success',
    'partial',
    'failed',
    'not_attempted',
]

RestoreVerdict = Literal[
    'restored_exact_observed_state',
    'restored_with_differences',
    'restore_unverified',
    'restore_incompatible',
    'restore_failed',
]
"""Restore outcome (#592 §10). ``restored_exact_observed_state`` requires
a semantic diff against the pinned reference — never just tool success."""

FieldPresenceState = Literal['observed', 'unsupported', 'unknown', 'absent']

_BLOCKED_PORTABILITY = frozenset({
    'device_instance_bound',
    'license_bound',
    'not_portable',
    'unknown',
})
_CONDITIONAL_PORTABILITY = frozenset({
    'portable_with_firmware_constraint',
    'measurement_reuse_conditional',
})


# ---------------------------------------------------------------------------
# Snapshot (#592 §1, §14)
# ---------------------------------------------------------------------------


def snapshot_state_digest(
    *,
    manufacturer: str | None,
    model: str | None,
    hardware_revision: str | None,
    serial_number: str | None,
    firmware_version: str | None,
    module_feature_state: str | None,
    active_preset_slot: str | None,
    fields: tuple[ObservedDeviceField, ...],
) -> str:
    """Content/state hash over the normalized identity pin + field set —
    what ``diff_snapshots`` falls back to when semantic fields are
    unavailable (the opaque compare)."""
    normalized = {
        'manufacturer': manufacturer,
        'model': model,
        'hardware_revision': hardware_revision,
        'serial_number': serial_number,
        'firmware_version': firmware_version,
        'module_feature_state': module_feature_state,
        'active_preset_slot': active_preset_slot,
        'fields': [
            {
                'field': item.field,
                'state': item.state,
                'value': item.value,
            }
            for item in sorted(fields, key=lambda f: f.field)
        ],
    }
    return _hash(normalized)


class DeviceConfigurationSnapshot(BaseModel):
    """Sealed snapshot of one device's configuration identity.

    ``state_content_sha256`` seals the identity pins + observed fields;
    ``snapshot_sha256`` seals the whole record (evidence refs, lineage,
    limitations). A snapshot is immutable once stored — corrections are
    new snapshots with the old one as parent.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    snapshot_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    evidence_class: DeviceStateEvidenceClass
    manufacturer: str | None = None
    model: str | None = None
    hardware_revision: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None
    module_feature_state: str | None = None
    active_preset_slot: str | None = None
    fields: tuple[ObservedDeviceField, ...] = ()
    state_content_sha256: str = Field(pattern=_SHA256)
    observation_sha256: str | None = Field(default=None, pattern=_SHA256)
    backup_artifact_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    applied_request_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    capture_tool: str | None = None
    capture_tool_version: str | None = None
    captured_at_utc: str = Field(min_length=1)
    parent_snapshot_ids: tuple[str, ...] = ()
    transition_kind: DeviceTransitionKind
    actor: str | None = None
    reason: str = ''
    limitations: tuple[str, ...] = ()
    sensitivity: DeviceSensitivityClass = 'unknown'
    snapshot_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'snapshot_sha256', 'snapshot_id'}
        )

    def field(self, name: str) -> ObservedDeviceField | None:
        for item in self.fields:
            if item.field == name:
                return item
        return None

    @model_validator(mode='after')
    def _check(self) -> 'DeviceConfigurationSnapshot':
        _require_iso8601(self.captured_at_utc, 'captured_at_utc')
        keys = [item.field for item in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError('snapshot field keys must be unique')
        if self.evidence_class == 'device_readback' and not (
            self.fields or self.observation_sha256
        ):
            raise ValueError(
                'a device_readback snapshot needs observed fields or the '
                'observation it was taken from — an empty readback claim '
                'is meaningless'
            )
        if self.evidence_class == 'device_export_backup' and (
            self.backup_artifact_sha256 is None
        ):
            raise ValueError(
                'a device_export_backup snapshot must pin the backup '
                'artifact it was captured from'
            )
        if self.evidence_class == 'htdt_applied_request' and (
            self.applied_request_sha256 is None
        ):
            raise ValueError(
                'an htdt_applied_request snapshot must pin the applied '
                'request — a request is evidence of intent, not applied '
                'state'
            )
        expected = snapshot_state_digest(
            manufacturer=self.manufacturer,
            model=self.model,
            hardware_revision=self.hardware_revision,
            serial_number=self.serial_number,
            firmware_version=self.firmware_version,
            module_feature_state=self.module_feature_state,
            active_preset_slot=self.active_preset_slot,
            fields=self.fields,
        )
        if self.state_content_sha256 != expected:
            raise ValueError('snapshot state content hash mismatch')
        if self.snapshot_id in self.parent_snapshot_ids:
            raise ValueError('a snapshot cannot be its own parent')
        digest = _hash(self.semantic_payload())
        if self.snapshot_sha256 != digest:
            raise ValueError('device snapshot hash mismatch')
        if self.snapshot_id != 'devsnap-' + digest[:24]:
            raise ValueError('device snapshot id mismatch')
        return self


def build_device_snapshot(
    *,
    document_id: str,
    instance_id: str,
    evidence_class: DeviceStateEvidenceClass,
    transition_kind: DeviceTransitionKind,
    manufacturer: str | None = None,
    model: str | None = None,
    hardware_revision: str | None = None,
    serial_number: str | None = None,
    firmware_version: str | None = None,
    module_feature_state: str | None = None,
    active_preset_slot: str | None = None,
    fields: tuple[ObservedDeviceField, ...] = (),
    observation_sha256: str | None = None,
    backup_artifact_sha256: str | None = None,
    applied_request_sha256: str | None = None,
    capture_tool: str | None = None,
    capture_tool_version: str | None = None,
    captured_at_utc: str | None = None,
    parent_snapshot_ids: tuple[str, ...] = (),
    actor: str | None = None,
    reason: str = '',
    limitations: tuple[str, ...] = (),
    sensitivity: DeviceSensitivityClass = 'unknown',
) -> DeviceConfigurationSnapshot:
    fields = tuple(fields)
    content = snapshot_state_digest(
        manufacturer=manufacturer,
        model=model,
        hardware_revision=hardware_revision,
        serial_number=serial_number,
        firmware_version=firmware_version,
        module_feature_state=module_feature_state,
        active_preset_slot=active_preset_slot,
        fields=fields,
    )
    payload = dict(
        schema_version=1,
        snapshot_id='',
        document_id=document_id,
        instance_id=instance_id,
        evidence_class=evidence_class,
        manufacturer=manufacturer,
        model=model,
        hardware_revision=hardware_revision,
        serial_number=serial_number,
        firmware_version=firmware_version,
        module_feature_state=module_feature_state,
        active_preset_slot=active_preset_slot,
        fields=fields,
        state_content_sha256=content,
        observation_sha256=observation_sha256,
        backup_artifact_sha256=backup_artifact_sha256,
        applied_request_sha256=applied_request_sha256,
        capture_tool=capture_tool,
        capture_tool_version=capture_tool_version,
        captured_at_utc=captured_at_utc or _utc_now(),
        parent_snapshot_ids=tuple(parent_snapshot_ids),
        transition_kind=transition_kind,
        actor=actor,
        reason=reason,
        limitations=tuple(limitations),
        sensitivity=sensitivity,
        snapshot_sha256='0' * 64,
    )
    probe = DeviceConfigurationSnapshot.model_construct(
        **canonicalize_payload(DeviceConfigurationSnapshot, payload)
    )
    digest = _hash(probe.semantic_payload())
    return DeviceConfigurationSnapshot(
        **probe.model_dump(
            mode='python', exclude={'snapshot_sha256', 'snapshot_id'}
        ),
        snapshot_id='devsnap-' + digest[:24],
        snapshot_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Known-good baseline (#592 §5, §16)
# ---------------------------------------------------------------------------


class DeviceKnownGoodBaseline(BaseModel):
    """Promotion of a sufficiently verified snapshot to the immutable
    commissioned reference (#592 §5). Immutable — a changed configuration
    produces a new descendant snapshot and a new baseline."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    baseline_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=_SHA256)
    promoted_at_utc: str = Field(min_length=1)
    promotion_evidence_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    baseline_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'baseline_sha256', 'baseline_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'DeviceKnownGoodBaseline':
        _require_iso8601(self.promoted_at_utc, 'promoted_at_utc')
        digest = _hash(self.semantic_payload())
        if self.baseline_sha256 != digest:
            raise ValueError('known-good baseline hash mismatch')
        if self.baseline_id != 'devkg-' + digest[:24]:
            raise ValueError('known-good baseline id mismatch')
        return self


def evaluate_known_good_eligibility(
    snapshot: DeviceConfigurationSnapshot,
    *,
    backup_retained: bool,
    post_verification_passed: bool,
) -> tuple[bool, tuple[str, ...]]:
    """Whether a snapshot may be promoted to KNOWN_GOOD_REFERENCE
    (#592 §5). Fail-closed: every gap is a named reason."""
    reasons: list[str] = []
    if snapshot.evidence_class not in (
        'device_readback',
        'device_export_backup',
        'user_recorded',
        'screenshot_documented',
    ):
        reasons.append(
            f'evidence class {snapshot.evidence_class!r} cannot ground a '
            'known-good reference — only observed/exported/recorded state '
            'qualifies (applied requests and inference do not)'
        )
    if not snapshot.fields and snapshot.observation_sha256 is None and (
        snapshot.backup_artifact_sha256 is None
    ):
        reasons.append(
            'the snapshot carries no observed fields, observation ref or '
            'backup artifact — nothing is sufficiently observed'
        )
    if snapshot.firmware_version is None:
        reasons.append(
            'firmware identity is UNKNOWN — a known-good baseline must '
            'pin the exact firmware'
        )
    if not backup_retained:
        reasons.append(
            'no backup/export artifact is retained for this device — '
            'recovery would rely on re-entry, not a captured artifact'
        )
    if not post_verification_passed:
        reasons.append(
            'post-deployment measurement/verification has not passed for '
            'this state'
        )
    return (not reasons, tuple(reasons))


def build_known_good_baseline(
    *,
    snapshot: DeviceConfigurationSnapshot,
    promoted_at_utc: str | None = None,
    promotion_evidence_refs: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
) -> DeviceKnownGoodBaseline:
    payload = dict(
        schema_version=1,
        baseline_id='',
        document_id=snapshot.document_id,
        instance_id=snapshot.instance_id,
        snapshot_sha256=snapshot.snapshot_sha256,
        promoted_at_utc=promoted_at_utc or _utc_now(),
        promotion_evidence_refs=tuple(promotion_evidence_refs),
        limitations=tuple(limitations),
        baseline_sha256='0' * 64,
    )
    probe = DeviceKnownGoodBaseline.model_construct(
        **canonicalize_payload(DeviceKnownGoodBaseline, payload)
    )
    digest = _hash(probe.semantic_payload())
    return DeviceKnownGoodBaseline(
        **probe.model_dump(
            mode='python', exclude={'baseline_sha256', 'baseline_id'}
        ),
        baseline_id='devkg-' + digest[:24],
        baseline_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Firmware transition (#592 §7, §9)
# ---------------------------------------------------------------------------


class DeviceFirmwareTransition(BaseModel):
    """One qualification-relevant firmware transition. Historical
    commissioning evidence keeps its pinned firmware identity — this
    record describes the change, never rewrites it."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    transition_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    from_firmware: str | None = None
    """None = old version UNKNOWN — honest, never fabricated."""
    to_firmware: str = Field(min_length=1)
    update_source: str | None = None
    release_note_source: str | None = None
    updated_at_utc: str = Field(min_length=1)
    operator: str | None = None
    reason: str = ''
    pre_snapshot_sha256: str | None = Field(default=None, pattern=_SHA256)
    post_observation_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    migration_result: FirmwareMigrationResult = 'unknown'
    rollback_status: RollbackStatus = 'unknown'
    reverify_domains: tuple[str, ...] = ()
    """Device-dependent domains that must re-verify after this update
    (#592 §9) — recorded explicitly; unknown impact errs toward
    REVERIFY_REQUIRED for affected domains."""
    transition_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'transition_sha256', 'transition_id'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'DeviceFirmwareTransition':
        _require_iso8601(self.updated_at_utc, 'updated_at_utc')
        if (
            self.from_firmware is not None
            and self.from_firmware == self.to_firmware
        ):
            raise ValueError(
                'a firmware transition must change the version — '
                'identical versions are no transition'
            )
        digest = _hash(self.semantic_payload())
        if self.transition_sha256 != digest:
            raise ValueError('firmware transition hash mismatch')
        if self.transition_id != 'devfw-' + digest[:24]:
            raise ValueError('firmware transition id mismatch')
        return self


def build_firmware_transition(
    *,
    document_id: str,
    instance_id: str,
    to_firmware: str,
    from_firmware: str | None = None,
    update_source: str | None = None,
    release_note_source: str | None = None,
    updated_at_utc: str | None = None,
    operator: str | None = None,
    reason: str = '',
    pre_snapshot_sha256: str | None = None,
    post_observation_sha256: str | None = None,
    migration_result: FirmwareMigrationResult = 'unknown',
    rollback_status: RollbackStatus = 'unknown',
    reverify_domains: tuple[str, ...] = (),
) -> DeviceFirmwareTransition:
    payload = dict(
        schema_version=1,
        transition_id='',
        document_id=document_id,
        instance_id=instance_id,
        from_firmware=from_firmware,
        to_firmware=to_firmware,
        update_source=update_source,
        release_note_source=release_note_source,
        updated_at_utc=updated_at_utc or _utc_now(),
        operator=operator,
        reason=reason,
        pre_snapshot_sha256=pre_snapshot_sha256,
        post_observation_sha256=post_observation_sha256,
        migration_result=migration_result,
        rollback_status=rollback_status,
        reverify_domains=tuple(reverify_domains),
        transition_sha256='0' * 64,
    )
    probe = DeviceFirmwareTransition.model_construct(
        **canonicalize_payload(DeviceFirmwareTransition, payload)
    )
    digest = _hash(probe.semantic_payload())
    return DeviceFirmwareTransition(
        **probe.model_dump(
            mode='python', exclude={'transition_sha256', 'transition_id'}
        ),
        transition_id='devfw-' + digest[:24],
        transition_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Configuration diff (#592 §6)
# ---------------------------------------------------------------------------


class ConfigurationFieldDiff(BaseModel):
    """One field's before→after state. ``absent`` marks a field present on
    only one side — distinct from ``unknown``."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(min_length=1)
    before_state: FieldPresenceState
    after_state: FieldPresenceState
    before_value: str | None = None
    after_value: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'ConfigurationFieldDiff':
        if self.before_state != 'observed' and self.before_value is not None:
            raise ValueError(
                'a non-observed before-state cannot carry a value'
            )
        if self.after_state != 'observed' and self.after_value is not None:
            raise ValueError(
                'a non-observed after-state cannot carry a value'
            )
        return self


class ConfigurationDiffResult(BaseModel):
    """Semantic diff of two snapshots where fields are observable.

    ``semantic_available=False`` means at least one side had no field
    set — only the state hash comparison exists (``identical`` is then the
    hash-equality answer, and it can never claim "no change")."""

    model_config = ConfigDict(frozen=True)

    from_snapshot_sha256: str = Field(pattern=_SHA256)
    to_snapshot_sha256: str = Field(pattern=_SHA256)
    entries: tuple[ConfigurationFieldDiff, ...] = ()
    semantic_available: bool
    identical: bool


def _field_presence(item: ObservedDeviceField | None) -> FieldPresenceState:
    if item is None:
        return 'absent'
    return item.state


def diff_snapshots(
    before: DeviceConfigurationSnapshot,
    after: DeviceConfigurationSnapshot,
) -> ConfigurationDiffResult:
    """Diff two snapshots of the same kind.

    Semantics over the field union when both sides carry fields; opaque
    state-hash comparison otherwise — the result never claims "no change"
    from a shared preset name or a missing readback (#592 §6).
    """
    if before.fields and after.fields:
        names = sorted(
            {f.field for f in before.fields}
            | {f.field for f in after.fields}
        )
        entries: list[ConfigurationFieldDiff] = []
        for name in names:
            b = before.field(name)
            a = after.field(name)
            b_state = _field_presence(b)
            a_state = _field_presence(a)
            b_value = b.value if b is not None else None
            a_value = a.value if a is not None else None
            if b_state != a_state or b_value != a_value:
                entries.append(
                    ConfigurationFieldDiff(
                        field=name,
                        before_state=b_state,
                        after_state=a_state,
                        before_value=b_value,
                        after_value=a_value,
                    )
                )
        return ConfigurationDiffResult(
            from_snapshot_sha256=before.snapshot_sha256,
            to_snapshot_sha256=after.snapshot_sha256,
            entries=tuple(entries),
            semantic_available=True,
            identical=not entries,
        )
    return ConfigurationDiffResult(
        from_snapshot_sha256=before.snapshot_sha256,
        to_snapshot_sha256=after.snapshot_sha256,
        entries=(),
        semantic_available=False,
        identical=(
            before.state_content_sha256 == after.state_content_sha256
        ),
    )


# ---------------------------------------------------------------------------
# Restore record (#592 §10)
# ---------------------------------------------------------------------------


class ConfigurationRestoreRecord(BaseModel):
    """One restore event: which artifact/snapshot was used, the exact
    target, compatibility checks, post-restore verification and the
    honest verdict."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    restore_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    artifact_sha256: str | None = Field(default=None, pattern=_SHA256)
    source_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    target_instance_id: str = Field(min_length=1)
    target_firmware_version: str | None = None
    restore_tool: str | None = None
    restore_tool_version: str | None = None
    compatibility_checks: tuple[RestoreCheckResult, ...] = ()
    requested_scope: str | None = None
    result_status: RestoreResultStatus
    post_restore_observation_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    post_restore_diff: ConfigurationDiffResult | None = None
    verdict: RestoreVerdict
    restored_at_utc: str = Field(min_length=1)
    limitations: tuple[str, ...] = ()
    record_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_sha256', 'restore_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'ConfigurationRestoreRecord':
        _require_iso8601(self.restored_at_utc, 'restored_at_utc')
        if self.artifact_sha256 is None and (
            self.source_snapshot_sha256 is None
        ):
            raise ValueError(
                'a restore record must name the artifact or source '
                'snapshot it restored from — "restored from nothing" is '
                'not a state'
            )
        if self.verdict == 'restored_exact_observed_state':
            if self.result_status != 'success':
                raise ValueError(
                    'restored_exact_observed_state requires a successful '
                    'restore'
                )
            if self.post_restore_diff is None or not (
                self.post_restore_diff.identical
                and self.post_restore_diff.semantic_available
            ):
                raise ValueError(
                    'restored_exact_observed_state requires an identical '
                    'semantic diff against the pinned reference — a '
                    'vendor load-ack is not verification'
                )
        if self.verdict == 'restored_with_differences' and (
            self.post_restore_diff is None
            or self.post_restore_diff.identical
        ):
            raise ValueError(
                'restored_with_differences requires a non-identical '
                'post-restore diff'
            )
        if self.verdict == 'restore_incompatible' and not any(
            check.status == 'FAIL'
            for check in self.compatibility_checks
        ):
            raise ValueError(
                'restore_incompatible requires a FAILed compatibility '
                'check'
            )
        if self.verdict == 'restore_failed' and (
            self.result_status not in ('failed', 'not_attempted')
        ):
            raise ValueError(
                'restore_failed requires a failed or never-attempted '
                'restore'
            )
        if self.verdict == 'restore_unverified' and (
            self.result_status not in ('success', 'partial')
            or self.post_restore_diff is not None
        ):
            raise ValueError(
                'restore_unverified is an attempted restore without a '
                'post-restore comparison'
            )
        digest = _hash(self.semantic_payload())
        if self.record_sha256 != digest:
            raise ValueError('restore record hash mismatch')
        if self.restore_id != 'devrst-' + digest[:24]:
            raise ValueError('restore record id mismatch')
        return self


def evaluate_restore_verdict(
    *,
    compatibility_checks: tuple[RestoreCheckResult, ...],
    result_status: RestoreResultStatus,
    post_restore_diff: ConfigurationDiffResult | None,
) -> RestoreVerdict:
    """The verdict a restore may claim — computed from evidence, never
    asserted by the tool's own success message."""
    if any(check.status == 'FAIL' for check in compatibility_checks):
        return 'restore_incompatible'
    if result_status == 'failed' or result_status == 'not_attempted':
        return 'restore_failed'
    if post_restore_diff is None:
        return 'restore_unverified'
    if post_restore_diff.identical and post_restore_diff.semantic_available:
        return 'restored_exact_observed_state'
    return 'restored_with_differences'


def build_restore_record(
    *,
    document_id: str,
    target_instance_id: str,
    artifact_sha256: str | None = None,
    source_snapshot_sha256: str | None = None,
    target_firmware_version: str | None = None,
    restore_tool: str | None = None,
    restore_tool_version: str | None = None,
    compatibility_checks: tuple[RestoreCheckResult, ...] = (),
    requested_scope: str | None = None,
    result_status: RestoreResultStatus = 'not_attempted',
    post_restore_observation_sha256: str | None = None,
    post_restore_diff: ConfigurationDiffResult | None = None,
    restored_at_utc: str | None = None,
    limitations: tuple[str, ...] = (),
) -> ConfigurationRestoreRecord:
    verdict = evaluate_restore_verdict(
        compatibility_checks=compatibility_checks,
        result_status=result_status,
        post_restore_diff=post_restore_diff,
    )
    payload = dict(
        schema_version=1,
        restore_id='',
        document_id=document_id,
        artifact_sha256=artifact_sha256,
        source_snapshot_sha256=source_snapshot_sha256,
        target_instance_id=target_instance_id,
        target_firmware_version=target_firmware_version,
        restore_tool=restore_tool,
        restore_tool_version=restore_tool_version,
        compatibility_checks=tuple(compatibility_checks),
        requested_scope=requested_scope,
        result_status=result_status,
        post_restore_observation_sha256=post_restore_observation_sha256,
        post_restore_diff=post_restore_diff,
        verdict=verdict,
        restored_at_utc=restored_at_utc or _utc_now(),
        limitations=tuple(limitations),
        record_sha256='0' * 64,
    )
    probe = ConfigurationRestoreRecord.model_construct(
        **canonicalize_payload(ConfigurationRestoreRecord, payload)
    )
    digest = _hash(probe.semantic_payload())
    return ConfigurationRestoreRecord(
        **probe.model_dump(
            mode='python', exclude={'record_sha256', 'restore_id'}
        ),
        restore_id='devrst-' + digest[:24],
        record_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Replacement-device portability (#592 §11)
# ---------------------------------------------------------------------------


class ReplacementPortabilityEntry(BaseModel):
    """Portability of one setting family / artifact for a device
    replacement."""

    model_config = ConfigDict(frozen=True)

    item_key: str = Field(min_length=1)
    portability: DevicePortabilityClass
    note: str = ''


class ReplacementDeviceAssessment(BaseModel):
    """Same-model-new-serial (or cross-model) service assessment —
    portable vs device-bound content separated explicitly."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = 1
    assessment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_instance_id: str = Field(min_length=1)
    target_instance_id: str = Field(min_length=1)
    source_model: str | None = None
    target_model: str | None = None
    entries: tuple[ReplacementPortabilityEntry, ...] = ()
    assessed_at_utc: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'assessment_sha256', 'assessment_id'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'ReplacementDeviceAssessment':
        _require_iso8601(self.assessed_at_utc, 'assessed_at_utc')
        if self.source_instance_id == self.target_instance_id:
            raise ValueError(
                'a replacement assessment compares two distinct device '
                'instances'
            )
        keys = [entry.item_key for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError('portability entry keys must be unique')
        digest = _hash(self.semantic_payload())
        if self.assessment_sha256 != digest:
            raise ValueError('replacement assessment hash mismatch')
        if self.assessment_id != 'devrpl-' + digest[:24]:
            raise ValueError('replacement assessment id mismatch')
        return self


def build_replacement_assessment(
    *,
    document_id: str,
    source_instance_id: str,
    target_instance_id: str,
    entries: tuple[ReplacementPortabilityEntry, ...] = (),
    source_model: str | None = None,
    target_model: str | None = None,
    assessed_at_utc: str | None = None,
) -> ReplacementDeviceAssessment:
    payload = dict(
        schema_version=1,
        assessment_id='',
        document_id=document_id,
        source_instance_id=source_instance_id,
        target_instance_id=target_instance_id,
        source_model=source_model,
        target_model=target_model,
        entries=tuple(entries),
        assessed_at_utc=assessed_at_utc or _utc_now(),
        assessment_sha256='0' * 64,
    )
    probe = ReplacementDeviceAssessment.model_construct(
        **canonicalize_payload(ReplacementDeviceAssessment, payload)
    )
    digest = _hash(probe.semantic_payload())
    return ReplacementDeviceAssessment(
        **probe.model_dump(
            mode='python', exclude={'assessment_sha256', 'assessment_id'}
        ),
        assessment_id='devrpl-' + digest[:24],
        assessment_sha256=digest,
    )


def replacement_blocking_entries(
    assessment: ReplacementDeviceAssessment,
) -> tuple[ReplacementPortabilityEntry, ...]:
    """Entries that may NOT transfer automatically — a backup from device
    A never silently restores device B over device-bound, license-bound
    or unknown content (#592 §11)."""
    return tuple(
        entry
        for entry in assessment.entries
        if entry.portability in _BLOCKED_PORTABILITY
    )


def replacement_conditional_entries(
    assessment: ReplacementDeviceAssessment,
) -> tuple[ReplacementPortabilityEntry, ...]:
    """Entries transferrable only under a named constraint."""
    return tuple(
        entry
        for entry in assessment.entries
        if entry.portability in _CONDITIONAL_PORTABILITY
    )


# ---------------------------------------------------------------------------
# Pre-update readiness gate (#592 §8)
# ---------------------------------------------------------------------------


class PreUpdateDeclaration(BaseModel):
    """Declared readiness evidence before a managed firmware/config
    update. ``None`` is UNKNOWN — never an implicit yes."""

    model_config = ConfigDict(frozen=True)

    known_good_captured: bool | None = None
    backup_created: bool | None = None
    rollback_documented: bool | None = None
    release_notes_reviewed: bool | None = None
    compatibility_known: bool | None = None
    maintenance_window_approved: bool | None = None


def evaluate_pre_update_gate(
    declaration: PreUpdateDeclaration,
) -> tuple[tuple[RestoreCheckResult, ...], str]:
    """The readiness checklist: ``update_ready`` only when every declared
    check is PASS; an explicit False is FAIL; an absent answer stays
    UNKNOWN — the gate never silently passes."""
    spec = (
        ('known_good_captured', declaration.known_good_captured,
         'current known-good state captured'),
        ('backup_created', declaration.backup_created,
         'backup/export artifact created'),
        ('rollback_documented', declaration.rollback_documented,
         'rollback path documented'),
        ('release_notes_reviewed', declaration.release_notes_reviewed,
         'release notes reviewed'),
        ('compatibility_known', declaration.compatibility_known,
         'known compatibility constraints checked'),
        ('maintenance_window_approved',
         declaration.maintenance_window_approved,
         'maintenance window approved'),
    )
    checks: list[RestoreCheckResult] = []
    for name, value, label in spec:
        if value is None:
            status, reason = 'UNKNOWN', f'{label}: not declared'
        elif value:
            status, reason = 'PASS', f'{label}: declared'
        else:
            status, reason = 'FAIL', f'{label}: declared missing'
        checks.append(
            RestoreCheckResult(check=name, status=status, reason=reason)
        )
    if any(c.status == 'FAIL' for c in checks):
        verdict = 'update_blocked'
    elif any(c.status == 'UNKNOWN' for c in checks):
        verdict = 'update_unverified'
    else:
        verdict = 'update_ready'
    return tuple(checks), verdict


__all__ = [
    'ConfigurationDiffResult',
    'ConfigurationFieldDiff',
    'ConfigurationRestoreRecord',
    'DeviceConfigurationSnapshot',
    'DeviceFirmwareTransition',
    'DeviceKnownGoodBaseline',
    'DevicePortabilityClass',
    'DeviceSensitivityClass',
    'DeviceStateEvidenceClass',
    'DeviceTransitionKind',
    'FieldPresenceState',
    'FirmwareMigrationResult',
    'PreUpdateDeclaration',
    'ReplacementDeviceAssessment',
    'ReplacementPortabilityEntry',
    'RestoreResultStatus',
    'RestoreVerdict',
    'RollbackStatus',
    'build_device_snapshot',
    'build_firmware_transition',
    'build_known_good_baseline',
    'build_replacement_assessment',
    'build_restore_record',
    'diff_snapshots',
    'evaluate_known_good_eligibility',
    'evaluate_pre_update_gate',
    'evaluate_restore_verdict',
    'replacement_blocking_entries',
    'replacement_conditional_entries',
    'snapshot_state_digest',
]
