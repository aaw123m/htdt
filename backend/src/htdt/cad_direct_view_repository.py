"""Append-only persistence for the direct-view display authority (#637)."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from .cad_direct_view import (
    DirectViewDisplaySpecification,
    DirectViewGeometryEvaluation,
    evaluate_direct_view_geometry,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .cad_system_variant_repository import CadSystemVariantRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DirectViewConflictError(ValueError):
    """A direct-view save violated append-only identity rules."""


class CadDirectViewRepository:
    """Native storage for display specifications and geometry evaluations."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

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

    def _specification_for(
        self,
        evaluation: DirectViewGeometryEvaluation,
    ) -> DirectViewDisplaySpecification | None:
        """Resolve the evaluation's spec pin (id + version + hash) (#1051)."""
        spec_sha256 = evaluation.display_specification_sha256
        request = evaluation.request
        if spec_sha256 is None:
            if request.display_specification_id is not None:
                raise ValueError(
                    'direct-view request pins a specification the '
                    'evaluation does not record'
                )
            return None
        specification = self.get_specification_by_hash(spec_sha256)
        if specification is None:
            raise ValueError(
                'direct-view geometry evaluation references an '
                'unpersisted display specification'
            )
        if (
            request.display_specification_id
            != specification.specification_id
            or request.display_specification_version
            != specification.version
            or request.display_specification_sha256
            != specification.specification_sha256
        ):
            raise ValueError(
                'direct-view request display-specification binding mismatch'
            )
        return specification

    def _reproduce_evaluation(
        self,
        evaluation: DirectViewGeometryEvaluation,
        specification: DirectViewDisplaySpecification | None,
    ) -> DirectViewGeometryEvaluation:
        """Resolve pinned authorities and replay the canonical evaluator.

        A persisted evaluation is only authoritative when the SceneRevision
        (id + content hash), the optional SystemVariant (id + hash +
        baseline pins) and the optional display specification all resolve
        and the canonical evaluator reproduces the payload exactly
        (#1051).
        """
        target = evaluation.target
        baseline = self.scene_repository.get(target.scene_revision_id)
        if baseline is None:
            raise ValueError('direct-view geometry SceneRevision does not exist')
        if (
            baseline.document_id != target.document_id
            or baseline.content_hash != target.scene_content_hash
        ):
            raise ValueError('direct-view geometry SceneRevision authority mismatch')

        variant = None
        if target.system_variant_id is not None:
            if self.variant_repository is None:
                raise ValueError(
                    'SystemVariant-bound direct-view evaluation requires '
                    'variant repository'
                )
            variant = self.variant_repository.get_variant(
                target.system_variant_id
            )
            if variant is None:
                raise ValueError(
                    'direct-view geometry SystemVariant does not exist'
                )
            if variant.variant_sha256 != target.system_variant_sha256:
                raise ValueError(
                    'direct-view geometry SystemVariant hash mismatch'
                )
            if (
                variant.document_id != target.document_id
                or variant.baseline_revision_id != target.scene_revision_id
                or variant.baseline_content_hash != target.scene_content_hash
            ):
                raise ValueError(
                    'direct-view geometry SystemVariant baseline mismatch'
                )

        reproduced = evaluate_direct_view_geometry(
            baseline=baseline,
            variant=variant,
            display_specification=specification,
            request=evaluation.request,
        )
        if reproduced != evaluation:
            raise ValueError(
                'direct-view geometry evaluation is not reproducible from '
                'exact persisted authority'
            )
        return reproduced

    def _replay_persisted_evaluation(
        self,
        row: sqlite3.Row,
    ) -> DirectViewGeometryEvaluation:
        """Cross-check indexed columns against the payload, then replay."""
        evaluation = DirectViewGeometryEvaluation.model_validate_json(
            row['payload_json']
        )
        target = evaluation.target
        if (
            row['evaluation_id'] != evaluation.evaluation_id
            or row['evaluation_sha256'] != evaluation.evaluation_sha256
            or row['document_id'] != target.document_id
            or row['scene_revision_id'] != target.scene_revision_id
            or row['system_variant_id'] != target.system_variant_id
            or row['request_sha256'] != evaluation.request.request_sha256
            or row['geometry_status'] != evaluation.geometry_status
        ):
            raise ValueError(
                'direct-view geometry evaluation row does not match its '
                'payload'
            )
        return self._reproduce_evaluation(
            evaluation,
            self._specification_for(evaluation),
        )

    def save_evaluation(
        self,
        evaluation: DirectViewGeometryEvaluation,
    ) -> DirectViewGeometryEvaluation:
        evaluation = DirectViewGeometryEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        reproduced = self._reproduce_evaluation(
            evaluation,
            self._specification_for(evaluation),
        )

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_view_evaluations
                WHERE evaluation_id=?
                """,
                (reproduced.evaluation_id,),
            ).fetchone()
            if existing is not None:
                persisted = DirectViewGeometryEvaluation.model_validate_json(
                    existing['payload_json']
                )
                if persisted != reproduced:
                    raise DirectViewConflictError(
                        'direct-view geometry evaluation id already exists '
                        'with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_direct_view_evaluations (
                    evaluation_id, document_id, scene_revision_id,
                    system_variant_id, request_sha256, geometry_status,
                    evaluation_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reproduced.evaluation_id,
                    reproduced.target.document_id,
                    reproduced.target.scene_revision_id,
                    reproduced.target.system_variant_id,
                    reproduced.request.request_sha256,
                    reproduced.geometry_status,
                    reproduced.evaluation_sha256,
                    _utc_now(),
                    reproduced.model_dump_json(),
                ),
            )
        return reproduced

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> DirectViewGeometryEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM cad_direct_view_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._replay_persisted_evaluation(row)

    def list_evaluations_for_revision(
        self,
        scene_revision_id: str,
    ) -> tuple[DirectViewGeometryEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cad_direct_view_evaluations
                WHERE scene_revision_id=?
                ORDER BY created_at_utc, evaluation_id
                """,
                (scene_revision_id,),
            ).fetchall()
        return tuple(
            self._replay_persisted_evaluation(row)
            for row in rows
        )
