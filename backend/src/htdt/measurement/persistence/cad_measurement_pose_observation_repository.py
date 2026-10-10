"""Pose-observation persistence store (#807 boundary refactor).

Append-only SQL store for ``MeasurementPoseObservation`` /
``PlannedObservedPoseDelta`` on the shared cad.sqlite3 DB. The value models
and delta evaluation live in ``domain/cad_measurement_pose``; this module is
the persistence-side table owner.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from ...cad_schema import connect_sqlite, ensure_native_schema, require_native_tables
from ..domain.cad_measurement_pose import (
    MeasurementPoseObservation,
    PlannedObservedPoseDelta,
)


class MeasurementPoseObservationRepository:
    """Append-only pose-observation store on the shared cad.sqlite3 DB."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        ensure_native_schema(self.path)
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection,
                'cad_measurement_pose_observations',
                'cad_planned_observed_deltas',
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_observation(
        self, observation: MeasurementPoseObservation
    ) -> MeasurementPoseObservation:
        """Append an observation; re-saving an identical row is a no-op,
        a conflicting row under the same id is a hard failure."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_measurement_pose_observations('
                'observation_id, document_id, measurement_ref, '
                'planned_target_ref, method, observed_at_utc, '
                'semantic_sha256, payload_json) VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(observation_id) DO NOTHING',
                (
                    observation.observation_id,
                    observation.document_id,
                    observation.measurement_ref,
                    observation.planned_target_ref,
                    observation.method,
                    observation.observed_at_utc,
                    observation.semantic_sha256,
                    observation.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT semantic_sha256, payload_json FROM '
                'cad_measurement_pose_observations WHERE observation_id=?',
                (observation.observation_id,),
            ).fetchone()
            if row['payload_json'] != observation.model_dump_json():
                raise ValueError(
                    f'pose observation {observation.observation_id} '
                    'already persisted with different content — '
                    'observations are immutable'
                )
        return observation

    def get_observation(
        self, observation_id: str
    ) -> MeasurementPoseObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_measurement_pose_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        return MeasurementPoseObservation.model_validate_json(
            row['payload_json']
        )

    def list_observations_for_measurement(
        self, measurement_ref: str
    ) -> tuple[MeasurementPoseObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_measurement_pose_observations '
                'WHERE measurement_ref=? '
                'ORDER BY observed_at_utc ASC, observation_id ASC',
                (measurement_ref,),
            ).fetchall()
        return tuple(
            MeasurementPoseObservation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def list_observations_for_document(
        self, document_id: str
    ) -> tuple[MeasurementPoseObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_measurement_pose_observations '
                'WHERE document_id=? '
                'ORDER BY observed_at_utc ASC, observation_id ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            MeasurementPoseObservation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def save_delta(
        self, delta: PlannedObservedPoseDelta
    ) -> PlannedObservedPoseDelta:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_planned_observed_deltas('
                'delta_id, observation_id, classification, payload_json) '
                'VALUES(?,?,?,?) ON CONFLICT(delta_id) DO NOTHING',
                (
                    delta.delta_id,
                    delta.observation_id,
                    delta.classification,
                    delta.model_dump_json(),
                ),
            )
            row = connection.execute(
                'SELECT payload_json FROM cad_planned_observed_deltas '
                'WHERE delta_id=?',
                (delta.delta_id,),
            ).fetchone()
            if row['payload_json'] != delta.model_dump_json():
                raise ValueError(
                    f'pose delta {delta.delta_id} already persisted with '
                    'different content — deltas are immutable'
                )
        return delta

    def get_delta(
        self, delta_id: str
    ) -> PlannedObservedPoseDelta | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_planned_observed_deltas '
                'WHERE delta_id=?',
                (delta_id,),
            ).fetchone()
        if row is None:
            return None
        return PlannedObservedPoseDelta.model_validate_json(
            row['payload_json']
        )

__all__ = ['MeasurementPoseObservationRepository']
