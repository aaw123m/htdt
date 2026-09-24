"""SQLite store for :class:`ProjectActionItem` (#667).

Shares the SceneRepository database so action items and spatial notes stay
inside the project file and travel with backup/export. Items are workflow
metadata, not authority: ``save`` upserts by ``action_id`` (status changes
are expected), while the content hash keeps each record self-verifying.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .cad_action_item import ProjectActionItem


class ActionItemConflictError(ValueError):
    """Persisted item exists for another document — never silently rebound."""


class CadActionItemRepository:
    """Per-document action items, ordered by creation."""

    def __init__(self, scene_repository) -> None:
        self.path = Path(scene_repository.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_action_items (
                action_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                action_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_project_action_items_document
            ON project_action_items(document_id)
            """
        )
        return connection

    def save(self, item: ProjectActionItem) -> ProjectActionItem:
        with closing(self._connect()) as connection:
            existing = connection.execute(
                'SELECT document_id FROM project_action_items WHERE action_id = ?',
                (item.action_id,),
            ).fetchone()
            if existing is not None and existing['document_id'] != item.document_id:
                raise ActionItemConflictError(
                    f'action item {item.action_id} belongs to another document'
                )
            with connection:
                connection.execute(
                    """
                    INSERT INTO project_action_items (
                        action_id, document_id, status, priority,
                        created_at_utc, updated_at_utc, archived,
                        action_sha256, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(action_id) DO UPDATE SET
                        status = excluded.status,
                        priority = excluded.priority,
                        updated_at_utc = excluded.updated_at_utc,
                        archived = excluded.archived,
                        action_sha256 = excluded.action_sha256,
                        payload_json = excluded.payload_json
                    """,
                    (
                        item.action_id,
                        item.document_id,
                        item.status,
                        item.priority,
                        item.created_at_utc,
                        item.updated_at_utc,
                        1 if item.archived else 0,
                        item.action_sha256,
                        json.dumps(
                            item.model_dump(mode='json'), ensure_ascii=False
                        ),
                    ),
                )
        return item

    def get(self, action_id: str) -> ProjectActionItem | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM project_action_items WHERE action_id = ?',
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        return ProjectActionItem.model_validate_json(row['payload_json'])

    def list_actions(
        self,
        document_id: str,
        *,
        status: str | None = None,
        include_archived: bool = False,
    ) -> tuple[ProjectActionItem, ...]:
        clauses = ['document_id = ?']
        params: list[object] = [document_id]
        if status is not None:
            clauses.append('status = ?')
            params.append(status)
        if not include_archived:
            clauses.append('archived = 0')
        query = (
            'SELECT payload_json FROM project_action_items WHERE '
            + ' AND '.join(clauses)
            + ' ORDER BY created_at_utc ASC, action_id ASC'
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            ProjectActionItem.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'ActionItemConflictError',
    'CadActionItemRepository',
]
