"""Persistence for speaker equipment binding semantics (#476)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_equipment_binding import EquipmentBindingSemantics
from .cad_equipment_repository import CadEquipmentRepository
from .cad_repository import SceneRepository


class CadEquipmentBindingRepository:
    """Append-only binding-semantics records, one authority per entity binding.

    The latest record for (document_id, entity_id) is the effective binding
    semantics; earlier records remain as history.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        equipment_repository: CadEquipmentRepository,
    ) -> None:
        self.scene_repository = scene_repository
        self.equipment_repository = equipment_repository
        self.path = Path(scene_repository.path)
        if Path(equipment_repository.path) != self.path:
            raise ValueError(
                'scene and equipment repositories must share one native '
                'CAD database'
            )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_equipment_binding_semantics (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    binding_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    equipment_definition_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_equipment_binding_entity
                    ON cad_equipment_binding_semantics(
                        document_id, entity_id, seq ASC
                    );
                """
            )

    def save_binding(
        self,
        binding: EquipmentBindingSemantics,
    ) -> EquipmentBindingSemantics:
        binding = EquipmentBindingSemantics.model_validate(
            binding.model_dump(mode='python')
        )
        if self.equipment_repository.get_definition_by_hash(
            binding.equipment.semantic_sha256
        ) is None:
            raise ValueError(
                'binding semantics references an unpersisted EquipmentDefinition'
            )
        head = self.scene_repository.current_head(binding.document_id)
        if head is None:
            raise ValueError(
                f'document {binding.document_id} has no scene revision to bind'
            )
        entity_ids = {item.entity_id for item in head.document.entities}
        if binding.entity_id not in entity_ids:
            raise ValueError(
                f'scene entity {binding.entity_id} is not present in the '
                f'current scene revision of {binding.document_id}'
            )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_equipment_binding_semantics
                WHERE binding_id=?
                """,
                (binding.binding_id,),
            ).fetchone()
            if existing is not None:
                persisted = EquipmentBindingSemantics.model_validate_json(
                    existing['payload_json']
                )
                if persisted != binding:
                    raise ValueError(
                        'equipment binding semantics ID already exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_equipment_binding_semantics(
                    binding_id, semantic_sha256, document_id, entity_id,
                    equipment_definition_sha256, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.semantic_sha256,
                    binding.document_id,
                    binding.entity_id,
                    binding.equipment.semantic_sha256,
                    binding.model_dump_json(),
                    binding.created_at_utc,
                ),
            )
        return binding

    def get_binding(self, binding_id: str) -> EquipmentBindingSemantics | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_equipment_binding_semantics
                WHERE binding_id=?
                """,
                (binding_id,),
            ).fetchone()
        return (
            None
            if row is None
            else EquipmentBindingSemantics.model_validate_json(
                row['payload_json']
            )
        )

    def get_binding_for_entity(
        self,
        document_id: str,
        entity_id: str,
    ) -> EquipmentBindingSemantics | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_equipment_binding_semantics
                WHERE document_id=? AND entity_id=?
                ORDER BY seq DESC LIMIT 1
                """,
                (document_id, entity_id),
            ).fetchone()
        return (
            None
            if row is None
            else EquipmentBindingSemantics.model_validate_json(
                row['payload_json']
            )
        )

    def get_binding_by_hash(
        self,
        semantic_sha256: str,
    ) -> EquipmentBindingSemantics | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_equipment_binding_semantics
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else EquipmentBindingSemantics.model_validate_json(
                row['payload_json']
            )
        )


__all__ = ['CadEquipmentBindingRepository']
