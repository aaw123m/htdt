from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .cad_constraint_models import CadConstraintSet
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
)


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
            require_native_tables(connection, 'cad_constraint_workspaces')

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
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            self.save_in_transaction(
                connection, constraint_set, updated_at_utc=updated_at
            )

    def save_in_transaction(
        self,
        connection: sqlite3.Connection,
        constraint_set: CadConstraintSet,
        *,
        updated_at_utc: str,
    ) -> None:
        """Write one workspace generation inside the caller's transaction.

        The caller owns BEGIN/COMMIT/ROLLBACK — this is the write path for
        higher-level operations (e.g. checkpoint restore) that must commit
        the workspace together with other domain mutations atomically.
        """
        payload = json.dumps(
            constraint_set.model_dump(mode='json'),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
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
                updated_at_utc,
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
