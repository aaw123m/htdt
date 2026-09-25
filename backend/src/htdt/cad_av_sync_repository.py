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

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        system_variant_repository=None,
        operating_state_repository=None,
    ) -> None:
        self.scene_repository = scene_repository
        self._system_variant_repository = system_variant_repository
        self._operating_state_repository = operating_state_repository
        self.path = scene_repository.path
        self._initialize()

    def _variants(self):
        if self._system_variant_repository is None:
            from .cad_system_variant_repository import (
                CadSystemVariantRepository,
            )
            self._system_variant_repository = CadSystemVariantRepository(
                self.scene_repository
            )
        return self._system_variant_repository

    def _operating_states(self):
        if self._operating_state_repository is None:
            from .cad_room_operating_state_repository import (
                CadRoomOperatingStateRepository,
            )
            self._operating_state_repository = CadRoomOperatingStateRepository(
                self.scene_repository
            )
        return self._operating_state_repository

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
        self._resolve_condition_references(condition)
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

    def _resolve_condition_references(
        self,
        condition: AVSyncCondition,
    ) -> None:
        """Every declared authority ref must resolve to the exact pinned row.

        A bare id is never accepted as evidence: the id must exist in its
        canonical repository and — where a content hash is pinned — must
        match it, so a condition cannot claim nonexistent, foreign or
        historically ambiguous Scene/SystemVariant/OperatingState identity.
        """
        if condition.scene_revision_id is not None:
            revision = self.scene_repository.get(condition.scene_revision_id)
            if revision is None or revision.document_id != condition.document_id:
                raise ValueError(
                    'A/V sync condition references an unpersisted or foreign '
                    'scene revision'
                )
            if revision.content_hash != condition.scene_revision_sha256:
                raise ValueError(
                    'A/V sync condition scene revision hash mismatch'
                )
        if condition.system_variant_id is not None:
            variant = self._variants().get_variant(condition.system_variant_id)
            if variant is None or variant.document_id != condition.document_id:
                raise ValueError(
                    'A/V sync condition references an unpersisted or foreign '
                    'system variant'
                )
            if variant.variant_sha256 != condition.system_variant_sha256:
                raise ValueError(
                    'A/V sync condition system variant hash mismatch'
                )
        if condition.operating_state_id is not None:
            state = self._operating_states().get_state_by_hash(
                condition.operating_state_sha256
            )
            if (
                state is None
                or state.state_id != condition.operating_state_id
                or state.document_id != condition.document_id
            ):
                raise ValueError(
                    'A/V sync condition references an unpersisted or foreign '
                    'room operating state'
                )

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
        self._validate_chain_transition(measurement)
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

    def _validate_chain_transition(
        self,
        measurement: AVLatencyMeasurement,
    ) -> None:
        """Lifecycle chain rules: one chain, one head, monotonic stages.

        A chain head is always the ``measured`` stage with no predecessor;
        every successor must resolve its predecessor, stay on the same
        condition, advance the status order and never branch an already
        extended stage.
        """
        order = {
            'measured': 0,
            'correction_requested': 1,
            'setting_applied': 2,
            'residual_verified': 3,
        }
        if measurement.predecessor_measurement_id is None:
            if measurement.status != 'measured':
                raise AVSyncConflictError(
                    'an A/V latency chain head must be the measured stage'
                )
            return
        predecessor = self.get_measurement(
            measurement.predecessor_measurement_id
        )
        if predecessor is None:
            raise ValueError(
                'A/V latency stage references an unpersisted predecessor'
            )
        if predecessor.measurement_sha256 != measurement.predecessor_sha256:
            raise ValueError(
                'A/V latency stage predecessor hash mismatch'
            )
        predecessor_chain = (
            predecessor.chain_id or predecessor.measurement_id
        )
        if measurement.chain_id != predecessor_chain:
            raise ValueError(
                'A/V latency stage does not belong to the predecessor chain'
            )
        if predecessor.condition_id != measurement.condition_id:
            raise ValueError(
                'A/V latency stage cannot change the bound condition'
            )
        if order[measurement.status] <= order[predecessor.status]:
            raise AVSyncConflictError(
                'A/V sync lifecycle transitions are monotonic'
            )
        siblings = [
            stage
            for stage in self.list_measurements(measurement.condition_id)
            if stage.predecessor_measurement_id
            == measurement.predecessor_measurement_id
        ]
        if siblings:
            raise AVSyncConflictError(
                'the predecessor stage already has a successor; the chain '
                'keeps one active head'
            )

    def list_chain_stages(
        self,
        measurement: AVLatencyMeasurement,
    ) -> tuple[AVLatencyMeasurement, ...]:
        """Every persisted stage of one measurement's chain, oldest first."""
        chain = measurement.chain_id or measurement.measurement_id
        return tuple(
            stage
            for stage in self.list_measurements(measurement.condition_id)
            if (stage.chain_id or stage.measurement_id) == chain
        )
