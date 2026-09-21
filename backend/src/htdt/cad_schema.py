from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .content_blobs import CONTENT_BLOB_DDL


NATIVE_SCHEMA_VERSION = 4

_METADATA_TABLE = 'native_schema_metadata'
_MIGRATION_TABLE = 'native_schema_migrations'
_LEGACY_NATIVE_TABLES = frozenset({
    'scene_revisions',
    'scene_recovery_snapshots',
    'editor_view_states',
})


class NativeSchemaError(RuntimeError):
    """Native CAD database schema is incompatible or cannot be adopted safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _stored_version(connection: sqlite3.Connection) -> int:
    tables = _table_names(connection)
    if _METADATA_TABLE not in tables:
        return 0
    try:
        row = connection.execute(
            f'SELECT schema_version FROM {_METADATA_TABLE} WHERE singleton=1'
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise NativeSchemaError(f'native schema metadata is unreadable: {exc}') from exc
    if row is None:
        raise NativeSchemaError('native schema metadata row is missing')
    try:
        version = int(row[0])
    except (TypeError, ValueError) as exc:
        raise NativeSchemaError('native schema version is invalid') from exc
    if version < 1:
        raise NativeSchemaError(f'native schema version must be positive: {version}')
    return version


def read_native_schema_version(path: Path) -> int:
    """Read the native DB schema without modifying the database.

    Version 0 means a pre-versioning/legacy database or an empty database.
    """

    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return 0
    try:
        with closing(
            sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
        ) as connection:
            return _stored_version(connection)
    except sqlite3.DatabaseError as exc:
        raise NativeSchemaError(f'native database schema could not be read: {exc}') from exc


def check_native_schema_compatibility(path: Path) -> int:
    """Reject data created by a newer native schema while accepting legacy v0."""

    version = read_native_schema_version(path)
    if version > NATIVE_SCHEMA_VERSION:
        raise NativeSchemaError(
            f'native database schema v{version} is newer than this application '
            f'supports (v{NATIVE_SCHEMA_VERSION})'
        )
    return version


def _validate_legacy_database(connection: sqlite3.Connection) -> None:
    tables = _table_names(connection)
    if not tables:
        return
    recognized = {
        table
        for table in tables
        if table in _LEGACY_NATIVE_TABLES or table.startswith('cad_')
    }
    unexpected = sorted(tables - recognized)
    if unexpected:
        raise NativeSchemaError(
            'refusing to adopt an unversioned database with unrelated tables: '
            + ', '.join(unexpected)
        )

    integrity = connection.execute('PRAGMA integrity_check').fetchall()
    if integrity != [('ok',)]:
        raise NativeSchemaError(f'legacy native database integrity check failed: {integrity!r}')
    foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
    if foreign_keys:
        raise NativeSchemaError(
            f'legacy native database foreign-key check failed: {foreign_keys!r}'
        )


def _migrate_0_to_1(connection: sqlite3.Connection) -> None:
    _validate_legacy_database(connection)
    connection.execute(
        f'''
        CREATE TABLE IF NOT EXISTS {_METADATA_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL
        )
        '''
    )
    connection.execute(
        f'''
        CREATE TABLE IF NOT EXISTS {_MIGRATION_TABLE} (
            schema_version INTEGER PRIMARY KEY,
            applied_at_utc TEXT NOT NULL,
            description TEXT NOT NULL
        )
        '''
    )
    connection.execute(
        f'INSERT INTO {_METADATA_TABLE}(singleton, schema_version) VALUES (1, ?)',
        (1,),
    )
    connection.execute(
        f'''
        INSERT INTO {_MIGRATION_TABLE}(schema_version, applied_at_utc, description)
        VALUES (?, ?, ?)
        ''',
        (1, _utc_now(), 'adopt pre-versioned native CAD schema as baseline v1'),
    )


def _migrate_1_to_2(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS cad_adaptive_extended_observations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id TEXT NOT NULL UNIQUE,
            extended_search_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            objective_id TEXT NOT NULL,
            observation_sha256 TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_adaptive_extended_observation_search_seq
            ON cad_adaptive_extended_observations(extended_search_id, seq ASC)
        """,
        """
        CREATE TABLE IF NOT EXISTS cad_adaptive_extended_plans (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL UNIQUE,
            document_id TEXT NOT NULL,
            extended_search_id TEXT NOT NULL,
            validation_id TEXT NOT NULL,
            execution_scope TEXT NOT NULL,
            selected_candidate_id TEXT NOT NULL,
            adaptive_extended_sha256 TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_adaptive_extended_plan_search_seq
            ON cad_adaptive_extended_plans(extended_search_id, seq ASC)
        """,
    )
    for statement in statements:
        connection.execute(statement)



def _migrate_2_to_3(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS cad_raw_mesh_repair_bundles (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            repaired_mesh_id TEXT NOT NULL UNIQUE,
            repaired_mesh_semantic_hash TEXT NOT NULL,
            raw_mesh_id TEXT NOT NULL,
            raw_mesh_semantic_hash TEXT NOT NULL,
            repair_plan_id TEXT NOT NULL,
            repair_plan_semantic_hash TEXT NOT NULL,
            post_diagnostic_id TEXT NOT NULL,
            post_diagnostic_semantic_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_mesh_repair_bundle_source
            ON cad_raw_mesh_repair_bundles(raw_mesh_id, seq ASC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_mesh_repair_bundle_plan
            ON cad_raw_mesh_repair_bundles(repair_plan_id, seq ASC)
        """
    )


def _migrate_3_to_4(connection: sqlite3.Connection) -> None:
    # Canonical content-addressed blob authority shared by capture ingestion
    # and raw-mesh repair. Raw payloads are stored once under their SHA-256
    # and records reference them by digest instead of embedding Base64 copies.
    connection.execute(CONTENT_BLOB_DDL)


_MIGRATIONS = {
    1: _migrate_0_to_1,
    2: _migrate_1_to_2,
    3: _migrate_2_to_3,
    4: _migrate_3_to_4,
}



def ensure_native_schema(path: Path) -> int:
    """Atomically migrate a native CAD database to the supported schema.

    Existing 0.1.0-era databases have no central schema marker. They are adopted
    as v1 only after integrity/foreign-key checks and only when their tables look
    like HTDT native tables. A database from a newer application is never opened.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
            version = _stored_version(connection)
            if version > NATIVE_SCHEMA_VERSION:
                raise NativeSchemaError(
                    f'native database schema v{version} is newer than this application '
                    f'supports (v{NATIVE_SCHEMA_VERSION})'
                )
            while version < NATIVE_SCHEMA_VERSION:
                target = version + 1
                migration = _MIGRATIONS.get(target)
                if migration is None:
                    raise NativeSchemaError(
                        f'no native database migration is registered for v{version}->v{target}'
                    )
                migration(connection)
                version = target
                if target != 1:
                    connection.execute(
                        f'UPDATE {_METADATA_TABLE} SET schema_version=? WHERE singleton=1',
                        (version,),
                    )
                    connection.execute(
                        f'''
                        INSERT INTO {_MIGRATION_TABLE}(
                            schema_version, applied_at_utc, description
                        ) VALUES (?, ?, ?)
                        ''',
                        (version, _utc_now(), f'migrate native schema to v{version}'),
                    )
            return version
    except sqlite3.DatabaseError as exc:
        raise NativeSchemaError(f'native database schema migration failed: {exc}') from exc
