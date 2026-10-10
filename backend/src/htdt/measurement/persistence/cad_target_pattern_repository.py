"""Persisted target-pattern store (#807 boundary refactor).

The SQL repository for ``MeasurementTargetPattern`` /
``MaterializedPatternPoint`` lives at persistence rank; the value models and
materialization math stay in ``domain/cad_measurement_target_pattern``.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from ...cad_repository import SceneRepository, SaveResult
from ...cad_schema import connect_sqlite, require_native_tables
from ..domain.cad_measurement_target_pattern import (
    MaterializedPatternPoint,
    MeasurementTargetPattern,
)


class CadTargetPatternRepository:
    """Append-only storage for patterns and materialized point lineage."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self):
        return closing(connect_sqlite(self.path))

    def _initialize(self) -> None:
        with self._connect() as connection, connection:
            require_native_tables(connection, 'cad_measurement_target_patterns', 'cad_materialized_pattern_points')

    def save_pattern(self, pattern: MeasurementTargetPattern) -> None:
        if self.get_pattern(pattern.pattern_id) is not None:
            raise ValueError('target pattern ids are append-only')
        revision = self.scene_repository.get(pattern.anchor_revision_id)
        if revision is None or revision.document_id != pattern.document_id:
            raise ValueError('pattern anchor SceneRevision does not exist')
        if revision.content_hash != pattern.anchor_revision_content_hash:
            raise ValueError('pattern anchor revision content hash mismatch')
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_measurement_target_patterns (
                    pattern_id, document_id, pattern_version, pattern_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern.pattern_id,
                    pattern.document_id,
                    pattern.pattern_version,
                    pattern.pattern_sha256,
                    datetime.now(timezone.utc).isoformat(),
                    pattern.model_dump_json(),
                ),
            )

    def list_patterns(
        self,
        document_id: str,
    ) -> tuple[MeasurementTargetPattern, ...]:
        """All persisted patterns of one project, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_measurement_target_patterns
                WHERE document_id=?
                ORDER BY created_at_utc, pattern_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            MeasurementTargetPattern.model_validate_json(row['payload_json'])
            for row in rows
        )

    def get_pattern(self, pattern_id: str) -> MeasurementTargetPattern | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_measurement_target_patterns '
                'WHERE pattern_id=?',
                (pattern_id,),
            ).fetchone()
        if row is None:
            return None
        return MeasurementTargetPattern.model_validate_json(row['payload_json'])

    def save_materialized_point(self, point: MaterializedPatternPoint) -> None:
        pattern = self.get_pattern(point.pattern_id)
        if pattern is None or pattern.pattern_sha256 != point.pattern_sha256:
            raise ValueError('materialized point requires the exact persisted pattern')
        revision = self.scene_repository.get(point.created_in_revision_id)
        if revision is None or revision.document_id != point.document_id:
            raise ValueError('materialized point creation revision does not exist')
        entity = revision.document.entity(point.measurement_point_entity_id)
        if entity.kind != 'measurement_point':
            raise ValueError('materialized point must bind a measurement_point entity')
        if entity.position != point.position:
            raise ValueError('materialized point position does not match the entity')
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_materialized_pattern_points (
                    point_id, pattern_id, document_id,
                    measurement_point_entity_id, point_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    point.point_id,
                    point.pattern_id,
                    point.document_id,
                    point.measurement_point_entity_id,
                    point.point_sha256,
                    datetime.now(timezone.utc).isoformat(),
                    point.model_dump_json(),
                ),
            )

    def list_pattern_points(
        self,
        pattern_id: str,
    ) -> tuple[MaterializedPatternPoint, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_materialized_pattern_points
                WHERE pattern_id=?
                ORDER BY json_extract(payload_json, '$.offset_index')
                """,
                (pattern_id,),
            ).fetchall()
        return tuple(
            MaterializedPatternPoint.model_validate_json(row['payload_json'])
            for row in rows
        )
