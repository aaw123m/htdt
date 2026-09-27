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
from .cad_schema import connect_sqlite, require_native_tables


class CadProjectActivityNoteRepository:
    """Append-only store for user milestone notes in the project timeline."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.database_path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.database_path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_project_notes',
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
