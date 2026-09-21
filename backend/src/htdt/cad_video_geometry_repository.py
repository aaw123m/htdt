from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_video_geometry import (
    ProjectorSpecification,
    VideoGeometryEvaluation,
    evaluate_video_geometry,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadVideoGeometryRepository:
    """Append-only projector specification and geometry-evaluation persistence."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.path = Path(scene_repository.path)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_projector_specifications (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    specification_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    specification_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    UNIQUE(specification_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_projector_specification_seq
                    ON cad_projector_specifications(seq ASC);

                CREATE TABLE IF NOT EXISTS cad_video_geometry_evaluations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    evaluation_id TEXT NOT NULL UNIQUE,
                    evaluation_sha256 TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    scene_content_hash TEXT NOT NULL,
                    system_variant_id TEXT,
                    system_variant_sha256 TEXT,
                    projector_specification_sha256 TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(scene_revision_id)
                        REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(projector_specification_sha256)
                        REFERENCES cad_projector_specifications(specification_sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_video_geometry_revision_seq
                    ON cad_video_geometry_evaluations(scene_revision_id, seq ASC);
                CREATE INDEX IF NOT EXISTS idx_video_geometry_variant_seq
                    ON cad_video_geometry_evaluations(system_variant_id, seq ASC);
                """
            )

    def save_projector_specification(
        self,
        specification: ProjectorSpecification,
    ) -> ProjectorSpecification:
        specification = ProjectorSpecification.model_validate(
            specification.model_dump(mode='python')
        )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_projector_specifications
                WHERE specification_id=? AND version=?
                """,
                (specification.specification_id, specification.version),
            ).fetchone()
            if existing is not None:
                persisted = ProjectorSpecification.model_validate_json(
                    existing['payload_json']
                )
                if persisted != specification:
                    raise ValueError(
                        'projector specification id/version already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_projector_specifications(
                    specification_id, version, specification_sha256,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    specification.specification_id,
                    specification.version,
                    specification.specification_sha256,
                    specification.model_dump_json(),
                    _utc_now(),
                ),
            )
        return specification

    def get_projector_specification(
        self,
        specification_id: str,
        version: str,
    ) -> ProjectorSpecification | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_projector_specifications
                WHERE specification_id=? AND version=?
                """,
                (specification_id, version),
            ).fetchone()
        return (
            None
            if row is None
            else ProjectorSpecification.model_validate_json(row['payload_json'])
        )

    def get_projector_specification_by_hash(
        self,
        specification_sha256: str,
    ) -> ProjectorSpecification | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_projector_specifications
                WHERE specification_sha256=?
                """,
                (specification_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else ProjectorSpecification.model_validate_json(row['payload_json'])
        )

    def list_projector_specifications(self) -> tuple[ProjectorSpecification, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_projector_specifications ORDER BY seq ASC'
            ).fetchall()
        return tuple(
            ProjectorSpecification.model_validate_json(row['payload_json'])
            for row in rows
        )

    def _reproduce_evaluation(
        self,
        evaluation: VideoGeometryEvaluation,
        specification: ProjectorSpecification,
    ) -> VideoGeometryEvaluation:
        target = evaluation.target
        baseline = self.scene_repository.get(target.scene_revision_id)
        if baseline is None:
            raise ValueError('video geometry SceneRevision does not exist')
        if (
            baseline.document_id != target.document_id
            or baseline.content_hash != target.scene_content_hash
        ):
            raise ValueError('video geometry SceneRevision authority mismatch')

        variant = None
        if target.system_variant_id is not None:
            if self.variant_repository is None:
                raise ValueError(
                    'SystemVariant-bound geometry evaluation requires variant repository'
                )
            variant = self.variant_repository.get_variant(target.system_variant_id)
            if variant is None:
                raise ValueError('video geometry SystemVariant does not exist')
            if variant.variant_sha256 != target.system_variant_sha256:
                raise ValueError('video geometry SystemVariant hash mismatch')
            if (
                variant.document_id != target.document_id
                or variant.baseline_revision_id != target.scene_revision_id
                or variant.baseline_content_hash != target.scene_content_hash
            ):
                raise ValueError('video geometry SystemVariant baseline mismatch')

        reproduced = evaluate_video_geometry(
            baseline=baseline,
            variant=variant,
            projector_specification=specification,
            request=evaluation.request,
        )
        if reproduced != evaluation:
            raise ValueError(
                'video geometry evaluation is not reproducible from exact persisted authority'
            )
        return reproduced

    def _specification_for(
        self,
        evaluation: VideoGeometryEvaluation,
    ) -> ProjectorSpecification:
        specification = self.get_projector_specification_by_hash(
            evaluation.projector_specification_sha256
        )
        if specification is None:
            raise ValueError(
                'video geometry evaluation references an unpersisted projector specification'
            )
        return specification

    def _replay_persisted_evaluation(
        self,
        payload_json: str,
    ) -> VideoGeometryEvaluation:
        evaluation = VideoGeometryEvaluation.model_validate_json(payload_json)
        return self._reproduce_evaluation(
            evaluation,
            self._specification_for(evaluation),
        )

    def save_evaluation(
        self,
        evaluation: VideoGeometryEvaluation,
    ) -> VideoGeometryEvaluation:
        evaluation = VideoGeometryEvaluation.model_validate(
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
                FROM cad_video_geometry_evaluations
                WHERE evaluation_id=?
                """,
                (reproduced.evaluation_id,),
            ).fetchone()
            if existing is not None:
                persisted = VideoGeometryEvaluation.model_validate_json(
                    existing['payload_json']
                )
                if persisted != reproduced:
                    raise ValueError(
                        'video geometry evaluation id already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_video_geometry_evaluations(
                    evaluation_id, evaluation_sha256, document_id,
                    scene_revision_id, scene_content_hash,
                    system_variant_id, system_variant_sha256,
                    projector_specification_sha256, request_sha256,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reproduced.evaluation_id,
                    reproduced.evaluation_sha256,
                    reproduced.target.document_id,
                    reproduced.target.scene_revision_id,
                    reproduced.target.scene_content_hash,
                    reproduced.target.system_variant_id,
                    reproduced.target.system_variant_sha256,
                    reproduced.projector_specification_sha256,
                    reproduced.request.request_sha256,
                    reproduced.model_dump_json(),
                    _utc_now(),
                ),
            )
        return reproduced

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> VideoGeometryEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_video_geometry_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._replay_persisted_evaluation(row['payload_json'])

    def list_evaluations_for_revision(
        self,
        scene_revision_id: str,
    ) -> tuple[VideoGeometryEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_video_geometry_evaluations
                WHERE scene_revision_id=?
                ORDER BY seq ASC
                """,
                (scene_revision_id,),
            ).fetchall()
        return tuple(
            self._replay_persisted_evaluation(row['payload_json'])
            for row in rows
        )

    def list_evaluations_for_variant(
        self,
        system_variant_id: str,
    ) -> tuple[VideoGeometryEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_video_geometry_evaluations
                WHERE system_variant_id=?
                ORDER BY seq ASC
                """,
                (system_variant_id,),
            ).fetchall()
        return tuple(
            self._replay_persisted_evaluation(row['payload_json'])
            for row in rows
        )
