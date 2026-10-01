"""Native data upgrade lifecycle (#606): preflight, recovery snapshot, journal.

Normal application startup ultimately invokes ``cad_schema.ensure_native_schema``
when opening the data store. That authority is careful — versioned databases
migrate inside one immediate transaction and newer-than-supported databases
fail closed — but it answers no product question: which formats are involved,
whether a pre-upgrade recovery copy exists, what happened on failure, and
whether the running build is opening normal or recovery workflow.

This module wraps the canonical migration authority in an explicit upgrade
lifecycle:

1. ``plan_native_upgrade`` inspects the live database read-only and derives
   a ``NativeUpgradePlan`` — formats, migration steps, compatibility,
   snapshot requirement and the cheap storage estimate.
2. ``execute_native_upgrade`` runs the full lifecycle for a real upgrade:
   bounded disk preflight, a validated pre-upgrade recovery generation built
   by the canonical ``native_backup`` authority, the schema migration itself,
   post-migration verification, then a persisted ``UpgradeEvent``.
3. ``newer_schema_guidance`` turns ``incompatible_newer`` fail-closed into
   actionable guidance instead of a bare schema-version error.

The module owns no migration logic: ``cad_schema`` remains the only schema
authority and ``native_backup`` the only archive authority. Everything here
is composition plus operational metadata.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .build_info import get_build_info
from .cad_schema import (
    NATIVE_SCHEMA_VERSION,
    NativeSchemaCompatibility,
    NativeSchemaError,
    ensure_native_schema,
    native_schema_compatibility,
    read_native_schema_version,
)
from .managed_assets import MANAGED_ASSETS_DIRNAME
from .native_backup import (
    DATABASE_NAME,
    create_backup,
    recover_interrupted_restore,
)
from .native_diagnostics import diagnostics_dir
from .clock import utc_now_iso as _utc_now


_LOGGER = logging.getLogger('htdt.native')

UPGRADE_RECOVERY_DIRNAME = 'upgrade-recovery'
UPGRADE_JOURNAL_NAME = 'upgrade-events.jsonl'
# Durable quarantine marker (#750): written BEFORE the migration mutates
# the live database and only cleared by a successful post-migration
# verification, so a committed-but-unverified generation can never be
# mistaken for a clean current-schema startup after a restart.
UPGRADE_STATE_FILENAME = '.native-upgrade-state.json'
# Bounded retention for pre-upgrade generations: pruning only ever runs after
# a *new* migration has verified, so the last known-good upgrade snapshot is
# never removed to make room for an unproven one.
KEEP_UPGRADE_SNAPSHOTS = 3
SNAPSHOT_PREFIX = 'pre-upgrade'
UPGRADE_EVENT_SCHEMA_VERSION = 1
UPGRADE_STATE_SCHEMA_VERSION = 1


class NativeUpgradeError(RuntimeError):
    """The native data upgrade lifecycle could not complete safely."""


class NativeUpgradeQuarantineError(NativeUpgradeError):
    """The live generation committed but never verified (#750).

    Raised instead of a silent ``no_upgrade`` when the durable upgrade
    marker shows the current-schema database is the product of a
    migration whose verification failed (or never ran). Normal project
    editing must stay blocked until verification passes or the pre-upgrade
    recovery copy is restored.
    """

    def __init__(
        self,
        message: str,
        *,
        recovery_snapshot_ref: str | None = None,
    ) -> None:
        super().__init__(message)
        self.recovery_snapshot_ref = recovery_snapshot_ref

    @property
    def recovery_choices(self) -> tuple[str, ...]:
        """Explicit choices a recovery surface may offer (#750)."""

        return (
            'retry_verification',
            'restore_recovery_copy',
            'open_diagnostics',
        )


class NativeUpgradeVerificationError(NativeUpgradeError):
    """A bounded verification stage failed after migration commit."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


class IncompatibleNewerSchemaError(NativeUpgradeError):
    """The live database was created/upgraded by a newer HTDT build.

    The offending schema versions travel alongside the technical message
    so the launch-failure dialog can compose localized copy without
    parsing the English diagnostic text.
    """

    def __init__(
        self,
        message: str,
        *,
        stored_schema_version: int | None = None,
        supported_schema_version: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stored_schema_version = stored_schema_version
        self.supported_schema_version = supported_schema_version


class InsufficientUpgradeSpaceError(NativeUpgradeError):
    """Not enough free space for the mandatory pre-upgrade recovery copy."""


UpgradeCompatibility = Literal[
    'fresh_install',
    NativeSchemaCompatibility,
]


@dataclass(frozen=True)
class NativeUpgradePlan:
    """Read-only preflight result for one managed data directory.

    ``estimated_snapshot_bytes`` is the cheap storage estimate for the
    recovery generation (database + managed assets). ``None`` means the
    estimate is UNKNOWN; the disk preflight must then compare against the
    largest member sizes it can measure rather than assuming enough space.
    """

    data_dir: Path
    database_path: Path
    compatibility: UpgradeCompatibility
    current_schema_version: int
    target_schema_version: int
    migration_steps: tuple[int, ...]
    from_build_display: str | None
    to_build_display: str
    recovery_snapshot_required: bool
    estimated_snapshot_bytes: int | None
    free_space_bytes: int | None

    @property
    def requires_data_update(self) -> bool:
        return self.compatibility in ('migration_required', 'legacy_unversioned')

    @property
    def upgrade_copy_ja(self) -> str:
        """Concise user-facing copy; raw migration detail stays in the log."""

        if self.compatibility == 'legacy_unversioned':
            return (
                '従来形式（バージョン情報なし）のプロジェクトデータを、'
                f'形式 {self.target_schema_version} へ更新します。'
                '先に復旧用コピーを作成します。'
            )
        return (
            f'プロジェクトデータを形式 {self.current_schema_version} から '
            f'{self.target_schema_version} へ更新します。'
            '先に復旧用コピーを作成します。'
        )


def _directory_size(path: Path) -> int | None:
    """Best-effort recursive size; UNKNOWN stays None rather than guessing."""

    if not path.is_dir():
        return 0
    total = 0
    try:
        for candidate in path.rglob('*'):
            try:
                if candidate.is_file() and not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                return None
    except OSError:
        return None
    return total


def _free_space(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def plan_native_upgrade(data_dir: Path) -> NativeUpgradePlan:
    """Inspect the live database without mutating it.

    A missing database is a fresh install, not an upgrade of existing user
    data: no recovery generation is meaningful there. An existing
    zero-byte file fails closed in ``read_native_schema_version`` as torn
    or truncated state. A non-empty unversioned database is a genuine
    legacy upgrade subject to ``cad_schema`` adoption gates.
    """

    data_dir = Path(data_dir)
    database_path = data_dir / DATABASE_NAME
    info = get_build_info()

    try:
        stored_version = read_native_schema_version(database_path)
    except NativeSchemaError:
        # Unreadable/corrupt DB is not an upgrade candidate; surface the
        # schema authority's own error at the lifecycle boundary.
        raise

    is_fresh = not database_path.is_file()
    if is_fresh:
        compatibility: UpgradeCompatibility = 'fresh_install'
        migration_steps: tuple[int, ...] = ()
        snapshot_required = False
    else:
        compatibility = native_schema_compatibility(stored_version)
        migration_steps = tuple(
            range(stored_version + 1, NATIVE_SCHEMA_VERSION + 1)
        )
        snapshot_required = compatibility in (
            'migration_required',
            'legacy_unversioned',
        )

    estimated: int | None = None
    if snapshot_required:
        database_size = database_path.stat().st_size
        assets_size = _directory_size(data_dir / MANAGED_ASSETS_DIRNAME)
        estimated = (
            None
            if assets_size is None
            else database_size + assets_size
        )

    free_space: int | None = None
    probe = data_dir if data_dir.is_dir() else data_dir.parent
    if probe is not None and probe.is_dir():
        free_space = _free_space(probe)

    # The build that produced the current data is not knowable from the live
    # DB alone (only the newest schema stamp exists); snapshot manifests
    # record their own build provenance instead.
    return NativeUpgradePlan(
        data_dir=data_dir,
        database_path=database_path,
        compatibility=compatibility,
        current_schema_version=stored_version,
        target_schema_version=NATIVE_SCHEMA_VERSION,
        migration_steps=migration_steps,
        from_build_display=None,
        to_build_display=info.display_version,
        recovery_snapshot_required=snapshot_required,
        estimated_snapshot_bytes=estimated,
        free_space_bytes=free_space,
    )


def upgrade_snapshot_dir(data_dir: Path) -> Path:
    return Path(data_dir) / UPGRADE_RECOVERY_DIRNAME


def upgrade_journal_path(data_dir: Path) -> Path:
    return diagnostics_dir(Path(data_dir)) / UPGRADE_JOURNAL_NAME


class UpgradeEvent(BaseModel):
    """One persisted upgrade lifecycle record (operational metadata).

    This is not project evidence: it lives in the app-local diagnostics area
    so #604-style support tooling can explain which build migrated which
    schema generation and which recovery copy protects it.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = UPGRADE_EVENT_SCHEMA_VERSION
    upgrade_id: str = Field(min_length=1)
    from_build: str | None
    to_build: str
    from_schema: int = Field(ge=0)
    to_schema: int = Field(ge=0)
    compatibility: str = Field(min_length=1)
    started_at_utc: str = Field(min_length=1)
    completed_at_utc: str | None = None
    recovery_snapshot_ref: str | None = None
    verification_state: Literal[
        'not_required',
        'not_attempted',
        'pending',
        'verified',
        'failed',
    ]
    outcome: Literal[
        'no_upgrade',
        'fresh_install',
        'completed',
        'failed',
        'blocked',
    ]
    failure_summary: str | None = None
    #: Whether the migration transaction actually committed (#750).
    migration_commit_state: Literal[
        'not_attempted',
        'committed',
        'rolled_back',
        'not_applicable',
    ] = 'not_applicable'
    #: What the live database is after this event (#750).
    live_generation_state: Literal[
        'unchanged',
        'migrated_unverified',
        'migrated_verified',
        'unknown',
    ] = 'unknown'
    #: Which bounded verification stage failed, when one did.
    verification_failure_stage: str | None = None
    #: Authority rows carried through the migration still flagged stale
    #: (declared by the degraded recovery snapshot). ``0``/None for a
    #: fully verified generation — the first-run summary audits them
    #: again and offers the revalidation lane.
    stale_authority_count: int | None = Field(default=None, ge=0)

    @property
    def upgraded(self) -> bool:
        return self.outcome in ('completed', 'fresh_install')


def _append_upgrade_event(data_dir: Path, event: UpgradeEvent) -> Path:
    journal = upgrade_journal_path(data_dir)
    journal.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        event.model_dump(mode='json'),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )
    with journal.open('a', encoding='utf-8') as handle:
        handle.write(payload + '\n')
    return journal


def list_upgrade_events(data_dir: Path) -> tuple[UpgradeEvent, ...]:
    """Read the app-local upgrade journal, newest first."""

    journal = upgrade_journal_path(data_dir)
    if not journal.is_file():
        return ()
    events: list[UpgradeEvent] = []
    try:
        for line in journal.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(UpgradeEvent.model_validate_json(line))
            except ValueError:
                _LOGGER.warning('skipping unreadable upgrade event: %r', line[:200])
    except (OSError, ValueError):
        return ()
    return tuple(reversed(events))


def newer_schema_guidance(stored_version: int, supported_version: int) -> str:
    """Actionable fail-closed guidance for newer-than-supported databases."""

    return (
        'This HTDT data was created or upgraded by a newer HTDT data format '
        f'(schema v{stored_version}); this build understands up to '
        f'v{supported_version} and will not modify it.\n\n'
        'To continue, either:\n'
        '- install the HTDT build that created or last upgraded this data, or\n'
        '- restore an older backup made with a compatible HTDT build.\n\n'
        'Downgrade is not supported: no data was changed.'
    )


def newer_schema_dialog_copy_ja(
    exc: IncompatibleNewerSchemaError,
) -> tuple[str, str]:
    """(reason, recovery) localized copy for the launch-failure dialog.

    The exception's own text stays English for the diagnostics log; the
    versions it carries are what the dialog needs to explain the
    fail-closed state in the operator's language.
    """

    reason = (
        'このデータはより新しいHTDTデータ形式'
        f'（スキーマ v{exc.stored_schema_version}）で作成または更新されています。'
        if exc.stored_schema_version is not None
        else 'このデータはこのビルドより新しいHTDTデータ形式で作成または更新されています。'
    )
    recovery = (
        (
            f'このビルドが対応するのは v{exc.supported_schema_version} までです。'
            if exc.supported_schema_version is not None
            else ''
        )
        + 'データを作成・更新したHTDTビルドをインストールするか、'
        '互換性のあるビルドで作成したバックアップを復元してください。'
        'ダウングレードには対応していません — データは変更されていません。'
    )
    return reason, recovery


def upgrade_failure_recovery_ja(exc: NativeUpgradeError) -> str:
    """Localized recovery hint for an upgrade lifecycle failure.

    A quarantined generation re-verifies on the next launch and can be
    rolled back to its pre-upgrade recovery copy; a pre-commit failure
    left the live database untouched and retries the whole upgrade.
    """

    if isinstance(exc, NativeUpgradeQuarantineError):
        return (
            'データは移行後の検証が完了していないため開けません。'
            'HTDTを再起動すると検証を自動で再試行します。'
            '解決しない場合は、アップグレード前に作成された復旧用コピーを'
            '「データ管理」の復元から戻すか、診断ログをサポートへ共有してください。'
        )
    return (
        'データは変更されていません。HTDTを再起動すると更新を再試行します。'
        '解決しない場合は最新のバックアップを復元し、'
        '診断ログをサポートへ共有してください。'
    )


def _snapshot_name(plan: NativeUpgradePlan) -> str:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return (
        f'{SNAPSHOT_PREFIX}-v{plan.current_schema_version}-to-'
        f'v{plan.target_schema_version}-{stamp}-{uuid4().hex[:8]}.htdt-backup'
    )


def list_upgrade_snapshots(data_dir: Path) -> list[Path]:
    """Every retained pre-upgrade recovery copy, oldest first.

    These are ordinary validated ``.htdt-backup`` archives kept under
    ``upgrade-recovery/`` — the restore path treats them exactly like
    automatic generations.
    """

    directory = upgrade_snapshot_dir(data_dir)
    if not directory.is_dir():
        return []
    return sorted(
        (
            candidate
            for candidate in directory.iterdir()
            if candidate.is_file()
            and candidate.name.startswith(f'{SNAPSHOT_PREFIX}-')
            and candidate.name.endswith('.htdt-backup')
        ),
        key=lambda candidate: candidate.name,
    )


def prune_upgrade_snapshots(data_dir: Path, *, keep: int = KEEP_UPGRADE_SNAPSHOTS) -> None:
    """Bound pre-upgrade retention — only ever called after verification."""

    snapshots = list_upgrade_snapshots(data_dir)
    for stale in snapshots[:-keep] if keep > 0 else snapshots:
        try:
            stale.unlink()
        except OSError:
            _LOGGER.warning('could not remove old upgrade snapshot %s', stale)


def upgrade_state_path(data_dir: Path) -> Path:
    return Path(data_dir) / UPGRADE_STATE_FILENAME


class UpgradeStateRecord(BaseModel):
    """Durable upgrade-transaction marker (#750).

    Persisted BEFORE any migration mutation and updated at each commit
    boundary. Its presence with an unresolved state is the authoritative
    quarantine signal a current-schema startup must honor — a mere
    journal event is historical metadata, not a gate.
    """

    model_config = ConfigDict(frozen=True)

    state: Literal[
        'migrating',
        'committed_pending_verification',
        'verified',
        'failed_before_commit',
        'failed_after_commit',
    ]
    upgrade_id: str = Field(min_length=1)
    from_schema: int = Field(ge=0)
    to_schema: int = Field(ge=0)
    schema_version: Literal[1] = UPGRADE_STATE_SCHEMA_VERSION
    recovery_snapshot_ref: str | None = None
    failure_stage: str | None = None
    failure_summary: str | None = None
    started_at_utc: str = Field(min_length=1)
    updated_at_utc: str = Field(min_length=1)
    # Authority rows already stale BEFORE the migration, declared by the
    # degraded recovery snapshot (authority/record_ref/failure_class
    # triples as plain dicts). Optional so markers written before this
    # field existed keep validating; a quarantine-retry tolerates exactly
    # this set, no more.
    declared_stale_authorities: tuple[dict[str, str], ...] = ()


def marker_stale_declaration(marker: UpgradeStateRecord) -> tuple:
    """The stale-authority set a quarantined upgrade may still tolerate."""
    return tuple(marker.declared_stale_authorities)


#: States in which the live generation may be committed-but-unverified.
QUARANTINED_UPGRADE_STATES: frozenset[str] = frozenset(
    {
        'migrating',
        'committed_pending_verification',
        'failed_after_commit',
    }
)


def write_upgrade_state(
    data_dir: Path, record: UpgradeStateRecord
) -> Path:
    """Atomically persist the durable upgrade marker in the data dir."""

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = upgrade_state_path(data_dir)
    temp = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    temp.write_text(
        record.model_dump_json(), encoding='utf-8'
    )
    os.replace(temp, path)
    return path


def read_upgrade_state(data_dir: Path) -> UpgradeStateRecord | None:
    """Read the durable upgrade marker; None when absent or unreadable."""

    path = upgrade_state_path(Path(data_dir))
    try:
        return UpgradeStateRecord.model_validate_json(
            path.read_text(encoding='utf-8')
        )
    except (OSError, ValueError):
        return None


def clear_upgrade_state(data_dir: Path) -> None:
    """Clear the quarantine marker — only after verification passes."""

    upgrade_state_path(Path(data_dir)).unlink(missing_ok=True)


def _verify_upgraded_database(
    database_path: Path,
    expected_version: int,
    *,
    tolerated_stale: tuple = (),
) -> None:
    """Post-migration verification on the live database.

    Structural level: SQLite integrity + foreign keys + the recorded schema
    version + a real ``SceneRepository`` open. The bounded semantic level
    replays the #426 authority audit on a throwaway clone so a corrupt
    authority graph fails the upgrade rather than the first project open.

    ``tolerated_stale`` carries the degraded-backup declaration from the
    pre-upgrade recovery snapshot: authority rows already stale BEFORE the
    migration (e.g. a re-keyed build awaiting revalidation) stay flagged
    through it — the post-migration audit tolerates exactly that set and
    refuses any new failure the migration introduced.
    """

    with closing(
        sqlite3.connect(f'file:{database_path.as_posix()}?mode=ro', uri=True)
    ) as connection:
        integrity = connection.execute('PRAGMA integrity_check').fetchall()
        if integrity != [('ok',)]:
            raise NativeUpgradeVerificationError(
                'sqlite_integrity',
                f'post-migration SQLite integrity check failed: {integrity!r}',
            )
        foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
        if foreign_keys:
            raise NativeUpgradeVerificationError(
                'foreign_key',
                f'post-migration foreign-key check failed: {foreign_keys!r}',
            )
    stored = read_native_schema_version(database_path)
    if stored != expected_version:
        raise NativeUpgradeVerificationError(
            'schema_version',
            f'post-migration schema version is v{stored}, expected v{expected_version}',
        )
    try:
        from .cad_repository import SceneRepository

        SceneRepository(database_path)
    except (NativeSchemaError, sqlite3.DatabaseError) as exc:
        raise NativeUpgradeVerificationError(
            'repository_open',
            f'post-migration repository openability check failed: {exc}',
        ) from exc

    # Bounded semantic level: replay the #426 authority audit on a throwaway
    # clone (repository construction may touch the DB; never audit the live
    # file). The audit resolves managed assets relative to the clone's parent
    # directory, so the probe gets a hardlinked asset subtree on the same
    # volume rather than an expensive copy.
    probe_dir = tempfile.mkdtemp(
        prefix='.upgrade-audit-',
        dir=database_path.parent,
    )
    try:
        probe_path = Path(probe_dir) / database_path.name
        shutil.copyfile(database_path, probe_path)
        live_assets = database_path.parent / MANAGED_ASSETS_DIRNAME
        if live_assets.is_dir():
            probe_assets = Path(probe_dir) / MANAGED_ASSETS_DIRNAME
            probe_assets.mkdir()
            for candidate in live_assets.iterdir():
                if not candidate.is_file():
                    continue
                target = probe_assets / candidate.name
                try:
                    target.hardlink_to(candidate)
                except OSError:
                    shutil.copyfile(candidate, target)
        from .native_authority_audit import audit_native_authority_graph

        try:
            audit_report = audit_native_authority_graph(probe_path)
        except Exception as exc:
            raise NativeUpgradeVerificationError(
                'semantic_audit',
                f'post-migration semantic audit failed: {exc}',
            ) from exc
        if not audit_report.ok:
            declared = {
                (entry['authority'], entry['record_ref'], entry['failure_class'])
                if isinstance(entry, dict)
                else (entry.authority, entry.record_ref, entry.failure_class)
                for entry in tolerated_stale
            }
            residual = tuple(
                diagnostic
                for diagnostic in audit_report.diagnostics
                if (
                    diagnostic.authority,
                    diagnostic.record_ref,
                    diagnostic.failure_class,
                )
                not in declared
            )
            if residual or audit_report.unclassified_tables:
                raise NativeUpgradeVerificationError(
                    'semantic_audit',
                    'post-migration semantic audit failed: '
                    + audit_report.summary(),
                )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def execute_native_upgrade(
    data_dir: Path,
    *,
    keep_snapshots: int = KEEP_UPGRADE_SNAPSHOTS,
) -> UpgradeEvent:
    """Run the native data upgrade lifecycle for one managed data directory.

    Silent no-op when the schema is current. For a real schema-changing
    upgrade: disk preflight → validated recovery generation → canonical
    migration → post-migration verification → persisted ``UpgradeEvent``.
    Any failure before the migration commit leaves the old live generation
    usable; any failure after it is reported with the recovery copy path.
    """

    data_dir = Path(data_dir)
    plan = plan_native_upgrade(data_dir)

    if plan.compatibility == 'incompatible_newer':
        raise IncompatibleNewerSchemaError(
            newer_schema_guidance(
                plan.current_schema_version,
                plan.target_schema_version,
            ),
            stored_schema_version=plan.current_schema_version,
            supported_schema_version=plan.target_schema_version,
        )

    if plan.compatibility == 'current':
        marker = read_upgrade_state(data_dir)
        if marker is None and upgrade_state_path(data_dir).is_file():
            # A present-but-unreadable marker is an unresolved quarantine
            # signal, not an absent one: the live generation may be the
            # committed-but-never-verified product of a crashed migration.
            # Fail closed instead of silently skipping quarantine.
            raise NativeUpgradeQuarantineError(
                'the durable upgrade state marker is unreadable, so the live '
                'database cannot be proven verified: '
                f'{upgrade_state_path(data_dir)}'
            )
        if (
            marker is not None
            and marker.state in QUARANTINED_UPGRADE_STATES
        ):
            return _resolve_quarantined_generation(
                data_dir, plan, marker, keep_snapshots=keep_snapshots
            )
        return UpgradeEvent(
            upgrade_id=uuid4().hex,
            from_build=plan.from_build_display,
            to_build=plan.to_build_display,
            from_schema=plan.current_schema_version,
            to_schema=plan.target_schema_version,
            compatibility=plan.compatibility,
            started_at_utc=_utc_now(),
            completed_at_utc=_utc_now(),
            verification_state='not_required',
            outcome='no_upgrade',
            live_generation_state='unchanged',
        )

    started = _utc_now()
    event_common = {
        'upgrade_id': uuid4().hex,
        'from_build': plan.from_build_display,
        'to_build': plan.to_build_display,
        'from_schema': plan.current_schema_version,
        'to_schema': plan.target_schema_version,
        'compatibility': str(plan.compatibility),
        'started_at_utc': started,
    }

    # Resolve an interrupted managed-data restore before measuring or
    # snapshotting so the upgrade never builds a recovery generation over a
    # mid-swap directory.
    try:
        recover_interrupted_restore(data_dir)
    except Exception as exc:
        raise NativeUpgradeError(
            'a previous restore is still unresolved; refusing to upgrade '
            f'managed data until it is recovered: {exc}'
        ) from exc

    if plan.recovery_snapshot_required and plan.estimated_snapshot_bytes is not None:
        probe = data_dir if data_dir.is_dir() else data_dir.parent
        free = _free_space(probe) or plan.free_space_bytes
        if free is not None and free < plan.estimated_snapshot_bytes:
            event = UpgradeEvent(
                **event_common,
                completed_at_utc=_utc_now(),
                verification_state='pending',
                outcome='blocked',
                failure_summary=(
                    'insufficient free space for the pre-upgrade recovery copy: '
                    f'need ~{plan.estimated_snapshot_bytes} bytes, '
                    f'have {free} bytes'
                ),
            )
            _append_upgrade_event(data_dir, event)
            raise InsufficientUpgradeSpaceError(
                'HTDT needs to create a recovery copy of your data before '
                f'updating it (~{plan.estimated_snapshot_bytes} bytes), but '
                f'only {free} bytes are free. Free up space, then start HTDT '
                'again. Your data was not modified.'
            )

    snapshot_path: Path | None = None
    upgrade_stale_declaration: tuple = ()
    if plan.recovery_snapshot_required:
        snapshot_dir = upgrade_snapshot_dir(data_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / _snapshot_name(plan)
        try:
            # Canonical archive authority: the snapshot is a fully validated
            # .htdt-backup, restorable through the normal Restore workflow.
            # allow_stale so a store awaiting post-update revalidation can
            # still take its recovery copy — every stale row is declared in
            # the manifest and tolerated through post-migration verification.
            snapshot_manifest = create_backup(
                data_dir, snapshot_path, allow_stale=True
            )
            upgrade_stale_declaration = tuple(
                entry.model_dump(mode='json')
                for entry in snapshot_manifest.stale_authorities
            )
        except Exception as exc:
            event = UpgradeEvent(
                **event_common,
                completed_at_utc=_utc_now(),
                verification_state='pending',
                outcome='failed',
                failure_summary=f'pre-upgrade recovery snapshot failed: {exc}',
            )
            _append_upgrade_event(data_dir, event)
            raise NativeUpgradeError(
                'HTDT could not create a recovery copy before updating your '
                f'data: {exc}. Your data was not modified.'
            ) from exc

    if plan.compatibility == 'fresh_install':
        ensure_native_schema(plan.database_path)
        event = UpgradeEvent(
            **event_common,
            completed_at_utc=_utc_now(),
            verification_state='not_required',
            outcome='fresh_install',
            migration_commit_state='committed',
            live_generation_state='migrated_verified',
        )
        _append_upgrade_event(data_dir, event)
        return event

    # Durable marker BEFORE the migration mutates the live database: any
    # crash or failure after this point leaves a quarantinable record the
    # next startup must honor (#750).
    state_record = UpgradeStateRecord(
        state='migrating',
        upgrade_id=event_common['upgrade_id'],
        from_schema=plan.current_schema_version,
        to_schema=plan.target_schema_version,
        recovery_snapshot_ref=(
            str(snapshot_path) if snapshot_path is not None else None
        ),
        started_at_utc=started,
        updated_at_utc=_utc_now(),
        declared_stale_authorities=upgrade_stale_declaration,
    )
    write_upgrade_state(data_dir, state_record)

    try:
        ensure_native_schema(plan.database_path)
    except Exception as exc:
        # The migration transaction rolled back: the live database is
        # unchanged, so this failure does not quarantine the generation.
        state_record = state_record.model_copy(update={
            'state': 'failed_before_commit',
            'failure_stage': 'migration',
            'failure_summary': str(exc)[:500],
            'updated_at_utc': _utc_now(),
        })
        write_upgrade_state(data_dir, state_record)
        event = UpgradeEvent(
            **event_common,
            completed_at_utc=_utc_now(),
            recovery_snapshot_ref=(
                str(snapshot_path) if snapshot_path is not None else None
            ),
            verification_state='not_attempted',
            outcome='failed',
            failure_summary=str(exc)[:500],
            migration_commit_state='rolled_back',
            live_generation_state='unchanged',
            verification_failure_stage='migration',
        )
        _append_upgrade_event(data_dir, event)
        recovery = (
            'The update did not modify the live database, and a verified '
            f'recovery copy is at {snapshot_path} — restore it with the '
            'older HTDT build to return to the previous format.'
            if snapshot_path is not None
            else 'The update did not modify the live database.'
        )
        raise NativeUpgradeError(
            f'HTDT could not update your data from format '
            f'{plan.current_schema_version} to {plan.target_schema_version}: '
            f'{exc}\n\n{recovery}'
        ) from exc

    # The migration transaction committed. Persist the pending-verification
    # state BEFORE running verification so a crash mid-verify still leaves
    # the quarantine marker on disk. The recovery snapshot's stale
    # declaration joins the marker so a quarantine-retry tolerates exactly
    # the same pre-existing set.
    state_record = state_record.model_copy(update={
        'state': 'committed_pending_verification',
        'updated_at_utc': _utc_now(),
    })
    write_upgrade_state(data_dir, state_record)

    try:
        _verify_upgraded_database(
            plan.database_path,
            plan.target_schema_version,
            tolerated_stale=upgrade_stale_declaration,
        )
    except Exception as exc:
        stage = getattr(exc, 'stage', None) or 'verification'
        state_record = state_record.model_copy(update={
            'state': 'failed_after_commit',
            'failure_stage': stage,
            'failure_summary': str(exc)[:500],
            'updated_at_utc': _utc_now(),
        })
        write_upgrade_state(data_dir, state_record)
        event = UpgradeEvent(
            **event_common,
            completed_at_utc=_utc_now(),
            recovery_snapshot_ref=(
                str(snapshot_path) if snapshot_path is not None else None
            ),
            verification_state='failed',
            outcome='failed',
            failure_summary=str(exc)[:500],
            migration_commit_state='committed',
            live_generation_state='migrated_unverified',
            verification_failure_stage=stage,
        )
        _append_upgrade_event(data_dir, event)
        snapshot_hint = (
            'A verified recovery copy of the previous format is at '
            f'{snapshot_path}.'
            if snapshot_path is not None
            else 'No recovery copy could be created; see the diagnostic log.'
        )
        raise NativeUpgradeQuarantineError(
            'The data format update committed, but verification failed. '
            'HTDT will not open this generation for normal editing until it '
            'is verified or the recovery copy is restored. '
            f'{snapshot_hint}\n\nCause: {exc}',
            recovery_snapshot_ref=(
                str(snapshot_path) if snapshot_path is not None else None
            ),
        ) from exc

    clear_upgrade_state(data_dir)
    event = UpgradeEvent(
        **event_common,
        completed_at_utc=_utc_now(),
        recovery_snapshot_ref=str(snapshot_path) if snapshot_path is not None else None,
        verification_state='verified',
        outcome='completed',
        migration_commit_state='committed',
        live_generation_state='migrated_verified',
        stale_authority_count=len(upgrade_stale_declaration),
    )
    _append_upgrade_event(data_dir, event)
    # Only a fully verified migration may trim retained recovery
    # generations; a failed or pending one never displaces a known-good copy.
    if snapshot_path is not None:
        prune_upgrade_snapshots(data_dir, keep=keep_snapshots)
    _LOGGER.info(
        'native data upgrade completed: schema v%s -> v%s snapshot=%s',
        plan.current_schema_version,
        plan.target_schema_version,
        snapshot_path,
    )
    return event


def _resolve_quarantined_generation(
    data_dir: Path,
    plan: NativeUpgradePlan,
    marker: UpgradeStateRecord,
    *,
    keep_snapshots: int = KEEP_UPGRADE_SNAPSHOTS,
) -> UpgradeEvent:
    """Resolve a migrated-but-unverified live generation (#750).

    Post-migration verification is deterministic and read-only, so the
    safe retry is to rerun it: either it clears the quarantine or the
    failure is re-persisted and normal editing stays blocked. Restore of
    the pre-upgrade recovery copy is offered separately through the
    canonical Restore workflow.
    """

    event_common = {
        'upgrade_id': marker.upgrade_id,
        'from_build': None,
        'to_build': plan.to_build_display,
        'from_schema': marker.from_schema,
        'to_schema': marker.to_schema,
        'compatibility': str(plan.compatibility),
        'started_at_utc': _utc_now(),
    }
    try:
        _verify_upgraded_database(
            plan.database_path,
            plan.target_schema_version,
            tolerated_stale=marker_stale_declaration(marker),
        )
    except Exception as exc:
        stage = getattr(exc, 'stage', None) or 'verification'
        write_upgrade_state(
            data_dir,
            marker.model_copy(update={
                'state': 'failed_after_commit',
                'failure_stage': stage,
                'failure_summary': str(exc)[:500],
                'updated_at_utc': _utc_now(),
            }),
        )
        event = UpgradeEvent(
            **event_common,
            completed_at_utc=_utc_now(),
            recovery_snapshot_ref=marker.recovery_snapshot_ref,
            verification_state='failed',
            outcome='failed',
            failure_summary=str(exc)[:500],
            migration_commit_state='committed',
            live_generation_state='migrated_unverified',
            verification_failure_stage=stage,
        )
        _append_upgrade_event(data_dir, event)
        snapshot_hint = (
            'A verified recovery copy of the previous format is at '
            f'{marker.recovery_snapshot_ref}.'
            if marker.recovery_snapshot_ref is not None
            else 'No recovery copy is recorded; see the diagnostic log.'
        )
        raise NativeUpgradeQuarantineError(
            'The data format update committed, but verification failed. '
            'HTDT will not open this generation for normal editing until it '
            'is verified or the recovery copy is restored. '
            f'{snapshot_hint}\n\nCause: {exc}',
            recovery_snapshot_ref=marker.recovery_snapshot_ref,
        ) from exc

    clear_upgrade_state(data_dir)
    event = UpgradeEvent(
        **event_common,
        completed_at_utc=_utc_now(),
        recovery_snapshot_ref=marker.recovery_snapshot_ref,
        verification_state='verified',
        outcome='completed',
        migration_commit_state='committed',
        live_generation_state='migrated_verified',
        stale_authority_count=len(marker.declared_stale_authorities),
    )
    _append_upgrade_event(data_dir, event)
    # Re-verification earned the right to prune retained recovery
    # generations, same gate as a first-pass verified migration.
    if marker.recovery_snapshot_ref is not None:
        prune_upgrade_snapshots(data_dir, keep=keep_snapshots)
    _LOGGER.info(
        'previously quarantined generation re-verified: schema v%s',
        plan.target_schema_version,
    )
    return event


__all__ = [
    'IncompatibleNewerSchemaError',
    'InsufficientUpgradeSpaceError',
    'NativeUpgradeError',
    'NativeUpgradePlan',
    'NativeUpgradeQuarantineError',
    'NativeUpgradeVerificationError',
    'QUARANTINED_UPGRADE_STATES',
    'UpgradeEvent',
    'UpgradeStateRecord',
    'clear_upgrade_state',
    'execute_native_upgrade',
    'list_upgrade_events',
    'newer_schema_dialog_copy_ja',
    'newer_schema_guidance',
    'plan_native_upgrade',
    'prune_upgrade_snapshots',
    'read_upgrade_state',
    'upgrade_failure_recovery_ja',
    'upgrade_journal_path',
    'upgrade_snapshot_dir',
    'upgrade_state_path',
    'write_upgrade_state',
]
