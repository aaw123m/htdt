# -*- coding: utf-8 -*-
"""Round 14 store-layer fault-injection regressions.

Each test injects a real fault (rolled-back backfill, corrupted JSON
column, sqlite busy/corrupt/full) and asserts the honest outcome:
converged data, flagged-but-readable rows, or a mapped operator message.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from htdt.database import (
    DatasetIntegrityError,
    Store,
    _pack,
)
from htdt.user_facing_error import (
    RETRYABLE_ERROR_CODES,
    to_user_facing_error,
)

REW = b'20 70\n40 71\n80 72\n160 73\n'


def _store_with_dataset(tmp_path: Path) -> tuple[Store, dict, dict, str]:
    store = Store(tmp_path)
    project = store.create_project('p')
    context = store.create_context(project['id'], {'room': {}}, None)
    imported = store.import_measurement(
        project['id'], context['id'], 'm.txt', REW, 'main', 'measured_direct',
        [], 'unknown', None, None,
    )
    store.create_constraint_set(
        project['id'], context['id'], 'cs', {'a': 1}
    )
    return store, project, context, imported['dataset_id']


def _legacy_crashed_db(tmp_path: Path) -> Path:
    """Build the interrupted-upgrade state a crash actually leaves behind.

    A pre-hash-column database is created with the old DDL; the hash columns
    are then added by ``ALTER`` (autocommitted, nullable — exactly what
    ``_initialise`` runs), but the backfill UPDATEs are never run, matching
    the kill-between-DDL-and-backfill window.
    """
    db_path = tmp_path / 'htdt.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.executescript(
        '''
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE contexts (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, revision_number INTEGER NOT NULL,
            parent_context_id TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE TABLE constraint_sets (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            context_id TEXT NOT NULL, name TEXT, spec_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE measurements (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, context_id TEXT NOT NULL,
            session_id TEXT, channel_role TEXT NOT NULL, evidence_type TEXT NOT NULL,
            source_speaker_ids_json TEXT NOT NULL, radiation_scope TEXT NOT NULL,
            routing_evidence TEXT NOT NULL DEFAULT 'unknown', captured_at TEXT, imported_at TEXT NOT NULL,
            notes TEXT, quality_status TEXT NOT NULL DEFAULT 'unknown',
            quality_reasons_json TEXT NOT NULL DEFAULT '[]',
            quality_source TEXT NOT NULL DEFAULT 'unknown', repeat_group TEXT
        );
        CREATE TABLE datasets (
            id TEXT PRIMARY KEY, measurement_id TEXT NOT NULL, asset_sha256 TEXT,
            kind TEXT NOT NULL, frequency_blob BLOB, level_blob BLOB, phase_blob BLOB,
            metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE assets (
            sha256 TEXT PRIMARY KEY, relative_path TEXT NOT NULL, original_filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        '''
    )
    connection.execute(
        "INSERT INTO metadata VALUES('schema_version','5')"
    )
    connection.execute(
        "INSERT INTO projects VALUES('p1','proj','2026-01-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO contexts VALUES('c1','p1',1,NULL,'2026-01-01T00:00:00+00:00','{}')"
    )
    connection.execute(
        "INSERT INTO constraint_sets VALUES('cs1','p1','c1','cs','{\"a\":1}',"
        "'2026-01-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO measurements VALUES('m1','p1','c1',NULL,'front_left','measured','[]',"
        "'single','unknown',NULL,'2026-01-01T00:00:00+00:00',NULL,'unknown','[]','unknown',NULL)"
    )
    connection.execute(
        "INSERT INTO datasets VALUES('d1','m1',NULL,'frequency_response',?,?,NULL,'{}',"
        "'2026-01-01T00:00:00+00:00')",
        (_pack((20.0, 40.0, 80.0, 160.0)), _pack((70.0, 71.0, 72.0, 73.0))),
    )
    # The crash: columns committed by ALTER, backfill UPDATEs never ran.
    connection.execute('ALTER TABLE constraint_sets ADD COLUMN spec_sha256 TEXT')
    connection.execute('ALTER TABLE datasets ADD COLUMN dataset_sha256 TEXT')
    connection.commit()
    connection.close()
    return db_path


def test_interrupted_hash_backfill_converges_on_next_open(tmp_path: Path) -> None:
    _legacy_crashed_db(tmp_path)
    reopened = Store(tmp_path)
    connection = sqlite3.connect(reopened.db_path)
    spec_nulls = connection.execute(
        'SELECT COUNT(*) FROM constraint_sets WHERE spec_sha256 IS NULL'
    ).fetchone()[0]
    dataset_nulls = connection.execute(
        'SELECT COUNT(*) FROM datasets WHERE dataset_sha256 IS NULL'
    ).fetchone()[0]
    connection.close()
    assert spec_nulls == 0 and dataset_nulls == 0
    assert reopened.integrity_problems() == []
    listed = reopened.list_constraint_sets('p1')
    assert [row['integrity_valid'] for row in listed] == [True]
    assert listed[0]['spec'] == {'a': 1}


def test_null_dataset_hash_is_reported_until_converged(tmp_path: Path) -> None:
    _legacy_crashed_db(tmp_path)
    # Read directly against the crashed file: NULL hashes are detectable.
    connection = sqlite3.connect(tmp_path / 'htdt.sqlite3')
    nulls = connection.execute(
        'SELECT COUNT(*) FROM datasets WHERE dataset_sha256 IS NULL'
    ).fetchone()[0]
    connection.close()
    assert nulls == 1
    reopened = Store(tmp_path)
    assert reopened.integrity_problems() == []
    assert reopened.list_measurements('p1')[0]['integrity_valid']


def test_corrupt_metadata_json_flags_row_without_bricking_list(
    tmp_path: Path,
) -> None:
    store, project, _context, dataset_id = _store_with_dataset(tmp_path)
    connection = sqlite3.connect(store.db_path)
    connection.execute("UPDATE datasets SET metadata_json='{not json'")
    connection.execute(
        "UPDATE measurements SET quality_reasons_json='[broken'"
    )
    connection.commit()
    connection.close()

    rows = store.list_measurements(project['id'])
    assert len(rows) == 1
    assert rows[0]['integrity_valid'] is False
    assert rows[0]['metadata'] is None
    assert rows[0]['quality_reasons'] is None

    descriptor = store.get_dataset_descriptor(dataset_id)
    assert descriptor['integrity_valid'] is False
    assert descriptor['dataset_metadata'] is None
    assert 'dataset_hash_mismatch' in ' '.join(store.integrity_problems())
    with pytest.raises(DatasetIntegrityError):
        store.get_frequency_response(dataset_id)


def test_corrupt_spec_json_flags_row_without_bricking_list(
    tmp_path: Path,
) -> None:
    store, project, _context, _dataset_id = _store_with_dataset(tmp_path)
    connection = sqlite3.connect(store.db_path)
    connection.execute("UPDATE constraint_sets SET spec_json='{nope'")
    connection.commit()
    connection.close()

    listed = store.list_constraint_sets(project['id'])
    assert len(listed) == 1
    assert listed[0]['integrity_valid'] is False
    assert listed[0]['spec'] is None
    fetched = store.get_constraint_set(project['id'], listed[0]['id'])
    assert fetched is not None and fetched['integrity_valid'] is False


def _raise_for_captured(
    connect: sqlite3.Connection, statement: str
) -> sqlite3.Error:
    try:
        connect.execute(statement)
    except sqlite3.Error as exc:  # real engine error, real errorcode
        return exc
    raise AssertionError(f'{statement} did not raise')


def test_locked_write_maps_to_retryable_locked_message(tmp_path: Path) -> None:
    db_path = tmp_path / 'contended.sqlite3'
    holder = sqlite3.connect(db_path)
    holder.execute('CREATE TABLE t(id INTEGER)')
    holder.execute('BEGIN IMMEDIATE')
    holder.execute('INSERT INTO t VALUES(1)')
    contended = sqlite3.connect(db_path, timeout=0.1)
    try:
        locked_exc = _raise_for_captured(contended, 'BEGIN IMMEDIATE')
    finally:
        contended.close()
        holder.rollback()
        holder.close()
    err = to_user_facing_error(locked_exc, title='保存に失敗しました')
    assert err.code == 'storage.locked'
    assert err.code in RETRYABLE_ERROR_CODES
    assert '使用' in err.message
    assert err.technical_detail is not None
    assert 'locked' in err.technical_detail


def test_not_a_database_maps_to_corruption_message(tmp_path: Path) -> None:
    bad = tmp_path / 'notadb.sqlite3'
    bad.write_bytes(b'\x00' * 512 + b'garbage')
    notadb_exc = _raise_for_captured(
        sqlite3.connect(bad), 'SELECT * FROM sqlite_master'
    )
    err = to_user_facing_error(notadb_exc, title='開けませんでした')
    assert err.code == 'storage.corrupt'
    assert '破損' in err.message
    assert err.recovery is not None and 'バックアップ' in err.recovery


def test_disk_full_maps_to_disk_space_message(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / 'full.sqlite3')
    conn.execute('CREATE TABLE t(v BLOB)')
    conn.execute('PRAGMA max_page_count=1')
    try:
        full_exc = _raise_for_captured(conn, "INSERT INTO t VALUES(randomblob(8192))")
    finally:
        conn.close()
    err = to_user_facing_error(full_exc, title='保存に失敗しました')
    assert err.code == 'io.no_space'
    assert '容量' in err.message


def test_generic_sqlite_error_stays_storage_scoped(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / 'gen.sqlite3')
    try:
        exc = _raise_for_captured(conn, 'SELECT * FROM no_such_table')
    finally:
        conn.close()
    err = to_user_facing_error(exc, title='読み込みに失敗しました')
    assert err.code == 'storage.error'
    assert err.technical_detail is not None and 'no_such_table' in err.technical_detail
