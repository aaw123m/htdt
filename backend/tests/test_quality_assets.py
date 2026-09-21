from __future__ import annotations

import sqlite3
from pathlib import Path

from htdt.conditions import classify_differences, context_differences
from htdt.database import SCHEMA_VERSION, Store


def _context_payload(*, fl_x: float = 1.0, volume_db: float | None = None) -> dict:
    return {
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4, 'geometry_kind': 'rectangular', 'notes': None},
        'speakers': [{'speaker_id': 'FL', 'role': 'front_left', 'model': None, 'position': {'x_m': fl_x, 'y_m': 1.0, 'z_m': 1.0}, 'aim_xyz': None, 'mounting_type': None}],
        'measurement_point': {'point_id': 'mlp', 'label': 'MLP', 'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0}, 'aim_xyz': None, 'position_precision_m': None},
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A', 'firmware': None, 'input_name': None, 'volume_db': volume_db, 'processing_mode': None, 'peq_mode': None, 'extra': {}},
        'notes': None,
        'parent_context_id': None,
    }


def test_schema_v1_is_migrated_to_current_schema(tmp_path: Path) -> None:
    root = tmp_path / 'legacy'
    root.mkdir()
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
        ''')
        db.commit()
    finally:
        db.close()

    Store(root)
    migrated = sqlite3.connect(root / 'htdt.sqlite3')
    try:
        version = migrated.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        columns = {row[1] for row in migrated.execute('PRAGMA table_info(measurements)')}
        tables = {row[0] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        migrated.close()
    assert int(version) == SCHEMA_VERSION
    assert {'quality_status', 'quality_reasons_json', 'quality_source', 'repeat_group', 'routing_evidence', 'session_id'} <= columns
    assert {'asset_links', 'sessions'} <= tables


def test_quality_duplicate_asset_attachment_persists_across_reopen(tmp_path: Path) -> None:
    data_root = tmp_path / 'data'
    store = Store(data_root)
    project = store.create_project('Room')
    context = store.create_context(project['id'], _context_payload(), None)
    raw = b'20 70\n40 71\n80 72\n160 73\n'
    first = store.import_measurement(project['id'], context['id'], 'fl.txt', raw, 'front_left', 'measured', ['FL'], 'single', None, None,
                                     quality_status='usable', quality_reasons=['level checked'], quality_source='manual', repeat_group='fl-r1', routing_evidence='manual')
    second = store.import_measurement(project['id'], context['id'], 'fl-repeat.txt', raw, 'front_left', 'measured', ['FL'], 'single', None, None,
                                      quality_status='warning', quality_reasons=['duplicate fixture'], quality_source='manual', repeat_group='fl-r1')
    assert first['duplicate_asset'] is False
    assert second['duplicate_asset'] is True
    rows = store.list_measurements(project['id'])
    assert {row['quality_status'] for row in rows} == {'usable', 'warning'}
    attachment = store.attach_asset(project['id'], 'session.mdat', b'synthetic-mdat-fixture', 'mdat', 'Synthetic fixture', first['measurement_id'], context['id'])
    assert attachment['kind'] == 'mdat'
    reopened = Store(data_root)
    assert len(reopened.list_attachments(project['id'])) == 1
    assert reopened.integrity_problems() == []


def test_missing_asset_is_reported_by_integrity_check(tmp_path: Path) -> None:
    store = Store(tmp_path / 'data')
    project = store.create_project('Room')
    context = store.create_context(project['id'], _context_payload(), None)
    imported = store.import_measurement(project['id'], context['id'], 'fl.txt', b'20 70\n40 71\n80 72\n160 73\n', 'front_left', 'measured', ['FL'], 'single', None, None)
    with store.connect() as db:
        relative = db.execute('SELECT relative_path FROM assets WHERE sha256 = ?', (imported['asset_sha256'],)).fetchone()[0]
    (store.root / relative).unlink()
    problems = store.integrity_problems()
    assert any(problem.startswith('missing_asset:') for problem in problems)


def test_context_diff_expected_and_confounder() -> None:
    a = _context_payload(fl_x=1.0, volume_db=-30.0)
    b = _context_payload(fl_x=1.1, volume_db=-28.0)
    classified = classify_differences(context_differences(a, b), ['speakers.FL.position'])
    assert any(item['path'] == 'speakers.FL.position.x_m' for item in classified['intended'])
    assert any(item['path'] == 'avr.volume_db' for item in classified['confounders'])
