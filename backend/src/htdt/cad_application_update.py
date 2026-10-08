"""Safe application updater authority (#889, REV69).

Updating HTDT is not replacing a stateless executable: the install owns
projects, measurement evidence, sealed authorities and device
integrations. This module is the sealed update authority:

    OPEN -> FETCH -> VERIFY PACKAGE -> PREFLIGHT -> AUTHORIZE
    -> RESTORE POINT -> STAGE -> SWAP -> HEALTH CHECK
    -> COMMIT | ROLLBACK (verified)

Fail-closed rules enforced here:

* package bytes are never trusted until their sha256 matches the sealed
  :class:`UpdatePackageDescriptor` — a mismatch is a terminal ``failed``
  update, not a retry hint;
* signatures are the #849 ``publisher_signature`` vocabulary
  (``signed_verified``/``unsigned``/``signing_failed``/``unverifiable``)
  — ``unsigned`` is a first-class state gated by session policy, never an
  error and never silently accepted;
* preflight is fail-closed: every check lands ``pass``/``warn``/``fail``/
  ``unknown`` and the verdict aggregates to ``eligible`` /
  ``eligible_with_warnings`` / ``incompatible`` / ``unverifiable`` — an
  unverifiable compatibility claim is never read as compatible;
* forward-only schema migrations are *disclosed* before apply
  (``irreversible_migration_disclosed``) and the operator authorization
  must explicitly acknowledge them;
* every stage transition is a sealed append-only record —
  :func:`derive_update_state` folds the log deterministically, so a
  crashed update is recoverable on next launch (:meth:`resume`);
* rollback is *verified*: the restore point is re-hashed against its
  sealed manifest, and an update that crossed a forward-only migration
  boundary can only ever report ``binary_only`` rollback — never a
  project/schema restore it did not perform;
* the updater performs no network I/O itself: packages arrive through an
  injectable :class:`UpdatePackageSource`, mutations through an
  :class:`UpdateInstallDriver`, and environment facts through an
  :class:`UpdateEnvironmentProbe` — tests drive it with deterministic
  fakes covering corruption, truncation, wrong sha, schema
  incompatibility and crash-mid-swap.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_calibration_deployment import _require_refs, _seal
from .canonical_json import canonical_sha256 as _hash
from .clock import utc_now_iso as _utc_now


APPLICATION_UPDATE_SCHEMA_VERSION = 'application-update-1'
APPLICATION_UPDATE_EVALUATION_VERSION = 'update-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

UpdateStage = Literal[
    'opened',
    'package_fetched',
    'package_verified',
    'preflight_evaluated',
    'recovery_point_captured',
    'staged',
    'applied',
    'health_checked',
    'rolling_back',
    'committed',
    'rolled_back',
    'rollback_failed',
    'blocked',
    'failed',
    'cancelled',
]

UPDATE_STAGE_ORDER: tuple[UpdateStage, ...] = (
    'opened',
    'package_fetched',
    'package_verified',
    'preflight_evaluated',
    'recovery_point_captured',
    'staged',
    'applied',
    'health_checked',
)

UPDATE_TERMINAL_STAGES: frozenset[UpdateStage] = frozenset({
    'committed',
    'rolled_back',
    'rollback_failed',
    'blocked',
    'failed',
    'cancelled',
})

#: Stages in which a plain cancel is still honest — no install mutation
#: can be in flight yet. From 'staged' on, a crash could have partially
#: swapped the install, so cancellation must go through the verified
#: rollback path instead.
UPDATE_CANCELLABLE_STAGES: frozenset[UpdateStage] = frozenset({
    'opened',
    'package_fetched',
    'package_verified',
    'preflight_evaluated',
    'recovery_point_captured',
})

#: Stages from which a rollback can start — the install may have been
#: (partially) mutated and a restore point exists.
UPDATE_ROLLBACK_STAGES: frozenset[UpdateStage] = frozenset({
    'staged',
    'applied',
    'health_checked',
    'rolling_back',
})

UpdateEventKind = Literal[
    'session_opened',
    'package_fetched',
    'package_verified',
    'preflight_evaluated',
    'operator_authorized',
    'restore_point_captured',
    'payload_staged',
    'swap_applied',
    'health_check_completed',
    'commit_decided',
    'rollback_started',
    'rollback_completed',
    'resumed',
    'cancelled',
    'failed',
]

UpdateTransitionOutcome = Literal[
    'advanced',
    'rejected',
    'held',
    'informational',
    'committed',
    'rolled_back',
    'rollback_failed',
    'blocked',
    'failed',
    'cancelled',
]

_UPDATE_TERMINAL_OUTCOMES: dict[UpdateTransitionOutcome, UpdateStage] = {
    'committed': 'committed',
    'rolled_back': 'rolled_back',
    'rollback_failed': 'rollback_failed',
    'blocked': 'blocked',
    'failed': 'failed',
    'cancelled': 'cancelled',
}

#: How a failed (event.succeeded=False) advance resolves: 'held' keeps the
#: stage so the step can be retried; 'failed' terminates the session —
#: package bytes disagreeing with the sealed descriptor is tamper
#: evidence, not a transient error.
_UPDATE_FAILURE_RESOLUTION: dict[UpdateEventKind, UpdateTransitionOutcome] = {
    'package_fetched': 'held',
    'restore_point_captured': 'held',
    'package_verified': 'failed',
    'payload_staged': 'failed',
    'swap_applied': 'held',
}

_UPDATE_ADVANCE_RULES: dict[tuple[UpdateStage, UpdateEventKind], UpdateStage] = {
    ('opened', 'package_fetched'): 'package_fetched',
    ('package_fetched', 'package_verified'): 'package_verified',
    ('preflight_evaluated', 'restore_point_captured'):
        'recovery_point_captured',
    ('recovery_point_captured', 'payload_staged'): 'staged',
    ('staged', 'swap_applied'): 'applied',
    ('applied', 'health_check_completed'): 'health_checked',
}

UpdatePreflightVerdict = Literal[
    'eligible',
    'eligible_with_warnings',
    'incompatible',
    'unverifiable',
]

UpdateHealthVerdict = Literal[
    'healthy',
    'healthy_with_warnings',
    'unhealthy',
    'unverifiable',
]

UpdateOutcomeVerdict = Literal[
    'committed',
    'rolled_back',
    'rollback_failed',
    'blocked',
    'failed',
    'cancelled',
]

#: What a rolled-back update actually restored. ``binary_only`` means the
#: install/config bytes were verified-restored but the project/schema
#: state is NOT claimed — either the update crossed a forward-only
#: migration boundary or the restore point only pinned data manifests.
UpdateRollbackScope = Literal['not_attempted', 'binary_only', 'full']

UpdateSignatureStatus = Literal[
    'signed_verified',
    'unsigned',
    'signing_failed',
    'unverifiable',
]

UpdateCheckStatus = Literal['pass', 'warn', 'fail', 'unknown']

UpdateChannel = Literal['stable', 'preview']

UpdateSignaturePolicy = Literal['allow_unsigned', 'require_signed']

UpdateDataBackupKind = Literal['manifest_only', 'full']

UpdateMigrationReversibility = Literal[
    'reversible',
    'forward_only',
    'unknown',
]

UpdateMigrationBoundary = Literal['none', 'reversible', 'forward_only']

UpdateAuthorizationScope = Literal['apply', 'rollback']

# ---------------------------------------------------------------------------
# Operator-facing JA labels
# ---------------------------------------------------------------------------

UPDATE_PREFLIGHT_VERDICT_LABELS: dict[str, str] = {
    'eligible': '適格',
    'eligible_with_warnings': '警告付き適格',
    'incompatible': '非互換',
    'unverifiable': '検証不能',
}

UPDATE_HEALTH_VERDICT_LABELS: dict[str, str] = {
    'healthy': '健全',
    'healthy_with_warnings': '警告付き健全',
    'unhealthy': '不健全',
    'unverifiable': '検証不能',
}

UPDATE_OUTCOME_LABELS: dict[str, str] = {
    'committed': '更新を確定',
    'rolled_back': 'ロールバック済み（検証済み）',
    'rollback_failed': 'ロールバック失敗',
    'blocked': '事前検査でブロック',
    'failed': '更新失敗',
    'cancelled': 'キャンセル済み',
}

UPDATE_ROLLBACK_SCOPE_LABELS: dict[str, str] = {
    'not_attempted': 'ロールバック未実行',
    'binary_only': 'バイナリのみ復元（プロジェクト/スキーマは対象外）',
    'full': 'バイナリ＋データ復元（検証済み）',
}

UPDATE_STAGE_LABELS: dict[str, str] = {
    'opened': '開始',
    'package_fetched': 'パッケージ取得済み',
    'package_verified': 'パッケージ検証済み',
    'preflight_evaluated': '事前検査済み',
    'recovery_point_captured': '復元ポイント取得済み',
    'staged': 'ステージ済み',
    'applied': '適用済み',
    'health_checked': '健全性検査済み',
    'rolling_back': 'ロールバック中',
    'committed': '確定済み',
    'rolled_back': 'ロールバック済み',
    'rollback_failed': 'ロールバック失敗',
    'blocked': 'ブロック済み',
    'failed': '失敗',
    'cancelled': 'キャンセル済み',
}

UPDATE_SIGNATURE_LABELS: dict[str, str] = {
    'signed_verified': '署名検証済み',
    'unsigned': '未署名',
    'signing_failed': '署名検証失敗',
    'unverifiable': '署名検証不能',
}


def update_preflight_verdict_label(verdict: str) -> str:
    return UPDATE_PREFLIGHT_VERDICT_LABELS.get(verdict, verdict)


def update_health_verdict_label(verdict: str) -> str:
    return UPDATE_HEALTH_VERDICT_LABELS.get(verdict, verdict)


def update_outcome_label(verdict: str) -> str:
    return UPDATE_OUTCOME_LABELS.get(verdict, verdict)


def update_rollback_scope_label(scope: str) -> str:
    return UPDATE_ROLLBACK_SCOPE_LABELS.get(scope, scope)


def update_stage_label(stage: str) -> str:
    return UPDATE_STAGE_LABELS.get(stage, stage)


def update_signature_label(status: str) -> str:
    return UPDATE_SIGNATURE_LABELS.get(status, status)


# ---------------------------------------------------------------------------
# Value models (not sealed rows themselves)
# ---------------------------------------------------------------------------


class UpdateArtifactRef(BaseModel):
    """One payload artifact the package promises, pinned by sha256."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    name: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    kind: Literal['payload', 'manifest', 'signature_report'] = 'payload'


class UpdateSignatureState(BaseModel):
    """The package's publisher-signature state — #849 vocabulary.

    ``unsigned`` is a first-class state, not an error. Signer identity
    fields are only meaningful on ``signed_verified``; any other status
    carrying them is rejected rather than silently reinterpreted.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    status: UpdateSignatureStatus
    signer_subject: str | None = None
    signer_thumbprint: str | None = None
    timestamped: bool | None = None

    @model_validator(mode='after')
    def _check(self) -> 'UpdateSignatureState':
        identity = (self.signer_subject, self.signer_thumbprint)
        if self.status == 'signed_verified':
            if not all(identity) or self.timestamped is None:
                raise ValueError(
                    'signed_verified requires signer_subject, '
                    'signer_thumbprint and timestamped')
        else:
            if any(identity) or self.timestamped is not None:
                raise ValueError(
                    f'{self.status} must not carry signer identity')
        return self


class UpdateCheckResult(BaseModel):
    """One named preflight/health check outcome.

    ``code`` is the machine-stable reason token (``unsigned_package``,
    ``forward_only_migration`` ...) that disclosure logic keys on;
    ``reason`` is the human-readable explanation.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    name: str = Field(min_length=1)
    status: UpdateCheckStatus
    code: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class UpdateEnvironmentSnapshot(BaseModel):
    """The environment facts a preflight/health evaluation ran on.

    None means "the probe could not observe this" — the evaluation treats
    it as unknown, never as a passing value.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    os_name: str = Field(min_length=1)
    runtime_version: str = Field(min_length=1)
    app_version: str = Field(min_length=1)
    app_native_schema_version: int = Field(ge=0)
    data_dir_schema_version: int = Field(ge=0)
    free_disk_bytes: int | None = Field(default=None, ge=0)
    open_transaction_kinds: tuple[str, ...] = ()
    pending_recovery_sessions: int | None = Field(default=None, ge=0)
    bound_adapter_ids: tuple[str, ...] = ()
    adapter_api_level: int | None = Field(default=None, ge=0)


class UpdateCapturedItem(BaseModel):
    """One file the restore point preserved (or pinned) for recovery."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    item_kind: Literal[
        'install_file',
        'preferences',
        'device_bindings',
        'schema_metadata',
        'project_backups',
    ]
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    #: 'full' backups hold the bytes; 'manifest_only' pins the hash so a
    #: rollback can verify presence/integrity but cannot restore content.
    bytes_captured: bool


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class UpdatePackageDescriptor(BaseModel):
    """The sealed update package descriptor (upkg-).

    Everything the updater needs to decide *before* touching bytes:
    artifact digests, schema floor/ceiling window, migration
    reversibility, platform/runtime declarations and the #849 signature
    state. A descriptor is sealed at intake — the fetched payload is only
    ever checked *against* it, never merged into it.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    package_id: str = Field(min_length=1)
    package_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    target_version: str = Field(min_length=1)
    target_build_id: str | None = None
    channel: UpdateChannel
    #: Where this package claim came from ('release-manifest',
    #: 'local-file', 'ci-build') — provenance, not trust.
    provenance: str = Field(min_length=1)
    artifacts: tuple[UpdateArtifactRef, ...] = Field(min_length=1)
    signature: UpdateSignatureState
    #: Optional pin to the #833 release-verification authority.
    release_ref: AuthorityRef | None = None
    #: The native schema version the packaged application expects.
    target_native_schema_version: int = Field(ge=0)
    #: Window of *current* (pre-update) schema versions this package can
    #: upgrade from: [schema_floor, schema_ceiling].
    schema_floor: int = Field(ge=0)
    schema_ceiling: int = Field(ge=0)
    migration_reversibility: UpdateMigrationReversibility
    supported_platforms: tuple[str, ...] = ()
    #: Minimum runtime the package needs, e.g. '3.10'.
    runtime_floor: str | None = None
    required_free_bytes: int = Field(ge=0)
    dropped_adapter_ids: tuple[str, ...] = ()
    adapter_api_floor: int | None = Field(default=None, ge=0)
    declared_by: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'UpdatePackageDescriptor':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.schema_floor > self.schema_ceiling:
            raise ValueError('schema_floor must be <= schema_ceiling')
        names = [artifact.name for artifact in self.artifacts]
        if len(names) != len(set(names)):
            raise ValueError('artifact names must be unique')
        if len(self.supported_platforms) != len(
                set(self.supported_platforms)):
            raise ValueError('supported_platforms must not repeat')
        if len(self.dropped_adapter_ids) != len(
                set(self.dropped_adapter_ids)):
            raise ValueError('dropped_adapter_ids must not repeat')
        if self.release_ref is not None:
            _require_refs(self.release_ref)
            if self.release_ref.kind != 'release_verification':
                raise ValueError(
                    "release_ref must pin a 'release_verification'")
        if self.runtime_floor is not None:
            _parse_version(self.runtime_floor)
        if self.package_sha256 != _hash(self.identity_payload()):
            raise ValueError('UpdatePackageDescriptor hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'package_id', 'package_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'UpdatePackageDescriptor':
        return _seal(
            cls, payload, 'package_id', 'package_sha256', 'upkg')


def package_binding(descriptor: UpdatePackageDescriptor) -> AuthorityRef:
    return AuthorityRef(
        kind='update_package',
        ref_id=descriptor.package_id,
        ref_sha256=descriptor.package_sha256,
    )


class UpdateSessionRecord(BaseModel):
    """One application-update session (upd-).

    Binds the sealed package, the update policy in force (signature
    requirement, release-evidence requirement, channel opt-in, data
    backup kind) and the pre-update application/schema identity.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    session_id: str = Field(min_length=1)
    session_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    package_ref: AuthorityRef
    install_root: str = Field(min_length=1)
    data_dir: str = Field(min_length=1)
    update_area: str = Field(min_length=1)
    app_version_before: str = Field(min_length=1)
    build_id_before: str | None = None
    native_schema_before: int = Field(ge=0)
    signature_policy: UpdateSignaturePolicy = 'allow_unsigned'
    require_release_evidence: bool = False
    allowed_channels: tuple[UpdateChannel, ...] = ('stable',)
    data_backup_kind: UpdateDataBackupKind = 'manifest_only'
    opened_by: str = Field(min_length=1)
    opened_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'UpdateSessionRecord':
        _require_iso8601(self.opened_at_utc, 'opened_at_utc')
        _require_refs(self.package_ref)
        if self.package_ref.kind != 'update_package':
            raise ValueError("package_ref must pin an 'update_package'")
        if not self.allowed_channels:
            raise ValueError('allowed_channels must not be empty')
        if len(self.allowed_channels) != len(set(self.allowed_channels)):
            raise ValueError('allowed_channels must not repeat')
        if self.session_sha256 != _hash(self.identity_payload()):
            raise ValueError('UpdateSessionRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'session_id', 'session_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'UpdateSessionRecord':
        return _seal(
            cls, payload, 'session_id', 'session_sha256', 'upd')


def session_binding(session: UpdateSessionRecord) -> AuthorityRef:
    return AuthorityRef(
        kind='update_session',
        ref_id=session.session_id,
        ref_sha256=session.session_sha256,
    )


def _aggregate_verdict(
    checks: Sequence[UpdateCheckResult],
) -> UpdatePreflightVerdict:
    """Fail-closed aggregation: any fail blocks, any unknown is
    unverifiable, any warn is eligible-with-warnings."""
    if any(check.status == 'fail' for check in checks):
        return 'incompatible'
    if any(check.status == 'unknown' for check in checks):
        return 'unverifiable'
    if any(check.status == 'warn' for check in checks):
        return 'eligible_with_warnings'
    return 'eligible'


def _aggregate_health(
    checks: Sequence[UpdateCheckResult],
) -> UpdateHealthVerdict:
    if any(check.status == 'fail' for check in checks):
        return 'unhealthy'
    if any(check.status == 'unknown' for check in checks):
        return 'unverifiable'
    if any(check.status == 'warn' for check in checks):
        return 'healthy_with_warnings'
    return 'healthy'


def _disclosed(checks: Sequence[UpdateCheckResult]) -> bool:
    return any(
        check.code == 'forward_only_migration' and check.status == 'warn'
        for check in checks)


class UpdatePreflightReport(BaseModel):
    """The sealed compatibility preflight (upre-).

    The verdict is *derived from the checks* — a report whose stated
    verdict disagrees with its own check list fails validation, so an
    update can never be silently eligible.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    report_id: str = Field(min_length=1)
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    package_ref: AuthorityRef
    verdict: UpdatePreflightVerdict
    checks: tuple[UpdateCheckResult, ...] = Field(min_length=1)
    irreversible_migration_disclosed: bool
    environment: UpdateEnvironmentSnapshot
    #: Hash of the environment snapshot — the verdict's input identity.
    environment_sha256: str = Field(pattern=_SHA256_PATTERN)
    #: True when the probe was a fake — simulated facts never read as
    #: real observations.
    probe_is_simulated: bool
    evaluated_at_utc: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'UpdatePreflightReport':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        _require_refs(self.session_ref, self.package_ref)
        if self.session_ref.kind != 'update_session':
            raise ValueError("session_ref must pin an 'update_session'")
        if self.package_ref.kind != 'update_package':
            raise ValueError("package_ref must pin an 'update_package'")
        names = [check.name for check in self.checks]
        if len(names) != len(set(names)):
            raise ValueError('check names must be unique')
        if self.verdict != _aggregate_verdict(self.checks):
            raise ValueError(
                'verdict inconsistent with checks — preflight reports '
                'may not claim a verdict their checks do not produce')
        if self.irreversible_migration_disclosed != _disclosed(
                self.checks):
            raise ValueError(
                'irreversible_migration_disclosed inconsistent with '
                'checks')
        if self.environment_sha256 != _hash(
                self.environment.model_dump(mode='python')):
            raise ValueError('environment_sha256 mismatch')
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('UpdatePreflightReport hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'report_id', 'report_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'UpdatePreflightReport':
        return _seal(
            cls, payload, 'report_id', 'report_sha256', 'upre')


def preflight_binding(report: UpdatePreflightReport) -> AuthorityRef:
    return AuthorityRef(
        kind='update_preflight_report',
        ref_id=report.report_id,
        ref_sha256=report.report_sha256,
    )


class UpdateRestorePoint(BaseModel):
    """The sealed restore-point descriptor (urp-).

    Pins what was captured before any mutation: the install tree hash,
    every captured item's sha, the schema version before, the migration
    boundary in force and — critically — what a rollback of this point
    can honestly claim (``rollback_scope_capable``).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    restore_id: str = Field(min_length=1)
    restore_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    preflight_ref: AuthorityRef
    backup_root: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    install_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    captured_items: tuple[UpdateCapturedItem, ...] = Field(min_length=1)
    schema_version_before: int = Field(ge=0)
    data_backup_kind: UpdateDataBackupKind
    migration_boundary: UpdateMigrationBoundary
    rollback_scope_capable: Literal['binary_only', 'full']
    driver_is_simulated: bool
    captured_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'UpdateRestorePoint':
        _require_iso8601(self.captured_at_utc, 'captured_at_utc')
        _require_refs(self.session_ref, self.preflight_ref)
        if self.session_ref.kind != 'update_session':
            raise ValueError("session_ref must pin an 'update_session'")
        if self.preflight_ref.kind != 'update_preflight_report':
            raise ValueError(
                "preflight_ref must pin an 'update_preflight_report'")
        if self.data_backup_kind == 'manifest_only' and (
                self.rollback_scope_capable == 'full'):
            raise ValueError(
                "manifest_only backups can never claim 'full' rollback")
        if self.migration_boundary == 'forward_only' and (
                self.rollback_scope_capable == 'full'):
            raise ValueError(
                'a forward-only migration boundary caps rollback at '
                'binary_only')
        paths = [item.relative_path for item in self.captured_items]
        if len(paths) != len(set(paths)):
            raise ValueError('captured_items paths must be unique')
        install_items = [
            item for item in self.captured_items
            if item.item_kind == 'install_file']
        if not install_items:
            raise ValueError('restore point must capture install files')
        if any(not item.bytes_captured for item in install_items):
            raise ValueError('install files must capture bytes')
        if self.restore_sha256 != _hash(self.identity_payload()):
            raise ValueError('UpdateRestorePoint hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'restore_id', 'restore_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'UpdateRestorePoint':
        return _seal(
            cls, payload, 'restore_id', 'restore_sha256', 'urp')


def restore_point_binding(point: UpdateRestorePoint) -> AuthorityRef:
    return AuthorityRef(
        kind='update_restore_point',
        ref_id=point.restore_id,
        ref_sha256=point.restore_sha256,
    )


class UpdateHealthReport(BaseModel):
    """The sealed post-swap health verdict (uhc-).

    ``observed_*`` fields are what the probe *saw* after the swap — they
    are evidence, and the validator binds them pairwise (both or neither).
    The verdict is re-derived from the check list at validation time.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    report_id: str = Field(min_length=1)
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    verdict: UpdateHealthVerdict
    checks: tuple[UpdateCheckResult, ...] = Field(min_length=1)
    observed_version: str | None = None
    observed_native_schema: int | None = Field(default=None, ge=0)
    probe_is_simulated: bool
    evaluated_at_utc: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'UpdateHealthReport':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        _require_refs(self.session_ref)
        if self.session_ref.kind != 'update_session':
            raise ValueError("session_ref must pin an 'update_session'")
        names = [check.name for check in self.checks]
        if len(names) != len(set(names)):
            raise ValueError('check names must be unique')
        if self.verdict != _aggregate_health(self.checks):
            raise ValueError(
                'verdict inconsistent with checks — health reports may '
                'not claim health their checks do not produce')
        if (self.observed_version is None) != (
                self.observed_native_schema is None):
            raise ValueError(
                'observed_version and observed_native_schema pin '
                'together or not at all')
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('UpdateHealthReport hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'report_id', 'report_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'UpdateHealthReport':
        return _seal(
            cls, payload, 'report_id', 'report_sha256', 'uhc')


def health_binding(report: UpdateHealthReport) -> AuthorityRef:
    return AuthorityRef(
        kind='update_health_report',
        ref_id=report.report_id,
        ref_sha256=report.report_sha256,
    )


class UpdateOperatorAuthorization(BaseModel):
    """One-shot operator authorization (uauth-).

    Pins the exact package + preflight report the operator approved —
    authorizing a stale preflight or a different package is a no-op. When
    the preflight disclosed a forward-only migration, the authorization
    must explicitly acknowledge it.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authorization_id: str = Field(min_length=1)
    authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    package_ref: AuthorityRef
    preflight_ref: AuthorityRef
    scope: UpdateAuthorizationScope
    acknowledges_irreversible_migration: bool
    authorized_by: str = Field(min_length=1)
    authorized_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'UpdateOperatorAuthorization':
        _require_iso8601(self.authorized_at_utc, 'authorized_at_utc')
        _require_refs(
            self.session_ref, self.package_ref, self.preflight_ref)
        if self.session_ref.kind != 'update_session':
            raise ValueError("session_ref must pin an 'update_session'")
        if self.package_ref.kind != 'update_package':
            raise ValueError("package_ref must pin an 'update_package'")
        if self.preflight_ref.kind != 'update_preflight_report':
            raise ValueError(
                "preflight_ref must pin an 'update_preflight_report'")
        if self.authorization_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'UpdateOperatorAuthorization hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'authorization_id', 'authorization_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'UpdateOperatorAuthorization':
        return _seal(
            cls, payload,
            'authorization_id', 'authorization_sha256', 'uauth')


def authorization_binding(
    authorization: UpdateOperatorAuthorization,
) -> AuthorityRef:
    return AuthorityRef(
        kind='update_authorization',
        ref_id=authorization.authorization_id,
        ref_sha256=authorization.authorization_sha256,
    )


class UpdateStageTransition(BaseModel):
    """One sealed update state-machine transition (utr-).

    The append-only log is the state — :func:`derive_update_state` folds
    it deterministically, so a crashed/interrupted update resumes or
    rolls back from exactly where the evidence left off.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    transition_id: str = Field(min_length=1)
    transition_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    seq: int = Field(ge=0)
    event_kind: UpdateEventKind
    outcome: UpdateTransitionOutcome
    actor: Literal['machine', 'operator', 'system'] = 'machine'
    from_stage: UpdateStage | None = None
    to_stage: UpdateStage
    event_succeeded: bool | None = None
    reason: str = Field(min_length=1)
    evidence_refs: tuple[AuthorityRef, ...] = ()
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'UpdateStageTransition':
        _require_iso8601(self.recorded_at_utc, 'recorded_at_utc')
        _require_refs(self.session_ref)
        if self.session_ref.kind != 'update_session':
            raise ValueError(
                "session_ref must pin an 'update_session'")
        _require_refs(*self.evidence_refs)
        if self.event_kind == 'session_opened':
            if self.from_stage is not None:
                raise ValueError('session_opened has no from_stage')
        elif self.from_stage is None:
            raise ValueError('non-creation transitions require from_stage')
        if self.outcome == 'advanced' and self.to_stage == self.from_stage:
            raise ValueError('advanced transitions must change stage')
        if self.outcome in ('rejected', 'held', 'informational') and (
                self.to_stage != self.from_stage):
            raise ValueError(
                f'{self.outcome} transitions must not change stage')
        for outcome, stage in _UPDATE_TERMINAL_OUTCOMES.items():
            if self.outcome == outcome and self.to_stage != stage:
                raise ValueError(
                    f'{outcome} transitions must land on {stage}')
        if self.transition_sha256 != _hash(self.identity_payload()):
            raise ValueError('UpdateStageTransition hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'transition_id', 'transition_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'UpdateStageTransition':
        return _seal(
            cls, payload, 'transition_id', 'transition_sha256', 'utr')


class UpdateOutcomeRecord(BaseModel):
    """The sealed terminal outcome of one update session (uout-).

    ``rollback_scope`` is honest about what was actually restored:
    ``binary_only`` never claims the project/schema state was rolled
    back, and ``full`` is only claimable when the restore point could
    carry it (``rollback_scope_capable`` pins that capability on the
    outcome itself so the claim is self-checking).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    outcome_id: str = Field(min_length=1)
    outcome_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    session_ref: AuthorityRef
    package_ref: AuthorityRef
    verdict: UpdateOutcomeVerdict
    final_stage: UpdateStage
    rollback_scope: UpdateRollbackScope = 'not_attempted'
    rollback_verified: bool | None = None
    rollback_scope_capable: Literal['binary_only', 'full'] | None = None
    restore_point_ref: AuthorityRef | None = None
    health_ref: AuthorityRef | None = None
    preflight_ref: AuthorityRef | None = None
    reason: str = Field(min_length=1)
    decided_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'UpdateOutcomeRecord':
        _require_iso8601(self.decided_at_utc, 'decided_at_utc')
        _require_refs(
            self.session_ref, self.package_ref,
            self.restore_point_ref, self.health_ref, self.preflight_ref)
        if self.session_ref.kind != 'update_session':
            raise ValueError("session_ref must pin an 'update_session'")
        if self.package_ref.kind != 'update_package':
            raise ValueError("package_ref must pin an 'update_package'")
        for ref, kind in (
            (self.restore_point_ref, 'update_restore_point'),
            (self.health_ref, 'update_health_report'),
            (self.preflight_ref, 'update_preflight_report'),
        ):
            if ref is not None and ref.kind != kind:
                raise ValueError(f'expected {kind} ref, got {ref.kind}')
        terminal_stage = _UPDATE_TERMINAL_OUTCOMES.get(
            self.verdict)  # type: ignore[arg-type]
        if terminal_stage is not None and (
                self.final_stage != terminal_stage):
            raise ValueError(
                f'{self.verdict} outcome must pin final_stage '
                f'{terminal_stage}')
        if self.verdict == 'committed':
            if self.health_ref is None:
                raise ValueError('committed outcomes pin the health ref')
            if self.rollback_scope != 'not_attempted':
                raise ValueError(
                    "committed outcomes keep rollback_scope "
                    "'not_attempted'")
            if self.rollback_verified is not None:
                raise ValueError(
                    'committed outcomes carry no rollback_verified claim')
        if self.verdict == 'rolled_back':
            if self.restore_point_ref is None:
                raise ValueError(
                    'rolled_back outcomes pin the restore point')
            if self.rollback_verified is not True:
                raise ValueError(
                    'rolled_back requires rollback_verified=True — an '
                    'unverified restore is rollback_failed, never '
                    'rolled_back')
            if self.rollback_scope not in ('binary_only', 'full'):
                raise ValueError(
                    'rolled_back requires an honest rollback_scope')
        if self.verdict == 'rollback_failed':
            if self.rollback_verified is not False:
                raise ValueError(
                    'rollback_failed requires rollback_verified=False')
        if self.rollback_scope == 'full':
            if self.rollback_scope_capable != 'full':
                raise ValueError(
                    "rollback_scope 'full' requires a restore point that "
                    'could carry it')
        if self.outcome_sha256 != _hash(self.identity_payload()):
            raise ValueError('UpdateOutcomeRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'outcome_id', 'outcome_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'UpdateOutcomeRecord':
        return _seal(
            cls, payload, 'outcome_id', 'outcome_sha256', 'uout')


def outcome_binding(outcome: UpdateOutcomeRecord) -> AuthorityRef:
    return AuthorityRef(
        kind='update_outcome',
        ref_id=outcome.outcome_id,
        ref_sha256=outcome.outcome_sha256,
    )


# ---------------------------------------------------------------------------
# Events, pure state machine, derivation
# ---------------------------------------------------------------------------


class UpdateEvent(BaseModel):
    """One fact offered to the update state machine."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: UpdateEventKind
    at_utc: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    actor: Literal['machine', 'operator', 'system'] = 'machine'
    succeeded: bool | None = None
    evidence_refs: tuple[AuthorityRef, ...] = ()
    target_stage: UpdateStage | None = None

    @model_validator(mode='after')
    def _check(self) -> 'UpdateEvent':
        _require_iso8601(self.at_utc, 'event at_utc')
        _require_refs(*self.evidence_refs)
        return self


@dataclass(frozen=True)
class UpdateSessionState:
    """Folded state of one update session — pure derivation."""

    session_ref: AuthorityRef
    current_stage: UpdateStage = 'opened'
    last_event_at_utc: str | None = None
    last_reason: str = ''
    held_reason: str | None = None
    terminal: bool = False
    terminated_stage: UpdateStage | None = None
    transition_count: int = 0
    seen_kinds: frozenset[str] = frozenset()
    rollback_attempts: int = 0
    apply_authorized: bool = False
    apply_authorization_consumed: bool = False
    evidence: dict[str, AuthorityRef] = field(default_factory=dict)


@dataclass(frozen=True)
class UpdateDecision:
    to_stage: UpdateStage
    outcome: UpdateTransitionOutcome
    reason: str


@dataclass(frozen=True)
class UpdateRejection:
    reason: str


def update_stage_transition(
    state: UpdateSessionState,
    event: UpdateEvent,
) -> UpdateDecision | UpdateRejection:
    """Pure, deterministic, total: (state, event) -> decision | rejection."""
    kind = event.kind
    current = state.current_stage

    if kind == 'session_opened':
        return UpdateDecision('opened', 'advanced', event.reason)

    if state.terminal:
        return UpdateRejection(f'session_terminal:{current}')

    if kind == 'failed':
        return UpdateDecision('failed', 'failed', event.reason)

    if kind == 'cancelled':
        if current in UPDATE_CANCELLABLE_STAGES:
            return UpdateDecision('cancelled', 'cancelled', event.reason)
        return UpdateRejection(f'cancel_requires_rollback:{current}')

    if kind == 'resumed':
        return UpdateDecision(current, 'informational', event.reason)

    if kind == 'operator_authorized':
        if current == 'preflight_evaluated':
            return UpdateDecision(
                current, 'informational', event.reason)
        return UpdateRejection(
            f'event_not_permitted:{kind}@{current}')

    if kind == 'preflight_evaluated':
        if current != 'package_verified':
            return UpdateRejection(
                f'event_not_permitted:{kind}@{current}')
        # The verdict is pinned on the report (evidence ref); the event's
        # target stage carries the gate decision so the log alone
        # re-derives blocked vs eligible.
        target = event.target_stage
        if target == 'blocked':
            return UpdateDecision('blocked', 'blocked', event.reason)
        if target == 'preflight_evaluated':
            return UpdateDecision(
                'preflight_evaluated', 'advanced', event.reason)
        return UpdateRejection(
            f'preflight_evaluated_bad_target:{target}')

    if kind == 'rollback_started':
        if current in UPDATE_ROLLBACK_STAGES:
            # Re-entering 'rolling_back' after a mid-rollback crash is
            # an informational repeat, not an advance.
            if current == 'rolling_back':
                return UpdateDecision(
                    'rolling_back', 'informational', event.reason)
            return UpdateDecision(
                'rolling_back', 'advanced', event.reason)
        return UpdateRejection(
            f'event_not_permitted:{kind}@{current}')

    if kind == 'rollback_completed':
        if current != 'rolling_back':
            return UpdateRejection(
                f'event_not_permitted:{kind}@{current}')
        if event.succeeded is True:
            return UpdateDecision(
                'rolled_back', 'rolled_back', event.reason)
        if event.succeeded is False:
            return UpdateDecision(
                'rollback_failed', 'rollback_failed', event.reason)
        return UpdateRejection('rollback_completed_requires_outcome')

    if kind == 'commit_decided':
        if current == 'health_checked':
            return UpdateDecision('committed', 'committed', event.reason)
        return UpdateRejection(
            f'event_not_permitted:{kind}@{current}')

    nxt = _UPDATE_ADVANCE_RULES.get((current, kind))
    if nxt is None:
        return UpdateRejection(
            f'event_not_permitted:{kind}@{current}')
    if event.succeeded is False:
        resolution = _UPDATE_FAILURE_RESOLUTION.get(kind, 'held')
        if resolution == 'failed':
            return UpdateDecision('failed', 'failed', event.reason)
        return UpdateDecision(current, 'held', event.reason)
    return UpdateDecision(nxt, 'advanced', event.reason)


def derive_update_state(
    session: UpdateSessionRecord,
    transitions: tuple[UpdateStageTransition, ...],
) -> UpdateSessionState:
    """Fold the sealed transition log into the current update state.

    Pure and deterministic — the same log folds to the same state after a
    restart, which is what makes an interrupted update resumable.
    """
    session_ref = session_binding(session)
    current: UpdateStage = 'opened'
    terminal = False
    terminated: UpdateStage | None = None
    held_reason: str | None = None
    last_reason = ''
    last_at: str | None = None
    seen: set[str] = set()
    evidence: dict[str, AuthorityRef] = {}
    rollback_attempts = 0
    apply_authorized = False

    for transition in sorted(transitions, key=lambda item: item.seq):
        seen.add(transition.event_kind)
        last_at = transition.recorded_at_utc
        last_reason = transition.reason
        if (transition.outcome == 'advanced'
                or transition.outcome in _UPDATE_TERMINAL_OUTCOMES):
            current = transition.to_stage
            held_reason = None
            if transition.outcome in _UPDATE_TERMINAL_OUTCOMES:
                terminal = True
                terminated = transition.to_stage
        elif transition.outcome in ('held', 'rejected'):
            held_reason = transition.reason
        if (
            transition.event_kind == 'rollback_started'
            and transition.outcome != 'rejected'
        ):
            rollback_attempts += 1
        if transition.event_kind == 'operator_authorized' and (
                transition.outcome == 'informational'):
            apply_authorized = True
        for ref in transition.evidence_refs:
            evidence[ref.kind] = ref

    return UpdateSessionState(
        session_ref=session_ref,
        current_stage=current,
        last_event_at_utc=last_at,
        last_reason=last_reason,
        held_reason=held_reason,
        terminal=terminal,
        terminated_stage=terminated,
        transition_count=len(transitions),
        seen_kinds=frozenset(seen),
        rollback_attempts=rollback_attempts,
        apply_authorized=apply_authorized,
        apply_authorization_consumed=(
            'restore_point_captured' in seen),
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Pure evaluators
# ---------------------------------------------------------------------------


def _parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for chunk in str(text).split('.'):
        digits = ''.join(ch for ch in chunk if ch.isdigit())
        if digits == '':
            break
        parts.append(int(digits))
    if not parts:
        raise ValueError(f'unparseable version {text!r}')
    return tuple(parts)


def _version_lt(left: str, right: str) -> bool:
    left_parts = _parse_version(left)
    right_parts = _parse_version(right)
    width = max(len(left_parts), len(right_parts))
    padded_left = left_parts + (0,) * (width - len(left_parts))
    padded_right = right_parts + (0,) * (width - len(right_parts))
    return padded_left < padded_right


@dataclass(frozen=True)
class UpdateEnvironmentFacts:
    """Facts one probe observed about the running environment."""

    os_name: str
    runtime_version: str
    app_version: str
    app_native_schema_version: int
    data_dir_schema_version: int
    free_disk_bytes: int | None = None
    open_transaction_kinds: tuple[str, ...] = ()
    pending_recovery_sessions: int | None = None
    bound_adapter_ids: tuple[str, ...] = ()
    adapter_api_level: int | None = None


@dataclass(frozen=True)
class PreflightEvaluation:
    verdict: UpdatePreflightVerdict
    checks: tuple[UpdateCheckResult, ...]
    irreversible_migration_disclosed: bool


def evaluate_preflight(
    descriptor: UpdatePackageDescriptor,
    facts: UpdateEnvironmentFacts,
    *,
    signature_policy: UpdateSignaturePolicy,
    require_release_evidence: bool,
    allowed_channels: Sequence[str],
) -> PreflightEvaluation:
    """Compatibility preflight — pure, deterministic, fail closed.

    The verdict is only ever as good as the checks: an undeclared or
    unobserved compatibility claim is ``unknown`` and pushes the session
    to ``unverifiable``, never to ``eligible``.
    """
    checks: list[UpdateCheckResult] = []

    # Release channel / provenance — opt-in only.
    if descriptor.channel not in tuple(allowed_channels):
        checks.append(UpdateCheckResult(
            name='release_channel',
            status='fail',
            code='channel_not_opted_in',
            reason=(
                f"channel '{descriptor.channel}' is not in the session "
                'opt-in list')))
    else:
        checks.append(UpdateCheckResult(
            name='release_channel',
            status='pass',
            code='channel_opted_in',
            reason=(
                f"channel '{descriptor.channel}' opted in via "
                f'{descriptor.provenance}')))

    # Publisher signature — #849 vocabulary; unsigned is a state.
    signature = descriptor.signature
    if signature.status == 'signed_verified':
        checks.append(UpdateCheckResult(
            name='signature',
            status='pass',
            code='signed_verified',
            reason='publisher signature verified'))
    elif signature.status == 'unsigned':
        if signature_policy == 'require_signed':
            checks.append(UpdateCheckResult(
                name='signature',
                status='fail',
                code='unsigned_package',
                reason='unsigned package rejected by signature policy'))
        else:
            checks.append(UpdateCheckResult(
                name='signature',
                status='warn',
                code='unsigned_package',
                reason='package is unsigned — allowed by policy'))
    elif signature.status == 'signing_failed':
        checks.append(UpdateCheckResult(
            name='signature',
            status='fail',
            code='signature_verification_failed',
            reason='publisher signature verification failed'))
    else:  # unverifiable
        if signature_policy == 'require_signed':
            checks.append(UpdateCheckResult(
                name='signature',
                status='fail',
                code='signature_unverifiable',
                reason='signature unverifiable under require_signed'))
        else:
            checks.append(UpdateCheckResult(
                name='signature',
                status='unknown',
                code='signature_unverifiable',
                reason='signature state could not be verified'))

    # #833 release-evidence binding.
    if descriptor.release_ref is not None:
        checks.append(UpdateCheckResult(
            name='release_evidence',
            status='pass',
            code='release_evidence_pinned',
            reason='package pins release-verification evidence'))
    elif require_release_evidence:
        checks.append(UpdateCheckResult(
            name='release_evidence',
            status='fail',
            code='release_evidence_required',
            reason='policy requires release-evidence binding'))
    else:
        checks.append(UpdateCheckResult(
            name='release_evidence',
            status='warn',
            code='release_evidence_absent',
            reason='no release-verification evidence bound'))

    # Supported OS / runtime.
    if not descriptor.supported_platforms:
        checks.append(UpdateCheckResult(
            name='supported_platform',
            status='unknown',
            code='platform_undeclared',
            reason='package declares no supported platforms'))
    elif facts.os_name == 'unknown' or facts.os_name not in (
            descriptor.supported_platforms):
        status: UpdateCheckStatus = (
            'unknown' if facts.os_name == 'unknown' else 'fail')
        checks.append(UpdateCheckResult(
            name='supported_platform',
            status=status,
            code=(
                'platform_unobserved' if status == 'unknown'
                else 'platform_unsupported'),
            reason=(
                f"os '{facts.os_name}' vs declared "
                f'{sorted(descriptor.supported_platforms)}')))
    else:
        checks.append(UpdateCheckResult(
            name='supported_platform',
            status='pass',
            code='platform_supported',
            reason=f"os '{facts.os_name}' is supported"))

    if descriptor.runtime_floor is None:
        checks.append(UpdateCheckResult(
            name='runtime_floor',
            status='unknown',
            code='runtime_floor_undeclared',
            reason='package declares no runtime floor'))
    else:
        try:
            below = _version_lt(facts.runtime_version,
                                descriptor.runtime_floor)
        except ValueError:
            checks.append(UpdateCheckResult(
                name='runtime_floor',
                status='unknown',
                code='runtime_unobserved',
                reason=(
                    f"runtime '{facts.runtime_version}' unparseable "
                    f"against floor {descriptor.runtime_floor}")))
        else:
            checks.append(UpdateCheckResult(
                name='runtime_floor',
                status='fail' if below else 'pass',
                code=(
                    'runtime_below_floor' if below
                    else 'runtime_supported'),
                reason=(
                    f"runtime {facts.runtime_version} vs floor "
                    f'{descriptor.runtime_floor}')))

    # Schema compatibility window.
    current_schema = facts.data_dir_schema_version
    if current_schema < descriptor.schema_floor:
        checks.append(UpdateCheckResult(
            name='schema_window',
            status='fail',
            code='schema_below_floor',
            reason=(
                f'data schema v{current_schema} older than package '
                f'floor v{descriptor.schema_floor}')))
    elif current_schema > descriptor.schema_ceiling:
        checks.append(UpdateCheckResult(
            name='schema_window',
            status='fail',
            code='schema_above_ceiling',
            reason=(
                f'data schema v{current_schema} newer than package '
                f'ceiling v{descriptor.schema_ceiling}')))
    else:
        checks.append(UpdateCheckResult(
            name='schema_window',
            status='pass',
            code='schema_within_window',
            reason=(
                f'data schema v{current_schema} within '
                f'[v{descriptor.schema_floor}, '
                f'v{descriptor.schema_ceiling}]')))

    # Migration direction + reversibility — the irreversible-migration
    # disclosure lives here.
    target_schema = descriptor.target_native_schema_version
    if target_schema < current_schema:
        checks.append(UpdateCheckResult(
            name='migration_direction',
            status='fail',
            code='schema_downgrade',
            reason=(
                f'package targets schema v{target_schema} below current '
                f'v{current_schema} — HTDT schema has no downgrade path')))
    elif target_schema == current_schema:
        checks.append(UpdateCheckResult(
            name='migration_direction',
            status='pass',
            code='no_schema_migration',
            reason='package keeps the current schema version'))
    elif descriptor.migration_reversibility == 'reversible':
        checks.append(UpdateCheckResult(
            name='migration_direction',
            status='pass',
            code='forward_migration_reversible',
            reason=(
                f'forward migration v{current_schema} -> '
                f'v{target_schema} declared reversible')))
    elif descriptor.migration_reversibility == 'forward_only':
        checks.append(UpdateCheckResult(
            name='migration_direction',
            status='warn',
            code='forward_only_migration',
            reason=(
                f'migration v{current_schema} -> v{target_schema} is '
                'forward-only — rollback cannot restore project/schema '
                'state across it')))
    else:
        checks.append(UpdateCheckResult(
            name='migration_direction',
            status='unknown',
            code='migration_reversibility_unknown',
            reason='package does not declare migration reversibility'))

    # Migrations already pending on the *current* install block updates.
    if facts.data_dir_schema_version < facts.app_native_schema_version:
        checks.append(UpdateCheckResult(
            name='pending_migrations',
            status='fail',
            code='unapplied_migrations_pending',
            reason=(
                f'data schema v{facts.data_dir_schema_version} behind '
                f'running app v{facts.app_native_schema_version} — '
                'finish migrations before updating')))
    else:
        checks.append(UpdateCheckResult(
            name='pending_migrations',
            status='pass',
            code='no_pending_migrations',
            reason='data schema is current'))

    # Disk space — unobserved free space is never assumed sufficient.
    if descriptor.required_free_bytes == 0:
        checks.append(UpdateCheckResult(
            name='disk_space',
            status='pass',
            code='no_disk_requirement',
            reason='package declares no disk requirement'))
    elif facts.free_disk_bytes is None:
        checks.append(UpdateCheckResult(
            name='disk_space',
            status='unknown',
            code='disk_free_unobserved',
            reason='free disk space could not be observed'))
    elif facts.free_disk_bytes < descriptor.required_free_bytes:
        checks.append(UpdateCheckResult(
            name='disk_space',
            status='fail',
            code='insufficient_disk_space',
            reason=(
                f'free {facts.free_disk_bytes}B < required '
                f'{descriptor.required_free_bytes}B')))
    else:
        checks.append(UpdateCheckResult(
            name='disk_space',
            status='pass',
            code='disk_sufficient',
            reason=(
                f'free {facts.free_disk_bytes}B >= required '
                f'{descriptor.required_free_bytes}B')))

    # Running commissioning/device transactions gate the update.
    if facts.open_transaction_kinds:
        checks.append(UpdateCheckResult(
            name='running_transactions',
            status='fail',
            code='active_transactions',
            reason=(
                'in-flight transactions: '
                + ', '.join(sorted(facts.open_transaction_kinds)))))
    else:
        checks.append(UpdateCheckResult(
            name='running_transactions',
            status='pass',
            code='no_active_transactions',
            reason='no in-flight commissioning/device transactions'))

    # Pending crash-recovery state must be resolved first.
    if facts.pending_recovery_sessions is None:
        checks.append(UpdateCheckResult(
            name='crash_recovery_pending',
            status='unknown',
            code='recovery_state_unobserved',
            reason='crash-recovery state could not be observed'))
    elif facts.pending_recovery_sessions > 0:
        checks.append(UpdateCheckResult(
            name='crash_recovery_pending',
            status='fail',
            code='pending_crash_recovery',
            reason=(
                f'{facts.pending_recovery_sessions} unresolved '
                'crash-recovery session(s)')))
    else:
        checks.append(UpdateCheckResult(
            name='crash_recovery_pending',
            status='pass',
            code='no_pending_recovery',
            reason='no pending crash recovery'))

    # Adapter/plugin compatibility.
    bound = set(facts.bound_adapter_ids)
    dropped = bound.intersection(descriptor.dropped_adapter_ids)
    if dropped:
        checks.append(UpdateCheckResult(
            name='adapter_compatibility',
            status='fail',
            code='dropped_adapter_bound',
            reason=(
                'package drops adapters with live bindings: '
                + ', '.join(sorted(dropped)))))
    elif descriptor.adapter_api_floor is not None:
        if facts.adapter_api_level is None:
            checks.append(UpdateCheckResult(
                name='adapter_compatibility',
                status='unknown',
                code='adapter_api_unobserved',
                reason='adapter API level could not be observed'))
        elif facts.adapter_api_level < descriptor.adapter_api_floor:
            checks.append(UpdateCheckResult(
                name='adapter_compatibility',
                status='fail',
                code='adapter_api_below_floor',
                reason=(
                    f'adapter API v{facts.adapter_api_level} below '
                    f'package floor v{descriptor.adapter_api_floor}')))
        else:
            checks.append(UpdateCheckResult(
                name='adapter_compatibility',
                status='pass',
                code='adapters_compatible',
                reason='adapter API satisfies package floor'))
    elif not descriptor.dropped_adapter_ids:
        checks.append(UpdateCheckResult(
            name='adapter_compatibility',
            status='unknown',
            code='adapter_compat_undeclared',
            reason='package declares no adapter compatibility claims'))
    else:
        checks.append(UpdateCheckResult(
            name='adapter_compatibility',
            status='pass',
            code='adapters_compatible',
            reason='no bound adapter is dropped by the package'))

    verdict = _aggregate_verdict(tuple(checks))
    return PreflightEvaluation(
        verdict=verdict,
        checks=tuple(checks),
        irreversible_migration_disclosed=_disclosed(checks),
    )


@dataclass(frozen=True)
class HealthObservation:
    """What the post-swap probe observed — evidence the report pins."""

    checks: tuple[UpdateCheckResult, ...]
    observed_version: str | None = None
    observed_native_schema: int | None = None


def evaluate_health(observation: HealthObservation) -> UpdateHealthVerdict:
    """Aggregate post-swap health — fail closed like preflight."""
    return _aggregate_health(observation.checks)


# ---------------------------------------------------------------------------
# Injectable seams: package source, install driver, environment probe
# ---------------------------------------------------------------------------


class ApplicationUpdateError(RuntimeError):
    """Update orchestration failure — surfaced through the log."""


class UpdateAuthorizationError(ApplicationUpdateError):
    """Apply was attempted without a valid one-shot authorization."""


class UpdateDriverError(ApplicationUpdateError):
    """The install driver could not complete or verify a step."""


class UpdateSourceError(ApplicationUpdateError):
    """The package source could not deliver the declared artifacts."""


class UpdatePackageSource(Protocol):
    """Delivers package artifact files — never trusted until verified.

    The updater performs no network I/O itself: sources may wrap HTTP,
    a local directory, or a deterministic fake. ``backend_is_simulated``
    marks evidence produced by fake sources.
    """

    backend_is_simulated: bool

    def fetch(
        self,
        descriptor: UpdatePackageDescriptor,
        destination_dir: Path,
    ) -> Mapping[str, Path]:
        """Materialize every declared artifact under destination_dir.

        Returns artifact name -> local file path. The service re-hashes
        each file against the sealed descriptor — a source that fetches
        partial sets or corrupt bytes simply fails verification.
        """
        ...


class LocalDirectoryPackageSource:
    """Package source reading artifacts from a local directory.

    Used for operator-side updates from a downloaded bundle — performs
    no network I/O. The returned files are *unverified*; the service's
    sha check decides their fate.
    """

    backend_is_simulated = False

    def __init__(self, package_dir: Path | str) -> None:
        self.package_dir = Path(package_dir)

    def fetch(
        self,
        descriptor: UpdatePackageDescriptor,
        destination_dir: Path,
    ) -> Mapping[str, Path]:
        destination_dir.mkdir(parents=True, exist_ok=True)
        fetched: dict[str, Path] = {}
        for artifact in descriptor.artifacts:
            source = self.package_dir / artifact.name
            if not source.is_file():
                raise UpdateSourceError(
                    f'package artifact missing: {artifact.name}')
            target = destination_dir / artifact.name
            shutil.copyfile(source, target)
            fetched[artifact.name] = target
        return fetched


class FakePackageScenario(BaseModel):
    """Deterministic package-source behavior for tests.

    ``payloads`` holds the bytes each artifact materializes with; the
    failure knobs simulate corruption, truncation, missing files and a
    source that cannot fetch at all.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    payloads: dict[str, bytes] = Field(default_factory=dict)
    corrupt_artifacts: tuple[str, ...] = ()
    truncate_bytes: int | None = Field(default=None, ge=0)
    missing_artifacts: tuple[str, ...] = ()
    fail_fetch: bool = False


class FakeUpdatePackageSource:
    """Deterministic in-memory package source — no I/O beyond the
    staging directory it is asked to write into."""

    backend_is_simulated = True

    def __init__(self, scenario: FakePackageScenario) -> None:
        self.scenario = scenario

    def fetch(
        self,
        descriptor: UpdatePackageDescriptor,
        destination_dir: Path,
    ) -> Mapping[str, Path]:
        if self.scenario.fail_fetch:
            raise UpdateSourceError('simulated fetch failure')
        destination_dir.mkdir(parents=True, exist_ok=True)
        fetched: dict[str, Path] = {}
        for artifact in descriptor.artifacts:
            if artifact.name in self.scenario.missing_artifacts:
                continue
            payload = self.scenario.payloads.get(
                artifact.name, b'')
            if self.scenario.truncate_bytes is not None:
                payload = payload[: self.scenario.truncate_bytes]
            if artifact.name in self.scenario.corrupt_artifacts:
                payload = payload + b'\x00' if payload else b'\x01'
            target = destination_dir / artifact.name
            target.write_bytes(payload)
            fetched[artifact.name] = target
        return fetched


@dataclass(frozen=True)
class RestorePointCapture:
    """What the driver preserved and verified before mutation."""

    backup_root: Path
    manifest_path: Path
    manifest_sha256: str
    install_tree_sha256: str
    captured_items: tuple[UpdateCapturedItem, ...]


@dataclass(frozen=True)
class RestoreVerification:
    """What a rollback actually restored and verified."""

    restored: bool
    verified: bool
    mismatches: tuple[str, ...]
    #: 'full' only when the restore point's data items were byte-restored
    #: *and* verified — the service still caps it at the recorded
    #: migration boundary before reporting scope.
    data_items_verified: bool


#: Data-dir items the restore point preserves per backup kind.
_DATA_ITEMS: tuple[tuple[str, str], ...] = (
    ('preferences', 'preferences.json'),
    ('device_bindings', 'device-bindings.json'),
    ('schema_metadata', 'schema-metadata.json'),
)


class UpdateInstallDriver(Protocol):
    """Performs the filesystem mutations of the staged apply.

    Every mutating step reports honestly: a swap that cannot complete is
    not reported as applied, and ``restore_install`` verifies the
    restored bytes against the sealed manifest before claiming anything.
    ``backend_is_simulated`` marks drivers used in tests.
    """

    backend_is_simulated: bool

    def capture_restore_point(
        self,
        *,
        install_root: Path,
        data_dir: Path,
        update_area: Path,
        data_backup_kind: UpdateDataBackupKind,
    ) -> RestorePointCapture:
        ...

    def stage_payload(
        self,
        *,
        artifacts: Mapping[str, Path],
        descriptor: UpdatePackageDescriptor,
        update_area: Path,
    ) -> Path:
        ...

    def apply_swap(
        self,
        *,
        install_root: Path,
        staged_dir: Path,
        update_area: Path,
    ) -> None:
        ...

    def inspect_install_state(
        self,
        *,
        install_root: Path,
        update_area: Path,
    ) -> Literal['clean', 'applied', 'partial']:
        ...

    def restore_install(
        self,
        *,
        restore_point: UpdateRestorePoint,
        install_root: Path,
        data_dir: Path,
        update_area: Path,
    ) -> RestoreVerification:
        ...


class FilesystemUpdateDriver:
    """Real driver for the staged apply over a local install tree.

    Layout:

        <install_root>/                 — the live install (files +
                                          version.json stamp)
        <update_area>/fetched/          — verified package artifacts
        <update_area>/staged/           — payload prepared for swap
        <update_area>/previous-install/ — diverted pre-swap install
        <update_area>/restore-point/    — install/ + data/ + manifest.json
        <update_area>/failed-install-N/ — post-health-check install
                                          diverted during rollback
    """

    backend_is_simulated = False

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _tree_manifest(root: Path) -> dict[str, dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        if not root.is_dir():
            return entries
        for path in sorted(root.rglob('*')):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                entries[rel] = {
                    'sha256': _file_sha256(path),
                    'size_bytes': path.stat().st_size,
                }
        return entries

    @staticmethod
    def _tree_sha256(root: Path) -> str:
        manifest = FilesystemUpdateDriver._tree_manifest(root)
        return _hash({
            path: entry['sha256']
            for path, entry in manifest.items()
        })

    @staticmethod
    def _copy_tree(source: Path, target: Path) -> None:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

    @staticmethod
    def _verify_tree(
        root: Path,
        manifest: dict[str, dict[str, Any]],
    ) -> tuple[str, ...]:
        mismatches: list[str] = []
        seen: set[str] = set()
        for rel, entry in manifest.items():
            path = root / rel
            seen.add(rel)
            if not path.is_file():
                mismatches.append(f'missing:{rel}')
            elif _file_sha256(path) != entry['sha256']:
                mismatches.append(f'content:{rel}')
        if root.is_dir():
            for path in root.rglob('*'):
                if path.is_file():
                    rel = path.relative_to(root).as_posix()
                    if rel not in seen:
                        mismatches.append(f'unexpected:{rel}')
        return tuple(mismatches)

    # -- steps ---------------------------------------------------------

    def capture_restore_point(
        self,
        *,
        install_root: Path,
        data_dir: Path,
        update_area: Path,
        data_backup_kind: UpdateDataBackupKind,
    ) -> RestorePointCapture:
        backup_root = update_area / 'restore-point'
        install_backup = backup_root / 'install'
        data_backup = backup_root / 'data'
        if backup_root.exists():
            shutil.rmtree(backup_root)
        backup_root.mkdir(parents=True)
        self._copy_tree(install_root, install_backup)

        captured: list[UpdateCapturedItem] = []
        install_manifest = self._tree_manifest(install_backup)
        for rel, entry in install_manifest.items():
            captured.append(UpdateCapturedItem(
                item_kind='install_file',
                relative_path=f'install/{rel}',
                sha256=entry['sha256'],
                size_bytes=entry['size_bytes'],
                bytes_captured=True,
            ))
        data_manifest: dict[str, dict[str, Any]] = {}
        for kind, name in _DATA_ITEMS:
            source = data_dir / name
            if not source.is_file():
                continue
            sha = _file_sha256(source)
            size = source.stat().st_size
            if data_backup_kind == 'full':
                data_backup.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, data_backup / name)
                captured.append(UpdateCapturedItem(
                    item_kind=kind,  # type: ignore[arg-type]
                    relative_path=f'data/{name}',
                    sha256=sha,
                    size_bytes=size,
                    bytes_captured=True,
                ))
            else:
                captured.append(UpdateCapturedItem(
                    item_kind=kind,  # type: ignore[arg-type]
                    relative_path=name,
                    sha256=sha,
                    size_bytes=size,
                    bytes_captured=False,
                ))
            data_manifest[name] = {'sha256': sha, 'size_bytes': size}

        import json

        manifest_payload = {
            'schema': 'htdt-update-restore-point/1',
            'install': install_manifest,
            'data': data_manifest,
            'data_backup_kind': data_backup_kind,
        }
        manifest_path = backup_root / 'manifest.json'
        manifest_path.write_text(
            json.dumps(manifest_payload, sort_keys=True, indent=2),
            encoding='utf-8',
        )
        manifest_sha = _file_sha256(manifest_path)
        captured.append(UpdateCapturedItem(
            item_kind='schema_metadata',
            relative_path='manifest.json',
            sha256=manifest_sha,
            size_bytes=manifest_path.stat().st_size,
            bytes_captured=True,
        ))

        # Fail closed: a restore point that cannot reproduce its own
        # manifest is not a restore point.
        verify = self._verify_tree(install_backup, install_manifest)
        if verify:
            raise UpdateDriverError(
                f'restore point failed self-verification: {verify}')
        if data_backup_kind == 'full':
            for name, entry in data_manifest.items():
                copied = data_backup / name
                if not copied.is_file() or (
                        _file_sha256(copied) != entry['sha256']):
                    raise UpdateDriverError(
                        f'restore point data item failed verification: '
                        f'{name}')

        return RestorePointCapture(
            backup_root=backup_root,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha,
            install_tree_sha256=self._tree_sha256(install_backup),
            captured_items=tuple(captured),
        )

    def stage_payload(
        self,
        *,
        artifacts: Mapping[str, Path],
        descriptor: UpdatePackageDescriptor,
        update_area: Path,
    ) -> Path:
        staged = update_area / 'staged'
        if staged.exists():
            shutil.rmtree(staged)
        staged.mkdir(parents=True)
        declared = {artifact.name: artifact
                    for artifact in descriptor.artifacts}
        for name, source in artifacts.items():
            artifact = declared.get(name)
            if artifact is None:
                raise UpdateDriverError(
                    f'staged artifact not declared in package: {name}')
            if artifact.kind != 'payload':
                continue
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            # Re-verify at stage time — bytes are only trusted while
            # their sha still matches the sealed descriptor.
            if _file_sha256(target) != artifact.sha256:
                raise UpdateDriverError(
                    f'staged content sha mismatch: {name}')
        import json

        stamp = {
            'version': descriptor.target_version,
            'build_id': descriptor.target_build_id,
            'channel': descriptor.channel,
            'target_native_schema_version':
                descriptor.target_native_schema_version,
        }
        (staged / 'version.json').write_text(
            json.dumps(stamp, sort_keys=True, indent=2),
            encoding='utf-8',
        )
        return staged

    def apply_swap(
        self,
        *,
        install_root: Path,
        staged_dir: Path,
        update_area: Path,
    ) -> None:
        previous = update_area / 'previous-install'
        if previous.exists():
            shutil.rmtree(previous)
        if not staged_dir.is_dir():
            raise UpdateDriverError('staged payload missing')
        if install_root.exists():
            install_root.rename(previous)
        try:
            staged_dir.rename(install_root)
        except Exception:
            # Best-effort repair: the previous install is still intact in
            # the update area — put it back so a partial swap never
            # leaves the install missing.
            if previous.exists() and not install_root.exists():
                previous.rename(install_root)
            raise

    def inspect_install_state(
        self,
        *,
        install_root: Path,
        update_area: Path,
    ) -> Literal['clean', 'applied', 'partial']:
        previous = update_area / 'previous-install'
        staged = update_area / 'staged'
        if not install_root.exists():
            return 'partial'
        if previous.exists() and staged.exists():
            # Crash between the two renames — the install may be either
            # generation; only the restore point can settle it.
            return 'partial'
        if previous.exists():
            return 'applied'
        return 'clean'

    def restore_install(
        self,
        *,
        restore_point: UpdateRestorePoint,
        install_root: Path,
        data_dir: Path,
        update_area: Path,
    ) -> RestoreVerification:
        import json

        backup_root = Path(restore_point.backup_root)
        manifest_path = backup_root / 'manifest.json'
        if not manifest_path.is_file():
            return RestoreVerification(
                restored=False,
                verified=False,
                mismatches=('manifest_missing',),
                data_items_verified=False,
            )
        if _file_sha256(manifest_path) != restore_point.manifest_sha256:
            return RestoreVerification(
                restored=False,
                verified=False,
                mismatches=('manifest_sha256_mismatch',),
                data_items_verified=False,
            )
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return RestoreVerification(
                restored=False,
                verified=False,
                mismatches=('manifest_unreadable',),
                data_items_verified=False,
            )

        install_backup = backup_root / 'install'
        if not install_backup.is_dir():
            return RestoreVerification(
                restored=False,
                verified=False,
                mismatches=('backup_install_missing',),
                data_items_verified=False,
            )

        previous = update_area / 'previous-install'
        failed_index = 0
        if install_root.exists():
            while (update_area / f'failed-install-{failed_index}'
                   ).exists():
                failed_index += 1
            install_root.rename(
                update_area / f'failed-install-{failed_index}')
        if previous.exists():
            shutil.rmtree(previous)
        try:
            # Copy, not move — the restore point survives as evidence
            # (and as the retry source) even after a verified restore.
            self._copy_tree(install_backup, install_root)
        except Exception as exc:  # error-boundary: filesystem restore
            raise UpdateDriverError(
                f'could not move restore point into place: {exc}') from exc

        install_manifest = manifest.get('install') or {}
        mismatches = list(self._verify_tree(install_root, install_manifest))
        verified = not mismatches

        data_items_verified = False
        if verified and restore_point.data_backup_kind == 'full':
            data_backup = backup_root / 'data'
            data_manifest = manifest.get('data') or {}
            data_mismatches: list[str] = []
            for name, entry in data_manifest.items():
                source = data_backup / name
                target = data_dir / name
                if not source.is_file():
                    data_mismatches.append(f'data_missing:{name}')
                    continue
                if _file_sha256(source) != entry['sha256']:
                    data_mismatches.append(f'data_content:{name}')
                    continue
                data_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                if _file_sha256(target) != entry['sha256']:
                    data_mismatches.append(f'data_restore:{name}')
            if data_mismatches:
                mismatches.extend(data_mismatches)
                verified = False
            else:
                data_items_verified = True
        return RestoreVerification(
            restored=True,
            verified=verified,
            mismatches=tuple(mismatches),
            data_items_verified=data_items_verified,
        )


class FakeDriverScenario(BaseModel):
    """Deterministic install-driver failures for tests."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    fail_capture: bool = False
    #: Tamper the restore-point backup after it was written (restore
    #: verification catches it -> rollback_failed).
    corrupt_backup_files: tuple[str, ...] = ()
    #: Corrupt one staged file after content verification — models a
    #: payload mutating between verify and swap.
    corrupt_staged_file: str | None = None
    fail_stage: bool = False
    #: Raise before any mutation — a clean swap refusal.
    fail_swap_cleanly: bool = False
    #: Perform the first rename then die — the classic mid-swap crash.
    crash_mid_swap: bool = False
    #: Die inside restore after diverting the broken install.
    crash_mid_restore: bool = False


class FakeUpdateInstallDriver(FilesystemUpdateDriver):
    """Filesystem driver with deterministic failure injection."""

    backend_is_simulated = True

    def __init__(self, scenario: FakeDriverScenario | None = None) -> None:
        self.scenario = scenario or FakeDriverScenario()

    def capture_restore_point(
        self, **kwargs: Any,
    ) -> RestorePointCapture:
        if self.scenario.fail_capture:
            raise UpdateDriverError('simulated capture failure')
        capture = super().capture_restore_point(**kwargs)
        for rel in self.scenario.corrupt_backup_files:
            target = capture.backup_root / 'install' / rel
            if target.is_file():
                target.write_bytes(target.read_bytes() + b'\x00corrupt')
        return capture

    def stage_payload(self, **kwargs: Any) -> Path:
        if self.scenario.fail_stage:
            raise UpdateDriverError('simulated stage failure')
        staged = super().stage_payload(**kwargs)
        if self.scenario.corrupt_staged_file is not None:
            target = staged / self.scenario.corrupt_staged_file
            if target.is_file():
                target.write_bytes(target.read_bytes() + b'\x00corrupt')
        return staged

    def apply_swap(
        self,
        *,
        install_root: Path,
        staged_dir: Path,
        update_area: Path,
    ) -> None:
        if self.scenario.fail_swap_cleanly:
            raise UpdateDriverError('simulated swap refusal')
        if self.scenario.crash_mid_swap:
            previous = update_area / 'previous-install'
            if previous.exists():
                shutil.rmtree(previous)
            if install_root.exists():
                install_root.rename(previous)
            raise UpdateDriverError('simulated crash mid-swap')
        super().apply_swap(
            install_root=install_root,
            staged_dir=staged_dir,
            update_area=update_area,
        )

    def restore_install(self, **kwargs: Any) -> RestoreVerification:
        restore_point = kwargs['restore_point']
        install_root = kwargs['install_root']
        update_area = kwargs['update_area']
        if self.scenario.crash_mid_restore:
            failed_index = 0
            while (update_area / f'failed-install-{failed_index}'
                   ).exists():
                failed_index += 1
            if install_root.exists():
                install_root.rename(
                    update_area / f'failed-install-{failed_index}')
            raise UpdateDriverError('simulated crash mid-restore')
        if self.scenario.corrupt_backup_files:
            # Re-tamper just before restore so the verification —
            # not the tamper — is what the test exercises.
            backup_root = Path(restore_point.backup_root)
            for rel in self.scenario.corrupt_backup_files:
                target = backup_root / 'install' / rel
                if target.is_file() and not target.name.endswith(
                        '.corrupt'):
                    target.write_bytes(
                        target.read_bytes() + b'\x00corrupt2')
        return super().restore_install(**kwargs)


class UpdateEnvironmentProbe(Protocol):
    """Observes the running environment for preflight and health.

    ``environment_facts`` feeds the pure preflight evaluator;
    ``health_check`` produces the post-swap observation items.
    Unobservable facts return ``None`` — the evaluators read that as
    unknown, never as satisfied.
    """

    backend_is_simulated: bool

    def environment_facts(
        self,
        *,
        session: UpdateSessionRecord,
    ) -> UpdateEnvironmentFacts:
        ...

    def health_check(
        self,
        *,
        session: UpdateSessionRecord,
        install_root: Path,
        expected_version: str,
        expected_schema: int,
    ) -> HealthObservation:
        ...


class FakeUpdateEnvironmentProbe:
    """Deterministic probe — facts and health come from the script."""

    backend_is_simulated = True

    def __init__(
        self,
        facts: UpdateEnvironmentFacts,
        health: HealthObservation | None = None,
        *,
        health_error: Exception | None = None,
    ) -> None:
        self.facts = facts
        self.health = health
        self.health_error = health_error

    def environment_facts(
        self,
        *,
        session: UpdateSessionRecord,
    ) -> UpdateEnvironmentFacts:
        return self.facts

    def health_check(
        self,
        *,
        session: UpdateSessionRecord,
        install_root: Path,
        expected_version: str,
        expected_schema: int,
    ) -> HealthObservation:
        if self.health_error is not None:
            raise self.health_error
        if self.health is not None:
            return self.health
        # Default fake observes the staged stamp honestly.
        import json

        observed_version: str | None = None
        observed_schema: int | None = None
        stamp = install_root / 'version.json'
        if stamp.is_file():
            try:
                payload = json.loads(
                    stamp.read_text(encoding='utf-8'))
                observed_version = str(payload.get('version'))
                observed_schema = int(
                    payload.get('target_native_schema_version'))
            except (OSError, ValueError, TypeError):
                observed_version = None
                observed_schema = None
        checks = [
            UpdateCheckResult(
                name='version_stamp',
                status=(
                    'pass' if observed_version == expected_version
                    else 'fail'),
                code=(
                    'version_stamp_match' if
                    observed_version == expected_version
                    else 'version_stamp_mismatch'),
                reason=(
                    f'observed {observed_version} vs expected '
                    f'{expected_version}')),
            UpdateCheckResult(
                name='schema_open',
                status=(
                    'pass' if observed_schema == expected_schema
                    else 'fail'),
                code=(
                    'schema_open_ok' if
                    observed_schema == expected_schema
                    else 'schema_open_mismatch'),
                reason=(
                    f'observed schema {observed_schema} vs expected '
                    f'{expected_schema}')),
            UpdateCheckResult(
                name='native_runtime',
                status='unknown',
                code='runtime_unobserved',
                reason='fake probe does not launch the runtime'),
            UpdateCheckResult(
                name='audio_backend',
                status='unknown',
                code='audio_unobserved',
                reason='fake probe has no audio backend'),
            UpdateCheckResult(
                name='gpu_backend',
                status='unknown',
                code='gpu_unobserved',
                reason='fake probe has no GPU backend'),
            UpdateCheckResult(
                name='adapter_loading',
                status='unknown',
                code='adapters_unobserved',
                reason='fake probe loads no adapters'),
        ]
        return HealthObservation(
            checks=tuple(checks),
            observed_version=observed_version,
            observed_native_schema=observed_schema,
        )


class LiveUpdateEnvironmentProbe:
    """Real probe wiring environment observation to HTDT facts.

    Deliberately narrow: it reports what it can observe directly and
    marks the rest unobservable — a packaged build's post-swap health
    (launch, UI init, audio/GPU) cannot be certified by the process that
    is being replaced, so those items stay ``unknown`` unless a
    ``health_runner`` is injected.
    """

    backend_is_simulated = False

    def __init__(
        self,
        *,
        open_transactions: Callable[[], Sequence[str]] | None = None,
        pending_recoveries: Callable[[], int | None] | None = None,
        bound_adapters: Callable[[], Sequence[str]] | None = None,
        adapter_api_level: Callable[[], int | None] | None = None,
        health_runner: Callable[
            [Path, str, int], HealthObservation] | None = None,
        data_dir_schema_version: Callable[[], int] | None = None,
        disk_path: Path | None = None,
    ) -> None:
        self._open_transactions = open_transactions
        self._pending_recoveries = pending_recoveries
        self._bound_adapters = bound_adapters
        self._adapter_api_level = adapter_api_level
        self._health_runner = health_runner
        self._data_dir_schema_version = data_dir_schema_version
        self._disk_path = disk_path

    @staticmethod
    def _os_name() -> str:
        if sys.platform.startswith('win'):
            return 'windows'
        if sys.platform == 'darwin':
            return 'macos'
        if sys.platform.startswith('linux'):
            return 'linux'
        return 'unknown'

    def environment_facts(
        self,
        *,
        session: UpdateSessionRecord,
    ) -> UpdateEnvironmentFacts:
        from .build_info import get_build_info
        from .cad_schema import NATIVE_SCHEMA_VERSION

        build = get_build_info()
        free_bytes: int | None = None
        try:
            usage = shutil.disk_usage(
                self._disk_path or Path(session.install_root))
            free_bytes = int(usage.free)
        except OSError:
            free_bytes = None

        def _call(fn: Callable[[], Any] | None, default: Any) -> Any:
            if fn is None:
                return default
            try:
                return fn()
            except Exception:  # error-boundary: environment probe
                return default

        schema_version = _call(
            self._data_dir_schema_version,
            session.native_schema_before,
        )
        return UpdateEnvironmentFacts(
            os_name=self._os_name(),
            runtime_version='.'.join(map(str, sys.version_info[:3])),
            app_version=build.display_version,
            app_native_schema_version=NATIVE_SCHEMA_VERSION,
            data_dir_schema_version=int(schema_version),
            free_disk_bytes=free_bytes,
            open_transaction_kinds=tuple(_call(
                self._open_transactions, ())),
            pending_recovery_sessions=_call(
                self._pending_recoveries, None),
            bound_adapter_ids=tuple(_call(self._bound_adapters, ())),
            adapter_api_level=_call(self._adapter_api_level, None),
        )

    def health_check(
        self,
        *,
        session: UpdateSessionRecord,
        install_root: Path,
        expected_version: str,
        expected_schema: int,
    ) -> HealthObservation:
        if self._health_runner is not None:
            return self._health_runner(
                install_root, expected_version, expected_schema)
        import json

        checks: list[UpdateCheckResult] = []
        observed_version: str | None = None
        observed_schema: int | None = None
        stamp = install_root / 'version.json'
        if stamp.is_file():
            try:
                payload = json.loads(stamp.read_text(encoding='utf-8'))
                observed_version = str(payload.get('version'))
                observed_schema = int(
                    payload.get('target_native_schema_version'))
            except (OSError, ValueError, TypeError):
                observed_version = None
                observed_schema = None
        checks.append(UpdateCheckResult(
            name='version_stamp',
            status=(
                'pass' if observed_version == expected_version
                else 'fail'),
            code=(
                'version_stamp_match' if
                observed_version == expected_version
                else 'version_stamp_mismatch'),
            reason=(
                f'observed {observed_version} vs expected '
                f'{expected_version}')))
        checks.append(UpdateCheckResult(
            name='schema_open',
            status=(
                'pass' if observed_schema == expected_schema
                else 'fail'),
            code=(
                'schema_open_ok' if observed_schema == expected_schema
                else 'schema_open_mismatch'),
            reason=(
                f'observed schema {observed_schema} vs expected '
                f'{expected_schema}')))
        # The still-running pre-update process cannot certify the new
        # build's launch/UI/audio/GPU — those stay unknown, which makes
        # an un-instrumented live update honestly unverifiable.
        for name, code, reason in (
            ('persistence_smoke', 'persistence_unobserved',
             'no health_runner observed persistence'),
            ('native_runtime', 'runtime_unobserved',
             'the running process cannot launch the new build'),
            ('audio_backend', 'audio_unobserved',
             'audio availability is a post-restart fact'),
            ('gpu_backend', 'gpu_unobserved',
             'GPU availability is a post-restart fact'),
            ('adapter_loading', 'adapters_unobserved',
             'adapter loading is a post-restart fact'),
        ):
            checks.append(UpdateCheckResult(
                name=name,
                status='unknown',
                code=code,
                reason=reason))
        return HealthObservation(
            checks=tuple(checks),
            observed_version=observed_version,
            observed_native_schema=observed_schema,
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ApplicationUpdateService:
    """The sealed-authority staged updater (#889).

    Every step — fetch, verify, preflight, authorize, restore point,
    stage, swap, health check, commit/rollback — is a sealed transition
    so a crashed or interrupted update is recoverable: a restarted
    process calls :meth:`resume` and the log + driver-side install
    inspection decide whether to continue or roll back.
    """

    def __init__(
        self,
        *,
        repository: Any,
        source: UpdatePackageSource,
        driver: UpdateInstallDriver,
        probe: UpdateEnvironmentProbe,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._source = source
        self._driver = driver
        self._probe = probe
        self._clock = clock or _utc_now

    # -- plumbing ------------------------------------------------------

    def _transitions(
        self, session: UpdateSessionRecord,
    ) -> tuple[UpdateStageTransition, ...]:
        return tuple(
            self._repository.list_transitions(session.session_id))

    def state(self, session: UpdateSessionRecord) -> UpdateSessionState:
        return derive_update_state(session, self._transitions(session))

    def _emit(
        self,
        session: UpdateSessionRecord,
        event: UpdateEvent,
    ) -> UpdateStageTransition:
        transitions = self._transitions(session)
        state = derive_update_state(session, transitions)
        decision = update_stage_transition(state, event)
        from_stage: UpdateStage | None = (
            None if event.kind == 'session_opened'
            else state.current_stage)
        if isinstance(decision, UpdateRejection):
            transition = UpdateStageTransition.create(
                document_id=session.document_id,
                session_ref=session_binding(session),
                seq=len(transitions),
                event_kind=event.kind,
                outcome='rejected',
                actor=event.actor,
                from_stage=from_stage,
                to_stage=from_stage or 'opened',
                event_succeeded=False,
                reason=decision.reason,
                evidence_refs=event.evidence_refs,
                recorded_at_utc=event.at_utc,
            )
            self._repository.save_transition(transition)
            return transition
        transition = UpdateStageTransition.create(
            document_id=session.document_id,
            session_ref=session_binding(session),
            seq=len(transitions),
            event_kind=event.kind,
            outcome=decision.outcome,
            actor=event.actor,
            from_stage=from_stage,
            to_stage=decision.to_stage,
            event_succeeded=event.succeeded,
            reason=decision.reason,
            evidence_refs=event.evidence_refs,
            recorded_at_utc=event.at_utc,
        )
        self._repository.save_transition(transition)
        return transition

    def _outcome(
        self,
        session: UpdateSessionRecord,
        *,
        verdict: UpdateOutcomeVerdict,
        reason: str,
        rollback_scope: UpdateRollbackScope = 'not_attempted',
        rollback_verified: bool | None = None,
        rollback_scope_capable: Literal[
            'binary_only', 'full'] | None = None,
        restore_point: UpdateRestorePoint | None = None,
        health: UpdateHealthReport | None = None,
        preflight: UpdatePreflightReport | None = None,
    ) -> UpdateOutcomeRecord:
        package = self._repository.get_package(
            session.package_ref.ref_id)
        outcome = UpdateOutcomeRecord.create(
            document_id=session.document_id,
            session_ref=session_binding(session),
            package_ref=session.package_ref,
            verdict=verdict,
            final_stage=self.state(session).current_stage,
            rollback_scope=rollback_scope,
            rollback_verified=rollback_verified,
            rollback_scope_capable=rollback_scope_capable,
            restore_point_ref=(
                restore_point_binding(restore_point)
                if restore_point is not None else None),
            health_ref=(
                health_binding(health) if health is not None else None),
            preflight_ref=(
                preflight_binding(preflight)
                if preflight is not None else None),
            reason=reason,
            decided_at_utc=self._clock(),
        )
        self._repository.save_outcome(outcome)
        return outcome

    def _latest_preflight(
        self, session: UpdateSessionRecord,
    ) -> UpdatePreflightReport | None:
        reports = self._repository.list_preflight_reports(
            session.session_id)
        return reports[-1] if reports else None

    def _restore_point(
        self, session: UpdateSessionRecord,
    ) -> UpdateRestorePoint | None:
        points = self._repository.list_restore_points(
            session.session_id)
        return points[-1] if points else None

    def _latest_health(
        self, session: UpdateSessionRecord,
    ) -> UpdateHealthReport | None:
        reports = self._repository.list_health_reports(
            session.session_id)
        return reports[-1] if reports else None

    # -- session lifecycle ---------------------------------------------

    def open_session(
        self,
        *,
        document_id: str,
        install_root: Path | str,
        data_dir: Path | str,
        update_area: Path | str,
        app_version_before: str,
        build_id_before: str | None = None,
        native_schema_before: int = 0,
        signature_policy: UpdateSignaturePolicy = 'allow_unsigned',
        require_release_evidence: bool = False,
        allowed_channels: Sequence[UpdateChannel] = ('stable',),
        data_backup_kind: UpdateDataBackupKind = 'manifest_only',
        # --- package descriptor fields ---
        target_version: str,
        target_build_id: str | None = None,
        channel: UpdateChannel = 'stable',
        provenance: str,
        artifacts: Sequence[UpdateArtifactRef | Mapping[str, Any]],
        signature: UpdateSignatureState | Mapping[str, Any],
        release_ref: AuthorityRef | None = None,
        target_native_schema_version: int,
        schema_floor: int,
        schema_ceiling: int,
        migration_reversibility: UpdateMigrationReversibility,
        supported_platforms: Sequence[str] = (),
        runtime_floor: str | None = None,
        required_free_bytes: int = 0,
        dropped_adapter_ids: Sequence[str] = (),
        adapter_api_floor: int | None = None,
        operator_id: str = 'system',
    ) -> tuple[UpdateSessionRecord, UpdatePackageDescriptor]:
        """Seal the package descriptor and open the update session."""
        package = UpdatePackageDescriptor.create(
            document_id=document_id,
            target_version=target_version,
            target_build_id=target_build_id,
            channel=channel,
            provenance=provenance,
            artifacts=tuple(
                a if isinstance(a, UpdateArtifactRef)
                else UpdateArtifactRef.model_validate(a)
                for a in artifacts),
            signature=(
                signature if isinstance(signature, UpdateSignatureState)
                else UpdateSignatureState.model_validate(signature)),
            release_ref=release_ref,
            target_native_schema_version=target_native_schema_version,
            schema_floor=schema_floor,
            schema_ceiling=schema_ceiling,
            migration_reversibility=migration_reversibility,
            supported_platforms=tuple(supported_platforms),
            runtime_floor=runtime_floor,
            required_free_bytes=required_free_bytes,
            dropped_adapter_ids=tuple(dropped_adapter_ids),
            adapter_api_floor=adapter_api_floor,
            declared_by=operator_id,
            declared_at_utc=self._clock(),
        )
        self._repository.save_package(package)
        session = UpdateSessionRecord.create(
            document_id=document_id,
            package_ref=package_binding(package),
            install_root=str(install_root),
            data_dir=str(data_dir),
            update_area=str(update_area),
            app_version_before=app_version_before,
            build_id_before=build_id_before,
            native_schema_before=native_schema_before,
            signature_policy=signature_policy,
            require_release_evidence=require_release_evidence,
            allowed_channels=tuple(allowed_channels),
            data_backup_kind=data_backup_kind,
            opened_by=operator_id,
            opened_at_utc=self._clock(),
            authority_version=APPLICATION_UPDATE_SCHEMA_VERSION,
        )
        self._repository.save_session(session)
        self._emit(session, UpdateEvent(
            kind='session_opened',
            at_utc=self._clock(),
            reason='update session opened',
            actor='operator' if operator_id != 'system' else 'system',
            evidence_refs=(package_binding(package),),
        ))
        return session, package

    # -- fetch / verify --------------------------------------------------

    def fetch_package(
        self, session: UpdateSessionRecord,
    ) -> Mapping[str, Path]:
        """Fetch the declared artifacts into the update area."""
        package = self._repository.get_package(
            session.package_ref.ref_id)
        if package is None:
            raise ApplicationUpdateError('session package missing')
        state = self.state(session)
        if state.current_stage != 'opened':
            self._emit(session, UpdateEvent(
                kind='package_fetched',
                at_utc=self._clock(),
                reason='fetch_package called out of order',
                succeeded=False,
            ))
            raise ApplicationUpdateError(
                f'fetch_package not permitted at {state.current_stage}')
        fetched_dir = Path(session.update_area) / 'fetched'
        try:  # error-boundary: package source
            fetched = self._source.fetch(package, fetched_dir)
        except Exception as exc:
            self._emit(session, UpdateEvent(
                kind='package_fetched',
                at_utc=self._clock(),
                reason=f'fetch_failed:{exc.__class__.__name__}',
                succeeded=False,
                evidence_refs=(package_binding(package),),
            ))
            raise ApplicationUpdateError(
                f'package fetch failed: {exc}') from exc
        self._emit(session, UpdateEvent(
            kind='package_fetched',
            at_utc=self._clock(),
            reason='artifacts materialized',
            succeeded=True,
            evidence_refs=(package_binding(package),),
        ))
        return fetched

    def verify_package(
        self, session: UpdateSessionRecord,
    ) -> UpdateStageTransition:
        """Re-hash every fetched artifact against the sealed descriptor.

        A mismatch terminates the session as ``failed`` — the payload
        disagrees with what was declared, so it is never staged.
        """
        package = self._repository.get_package(
            session.package_ref.ref_id)
        if package is None:
            raise ApplicationUpdateError('session package missing')
        fetched_dir = Path(session.update_area) / 'fetched'
        mismatches: list[str] = []
        for artifact in package.artifacts:
            path = fetched_dir / artifact.name
            if not path.is_file():
                mismatches.append(f'missing:{artifact.name}')
                continue
            actual = _file_sha256(path)
            if actual != artifact.sha256:
                mismatches.append(
                    f'sha256:{artifact.name}')
            elif path.stat().st_size != artifact.size_bytes:
                mismatches.append(f'size:{artifact.name}')
        if mismatches:
            transition = self._emit(session, UpdateEvent(
                kind='package_verified',
                at_utc=self._clock(),
                reason='artifact verification failed: '
                       + ','.join(mismatches),
                succeeded=False,
                evidence_refs=(package_binding(package),),
            ))
            if transition.outcome != 'rejected':
                self._outcome(
                    session,
                    verdict='failed',
                    reason='package_tamper_or_corruption:'
                           + ','.join(mismatches),
                )
            return transition
        return self._emit(session, UpdateEvent(
            kind='package_verified',
            at_utc=self._clock(),
            reason='all artifacts match sealed descriptor',
            succeeded=True,
            evidence_refs=(package_binding(package),),
        ))

    # -- preflight ---------------------------------------------------------

    def run_preflight(
        self, session: UpdateSessionRecord,
    ) -> UpdatePreflightReport:
        """Observe the environment and seal the compatibility verdict."""
        package = self._repository.get_package(
            session.package_ref.ref_id)
        if package is None:
            raise ApplicationUpdateError('session package missing')
        state = self.state(session)
        if state.current_stage != 'package_verified':
            self._emit(session, UpdateEvent(
                kind='preflight_evaluated',
                at_utc=self._clock(),
                reason='run_preflight called out of order',
                succeeded=False,
            ))
            raise ApplicationUpdateError(
                f'run_preflight not permitted at {state.current_stage}')
        try:  # error-boundary: environment probe
            facts = self._probe.environment_facts(session=session)
        except Exception as exc:
            self._emit(session, UpdateEvent(
                kind='preflight_evaluated',
                at_utc=self._clock(),
                reason=f'probe_failed:{exc.__class__.__name__}',
                succeeded=False,
            ))
            raise ApplicationUpdateError(
                f'environment probe failed: {exc}') from exc
        evaluation = evaluate_preflight(
            package,
            facts,
            signature_policy=session.signature_policy,
            require_release_evidence=session.require_release_evidence,
            allowed_channels=session.allowed_channels,
        )
        snapshot = UpdateEnvironmentSnapshot(
            os_name=facts.os_name,
            runtime_version=facts.runtime_version,
            app_version=facts.app_version,
            app_native_schema_version=facts.app_native_schema_version,
            data_dir_schema_version=facts.data_dir_schema_version,
            free_disk_bytes=facts.free_disk_bytes,
            open_transaction_kinds=facts.open_transaction_kinds,
            pending_recovery_sessions=facts.pending_recovery_sessions,
            bound_adapter_ids=facts.bound_adapter_ids,
            adapter_api_level=facts.adapter_api_level,
        )
        report = UpdatePreflightReport.create(
            document_id=session.document_id,
            session_ref=session_binding(session),
            package_ref=package_binding(package),
            verdict=evaluation.verdict,
            checks=evaluation.checks,
            irreversible_migration_disclosed=(
                evaluation.irreversible_migration_disclosed),
            environment=snapshot,
            environment_sha256=_hash(
                snapshot.model_dump(mode='python')),
            probe_is_simulated=bool(
                getattr(self._probe, 'backend_is_simulated', False)),
            evaluated_at_utc=self._clock(),
            evaluator_version=APPLICATION_UPDATE_EVALUATION_VERSION,
        )
        self._repository.save_preflight_report(report)
        if evaluation.verdict in ('incompatible', 'unverifiable'):
            transition = self._emit(session, UpdateEvent(
                kind='preflight_evaluated',
                at_utc=self._clock(),
                reason=f'preflight {evaluation.verdict} — update blocked',
                succeeded=True,
                target_stage='blocked',
                evidence_refs=(
                    preflight_binding(report),
                    package_binding(package),
                ),
            ))
            self._outcome(
                session,
                verdict='blocked',
                reason=f'preflight_{evaluation.verdict}',
                preflight=report,
            )
            return report
        self._emit(session, UpdateEvent(
            kind='preflight_evaluated',
            at_utc=self._clock(),
            reason=f'preflight {evaluation.verdict}',
            succeeded=True,
            target_stage='preflight_evaluated',
            evidence_refs=(
                preflight_binding(report),
                package_binding(package),
            ),
        ))
        return report

    # -- authorization -------------------------------------------------------

    def authorize(
        self,
        session: UpdateSessionRecord,
        *,
        scope: UpdateAuthorizationScope,
        actor: str,
        acknowledges_irreversible_migration: bool = False,
    ) -> UpdateOperatorAuthorization:
        """Seal the one-shot operator authorization.

        The authorization pins the current package + the latest preflight
        report, so it can never be replayed against a different package
        or a stale evaluation. A disclosed forward-only migration must be
        explicitly acknowledged.
        """
        package = self._repository.get_package(
            session.package_ref.ref_id)
        state = self.state(session)
        report = self._latest_preflight(session)
        if state.current_stage != 'preflight_evaluated' or report is None:
            # The machine itself forbids authorization outside
            # 'preflight_evaluated' — let it record the rejection.
            transition = self._emit(session, UpdateEvent(
                kind='operator_authorized',
                at_utc=self._clock(),
                reason=f'authorize({scope}) called out of order',
                actor='operator',
                succeeded=False,
            ))
            if transition.outcome == 'rejected':
                raise UpdateAuthorizationError(
                    f'authorization rejected at '
                    f'{state.current_stage}: {transition.reason}')
            raise UpdateAuthorizationError(
                'authorization requires a preflight report')
        if report.verdict not in ('eligible', 'eligible_with_warnings'):
            raise UpdateAuthorizationError(
                f'cannot authorize after {report.verdict} preflight')
        if (report.irreversible_migration_disclosed
                and scope == 'apply'
                and not acknowledges_irreversible_migration):
            raise UpdateAuthorizationError(
                'forward-only migration disclosed — apply requires '
                'acknowledges_irreversible_migration=True')
        authorization = UpdateOperatorAuthorization.create(
            document_id=session.document_id,
            session_ref=session_binding(session),
            package_ref=package_binding(package),
            preflight_ref=preflight_binding(report),
            scope=scope,
            acknowledges_irreversible_migration=(
                acknowledges_irreversible_migration),
            authorized_by=actor,
            authorized_at_utc=self._clock(),
        )
        transition = self._emit(session, UpdateEvent(
            kind='operator_authorized',
            at_utc=self._clock(),
            reason=f'{scope} authorized by {actor}',
            actor='operator',
            succeeded=True,
            evidence_refs=(
                authorization_binding(authorization),
                preflight_binding(report),
            ),
        ))
        if transition.outcome == 'rejected':
            raise UpdateAuthorizationError(
                f'authorization rejected at {state.current_stage}: '
                f'{transition.reason}')
        self._repository.save_authorization(authorization)
        return authorization

    def _require_apply_authorization(
        self,
        session: UpdateSessionRecord,
        state: UpdateSessionState,
    ) -> UpdateOperatorAuthorization:
        if not state.apply_authorized:
            raise UpdateAuthorizationError(
                'apply requires an operator authorization')
        if state.apply_authorization_consumed:
            raise UpdateAuthorizationError(
                'apply authorization is single-use — already consumed')
        authorizations = [
            a for a in self._repository.list_authorizations(
                session.session_id)
            if a.scope == 'apply']
        if not authorizations:
            raise UpdateAuthorizationError(
                'apply authorization record missing')
        return authorizations[-1]

    # -- staged apply ----------------------------------------------------------

    def capture_restore_point(
        self, session: UpdateSessionRecord,
    ) -> UpdateRestorePoint:
        """Capture and verify the pre-mutation restore point."""
        state = self.state(session)
        if state.current_stage != 'preflight_evaluated':
            self._emit(session, UpdateEvent(
                kind='restore_point_captured',
                at_utc=self._clock(),
                reason='capture_restore_point called out of order',
                succeeded=False,
            ))
            raise ApplicationUpdateError(
                'capture_restore_point not permitted at '
                f'{state.current_stage}')
        report = self._latest_preflight(session)
        if report is None:
            raise ApplicationUpdateError('preflight report missing')
        authorization = self._require_apply_authorization(session, state)
        boundary: UpdateMigrationBoundary
        if report.irreversible_migration_disclosed:
            boundary = 'forward_only'
        elif any(
            check.code == 'forward_migration_reversible'
            for check in report.checks
        ):
            boundary = 'reversible'
        else:
            boundary = 'none'
        scope_capable: Literal['binary_only', 'full'] = (
            'full' if (
                session.data_backup_kind == 'full'
                and boundary != 'forward_only')
            else 'binary_only')
        try:  # error-boundary: install driver
            capture = self._driver.capture_restore_point(
                install_root=Path(session.install_root),
                data_dir=Path(session.data_dir),
                update_area=Path(session.update_area),
                data_backup_kind=session.data_backup_kind,
            )
        except Exception as exc:
            self._emit(session, UpdateEvent(
                kind='restore_point_captured',
                at_utc=self._clock(),
                reason=f'restore_point_failed:{exc.__class__.__name__}',
                succeeded=False,
            ))
            raise ApplicationUpdateError(
                f'restore point capture failed: {exc}') from exc
        point = UpdateRestorePoint.create(
            document_id=session.document_id,
            session_ref=session_binding(session),
            preflight_ref=preflight_binding(report),
            backup_root=str(capture.backup_root),
            manifest_sha256=capture.manifest_sha256,
            install_tree_sha256=capture.install_tree_sha256,
            captured_items=capture.captured_items,
            schema_version_before=session.native_schema_before,
            data_backup_kind=session.data_backup_kind,
            migration_boundary=boundary,
            rollback_scope_capable=scope_capable,
            driver_is_simulated=bool(
                getattr(self._driver, 'backend_is_simulated', False)),
            captured_at_utc=self._clock(),
        )
        self._repository.save_restore_point(point)
        self._emit(session, UpdateEvent(
            kind='restore_point_captured',
            at_utc=self._clock(),
            reason=(
                'restore point captured and verified '
                f'({authorization.authorization_id})'),
            succeeded=True,
            evidence_refs=(
                restore_point_binding(point),
                authorization_binding(authorization),
            ),
        ))
        return point

    def stage_payload(
        self, session: UpdateSessionRecord,
    ) -> Path:
        """Stage the verified payload into the update area."""
        package = self._repository.get_package(
            session.package_ref.ref_id)
        state = self.state(session)
        if state.current_stage != 'recovery_point_captured':
            self._emit(session, UpdateEvent(
                kind='payload_staged',
                at_utc=self._clock(),
                reason='stage_payload called out of order',
                succeeded=False,
            ))
            raise ApplicationUpdateError(
                f'stage_payload not permitted at {state.current_stage}')
        fetched = {
            artifact.name: Path(session.update_area)
            / 'fetched' / artifact.name
            for artifact in package.artifacts
        }
        try:  # error-boundary: install driver
            staged = self._driver.stage_payload(
                artifacts=fetched,
                descriptor=package,
                update_area=Path(session.update_area),
            )
        except Exception as exc:
            self._emit(session, UpdateEvent(
                kind='payload_staged',
                at_utc=self._clock(),
                reason=f'staging_failed:{exc.__class__.__name__}:{exc}',
                succeeded=False,
            ))
            self._outcome(
                session,
                verdict='failed',
                reason='staging_failed',
                preflight=self._latest_preflight(session),
                restore_point=self._restore_point(session),
            )
            raise ApplicationUpdateError(
                f'payload staging failed: {exc}') from exc
        self._emit(session, UpdateEvent(
            kind='payload_staged',
            at_utc=self._clock(),
            reason='payload staged and content re-verified',
            succeeded=True,
            evidence_refs=(package_binding(package),),
        ))
        return staged

    def apply_swap(
        self, session: UpdateSessionRecord,
    ) -> UpdateStageTransition:
        """Swap the staged payload into the install root."""
        state = self.state(session)
        if state.current_stage != 'staged':
            self._emit(session, UpdateEvent(
                kind='swap_applied',
                at_utc=self._clock(),
                reason='apply_swap called out of order',
                succeeded=False,
            ))
            raise ApplicationUpdateError(
                f'apply_swap not permitted at {state.current_stage}')
        try:  # error-boundary: install driver
            self._driver.apply_swap(
                install_root=Path(session.install_root),
                staged_dir=Path(session.update_area) / 'staged',
                update_area=Path(session.update_area),
            )
        except Exception as exc:
            # A failed swap may have partially mutated the install —
            # hold the stage and enter the verified rollback path.
            self._emit(session, UpdateEvent(
                kind='swap_applied',
                at_utc=self._clock(),
                reason=f'swap_failed:{exc.__class__.__name__}:{exc}',
                succeeded=False,
            ))
            self.rollback(session, reason=f'swap_failed:{exc}')
            raise ApplicationUpdateError(
                f'apply swap failed: {exc}') from exc
        return self._emit(session, UpdateEvent(
            kind='swap_applied',
            at_utc=self._clock(),
            reason='staged payload swapped into install root',
            succeeded=True,
            evidence_refs=(
                restore_point_binding(self._restore_point(session)),
            ),
        ))

    # -- health check / commit / rollback -------------------------------------

    def health_check(
        self, session: UpdateSessionRecord,
    ) -> UpdateHealthReport:
        """Run the post-swap health check and seal the verdict."""
        package = self._repository.get_package(
            session.package_ref.ref_id)
        state = self.state(session)
        if state.current_stage != 'applied':
            self._emit(session, UpdateEvent(
                kind='health_check_completed',
                at_utc=self._clock(),
                reason='health_check called out of order',
                succeeded=False,
            ))
            raise ApplicationUpdateError(
                f'health_check not permitted at {state.current_stage}')
        try:  # error-boundary: environment probe
            observation = self._probe.health_check(
                session=session,
                install_root=Path(session.install_root),
                expected_version=package.target_version,
                expected_schema=package.target_native_schema_version,
            )
        except Exception as exc:
            # Health could not be evaluated — fail closed into the
            # verified rollback path rather than guess.
            self._emit(session, UpdateEvent(
                kind='health_check_completed',
                at_utc=self._clock(),
                reason=f'health_probe_failed:{exc.__class__.__name__}',
                succeeded=False,
            ))
            self.rollback(session, reason=f'health_probe_failed:{exc}')
            raise ApplicationUpdateError(
                f'health check failed to run: {exc}') from exc
        report = UpdateHealthReport.create(
            document_id=session.document_id,
            session_ref=session_binding(session),
            verdict=evaluate_health(observation),
            checks=observation.checks,
            observed_version=observation.observed_version,
            observed_native_schema=observation.observed_native_schema,
            probe_is_simulated=bool(
                getattr(self._probe, 'backend_is_simulated', False)),
            evaluated_at_utc=self._clock(),
            evaluator_version=APPLICATION_UPDATE_EVALUATION_VERSION,
        )
        self._repository.save_health_report(report)
        self._emit(session, UpdateEvent(
            kind='health_check_completed',
            at_utc=self._clock(),
            reason=f'post-swap health {report.verdict}',
            succeeded=True,
            evidence_refs=(health_binding(report),),
        ))
        return report

    def decide(
        self, session: UpdateSessionRecord,
    ) -> UpdateOutcomeRecord:
        """Commit or roll back from the sealed health verdict.

        ``healthy``/``healthy_with_warnings`` commit; ``unhealthy`` and
        ``unverifiable`` take the automatic verified-rollback path —
        unknown health is never committed.
        """
        state = self.state(session)
        if state.current_stage != 'health_checked':
            self._emit(session, UpdateEvent(
                kind='commit_decided',
                at_utc=self._clock(),
                reason='decide called out of order',
                succeeded=False,
            ))
            raise ApplicationUpdateError(
                f'decide not permitted at {state.current_stage}')
        report = self._latest_health(session)
        if report is None:
            raise ApplicationUpdateError('health report missing')
        if report.verdict in ('healthy', 'healthy_with_warnings'):
            self._emit(session, UpdateEvent(
                kind='commit_decided',
                at_utc=self._clock(),
                reason=f'health {report.verdict} — committing update',
                succeeded=True,
                evidence_refs=(health_binding(report),),
            ))
            return self._outcome(
                session,
                verdict='committed',
                reason=f'health_{report.verdict}',
                health=report,
                preflight=self._latest_preflight(session),
                restore_point=self._restore_point(session),
            )
        return self.rollback(
            session,
            reason=f'health_{report.verdict} — automatic rollback',
        )

    def rollback(
        self,
        session: UpdateSessionRecord,
        *,
        reason: str,
    ) -> UpdateOutcomeRecord:
        """Verified rollback: restore the point, verify it, report honestly.

        A rollback that cannot verify its restore is ``rollback_failed``
        — never reported as rolled back. The reported scope is capped by
        the restore point's recorded capability: across a forward-only
        migration boundary, ``binary_only`` is the strongest honest claim.
        """
        state = self.state(session)
        point = self._restore_point(session)
        health = self._latest_health(session)
        if point is None:
            # Nothing trustworthy to restore to — fail honestly. Still
            # walks through the rolling_back stage so the terminal
            # rollback_failed lands from a legal stage.
            entered = self._emit(session, UpdateEvent(
                kind='rollback_started',
                at_utc=self._clock(),
                reason=reason,
                succeeded=True,
            ))
            if entered.outcome == 'rejected':
                raise ApplicationUpdateError(
                    'rollback rejected at ' + state.current_stage)
            self._emit(session, UpdateEvent(
                kind='rollback_completed',
                at_utc=self._clock(),
                reason='no restore point exists to roll back to',
                succeeded=False,
            ))
            return self._outcome(
                session,
                verdict='rollback_failed',
                reason='no_restore_point:' + reason,
                rollback_scope='not_attempted',
                rollback_verified=False,
                health=health,
                preflight=self._latest_preflight(session),
            )
        self._emit(session, UpdateEvent(
            kind='rollback_started',
            at_utc=self._clock(),
            reason=reason,
            succeeded=True,
            evidence_refs=(restore_point_binding(point),),
        ))
        try:  # error-boundary: install driver
            verification = self._driver.restore_install(
                restore_point=point,
                install_root=Path(session.install_root),
                data_dir=Path(session.data_dir),
                update_area=Path(session.update_area),
            )
        except Exception as exc:
            self._emit(session, UpdateEvent(
                kind='rollback_completed',
                at_utc=self._clock(),
                reason=f'rollback_failed:{exc.__class__.__name__}:{exc}',
                succeeded=False,
                evidence_refs=(restore_point_binding(point),),
            ))
            return self._outcome(
                session,
                verdict='rollback_failed',
                reason=f'rollback_error:{exc}',
                rollback_scope='binary_only',
                rollback_verified=False,
                rollback_scope_capable=point.rollback_scope_capable,
                restore_point=point,
                health=health,
                preflight=self._latest_preflight(session),
            )
        if not verification.verified:
            self._emit(session, UpdateEvent(
                kind='rollback_completed',
                at_utc=self._clock(),
                reason='rollback verification failed: '
                       + ','.join(verification.mismatches),
                succeeded=False,
                evidence_refs=(restore_point_binding(point),),
            ))
            return self._outcome(
                session,
                verdict='rollback_failed',
                reason='rollback_unverified:'
                       + ','.join(verification.mismatches),
                rollback_scope='binary_only',
                rollback_verified=False,
                rollback_scope_capable=point.rollback_scope_capable,
                restore_point=point,
                health=health,
                preflight=self._latest_preflight(session),
            )
        scope: UpdateRollbackScope = (
            'full' if (
                point.rollback_scope_capable == 'full'
                and verification.data_items_verified)
            else 'binary_only')
        self._emit(session, UpdateEvent(
            kind='rollback_completed',
            at_utc=self._clock(),
            reason=f'rollback restored and verified ({scope})',
            succeeded=True,
            evidence_refs=(restore_point_binding(point),),
        ))
        return self._outcome(
            session,
            verdict='rolled_back',
            reason=reason + f' -> rolled_back:{scope}',
            rollback_scope=scope,
            rollback_verified=True,
            rollback_scope_capable=point.rollback_scope_capable,
            restore_point=point,
            health=health,
            preflight=self._latest_preflight(session),
        )

    # -- operator verbs ---------------------------------------------------------

    def cancel(
        self,
        session: UpdateSessionRecord,
        *,
        reason: str,
        actor: str = 'operator',
    ) -> UpdateOutcomeRecord:
        """Cancel before mutation. Past the restore point the install may
        be mid-swap, so cancellation is refused — use rollback."""
        transition = self._emit(session, UpdateEvent(
            kind='cancelled',
            at_utc=self._clock(),
            reason=reason,
            actor=actor,
            succeeded=True,
        ))
        if transition.outcome == 'rejected':
            raise ApplicationUpdateError(
                f'cancel rejected: {transition.reason}')
        return self._outcome(
            session,
            verdict='cancelled',
            reason=reason,
            preflight=self._latest_preflight(session),
        )

    def fail(
        self,
        session: UpdateSessionRecord,
        *,
        reason: str,
    ) -> UpdateOutcomeRecord:
        """Terminate an update that cannot continue safely."""
        transition = self._emit(session, UpdateEvent(
            kind='failed',
            at_utc=self._clock(),
            reason=reason,
            actor='system',
            succeeded=False,
        ))
        if transition.outcome == 'rejected':
            raise ApplicationUpdateError(
                f'fail rejected: {transition.reason}')
        return self._outcome(
            session,
            verdict='failed',
            reason=reason,
            preflight=self._latest_preflight(session),
            restore_point=self._restore_point(session),
            health=self._latest_health(session),
        )

    def resume(
        self, session: UpdateSessionRecord,
    ) -> UpdateSessionState:
        """Next-launch recovery for an interrupted update.

        Derives state from the sealed log, records the resume, then:
        * mid-rollback crash -> the bounded rollback re-runs (idempotent);
        * crashed after swap but before health verdict -> health check
          re-runs and decides;
        * crashed during the swap (stage 'staged' with a dirty install)
          -> automatic verified rollback;
        * clean pre-swap stages -> the operator re-invokes the pipeline.
        """
        state = self.state(session)
        if state.terminal:
            return state
        self._emit(session, UpdateEvent(
            kind='resumed',
            at_utc=self._clock(),
            reason=f'resumed at {state.current_stage}',
            actor='system',
        ))
        state = self.state(session)
        if state.current_stage == 'rolling_back':
            self.rollback(session, reason='resume_continue_rollback')
            return self.state(session)
        if state.current_stage == 'applied':
            report = self.health_check(session)
            if report.verdict in ('healthy', 'healthy_with_warnings'):
                self.decide(session)
            else:
                self.rollback(
                    session,
                    reason=f'resume_health_{report.verdict}')
            return self.state(session)
        if state.current_stage == 'staged':
            install_state = self._driver.inspect_install_state(
                install_root=Path(session.install_root),
                update_area=Path(session.update_area),
            )
            if install_state == 'partial':
                self.rollback(
                    session, reason='resume_partial_swap_detected')
            elif install_state == 'applied':
                # Swap landed but its transition never sealed — record
                # the truth and continue to the health check.
                self._emit(session, UpdateEvent(
                    kind='swap_applied',
                    at_utc=self._clock(),
                    reason='resume found swap already applied',
                    succeeded=True,
                ))
            # 'clean' -> nothing mutated; caller continues the pipeline.
        return self.state(session)


__all__ = [
    'APPLICATION_UPDATE_EVALUATION_VERSION',
    'APPLICATION_UPDATE_SCHEMA_VERSION',
    'ApplicationUpdateError',
    'ApplicationUpdateService',
    'FakeDriverScenario',
    'FakePackageScenario',
    'FakeUpdateEnvironmentProbe',
    'FakeUpdateInstallDriver',
    'FakeUpdatePackageSource',
    'FilesystemUpdateDriver',
    'HealthObservation',
    'LiveUpdateEnvironmentProbe',
    'LocalDirectoryPackageSource',
    'PreflightEvaluation',
    'RestorePointCapture',
    'RestoreVerification',
    'UPDATE_CANCELLABLE_STAGES',
    'UPDATE_HEALTH_VERDICT_LABELS',
    'UPDATE_OUTCOME_LABELS',
    'UPDATE_PREFLIGHT_VERDICT_LABELS',
    'UPDATE_ROLLBACK_SCOPE_LABELS',
    'UPDATE_ROLLBACK_STAGES',
    'UPDATE_SIGNATURE_LABELS',
    'UPDATE_STAGE_LABELS',
    'UPDATE_STAGE_ORDER',
    'UPDATE_TERMINAL_STAGES',
    'UpdateArtifactRef',
    'UpdateAuthorizationError',
    'UpdateAuthorizationScope',
    'UpdateChannel',
    'UpdateCheckResult',
    'UpdateCheckStatus',
    'UpdateDataBackupKind',
    'UpdateDecision',
    'UpdateDriverError',
    'UpdateEnvironmentFacts',
    'UpdateEnvironmentProbe',
    'UpdateEnvironmentSnapshot',
    'UpdateEvent',
    'UpdateEventKind',
    'UpdateHealthReport',
    'UpdateHealthVerdict',
    'UpdateInstallDriver',
    'UpdateMigrationBoundary',
    'UpdateMigrationReversibility',
    'UpdateOperatorAuthorization',
    'UpdateOutcomeRecord',
    'UpdateOutcomeVerdict',
    'UpdatePackageDescriptor',
    'UpdatePackageSource',
    'UpdatePreflightReport',
    'UpdatePreflightVerdict',
    'UpdateRejection',
    'UpdateRollbackScope',
    'UpdateSessionRecord',
    'UpdateSessionState',
    'UpdateSignaturePolicy',
    'UpdateSignatureState',
    'UpdateSignatureStatus',
    'UpdateSourceError',
    'UpdateStage',
    'UpdateStageTransition',
    'UpdateTransitionOutcome',
    'authorization_binding',
    'derive_update_state',
    'evaluate_health',
    'evaluate_preflight',
    'health_binding',
    'outcome_binding',
    'package_binding',
    'preflight_binding',
    'restore_point_binding',
    'session_binding',
    'update_health_verdict_label',
    'update_outcome_label',
    'update_preflight_verdict_label',
    'update_rollback_scope_label',
    'update_signature_label',
    'update_stage_label',
    'update_stage_transition',
]
