"""Persistence + usage inventory for the library upgrade workflow (#608)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_equipment_instance import InstalledEquipmentInstance
from .cad_equipment_repository import CadEquipmentRepository
from .cad_library_upgrade import (
    EquipmentDefinitionUpgrade,
    EquipmentDefinitionUsage,
    UpgradeAdoptionRecord,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables


class CadLibraryUpgradeRepository:
    """Append-only upgrade/adoption records plus old-version usage scanning."""

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
            require_native_tables(connection, 'cad_equipment_upgrades', 'cad_upgrade_adoptions')

    def _table_exists(self, connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            is not None
        )

    def save_upgrade(
        self,
        upgrade: EquipmentDefinitionUpgrade,
    ) -> EquipmentDefinitionUpgrade:
        upgrade = EquipmentDefinitionUpgrade.model_validate(
            upgrade.model_dump(mode='python')
        )
        for ref in (upgrade.from_version, upgrade.to_version):
            if self.equipment_repository.get_definition_by_hash(
                ref.semantic_sha256
            ) is None:
                raise ValueError(
                    'upgrade references an unpersisted EquipmentDefinition'
                )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_equipment_upgrades
                WHERE upgrade_id=?
                """,
                (upgrade.upgrade_id,),
            ).fetchone()
            if existing is not None:
                persisted = EquipmentDefinitionUpgrade.model_validate_json(
                    existing['payload_json']
                )
                if persisted != upgrade:
                    raise ValueError(
                        'equipment upgrade id already exists with different '
                        'semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_equipment_upgrades(
                    upgrade_id, semantic_sha256, definition_id,
                    from_sha256, to_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    upgrade.upgrade_id,
                    upgrade.semantic_sha256,
                    upgrade.definition_id,
                    upgrade.from_version.semantic_sha256,
                    upgrade.to_version.semantic_sha256,
                    upgrade.model_dump_json(),
                ),
            )
        return upgrade

    def get_upgrade(
        self,
        upgrade_id: str,
    ) -> EquipmentDefinitionUpgrade | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_equipment_upgrades
                WHERE upgrade_id=?
                """,
                (upgrade_id,),
            ).fetchone()
        return (
            None
            if row is None
            else EquipmentDefinitionUpgrade.model_validate_json(
                row['payload_json']
            )
        )

    def list_upgrades(
        self,
        definition_id: str | None = None,
    ) -> tuple[EquipmentDefinitionUpgrade, ...]:
        with closing(self._connect()) as connection:
            if definition_id is None:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_equipment_upgrades '
                    'ORDER BY seq ASC'
                ).fetchall()
            else:
                rows = connection.execute(
                    'SELECT payload_json FROM cad_equipment_upgrades '
                    'WHERE definition_id=? ORDER BY seq ASC',
                    (definition_id,),
                ).fetchall()
        return tuple(
            EquipmentDefinitionUpgrade.model_validate_json(row['payload_json'])
            for row in rows
        )

    def usages_for_definition(
        self,
        definition_sha256: str,
    ) -> tuple[EquipmentDefinitionUsage, ...]:
        """Inventory every persisted authority bound to the old definition."""
        usages: list[EquipmentDefinitionUsage] = []
        with closing(self._connect()) as connection:
            if self._table_exists(connection, 'cad_equipment_binding_semantics'):
                for row in connection.execute(
                    """
                    SELECT binding_id, semantic_sha256, document_id, entity_id
                    FROM cad_equipment_binding_semantics
                    WHERE equipment_definition_sha256=? ORDER BY seq ASC
                    """,
                    (definition_sha256,),
                ):
                    usages.append(
                        EquipmentDefinitionUsage(
                            binding_kind='binding_semantics',
                            authority_id=row['binding_id'],
                            authority_sha256=row['semantic_sha256'],
                            document_id=row['document_id'],
                            entity_id=row['entity_id'],
                        )
                    )
            if self._table_exists(connection, 'cad_installation_contexts'):
                for row in connection.execute(
                    """
                    SELECT context_id, semantic_sha256, document_id, entity_id
                    FROM cad_installation_contexts
                    WHERE equipment_definition_sha256=? ORDER BY seq ASC
                    """,
                    (definition_sha256,),
                ):
                    usages.append(
                        EquipmentDefinitionUsage(
                            binding_kind='installation_context',
                            authority_id=row['context_id'],
                            authority_sha256=row['semantic_sha256'],
                            document_id=row['document_id'],
                            entity_id=row['entity_id'],
                        )
                    )
            if self._table_exists(
                connection, 'cad_r110_compiled_source_models'
            ):
                for row in connection.execute(
                    """
                    SELECT semantic_sha256, source_entity_id
                    FROM cad_r110_compiled_source_models
                    WHERE equipment_definition_sha256=? ORDER BY seq ASC
                    """,
                    (definition_sha256,),
                ):
                    usages.append(
                        EquipmentDefinitionUsage(
                            binding_kind='r110_source_model',
                            authority_id=row['semantic_sha256'],
                            authority_sha256=row['semantic_sha256'],
                            entity_id=row['source_entity_id'],
                        )
                    )
            if self._table_exists(connection, 'cad_system_variants'):
                needle = f'%{definition_sha256}%'
                for row in connection.execute(
                    """
                    SELECT variant_id, variant_sha256, document_id,
                           payload_json
                    FROM cad_system_variants
                    WHERE payload_json LIKE ? ORDER BY seq ASC
                    """,
                    (needle,),
                ):
                    if definition_sha256 not in row['payload_json']:
                        continue
                    usages.append(
                        EquipmentDefinitionUsage(
                            binding_kind='system_variant_equipment',
                            authority_id=row['variant_id'],
                            authority_sha256=row['variant_sha256'],
                            document_id=row['document_id'],
                        )
                    )
            if self._table_exists(
                connection, 'cad_installed_equipment_instances'
            ):
                needle = f'%{definition_sha256}%'
                for row in connection.execute(
                    """
                    SELECT instance_id, document_id, payload_json
                    FROM cad_installed_equipment_instances
                    WHERE payload_json LIKE ? ORDER BY seq ASC
                    """,
                    (needle,),
                ):
                    if definition_sha256 not in row['payload_json']:
                        continue
                    usages.append(
                        EquipmentDefinitionUsage(
                            binding_kind='installed_instance',
                            authority_id=row['instance_id'],
                            authority_sha256=(
                                InstalledEquipmentInstance.model_validate_json(
                                    row['payload_json']
                                ).semantic_sha256
                            ),
                            document_id=row['document_id'],
                        )
                    )
            if self._table_exists(connection, 'cad_current_topologies'):
                needle = f'%{definition_sha256}%'
                for row in connection.execute(
                    """
                    SELECT topology_id, semantic_sha256, document_id,
                           payload_json
                    FROM cad_current_topologies
                    WHERE payload_json LIKE ? ORDER BY seq ASC
                    """,
                    (needle,),
                ):
                    if definition_sha256 not in row['payload_json']:
                        continue
                    usages.append(
                        EquipmentDefinitionUsage(
                            binding_kind='current_topology',
                            authority_id=row['topology_id'],
                            authority_sha256=row['semantic_sha256'],
                            document_id=row['document_id'],
                        )
                    )
            if self._table_exists(connection, 'cad_directivity_datasets'):
                for row in connection.execute(
                    """
                    SELECT dataset_id, semantic_sha256
                    FROM cad_directivity_datasets
                    WHERE equipment_definition_sha256=? ORDER BY seq ASC
                    """,
                    (definition_sha256,),
                ):
                    usages.append(
                        EquipmentDefinitionUsage(
                            binding_kind='directivity_dataset',
                            authority_id=row['dataset_id'],
                            authority_sha256=row['semantic_sha256'],
                        )
                    )
        return tuple(usages)

    def save_adoption(
        self,
        adoption: UpgradeAdoptionRecord,
    ) -> UpgradeAdoptionRecord:
        adoption = UpgradeAdoptionRecord.model_validate(
            adoption.model_dump(mode='python')
        )
        if self.get_upgrade_by_hash(adoption.upgrade_sha256) is None:
            raise ValueError(
                'adoption references an unpersisted EquipmentDefinitionUpgrade'
            )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_upgrade_adoptions
                WHERE adoption_id=?
                """,
                (adoption.adoption_id,),
            ).fetchone()
            if existing is not None:
                persisted = UpgradeAdoptionRecord.model_validate_json(
                    existing['payload_json']
                )
                if persisted != adoption:
                    raise ValueError(
                        'upgrade adoption id already exists with different '
                        'semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_upgrade_adoptions(
                    adoption_id, semantic_sha256, upgrade_sha256,
                    document_id, decision, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    adoption.adoption_id,
                    adoption.semantic_sha256,
                    adoption.upgrade_sha256,
                    adoption.document_id,
                    adoption.decision,
                    adoption.model_dump_json(),
                ),
            )
        return adoption

    def get_upgrade_by_hash(
        self,
        semantic_sha256: str,
    ) -> EquipmentDefinitionUpgrade | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_equipment_upgrades
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else EquipmentDefinitionUpgrade.model_validate_json(
                row['payload_json']
            )
        )

    def adoptions_for_document(
        self,
        document_id: str,
    ) -> tuple[UpgradeAdoptionRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_upgrade_adoptions '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            UpgradeAdoptionRecord.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = ['CadLibraryUpgradeRepository']
