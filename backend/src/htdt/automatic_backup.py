"""Automatic local backup policy (#617).

Manual ``create_backup`` requires the user to remember; this module adds the
policy layer that decides *when* a new generation is warranted and *how
many* validated generations are retained — while delegating every archive
to the canonical ``native_backup`` authority (snapshot + asset contract +
round-trip validation).

Trigger policy (one documented interval policy, #755):

- ``periodic``: at most one automatic generation per ``interval_hours``,
  and only when managed data actually changed.
- ``clean_close``: a clean application shutdown may *opportunistically
  satisfy* a generation that is already due under the same interval
  policy — changed data whose interval elapsed. It never forces an
  extra archive inside the interval, and shutdown itself performs no
  archive work: the GUI records the close via ``record_clean_close`` and
  the next eligible scheduler tick performs the due generation.
- ``pre_destructive``: always, immediately before a destructive workflow
  (the caller invokes this at its own destructive boundary).

Change detection is a *cheap stat fingerprint* (database mtime/size plus
managed-asset count/size/newest mtime), not a content hash — hashing a
mature project's assets on every tick would defeat the "cheap" contract.

Generations live OUTSIDE the managed input tree (default:
``<data_dir>-backups`` sibling directory) and carry their classification in
the filename, so automatic/manual/pre-upgrade/pre-restore lifecycles are
never conflated (#752):

- ``AUTOMATIC_CLASSIFICATIONS`` (``automatic_periodic``,
  ``automatic_clean_close``) are the only classes the rotation policy may
  ever delete. ``manual``, ``pre_upgrade``, ``pre_restore`` and
  ``pre_destructive`` generations are restoration-sensitive: rotation
  never touches them regardless of age or count.
- Generation ordering is chronological by the creation timestamp encoded
  in the filename, never by file mtime or raw name ordering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .managed_assets import MANAGED_ASSETS_DIRNAME
from .native_backup import DATABASE_NAME, BackupManifest, create_backup


_LOGGER = logging.getLogger('htdt.native')

AUTOMATIC_BACKUP_POLICY_SCHEMA = 1
STATE_FILENAME = 'automatic-backup-state.json'
POLICY_FILENAME = 'automatic-backup-policy.json'
GENERATION_PREFIX = 'htdt-backup'
BACKUP_DIR_SUFFIX = '-backups'

# Fallback used when persisted policy is missing/corrupt.
DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_KEEP_GENERATIONS = 5
DEFAULT_KEEP_DAILY_GENERATIONS = 14


BackupTrigger = Literal['periodic', 'clean_close', 'pre_destructive']
BackupClassification = Literal[
    'manual',
    'automatic_periodic',
    'automatic_clean_close',
    'pre_upgrade',
    'pre_restore',
    'pre_destructive',
]

# Classes produced by the automatic policy itself; only these are ever
# rotated. Every other class is a user-visible or safety archive that
# automatic retention must never delete.
AUTOMATIC_CLASSIFICATIONS: frozenset[str] = frozenset(
    {'automatic_periodic', 'automatic_clean_close'}
)
_KNOWN_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        'manual',
        'automatic_periodic',
        'automatic_clean_close',
        'pre_upgrade',
        'pre_restore',
        'pre_destructive',
    }
)


class AutomaticBackupError(RuntimeError):
    pass


class AutomaticBackupPolicy(BaseModel):
    """Persisted user-visible policy for automatic generations."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = AUTOMATIC_BACKUP_POLICY_SCHEMA
    enabled: bool = True
    interval_hours: float = Field(gt=0, le=24 * 90)
    keep_generations: int = Field(ge=1, le=100)
    keep_daily_generations: int = Field(ge=0, le=366)
    # None = the default ``<data_dir>-backups`` sibling directory.
    backup_dir: str | None = None

    @classmethod
    def defaults(cls) -> 'AutomaticBackupPolicy':
        return cls(
            interval_hours=DEFAULT_INTERVAL_HOURS,
            keep_generations=DEFAULT_KEEP_GENERATIONS,
            keep_daily_generations=DEFAULT_KEEP_DAILY_GENERATIONS,
        )


class BackupGenerationRecord(BaseModel):
    """Typed inventory record for one generation on disk (#752).

    ``classification`` is parsed from the generation filename; None means
    the archive does not match the naming contract and is treated as
    restoration-sensitive (never rotated). ``created_at_utc`` is the
    creation timestamp encoded in the filename — the ordering authority,
    not file mtime.
    """

    model_config = ConfigDict(frozen=True)

    path: Path
    classification: BackupClassification | None
    created_at_utc: datetime | None

    @property
    def is_automatic(self) -> bool:
        return self.classification in AUTOMATIC_CLASSIFICATIONS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def backups_dir(data_dir: Path, policy: AutomaticBackupPolicy) -> Path:
    if policy.backup_dir:
        return Path(policy.backup_dir)
    data_dir = Path(data_dir)
    return data_dir.parent / f'{data_dir.name}{BACKUP_DIR_SUFFIX}'


def policy_path(data_dir: Path) -> Path:
    return Path(data_dir) / POLICY_FILENAME


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / STATE_FILENAME


def managed_data_fingerprint(data_dir: Path) -> str:
    """Cheap change-generation marker over the managed input tree.

    Stat-level only: database size+mtime plus managed-asset count, total
    bytes and newest mtime. Content hashes are intentionally not used —
    this marker must stay cheap on mature projects.
    """

    data_dir = Path(data_dir)
    parts: list[str] = []
    database = data_dir / DATABASE_NAME
    try:
        stat = database.stat()
        parts.append(f'db:{stat.st_size}:{stat.st_mtime_ns}')
    except OSError:
        parts.append('db:absent')
    # Commit-level change marker: the SQLite file change counter (bytes
    # 24-27 of the database header) bumps on every commit even when size
    # and mtime happen to stay identical.
    try:
        with database.open('rb') as stream:
            header = stream.read(28)
        counter = int.from_bytes(header[24:28], 'big')
        parts.append(f'counter:{counter}')
    except (OSError, IndexError):
        pass
    # The SQLite journal alongside the DB also mutates on write.
    for suffix in ('-wal', '-shm', '-journal'):
        side = Path(f'{database}{suffix}')
        try:
            stat = side.stat()
            parts.append(f'{side.name}:{stat.st_size}:{stat.st_mtime_ns}')
        except OSError:
            pass
    assets_root = data_dir / MANAGED_ASSETS_DIRNAME
    count = 0
    total = 0
    newest = 0
    if assets_root.is_dir():
        try:
            for candidate in assets_root.iterdir():
                try:
                    if not candidate.is_file():
                        continue
                    stat = candidate.stat()
                except OSError:
                    continue
                count += 1
                total += stat.st_size
                newest = max(newest, stat.st_mtime_ns)
        except OSError:
            pass
    parts.append(f'assets:{count}:{total}:{newest}')
    return '|'.join(parts)


class AutomaticBackupScheduler:
    """Decide and run automatic backup generations for one data dir."""

    def __init__(
        self,
        data_dir: Path,
        policy: AutomaticBackupPolicy | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.policy = policy or self.load_policy()

    # -- persistence -----------------------------------------------------

    def load_policy(self) -> AutomaticBackupPolicy:
        path = policy_path(self.data_dir)
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            return AutomaticBackupPolicy.model_validate(payload)
        except (OSError, ValueError):
            return AutomaticBackupPolicy.defaults()

    def save_policy(self, policy: AutomaticBackupPolicy) -> Path:
        path = policy_path(self.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        temp.write_text(
            json.dumps(policy.model_dump(mode='json'), indent=2, sort_keys=True),
            encoding='utf-8',
        )
        os.replace(temp, path)
        return path

    def _load_state(self) -> dict[str, object]:
        path = state_path(self.data_dir)
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_state(self, state: dict[str, object]) -> None:
        path = state_path(self.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        temp.write_text(
            json.dumps(state, indent=2, sort_keys=True),
            encoding='utf-8',
        )
        os.replace(temp, path)

    # -- generations -----------------------------------------------------

    def _generation_name(
        self, classification: BackupClassification
    ) -> str:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        return (
            f'{GENERATION_PREFIX}-{classification}-'
            f'{stamp}-{uuid4().hex[:8]}.htdt-backup'
        )

    def _parse_generation(self, path: Path) -> BackupGenerationRecord:
        """Parse ``htdt-backup-<class>-<stamp>-<id>.htdt-backup`` names."""

        stem = path.name
        if stem.endswith('.htdt-backup'):
            stem = stem[: -len('.htdt-backup')]
        parts = stem.split('-')
        classification: BackupClassification | None = None
        created_at: datetime | None = None
        if len(parts) >= 4 and parts[2] in _KNOWN_CLASSIFICATIONS:
            classification = parts[2]  # type: ignore[assignment]
            try:
                created_at = datetime.strptime(
                    parts[3], '%Y%m%dT%H%M%SZ'
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                created_at = None
        return BackupGenerationRecord(
            path=path,
            classification=classification,
            created_at_utc=created_at,
        )

    def generation_records(self) -> tuple[BackupGenerationRecord, ...]:
        """Typed inventory of all generations, newest first by creation
        timestamp recorded in the filename (#752).

        Every ``htdt-backup-*`` archive is listed — manual, safety and
        automatic alike — so inventory stays complete; classification
        decides what rotation may touch, not listing.
        """

        directory = backups_dir(self.data_dir, self.policy)
        if not directory.is_dir():
            return ()
        records = [
            self._parse_generation(candidate)
            for candidate in directory.iterdir()
            if candidate.is_file()
            and candidate.name.startswith(f'{GENERATION_PREFIX}-')
            and candidate.name.endswith('.htdt-backup')
        ]
        return tuple(
            sorted(
                records,
                key=lambda record: (
                    record.created_at_utc or datetime.min.replace(tzinfo=timezone.utc),
                    record.path.name,
                ),
                reverse=True,
            )
        )

    def list_generations(self) -> tuple[Path, ...]:
        """All generations, newest first by recorded creation time."""

        return tuple(record.path for record in self.generation_records())

    def prune_generations(self) -> tuple[Path, ...]:
        """Apply the rotation policy: rolling N newest + daily coverage.

        Scoped to ``AUTOMATIC_CLASSIFICATIONS`` only (#752): manual,
        pre-upgrade, pre-restore and pre-destructive generations — and any
        archive whose classification cannot be determined — are
        restoration-sensitive and are never deleted here. Within the
        automatic class, keeps the newest ``keep_generations`` generations
        always, plus the newest generation of each UTC day while that day
        has no already-retained representative, up to
        ``keep_daily_generations`` distinct days.
        """

        automatic = [
            record for record in self.generation_records() if record.is_automatic
        ]
        keep: set[Path] = {
            record.path for record in automatic[: self.policy.keep_generations]
        }
        day_seen: set[str] = set()
        day_kept = 0
        for record in automatic:
            day = (
                record.created_at_utc.strftime('%Y%m%d')
                if record.created_at_utc is not None
                else None
            )
            if record.path in keep:
                if day:
                    day_seen.add(day)
                continue
            if (
                day is not None
                and day not in day_seen
                and day_kept < self.policy.keep_daily_generations
            ):
                day_seen.add(day)
                day_kept += 1
                keep.add(record.path)
        removed: list[Path] = []
        for record in automatic:
            if record.path in keep:
                continue
            try:
                record.path.unlink()
                removed.append(record.path)
            except OSError:
                _LOGGER.warning(
                    'could not prune backup generation %s', record.path
                )
        return tuple(removed)

    # -- trigger evaluation -----------------------------------------------

    def evaluate(self, trigger: BackupTrigger) -> tuple[bool, str]:
        """Return (should_run, reason) for one trigger point."""

        if not self.policy.enabled:
            return False, 'automatic backups are disabled'
        fingerprint = managed_data_fingerprint(self.data_dir)
        state = self._load_state()
        last_fingerprint = str(state.get('fingerprint', ''))
        changed = fingerprint != last_fingerprint
        has_data = (self.data_dir / DATABASE_NAME).is_file()

        if trigger == 'pre_destructive':
            if not has_data:
                return False, 'no managed data exists yet'
            return True, 'safety generation before a destructive operation'
        if not has_data:
            return False, 'no managed data exists yet'
        if not changed:
            return False, 'managed data unchanged since last backup'
        # periodic and clean_close share one interval policy (#755): a
        # clean close may satisfy a backup that is already due, but never
        # forces an extra archive inside the interval.
        last_run = state.get('last_automatic_at_utc')
        if last_run is None:
            due_reason = 'no automatic backup has ever run'
        else:
            try:
                elapsed = datetime.now(timezone.utc) - _parse_utc(str(last_run))
            except ValueError:
                due_reason = 'last automatic backup time is unreadable'
            else:
                if elapsed < timedelta(hours=self.policy.interval_hours):
                    return False, (
                        f'within interval ({elapsed} < '
                        f'{self.policy.interval_hours}h)'
                    )
                due_reason = (
                    f'last automatic backup was {elapsed} ago '
                    f'(interval {self.policy.interval_hours}h)'
                )
        if trigger == 'clean_close':
            return True, f'clean shutdown satisfies a due backup: {due_reason}'
        return True, due_reason

    # -- run ---------------------------------------------------------------

    def run_due(
        self,
        trigger: BackupTrigger,
        *,
        classification: BackupClassification | None = None,
        force: bool = False,
    ) -> tuple[Path, BackupManifest] | None:
        """Create a validated generation when the trigger says it is due.

        Returns the (path, manifest) pair of the new generation, or None
        when evaluation said no backup was warranted.
        """

        should, reason = self.evaluate(trigger)
        if not should and not force:
            _LOGGER.info('automatic backup skipped (%s): %s', trigger, reason)
            return None
        kind: BackupClassification = classification or (
            'automatic_periodic'
            if trigger == 'periodic'
            else 'pre_destructive'
            if trigger == 'pre_destructive'
            else 'automatic_clean_close'
        )
        destination_dir = backups_dir(self.data_dir, self.policy)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / self._generation_name(kind)
        # Canonical archive authority — the generation is fully validated
        # before it replaces any previous generation.
        manifest = create_backup(self.data_dir, destination)
        # A generation of ANY class proves the current fingerprint is
        # covered, but only routine automatic classes advance the periodic
        # interval clock — a manual/safety archive must not postpone the
        # next due automatic generation (#752).
        state = self._load_state()
        if kind in AUTOMATIC_CLASSIFICATIONS:
            state['last_automatic_at_utc'] = _utc_now()
        state.update({
            'schema_version': AUTOMATIC_BACKUP_POLICY_SCHEMA,
            'fingerprint': managed_data_fingerprint(self.data_dir),
            'last_generation': str(destination),
            'classification': kind,
            'clean_close_pending': False,
        })
        self._save_state(state)
        self.prune_generations()
        _LOGGER.info(
            'automatic backup created: %s (classification=%s trigger=%s)',
            destination,
            kind,
            trigger,
        )
        return destination, manifest

    # -- clean-close hint --------------------------------------------------

    def record_clean_close(self) -> None:
        """Record a clean UI shutdown without performing any archive work.

        #755: shutdown never runs a backup inside teardown — this writes a
        cheap state marker only. The next eligible scheduler tick
        (``run_due('periodic')`` or an in-session ``run_due('clean_close')``
        decision point while the UI is still alive) performs the due
        generation under the normal interval policy.
        """

        state = self._load_state()
        state['schema_version'] = AUTOMATIC_BACKUP_POLICY_SCHEMA
        state['last_clean_close_at_utc'] = _utc_now()
        state['clean_close_pending'] = (
            managed_data_fingerprint(self.data_dir)
            != str(state.get('fingerprint', ''))
        )
        self._save_state(state)


__all__ = [
    'AUTOMATIC_CLASSIFICATIONS',
    'AutomaticBackupError',
    'AutomaticBackupPolicy',
    'AutomaticBackupScheduler',
    'BackupGenerationRecord',
    'backups_dir',
    'managed_data_fingerprint',
    'policy_path',
    'state_path',
]
