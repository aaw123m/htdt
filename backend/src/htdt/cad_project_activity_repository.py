"""Project activity note persistence (#615).

Timeline events are a pure projection and need no table — but user milestone
notes ARE persisted records. They are append-only documentation rows that can
never serve as measured/as-built evidence.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .cad_project_activity import ProjectActivityNote
from .cad_repository import SceneRepository


class CadProjectActivityNoteRepository:
    """Append-only store for user milestone notes in the project timeline."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.database_path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_project_notes (
                    note_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    note_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_project_notes_document
                ON cad_project_notes(document_id)
                """
            )

    def save_note(self, note: ProjectActivityNote) -> ProjectActivityNote:
        self._validate(note)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_project_notes(
                    note_id, document_id, note_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    note.note_id,
                    note.document_id,
                    note.note_sha256,
                    note.created_at_utc,
                    note.model_dump_json(),
                ),
            )
        return note

    def get_note(self, note_id: str) -> ProjectActivityNote | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_project_notes WHERE note_id=?
                """,
                (note_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            ProjectActivityNote.model_validate_json(row['payload_json'])
        )

    def list_notes(self, document_id: str) -> tuple[ProjectActivityNote, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_project_notes
                WHERE document_id=? ORDER BY created_at_utc, note_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            self._validate(
                ProjectActivityNote.model_validate_json(row['payload_json'])
            )
            for row in rows
        )

    def _validate(self, note: ProjectActivityNote) -> ProjectActivityNote:
        if self.scene_repository.current_head(note.document_id) is None:
            raise ValueError(
                f'activity note {note.note_id} references an unknown document'
            )
        return note
