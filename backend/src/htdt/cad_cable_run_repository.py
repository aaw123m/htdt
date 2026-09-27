"""Append-only persistence for cable runs (#538)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_cable_run import CableRun
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite


class CableRunConflictError(ValueError):
    """A cable-run save violated append-only identity rules."""


class CadCableRunRepository:
    """Native storage for versioned CableRun records.

    ``(run_id, version)`` is saved exactly once: changing a route appends a
    new version row, never an UPDATE. Every save re-checks the pinned
    SceneRevision exists with the recorded content hash.
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_cable_runs')

    def save_run(self, run: CableRun) -> None:
        if self.get_run(run.run_id, run.version) is not None:
            raise CableRunConflictError(
                'CableRun (run_id, version) is append-only'
            )
        revision = self.scene_repository.get(run.scene_revision_id)
        if revision is None:
            raise ValueError('cable run pins a SceneRevision that is not persisted')
        if revision.content_hash != run.scene_content_hash:
            raise ValueError('cable run SceneRevision content hash mismatch')
        if revision.document_id != run.document_id:
            raise ValueError('cable run SceneRevision belongs to another document')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_cable_runs (
                    run_id, version, document_id, scene_revision_id,
                    scene_content_hash, kind, total_length_m,
                    semantic_sha256, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.version,
                    run.document_id,
                    run.scene_revision_id,
                    run.scene_content_hash,
                    run.kind,
                    run.total_length_m,
                    run.semantic_sha256,
                    run.model_dump_json(),
                    run.created_at_utc,
                ),
            )

    def get_run(self, run_id: str, version: str) -> CableRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_cable_runs
                WHERE run_id=? AND version=?
                """,
                (run_id, version),
            ).fetchone()
        if row is None:
            return None
        return CableRun.model_validate_json(row['payload_json'])

    def get_run_by_hash(self, semantic_sha256: str) -> CableRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_cable_runs
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return CableRun.model_validate_json(row['payload_json'])

    def list_runs(self, document_id: str) -> tuple[CableRun, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_cable_runs
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            CableRun.model_validate_json(row['payload_json'])
            for row in rows
        )

    def list_run_versions(self, run_id: str) -> tuple[CableRun, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_cable_runs
                WHERE run_id=?
                ORDER BY seq ASC
                """,
                (run_id,),
            ).fetchall()
        return tuple(
            CableRun.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'CadCableRunRepository',
    'CableRunConflictError',
]
