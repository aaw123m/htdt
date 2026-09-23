from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .cad_constraint_models import CadConstraintSet
from .cad_schema import ensure_native_schema


class CadConstraintRepository:
    """Persist document-scoped native constraint definitions beside CAD scene data."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS cad_constraint_workspaces (
                    document_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                '''
            )

    def load(self, document_id: str) -> CadConstraintSet:
        if not document_id:
            raise ValueError('document_id must not be empty')
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_constraint_workspaces WHERE document_id=?',
                (document_id,),
            ).fetchone()
        if row is None:
            return CadConstraintSet(document_id=document_id)
        return CadConstraintSet.model_validate(json.loads(str(row['payload_json'])))

    def save(self, constraint_set: CadConstraintSet) -> None:
        payload = json.dumps(
            constraint_set.model_dump(mode='json'),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''
                INSERT INTO cad_constraint_workspaces(document_id, schema_version, updated_at_utc, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    schema_version=excluded.schema_version,
                    updated_at_utc=excluded.updated_at_utc,
                    payload_json=excluded.payload_json
                ''',
                (
                    constraint_set.document_id,
                    constraint_set.schema_version,
                    updated_at,
                    payload,
                ),
            )

    def delete(self, document_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                'DELETE FROM cad_constraint_workspaces WHERE document_id=?',
                (document_id,),
            )
        return cursor.rowcount > 0
