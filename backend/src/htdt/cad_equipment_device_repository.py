"""Append-only persistence for the equipment device framework (#726).

Every record — binding, capability snapshot, observation, action, ack —
is immutable: a new observation or a new snapshot is a new row, never an
UPDATE of the old one. Device semantics live in ``cad_equipment_device``;
this module only guards identity/parentage.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_equipment_device import (
    DeviceActionAck,
    DeviceCapabilitySnapshot,
    DeviceTargetBinding,
    ObservedDeviceState,
    ProposedDeviceAction,
)
from .cad_repository import SceneRepository


class DeviceFrameworkConflictError(ValueError):
    """A framework record violated append-only identity rules."""


class CadEquipmentDeviceRepository:
    """Native storage for device bindings and their evidence records."""

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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_device_target_bindings (
                    binding_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_device_capability_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    binding_sha256 TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL UNIQUE,
                    probed_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_observed_device_states (
                    observation_id TEXT PRIMARY KEY,
                    binding_sha256 TEXT NOT NULL,
                    observation_sha256 TEXT NOT NULL UNIQUE,
                    observed_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_proposed_device_actions (
                    action_id TEXT PRIMARY KEY,
                    binding_sha256 TEXT NOT NULL,
                    action_sha256 TEXT NOT NULL UNIQUE,
                    planned_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_device_action_acks (
                    ack_id TEXT PRIMARY KEY,
                    action_sha256 TEXT NOT NULL,
                    ack_sha256 TEXT NOT NULL UNIQUE,
                    acked_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    # ------------------------------------------------------------------
    # Bindings

    def save_binding(self, binding: DeviceTargetBinding) -> None:
        if self.get_binding(binding.binding_id) is not None:
            raise DeviceFrameworkConflictError(
                'DeviceTargetBinding ids are append-only'
            )
        if self.scene_repository.latest(binding.document_id) is None:
            raise ValueError(
                'binding pins a document with no persisted SceneRevision'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_device_target_bindings (
                    binding_id, document_id, binding_sha256, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.document_id,
                    binding.binding_sha256,
                    binding.model_dump_json(),
                ),
            )

    def get_binding(self, binding_id: str) -> DeviceTargetBinding | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_device_target_bindings WHERE binding_id=?',
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return DeviceTargetBinding.model_validate_json(row['payload_json'])

    def get_binding_by_hash(
        self, binding_sha256: str
    ) -> DeviceTargetBinding | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_device_target_bindings WHERE binding_sha256=?',
                (binding_sha256,),
            ).fetchone()
        if row is None:
            return None
        return DeviceTargetBinding.model_validate_json(row['payload_json'])

    def _require_binding(self, binding_sha256: str) -> DeviceTargetBinding:
        binding = self.get_binding_by_hash(binding_sha256)
        if binding is None:
            raise ValueError(
                'record references a DeviceTargetBinding that is not persisted'
            )
        return binding

    # ------------------------------------------------------------------
    # Capability snapshots

    def save_capability_snapshot(
        self, snapshot: DeviceCapabilitySnapshot
    ) -> None:
        self._require_binding(snapshot.binding_sha256)
        if self.get_capability_snapshot(snapshot.snapshot_id) is not None:
            raise DeviceFrameworkConflictError(
                'DeviceCapabilitySnapshot ids are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_device_capability_snapshots (
                    snapshot_id, binding_sha256, snapshot_sha256,
                    probed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.binding_sha256,
                    snapshot.snapshot_sha256,
                    snapshot.probed_at_utc,
                    snapshot.model_dump_json(),
                ),
            )

    def get_capability_snapshot(
        self, snapshot_id: str
    ) -> DeviceCapabilitySnapshot | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_device_capability_snapshots WHERE snapshot_id=?',
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return DeviceCapabilitySnapshot.model_validate_json(row['payload_json'])

    def list_capability_snapshots(
        self, binding_sha256: str
    ) -> tuple[DeviceCapabilitySnapshot, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_device_capability_snapshots
                WHERE binding_sha256=? ORDER BY probed_at_utc, snapshot_id
                """,
                (binding_sha256,),
            ).fetchall()
        return tuple(
            DeviceCapabilitySnapshot.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Observations

    def save_observation(self, observation: ObservedDeviceState) -> None:
        self._require_binding(observation.binding_sha256)
        if self.get_observation(observation.observation_id) is not None:
            raise DeviceFrameworkConflictError(
                'ObservedDeviceState ids are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_observed_device_states (
                    observation_id, binding_sha256, observation_sha256,
                    observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.binding_sha256,
                    observation.observation_sha256,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> ObservedDeviceState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_observed_device_states WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        return ObservedDeviceState.model_validate_json(row['payload_json'])

    def list_observations(
        self, binding_sha256: str
    ) -> tuple[ObservedDeviceState, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_observed_device_states
                WHERE binding_sha256=? ORDER BY observed_at_utc, observation_id
                """,
                (binding_sha256,),
            ).fetchall()
        return tuple(
            ObservedDeviceState.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Actions and acks

    def save_action(self, action: ProposedDeviceAction) -> None:
        self._require_binding(action.binding_sha256)
        if self.get_action(action.action_id) is not None:
            raise DeviceFrameworkConflictError(
                'ProposedDeviceAction ids are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_proposed_device_actions (
                    action_id, binding_sha256, action_sha256,
                    planned_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    action.binding_sha256,
                    action.action_sha256,
                    action.planned_at_utc,
                    action.model_dump_json(),
                ),
            )

    def get_action(self, action_id: str) -> ProposedDeviceAction | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_proposed_device_actions WHERE action_id=?',
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        return ProposedDeviceAction.model_validate_json(row['payload_json'])

    def save_ack(self, ack: DeviceActionAck) -> None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT action_id FROM cad_proposed_device_actions WHERE action_sha256=?',
                (ack.action_sha256,),
            ).fetchone()
        if row is None:
            raise ValueError(
                'ack references a ProposedDeviceAction that is not persisted'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_device_action_acks (
                    ack_id, action_sha256, ack_sha256,
                    acked_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    ack.ack_id,
                    ack.action_sha256,
                    ack.ack_sha256,
                    ack.acked_at_utc,
                    ack.model_dump_json(),
                ),
            )

    def list_acks(
        self, action_sha256: str
    ) -> tuple[DeviceActionAck, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_device_action_acks
                WHERE action_sha256=? ORDER BY acked_at_utc, ack_id
                """,
                (action_sha256,),
            ).fetchall()
        return tuple(
            DeviceActionAck.model_validate_json(row['payload_json'])
            for row in rows
        )
