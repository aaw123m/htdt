from __future__ import annotations

from array import array
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from .comparison import FrequencyResponse
from .rew_api import RewFrequencyResponseSnapshot
from .rew_parser import parse_rew_frequency_response


SCHEMA_VERSION = 5
REW_API_SNAPSHOT_FORMAT = 'htdt-rew-api-frequency-response-snapshot-1'
REW_API_ADAPTER_VERSION = 'rew-api-snapshot-1'


class AssetIntegrityError(RuntimeError):
    """The store references an asset that is missing on disk.

    This is server-side store corruption, not a client input problem, so it
    deliberately does not subclass ``ValueError``: API handlers map
    ``ValueError`` to 422, which would misreport the fault as a bad request.
    """


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_sha256(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _pack(values: tuple[float, ...] | None) -> bytes | None:
    if values is None:
        return None
    payload = array('d', values)
    if payload.itemsize != 8:
        raise RuntimeError('Unexpected double size')
    if os.sys.byteorder != 'little':
        payload.byteswap()
    return payload.tobytes()


def _unpack(blob: bytes | None) -> tuple[float, ...] | None:
    if blob is None:
        return None
    payload = array('d')
    payload.frombytes(blob)
    if os.sys.byteorder != 'little':
        payload.byteswap()
    return tuple(payload)


def _column_names(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row['name']) for row in db.execute(f'PRAGMA table_info({table})')}


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets_dir = self.root / 'assets'
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / 'htdt.sqlite3'
        self._initialise()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        try:
            yield connection
        finally:
            connection.close()

    def _initialise(self) -> None:
        with self.connect() as db:
            db.executescript(
                '''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS contexts (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), revision_number INTEGER NOT NULL,
                    parent_context_id TEXT REFERENCES contexts(id), created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                    UNIQUE(project_id, revision_number)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), purpose TEXT,
                    started_at TEXT, notes TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS constraint_sets (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    context_id TEXT NOT NULL REFERENCES contexts(id), name TEXT, spec_json TEXT NOT NULL,
                    spec_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS search_specs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    context_id TEXT NOT NULL REFERENCES contexts(id),
                    constraint_set_id TEXT NOT NULL REFERENCES constraint_sets(id),
                    name TEXT, spec_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    sha256 TEXT PRIMARY KEY, relative_path TEXT NOT NULL, original_filename TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS measurements (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), context_id TEXT NOT NULL REFERENCES contexts(id),
                    session_id TEXT REFERENCES sessions(id), channel_role TEXT NOT NULL, evidence_type TEXT NOT NULL,
                    source_speaker_ids_json TEXT NOT NULL, radiation_scope TEXT NOT NULL,
                    routing_evidence TEXT NOT NULL DEFAULT 'unknown', captured_at TEXT, imported_at TEXT NOT NULL, notes TEXT,
                    quality_status TEXT NOT NULL DEFAULT 'unknown', quality_reasons_json TEXT NOT NULL DEFAULT '[]',
                    quality_source TEXT NOT NULL DEFAULT 'unknown', repeat_group TEXT
                );
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY, measurement_id TEXT NOT NULL REFERENCES measurements(id), asset_sha256 TEXT NOT NULL REFERENCES assets(sha256),
                    kind TEXT NOT NULL, frequency_blob BLOB NOT NULL, level_blob BLOB NOT NULL, phase_blob BLOB,
                    metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS comparisons (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), dataset_a_id TEXT NOT NULL REFERENCES datasets(id),
                    dataset_b_id TEXT NOT NULL REFERENCES datasets(id), spec_json TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_links (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), asset_sha256 TEXT NOT NULL REFERENCES assets(sha256),
                    measurement_id TEXT REFERENCES measurements(id), context_id TEXT REFERENCES contexts(id), kind TEXT NOT NULL,
                    label TEXT, filename TEXT NOT NULL, created_at TEXT NOT NULL
                );
                '''
            )
            columns = _column_names(db, 'measurements')
            migrations = (
                ('routing_evidence', "ALTER TABLE measurements ADD COLUMN routing_evidence TEXT NOT NULL DEFAULT 'unknown'"),
                ('quality_status', "ALTER TABLE measurements ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'unknown'"),
                ('quality_reasons_json', "ALTER TABLE measurements ADD COLUMN quality_reasons_json TEXT NOT NULL DEFAULT '[]'"),
                ('quality_source', "ALTER TABLE measurements ADD COLUMN quality_source TEXT NOT NULL DEFAULT 'unknown'"),
                ('repeat_group', 'ALTER TABLE measurements ADD COLUMN repeat_group TEXT'),
                ('session_id', 'ALTER TABLE measurements ADD COLUMN session_id TEXT REFERENCES sessions(id)'),
            )
            for name, sql in migrations:
                if name not in columns:
                    db.execute(sql)
            constraint_columns = _column_names(db, 'constraint_sets')
            if 'spec_sha256' not in constraint_columns:
                db.execute('ALTER TABLE constraint_sets ADD COLUMN spec_sha256 TEXT')
                for row in db.execute('SELECT id, spec_json FROM constraint_sets').fetchall():
                    spec = json.loads(row['spec_json'])
                    db.execute(
                        'UPDATE constraint_sets SET spec_sha256 = ? WHERE id = ?',
                        (canonical_json_sha256(spec), row['id']),
                    )
            db.execute('INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', ('schema_version', str(SCHEMA_VERSION)))
            db.commit()

    def create_project(self, name: str) -> dict[str, Any]:
        project = {'id': str(uuid4()), 'name': name.strip(), 'created_at': utc_now()}
        with self.connect() as db:
            db.execute('INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)', tuple(project.values()))
            db.commit()
        return project

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM projects ORDER BY created_at DESC')]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM projects WHERE id = ?', (project_id,)).fetchone()
            return dict(row) if row else None

    def create_session(self, project_id: str, purpose: str | None, started_at: str | None, notes: str | None) -> dict[str, Any]:
        session = {
            'id': str(uuid4()),
            'project_id': project_id,
            'purpose': purpose.strip() if purpose and purpose.strip() else None,
            'started_at': started_at.strip() if started_at and started_at.strip() else None,
            'notes': notes.strip() if notes and notes.strip() else None,
            'created_at': utc_now(),
        }
        with self.connect() as db:
            if db.execute('SELECT id FROM projects WHERE id = ?', (project_id,)).fetchone() is None:
                raise KeyError('project_not_found')
            db.execute(
                'INSERT INTO sessions(id, project_id, purpose, started_at, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                (session['id'], project_id, session['purpose'], session['started_at'], session['notes'], session['created_at']),
            )
            db.commit()
        return session

    def get_session(self, project_id: str, session_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM sessions WHERE id = ? AND project_id = ?', (session_id, project_id)).fetchone()
            return dict(row) if row else None

    def list_sessions(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                '''SELECT s.*, COUNT(m.id) AS measurement_count
                   FROM sessions s LEFT JOIN measurements m ON m.session_id = s.id
                   WHERE s.project_id = ? GROUP BY s.id ORDER BY COALESCE(s.started_at, s.created_at) DESC, s.created_at DESC''',
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_context(self, project_id: str, payload: dict[str, Any], parent_context_id: str | None) -> dict[str, Any]:
        with self.connect() as db:
            if db.execute('SELECT id FROM projects WHERE id = ?', (project_id,)).fetchone() is None:
                raise KeyError('project_not_found')
            if parent_context_id is not None and db.execute('SELECT id FROM contexts WHERE id = ? AND project_id = ?', (parent_context_id, project_id)).fetchone() is None:
                raise KeyError('parent_context_not_found')
            revision = db.execute('SELECT COALESCE(MAX(revision_number), 0) + 1 FROM contexts WHERE project_id = ?', (project_id,)).fetchone()[0]
            context = {'id': str(uuid4()), 'project_id': project_id, 'revision_number': revision, 'parent_context_id': parent_context_id, 'created_at': utc_now(), 'payload': payload}
            db.execute('INSERT INTO contexts(id, project_id, revision_number, parent_context_id, created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?)',
                       (context['id'], project_id, revision, parent_context_id, context['created_at'], json.dumps(payload, ensure_ascii=False, sort_keys=True)))
            db.commit()
        return context

    def list_contexts(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute('SELECT * FROM contexts WHERE project_id = ? ORDER BY revision_number DESC', (project_id,)).fetchall()
        return [{'id': row['id'], 'project_id': row['project_id'], 'revision_number': row['revision_number'], 'parent_context_id': row['parent_context_id'], 'created_at': row['created_at'], 'payload': json.loads(row['payload_json'])} for row in rows]

    def get_context(self, project_id: str, context_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM contexts WHERE id = ? AND project_id = ?', (context_id, project_id)).fetchone()
        if row is None:
            return None
        return {'id': row['id'], 'project_id': row['project_id'], 'revision_number': row['revision_number'], 'parent_context_id': row['parent_context_id'], 'created_at': row['created_at'], 'payload': json.loads(row['payload_json'])}

    def create_constraint_set(self, project_id: str, context_id: str, name: str | None, spec: dict[str, Any]) -> dict[str, Any]:
        record = {
            'id': str(uuid4()), 'project_id': project_id, 'context_id': context_id,
            'name': name.strip() if name and name.strip() else None, 'spec': spec,
            'spec_sha256': canonical_json_sha256(spec), 'created_at': utc_now(),
        }
        with self.connect() as db:
            if db.execute('SELECT id FROM projects WHERE id = ?', (project_id,)).fetchone() is None:
                raise KeyError('project_not_found')
            if db.execute('SELECT id FROM contexts WHERE id = ? AND project_id = ?', (context_id, project_id)).fetchone() is None:
                raise KeyError('context_not_found')
            db.execute(
                'INSERT INTO constraint_sets(id, project_id, context_id, name, spec_json, spec_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (record['id'], project_id, context_id, record['name'], json.dumps(spec, ensure_ascii=False, sort_keys=True),
                 record['spec_sha256'], record['created_at']),
            )
            db.commit()
        return record

    def list_constraint_sets(self, project_id: str, context_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            if context_id is None:
                rows = db.execute('SELECT * FROM constraint_sets WHERE project_id = ? ORDER BY created_at DESC', (project_id,)).fetchall()
            else:
                rows = db.execute(
                    'SELECT * FROM constraint_sets WHERE project_id = ? AND context_id = ? ORDER BY created_at DESC',
                    (project_id, context_id),
                ).fetchall()
        result = []
        for row in rows:
            spec = json.loads(row['spec_json'])
            digest = canonical_json_sha256(spec)
            result.append({
                'id': row['id'], 'project_id': row['project_id'], 'context_id': row['context_id'], 'name': row['name'],
                'spec': spec, 'spec_sha256': row['spec_sha256'], 'integrity_valid': digest == row['spec_sha256'],
                'created_at': row['created_at'],
            })
        return result

    def get_constraint_set(self, project_id: str, constraint_set_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                'SELECT * FROM constraint_sets WHERE id = ? AND project_id = ?',
                (constraint_set_id, project_id),
            ).fetchone()
        if row is None:
            return None
        spec = json.loads(row['spec_json'])
        digest = canonical_json_sha256(spec)
        return {
            'id': row['id'], 'project_id': row['project_id'], 'context_id': row['context_id'], 'name': row['name'],
            'spec': spec, 'spec_sha256': row['spec_sha256'], 'integrity_valid': digest == row['spec_sha256'],
            'created_at': row['created_at'],
        }

    def create_search_spec(self, project_id: str, context_id: str, constraint_set_id: str, name: str | None, spec: dict[str, Any]) -> dict[str, Any]:
        record = {
            'id': str(uuid4()), 'project_id': project_id, 'context_id': context_id,
            'constraint_set_id': constraint_set_id,
            'name': name.strip() if name and name.strip() else None, 'spec': spec,
            'spec_sha256': canonical_json_sha256(spec), 'created_at': utc_now(),
        }
        with self.connect() as db:
            row = db.execute(
                'SELECT context_id FROM constraint_sets WHERE id = ? AND project_id = ?',
                (constraint_set_id, project_id),
            ).fetchone()
            if row is None:
                raise KeyError('constraint_set_not_found')
            if row['context_id'] != context_id:
                raise ValueError('SearchSpec context must match ConstraintSet context')
            db.execute(
                'INSERT INTO search_specs(id, project_id, context_id, constraint_set_id, name, spec_json, spec_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (record['id'], project_id, context_id, constraint_set_id, record['name'],
                 json.dumps(spec, ensure_ascii=False, sort_keys=True), record['spec_sha256'], record['created_at']),
            )
            db.commit()
        return record

    def list_search_specs(self, project_id: str, context_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            if context_id is None:
                rows = db.execute('SELECT * FROM search_specs WHERE project_id = ? ORDER BY created_at DESC', (project_id,)).fetchall()
            else:
                rows = db.execute(
                    'SELECT * FROM search_specs WHERE project_id = ? AND context_id = ? ORDER BY created_at DESC',
                    (project_id, context_id),
                ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            spec = json.loads(row['spec_json'])
            result.append({
                'id': row['id'], 'project_id': row['project_id'], 'context_id': row['context_id'],
                'constraint_set_id': row['constraint_set_id'], 'name': row['name'], 'spec': spec,
                'spec_sha256': row['spec_sha256'],
                'integrity_valid': canonical_json_sha256(spec) == row['spec_sha256'],
                'created_at': row['created_at'],
            })
        return result

    def get_search_spec(self, project_id: str, search_spec_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                'SELECT * FROM search_specs WHERE id = ? AND project_id = ?',
                (search_spec_id, project_id),
            ).fetchone()
        if row is None:
            return None
        spec = json.loads(row['spec_json'])
        return {
            'id': row['id'], 'project_id': row['project_id'], 'context_id': row['context_id'],
            'constraint_set_id': row['constraint_set_id'], 'name': row['name'], 'spec': spec,
            'spec_sha256': row['spec_sha256'],
            'integrity_valid': canonical_json_sha256(spec) == row['spec_sha256'],
            'created_at': row['created_at'],
        }

    def _store_asset(self, filename: str, raw: bytes) -> tuple[str, Path, bool, bool]:
        digest = hashlib.sha256(raw).hexdigest()
        with self.connect() as db:
            existing = db.execute('SELECT relative_path FROM assets WHERE sha256 = ?', (digest,)).fetchone()
        if existing is not None:
            target = self.root / str(existing['relative_path'])
            if not target.is_file():
                raise AssetIntegrityError(f'Raw asset {digest} is referenced by the database but missing on disk')
            return digest, target, False, True
        suffix = Path(filename).suffix.lower()
        safe_suffix = suffix if suffix and len(suffix) <= 12 else '.bin'
        target = self.assets_dir / f'{digest}{safe_suffix}'
        created = False
        if not target.exists():
            temp = target.with_suffix(target.suffix + '.tmp')
            temp.write_bytes(raw)
            os.replace(temp, target)
            created = True
        return digest, target, created, False

    def import_measurement(self, project_id: str, context_id: str, filename: str, raw: bytes, channel_role: str,
                           evidence_type: str, source_speaker_ids: list[str], radiation_scope: str, captured_at: str | None,
                           notes: str | None, routing_evidence: str = 'unknown', quality_status: str = 'unknown',
                           quality_reasons: list[str] | None = None, quality_source: str = 'unknown', repeat_group: str | None = None,
                           session_id: str | None = None) -> dict[str, Any]:
        parsed = parse_rew_frequency_response(raw)
        asset_sha, asset_path, asset_created, already_known = self._store_asset(filename, raw)
        measurement_id, dataset_id, imported_at = str(uuid4()), str(uuid4()), utc_now()
        metadata = {'parser_version': parsed.parser_version, 'phase_status': parsed.phase_status, 'level_reference': parsed.level_reference,
                    'warnings': list(parsed.warnings), 'header_lines': list(parsed.header_lines), 'source_sha256': parsed.source_sha256}
        existing_count = 0
        try:
            with self.connect() as db:
                if db.execute('SELECT id FROM contexts WHERE id = ? AND project_id = ?', (context_id, project_id)).fetchone() is None:
                    raise KeyError('context_not_found')
                if session_id is not None and db.execute('SELECT id FROM sessions WHERE id = ? AND project_id = ?', (session_id, project_id)).fetchone() is None:
                    raise KeyError('session_not_found')
                existing_count = int(db.execute('SELECT COUNT(*) FROM datasets WHERE asset_sha256 = ?', (asset_sha,)).fetchone()[0])
                db.execute('INSERT OR IGNORE INTO assets(sha256, relative_path, original_filename, size_bytes, created_at) VALUES (?, ?, ?, ?, ?)',
                           (asset_sha, str(asset_path.relative_to(self.root)), filename, len(raw), imported_at))
                db.execute('''INSERT INTO measurements(id, project_id, context_id, session_id, channel_role, evidence_type, source_speaker_ids_json,
                           radiation_scope, routing_evidence, captured_at, imported_at, notes, quality_status, quality_reasons_json,
                           quality_source, repeat_group) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                           (measurement_id, project_id, context_id, session_id, channel_role, evidence_type, json.dumps(source_speaker_ids), radiation_scope,
                            routing_evidence, captured_at, imported_at, notes, quality_status, json.dumps(quality_reasons or [], ensure_ascii=False),
                            quality_source, repeat_group.strip() if repeat_group else None))
                db.execute('''INSERT INTO datasets(id, measurement_id, asset_sha256, kind, frequency_blob, level_blob, phase_blob, metadata_json, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                           (dataset_id, measurement_id, asset_sha, 'frequency_response', _pack(parsed.frequency_hz), _pack(parsed.level_db),
                            _pack(parsed.phase_deg), json.dumps(metadata, ensure_ascii=False, sort_keys=True), imported_at))
                db.commit()
        except Exception:
            if asset_created:
                asset_path.unlink(missing_ok=True)
            raise
        return {'measurement_id': measurement_id, 'dataset_id': dataset_id, 'asset_sha256': asset_sha, 'points': len(parsed.frequency_hz),
                'frequency_min_hz': parsed.frequency_hz[0], 'frequency_max_hz': parsed.frequency_hz[-1], 'phase_status': parsed.phase_status,
                'warnings': list(parsed.warnings), 'duplicate_asset': already_known or existing_count > 0,
                'existing_dataset_count': existing_count, 'quality_status': quality_status, 'session_id': session_id}

    def import_rew_api_snapshot(
        self,
        project_id: str,
        context_id: str,
        snapshot: RewFrequencyResponseSnapshot,
        *,
        channel_role: str,
        evidence_type: str = 'unknown',
        source_speaker_ids: list[str] | None = None,
        radiation_scope: str = 'unknown',
        routing_evidence: str = 'unknown',
        notes: str | None = None,
        quality_status: str = 'unknown',
        quality_reasons: list[str] | None = None,
        quality_source: str = 'unknown',
        repeat_group: str | None = None,
        session_id: str | None = None,
        api_base_url: str,
    ) -> dict[str, Any]:
        decoded = snapshot.decoded
        phase_status = 'unknown' if decoded.phase_deg is not None else 'absent'
        warnings: list[str] = []
        if decoded.phase_deg is not None and all(value == 0 for value in decoded.phase_deg):
            warnings.append('phase_all_zero_unverified')
        wrapper = {
            'format': REW_API_SNAPSHOT_FORMAT,
            'measurement_uuid': decoded.measurement_id,
            'query': snapshot.query,
            'measurement_summary': snapshot.measurement_summary,
            'frequency_response': snapshot.raw_frequency_response,
        }
        raw = json.dumps(wrapper, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        filename = f'rew-api-{decoded.measurement_id}.json'
        asset_sha, asset_path, asset_created, already_known = self._store_asset(filename, raw)
        measurement_id, dataset_id, imported_at = str(uuid4()), str(uuid4()), utc_now()
        summary = snapshot.measurement_summary
        captured_at = summary.get('date') if isinstance(summary.get('date'), str) and summary.get('date') else None
        metadata = {
            'source': 'rew_api',
            'adapter_version': REW_API_ADAPTER_VERSION,
            'raw_format': REW_API_SNAPSHOT_FORMAT,
            'rew_measurement_uuid': decoded.measurement_id,
            'requested': {
                'unit': decoded.requested_unit,
                'ppo': decoded.requested_ppo,
                'smoothing': decoded.requested_smoothing,
            },
            'returned': {
                'unit': decoded.unit,
                'ppo': decoded.points_per_octave,
                'freq_step_hz': decoded.frequency_step_hz,
                'smoothing': decoded.smoothing,
                'start_frequency_hz': decoded.start_frequency_hz,
            },
            'rew_version': summary.get('rewVersion') if isinstance(summary.get('rewVersion'), str) else None,
            'raw_asset_sha256': asset_sha,
            'phase_status': phase_status,
            'warnings': warnings,
            'api_base_url': api_base_url,
        }
        existing_count = 0
        try:
            with self.connect() as db:
                if db.execute('SELECT id FROM contexts WHERE id = ? AND project_id = ?', (context_id, project_id)).fetchone() is None:
                    raise KeyError('context_not_found')
                if session_id is not None and db.execute('SELECT id FROM sessions WHERE id = ? AND project_id = ?', (session_id, project_id)).fetchone() is None:
                    raise KeyError('session_not_found')
                existing_count = int(db.execute('SELECT COUNT(*) FROM datasets WHERE asset_sha256 = ?', (asset_sha,)).fetchone()[0])
                db.execute('INSERT OR IGNORE INTO assets(sha256, relative_path, original_filename, size_bytes, created_at) VALUES (?, ?, ?, ?, ?)',
                           (asset_sha, str(asset_path.relative_to(self.root)), filename, len(raw), imported_at))
                db.execute('''INSERT INTO measurements(id, project_id, context_id, session_id, channel_role, evidence_type, source_speaker_ids_json,
                           radiation_scope, routing_evidence, captured_at, imported_at, notes, quality_status, quality_reasons_json,
                           quality_source, repeat_group) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                           (measurement_id, project_id, context_id, session_id, channel_role, evidence_type, json.dumps(source_speaker_ids or []),
                            radiation_scope, routing_evidence, captured_at, imported_at, notes, quality_status,
                            json.dumps(quality_reasons or [], ensure_ascii=False), quality_source, repeat_group.strip() if repeat_group else None))
                db.execute('''INSERT INTO datasets(id, measurement_id, asset_sha256, kind, frequency_blob, level_blob, phase_blob, metadata_json, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                           (dataset_id, measurement_id, asset_sha, 'frequency_response', _pack(decoded.frequency_hz), _pack(decoded.magnitude),
                            _pack(decoded.phase_deg), json.dumps(metadata, ensure_ascii=False, sort_keys=True), imported_at))
                db.commit()
        except Exception:
            if asset_created:
                asset_path.unlink(missing_ok=True)
            raise
        return {
            'measurement_id': measurement_id,
            'dataset_id': dataset_id,
            'asset_sha256': asset_sha,
            'points': len(decoded.frequency_hz),
            'frequency_min_hz': decoded.frequency_hz[0],
            'frequency_max_hz': decoded.frequency_hz[-1],
            'phase_status': phase_status,
            'warnings': warnings,
            'duplicate_asset': already_known or existing_count > 0,
            'existing_dataset_count': existing_count,
            'quality_status': quality_status,
            'routing_evidence': routing_evidence,
            'evidence_type': evidence_type,
            'session_id': session_id,
            'metadata': metadata,
        }

    def list_measurements(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute('''SELECT m.*, d.id AS dataset_id, d.frequency_blob, d.metadata_json, d.asset_sha256 FROM measurements m
                               JOIN datasets d ON d.measurement_id = m.id WHERE m.project_id = ? ORDER BY m.imported_at DESC''', (project_id,)).fetchall()
        result = []
        for row in rows:
            frequency = _unpack(row['frequency_blob']) or ()
            result.append({'id': row['id'], 'dataset_id': row['dataset_id'], 'context_id': row['context_id'], 'session_id': row['session_id'],
                           'channel_role': row['channel_role'], 'evidence_type': row['evidence_type'],
                           'source_speaker_ids': json.loads(row['source_speaker_ids_json']), 'radiation_scope': row['radiation_scope'],
                           'routing_evidence': row['routing_evidence'], 'captured_at': row['captured_at'], 'imported_at': row['imported_at'],
                           'notes': row['notes'], 'quality_status': row['quality_status'], 'quality_reasons': json.loads(row['quality_reasons_json']),
                           'quality_source': row['quality_source'], 'repeat_group': row['repeat_group'], 'asset_sha256': row['asset_sha256'],
                           'frequency_min_hz': frequency[0] if frequency else None, 'frequency_max_hz': frequency[-1] if frequency else None,
                           'points': len(frequency), 'metadata': json.loads(row['metadata_json'])})
        return result

    def get_frequency_response(self, dataset_id: str) -> FrequencyResponse:
        with self.connect() as db:
            row = db.execute('SELECT frequency_blob, level_blob FROM datasets WHERE id = ? AND kind = ?', (dataset_id, 'frequency_response')).fetchone()
            if row is None:
                raise KeyError('dataset_not_found')
        return FrequencyResponse(frequency_hz=_unpack(row['frequency_blob']) or (), level_db=_unpack(row['level_blob']) or ())

    def get_dataset_descriptor(self, dataset_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute('''SELECT d.id AS dataset_id, d.metadata_json, d.asset_sha256, m.id AS measurement_id, m.project_id, m.context_id,
                               m.session_id, m.channel_role, m.evidence_type, m.quality_status, m.quality_reasons_json, m.quality_source, m.repeat_group,
                               c.payload_json AS context_payload_json FROM datasets d JOIN measurements m ON m.id = d.measurement_id
                               JOIN contexts c ON c.id = m.context_id WHERE d.id = ?''', (dataset_id,)).fetchone()
        if row is None:
            raise KeyError('dataset_not_found')
        return {'dataset_id': row['dataset_id'], 'measurement_id': row['measurement_id'], 'project_id': row['project_id'], 'context_id': row['context_id'],
                'session_id': row['session_id'], 'channel_role': row['channel_role'], 'evidence_type': row['evidence_type'],
                'quality_status': row['quality_status'], 'quality_reasons': json.loads(row['quality_reasons_json']),
                'quality_source': row['quality_source'], 'repeat_group': row['repeat_group'], 'asset_sha256': row['asset_sha256'],
                'dataset_metadata': json.loads(row['metadata_json']), 'context_payload': json.loads(row['context_payload_json'])}

    def attach_asset(self, project_id: str, filename: str, raw: bytes, kind: str, label: str | None,
                     measurement_id: str | None, context_id: str | None) -> dict[str, Any]:
        asset_sha, asset_path, asset_created, already_known = self._store_asset(filename, raw)
        created_at, link_id = utc_now(), str(uuid4())
        try:
            with self.connect() as db:
                if db.execute('SELECT id FROM projects WHERE id = ?', (project_id,)).fetchone() is None:
                    raise KeyError('project_not_found')
                if measurement_id is not None and db.execute('SELECT id FROM measurements WHERE id = ? AND project_id = ?', (measurement_id, project_id)).fetchone() is None:
                    raise KeyError('measurement_not_found')
                if context_id is not None and db.execute('SELECT id FROM contexts WHERE id = ? AND project_id = ?', (context_id, project_id)).fetchone() is None:
                    raise KeyError('context_not_found')
                db.execute('INSERT OR IGNORE INTO assets(sha256, relative_path, original_filename, size_bytes, created_at) VALUES (?, ?, ?, ?, ?)',
                           (asset_sha, str(asset_path.relative_to(self.root)), filename, len(raw), created_at))
                db.execute('INSERT INTO asset_links(id, project_id, asset_sha256, measurement_id, context_id, kind, label, filename, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (link_id, project_id, asset_sha, measurement_id, context_id, kind, label, filename, created_at))
                db.commit()
        except Exception:
            if asset_created:
                asset_path.unlink(missing_ok=True)
            raise
        return {'id': link_id, 'project_id': project_id, 'asset_sha256': asset_sha, 'measurement_id': measurement_id, 'context_id': context_id,
                'kind': kind, 'label': label, 'filename': filename, 'size_bytes': len(raw), 'created_at': created_at, 'duplicate_asset': already_known}

    def list_attachments(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute('SELECT l.*, a.size_bytes FROM asset_links l JOIN assets a ON a.sha256 = l.asset_sha256 WHERE l.project_id = ? ORDER BY l.created_at DESC', (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def save_comparison(self, project_id: str, dataset_a_id: str, dataset_b_id: str, spec: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        comparison = {'id': str(uuid4()), 'created_at': utc_now()}
        with self.connect() as db:
            for dataset_id in (dataset_a_id, dataset_b_id):
                row = db.execute('SELECT m.project_id FROM datasets d JOIN measurements m ON m.id = d.measurement_id WHERE d.id = ?', (dataset_id,)).fetchone()
                if row is None or row['project_id'] != project_id:
                    raise KeyError('dataset_not_found')
            db.execute('INSERT INTO comparisons(id, project_id, dataset_a_id, dataset_b_id, spec_json, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (comparison['id'], project_id, dataset_a_id, dataset_b_id, json.dumps(spec, ensure_ascii=False, sort_keys=True),
                        json.dumps(result, ensure_ascii=False, sort_keys=True), comparison['created_at']))
            db.commit()
        return {**comparison, 'project_id': project_id, 'dataset_a_id': dataset_a_id, 'dataset_b_id': dataset_b_id, 'spec': spec, 'result': result}

    def list_comparisons(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute('SELECT * FROM comparisons WHERE project_id = ? ORDER BY created_at DESC', (project_id,)).fetchall()
        return [{'id': row['id'], 'project_id': row['project_id'], 'dataset_a_id': row['dataset_a_id'], 'dataset_b_id': row['dataset_b_id'],
                 'spec': json.loads(row['spec_json']), 'result': json.loads(row['result_json']), 'created_at': row['created_at']} for row in rows]

    def integrity_problems(self, base_root: Path | None = None, db_path: Path | None = None) -> list[str]:
        root, database_path = base_root or self.root, db_path or self.db_path
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        problems: list[str] = []
        try:
            for row in connection.execute('PRAGMA foreign_key_check').fetchall():
                problems.append(f'foreign_key:{row[0]}:{row[1]}')
            for row in connection.execute('SELECT sha256, relative_path FROM assets'):
                relative = Path(str(row['relative_path']))
                if relative.is_absolute() or '..' in relative.parts:
                    problems.append(f'unsafe_asset_path:{row["sha256"]}')
                elif not (root / relative).is_file():
                    problems.append(f'missing_asset:{row["sha256"]}:{relative.as_posix()}')
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='constraint_sets'").fetchone() is not None:
                for row in connection.execute('SELECT id, spec_json, spec_sha256 FROM constraint_sets'):
                    try:
                        spec = json.loads(row['spec_json'])
                    except (TypeError, json.JSONDecodeError):
                        problems.append(f'invalid_constraint_set_json:{row["id"]}')
                        continue
                    if canonical_json_sha256(spec) != row['spec_sha256']:
                        problems.append(f'constraint_set_hash_mismatch:{row["id"]}')
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_specs'").fetchone() is not None:
                for row in connection.execute('SELECT id, spec_json, spec_sha256 FROM search_specs'):
                    try:
                        spec = json.loads(row['spec_json'])
                    except (TypeError, json.JSONDecodeError):
                        problems.append(f'invalid_search_spec_json:{row["id"]}')
                        continue
                    if canonical_json_sha256(spec) != row['spec_sha256']:
                        problems.append(f'search_spec_hash_mismatch:{row["id"]}')
        finally:
            connection.close()
        return problems

    @staticmethod
    def decode_base64(raw_base64: str) -> bytes:
        try:
            return base64.b64decode(raw_base64, validate=True)
        except ValueError as exc:
            raise ValueError('Invalid base64 payload') from exc
