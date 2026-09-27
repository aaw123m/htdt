"""Append-only persistence for multi-seat analysis sets and results (#510)."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from .cad_measurement_repository import CadMeasurementRepository
from .cad_multi_seat_analysis import (
    MultiSeatAnalysisResult,
    MultiSeatAnalysisSet,
    replay_multi_seat_analysis,
)
from .comparison import FrequencyResponse
from .cad_schema import require_native_tables, connect_sqlite


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MultiSeatAnalysisConflictError(ValueError):
    """A multi-seat analysis save violated append-only identity rules."""


class CadMultiSeatAnalysisRepository:
    """Native storage for MultiSeatAnalysisSet + MultiSeatAnalysisResult.

    Results are validated by replay on read: a persisted result is only
    trusted when the pinned algorithm recomputes the persisted values from
    the exact member datasets it claims.
    """

    def __init__(self, measurement_repository: CadMeasurementRepository) -> None:
        self.measurement_repository = measurement_repository
        self.path = measurement_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_multi_seat_sets', 'cad_multi_seat_results')

    def _member_responses(
        self,
        analysis_set: MultiSeatAnalysisSet,
    ) -> tuple[FrequencyResponse, ...]:
        responses: list[FrequencyResponse] = []
        for member in analysis_set.members:
            dataset = self.measurement_repository.get_dataset(member.dataset_id)
            if dataset is None or dataset.dataset_sha256 != member.dataset_sha256:
                raise ValueError('multi-seat member dataset is unavailable or changed')
            responses.append(
                FrequencyResponse(dataset.frequency_hz, dataset.level_db)
            )
        return tuple(responses)

    def save_set(self, analysis_set: MultiSeatAnalysisSet) -> None:
        if self.get_set(analysis_set.set_id) is not None:
            raise MultiSeatAnalysisConflictError('multi-seat sets are append-only')
        for member in analysis_set.members:
            measurement = self.measurement_repository.get_measurement(member.measurement_id)
            if measurement is None or measurement.measurement_entity_id != member.target_entity_id:
                raise ValueError('multi-seat member measurement binding is unavailable')
            dataset = self.measurement_repository.get_dataset(member.dataset_id)
            if dataset is None or dataset.measurement_id != member.measurement_id:
                raise ValueError('multi-seat member dataset binding is unavailable')
            if dataset.dataset_sha256 != member.dataset_sha256:
                raise ValueError('multi-seat member dataset hash mismatch')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_multi_seat_sets (
                    set_id, document_id, set_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    analysis_set.set_id,
                    analysis_set.document_id,
                    analysis_set.set_sha256,
                    _utc_now(),
                    analysis_set.model_dump_json(),
                ),
            )

    def get_set(self, set_id: str) -> MultiSeatAnalysisSet | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_multi_seat_sets WHERE set_id=?',
                (set_id,),
            ).fetchone()
        if row is None:
            return None
        return MultiSeatAnalysisSet.model_validate_json(row['payload_json'])

    def save_result(self, result: MultiSeatAnalysisResult) -> None:
        if self.get_result(result.result_id) is not None:
            raise MultiSeatAnalysisConflictError('multi-seat results are append-only')
        analysis_set = self.get_set(result.set_id)
        if analysis_set is None or analysis_set.set_sha256 != result.set_sha256:
            raise ValueError('analysis result requires the exact persisted set')
        replay_multi_seat_analysis(result, analysis_set, self._member_responses(analysis_set))
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_multi_seat_results (
                    result_id, set_id, analysis_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    result.result_id,
                    result.set_id,
                    result.analysis_sha256,
                    _utc_now(),
                    result.model_dump_json(),
                ),
            )

    def get_result(self, result_id: str) -> MultiSeatAnalysisResult | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_multi_seat_results WHERE result_id=?',
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        return MultiSeatAnalysisResult.model_validate_json(row['payload_json'])

    def list_results(self, set_id: str) -> tuple[MultiSeatAnalysisResult, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_multi_seat_results
                WHERE set_id=?
                ORDER BY created_at_utc, result_id
                """,
                (set_id,),
            ).fetchall()
        return tuple(
            MultiSeatAnalysisResult.model_validate_json(row['payload_json'])
            for row in rows
        )
