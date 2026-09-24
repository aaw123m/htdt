"""Append-only persistence for project design briefs (#555)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_design_brief import ProjectDesignBrief
from .cad_repository import SceneRepository


class DesignBriefConflictError(ValueError):
    """A brief save violated append-only identity rules."""


class CadDesignBriefRepository:
    """Native storage for ProjectDesignBrief records.

    Briefs are immutable: changing a project's goals appends a new brief
    (``supersedes_brief_id`` points back), never an UPDATE. ``latest_brief``
    reads the newest appended record without turning absence into an
    inferred configuration.
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_design_briefs (
                    brief_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    brief_sha256 TEXT NOT NULL,
                    supersedes_brief_id TEXT,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_design_briefs_document
                ON cad_design_briefs (document_id, created_at_utc)
                """
            )

    def save_brief(self, brief: ProjectDesignBrief) -> None:
        if self.get_brief(brief.brief_id) is not None:
            raise DesignBriefConflictError(
                'ProjectDesignBrief ids are append-only'
            )
        if brief.supersedes_brief_id is not None:
            prior = self.get_brief(brief.supersedes_brief_id)
            if prior is None:
                raise ValueError('superseded brief is not persisted')
            if prior.document_id != brief.document_id:
                raise ValueError('superseded brief belongs to another document')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_design_briefs (
                    brief_id, document_id, brief_sha256, supersedes_brief_id,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    brief.brief_id,
                    brief.document_id,
                    brief.brief_sha256,
                    brief.supersedes_brief_id,
                    brief.created_at_utc,
                    brief.model_dump_json(),
                ),
            )

    def get_brief(self, brief_id: str) -> ProjectDesignBrief | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_design_briefs WHERE brief_id=?',
                (brief_id,),
            ).fetchone()
        if row is None:
            return None
        return ProjectDesignBrief.model_validate_json(row['payload_json'])

    def list_briefs(
        self,
        document_id: str,
    ) -> tuple[ProjectDesignBrief, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_design_briefs
                WHERE document_id=?
                ORDER BY created_at_utc, brief_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            ProjectDesignBrief.model_validate_json(row['payload_json'])
            for row in rows
        )

    def latest_brief(self, document_id: str) -> ProjectDesignBrief | None:
        """Newest appended brief for the document, or ``None``.

        ``None`` means NOT_CONFIGURED — the caller must not infer goals.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_design_briefs
                WHERE document_id=?
                ORDER BY created_at_utc DESC, brief_id DESC
                LIMIT 1
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        return ProjectDesignBrief.model_validate_json(row['payload_json'])


__all__ = ['CadDesignBriefRepository', 'DesignBriefConflictError']
