"""Append-only persistence for room operating states (#556)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_room_operating_state import RoomOperatingState
from .cad_schema import require_native_tables


class OperatingStateConflictError(ValueError):
    """An operating-state save violated append-only identity rules."""


class CadRoomOperatingStateRepository:
    """Native storage for versioned RoomOperatingState records.

    ``(state_id, version)`` is saved exactly once: a changed observed
    configuration appends a new version row, never an UPDATE. Every save
    re-checks the pinned SceneRevision exists with the recorded content hash.
    """

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
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_room_operating_states')

    def save_state(self, state: RoomOperatingState) -> None:
        if self.get_state(state.state_id, state.version) is not None:
            raise OperatingStateConflictError(
                'RoomOperatingState (state_id, version) is append-only'
            )
        revision = self.scene_repository.get(state.scene_revision_id)
        if revision is None:
            raise ValueError('state pins a SceneRevision that is not persisted')
        if revision.content_hash != state.scene_content_hash:
            raise ValueError('state SceneRevision content hash mismatch')
        if revision.document_id != state.document_id:
            raise ValueError('state SceneRevision belongs to another document')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_room_operating_states (
                    state_id, version, document_id, scene_revision_id,
                    scene_content_hash, name, semantic_sha256, payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.state_id,
                    state.version,
                    state.document_id,
                    state.scene_revision_id,
                    state.scene_content_hash,
                    state.name,
                    state.semantic_sha256,
                    state.model_dump_json(),
                    state.observed_at_utc,
                ),
            )

    def get_state(
        self,
        state_id: str,
        version: str,
    ) -> RoomOperatingState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_room_operating_states
                WHERE state_id=? AND version=?
                """,
                (state_id, version),
            ).fetchone()
        if row is None:
            return None
        return RoomOperatingState.model_validate_json(row['payload_json'])

    def get_state_by_hash(
        self,
        semantic_sha256: str,
    ) -> RoomOperatingState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_room_operating_states
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return RoomOperatingState.model_validate_json(row['payload_json'])

    def list_states(
        self,
        document_id: str,
    ) -> tuple[RoomOperatingState, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_room_operating_states
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            RoomOperatingState.model_validate_json(row['payload_json'])
            for row in rows
        )

    def list_state_versions(
        self,
        state_id: str,
    ) -> tuple[RoomOperatingState, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_room_operating_states
                WHERE state_id=?
                ORDER BY seq ASC
                """,
                (state_id,),
            ).fetchall()
        return tuple(
            RoomOperatingState.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'CadRoomOperatingStateRepository',
    'OperatingStateConflictError',
]
