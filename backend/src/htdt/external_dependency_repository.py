"""Append-only persistence for external authority dependencies (#600).

Resolution events are never caller-shaped: the only write path is
:meth:`ExternalDependencyRepository.resolve_and_record`, which reloads the
stored dependency, runs the canonical resolver over the supplied
resolution context and persists the event the resolver derived. A forged
outcome — ``resolved_exact_*`` with a hash that does not satisfy the
dependency pin — cannot be recorded because no event is ever accepted
from outside.

The *current* resolution is the most recently recorded event — insertion
sequence, not the caller-supplied ``created_at_utc`` inside the event.
A forged or future-dated timestamp label can never move an older event
ahead of a later resolution.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .external_dependency_resolver import (
    DependencyResolutionContext,
    DependencyResolutionEvent,
    ExternalAuthorityDependency,
    resolve_external_dependency,
)


class ExternalDependencyConflictError(ValueError):
    """A dependency or resolution-event save violated identity rules."""


class ExternalDependencyRepository:
    """Native storage for dependencies and their resolution events.

    Both records are append-only: a dependency pins *what* a project needs,
    and each resolution event records *which exact authority* was consumed at
    a point in time — re-resolution appends a new event rather than updating.
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
                CREATE TABLE IF NOT EXISTS cad_external_dependencies (
                    dependency_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    authority_ref TEXT NOT NULL,
                    dependency_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_external_dependencies_ref
                ON cad_external_dependencies (document_id, kind, authority_ref)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_dependency_resolution_events (
                    event_id TEXT PRIMARY KEY,
                    dependency_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    resolved_sha256 TEXT,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (dependency_id)
                        REFERENCES cad_external_dependencies (dependency_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_resolution_events_dependency
                ON cad_dependency_resolution_events (dependency_id, created_at_utc)
                """
            )

    def save_dependency(
        self, dependency: ExternalAuthorityDependency
    ) -> None:
        if self.get_dependency(dependency.dependency_id) is not None:
            raise ExternalDependencyConflictError(
                'ExternalAuthorityDependency ids are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_external_dependencies (
                    dependency_id, document_id, kind, authority_ref,
                    dependency_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dependency.dependency_id,
                    dependency.document_id,
                    dependency.kind,
                    dependency.authority_ref,
                    dependency.dependency_sha256,
                    dependency.created_at_utc,
                    dependency.model_dump_json(),
                ),
            )

    def get_dependency(
        self, dependency_id: str
    ) -> ExternalAuthorityDependency | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_external_dependencies
                WHERE dependency_id=?
                """,
                (dependency_id,),
            ).fetchone()
        if row is None:
            return None
        return ExternalAuthorityDependency.model_validate_json(
            row['payload_json']
        )

    def list_dependencies(
        self, document_id: str
    ) -> tuple[ExternalAuthorityDependency, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_external_dependencies
                WHERE document_id=?
                ORDER BY created_at_utc, dependency_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            ExternalAuthorityDependency.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def resolve_and_record(
        self,
        dependency_id: str,
        context: DependencyResolutionContext,
        *,
        resolved_at_utc: str,
        event_id: str | None = None,
    ) -> DependencyResolutionEvent:
        """Resolve a stored dependency and persist the derived event.

        The dependency is reloaded from this repository and the event is
        produced by the canonical resolver — the stored event's
        ``document_id``, outcome, ``resolved_via`` and ``resolved_sha256``
        are therefore always the resolver's own output tied to the stored
        dependency, never caller-forged.
        """

        dependency = self.get_dependency(dependency_id)
        if dependency is None:
            raise ValueError('resolution event references unknown dependency')
        event = resolve_external_dependency(
            dependency,
            context,
            resolved_at_utc=resolved_at_utc,
            event_id=event_id,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dependency_resolution_events (
                    event_id, dependency_id, document_id, outcome,
                    resolved_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.dependency_id,
                    event.document_id,
                    event.outcome,
                    event.resolved_sha256,
                    event.created_at_utc,
                    event.model_dump_json(),
                ),
            )
        return event

    def list_resolutions(
        self, dependency_id: str
    ) -> tuple[DependencyResolutionEvent, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_dependency_resolution_events
                WHERE dependency_id=?
                ORDER BY rowid
                """,
                (dependency_id,),
            ).fetchall()
        return tuple(
            DependencyResolutionEvent.model_validate_json(row['payload_json'])
            for row in rows
        )

    def latest_resolution(
        self, dependency_id: str
    ) -> DependencyResolutionEvent | None:
        """The current resolution: the most recently recorded event.

        Ordering is the table's insertion sequence, not the event's
        caller-declared ``created_at_utc`` — since every row enters through
        :meth:`resolve_and_record`, append order is the authoritative
        resolution history and a future-dated label cannot hijack it.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_dependency_resolution_events
                WHERE dependency_id=?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (dependency_id,),
            ).fetchone()
        if row is None:
            return None
        return DependencyResolutionEvent.model_validate_json(
            row['payload_json']
        )


__all__ = ['ExternalDependencyConflictError', 'ExternalDependencyRepository']
