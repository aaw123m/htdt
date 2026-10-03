"""Storage & evidence maintenance — inventory and safe GC (#501).

The write-side contract deliberately keeps orphans over deleting
referenced data: a failed transaction may leave a content-addressed asset
installed with no authority pointing at it. This service owns the
read-side counterpart: a live storage inventory that distinguishes
referenced evidence from unreferenced candidates, plus a guarded
garbage-collect that only ever removes an asset after re-proving it is
unreachable inside a stable transaction snapshot.

Rules that hold regardless of shape:

* a digest is **referenced** when any authority row anywhere in the
  database carries it — a dedicated digest column, an embedded occurrence
  inside any text payload (a ``*_json`` document or any other string
  field), not only a ``source_sha256``-style column — and never just
  because a ``cad_measurement_assets`` registry row exists;
* a ``cad_measurement_assets`` row whose file is missing is an
  **integrity failure**, never a cleanup candidate;
* a file with no registry row is **unmanaged**, never a cleanup
  candidate — unless ``htdt_storage_gc_pending`` carries its digest,
  which marks an interrupted delete and keeps it GC-eligible until the
  file is unlinked or the pending row is resolved;
* cleanup runs inside two ``BEGIN IMMEDIATE`` transactions so no writer
  can interleave at either decision point: the first recomputes the
  referenced set, sha256-verifies each candidate's file content, and
  commits the registry-row deletes together with pending-ledger rows
  (a crash or failed unlink leaves a retryable pending row — never a
  permanently unmanaged orphan, #501); the second re-proves
  unreachability under a fresh write exclusion and unlinks inside it,
  so a writer whose reference commits between the transactions gets
  its registry row restored instead of dangling over a deleted file;
* immutable evidence rows are never mutated to reclaim space;
* physical unique bytes are counted once per digest file — logical
  references are counted per referencing authority.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import time
from typing import TypeVar

from .cad_schema import require_native_tables, connect_sqlite
from .clock import utc_now_iso as _utc_now


logger = logging.getLogger(__name__)

MANAGED_ASSETS_DIRNAME = 'measurement-assets'

_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
# Substring matcher: a digest may be embedded anywhere in a text cell
# (JSON payloads in non-``*_json`` columns, digest lists, URIs), not only
# occupy a whole cell. Extracting every 64-hex substring keeps the
# reachability set a strict superset of any single typed decoder.
_SHA256_SUBSTRING_RE = re.compile(r'[0-9a-f]{64}')
_TEMP_NAME_RE = re.compile(r'^\.asset-.*\.tmp$|\.tmp$')

#: Storage bookkeeping tables are never referents: ``cad_measurement_assets``
#: is the registry, ``htdt_storage_gc_pending`` is the interrupted-delete
#: ledger — a digest appearing only in either must not count as referenced.
_STORAGE_LEDGER_TABLES = frozenset(
    {'cad_measurement_assets', 'htdt_storage_gc_pending'}
)
GC_PENDING_TABLE = 'htdt_storage_gc_pending'


class StorageMaintenanceError(ValueError):
    """Storage maintenance could not honor its safety contract."""


class StorageMaintenanceCancelledError(StorageMaintenanceError):
    """A scan/GC job honored a cooperative cancel request (#REV19/D2).

    Inside the GC transaction the ``except Exception`` rollback path
    leaves every registry row exactly as found; post-commit a partially
    completed unlink pass just leaves pending rows the next GC resumes.
    """


def _raise_if_storage_cancelled(
    is_cancelled: Callable[[], bool] | None,
) -> None:
    if is_cancelled is not None and is_cancelled():
        raise StorageMaintenanceCancelledError(
            'storage maintenance cancelled by the caller'
        )


@dataclass(frozen=True, slots=True)
class ManagedFileReport:
    """One file under the managed assets directory."""

    path: Path
    digest: str | None
    size_bytes: int
    classification: str
    # 'referenced' | 'unreferenced' | 'temporary' | 'unmanaged'


@dataclass(frozen=True, slots=True)
class MissingAssetReport:
    """A registry row referencing a file that does not exist — integrity
    failure, surfaced separately from cleanup candidates (#501)."""

    digest: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class StorageCategoryReport:
    category: str
    file_count: int
    logical_referenced_bytes: int
    physical_unique_bytes: int
    unreferenced_bytes: int


@dataclass(frozen=True, slots=True)
class StorageReport:
    """Live inventory of managed storage — no backup required."""

    data_dir: Path
    database_bytes: int
    diagnostics_bytes: int
    categories: tuple[StorageCategoryReport, ...]
    referenced_files: tuple[ManagedFileReport, ...]
    orphan_candidates: tuple[ManagedFileReport, ...]
    temporary_files: tuple[ManagedFileReport, ...]
    unmanaged_files: tuple[ManagedFileReport, ...]
    missing_referenced: tuple[MissingAssetReport, ...]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(item.size_bytes for item in self.orphan_candidates)


@dataclass(frozen=True, slots=True)
class StorageGcResult:
    deleted_files: int
    deleted_registry_rows: int
    freed_bytes: int
    skipped_digests: tuple[str, ...]


def _connect(db_path: Path) -> sqlite3.Connection:
    return connect_sqlite(db_path)


def _payload_digests(value: object, out: set[str]) -> None:
    if isinstance(value, str):
        if _SHA256_RE.match(value):
            out.add(value)
    elif isinstance(value, dict):
        for child in value.values():
            _payload_digests(child, out)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _payload_digests(child, out)


def referenced_asset_digests(connection: sqlite3.Connection) -> set[str]:
    """Every SHA-256 that any authority row in the database carries.

    ``cad_measurement_assets`` itself is excluded: the registry row is
    storage bookkeeping, not a referent — otherwise nothing could ever be
    classified unreferenced.
    """

    referenced: set[str] = set()
    tables = [
        row['name']
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
        if row['name'] not in _STORAGE_LEDGER_TABLES
    ]
    for table in tables:
        try:
            rows = connection.execute(f'SELECT * FROM {table}').fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            for key in row.keys():
                value = row[key]
                if not isinstance(value, str):
                    continue
                if _SHA256_RE.match(value):
                    referenced.add(value)
                    continue
                if key.endswith('_json'):
                    try:
                        _payload_digests(json.loads(value), referenced)
                        continue
                    except (ValueError, TypeError):
                        pass
                referenced.update(_SHA256_SUBSTRING_RE.findall(value))
    return referenced


def _asset_registry(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    try:
        rows = connection.execute(
            'SELECT * FROM cad_measurement_assets'
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row['sha256']): row for row in rows}


def _gc_pending(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Interrupted deletes queued by an earlier GC pass.

    A missing table means the schema predates the ledger — report no
    pending work rather than fail a read-only scan.
    """

    try:
        rows = connection.execute(
            f'SELECT * FROM {GC_PENDING_TABLE}'
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row['sha256']): row for row in rows}


def scan_storage(
    data_dir: Path,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> StorageReport:
    """Classify every managed file without requiring a backup first."""

    data_dir = Path(data_dir)
    db_path = data_dir / 'cad-scenes.sqlite3'
    assets_root = data_dir / MANAGED_ASSETS_DIRNAME

    database_bytes = 0
    for suffix in ('', '-wal', '-shm'):
        sidecar = Path(f'{db_path}{suffix}')
        if sidecar.is_file():
            database_bytes += sidecar.stat().st_size

    diagnostics_bytes = 0
    diagnostics_dir = data_dir / 'diagnostics'
    if diagnostics_dir.is_dir():
        for entry in diagnostics_dir.rglob('*'):
            _raise_if_storage_cancelled(is_cancelled)
            if entry.is_file():
                diagnostics_bytes += entry.stat().st_size

    missing: list[MissingAssetReport] = []

    referenced_files: list[ManagedFileReport] = []
    orphan_files: list[ManagedFileReport] = []
    temp_files: list[ManagedFileReport] = []
    unmanaged_files: list[ManagedFileReport] = []
    physical_bytes = 0
    logical_bytes = 0

    if db_path.is_file():
        with closing(_connect(db_path)) as connection:
            digests = referenced_asset_digests(connection)
            registry = _asset_registry(connection)
            pending = _gc_pending(connection)
    else:
        digests = set()
        registry = {}
        pending = {}

    for digest, row in registry.items():
        _raise_if_storage_cancelled(is_cancelled)
        relative = str(row['relative_path'])
        size = int(row['size_bytes'])
        if digest in digests:
            # Logical reference bytes are counted once per referent —
            # shared assets are not double-counted physically below.
            logical_bytes += size
        if not (data_dir / relative).is_file():
            missing.append(
                MissingAssetReport(digest=digest, relative_path=relative)
            )

    if assets_root.is_dir():
        for entry in sorted(assets_root.iterdir()):
            _raise_if_storage_cancelled(is_cancelled)
            if not entry.is_file():
                continue
            size = entry.stat().st_size
            name = entry.name
            physical_bytes += size
            if _TEMP_NAME_RE.match(name):
                temp_files.append(
                    ManagedFileReport(
                        path=entry,
                        digest=None,
                        size_bytes=size,
                        classification='temporary',
                    )
                )
            elif not _SHA256_RE.match(name):
                unmanaged_files.append(
                    ManagedFileReport(
                        path=entry,
                        digest=None,
                        size_bytes=size,
                        classification='unmanaged',
                    )
                )
            elif name in digests:
                referenced_files.append(
                    ManagedFileReport(
                        path=entry,
                        digest=name,
                        size_bytes=size,
                        classification='referenced',
                    )
                )
            elif name in registry or name in pending:
                # A pending-ledger row marks an interrupted delete: the
                # file stays a GC candidate so the next pass retries the
                # unlink instead of abandoning it as unmanaged.
                orphan_files.append(
                    ManagedFileReport(
                        path=entry,
                        digest=name,
                        size_bytes=size,
                        classification='unreferenced',
                    )
                )
            else:
                # Digest-named file with no registry row and no pending
                # delete: the install never registered it. Without an
                # authority row there is no safe delete boundary, so it
                # is unmanaged — reported, never GC-eligible.
                unmanaged_files.append(
                    ManagedFileReport(
                        path=entry,
                        digest=name,
                        size_bytes=size,
                        classification='unmanaged',
                    )
                )

    unreferenced_bytes = sum(item.size_bytes for item in orphan_files)
    categories = (
        StorageCategoryReport(
            category='native-database',
            file_count=1 if db_path.is_file() else 0,
            logical_referenced_bytes=database_bytes,
            physical_unique_bytes=database_bytes,
            unreferenced_bytes=0,
        ),
        StorageCategoryReport(
            category='managed-assets',
            file_count=(
                len(referenced_files)
                + len(orphan_files)
                + len(temp_files)
                + len(unmanaged_files)
            ),
            logical_referenced_bytes=logical_bytes,
            physical_unique_bytes=physical_bytes,
            unreferenced_bytes=unreferenced_bytes,
        ),
        StorageCategoryReport(
            category='diagnostics',
            file_count=0,
            logical_referenced_bytes=diagnostics_bytes,
            physical_unique_bytes=diagnostics_bytes,
            unreferenced_bytes=0,
        ),
    )
    return StorageReport(
        data_dir=data_dir,
        database_bytes=database_bytes,
        diagnostics_bytes=diagnostics_bytes,
        categories=categories,
        referenced_files=tuple(referenced_files),
        orphan_candidates=tuple(orphan_files),
        temporary_files=tuple(temp_files),
        unmanaged_files=tuple(unmanaged_files),
        missing_referenced=tuple(missing),
    )


def _file_matches_digest(
    path: Path, digest: str, size_bytes: int
) -> bool:
    """Content-verify a GC candidate against its recorded identity.

    Size is checked first (cheap) against the registry or pending-ledger
    row, then the file's sha256 must equal the digest its name claims —
    a swapped or corrupted file is an integrity anomaly, never a
    deletable orphan.
    """

    try:
        if path.stat().st_size != size_bytes:
            return False
    except OSError:
        return False
    hasher = hashlib.sha256()
    try:
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b''):
                hasher.update(chunk)
    except OSError:
        return False
    return hasher.hexdigest() == digest


def plan_storage_gc(
    data_dir: Path,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> StorageReport:
    """Dry-run: the current orphan candidates and reclaimable bytes."""
    return scan_storage(data_dir, is_cancelled=is_cancelled)


def _begin_immediate(
    connection: sqlite3.Connection,
    *,
    attempts: int = 4,
) -> None:
    """``BEGIN IMMEDIATE`` with bounded retry against busy starvation.

    sqlite's writer lock is not fair — a hot writer can starve the
    acquire past the connection busy timeout and fail the whole GC pass
    with ``database is locked``. A few short-spaced retries ride out a
    sustained commit burst without extending the hold GC itself takes.
    """
    for attempt in range(attempts):
        try:
            connection.execute('BEGIN IMMEDIATE')
            return
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


_PassResult = TypeVar('_PassResult')


def _run_write_pass(
    body: Callable[[], _PassResult], *, attempts: int = 3
) -> _PassResult:
    """Retry a whole ``BEGIN IMMEDIATE``→``COMMIT`` unit on lock errors.

    ``_begin_immediate`` only bounds BEGIN-time starvation; a ``COMMIT``
    that cannot upgrade RESERVED to EXCLUSIVE while readers hold SHARED
    also fails ``database is locked`` — rolling the pass back and
    surfacing a retryable error to the caller. Re-running the whole pass
    a bounded number of times rides out a sustained burst. Every attempt
    re-proves reachability, pending state and file identity from scratch,
    so a retried pass can never delete on stale information; the pending
    ledger keeps an interrupted pass resumable by the next run.
    """
    for attempt in range(attempts):
        try:
            return body()
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
    raise AssertionError('unreachable')


def run_storage_gc(
    data_dir: Path,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> StorageGcResult:
    """Delete only candidates still unreachable at delete time.

    Two ``BEGIN IMMEDIATE`` transactions: the first spans the
    reachability recomputation, per-candidate content verification, the
    registry-row deletes and the pending-ledger writes; the second
    re-proves unreachability under a fresh write exclusion and unlinks
    inside it, so a writer whose reference committed between the two
    transactions has its registry row restored — never a
    ``missing_referenced`` dangling over a deleted file. Worst partial
    state stays the pending row: retryable on the next pass. Each
    transaction is re-run a bounded number of times when a ``database is
    locked`` escapes the begin retry (a commit losing the EXCLUSIVE
    upgrade to saturated readers); every attempt re-proves everything
    from the live database, so retries stay fail-closed.
    """

    data_dir = Path(data_dir)
    db_path = data_dir / 'cad-scenes.sqlite3'
    if not db_path.is_file():
        raise StorageMaintenanceError(
            f'native database not found: {db_path}'
        )
    report = scan_storage(data_dir, is_cancelled=is_cancelled)
    candidates = {
        item.digest: item
        for item in report.orphan_candidates
        if item.digest is not None
    }

    # The pending ledger is what makes a post-commit crash retryable:
    # registry row and pending row commit together, so a file delete that
    # fails or a process that dies mid-pass resumes on the next run.
    def _delete_rows_pass() -> tuple[
        int, set[str], dict[str, sqlite3.Row], list[str]
    ]:
        deleted_rows = 0
        deleted_digests: set[str] = set()
        deleted_registry_rows: dict[str, sqlite3.Row] = {}
        skipped: list[str] = []
        with closing(_connect(db_path)) as connection:
            _begin_immediate(connection)
            try:
                require_native_tables(connection, GC_PENDING_TABLE)
                live_digests = referenced_asset_digests(connection)
                registry = _asset_registry(connection)
                pending_rows = _gc_pending(connection)
                now = _utc_now()
                for digest in sorted(candidates):
                    _raise_if_storage_cancelled(is_cancelled)
                    # Re-validation immediately before delete: if the
                    # digest became reachable since the scan, keep it.
                    if digest in live_digests:
                        skipped.append(digest)
                        continue
                    candidate = candidates[digest]
                    row = registry.get(digest)
                    pending_row = pending_rows.get(digest)
                    if row is None and pending_row is None:
                        continue
                    size_bytes = int(
                        row['size_bytes']
                        if row is not None
                        else pending_row['size_bytes']
                    )
                    # Content verification: a file whose bytes do not hash
                    # to its digest name is an integrity anomaly — keep
                    # the row and the file, skip the delete.
                    if not _file_matches_digest(
                        candidate.path, digest, size_bytes
                    ):
                        skipped.append(digest)
                        logger.warning(
                            'storage gc: orphan asset %s failed content '
                            'verification; leaving it registered',
                            digest,
                        )
                        continue
                    if pending_row is None:
                        connection.execute(
                            f'INSERT INTO {GC_PENDING_TABLE}('
                            'sha256, size_bytes, queued_at_utc'
                            ') VALUES (?, ?, ?)',
                            (digest, size_bytes, now),
                        )
                    if row is not None:
                        connection.execute(
                            'DELETE FROM cad_measurement_assets '
                            'WHERE sha256=?',
                            (digest,),
                        )
                        deleted_rows += 1
                        deleted_registry_rows[digest] = row
                    deleted_digests.add(digest)
                # Sweep pending rows that can no longer produce a delete:
                # the file vanished out-of-band (clear it) or the digest
                # became referenced again (keep the file, clear the row).
                for digest, pending_row in pending_rows.items():
                    if digest in deleted_digests:
                        continue
                    if digest in live_digests or not (
                        data_dir
                        / MANAGED_ASSETS_DIRNAME
                        / digest
                    ).is_file():
                        connection.execute(
                            f'DELETE FROM {GC_PENDING_TABLE} '
                            'WHERE sha256=?',
                            (digest,),
                        )
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()
        return deleted_rows, deleted_digests, deleted_registry_rows, skipped

    (
        deleted_rows,
        deleted_digests,
        deleted_registry_rows,
        skipped,
    ) = _run_write_pass(_delete_rows_pass)

    # Second exclusion window: re-prove unreachability, then unlink while
    # still holding the write lock that publishes the pending-ledger
    # outcome. A writer whose republish committed between the two
    # transactions lands in ``live_digests`` here — its registry row is
    # restored and its file kept; a writer still racing at commit time
    # lands strictly after this transaction and re-installs the asset
    # inside its own write transaction (the repository write path
    # installs bytes under BEGIN IMMEDIATE), so neither ordering can
    # leave a committed reference over a deleted file. Unlink OSError
    # keeps the pending row: the next pass retries it.
    def _unlink_pass() -> tuple[int, int, int, list[str]]:
        freed = 0
        deleted_files = 0
        restored_rows = 0
        skipped: list[str] = []
        with closing(_connect(db_path)) as connection:
            _begin_immediate(connection)
            try:
                live_digests = referenced_asset_digests(connection)
                for digest in sorted(deleted_digests):
                    _raise_if_storage_cancelled(is_cancelled)
                    target = candidates[digest].path
                    if digest in live_digests:
                        # Resurrected between the transactions: restore
                        # the registry row and keep the file.
                        row = deleted_registry_rows.get(digest)
                        if row is not None:
                            connection.execute(
                                'INSERT OR IGNORE INTO '
                                'cad_measurement_assets('
                                'sha256, filename, relative_path, size_bytes'
                                ') VALUES (?, ?, ?, ?)',
                                (
                                    row['sha256'],
                                    row['filename'],
                                    row['relative_path'],
                                    row['size_bytes'],
                                ),
                            )
                            restored_rows += 1
                        connection.execute(
                            f'DELETE FROM {GC_PENDING_TABLE} WHERE sha256=?',
                            (digest,),
                        )
                        skipped.append(digest)
                        continue
                    try:
                        size = target.stat().st_size
                        target.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        # The pending row stays — the next GC pass retries
                        # the unlink.
                        logger.warning(
                            'storage gc: could not delete orphan asset '
                            '%s: %s',
                            digest,
                            exc,
                        )
                        skipped.append(digest)
                        continue
                    else:
                        freed += size
                        deleted_files += 1
                    # File is gone (or was already): the pending row's
                    # work is done.
                    connection.execute(
                        f'DELETE FROM {GC_PENDING_TABLE} WHERE sha256=?',
                        (digest,),
                    )
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()
        return freed, deleted_files, restored_rows, skipped

    freed = 0
    deleted_files = 0
    if deleted_digests:
        freed, deleted_files, restored_rows, unlink_skipped = (
            _run_write_pass(_unlink_pass)
        )
        deleted_rows -= restored_rows
        skipped.extend(unlink_skipped)

    logger.info(
        'storage gc complete: files=%d rows=%d freed_bytes=%d skipped=%d',
        deleted_files,
        deleted_rows,
        freed,
        len(skipped),
    )
    return StorageGcResult(
        deleted_files=deleted_files,
        deleted_registry_rows=deleted_rows,
        freed_bytes=freed,
        skipped_digests=tuple(skipped),
    )


__all__ = [
    'MANAGED_ASSETS_DIRNAME',
    'ManagedFileReport',
    'MissingAssetReport',
    'StorageCategoryReport',
    'StorageGcResult',
    'StorageMaintenanceError',
    'StorageMaintenanceCancelledError',
    'StorageReport',
    'plan_storage_gc',
    'referenced_asset_digests',
    'run_storage_gc',
    'scan_storage',
]
