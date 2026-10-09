"""Append-only persistence for cable-run route geometry (#1011, M2)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_cable_run_repository import CadCableRunRepository
from .cad_cable_run_geometry import CableRunGeometry
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite


class CableRunGeometryConflictError(ValueError):
    """A geometry save violated append-only identity rules."""


class CadCableRunGeometryRepository:
    """Native storage for versioned CableRunGeometry records.

    ``(geometry_id, version)`` is saved exactly once: revising a recorded
    route appends a new geometry version, never an UPDATE. Every save
    re-resolves the pinned CableRun and re-checks every declared segment
    sequence, so a geometry row can never outlive the record it
    describes as if it were current.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        cable_run_repository: CadCableRunRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.cable_run_repository = (
            cable_run_repository
            if cable_run_repository is not None
            else CadCableRunRepository(scene_repository)
        )
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_cable_run_geometries')

    def save_geometry(self, geometry: CableRunGeometry) -> None:
        if (
            self.get_geometry(geometry.geometry_id, geometry.version)
            is not None
        ):
            raise CableRunGeometryConflictError(
                'CableRunGeometry (geometry_id, version) is append-only'
            )
        run = self.cable_run_repository.get_run(
            geometry.run_id, geometry.run_version
        )
        if run is None:
            raise ValueError(
                'cable run geometry pins a CableRun that is not persisted'
            )
        if run.semantic_sha256 != geometry.run_semantic_sha256:
            raise ValueError(
                'cable run geometry pins a different CableRun record'
            )
        if run.document_id != geometry.document_id:
            raise ValueError(
                'cable run geometry belongs to another document'
            )
        if (
            run.scene_revision_id != geometry.scene_revision_id
            or run.scene_content_hash != geometry.scene_content_hash
        ):
            raise ValueError(
                'cable run geometry and CableRun pin different revisions'
            )
        declared = {segment.sequence for segment in run.segments}
        unknown = [
            segment.segment_sequence
            for segment in geometry.segment_geometries
            if segment.segment_sequence not in declared
        ]
        if unknown:
            raise ValueError(
                'cable run geometry registers sequences the CableRun '
                'never declared: '
                + ', '.join(str(item) for item in unknown)
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_cable_run_geometries (
                    geometry_id, version, document_id, run_id, run_version,
                    run_semantic_sha256, geometric_length_m,
                    semantic_sha256, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    geometry.geometry_id,
                    geometry.version,
                    geometry.document_id,
                    geometry.run_id,
                    geometry.run_version,
                    geometry.run_semantic_sha256,
                    geometry.geometric_length_m,
                    geometry.semantic_sha256,
                    geometry.model_dump_json(),
                    geometry.created_at_utc,
                ),
            )

    def get_geometry(
        self, geometry_id: str, version: str
    ) -> CableRunGeometry | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_cable_run_geometries
                WHERE geometry_id=? AND version=?
                """,
                (geometry_id, version),
            ).fetchone()
        if row is None:
            return None
        return CableRunGeometry.model_validate_json(row['payload_json'])

    def get_geometry_by_hash(
        self, semantic_sha256: str
    ) -> CableRunGeometry | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_cable_run_geometries
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return CableRunGeometry.model_validate_json(row['payload_json'])

    def list_geometries(
        self, document_id: str
    ) -> tuple[CableRunGeometry, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_cable_run_geometries
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            CableRunGeometry.model_validate_json(row['payload_json'])
            for row in rows
        )

    def list_geometry_versions(
        self, geometry_id: str
    ) -> tuple[CableRunGeometry, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_cable_run_geometries
                WHERE geometry_id=?
                ORDER BY seq ASC
                """,
                (geometry_id,),
            ).fetchall()
        return tuple(
            CableRunGeometry.model_validate_json(row['payload_json'])
            for row in rows
        )

    def latest_geometry_for_run(
        self, run_id: str, run_version: str
    ) -> CableRunGeometry | None:
        """Latest geometry recorded against one exact run version."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_cable_run_geometries
                WHERE run_id=? AND run_version=?
                ORDER BY seq DESC
                LIMIT 1
                """,
                (run_id, run_version),
            ).fetchone()
        if row is None:
            return None
        return CableRunGeometry.model_validate_json(row['payload_json'])


__all__ = [
    'CadCableRunGeometryRepository',
    'CableRunGeometryConflictError',
]
