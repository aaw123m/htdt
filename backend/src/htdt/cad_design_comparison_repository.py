"""Append-only persistence for design comparison sets (#447)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_design_comparison import DesignComparisonSet
from .cad_repository import SceneRepository


class DesignComparisonConflictError(ValueError):
    """A comparison-set save violated append-only identity rules."""


class CadDesignComparisonRepository:
    """Native storage for immutable DesignComparisonSet rows.

    Each save is a new immutable row; set evolution is expressed through
    ``supersedes_set_id`` chains so an opened historical set keeps its exact
    alternatives forever — including after Undo history is cleared.
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
                CREATE TABLE IF NOT EXISTS cad_design_comparison_sets (
                    set_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    supersedes_set_id TEXT,
                    set_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def save_set(self, comparison_set: DesignComparisonSet) -> None:
        if self.get_set(comparison_set.set_id) is not None:
            raise DesignComparisonConflictError(
                'DesignComparisonSet ids are append-only'
            )
        if comparison_set.supersedes_set_id is not None:
            previous = self.get_set(comparison_set.supersedes_set_id)
            if previous is None:
                raise ValueError('superseded comparison set is not persisted')
            if previous.document_id != comparison_set.document_id:
                raise ValueError('superseded comparison set belongs to another document')
            if previous.revision + 1 != comparison_set.revision:
                raise ValueError('comparison set revision must continue the supersede chain')
        for alternative in comparison_set.alternatives:
            revision = self.scene_repository.get(alternative.scene_revision_id)
            if revision is None:
                raise ValueError(
                    f'alternative {alternative.label} pins a SceneRevision that is not persisted'
                )
            if revision.content_hash != alternative.scene_content_hash:
                raise ValueError(
                    f'alternative {alternative.label} SceneRevision content hash mismatch'
                )
            if revision.document_id != comparison_set.document_id:
                raise ValueError(
                    f'alternative {alternative.label} pins a SceneRevision of another document'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_design_comparison_sets (
                    set_id, document_id, revision, supersedes_set_id,
                    set_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comparison_set.set_id,
                    comparison_set.document_id,
                    comparison_set.revision,
                    comparison_set.supersedes_set_id,
                    comparison_set.set_sha256,
                    comparison_set.created_at_utc,
                    comparison_set.model_dump_json(),
                ),
            )

    def get_set(self, set_id: str) -> DesignComparisonSet | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_design_comparison_sets WHERE set_id=?',
                (set_id,),
            ).fetchone()
        if row is None:
            return None
        return DesignComparisonSet.model_validate_json(row['payload_json'])

    def list_sets(
        self,
        document_id: str,
    ) -> tuple[DesignComparisonSet, ...]:
        """All saved sets, oldest first — including superseded revisions."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_design_comparison_sets
                WHERE document_id=?
                ORDER BY created_at_utc, set_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            DesignComparisonSet.model_validate_json(row['payload_json'])
            for row in rows
        )

    def latest_sets(
        self,
        document_id: str,
    ) -> tuple[DesignComparisonSet, ...]:
        """Newest revision of each supersede chain, oldest chain first."""
        all_sets = self.list_sets(document_id)
        superseded = {item.supersedes_set_id for item in all_sets}
        return tuple(item for item in all_sets if item.set_id not in superseded)
