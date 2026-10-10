"""Managed data relocation (#621): move the live root to another drive.

The HTDT data directory holds the live database and managed evidence; users
with nearly-full drives need a supported way to move that root instead of
hand-copying it and breaking every authority invariant.

This module owns three things:

1. ``HTDTBootstrapConfig`` — the pointer from the user profile to the
   *current* managed root. It lives OUTSIDE the managed root itself so it
   survives the move; startup precedence is explicit ``--data-dir`` >
   bootstrap config > platform default. A configured-but-unavailable root
   is a first-class error, never a silent fall-back to the default.
2. ``plan_data_relocation`` — read-only preflight: byte estimates, free
   space, and the hard blockers (destination inside source, non-empty
   destination, source in use, ...).
3. ``execute_data_relocation`` — copy → verify → cutover. The source is
   kept, intact, until the staged copy at the destination has been
   verified; it is then parked as ``<source>.relocated-<stamp>`` rather
   than deleted, so nothing is unrecoverable.

Relocation runs on a quiesced data directory — the caller must not hold
the instance lock and no other HTDT process may.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .managed_assets import (
    MANAGED_ASSETS_DIRNAME,
    ManagedAssetError,
    managed_asset_path,
    sha256_file,
)
from .cad_schema import connect_sqlite
from .native_backup import DATABASE_NAME, recover_interrupted_restore
from .persisted_data import component_for_path, relocation_carried_components
from .runtime_instance import (
    LOCK_FILENAME,
    RUNTIME_FILENAME,
    SingleInstanceGuard,
    _lock_first_byte,
    _unlock_first_byte,
)
from .clock import utc_now_iso as _utc_now


_LOGGER = logging.getLogger('htdt.native')

BOOTSTRAP_SCHEMA_VERSION = 1
BOOTSTRAP_FILENAME = 'htdt-bootstrap.json'
RELOCATED_SUFFIX = '.relocated-'

# #751: relocation cutover runs under an external transaction authority that
# lives OUTSIDE either renamed directory — beside the bootstrap pointer —
# so no directory rename can ever release it.
RELOCATION_LOCK_FILENAME = 'htdt-relocation.lock'
RELOCATION_JOURNAL_FILENAME = 'htdt-relocation.json'

RelocationPhase = Literal[
    'PREPARED',
    'STAGED_VERIFIED',
    'DESTINATION_PROMOTED',
    'SOURCE_PARKED',
    'BOOTSTRAP_SWITCHED',
    'COMPLETED',
]

# #751 section 6 — explicit root-local file classification. Canonical state
# moves with the root; operational residue is regenerated at the destination;
# the instance lock and runtime pointer are recreated by the next launch and
# must never be copied into a still-running directory's successor.
_ROOT_FILES_TO_MOVE: tuple[str, ...] = (
    DATABASE_NAME,
    # Automatic-backup configuration/state is root-coupled: a canonical-root
    # move must not silently reset the user's backup policy.
    'automatic-backup-policy.json',
    'automatic-backup-state.json',
)
_ROOT_DIRECTORIES_TO_MOVE: tuple[str, ...] = (MANAGED_ASSETS_DIRNAME,)
_ROOT_NAMES_LEFT_BEHIND: frozenset[str] = frozenset(
    {
        LOCK_FILENAME,
        RUNTIME_FILENAME,
        RELOCATION_JOURNAL_FILENAME,
        RELOCATION_LOCK_FILENAME,
    }
)

#: ``<name>.<n>`` numbered archive generations next to a carried
#: component (e.g. ``htdt.migrated.sqlite3.2``).
_NUMBERED_SIBLING = re.compile(r'^(?P<base>.+)\.(?P<n>\d+)$')


def _numbered_siblings(source_dir: Path, component_path: str) -> list[str]:
    """Existing ``<component_path>.<n>`` siblings, sorted for determinism."""

    names: list[str] = []
    for entry in source_dir.iterdir():
        match = _NUMBERED_SIBLING.fullmatch(entry.name)
        if match is not None and match.group('base') == component_path:
            names.append(entry.name)
    return sorted(names, key=lambda n: int(_NUMBERED_SIBLING.fullmatch(n).group('n')))


def _is_operational_residue(name: str) -> bool:
    """Logs, temp files and drop-queue residue are regenerated, not moved."""

    lowered = name.lower()
    return (
        name in _ROOT_NAMES_LEFT_BEHIND
        or lowered.endswith(('.log', '.tmp', '.bak'))
        or lowered.startswith('.')
    )


class ManagedDataUnavailableError(RuntimeError):
    """The configured managed data root is not usable right now."""


class DataRelocationError(RuntimeError):
    pass


class DataRelocationBlockedError(DataRelocationError):
    pass


class DataRelocationCancelledError(DataRelocationError):
    """A relocation honored a cooperative cancel request before commit.

    Raised only while the journal is still in its PREPARED phase — the
    cleanup path removes the staged copy and the journal, leaving the
    source untouched. After STAGED_VERIFIED the cutover always completes.
    """


def _raise_if_relocation_cancelled(
    is_cancelled: Callable[[], bool] | None,
) -> None:
    if is_cancelled is not None and is_cancelled():
        raise DataRelocationCancelledError(
            'data relocation cancelled by the caller'
        )


class HTDTBootstrapConfig(BaseModel):
    """The persistent pointer to the managed data root.

    Stored outside the managed root (``%LOCALAPPDATA%\\HTDT\\htdt-bootstrap.json``
    on Windows, ``~/.htdt/htdt-bootstrap.json`` elsewhere) so it survives
    the move it describes.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = BOOTSTRAP_SCHEMA_VERSION
    data_dir: str = Field(min_length=1)
    written_at_utc: str = Field(min_length=1)

    @field_validator('data_dir')
    @classmethod
    def _no_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('data_dir must be non-empty')
        return value


def bootstrap_config_path() -> Path:
    """Where the bootstrap pointer lives — outside any managed root."""

    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        return Path(local_app_data) / 'HTDT' / BOOTSTRAP_FILENAME
    return Path.home() / '.htdt' / BOOTSTRAP_FILENAME


def load_bootstrap_config(
    path: Path | None = None,
) -> HTDTBootstrapConfig | None:
    path = path or bootstrap_config_path()
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return HTDTBootstrapConfig.model_validate(payload)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as exc:
        _LOGGER.warning('unreadable bootstrap config %s: %s', path, exc)
        return None


def save_bootstrap_config(
    config: HTDTBootstrapConfig, path: Path | None = None
) -> Path:
    path = path or bootstrap_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    temp.write_text(
        json.dumps(config.model_dump(mode='json'), indent=2, sort_keys=True, allow_nan=False),
        encoding='utf-8',
    )
    os.replace(temp, path)
    return path


def resolve_data_dir(
    explicit: Path | None,
    *,
    bootstrap_path: Path | None = None,
    default: Path | None = None,
) -> tuple[Path, Literal['explicit', 'bootstrap', 'default']]:
    """Resolve the managed root: ``--data-dir`` > bootstrap > default."""

    if explicit is not None:
        return Path(explicit), 'explicit'
    config = load_bootstrap_config(bootstrap_path)
    if config is not None:
        return Path(config.data_dir), 'bootstrap'
    if default is None:
        from .runtime_instance import default_data_dir

        default = default_data_dir()
    return Path(default), 'default'


def assert_managed_root_available(
    root: Path,
    source: Literal['explicit', 'bootstrap', 'default'],
    *,
    bootstrap_path: Path | None = None,
) -> None:
    """Fail closed when the resolved root does not exist.

    An explicit path may create a new directory. A bootstrap-configured
    root that has gone missing (drive unplugged, directory moved) is an
    error — silently opening the default instead would look like data loss.

    #751: a durable relocation journal outside the managed roots is
    authoritative for cutover state. A *stale* journal (crash boundary)
    is settled deterministically here; a *live* one means the resolved
    root is either mid-relocation source or a reserved destination —
    either way it must not be opened.
    """

    root = Path(root)
    journal = _read_relocation_journal(
        _relocation_journal_path(bootstrap_path)
    )
    if journal is not None:
        lock = _RelocationLock(bootstrap_path)
        if lock.acquire():
            try:
                _recover_journal_locked(
                    journal, bootstrap_path=bootstrap_path
                )
            finally:
                lock.release()
            journal = _read_relocation_journal(
                _relocation_journal_path(bootstrap_path)
            )
    if journal is not None:
        resolved_root = _canonical(root)
        phase_detail = (
            f'a managed-data relocation ({journal.transaction_id}) is in '
            f'progress (phase {journal.phase})'
        )
        if resolved_root == _canonical(journal.source_dir):
            raise ManagedDataUnavailableError(
                f'{root} is the source of {phase_detail}; it must not be '
                'opened until the cutover completes'
            )
        if resolved_root == _canonical(journal.destination_dir):
            raise ManagedDataUnavailableError(
                f'{root} is reserved as the destination of {phase_detail}'
            )
    if root.exists() and not root.is_dir():
        # A file sitting where the managed root belongs fails identically
        # for explicit/default/bootstrap — without this guard the first
        # directory creation downstream crashes on an opaque FileExistsError.
        raise ManagedDataUnavailableError(
            f'HTDT data directory {root} is occupied by a file, not a '
            'directory. Remove or rename the file, or start HTDT with a '
            'different --data-dir.'
        )
    if source == 'bootstrap' and not root.is_dir():
        raise ManagedDataUnavailableError(
            f'HTDT data directory {root} (from the bootstrap configuration) '
            'is not available. The drive may be disconnected or the data '
            'may have been moved. Reconnect the drive, restore the '
            'directory, or start HTDT with an explicit --data-dir.'
        )


@dataclass(frozen=True)
class RelocationBlocker:
    kind: str
    detail: str


@dataclass(frozen=True)
class ManagedDataRelocationPlan:
    source_dir: Path
    destination_dir: Path
    database_bytes: int
    asset_count: int
    asset_bytes: int
    total_bytes: int
    free_space_bytes: int | None
    blockers: tuple[RelocationBlocker, ...]

    @property
    def executable(self) -> bool:
        return not self.blockers


def _tree_stats(assets_root: Path) -> tuple[int, int]:
    if not assets_root.is_dir():
        return 0, 0
    count = 0
    total = 0
    for candidate in assets_root.iterdir():
        try:
            if candidate.is_file():
                count += 1
                total += candidate.stat().st_size
        except OSError:
            continue
    return count, total


def _is_within(path: Path, ancestor: Path) -> bool:
    try:
        path.resolve().relative_to(ancestor.resolve())
        return True
    except ValueError:
        return False


def _destination_has_live_data(destination: Path) -> bool:
    database = destination / DATABASE_NAME
    return database.is_file() and database.stat().st_size > 0


def plan_data_relocation(
    source_dir: Path, destination_dir: Path
) -> ManagedDataRelocationPlan:
    """Read-only preflight for moving the managed root."""

    source_dir = Path(source_dir)
    destination_dir = Path(destination_dir)
    database = source_dir / DATABASE_NAME
    database_bytes = 0
    if database.is_file():
        try:
            database_bytes = database.stat().st_size
        except OSError:
            pass
    asset_count, asset_bytes = _tree_stats(
        source_dir / MANAGED_ASSETS_DIRNAME
    )
    # Root-tied registry components (preferences, history, recovery
    # generations, ...) are carried too — account for their bytes so the
    # preflight free-space estimate is not silently optimistic (#769).
    carried_bytes = 0
    for component in relocation_carried_components():
        carried = source_dir / component.path
        if component.is_directory:
            _count, carried = _tree_stats(carried)
            carried_bytes += carried
        elif carried.is_file():
            try:
                carried_bytes += carried.stat().st_size
            except OSError:
                pass
    total = database_bytes + asset_bytes + carried_bytes

    free: int | None = None
    probe = (
        destination_dir
        if destination_dir.is_dir()
        else destination_dir.parent
    )
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        pass

    blockers: list[RelocationBlocker] = []
    if not database.is_file():
        blockers.append(
            RelocationBlocker(
                'source_has_no_data',
                f'{source_dir} does not contain a live HTDT database',
            )
        )
    if source_dir.resolve() == destination_dir.resolve():
        blockers.append(
            RelocationBlocker(
                'same_location',
                'destination is the current data directory',
            )
        )
    if _is_within(destination_dir, source_dir):
        blockers.append(
            RelocationBlocker(
                'destination_inside_source',
                'destination is inside the managed data directory',
            )
        )
    if _is_within(source_dir, destination_dir):
        blockers.append(
            RelocationBlocker(
                'source_inside_destination',
                'the managed data directory is inside the destination',
            )
        )
    if destination_dir.exists() and any(destination_dir.iterdir()):
        if _destination_has_live_data(destination_dir):
            blockers.append(
                RelocationBlocker(
                    'destination_has_live_data',
                    f'{destination_dir} already contains a live HTDT '
                    'database; choose an empty destination',
                )
            )
        else:
            blockers.append(
                RelocationBlocker(
                    'destination_not_empty',
                    f'{destination_dir} is not empty; choose an empty '
                    'destination',
                )
            )
    if free is not None and free < total:
        blockers.append(
            RelocationBlocker(
                'insufficient_space',
                f'destination needs ~{total} bytes, only {free} free',
            )
        )
    return ManagedDataRelocationPlan(
        source_dir=source_dir,
        destination_dir=destination_dir,
        database_bytes=database_bytes,
        asset_count=asset_count,
        asset_bytes=asset_bytes,
        total_bytes=total,
        free_space_bytes=free,
        blockers=tuple(blockers),
    )


def _promote_directory(source: Path, destination: Path) -> None:
    """``os.replace`` a directory onto the destination path.

    On Windows ``os.replace`` refuses to move a directory onto ANY existing
    directory — even an empty one (WinError 5) — while a POSIX rename only
    replaces an empty directory. The relocation plan explicitly allows an
    existing *empty* destination, so remove it first; a non-empty one still
    fails honestly rather than merging into foreign content.
    """

    if destination.is_dir() and not any(destination.iterdir()):
        destination.rmdir()
    os.replace(source, destination)


def _park_directory(source: Path, parked: Path) -> Path:
    """Move *source* aside to *parked*, returning the name actually used.

    A leftover or foreign occupant at the requested park slot can never be
    overwritten silently: an empty placeholder is reclaimed, a populated
    one diverts the park to ``<parked>.<n>`` so both generations survive.
    """

    target = parked
    suffix = 1
    while target.exists() and (not target.is_dir() or any(target.iterdir())):
        target = parked.with_name(f'{parked.name}.{suffix}')
        suffix += 1
    if target.exists():
        target.rmdir()
    os.replace(source, target)
    return target


def _locate_parked_source(
    journal: RelocationJournal, parked: Path
) -> Path | None:
    """Resolve the directory holding the pre-relocation generation.

    ``journal.parked_dir`` is only the *planned* park name until the
    SOURCE_PARKED journal write lands: a crash between the park rename
    and that write leaves the real generation at a diverted
    ``<parked>.<n>`` sibling while the journaled name may hold an older
    parked generation or foreign content. The database digest recorded
    at PREPARED identifies the generation by content, so a stale plan
    name can never promote the wrong directory back live. Journals from
    before the digest was recorded fall back to trusting the journaled
    name exactly.
    """

    digest = journal.source_database_sha256
    if digest is None:
        return parked if parked.is_dir() else None
    prefix = f'{parked.name}.'
    try:
        siblings = list(parked.parent.iterdir())
    except OSError:
        siblings = []
    candidates = [parked] + sorted(
        (
            sibling
            for sibling in siblings
            if sibling.name.startswith(prefix)
            and sibling.name[len(prefix) :].isdigit()
        ),
        key=lambda sibling: int(sibling.name[len(prefix) :]),
    )
    for candidate in candidates:
        database = candidate / DATABASE_NAME
        if not candidate.is_dir() or not database.is_file():
            continue
        try:
            if sha256_file(database) == digest:
                return candidate
        except OSError:
            continue
    return None


def _verify_staged_root(staged: Path) -> None:
    """Verification phase: the copied root must be a valid data directory."""

    database = staged / DATABASE_NAME
    if not database.is_file():
        raise DataRelocationError(
            f'staged copy is missing {DATABASE_NAME}'
        )
    with closing(connect_sqlite(database)) as connection:
        integrity = connection.execute('PRAGMA integrity_check').fetchall()
        if len(integrity) != 1 or integrity[0][0] != 'ok':
            raise DataRelocationError(
                f'staged database integrity check failed: {integrity!r}'
            )
        # Re-assert the managed-byte contract on the staged copy: every
        # row the authority audit binds to bytes (``_ASSET_TABLES``) must
        # have a copied file under the staged root with the recorded
        # digest — not only the two byte-store tables. Digest-only rows
        # resolve to their content address; registry rows declare a
        # ``relative_path`` resolved through the shared containment check.
        from .native_authority_audit import _ASSET_TABLES

        for table, sha_column, size_column, path_column, where in (
            _ASSET_TABLES
        ):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists is None:
                continue
            for row in connection.execute(f'SELECT * FROM {table}'):
                record = dict(row)
                if not _staged_row_matches_asset_where(record, where):
                    continue
                digest = str(record[sha_column])
                if not _SHA256_ROW_RE.match(digest):
                    raise DataRelocationError(
                        f'staged asset digest malformed: '
                        f'{table}.{sha_column}={digest}'
                    )
                if path_column is not None and record.get(path_column):
                    relative = str(record[path_column])
                else:
                    relative = f'{MANAGED_ASSETS_DIRNAME}/{digest}'
                try:
                    candidate = managed_asset_path(staged, relative)
                except ManagedAssetError as exc:
                    raise DataRelocationError(str(exc)) from exc
                if not candidate.is_file():
                    raise DataRelocationError(
                        f'staged asset missing: {relative}'
                    )
                if (
                    size_column is not None
                    and record.get(size_column) is not None
                    and candidate.stat().st_size
                    != int(record[size_column])
                ):
                    raise DataRelocationError(
                        f'staged asset size mismatch: {relative}'
                    )
                if sha256_file(candidate) != digest:
                    raise DataRelocationError(
                        f'staged asset digest mismatch: {relative}'
                    )


_SHA256_ROW_RE = re.compile(r'^[0-9a-f]{64}$')


def _staged_row_matches_asset_where(
    record: dict, where: str | None
) -> bool:
    """Evaluate the audit registry's row predicates on one staged row.

    Supported forms mirror ``project_bundle._row_matches_asset_where``:
    ``column='literal'`` and ``column IS NOT NULL``. Anything else fails
    closed — an unknown predicate shape cannot silently skip coverage.
    """

    if where is None:
        return True
    if where.endswith(' IS NOT NULL'):
        column = where[: -len(' IS NOT NULL')].strip()
        return record.get(column) is not None
    column, _eq, literal = where.partition('=')
    column = column.strip()
    literal = literal.strip()
    if literal.startswith("'") and literal.endswith("'") and column:
        return str(record.get(column)) == literal[1:-1]
    raise DataRelocationError(
        f'staged asset coverage predicate is not understood: {where}'
    )


# --------------------------------------------------------------------------
# #751 durable relocation journal — external transaction authority
# --------------------------------------------------------------------------


class RelocationJournal(BaseModel):
    """Crash-survivable record of one cutover transaction.

    Lives beside the bootstrap pointer — outside either managed root —
    so a rename of source or destination can never strand it. ``phase``
    is the last fsync'd boundary the transaction passed; recovery replays
    from it.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    transaction_id: str = Field(min_length=1)
    phase: RelocationPhase
    source_dir: str = Field(min_length=1)
    destination_dir: str = Field(min_length=1)
    staged_dir: str = Field(min_length=1)
    parked_dir: str = Field(min_length=1)
    source_database_sha256: str | None = None
    staged_database_sha256: str | None = None
    created_at_utc: str = Field(min_length=1)
    updated_at_utc: str = Field(min_length=1)


class _RelocationLock:
    """Non-blocking exclusive lock on the relocation journal directory.

    The lock file sits beside the bootstrap pointer — never inside a
    directory that gets renamed — so the coordination authority survives
    the whole cutover, unlike the per-root ``SingleInstanceGuard`` which
    must be released before the source rename on Windows.
    """

    def __init__(self, bootstrap_path: Path | None = None) -> None:
        bootstrap = bootstrap_path or bootstrap_config_path()
        self._path = bootstrap.parent / RELOCATION_LOCK_FILENAME
        self._file = None
        self._acquired = False

    def acquire(self) -> bool:
        if self._acquired:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        file = os.fdopen(fd, 'r+b', buffering=0)
        try:
            _lock_first_byte(file)
        except OSError:
            file.close()
            return False
        self._file = file
        self._acquired = True
        return True

    def release(self) -> None:
        if not self._acquired:
            return
        file = self._file
        self._file = None
        self._acquired = False
        if file is None:
            return
        try:
            _unlock_first_byte(file)
        finally:
            file.close()

    def __enter__(self) -> '_RelocationLock':
        if not self.acquire():
            raise DataRelocationBlockedError(
                'another managed-data relocation is in progress'
            )
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


def _relocation_journal_path(bootstrap_path: Path | None = None) -> Path:
    bootstrap = bootstrap_path or bootstrap_config_path()
    return bootstrap.parent / RELOCATION_JOURNAL_FILENAME


def _canonical(path: Path | str) -> Path:
    p = Path(path)
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


def _read_relocation_journal(path: Path) -> RelocationJournal | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return RelocationJournal.model_validate(payload)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as exc:
        _LOGGER.warning('unreadable relocation journal %s: %s', path, exc)
        return None


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_relocation_journal(
    journal: RelocationJournal, path: Path
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    with open(temp, 'w', encoding='utf-8') as file:
        file.write(
            json.dumps(
                journal.model_dump(mode='json'), indent=2, sort_keys=True,
                allow_nan=False,
            )
        )
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)
    _fsync_directory(path.parent)


def _journal_with_phase(
    journal: RelocationJournal,
    phase: RelocationPhase,
    path: Path,
    **updates: object,
) -> RelocationJournal:
    advanced = journal.model_copy(
        update={'phase': phase, 'updated_at_utc': _utc_now(), **updates}
    )
    _write_relocation_journal(advanced, path)
    return advanced


@dataclass(frozen=True)
class RelocationRecoveryEvent:
    action: str
    detail: str


def _staged_database(journal: RelocationJournal) -> Path:
    return Path(journal.staged_dir) / DATABASE_NAME


def _recover_journal_locked(
    journal: RelocationJournal,
    *,
    bootstrap_path: Path | None = None,
) -> list[RelocationRecoveryEvent]:
    """Settle one journal deterministically. Caller holds the lock.

    Any phase before DESTINATION_PROMOTED leaves the source untouched and
    the staged copy disposable; any phase at or past it has a verified
    destination that must end up both live and pointed at by the bootstrap
    pointer — recovery completes the remaining steps rather than rolling
    a verified generation back under a fallback window.
    """

    journal_path = _relocation_journal_path(bootstrap_path)
    source = Path(journal.source_dir)
    destination = Path(journal.destination_dir)
    staged = Path(journal.staged_dir)
    parked = Path(journal.parked_dir)
    events: list[RelocationRecoveryEvent] = []

    # The renames below move whole directories containing the live
    # database; a pooled read connection inside any of them fails the
    # rename with WinError 32 on Windows (the same class restore guards
    # against). Releasing is safe — reads re-open lazily — and the re-open
    # fences keep those lazy re-opens from winning the race back open
    # before the renames land.
    from .cad_repository import (
        fenced_read_reopens_under,
        release_read_handles_under,
    )

    with (
        fenced_read_reopens_under(source),
        fenced_read_reopens_under(destination),
        fenced_read_reopens_under(staged),
        fenced_read_reopens_under(parked),
    ):
        for swap_root in (source, destination, staged, parked):
            release_read_handles_under(swap_root)

        if journal.phase == 'COMPLETED':
            journal_path.unlink(missing_ok=True)
            return events

        destination_database = destination / DATABASE_NAME
        if (
            not destination_database.is_file()
            and journal.phase != 'PREPARED'
            and _staged_database(journal).is_file()
        ):
            # The promotion rename never fsync'd into the journal: promote the
            # verified staged copy now. A PREPARED journal means the copy phase
            # died before verification ever ran — that staged root is
            # unverified and disposable (it falls to the abort path below),
            # never promotable even when it happens to contain a database.
            _verify_staged_root(staged)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _promote_directory(staged, destination)
            events.append(
                RelocationRecoveryEvent(
                    'destination_promoted',
                    f'verified staged copy promoted to {destination}',
                )
            )

        if not destination_database.is_file():
            if journal.phase in (
                'DESTINATION_PROMOTED',
                'SOURCE_PARKED',
                'BOOTSTRAP_SWITCHED',
            ):
                # The journal claims a promoted destination that does not
                # exist — roll the source back to its original name so the
                # pre-relocation generation is what the next launch sees.
                # The rollback rename needs the same treatment as the
                # forward promote: ``os.replace`` cannot move a directory
                # over even an EMPTY post-crash residue dir on Windows,
                # while foreign content must still block the restore.
                # And the journaled park slot is only the *planned* name
                # until SOURCE_PARKED lands — resolve the generation by
                # its journaled database digest, not by slot name alone.
                parked_source = _locate_parked_source(journal, parked)
                source_free = not source.exists() or (
                    source.is_dir() and not any(source.iterdir())
                )
                if parked_source is not None and source_free:
                    _promote_directory(parked_source, source)
                    events.append(
                        RelocationRecoveryEvent(
                            'source_restored',
                            f'parked source restored to {source}',
                        )
                    )
                    journal_path.unlink(missing_ok=True)
                    raise DataRelocationError(
                        f'relocation journal records phase {journal.phase} '
                        f'but {destination} has no database; rolled the '
                        f'source back to {source} and cleared the '
                        'transaction'
                    )
                source_database = source / DATABASE_NAME
                source_is_generation = (
                    journal.source_database_sha256 is not None
                    and source_database.is_file()
                    and sha256_file(source_database)
                    == journal.source_database_sha256
                )
                if source_is_generation:
                    detail = (
                        f'{source} already holds the pre-relocation '
                        'generation'
                    )
                elif parked_source is not None:
                    detail = (
                        f'the parked generation at {parked_source} was '
                        f'left in place because {source} already holds '
                        'content'
                    )
                elif source_free:
                    detail = (
                        'no restorable parked generation was found at '
                        f'{parked} or its numbered siblings'
                    )
                else:
                    detail = (
                        f'{source} holds content and no restorable parked '
                        f'generation was found at {parked} or its '
                        'numbered siblings'
                    )
                journal_path.unlink(missing_ok=True)
                raise DataRelocationError(
                    f'relocation journal records phase {journal.phase} but '
                    f'{destination} has no database; {detail}; cleared '
                    'the transaction'
                )
            # Copy never reached promotion: the source was never touched and
            # the partial staged copy is disposable.
            if staged.is_dir():
                shutil.rmtree(staged, ignore_errors=True)
            journal_path.unlink(missing_ok=True)
            events.append(
                RelocationRecoveryEvent(
                    'aborted_before_promotion',
                    f'pre-cutover residue cleared (phase {journal.phase})',
                )
            )
            return events

        # Destination holds a verified generation — finish the cutover.
        _verify_staged_root(destination)
        if source.is_dir():
            parked = _park_directory(source, parked)
            events.append(
                RelocationRecoveryEvent(
                    'source_parked', f'source parked at {parked}'
                )
            )
        save_bootstrap_config(
            HTDTBootstrapConfig(
                data_dir=str(destination), written_at_utc=_utc_now()
            ),
            bootstrap_path,
        )
        _write_relocation_journal(
            journal.model_copy(
                update={'phase': 'COMPLETED', 'updated_at_utc': _utc_now()}
            ),
            journal_path,
        )
        journal_path.unlink(missing_ok=True)
        events.append(
            RelocationRecoveryEvent(
                'completed',
                f'cutover completed; bootstrap points at {destination}',
            )
        )
    return events


def recover_interrupted_relocation(
    *, bootstrap_path: Path | None = None
) -> list[RelocationRecoveryEvent]:
    """Settle a crashed relocation transaction, if a journal exists.

    Safe to call at every startup: no journal means a no-op. A live
    relocation holds the external lock, so this returns [] rather than
    racing the in-flight cutover.
    """

    journal = _read_relocation_journal(
        _relocation_journal_path(bootstrap_path)
    )
    if journal is None:
        return []
    lock = _RelocationLock(bootstrap_path)
    if not lock.acquire():
        return []
    try:
        journal = _read_relocation_journal(
            _relocation_journal_path(bootstrap_path)
        )
        if journal is None:
            return []
        return _recover_journal_locked(
            journal, bootstrap_path=bootstrap_path
        )
    finally:
        lock.release()


def execute_data_relocation(
    source_dir: Path,
    destination_dir: Path,
    *,
    bootstrap_path: Path | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_commit_point: Callable[[], None] | None = None,
) -> tuple[ManagedDataRelocationPlan, Path]:
    """Copy → verify → cutover the managed root under a durable journal.

    Returns (plan, parked_source) — the old root is renamed to
    ``<source>.relocated-<stamp>`` after the new location verifies, never
    deleted. Raises ``DataRelocationBlockedError`` when preflight found
    blockers or another relocation is live, or ``DataRelocationError``
    mid-flight — with the durable journal preserved so the next startup
    settles the cutover deterministically.
    """

    source_dir = Path(source_dir)
    destination_dir = Path(destination_dir)

    # The destination must be reachable before any data moves.
    plan = plan_data_relocation(source_dir, destination_dir)
    if not plan.executable:
        raise DataRelocationBlockedError(
            'relocation is blocked: '
            + '; '.join(b.detail for b in plan.blockers)
        )

    # The external transaction lock — held across the whole cutover,
    # outside both renamed directories — serializes relocations and lets
    # startup distinguish a live cutover from a crashed one.
    with _RelocationLock(bootstrap_path):
        # Settle a crashed prior transaction before starting a new one.
        journal_path = _relocation_journal_path(bootstrap_path)
        leftover = _read_relocation_journal(journal_path)
        if leftover is not None:
            _recover_journal_locked(leftover, bootstrap_path=bootstrap_path)

        # Refuse to move a live data directory: acquire the instance lock
        # so a running HTDT is a hard blocker, not a torn copy.
        guard = SingleInstanceGuard(source_dir)
        if not guard.acquire():
            raise DataRelocationBlockedError(
                'relocation is blocked: the data directory is in use by a '
                'running HTDT process'
            )
        staged = destination_dir.with_name(
            f'{destination_dir.name}.relocation-{uuid4().hex[:8]}'
        )
        parked = source_dir.with_name(
            f'{source_dir.name}{RELOCATED_SUFFIX}'
            f'{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}'
        )
        journal: RelocationJournal | None = None
        try:
            # Resolve any interrupted restore before copying so the moved
            # tree is the resolved generation, not a mid-swap artifact.
            # Deferred import keeps cad_repository out of this module's
            # import-time dependency graph.
            from .cad_repository import (
                fenced_read_reopens_under,
                release_read_handles_under,
            )

            recover_interrupted_restore(source_dir)

            database = source_dir / DATABASE_NAME
            journal = RelocationJournal(
                transaction_id=uuid4().hex,
                phase='PREPARED',
                source_dir=str(source_dir),
                destination_dir=str(destination_dir),
                staged_dir=str(staged),
                parked_dir=str(parked),
                source_database_sha256=sha256_file(database),
                created_at_utc=_utc_now(),
                updated_at_utc=_utc_now(),
            )
            _write_relocation_journal(journal, journal_path)

            # ---- copy phase ---------------------------------------------
            staged.mkdir(parents=True, exist_ok=True)
            staged_database = staged / DATABASE_NAME
            with closing(connect_sqlite(database)) as source, closing(
                connect_sqlite(staged_database)
            ) as destination:
                source.backup(destination)
                destination.commit()
            live_assets = source_dir / MANAGED_ASSETS_DIRNAME
            staged_assets = staged / MANAGED_ASSETS_DIRNAME
            if live_assets.is_dir():
                staged_assets.mkdir(exist_ok=True)
                for candidate in live_assets.iterdir():
                    if candidate.is_file():
                        _raise_if_relocation_cancelled(is_cancelled)
                        shutil.copy2(candidate, staged_assets / candidate.name)
            # Registry-carried root components: preferences, library
            # metadata, operational history/state, upgrade journal +
            # recovery generations and diagnostics move with the managed
            # root so nothing root-tied silently resets at the new
            # location (#769). Transient components (runtime.json,
            # instance locks) are never copied.
            for component in relocation_carried_components():
                _raise_if_relocation_cancelled(is_cancelled)
                # Canonical path plus numbered archive generations
                # (``<path>.<n>`` — e.g. htdt.migrated.sqlite3.2): sibling
                # generations are HTDT-owned and carry the base policy.
                for carried_name in (
                    [component.path]
                    + _numbered_siblings(source_dir, component.path)
                ):
                    source_path = source_dir / carried_name
                    staged_path = staged / carried_name
                    if source_path.is_dir():
                        shutil.copytree(source_path, staged_path)
                    elif source_path.is_file():
                        staged_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_path, staged_path)
            # Root-local canonical state travels with the root; operational
            # residue (locks, runtime, logs, temp files) is left behind.
            for name in _ROOT_FILES_TO_MOVE:
                _raise_if_relocation_cancelled(is_cancelled)
                candidate = source_dir / name
                if candidate.is_file() and candidate.name != DATABASE_NAME:
                    shutil.copy2(candidate, staged / candidate.name)
            # Warn on ANY unclassified leftover — directories included:
            # the previous file-only check silently skipped whole trees
            # (launch-intents/, capture-receiver/, user stray dirs).
            for candidate in source_dir.iterdir():
                if (
                    not _is_operational_residue(candidate.name)
                    and candidate.name not in _ROOT_FILES_TO_MOVE
                    and candidate.name not in _ROOT_DIRECTORIES_TO_MOVE
                    and component_for_path(candidate.name) is None
                ):
                    _LOGGER.warning(
                        'unclassified root file %s left behind during '
                        'relocation; classify it explicitly',
                        candidate.name,
                    )

            # ---- verify phase -------------------------------------------
            _verify_staged_root(staged)
            # Same canonical authority contract as backup/upgrade (#757):
            # the destination generation is semantically replayed before
            # the pointer switch, not merely structurally parsed.
            from .native_authority_audit import assert_native_authority_graph

            assert_native_authority_graph(staged_database)
            # Last cheap abort point: still PREPARED, so the exception path
            # removes the staged copy and the journal (#REV19/D2).
            _raise_if_relocation_cancelled(is_cancelled)
            journal = _journal_with_phase(
                journal,
                'STAGED_VERIFIED',
                journal_path,
                staged_database_sha256=sha256_file(staged_database),
            )
            # Commit point: the verified stage is durable in the journal —
            # the cutover runs to completion even if a cancel was meanwhile
            # requested (CANCEL_UNTIL_COMMIT).
            if on_commit_point is not None:
                on_commit_point()

            # ---- cutover --------------------------------------------------
            # Windows cannot rename a directory while a handle inside it is
            # open, so the per-root instance lock is released before either
            # rename — the external relocation lock and journal still keep
            # the cutover exclusive and recoverable. The order keeps at
            # least one complete root live at every instant: promote the
            # verified staged copy first, then park the source, then move
            # the bootstrap pointer last so a crash never exposes a root
            # the locator does not describe. Pooled read connections are
            # in-process handles the instance lock cannot see — release
            # them as well or the source rename fails with WinError 32.
            # The re-open fences keep a lazy re-open from winning the race
            # back open before the renames land.
            with (
                fenced_read_reopens_under(source_dir),
                fenced_read_reopens_under(destination_dir),
                fenced_read_reopens_under(staged),
                fenced_read_reopens_under(parked),
            ):
                for swap_root in (
                    source_dir,
                    destination_dir,
                    staged,
                    parked,
                ):
                    release_read_handles_under(swap_root)
                guard.release()
                destination_dir.parent.mkdir(parents=True, exist_ok=True)
                _promote_directory(staged, destination_dir)
                journal = _journal_with_phase(
                    journal, 'DESTINATION_PROMOTED', journal_path
                )
                try:
                    parked_actual = _park_directory(source_dir, parked)
                except OSError as exc:
                    raise DataRelocationError(
                        f'the verified copy is live at {destination_dir}, but '
                        f'the old source directory {source_dir} could not be '
                        f'parked: {exc}. The relocation journal will recover '
                        'the cutover on the next startup.'
                    ) from exc
                journal = _journal_with_phase(
                    journal,
                    'SOURCE_PARKED',
                    journal_path,
                    parked_dir=str(parked_actual),
                )

                # Point the bootstrap config at the new root.
                try:
                    save_bootstrap_config(
                        HTDTBootstrapConfig(
                            data_dir=str(destination_dir),
                            written_at_utc=_utc_now(),
                        ),
                        bootstrap_path,
                    )
                except OSError as exc:
                    raise DataRelocationError(
                        'the relocated generation is live at '
                        f'{destination_dir} and the source is parked at '
                        f'{parked}, but the bootstrap pointer could not be '
                        f'written: {exc}. The relocation journal will finish '
                        'the switch on the next startup — no default data '
                        'universe will be created.'
                    ) from exc
            journal = _journal_with_phase(
                journal, 'BOOTSTRAP_SWITCHED', journal_path
            )
            _write_relocation_journal(
                journal.model_copy(
                    update={
                        'phase': 'COMPLETED',
                        'updated_at_utc': _utc_now(),
                    }
                ),
                journal_path,
            )
            journal_path.unlink(missing_ok=True)
            _LOGGER.info(
                'managed data relocated: %s -> %s (source parked at %s)',
                source_dir,
                destination_dir,
                parked,
            )
            return plan, parked
        except Exception:  # error-boundary: cutover boundary — any copy/verify failure leaves the source intact and removes only the unpromoted staged copy; durable journal phases are owned by startup recovery (noqa: BLE001)
            # Copy/verify failure leaves the source exactly as it was; the
            # staged copy is removed only if the cutover never promoted it.
            # Once a journal phase is durable, startup recovery owns the
            # residue instead of this process guessing under the crash
            # boundary it just crossed.
            if (
                journal is not None
                and journal.phase == 'PREPARED'
                and staged.is_dir()
                and not destination_dir.exists()
            ):
                shutil.rmtree(staged, ignore_errors=True)
                journal_path.unlink(missing_ok=True)
            elif journal is None and staged.is_dir():
                shutil.rmtree(staged, ignore_errors=True)
            raise
        finally:
            guard.release()


__all__ = [
    'DataRelocationBlockedError',
    'DataRelocationError',
    'HTDTBootstrapConfig',
    'ManagedDataRelocationPlan',
    'ManagedDataUnavailableError',
    'RELOCATION_JOURNAL_FILENAME',
    'RELOCATION_LOCK_FILENAME',
    'RelocationBlocker',
    'RelocationJournal',
    'RelocationRecoveryEvent',
    'assert_managed_root_available',
    'bootstrap_config_path',
    'execute_data_relocation',
    'load_bootstrap_config',
    'plan_data_relocation',
    'recover_interrupted_relocation',
    'resolve_data_dir',
    'save_bootstrap_config',
]
