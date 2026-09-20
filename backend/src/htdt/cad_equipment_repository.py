from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_equipment import EquipmentDefinition
from .cad_equipment_catalog import (
    EquipmentCatalogSnapshot,
    build_equipment_catalog_snapshot,
)
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema
from .cad_system_variant import (
    EquipmentBindingRef,
    SystemVariant,
    materialize_system_variant,
)
from .cad_system_variant_repository import CadSystemVariantRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResolvedEquipmentBinding(BaseModel):
    """Exact SystemVariant source-entity -> EquipmentDefinition resolution."""

    model_config = ConfigDict(frozen=True)

    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    entity_id: str = Field(min_length=1)
    binding: EquipmentBindingRef
    definition: EquipmentDefinition


class CadEquipmentRepository:
    """Append-only O100C EquipmentDefinition persistence and exact variant resolution."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.path = Path(scene_repository.path)
        ensure_native_schema(self.path)
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
                CREATE TABLE IF NOT EXISTS cad_equipment_definitions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    definition_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    UNIQUE(definition_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_equipment_definition_seq
                    ON cad_equipment_definitions(seq ASC);
                """
            )

    def save_definition(
        self,
        definition: EquipmentDefinition,
    ) -> EquipmentDefinition:
        definition = EquipmentDefinition.model_validate(
            definition.model_dump(mode='python')
        )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_equipment_definitions
                WHERE definition_id=? AND version=?
                """,
                (definition.definition_id, definition.version),
            ).fetchone()
            if existing is not None:
                persisted = EquipmentDefinition.model_validate_json(
                    existing['payload_json']
                )
                if persisted != definition:
                    raise ValueError(
                        'equipment definition id/version already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_equipment_definitions(
                    definition_id, version, semantic_sha256,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    definition.definition_id,
                    definition.version,
                    definition.semantic_sha256,
                    definition.model_dump_json(),
                    _utc_now(),
                ),
            )
        return definition

    def get_definition(
        self,
        definition_id: str,
        version: str,
    ) -> EquipmentDefinition | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_equipment_definitions
                WHERE definition_id=? AND version=?
                """,
                (definition_id, version),
            ).fetchone()
        return (
            None
            if row is None
            else EquipmentDefinition.model_validate_json(row['payload_json'])
        )

    def get_definition_by_hash(
        self,
        semantic_sha256: str,
    ) -> EquipmentDefinition | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_equipment_definitions
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else EquipmentDefinition.model_validate_json(row['payload_json'])
        )

    def catalog_snapshot(self) -> EquipmentCatalogSnapshot:
        return build_equipment_catalog_snapshot(
            self.list_definitions()
        )

    def list_definitions(self) -> tuple[EquipmentDefinition, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_equipment_definitions
                ORDER BY seq ASC
                """
            ).fetchall()
        return tuple(
            EquipmentDefinition.model_validate_json(row['payload_json'])
            for row in rows
        )

    def _definition_for_ref(
        self,
        binding: EquipmentBindingRef,
    ) -> EquipmentDefinition:
        definition = self.get_definition_by_hash(
            binding.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'SystemVariant equipment binding references an unpersisted definition'
            )
        if (
            definition.definition_id != binding.equipment_definition_id
            or definition.version != binding.equipment_definition_version
        ):
            raise ValueError(
                'SystemVariant equipment binding definition identity mismatch'
            )
        return definition

    def resolve_variant_bindings(
        self,
        variant_id: str,
    ) -> tuple[ResolvedEquipmentBinding, ...]:
        if self.variant_repository is None:
            raise ValueError(
                'variant equipment resolution requires CadSystemVariantRepository'
            )
        variant = self.variant_repository.get_variant(variant_id)
        if variant is None:
            raise ValueError('SystemVariant does not exist')
        return self.resolve_bindings(variant)

    def resolve_bindings(
        self,
        variant: SystemVariant,
    ) -> tuple[ResolvedEquipmentBinding, ...]:
        baseline = self.scene_repository.get(variant.baseline_revision_id)
        if baseline is None:
            raise ValueError('SystemVariant baseline SceneRevision does not exist')
        if (
            baseline.document_id != variant.document_id
            or baseline.content_hash != variant.baseline_content_hash
        ):
            raise ValueError('SystemVariant baseline authority mismatch')

        scene = materialize_system_variant(baseline, variant)
        final_entities = {
            entity.entity_id: entity
            for entity in scene.entities
        }

        resolved: list[ResolvedEquipmentBinding] = []
        for binding in variant.equipment_bindings:
            entity = final_entities.get(binding.entity_id)
            if entity is None:
                raise ValueError(
                    'SystemVariant equipment binding references missing source entity'
                )
            if entity.kind != 'speaker':
                raise ValueError(
                    'SystemVariant equipment binding requires a speaker source entity'
                )
            definition = self._definition_for_ref(binding)
            resolved.append(
                ResolvedEquipmentBinding(
                    variant_id=variant.variant_id,
                    variant_sha256=variant.variant_sha256,
                    entity_id=binding.entity_id,
                    binding=binding,
                    definition=definition,
                )
            )
        return tuple(resolved)

    def definition_for_variant_entity(
        self,
        variant_id: str,
        entity_id: str,
    ) -> EquipmentDefinition:
        matches = [
            item.definition
            for item in self.resolve_variant_bindings(variant_id)
            if item.entity_id == entity_id
        ]
        if len(matches) != 1:
            raise ValueError(
                'exactly one persisted equipment binding is required for source entity'
            )
        return matches[0]
