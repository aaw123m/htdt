"""Append-only persistence for as-built wiring traceability (#597)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_physical_interconnect import (
    LogicalPhysicalBinding,
    PhysicalInterconnect,
    WiringVerificationRecord,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite


class WiringTraceConflictError(ValueError):
    """A wiring-trace save violated append-only identity rules."""


class CadWiringTraceRepository:
    """Native storage for physical interconnects, verifications, bindings.

    ``(path_id, version)`` is saved exactly once: a service change appends
    a new sealed revision (supersede chain), never an UPDATE. Verification
    records and logical→physical bindings reject orphans — the physical
    path they reference must already be stored at the pinned hash.
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_physical_interconnects',
                'cad_wiring_verifications',
                'cad_logical_physical_bindings',
            )

    # ------------------------------------------------------------- paths
    def save_interconnect(self, path: PhysicalInterconnect) -> None:
        if (
            self.get_interconnect(path.path_id, path.version) is not None
        ):
            raise WiringTraceConflictError(
                'PhysicalInterconnect (path_id, version) is append-only'
            )
        if path.scene_revision_id is not None:
            revision = self.scene_repository.get(path.scene_revision_id)
            if revision is None:
                raise ValueError(
                    'physical interconnect pins a SceneRevision that is '
                    'not persisted'
                )
            if revision.content_hash != path.scene_content_hash:
                raise ValueError(
                    'physical interconnect SceneRevision content hash '
                    'mismatch'
                )
            if revision.document_id != path.document_id:
                raise ValueError(
                    'physical interconnect SceneRevision belongs to '
                    'another document'
                )
        if path.supersedes_path_id is not None:
            superseded = self.get_interconnect_by_hash(
                path.supersedes_path_sha256
            )
            if superseded is None:
                raise ValueError(
                    'superseded physical path is not persisted — '
                    'as-built history cannot be fabricated'
                )
            if superseded.path_id != path.supersedes_path_id:
                raise ValueError(
                    'superseded path id does not match the stored record'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_physical_interconnects (
                    path_id, version, document_id, scene_revision_id,
                    scene_content_hash, path_class, evidence_state,
                    semantic_sha256, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    path.path_id,
                    path.version,
                    path.document_id,
                    path.scene_revision_id,
                    path.scene_content_hash,
                    path.path_class,
                    path.evidence_state,
                    path.semantic_sha256,
                    path.model_dump_json(),
                    path.created_at_utc,
                ),
            )

    def get_interconnect(
        self, path_id: str, version: str
    ) -> PhysicalInterconnect | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_physical_interconnects
                WHERE path_id=? AND version=?
                """,
                (path_id, version),
            ).fetchone()
        if row is None:
            return None
        return PhysicalInterconnect.model_validate_json(row['payload_json'])

    def get_interconnect_by_hash(
        self, semantic_sha256: str
    ) -> PhysicalInterconnect | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_physical_interconnects
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return PhysicalInterconnect.model_validate_json(row['payload_json'])

    def list_interconnects(
        self, document_id: str
    ) -> tuple[PhysicalInterconnect, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_physical_interconnects
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            PhysicalInterconnect.model_validate_json(row['payload_json'])
            for row in rows
        )

    def list_interconnect_history(
        self, path_id: str
    ) -> tuple[PhysicalInterconnect, ...]:
        """All revisions of one path — the supersede/service chain."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_physical_interconnects
                WHERE path_id=?
                ORDER BY seq ASC
                """,
                (path_id,),
            ).fetchall()
        return tuple(
            PhysicalInterconnect.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------ verifications
    def save_verification(self, record: WiringVerificationRecord) -> None:
        if self.get_verification(record.verification_id) is not None:
            raise WiringTraceConflictError(
                'WiringVerificationRecord is append-only'
            )
        if self.get_interconnect_by_hash(record.path_sha256) is None:
            raise ValueError(
                'verification references a physical path that is not '
                'stored at the pinned hash'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_wiring_verifications (
                    verification_id, path_id, path_sha256, test_kind,
                    result, semantic_sha256, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.verification_id,
                    record.path_id,
                    record.path_sha256,
                    record.test_kind,
                    record.result,
                    record.semantic_sha256,
                    record.model_dump_json(),
                    record.verified_at_utc,
                ),
            )

    def get_verification(
        self, verification_id: str
    ) -> WiringVerificationRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_wiring_verifications
                WHERE verification_id=?
                """,
                (verification_id,),
            ).fetchone()
        if row is None:
            return None
        return WiringVerificationRecord.model_validate_json(
            row['payload_json']
        )

    def list_verifications(
        self, path_id: str
    ) -> tuple[WiringVerificationRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_wiring_verifications
                WHERE path_id=?
                ORDER BY seq ASC
                """,
                (path_id,),
            ).fetchall()
        return tuple(
            WiringVerificationRecord.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ---------------------------------------------------------- bindings
    def save_binding(self, binding: LogicalPhysicalBinding) -> None:
        if self.get_binding(binding.binding_id) is not None:
            raise WiringTraceConflictError(
                'LogicalPhysicalBinding is append-only'
            )
        if self.get_interconnect_by_hash(binding.path_sha256) is None:
            raise ValueError(
                'binding references a physical path that is not stored '
                'at the pinned hash'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_logical_physical_bindings (
                    binding_id, document_id, logical_ref_kind,
                    logical_ref_id, path_id, path_sha256,
                    semantic_sha256, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.document_id,
                    binding.logical_ref_kind,
                    binding.logical_ref_id,
                    binding.path_id,
                    binding.path_sha256,
                    binding.semantic_sha256,
                    binding.model_dump_json(),
                    binding.recorded_at_utc,
                ),
            )

    def get_binding(
        self, binding_id: str
    ) -> LogicalPhysicalBinding | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_logical_physical_bindings
                WHERE binding_id=?
                """,
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return LogicalPhysicalBinding.model_validate_json(
            row['payload_json']
        )

    def bindings_for(
        self, logical_ref_id: str
    ) -> tuple[LogicalPhysicalBinding, ...]:
        """Binding history for one logical route, oldest first."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_logical_physical_bindings
                WHERE logical_ref_id=?
                ORDER BY seq ASC
                """,
                (logical_ref_id,),
            ).fetchall()
        return tuple(
            LogicalPhysicalBinding.model_validate_json(row['payload_json'])
            for row in rows
        )

    def latest_binding_for(
        self, logical_ref_id: str
    ) -> LogicalPhysicalBinding | None:
        bindings = self.bindings_for(logical_ref_id)
        return bindings[-1] if bindings else None

    def list_bindings(
        self, document_id: str
    ) -> tuple[LogicalPhysicalBinding, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_logical_physical_bindings
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            LogicalPhysicalBinding.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'CadWiringTraceRepository',
    'WiringTraceConflictError',
]
