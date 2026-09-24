"""Append-only persistence for project design briefs (#555).

Every non-free-text goal ref resolves against the shared exact-authority
resolver before the brief commits — an unresolvable authority id or a
hash mismatch fails the save, so a brief can never bind a goal to an
authority that does not exist in this document.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import sqlite3
from typing import Mapping

from .cad_authority_resolver import (
    AuthorityRef,
    ExactAuthorityResolver,
    KindResolver,
)
from .cad_design_brief import ProjectDesignBrief
from .cad_repository import SceneRepository
from .cad_system_variant_repository import CadSystemVariantRepository


class DesignBriefConflictError(ValueError):
    """A brief save violated append-only identity rules."""


class CadDesignBriefRepository:
    """Native storage for ProjectDesignBrief records.

    Briefs are immutable: changing a project's goals appends a new brief
    (``supersedes_brief_id`` points back), never an UPDATE. ``latest_brief``
    reads the newest appended record without turning absence into an
    inferred configuration.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        system_variant_repository: CadSystemVariantRepository | None = None,
        kind_resolvers: Mapping[str, KindResolver] | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.resolver = ExactAuthorityResolver(
            scene_repository,
            system_variant_repository=system_variant_repository,
            kind_resolvers=kind_resolvers,
        )
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
            # Single-head lineage (#832): a document's brief history is one
            # chain — the supersede target must be its current head, so two
            # children can never fork the same parent into competing
            # "latest" briefs.
            if datetime.fromisoformat(brief.created_at_utc) < (
                datetime.fromisoformat(prior.created_at_utc)
            ):
                raise ValueError(
                    'a superseding brief cannot predate the brief it '
                    'supersedes'
                )
        for goal in brief.goal_refs:
            if goal.ref_id is None:
                continue  # free_text intent carries no authority ref
            self.resolver.resolve(
                AuthorityRef(
                    kind=goal.kind,
                    ref_id=goal.ref_id,
                    ref_sha256=goal.ref_sha256,
                ),
                document_id=brief.document_id,
            )
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if brief.supersedes_brief_id is not None and connection.execute(
                'SELECT 1 FROM cad_design_briefs WHERE supersedes_brief_id=?',
                (brief.supersedes_brief_id,),
            ).fetchone() is not None:
                raise DesignBriefConflictError(
                    'superseded brief already has a successor — the lineage '
                    'is single-head'
                )
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

    def _check_row(self, row: sqlite3.Row, brief: ProjectDesignBrief) -> None:
        """Reject a row whose indexed columns disagree with its payload (#832)."""
        if (
            row['brief_id'] != brief.brief_id
            or row['document_id'] != brief.document_id
            or row['brief_sha256'] != brief.brief_sha256
            or row['supersedes_brief_id'] != brief.supersedes_brief_id
            or row['created_at_utc'] != brief.created_at_utc
        ):
            raise ValueError(
                'persisted design brief row disagrees with its payload'
            )

    def _load_row(self, row: sqlite3.Row) -> ProjectDesignBrief:
        brief = ProjectDesignBrief.model_validate_json(row['payload_json'])
        self._check_row(row, brief)
        return brief

    def get_brief(self, brief_id: str) -> ProjectDesignBrief | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT brief_id, document_id, brief_sha256, '
                'supersedes_brief_id, created_at_utc, payload_json '
                'FROM cad_design_briefs WHERE brief_id=?',
                (brief_id,),
            ).fetchone()
        if row is None:
            return None
        return self._load_row(row)

    def list_briefs(
        self,
        document_id: str,
    ) -> tuple[ProjectDesignBrief, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT brief_id, document_id, brief_sha256,
                       supersedes_brief_id, created_at_utc, payload_json
                FROM cad_design_briefs
                WHERE document_id=?
                ORDER BY created_at_utc, brief_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._load_row(row) for row in rows)

    def latest_brief(self, document_id: str) -> ProjectDesignBrief | None:
        """The lineage head — the persisted brief nothing supersedes — or
        ``None``.

        ``latest`` follows the supersedes chain, never ``created_at_utc``:
        an out-of-order or fabricated timestamp can never promote a stale
        record over the lineage head (#832). ``None`` means NOT_CONFIGURED
        — the caller must not infer goals.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT brief_id, document_id, brief_sha256,
                       supersedes_brief_id, created_at_utc, payload_json
                FROM cad_design_briefs
                WHERE document_id=?
                  AND brief_id NOT IN (
                      SELECT supersedes_brief_id FROM cad_design_briefs
                      WHERE document_id=? AND supersedes_brief_id IS NOT NULL
                  )
                ORDER BY created_at_utc DESC, brief_id DESC
                LIMIT 1
                """,
                (document_id, document_id),
            ).fetchone()
        if row is None:
            return None
        return self._load_row(row)


__all__ = ['CadDesignBriefRepository', 'DesignBriefConflictError']
