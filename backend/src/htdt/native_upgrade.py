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


_LOGGER = logging.getLogger('htdt.native')

UPGRADE_RECOVERY_DIRNAME = 'upgrade-recovery'
UPGRADE_JOURNAL_NAME = 'upgrade-events.jsonl'
# Bounded retention for pre-upgrade generations: pruning only ever runs after
# a *new* migration has verified, so the last known-good upgrade snapshot is
# never removed to make room for an unproven one.
KEEP_UPGRADE_SNAPSHOTS = 3
SNAPSHOT_PREFIX = 'pre-upgrade'
UPGRADE_EVENT_SCHEMA_VERSION = 1


class NativeUpgradeError(RuntimeError):
    """The native data upgrade lifecycle could not complete safely."""


class IncompatibleNewerSchemaError(NativeUpgradeError):
    """The live database was created/upgraded by a newer HTDT build."""


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
                'HTDT needs to update project data from format 0.x '
                f'to {self.target_schema_version}. '
                'A recovery copy will be created first.'
            )
        return (
            'HTDT needs to update project data from format '
            f'{self.current_schema_version} to {self.target_schema_version}. '
            'A recovery copy will be created first.'
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    A missing or zero-byte database is a fresh install, not an upgrade of
    existing user data: no recovery generation is meaningful there. A
    non-empty unversioned database is a genuine legacy upgrade subject to
    ``cad_schema`` adoption gates.
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

    is_fresh = not database_path.is_file() or database_path.stat().st_size == 0
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
    except OSError:
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


def _snapshot_name(plan: NativeUpgradePlan) -> str:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return (
        f'{SNAPSHOT_PREFIX}-v{plan.current_schema_version}-to-'
        f'v{plan.target_schema_version}-{stamp}-{uuid4().hex[:8]}.htdt-backup'
    )


def _list_upgrade_snapshots(data_dir: Path) -> list[Path]:
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

    snapshots = _list_upgrade_snapshots(data_dir)
    for stale in snapshots[:-keep] if keep > 0 else snapshots:
        try:
            stale.unlink()
        except OSError:
            _LOGGER.warning('could not remove old upgrade snapshot %s', stale)


def _verify_upgraded_database(database_path: Path, expected_version: int) -> None:
    """Post-migration verification on the live database.

    Structural level: SQLite integrity + foreign keys + the recorded schema
    version + a real ``SceneRepository`` open. The bounded semantic level
    replays the #426 authority audit on a throwaway clone so a corrupt
    authority graph fails the upgrade rather than the first project open.
    """

    with closing(
        sqlite3.connect(f'file:{database_path.as_posix()}?mode=ro', uri=True)
    ) as connection:
        integrity = connection.execute('PRAGMA integrity_check').fetchall()
        if integrity != [('ok',)]:
            raise NativeUpgradeError(
                f'post-migration SQLite integrity check failed: {integrity!r}'
            )
        foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
        if foreign_keys:
            raise NativeUpgradeError(
                f'post-migration foreign-key check failed: {foreign_keys!r}'
            )
    stored = read_native_schema_version(database_path)
    if stored != expected_version:
        raise NativeUpgradeError(
            f'post-migration schema version is v{stored}, expected v{expected_version}'
        )
    try:
        from .cad_repository import SceneRepository

        SceneRepository(database_path)
    except (NativeSchemaError, sqlite3.DatabaseError) as exc:
        raise NativeUpgradeError(
            f'post-migration repository openability check failed: {exc}'
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
        from .native_authority_audit import assert_native_authority_graph

        try:
            assert_native_authority_graph(probe_path)
        except Exception as exc:
            raise NativeUpgradeError(
                f'post-migration semantic audit failed: {exc}'
            ) from exc
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
            )
        )

    if plan.compatibility == 'current':
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
    if plan.recovery_snapshot_required:
        snapshot_dir = upgrade_snapshot_dir(data_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / _snapshot_name(plan)
        try:
            # Canonical archive authority: the snapshot is a fully validated
            # .htdt-backup, restorable through the normal Restore workflow.
            create_backup(data_dir, snapshot_path)
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
        )
        _append_upgrade_event(data_dir, event)
        return event

    try:
        ensure_native_schema(plan.database_path)
        _verify_upgraded_database(plan.database_path, plan.target_schema_version)
    except Exception as exc:
        event = UpgradeEvent(
            **event_common,
            completed_at_utc=_utc_now(),
            recovery_snapshot_ref=(
                str(snapshot_path) if snapshot_path is not None else None
            ),
            verification_state='failed',
            outcome='failed',
            failure_summary=str(exc)[:500],
        )
        _append_upgrade_event(data_dir, event)
        recovery = (
            'Your data was not modified by the failed step, and a verified '
            f'recovery copy is at {snapshot_path} — restore it with the older '
            'HTDT build to return to the previous format.'
            if snapshot_path is not None
            else 'No recovery copy could be created; see the diagnostic log.'
        )
        raise NativeUpgradeError(
            f'HTDT could not update your data from format '
            f'{plan.current_schema_version} to {plan.target_schema_version}: '
            f'{exc}\n\n{recovery}'
        ) from exc

    event = UpgradeEvent(
        **event_common,
        completed_at_utc=_utc_now(),
        recovery_snapshot_ref=str(snapshot_path) if snapshot_path is not None else None,
        verification_state='verified',
        outcome='completed',
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


__all__ = [
    'IncompatibleNewerSchemaError',
    'InsufficientUpgradeSpaceError',
    'NativeUpgradeError',
    'NativeUpgradePlan',
    'UpgradeEvent',
    'execute_native_upgrade',
    'list_upgrade_events',
    'newer_schema_guidance',
    'plan_native_upgrade',
    'prune_upgrade_snapshots',
    'upgrade_journal_path',
    'upgrade_snapshot_dir',
]
