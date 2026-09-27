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
* cleanup runs inside one ``BEGIN IMMEDIATE`` transaction so no writer
  can interleave: the referenced set is recomputed inside the
  transaction, each candidate's file content is sha256-verified before
  its registry row is deleted, and a reachability change skips the
  candidate rather than racing a delete. The registry delete and the
  pending-ledger insert commit together, so a crash or failed unlink
  leaves a retryable pending row — never a permanently unmanaged
  orphan (#501);
* immutable evidence rows are never mutated to reclaim space;
* physical unique bytes are counted once per digest file — logical
  references are counted per referencing authority.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3

from .cad_schema import require_native_tables, connect_sqlite


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StorageMaintenanceError(ValueError):
    """Storage maintenance could not honor its safety contract."""


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


def scan_storage(data_dir: Path) -> StorageReport:
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


def plan_storage_gc(data_dir: Path) -> StorageReport:
    """Dry-run: the current orphan candidates and reclaimable bytes."""
    return scan_storage(data_dir)


def run_storage_gc(data_dir: Path) -> StorageGcResult:
    """Delete only candidates still unreachable at delete time.

    One ``BEGIN IMMEDIATE`` transaction spans the reachability
    recomputation, per-candidate content verification, and the registry
    row deletes — no writer can interleave and resurrect a reference
    mid-pass. File deletion happens after the row commit, so the worst
    partial state is a file with no registry row — reported as unmanaged
    on the next scan, never a missing referenced asset.
    """

    data_dir = Path(data_dir)
    db_path = data_dir / 'cad-scenes.sqlite3'
    if not db_path.is_file():
        raise StorageMaintenanceError(
            f'native database not found: {db_path}'
        )
    report = scan_storage(data_dir)
    candidates = {
        item.digest: item
        for item in report.orphan_candidates
        if item.digest is not None
    }

    # One BEGIN IMMEDIATE transaction spans reachability recomputation,
    # per-candidate content verification, every registry-row delete and
    # every pending-ledger write. No concurrent writer can interleave
    # between the read and the delete, which is the mutation-stable
    # boundary this authority owes. The pending ledger is what makes a
    # post-commit crash retryable: registry row and pending row commit
    # together, so the file delete can fail and the next pass resumes it.
    deleted_rows = 0
    deleted_digests: set[str] = set()
    skipped: list[str] = []
    with closing(_connect(db_path)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            require_native_tables(connection, GC_PENDING_TABLE)
            live_digests = referenced_asset_digests(connection)
            registry = _asset_registry(connection)
            pending_rows = _gc_pending(connection)
            now = _utc_now()
            for digest in sorted(candidates):
                # Re-validation immediately before delete: if the digest
                # became reachable since the scan, keep it.
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
                # Content verification: a file whose bytes do not hash to
                # its digest name is an integrity anomaly — keep the row
                # and the file, skip the delete.
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
                        'DELETE FROM cad_measurement_assets WHERE sha256=?',
                        (digest,),
                    )
                    deleted_rows += 1
                deleted_digests.add(digest)
            # Sweep pending rows that can no longer produce a delete: the
            # file vanished out-of-band (clear it) or the digest became
            # referenced again (keep the file, clear the row).
            for digest, pending_row in pending_rows.items():
                if digest in deleted_digests:
                    continue
                if digest in live_digests or not (
                    data_dir
                    / MANAGED_ASSETS_DIRNAME
                    / digest
                ).is_file():
                    connection.execute(
                        f'DELETE FROM {GC_PENDING_TABLE} WHERE sha256=?',
                        (digest,),
                    )
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    freed = 0
    deleted_files = 0
    for digest in sorted(deleted_digests):
        target = candidates[digest].path
        try:
            size = target.stat().st_size
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # The pending row stays — the next GC pass retries the unlink.
            logger.warning(
                'storage gc: could not delete orphan asset %s: %s',
                digest,
                exc,
            )
            skipped.append(digest)
            continue
        else:
            freed += size
            deleted_files += 1
        # File is gone (or was already): the pending row's work is done.
        with closing(_connect(db_path)) as connection, connection:
            connection.execute(
                f'DELETE FROM {GC_PENDING_TABLE} WHERE sha256=?',
                (digest,),
            )

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
    'StorageReport',
    'plan_storage_gc',
    'referenced_asset_digests',
    'run_storage_gc',
    'scan_storage',
]
