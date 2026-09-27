"""Append-only persistence for project design briefs (#555).

Every non-free-text goal ref resolves against the shared exact-authority
resolver before the brief commits — an unresolvable authority id or a
hash mismatch fails the save, so a brief can never bind a goal to an
authority that does not exist in this document.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3
from typing import Mapping

from pydantic import ValidationError

from .cad_authority_resolver import (
    AuthorityRef,
    ExactAuthorityResolver,
    KindResolver,
)
from .cad_design_brief import (
    DesignBriefIntegrityError,
    ProjectDesignBrief,
    current_brief,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .cad_system_variant_repository import CadSystemVariantRepository


class DesignBriefConflictError(ValueError):
    """A brief save violated append-only identity rules."""


class DesignBriefStaleHeadError(ValueError):
    """The superseded brief already has a successor (#870).

    A project's brief is a single-head lineage: the losing writer revises
    the current head instead.
    """


def _instant(value: str) -> datetime:
    """Parse a validated UTC-aware persisted timestamp (#870)."""

    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(timezone.utc)


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
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_design_briefs')

    def save_brief(self, brief: ProjectDesignBrief) -> None:
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
        # BEGIN IMMEDIATE makes the successor-existence check atomic: two
        # concurrent writers cannot both observe the same head (#870).
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                'SELECT brief_sha256 FROM cad_design_briefs WHERE brief_id=?',
                (brief.brief_id,),
            ).fetchone()
            if existing is not None:
                raise DesignBriefConflictError(
                    'ProjectDesignBrief ids are append-only'
                )
            if brief.supersedes_brief_id is not None:
                prior = connection.execute(
                    'SELECT document_id, created_at_utc '
                    'FROM cad_design_briefs WHERE brief_id=?',
                    (brief.supersedes_brief_id,),
                ).fetchone()
                if prior is None:
                    raise ValueError('superseded brief is not persisted')
                if prior['document_id'] != brief.document_id:
                    raise ValueError(
                        'superseded brief belongs to another document'
                    )
                if _instant(brief.created_at_utc) < _instant(
                    prior['created_at_utc']
                ):
                    raise ValueError(
                        'superseding brief predates its predecessor'
                    )
                successor = connection.execute(
                    'SELECT brief_id FROM cad_design_briefs '
                    'WHERE supersedes_brief_id=?',
                    (brief.supersedes_brief_id,),
                ).fetchone()
                if successor is not None:
                    raise DesignBriefStaleHeadError(
                        f'brief {brief.supersedes_brief_id} is already '
                        f'superseded by {successor["brief_id"]}'
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

    def _row_to_brief(self, row: sqlite3.Row) -> ProjectDesignBrief:
        """Authoritative read: row columns, payload and goal refs must agree
        (#870).

        Goal refs are re-resolved exactly: the persisted authority pins must
        still resolve to the same semantic hash — a goal that no longer
        matches an existing authority fails the read closed instead of
        returning an unverifiable claim.
        """

        try:
            brief = ProjectDesignBrief.model_validate_json(
                row['payload_json']
            )
        except ValidationError as exc:
            raise DesignBriefIntegrityError(
                f'design brief payload corrupt: {row["brief_id"]}'
            ) from exc
        if (
            brief.brief_id != row['brief_id']
            or brief.document_id != row['document_id']
            or brief.brief_sha256 != row['brief_sha256']
            or brief.supersedes_brief_id != row['supersedes_brief_id']
            or brief.created_at_utc != row['created_at_utc']
        ):
            raise DesignBriefIntegrityError(
                f'design brief row/payload mismatch: {row["brief_id"]}'
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
        return brief

    def get_brief(self, brief_id: str) -> ProjectDesignBrief | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_design_briefs WHERE brief_id=?',
                (brief_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_brief(row)

    def list_briefs(
        self,
        document_id: str,
    ) -> tuple[ProjectDesignBrief, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cad_design_briefs
                WHERE document_id=?
                ORDER BY created_at_utc, brief_id
                """,
                (document_id,),
            ).fetchall()
        briefs = tuple(self._row_to_brief(row) for row in rows)
        # Validate the whole supersession topology even though the caller
        # asked for history: a corrupt chain must fail closed (#870).
        current_brief(briefs)
        return briefs

    def latest_brief(self, document_id: str) -> ProjectDesignBrief | None:
        """The unique head of the document's supersession chain, or ``None``.

        The current brief is derived from topology, not timestamp order —
        persisted forks or ambiguous heads raise
        :class:`DesignBriefIntegrityError` rather than picking a winner
        (#870). ``None`` means NOT_CONFIGURED — the caller must not infer
        goals.
        """

        return current_brief(self.list_briefs(document_id))


__all__ = [
    'CadDesignBriefRepository',
    'DesignBriefConflictError',
    'DesignBriefIntegrityError',
    'DesignBriefStaleHeadError',
]
