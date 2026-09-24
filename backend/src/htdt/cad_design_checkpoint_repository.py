"""Append-only persistence for project design checkpoints (#619)."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from .cad_design_checkpoint import (
    CheckpointRestoreRecord,
    ConstraintWorkspaceSnapshot,
    ProjectDesignCheckpoint,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DesignCheckpointConflictError(ValueError):
    """A checkpoint/snapshot/restore save violated append-only identity rules."""


class CadDesignCheckpointRepository:
    """Native storage for checkpoint manifests and their snapshots.

    Three append-only tables: immutable constraint-workspace snapshots,
    immutable checkpoint manifests, and restore records. No row is ever
    updated or deleted — history stays inspectable and a restore always
    produces new current authority instead of rewriting these rows.
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
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_constraint_snapshots',
                'cad_design_checkpoints',
                'cad_checkpoint_restores',
            )


    def save_snapshot(self, snapshot: ConstraintWorkspaceSnapshot) -> None:
        if self.get_snapshot(snapshot.snapshot_id) is not None:
            raise DesignCheckpointConflictError(
                'ConstraintWorkspaceSnapshot ids are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_constraint_snapshots (
                    snapshot_id, document_id, constraint_sha256,
                    snapshot_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.document_id,
                    snapshot.constraint_sha256,
                    snapshot.snapshot_sha256,
                    snapshot.created_at_utc,
                    snapshot.model_dump_json(),
                ),
            )

    def get_snapshot(self, snapshot_id: str) -> ConstraintWorkspaceSnapshot | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_constraint_snapshots WHERE snapshot_id=?',
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return ConstraintWorkspaceSnapshot.model_validate_json(row['payload_json'])

    def list_snapshots(
        self,
        document_id: str,
    ) -> tuple[ConstraintWorkspaceSnapshot, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_constraint_snapshots
                WHERE document_id=?
                ORDER BY created_at_utc, snapshot_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            ConstraintWorkspaceSnapshot.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Checkpoints

    def save_checkpoint(self, checkpoint: ProjectDesignCheckpoint) -> None:
        if self.get_checkpoint(checkpoint.checkpoint_id) is not None:
            raise DesignCheckpointConflictError(
                'ProjectDesignCheckpoint ids are append-only'
            )
        if self.scene_repository.get(checkpoint.scene_revision_id) is None:
            raise ValueError('checkpoint pins a SceneRevision that is not persisted')
        if checkpoint.constraint_snapshot_id is not None:
            snapshot = self.get_snapshot(checkpoint.constraint_snapshot_id)
            if snapshot is None:
                raise ValueError('checkpoint pins a constraint snapshot that is not persisted')
            if snapshot.snapshot_sha256 != checkpoint.constraint_snapshot_sha256:
                raise ValueError('checkpoint constraint snapshot hash mismatch')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_design_checkpoints (
                    checkpoint_id, document_id, checkpoint_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.document_id,
                    checkpoint.checkpoint_sha256,
                    checkpoint.created_at_utc,
                    checkpoint.model_dump_json(),
                ),
            )

    def get_checkpoint(self, checkpoint_id: str) -> ProjectDesignCheckpoint | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_design_checkpoints WHERE checkpoint_id=?',
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return ProjectDesignCheckpoint.model_validate_json(row['payload_json'])

    def list_checkpoints(
        self,
        document_id: str,
    ) -> tuple[ProjectDesignCheckpoint, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_design_checkpoints
                WHERE document_id=?
                ORDER BY created_at_utc, checkpoint_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            ProjectDesignCheckpoint.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Restore records

    def save_restore(self, restore: CheckpointRestoreRecord) -> None:
        if self.get_restore(restore.restore_id) is not None:
            raise DesignCheckpointConflictError(
                'CheckpointRestoreRecord ids are append-only'
            )
        checkpoint = self.get_checkpoint(restore.checkpoint_id)
        if checkpoint is None:
            raise ValueError('restore record requires a persisted checkpoint')
        if checkpoint.checkpoint_sha256 != restore.checkpoint_sha256:
            raise ValueError('restore record is bound to a different checkpoint')
        if checkpoint.document_id != restore.document_id:
            raise ValueError('restore record document does not match checkpoint')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_checkpoint_restores (
                    restore_id, document_id, checkpoint_id,
                    restore_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    restore.restore_id,
                    restore.document_id,
                    restore.checkpoint_id,
                    restore.restore_sha256,
                    restore.created_at_utc,
                    restore.model_dump_json(),
                ),
            )

    def get_restore(self, restore_id: str) -> CheckpointRestoreRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_checkpoint_restores WHERE restore_id=?',
                (restore_id,),
            ).fetchone()
        if row is None:
            return None
        return CheckpointRestoreRecord.model_validate_json(row['payload_json'])

    def list_restores(
        self,
        document_id: str,
    ) -> tuple[CheckpointRestoreRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_checkpoint_restores
                WHERE document_id=?
                ORDER BY created_at_utc, restore_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            CheckpointRestoreRecord.model_validate_json(row['payload_json'])
            for row in rows
        )
