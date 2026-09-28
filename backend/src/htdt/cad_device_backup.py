"""Device configuration backup & recovery authority (#1055).

Preserves vendor-native backup snapshots, firmware/format compatibility
and safe restore lineage — separate from #609 calibration apply/readback
and #808 calibration-artifact import. A vendor "Save/Load" label never
makes a backup complete: scope is recorded per setting family as
INCLUDED / EXCLUDED / UNKNOWN.

- :class:`DeviceConfigurationBackupArtifact` — the immutable artifact
  identity: exact device + firmware + producer/tool + content hash +
  declared scope + privacy classification. Filenames are never identity.
  Raw bytes live wherever the artifact is stored; the authority keeps the
  hash, size and source name.
- :class:`RestoreCompatibilityDecision` — a documented/observed
  compatibility verdict between (device model, firmware, backup format)
  and a target — required before any restore. UNKNOWN requires an
  explicit user decision, never silent trial.
- :func:`evaluate_restore_compatibility` — the gate: documented or
  observed incompatibility blocks; unknown stays UNKNOWN.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload




BackupScopeState = Literal['included', 'excluded', 'unknown']
"""Per setting family: what the vendor declares the backup covers."""

RestoreCompatibilityState = Literal[
    'documented_compatible',
    'documented_incompatible',
    'observed_compatible',
    'unknown',
]

PrivacyClass = Literal['local_private', 'exportable']


class BackupScopeCoverage(BaseModel):
    """Coverage of one setting family in the artifact."""

    model_config = ConfigDict(frozen=True)

    family: str = Field(min_length=1)
    state: BackupScopeState
    detail: str | None = None


class DeviceConfigurationBackupArtifact(BaseModel):
    """Immutable identity + declared scope of one vendor backup."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['device-config-backup-1'] = (
        'device-config-backup-1'
    )
    artifact_id: str = Field(min_length=1)
    device_equipment_id: str = Field(min_length=1)
    manufacturer: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    producer_tool: str | None = None
    producer_tool_version: str | None = None
    backup_format: str | None = None
    backup_format_version: str | None = None
    content_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int | None = Field(default=None, ge=0)
    source_filename: str | None = None
    captured_at_utc: str | None = None
    scope: tuple[BackupScopeCoverage, ...] = ()
    parsed_view_ref: str | None = None
    privacy_class: PrivacyClass = 'local_private'
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    artifact_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'artifact_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'DeviceConfigurationBackupArtifact':
        families = [s.family for s in self.scope]
        if len(set(families)) != len(families):
            raise ValueError(
                'each scope family may appear at most once'
            )
        if self.artifact_sha256 != _hash(self.semantic_payload()):
            raise ValueError('backup artifact semantic hash mismatch')
        return self


def build_backup_artifact(
    *,
    artifact_id: str,
    device_equipment_id: str,
    content_sha256: str,
    manufacturer: str | None = None,
    model: str | None = None,
    firmware_version: str | None = None,
    producer_tool: str | None = None,
    producer_tool_version: str | None = None,
    backup_format: str | None = None,
    backup_format_version: str | None = None,
    size_bytes: int | None = None,
    source_filename: str | None = None,
    captured_at_utc: str | None = None,
    scope: tuple[BackupScopeCoverage, ...] = (),
    parsed_view_ref: str | None = None,
    privacy_class: PrivacyClass = 'local_private',
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> DeviceConfigurationBackupArtifact:
    probe = DeviceConfigurationBackupArtifact.model_construct(**canonicalize_payload(DeviceConfigurationBackupArtifact, dict(
        artifact_id=artifact_id,
        device_equipment_id=device_equipment_id,
        manufacturer=manufacturer,
        model=model,
        firmware_version=firmware_version,
        producer_tool=producer_tool,
        producer_tool_version=producer_tool_version,
        backup_format=backup_format,
        backup_format_version=backup_format_version,
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        source_filename=source_filename,
        captured_at_utc=captured_at_utc,
        scope=tuple(scope),
        parsed_view_ref=parsed_view_ref,
        privacy_class=privacy_class,
        provenance=tuple(provenance),
        artifact_sha256='',
    )))
    return DeviceConfigurationBackupArtifact(
        **probe.model_dump(mode='python', exclude={'artifact_sha256'}),
        artifact_sha256=_hash(probe.semantic_payload()),
    )


class RestoreCompatibilityDecision(BaseModel):
    """A recorded compatibility verdict between one artifact and one
    exact target (device + firmware + format). Never derived silently."""

    model_config = ConfigDict(frozen=True)

    artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    target_equipment_id: str = Field(min_length=1)
    target_firmware_version: str | None = None
    state: RestoreCompatibilityState
    rule_source: str | None = None
    decided_at_utc: str | None = None


class RestoreCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str | None = None


def evaluate_restore_compatibility(
    *,
    artifact: DeviceConfigurationBackupArtifact,
    target_equipment_id: str,
    target_firmware_version: str | None,
    decisions: tuple[RestoreCompatibilityDecision, ...],
) -> tuple[RestoreCheckResult, ...]:
    """The restore gate: only a documented/observed compatible decision
    for the exact (artifact, target, firmware) permits restore; known
    incompatibility is a FAIL; anything else is UNKNOWN and requires an
    explicit user decision — never silent trial."""
    checks: list[RestoreCheckResult] = []

    matching = [
        d
        for d in decisions
        if d.artifact_sha256 == artifact.content_sha256
        and d.target_equipment_id == target_equipment_id
        and (
            d.target_firmware_version is None
            or target_firmware_version is None
            or d.target_firmware_version == target_firmware_version
        )
    ]

    checks.append(
        RestoreCheckResult(
            check='device_identity',
            status=(
                'PASS'
                if target_equipment_id == artifact.device_equipment_id
                else 'UNKNOWN'
            ),
            reason=(
                'restore target is the artifact source device'
                if target_equipment_id == artifact.device_equipment_id
                else 'restore target is a different device instance — '
                'same-family names do not prove compatibility'
            ),
        )
    )

    if any(d.state == 'documented_incompatible' for d in matching):
        checks.append(
            RestoreCheckResult(
                check='compatibility_decision',
                status='FAIL',
                reason='documented incompatibility — restore must not be '
                'attempted',
            )
        )
    elif any(
        d.state in ('documented_compatible', 'observed_compatible')
        for d in matching
    ):
        checks.append(
            RestoreCheckResult(
                check='compatibility_decision',
                status='PASS',
                reason='a recorded decision marks this target compatible',
            )
        )
    else:
        checks.append(
            RestoreCheckResult(
                check='compatibility_decision',
                status='UNKNOWN',
                reason='no compatibility decision recorded for this '
                'exact artifact/target/firmware',
            )
        )

    unknown_scope = [s.family for s in artifact.scope if s.state != 'included']
    checks.append(
        RestoreCheckResult(
            check='scope_coverage',
            status='UNKNOWN' if unknown_scope else 'PASS',
            reason=(
                'families not recorded as included: '
                + ', '.join(sorted(unknown_scope))
                if unknown_scope
                else 'all declared setting families are included'
            ),
        )
    )

    return tuple(checks)
