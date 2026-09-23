from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from htdt.database import SCHEMA_VERSION, Store, canonical_json_sha256
from htdt.migration_guard import MigrationOpenError


def _create_v1_root(root: Path, *, with_asset: bool = True) -> tuple[Path, bytes | None]:
    root.mkdir(parents=True)
    assets = root / 'assets'
    assets.mkdir()
    asset_bytes = b'legacy-raw-asset' if with_asset else None
    digest = hashlib.sha256(asset_bytes).hexdigest() if asset_bytes is not None else None
    relative = f'assets/{digest}.bin' if digest else None
    if asset_bytes is not None and relative is not None:
        (root / relative).write_bytes(asset_bytes)

    db = sqlite3.connect(root / 'htdt.sqlite3')
    try:
        db.executescript('''
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata(key, value) VALUES ('schema_version', '1');
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE contexts (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), revision_number INTEGER NOT NULL, parent_context_id TEXT REFERENCES contexts(id), created_at TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(project_id, revision_number));
        CREATE TABLE assets (sha256 TEXT PRIMARY KEY, relative_path TEXT NOT NULL, original_filename TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE measurements (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), context_id TEXT NOT NULL REFERENCES contexts(id), channel_role TEXT NOT NULL, evidence_type TEXT NOT NULL, source_speaker_ids_json TEXT NOT NULL, radiation_scope TEXT NOT NULL, captured_at TEXT, imported_at TEXT NOT NULL, notes TEXT);
        CREATE TABLE datasets (id TEXT PRIMARY KEY, measurement_id TEXT NOT NULL REFERENCES measurements(id), asset_sha256 TEXT NOT NULL REFERENCES assets(sha256), kind TEXT NOT NULL, frequency_blob BLOB NOT NULL, level_blob BLOB NOT NULL, phase_blob BLOB, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE comparisons (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), dataset_a_id TEXT NOT NULL REFERENCES datasets(id), dataset_b_id TEXT NOT NULL REFERENCES datasets(id), spec_json TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO projects(id, name, created_at) VALUES ('p1', 'Legacy Room', '2026-09-15T00:00:00+00:00');
        ''')
        if asset_bytes is not None and digest is not None and relative is not None:
            db.execute(
                'INSERT INTO assets(sha256, relative_path, original_filename, size_bytes, created_at) VALUES (?, ?, ?, ?, ?)',
                (digest, relative, 'legacy.bin', len(asset_bytes), '2026-09-15T00:00:00+00:00'),
            )
        db.commit()
    finally:
        db.close()
    return root, asset_bytes


def _create_v2_root(root: Path) -> Path:
    root, _ = _create_v1_root(root, with_asset=False)
    db = sqlite3.connect(root / 'htdt.sqlite3')
    try:
        db.executescript('''
        ALTER TABLE measurements ADD COLUMN routing_evidence TEXT NOT NULL DEFAULT 'unknown';
        ALTER TABLE measurements ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'unknown';
        ALTER TABLE measurements ADD COLUMN quality_reasons_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE measurements ADD COLUMN quality_source TEXT NOT NULL DEFAULT 'unknown';
        ALTER TABLE measurements ADD COLUMN repeat_group TEXT;
        CREATE TABLE asset_links (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), asset_sha256 TEXT NOT NULL REFERENCES assets(sha256),
            measurement_id TEXT REFERENCES measurements(id), context_id TEXT REFERENCES contexts(id), kind TEXT NOT NULL,
            label TEXT, filename TEXT NOT NULL, created_at TEXT NOT NULL
        );
        UPDATE metadata SET value='2' WHERE key='schema_version';
        ''')
        db.commit()
    finally:
        db.close()
    return root


def _create_v3_root(root: Path) -> Path:
    root = _create_v2_root(root)
    db = sqlite3.connect(root / 'htdt.sqlite3')
    try:
        db.executescript('''
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), purpose TEXT,
            started_at TEXT, notes TEXT, created_at TEXT NOT NULL
        );
        ALTER TABLE measurements ADD COLUMN session_id TEXT REFERENCES sessions(id);
        UPDATE metadata SET value='3' WHERE key='schema_version';
        ''')
        db.commit()
    finally:
        db.close()
    return root


def _create_v4_root(root: Path, *, hashless_constraint_set: bool = False) -> Path:
    root = _create_v3_root(root)
    db = sqlite3.connect(root / 'htdt.sqlite3')
    try:
        if hashless_constraint_set:
            db.execute("CREATE TABLE constraint_sets (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), context_id TEXT NOT NULL REFERENCES contexts(id), name TEXT, spec_json TEXT NOT NULL, created_at TEXT NOT NULL)")
            db.execute("INSERT INTO contexts(id, project_id, revision_number, parent_context_id, created_at, payload_json) VALUES ('c1','p1',1,NULL,'2026-09-15T00:00:00+00:00','{}')")
            db.execute("INSERT INTO constraint_sets VALUES ('cs1','p1','c1','legacy','{\"engine_version\":\"placement-constraints-1\",\"constraints\":[],\"entity_profiles\":[]}','2026-09-15T00:00:00+00:00')")
        else:
            db.execute("CREATE TABLE constraint_sets (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), context_id TEXT NOT NULL REFERENCES contexts(id), name TEXT, spec_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)")
        db.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
        db.commit()
    finally:
        db.close()
    return root


def _schema_version(root: Path) -> int:
    db = sqlite3.connect(root / 'htdt.sqlite3')
    try:
        return int(db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0])
    finally:
        db.close()


def test_v1_open_creates_pre_migration_backup_before_upgrade(tmp_path: Path) -> None:
    root, asset_bytes = _create_v1_root(tmp_path / 'legacy')

    store = Store(root)

    assert SCHEMA_VERSION == 5
    assert _schema_version(root) == SCHEMA_VERSION
    assert store.migrated_from_schema_version == 1
    assert store.pre_migration_backup.is_file()
    assert store.list_projects()[0]['name'] == 'Legacy Room'

    with zipfile.ZipFile(store.pre_migration_backup, 'r') as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest == {'reason': 'pre_migration', 'schema_version': 1, 'target_schema_version': SCHEMA_VERSION}
        snapshot = tmp_path / 'snapshot.sqlite3'
        snapshot.write_bytes(archive.read('htdt.sqlite3'))
        asset_name = next(name for name in archive.namelist() if name.startswith('assets/') and name != 'assets/')
        assert archive.read(asset_name) == asset_bytes
    snapshot_db = sqlite3.connect(snapshot)
    try:
        assert int(snapshot_db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]) == 1
        columns = {row[1] for row in snapshot_db.execute('PRAGMA table_info(measurements)')}
        assert 'quality_status' not in columns
        assert 'session_id' not in columns
    finally:
        snapshot_db.close()


def test_v2_open_adds_sessions_without_inventing_session_membership(tmp_path: Path) -> None:
    root = _create_v2_root(tmp_path / 'legacy-v2')

    store = Store(root)

    assert _schema_version(root) == SCHEMA_VERSION
    assert store.migrated_from_schema_version == 2
    assert store.list_sessions('p1') == []
    with store.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in db.execute('PRAGMA table_info(measurements)')}
    assert 'sessions' in tables
    assert 'session_id' in columns

    with zipfile.ZipFile(store.pre_migration_backup, 'r') as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest == {'reason': 'pre_migration', 'schema_version': 2, 'target_schema_version': SCHEMA_VERSION}
        snapshot = tmp_path / 'snapshot-v2.sqlite3'
        snapshot.write_bytes(archive.read('htdt.sqlite3'))
    snapshot_db = sqlite3.connect(snapshot)
    try:
        assert int(snapshot_db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]) == 2
        assert snapshot_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone() is None
    finally:
        snapshot_db.close()


def test_v3_open_adds_empty_constraint_sets_table_after_pre_migration_backup(tmp_path: Path) -> None:
    root = _create_v3_root(tmp_path / 'legacy-v3')

    store = Store(root)

    assert _schema_version(root) == SCHEMA_VERSION == 5
    assert store.migrated_from_schema_version == 3
    assert store.list_constraint_sets('p1') == []
    with store.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'constraint_sets' in tables

    with zipfile.ZipFile(store.pre_migration_backup, 'r') as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest == {'reason': 'pre_migration', 'schema_version': 3, 'target_schema_version': SCHEMA_VERSION}
        snapshot = tmp_path / 'snapshot-v3.sqlite3'
        snapshot.write_bytes(archive.read('htdt.sqlite3'))
    snapshot_db = sqlite3.connect(snapshot)
    try:
        assert int(snapshot_db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]) == 3
        assert snapshot_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='constraint_sets'").fetchone() is None
    finally:
        snapshot_db.close()


def test_v4_open_adds_empty_search_specs_after_pre_migration_backup(tmp_path: Path) -> None:
    root = _create_v4_root(tmp_path / 'legacy-v4')
    store = Store(root)
    assert _schema_version(root) == SCHEMA_VERSION == 5
    assert store.migrated_from_schema_version == 4
    assert store.list_search_specs('p1') == []
    with store.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'search_specs' in tables
    with zipfile.ZipFile(store.pre_migration_backup, 'r') as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest == {'reason': 'pre_migration', 'schema_version': 4, 'target_schema_version': 5}


def test_v4_hashless_constraint_set_is_normalized_before_v5_integrity_check(tmp_path: Path) -> None:
    root = _create_v4_root(tmp_path / 'legacy-v4-hashless', hashless_constraint_set=True)
    store = Store(root)
    record = store.get_constraint_set('p1', 'cs1')
    assert record is not None and record['integrity_valid'] is True
    assert record['spec_sha256'] == canonical_json_sha256(record['spec'])
    with store.connect() as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(constraint_sets)')}
    assert 'spec_sha256' in columns
    with zipfile.ZipFile(store.pre_migration_backup, 'r') as archive:
        snapshot = tmp_path / 'hashless-v4.sqlite3'
        snapshot.write_bytes(archive.read('htdt.sqlite3'))
    legacy = sqlite3.connect(snapshot)
    try:
        legacy_columns = {row[1] for row in legacy.execute('PRAGMA table_info(constraint_sets)')}
        assert 'spec_sha256' not in legacy_columns
    finally:
        legacy.close()


def test_failed_migration_restores_v1_database_and_raw_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, asset_bytes = _create_v1_root(tmp_path / 'legacy')
    assert asset_bytes is not None
    asset_path = next((root / 'assets').iterdir())
    original_initialise = Store._initialise

    def fail_after_partial_mutation(self: Store) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE metadata SET value='99' WHERE key='schema_version'")
            db.commit()
        asset_path.write_bytes(b'corrupted-during-migration')
        raise RuntimeError('synthetic migration failure')

    monkeypatch.setattr(Store, '_initialise', fail_after_partial_mutation)
    with pytest.raises(MigrationOpenError, match='previous data restored'):
        Store(root)

    assert _schema_version(root) == 1
    assert asset_path.read_bytes() == asset_bytes
    backups = list((root / 'backups').glob(f'pre-migration-v1-to-v{SCHEMA_VERSION}-*.zip'))
    assert len(backups) == 1

    monkeypatch.setattr(Store, '_initialise', original_initialise)
    reopened = Store(root)
    assert _schema_version(root) == SCHEMA_VERSION
    assert reopened.list_projects()[0]['name'] == 'Legacy Room'
    assert asset_path.read_bytes() == asset_bytes


def test_missing_legacy_asset_blocks_migration_without_schema_change(tmp_path: Path) -> None:
    root, _ = _create_v1_root(tmp_path / 'legacy')
    next((root / 'assets').iterdir()).unlink()

    with pytest.raises(MigrationOpenError, match='missing_asset'):
        Store(root)

    assert _schema_version(root) == 1
    assert not (root / 'backups').exists()


def test_newer_schema_is_refused_without_downgrade(tmp_path: Path) -> None:
    root = tmp_path / 'future'
    root.mkdir()
    db_path = root / 'htdt.sqlite3'
    db = sqlite3.connect(db_path)
    try:
        db.executescript("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL); INSERT INTO metadata VALUES ('schema_version', '999');")
        db.commit()
    finally:
        db.close()
    before = db_path.read_bytes()

    with pytest.raises(MigrationOpenError, match='newer'):
        Store(root)

    assert db_path.read_bytes() == before
