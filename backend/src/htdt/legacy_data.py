from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import BaseModel

from uuid import uuid4

from .export_io import write_text_atomic
from .project_library import ProjectLibraryEntry
from .project_library_repository import ProjectLibraryRepository
from .clock import utc_now_iso as _utc_now


LEGACY_SCHEMA = 'htdt.legacy-data'
LEGACY_SCHEMA_VERSION = '1.0.0'

LEGACY_DB_NAME = 'htdt.sqlite3'
MIGRATED_DB_NAME = 'htdt.migrated.sqlite3'
LEGACY_ASSETS_DIRNAME = 'assets'
MIGRATED_ASSETS_DIRNAME = 'htdt.migrated.assets'
# Crash journal for the archive rename step (#759): its presence means a
# migration was interrupted mid-rename and the retired/live boundary is
# undecidable — inspection reports 'interrupted' instead of guessing.
MIGRATION_JOURNAL_NAME = 'htdt-legacy-migration.journal'

# User-content tables of the retired browser authority. ``metadata`` only holds
# the legacy schema marker, so it does not count as user data.
LEGACY_DATA_TABLES = (
    'projects',
    'contexts',
    'sessions',
    'constraint_sets',
    'search_specs',
    'assets',
    'measurements',
    'datasets',
    'comparisons',
    'asset_links',
)

# cad-scenes.sqlite3 table recording which legacy projects were imported.
# Its DDL lives in cad_schema_ddl (the versioned migration authority, #302).
LEGACY_IMPORTS_TABLE = 'htdt_legacy_imports'

IMPORT_NAME_SUFFIX = '（レガシー移行）'


class LegacyDataError(ValueError):
    """Raised when legacy-data inspection or migration cannot run safely."""


class LegacyDataReport(BaseModel, frozen=True):
    """Read-only inventory of the retired browser data authority (#598)."""

    schema: Literal['htdt.legacy-data.report'] = 'htdt.legacy-data.report'
    schema_version: Literal['1.0.0'] = '1.0.0'
    state: Literal[
        'absent', 'empty', 'populated', 'migrated', 'unreadable',
        'interrupted',
    ]
    db_path: str
    table_counts: dict[str, int]
    legacy_assets_bytes: int
    detail: str | None = None


class LegacyMigrationResult(BaseModel, frozen=True):
    """Outcome of the bounded one-way legacy -> native migration."""

    schema: Literal['htdt.legacy-data.migration'] = 'htdt.legacy-data.migration'
    schema_version: Literal['1.0.0'] = '1.0.0'
    state: Literal['nothing_to_migrate', 'migrated', 'already_migrated']
    imported_projects: int = 0
    skipped_existing: int = 0
    archived_db_path: str | None = None
    archived_assets_path: str | None = None
    detail: str | None = None


def _directory_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for entry in path.rglob('*'):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def inspect_legacy_store(data_dir: Path) -> LegacyDataReport:
    """Classify the retired browser store without mutating it.

    Read-only: the legacy file is opened with SQLite's ``mode=ro`` URI so a
    missing or partially-migrated store is never recreated as a side effect.
    """

    data_dir = Path(data_dir)
    db_path = data_dir / LEGACY_DB_NAME
    assets_dir = data_dir / LEGACY_ASSETS_DIRNAME
    assets_bytes = _directory_size(assets_dir)
    # Journal first: an interrupted rename leaves the live/archived
    # boundary undecidable, so no state below may be claimed.
    if (data_dir / MIGRATION_JOURNAL_NAME).is_file():
        return LegacyDataReport(
            state='interrupted',
            db_path=str(db_path),
            table_counts={},
            legacy_assets_bytes=assets_bytes,
            detail=(
                'a previous migration was interrupted mid-archive; resolve '
                f'the pending renames recorded in {MIGRATION_JOURNAL_NAME} '
                'or remove it once the store is consistent'
            ),
        )
    # Live file first: a recreated htdt.sqlite3 must never be masked by the
    # migrated sentinel — real data in it still needs migration.
    if db_path.is_file():
        pass
    elif (data_dir / MIGRATED_DB_NAME).is_file():
        return LegacyDataReport(
            state='migrated',
            db_path=str(db_path),
            table_counts={},
            legacy_assets_bytes=assets_bytes,
            detail='legacy store archived by migration',
        )
    else:
        return LegacyDataReport(
            state='absent',
            db_path=str(db_path),
            table_counts={},
            legacy_assets_bytes=assets_bytes,
        )
    try:
        uri = f'file:{db_path.as_posix()}?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            tables = {
                str(row['name'])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            counts: dict[str, int] = {}
            for table in LEGACY_DATA_TABLES:
                if table in tables:
                    counts[table] = int(
                        connection.execute(
                            f'SELECT COUNT(*) AS n FROM "{table}"'
                        ).fetchone()['n']
                    )
    except sqlite3.DatabaseError as exc:
        return LegacyDataReport(
            state='unreadable',
            db_path=str(db_path),
            table_counts={},
            legacy_assets_bytes=assets_bytes,
            detail=str(exc),
        )
    state = 'populated' if any(counts.values()) else 'empty'
    return LegacyDataReport(
        state=state,
        db_path=str(db_path),
        table_counts=counts,
        legacy_assets_bytes=assets_bytes,
    )


def _read_legacy_projects(db_path: Path) -> list[dict[str, str]]:
    uri = f'file:{db_path.as_posix()}?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            {
                'id': str(row['id']),
                'name': str(row['name']),
                'created_at': str(row['created_at']),
            }
            for row in connection.execute(
                'SELECT id, name, created_at FROM projects ORDER BY created_at, id'
            )
        ]


def _next_available(path: Path) -> Path:
    """First free ``path`` variant — ``name``, then ``name.1``, ``name.2``…"""

    candidate = path
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = path.with_name(f'{path.name}.{suffix}')
    return candidate


def _archive_legacy_store(data_dir: Path) -> tuple[Path, Path | None]:
    """Rename the legacy store to its immutable archived form.

    A crash journal records the pending renames before they start and is
    removed only after both complete — an interrupted run is detected by
    ``inspect_legacy_store`` instead of silently resuming. When a previous
    archive already occupies the canonical name (e.g. a recreated empty
    store after migration), a numbered sibling keeps both generations.
    """

    db_path = data_dir / LEGACY_DB_NAME
    assets_dir = data_dir / LEGACY_ASSETS_DIRNAME
    archived_db = _next_available(data_dir / MIGRATED_DB_NAME)
    archived_assets = _next_available(data_dir / MIGRATED_ASSETS_DIRNAME)

    journal = data_dir / MIGRATION_JOURNAL_NAME
    write_text_atomic(
        journal,
        json.dumps(
            {
                'renames': [
                    {'from': str(db_path), 'to': str(archived_db)}
                ]
                + (
                    [{'from': str(assets_dir), 'to': str(archived_assets)}]
                    if assets_dir.is_dir()
                    else []
                ),
                'recorded_at_utc': _utc_now(),
            },
            indent=2,
            allow_nan=False,
        ),
    )
    archived_assets_path: Path | None = None
    try:
        if assets_dir.is_dir():
            os.replace(assets_dir, archived_assets)
            archived_assets_path = archived_assets
        os.replace(db_path, archived_db)
    except BaseException:  # error-boundary: journal-survival re-raise — any rename failure propagates unchanged; the journal is written outside the try so it survives (noqa: BLE001)
        # The journal MUST survive a failed rename: half-applied renames
        # leave the live/archived boundary undecidable, and inspection
        # reports 'interrupted' only while the journal exists.
        raise
    journal.unlink()
    return archived_db, archived_assets_path


def migrate_legacy_data(
    data_dir: Path,
    *,
    project_library: ProjectLibraryRepository,
) -> LegacyMigrationResult:
    """One-way, bounded migration of legacy projects into native projects.

    Projects map one-to-one onto native library entries; contexts,
    measurements and attachments have no faithful semantic upgrade path, so
    they remain as legacy metadata inside the archived database (renamed so
    the retired store can never silently resume as a second authority).
    Idempotent: already-imported legacy project ids are skipped via
    ``htdt_legacy_imports``.
    """

    data_dir = Path(data_dir)
    report = inspect_legacy_store(data_dir)
    if report.state == 'interrupted':
        raise LegacyDataError(
            'legacy migration was interrupted mid-archive and cannot be '
            f'resumed safely: {report.detail}'
        )
    if report.state == 'unreadable':
        raise LegacyDataError(
            f'legacy database is unreadable: {report.detail}'
        )
    if report.state == 'migrated':
        return LegacyMigrationResult(
            state='already_migrated',
            archived_db_path=str(data_dir / MIGRATED_DB_NAME),
        )
    if report.state == 'absent':
        return LegacyMigrationResult(state='nothing_to_migrate')

    db_path = Path(report.db_path)
    if report.state == 'empty':
        archived_db, archived_assets = _archive_legacy_store(data_dir)
        return LegacyMigrationResult(
            state='migrated',
            archived_db_path=str(archived_db),
            archived_assets_path=str(archived_assets) if archived_assets else None,
            detail='empty legacy store archived',
        )

    projects = _read_legacy_projects(db_path)
    imported = 0
    skipped = 0
    with closing(project_library._connect()) as connection:
        imported_ids = {
            str(row['legacy_project_id'])
            for row in connection.execute(
                f'SELECT legacy_project_id FROM {LEGACY_IMPORTS_TABLE}'
            )
        }
    # One transaction for the whole batch: a project row is never
    # observable without its htdt_legacy_imports marker, and a partial
    # import never publishes (#759).
    with closing(project_library._connect()) as connection, connection:
        for project in projects:
            if project['id'] in imported_ids:
                skipped += 1
                continue
            entry = ProjectLibraryEntry(
                project_id=str(uuid4()),
                document_id=str(uuid4()),
                display_name=f"{project['name']}{IMPORT_NAME_SUFFIX}",
                description=(
                    f"Imported from legacy project {project['id']} "
                    f"(created {project['created_at']}). Contexts, "
                    'measurements and attachments remain in the archived '
                    'legacy database.'
                ),
                created_at_utc=_utc_now(),
            )
            project_library._insert_entry(connection, entry)
            connection.execute(
                f'INSERT INTO {LEGACY_IMPORTS_TABLE}('
                'legacy_project_id, project_id, document_id, '
                'migrated_at_utc, detail'
                ') VALUES (?, ?, ?, ?, ?)',
                (
                    project['id'],
                    entry.project_id,
                    entry.document_id,
                    _utc_now(),
                    f'legacy project "{project["name"]}" imported as an '
                    'empty native project; legacy payloads archived',
                ),
            )
            imported += 1
    archived_db, archived_assets = _archive_legacy_store(data_dir)
    return LegacyMigrationResult(
        state='migrated',
        imported_projects=imported,
        skipped_existing=skipped,
        archived_db_path=str(archived_db),
        archived_assets_path=str(archived_assets) if archived_assets else None,
        detail=(
            'legacy projects imported as native projects; legacy payloads '
            'remain in the archived database'
        ),
    )
