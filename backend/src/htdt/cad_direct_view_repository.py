"""Append-only persistence for the direct-view display authority (#637)."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from .cad_direct_view import (
    DirectViewDisplaySpecification,
    DirectViewGeometryEvaluation,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DirectViewConflictError(ValueError):
    """A direct-view save violated append-only identity rules."""


class CadDirectViewRepository:
    """Native storage for display specifications and geometry evaluations."""

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
            require_native_tables(connection, 'cad_direct_view_specifications', 'cad_direct_view_evaluations')

    # ------------------------------------------------------------------
    # Display specifications

    def save_specification(
        self,
        specification: DirectViewDisplaySpecification,
    ) -> None:
        existing = self.get_specification(
            specification.specification_id, specification.version
        )
        if existing is not None:
            if existing.specification_sha256 == specification.specification_sha256:
                return
            raise DirectViewConflictError(
                'direct-view display specification (id, version) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_direct_view_specifications (
                    specification_id, version, specification_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    specification.specification_id,
                    specification.version,
                    specification.specification_sha256,
                    _utc_now(),
                    specification.model_dump_json(),
                ),
            )

    def get_specification(
        self,
        specification_id: str,
        version: str,
    ) -> DirectViewDisplaySpecification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_view_specifications
                WHERE specification_id=? AND version=?
                """,
                (specification_id, version),
            ).fetchone()
        if row is None:
            return None
        return DirectViewDisplaySpecification.model_validate_json(
            row['payload_json']
        )

    def get_specification_by_hash(
        self,
        specification_sha256: str,
    ) -> DirectViewDisplaySpecification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_view_specifications
                WHERE specification_sha256=?
                """,
                (specification_sha256,),
            ).fetchone()
        if row is None:
            return None
        return DirectViewDisplaySpecification.model_validate_json(
            row['payload_json']
        )

    def list_specifications(self) -> tuple[DirectViewDisplaySpecification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_view_specifications
                ORDER BY created_at_utc, specification_id, version
                """,
            ).fetchall()
        return tuple(
            DirectViewDisplaySpecification.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Geometry evaluations

    def save_evaluation(
        self,
        evaluation: DirectViewGeometryEvaluation,
    ) -> None:
        if self.get_evaluation(evaluation.evaluation_id) is not None:
            raise DirectViewConflictError(
                'direct-view geometry evaluation ids are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_direct_view_evaluations (
                    evaluation_id, document_id, scene_revision_id,
                    system_variant_id, request_sha256, geometry_status,
                    evaluation_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.target.document_id,
                    evaluation.target.scene_revision_id,
                    evaluation.target.system_variant_id,
                    evaluation.request.request_sha256,
                    evaluation.geometry_status,
                    evaluation.evaluation_sha256,
                    _utc_now(),
                    evaluation.model_dump_json(),
                ),
            )

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> DirectViewGeometryEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_view_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return DirectViewGeometryEvaluation.model_validate_json(row['payload_json'])

    def list_evaluations_for_revision(
        self,
        scene_revision_id: str,
    ) -> tuple[DirectViewGeometryEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_view_evaluations
                WHERE scene_revision_id=?
                ORDER BY created_at_utc, evaluation_id
                """,
                (scene_revision_id,),
            ).fetchall()
        return tuple(
            DirectViewGeometryEvaluation.model_validate_json(row['payload_json'])
            for row in rows
        )
