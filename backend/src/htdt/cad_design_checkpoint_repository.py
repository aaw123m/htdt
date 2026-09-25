"""Append-only persistence for project design checkpoints (#619)."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3
from typing import TYPE_CHECKING

from .cad_design_checkpoint import (
    CheckpointRestoreRecord,
    ConstraintWorkspaceSnapshot,
    ProjectDesignCheckpoint,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables

if TYPE_CHECKING:
    from .cad_authority_refs import AuthorityRefResolver


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Checkpoint component kinds that name canonical persisted authorities.
#: Kinds naming declared-only artifacts (``system_topology``,
#: ``constraint_workspace`` payloads, ``layout_profile``, ``target_curve``,
#: ``standards_profile``, ``room_operating_state``, ``other``) have no
#: canonical table and stay declared-only — their absence is never
#: fabricated as a failure.
_COMPONENT_RESOLVER_KINDS: dict[str, str] = {
    'scene_revision': 'scene_revision',
    'system_variant': 'system_variant',
    'operating_preset': 'operating_preset',
    'design_checkpoint': 'design_checkpoint',
}


class DesignCheckpointConflictError(ValueError):
    """A checkpoint/snapshot/restore save violated append-only identity rules."""


class CadDesignCheckpointRepository:
    """Native storage for checkpoint manifests and their snapshots.

    Three append-only tables: immutable constraint-workspace snapshots,
    immutable checkpoint manifests, and restore records. No row is ever
    updated or deleted — history stays inspectable and a restore always
    produces new current authority instead of rewriting these rows.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        ref_resolver: 'AuthorityRefResolver | None' = None,
    ) -> None:
        self.scene_repository = scene_repository
        if ref_resolver is None:
            from .cad_authority_refs import CanonicalAuthorityRefResolver

            ref_resolver = CanonicalAuthorityRefResolver(scene_repository)
        self.ref_resolver = ref_resolver
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
        with closing(self._connect()) as connection, connection:
            self.save_restore_in_transaction(connection, restore)

    def save_restore_in_transaction(
        self,
        connection: sqlite3.Connection,
        restore: CheckpointRestoreRecord,
    ) -> None:
        """Append one restore record inside the caller's transaction.

        The caller owns BEGIN/COMMIT/ROLLBACK — checkpoint restore uses this
        to commit the record in the same boundary as the design-state
        mutation it documents, so a current-state change can never exist
        without its audit row.
        """
        if connection.execute(
            'SELECT 1 FROM cad_checkpoint_restores WHERE restore_id=?',
            (restore.restore_id,),
        ).fetchone() is not None:
            raise DesignCheckpointConflictError(
                'CheckpointRestoreRecord ids are append-only'
            )
        row = connection.execute(
            'SELECT checkpoint_sha256, document_id'
            ' FROM cad_design_checkpoints WHERE checkpoint_id=?',
            (restore.checkpoint_id,),
        ).fetchone()
        if row is None:
            raise ValueError('restore record requires a persisted checkpoint')
        if row['checkpoint_sha256'] != restore.checkpoint_sha256:
            raise ValueError('restore record is bound to a different checkpoint')
        if row['document_id'] != restore.document_id:
            raise ValueError('restore record document does not match checkpoint')
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

    # ------------------------------------------------------------------
    # Persisted-record verification (#757 semantic audit)

    def verify_persisted_snapshot(
        self, snapshot_id: str
    ) -> ConstraintWorkspaceSnapshot:
        """Re-run snapshot invariants: the embedded constraint workspace is
        self-consistent (its ``constraint_sha256`` and ``snapshot_sha256``
        self-hashes are re-derived by the model on parse)."""
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            raise ValueError(
                f'constraint snapshot {snapshot_id} no longer resolves'
            )
        return snapshot

    def verify_persisted_checkpoint(
        self, checkpoint_id: str
    ) -> ProjectDesignCheckpoint:
        """Re-run checkpoint invariants on a persisted row.

        The scene pin must resolve and stay on-hash/document; the
        constraint-snapshot binding (when pinned) must resolve exactly;
        every component ref naming a canonical authority must resolve,
        stay in-project, and match its pinned hash.
        """
        checkpoint = self.get_checkpoint(checkpoint_id)
        if checkpoint is None:
            raise ValueError(
                f'design checkpoint {checkpoint_id} no longer resolves'
            )
        revision = self.scene_repository.get(checkpoint.scene_revision_id)
        if revision is None:
            raise ValueError('checkpoint pins a SceneRevision that is not persisted')
        if revision.content_hash != checkpoint.scene_content_hash:
            raise ValueError('checkpoint SceneRevision content hash mismatch')
        if revision.document_id != checkpoint.document_id:
            raise ValueError('checkpoint SceneRevision belongs to another document')
        if checkpoint.constraint_snapshot_id is not None:
            self._assert_resolves(
                'constraint_snapshot',
                checkpoint.constraint_snapshot_id,
                checkpoint.constraint_snapshot_sha256,
                checkpoint.document_id,
            )
        for ref in checkpoint.component_refs:
            resolver_kind = _COMPONENT_RESOLVER_KINDS.get(ref.kind)
            if resolver_kind is None:
                continue
            self._assert_resolves(
                resolver_kind, ref.ref_id, ref.ref_sha256, checkpoint.document_id
            )
        return checkpoint

    def verify_persisted_restore(
        self, restore_id: str
    ) -> CheckpointRestoreRecord:
        restore = self.get_restore(restore_id)
        if restore is None:
            raise ValueError(f'checkpoint restore {restore_id} no longer resolves')
        checkpoint = self.get_checkpoint(restore.checkpoint_id)
        if checkpoint is None:
            raise ValueError('restore record requires a persisted checkpoint')
        if checkpoint.checkpoint_sha256 != restore.checkpoint_sha256:
            raise ValueError('restore record is bound to a different checkpoint')
        if checkpoint.document_id != restore.document_id:
            raise ValueError('restore record document does not match checkpoint')
        if restore.new_scene_revision_id is not None:
            revision = self.scene_repository.get(restore.new_scene_revision_id)
            if revision is None:
                raise ValueError(
                    'restore record names a SceneRevision that is not persisted'
                )
            if revision.document_id != restore.document_id:
                raise ValueError(
                    'restore record SceneRevision belongs to another document'
                )
        return restore

    def _assert_resolves(
        self,
        resolver_kind: str,
        ref_id: str,
        ref_sha256: str | None,
        document_id: str,
    ) -> None:
        resolved = self.ref_resolver.resolve(resolver_kind, ref_id, document_id)
        if resolved is None:
            raise ValueError(
                f'references a {resolver_kind} authority that does not resolve'
            )
        if (
            resolved.document_id is not None
            and resolved.document_id != document_id
        ):
            raise ValueError(
                f'{resolver_kind} authority belongs to another document'
            )
        if resolved.semantic_sha256 is not None:
            if ref_sha256 is None:
                raise ValueError(
                    f'ref must pin the {resolver_kind} semantic hash to claim '
                    'an exact reference'
                )
            if ref_sha256 != resolved.semantic_sha256:
                raise ValueError(
                    f'{resolver_kind} hash does not match the canonical authority'
                )
        elif ref_sha256 is not None:
            raise ValueError(
                f'ref supplies a hash the id-only {resolver_kind} authority '
                'does not expose'
            )
