"""Append-only persistence for the A/V synchronization authority (#560)."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3
from uuid import uuid4

from .cad_av_sync import (
    AVLatencyMeasurement,
    AVSyncCondition,
    _hash,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AVSyncConflictError(ValueError):
    """An A/V-sync save violated append-only identity rules."""


class CadAVSyncRepository:
    """Native storage for AVSyncCondition + AVLatencyMeasurement rows."""

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
            require_native_tables(connection, 'cad_av_sync_conditions', 'cad_av_latency_measurements')

    # ------------------------------------------------------------------
    # Conditions

    def save_condition(self, condition: AVSyncCondition) -> None:
        if self.get_condition(condition.condition_id) is not None:
            raise AVSyncConflictError('AVSyncCondition ids are append-only')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_av_sync_conditions (
                    condition_id, document_id, condition_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    condition.condition_id,
                    condition.document_id,
                    condition.condition_sha256,
                    _utc_now(),
                    condition.model_dump_json(),
                ),
            )

    def get_condition(self, condition_id: str) -> AVSyncCondition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_av_sync_conditions WHERE condition_id=?',
                (condition_id,),
            ).fetchone()
        if row is None:
            return None
        return AVSyncCondition.model_validate_json(row['payload_json'])

    def list_conditions(
        self,
        document_id: str,
    ) -> tuple[AVSyncCondition, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_av_sync_conditions
                WHERE document_id=?
                ORDER BY created_at_utc, condition_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            AVSyncCondition.model_validate_json(row['payload_json']) for row in rows
        )

    # ------------------------------------------------------------------
    # Measurements

    def save_measurement(self, measurement: AVLatencyMeasurement) -> None:
        if self.get_measurement(measurement.measurement_id) is not None:
            raise AVSyncConflictError('AVLatencyMeasurement ids are append-only')
        condition = self.get_condition(measurement.condition_id)
        if condition is None:
            raise ValueError('A/V latency measurement requires a persisted condition')
        if condition.document_id != measurement.document_id:
            raise ValueError('A/V latency measurement document does not match condition')
        if condition.condition_sha256 != measurement.condition_sha256:
            raise ValueError('A/V latency measurement is bound to a different condition')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_av_latency_measurements (
                    measurement_id, document_id, condition_id, status,
                    measurement_sha256, captured_at_utc, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.document_id,
                    measurement.condition_id,
                    measurement.status,
                    measurement.measurement_sha256,
                    measurement.captured_at,
                    _utc_now(),
                    measurement.model_dump_json(),
                ),
            )

    def get_measurement(self, measurement_id: str) -> AVLatencyMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_av_latency_measurements WHERE measurement_id=?',
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        return AVLatencyMeasurement.model_validate_json(row['payload_json'])

    def list_measurements(
        self,
        condition_id: str,
    ) -> tuple[AVLatencyMeasurement, ...]:
        """All lifecycle stages recorded for one condition, oldest first."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_av_latency_measurements
                WHERE condition_id=?
                ORDER BY created_at_utc, measurement_id
                """,
                (condition_id,),
            ).fetchall()
        return tuple(
            AVLatencyMeasurement.model_validate_json(row['payload_json']) for row in rows
        )
