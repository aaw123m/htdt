"""Append-only persistence for native field explorer sessions (#953).

Sessions are immutable authorities: ``save`` writes the sealed
:class:`FieldExplorerSession` payload once (same id + same hash), and every
read re-validates the stored payload through the pydantic contract — a row
whose bytes were rewritten fails closed on the semantic hash before it can
reach an explorer view.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .cad_field_explorer import FieldExplorerSession
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .canonical_json import canonical_json
from .clock import utc_now_iso as _utc_now


class FieldExplorerConflictError(ValueError):
    """A session save violated append-only identity rules."""


class CadFieldExplorerRepository:
    """Native storage for FieldExplorerSession records."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_field_explorer_sessions')

    def _source_revision(self, session: FieldExplorerSession) -> None:
        source = self.scene_repository.get(session.scene_revision_id)
        if source is None:
            raise ValueError('field explorer source revision does not exist')
        if source.document_id != session.document_id:
            raise ValueError(
                'field explorer session belongs to another document'
            )
        if source.content_hash != session.scene_content_hash:
            raise ValueError(
                'field explorer session content hash does not match revision'
            )

    def save(self, session: FieldExplorerSession) -> FieldExplorerSession:
        """Persist one sealed session; identical re-saves are idempotent."""

        self._source_revision(session)
        payload = canonical_json(session.model_dump(mode='json'))
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                'SELECT session_id, semantic_sha256 FROM '
                'cad_field_explorer_sessions WHERE session_id = ?',
                (session.session_id,),
            ).fetchone()
            if existing is not None:
                if existing['semantic_sha256'] != session.semantic_sha256:
                    raise FieldExplorerConflictError(
                        'field explorer session id was reused for a '
                        'different payload'
                    )
                return session
            connection.execute(
                '''
                INSERT INTO cad_field_explorer_sessions(
                    session_id, semantic_sha256, document_id,
                    scene_revision_id, prediction_run_id,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    session.session_id,
                    session.semantic_sha256,
                    session.document_id,
                    session.scene_revision_id,
                    session.prediction_run_id,
                    payload,
                    _utc_now(),
                ),
            )
        return session

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> FieldExplorerSession:
        # model_validate replays the sealed semantic-hash/id validators, so a
        # tampered payload fails closed on read.
        return FieldExplorerSession.model_validate(
            json.loads(row['payload_json'])
        )

    def get(self, session_id: str) -> FieldExplorerSession | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_field_explorer_sessions '
                'WHERE session_id = ?',
                (session_id,),
            ).fetchone()
        return None if row is None else self._row_to_session(row)

    def list_sessions(
        self, document_id: str
    ) -> tuple[FieldExplorerSession, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_field_explorer_sessions '
                'WHERE document_id = ? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_session(row) for row in rows)

    def list_for_run(
        self, prediction_run_id: str
    ) -> tuple[FieldExplorerSession, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_field_explorer_sessions '
                'WHERE prediction_run_id = ? ORDER BY seq ASC',
                (prediction_run_id,),
            ).fetchall()
        return tuple(self._row_to_session(row) for row in rows)
