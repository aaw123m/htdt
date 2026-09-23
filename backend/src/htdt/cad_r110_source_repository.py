from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .cad_directivity import DirectivityDataset
from .cad_directivity_repository import CadDirectivityRepository
from .cad_equipment import EquipmentDefinition
from .cad_equipment_repository import CadEquipmentRepository
from .cad_r110_source import (
    R110CompiledSourceModel,
    compile_r110_source_model,
)
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema
from .cad_system_variant import SystemVariant, materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadR110SourceRepository:
    """Append-only persistence for exact compiled R110 source-model authority."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository | None = None,
        equipment_repository: CadEquipmentRepository | None = None,
        directivity_repository: CadDirectivityRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = (
            variant_repository
            if variant_repository is not None
            else CadSystemVariantRepository(scene_repository)
        )
        self.equipment_repository = (
            equipment_repository
            if equipment_repository is not None
            else CadEquipmentRepository(
                scene_repository,
                self.variant_repository,
            )
        )
        self.directivity_repository = (
            directivity_repository
            if directivity_repository is not None
            else CadDirectivityRepository(
                scene_repository,
                self.equipment_repository,
            )
        )
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
                CREATE TABLE IF NOT EXISTS cad_r110_compiled_source_models (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    scene_revision_id TEXT NOT NULL,
                    scene_content_hash TEXT NOT NULL,
                    system_variant_id TEXT NOT NULL,
                    system_variant_sha256 TEXT NOT NULL,
                    source_entity_id TEXT NOT NULL,
                    equipment_definition_sha256 TEXT NOT NULL,
                    directivity_dataset_sha256 TEXT,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(scene_revision_id)
                        REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(system_variant_id)
                        REFERENCES cad_system_variants(variant_id)
                );
                CREATE INDEX IF NOT EXISTS idx_r110_source_variant_entity
                    ON cad_r110_compiled_source_models(
                        system_variant_id, source_entity_id, seq ASC
                    );
                """
            )

    def _resolve_exact_authorities(
        self,
        model: R110CompiledSourceModel,
    ) -> tuple[object, SystemVariant, EquipmentDefinition, DirectivityDataset | None]:
        scene_revision = self.scene_repository.get(model.scene_revision_id)
        if scene_revision is None:
            raise ValueError(
                'persisted R110 source references missing SceneRevision'
            )
        if scene_revision.content_hash != model.scene_content_hash:
            raise ValueError('persisted R110 source SceneRevision hash mismatch')

        variant = self.variant_repository.get_variant(model.system_variant_id)
        if variant is None:
            raise ValueError(
                'persisted R110 source references missing SystemVariant'
            )
        if variant.variant_sha256 != model.system_variant_sha256:
            raise ValueError('persisted R110 source SystemVariant hash mismatch')
        if (
            variant.baseline_revision_id != scene_revision.revision_id
            or variant.baseline_content_hash != scene_revision.content_hash
        ):
            raise ValueError(
                'persisted R110 source SceneRevision/SystemVariant binding mismatch'
            )

        derived_scene = materialize_system_variant(scene_revision, variant)
        try:
            source_entity = derived_scene.entity(model.source_entity_id)
        except KeyError as exc:
            raise ValueError(
                'persisted R110 source entity is missing from exact SystemVariant'
            ) from exc
        if source_entity.kind != 'speaker':
            raise ValueError('persisted R110 source entity is not a speaker')

        bindings = [
            item
            for item in variant.equipment_bindings
            if item.entity_id == model.source_entity_id
        ]
        if len(bindings) != 1:
            raise ValueError(
                'persisted R110 source requires one exact equipment binding'
            )
        binding = bindings[0]
        if binding.equipment_definition_sha256 != model.equipment_definition_sha256:
            raise ValueError(
                'persisted R110 source equipment binding hash mismatch'
            )

        definition = self.equipment_repository.get_definition_by_hash(
            model.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'persisted R110 source references missing EquipmentDefinition'
            )
        if (
            definition.definition_id != model.equipment_definition_id
            or definition.version != model.equipment_definition_version
            or binding.equipment_definition_id != definition.definition_id
            or binding.equipment_definition_version != definition.version
        ):
            raise ValueError(
                'persisted R110 source EquipmentDefinition identity mismatch'
            )

        dataset: DirectivityDataset | None = None
        if model.directivity_dataset_sha256 is not None:
            dataset = self.directivity_repository.get_dataset_by_hash(
                model.directivity_dataset_sha256
            )
            if dataset is None:
                raise ValueError(
                    'persisted R110 source references missing DirectivityDataset'
                )
            if (
                dataset.dataset_id != model.directivity_dataset_id
                or dataset.version != model.directivity_dataset_version
                or dataset.source_asset_sha256
                != model.directivity_source_asset_sha256
            ):
                raise ValueError(
                    'persisted R110 source DirectivityDataset identity mismatch'
                )

        return scene_revision, variant, definition, dataset

    def _validate_exact_authorities(
        self,
        model: R110CompiledSourceModel,
    ) -> R110CompiledSourceModel:
        (
            scene_revision,
            variant,
            definition,
            dataset,
        ) = self._resolve_exact_authorities(model)
        recompiled = compile_r110_source_model(
            scene_revision=scene_revision,
            system_variant=variant,
            source_entity_id=model.source_entity_id,
            equipment_definition=definition,
            directivity_dataset=dataset,
        )
        if recompiled != model:
            raise ValueError(
                'persisted R110 source does not reproduce from exact authorities'
            )
        return model

    def compile_for_variant_source(
        self,
        *,
        system_variant_id: str,
        source_entity_id: str,
        directivity_dataset_sha256: str | None = None,
    ) -> R110CompiledSourceModel:
        variant = self.variant_repository.get_variant(system_variant_id)
        if variant is None:
            raise ValueError('SystemVariant does not exist')
        scene_revision = self.scene_repository.get(variant.baseline_revision_id)
        if scene_revision is None:
            raise ValueError('SystemVariant baseline SceneRevision does not exist')
        if scene_revision.content_hash != variant.baseline_content_hash:
            raise ValueError('SystemVariant baseline authority mismatch')

        bindings = [
            item
            for item in variant.equipment_bindings
            if item.entity_id == source_entity_id
        ]
        if len(bindings) != 1:
            raise ValueError(
                'exactly one persisted equipment binding is required for source entity'
            )
        binding = bindings[0]
        definition = self.equipment_repository.get_definition_by_hash(
            binding.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'SystemVariant equipment binding references missing EquipmentDefinition'
            )
        if (
            definition.definition_id != binding.equipment_definition_id
            or definition.version != binding.equipment_definition_version
        ):
            raise ValueError('SystemVariant equipment binding identity mismatch')

        dataset = None
        if directivity_dataset_sha256 is not None:
            dataset = self.directivity_repository.get_dataset_by_hash(
                directivity_dataset_sha256
            )
            if dataset is None:
                raise ValueError('DirectivityDataset does not exist')

        return compile_r110_source_model(
            scene_revision=scene_revision,
            system_variant=variant,
            source_entity_id=source_entity_id,
            equipment_definition=definition,
            directivity_dataset=dataset,
        )

    def save_model(
        self,
        model: R110CompiledSourceModel,
    ) -> R110CompiledSourceModel:
        model = R110CompiledSourceModel.model_validate(
            model.model_dump(mode='python')
        )
        self._validate_exact_authorities(model)

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_r110_compiled_source_models
                WHERE semantic_sha256=?
                """,
                (model.semantic_sha256,),
            ).fetchone()
            if existing is not None:
                persisted = R110CompiledSourceModel.model_validate_json(
                    existing['payload_json']
                )
                if persisted != model:
                    raise ValueError(
                        'R110 semantic hash already exists with different semantics'
                    )
                return self._validate_exact_authorities(persisted)

            connection.execute(
                """
                INSERT INTO cad_r110_compiled_source_models(
                    semantic_sha256,
                    scene_revision_id,
                    scene_content_hash,
                    system_variant_id,
                    system_variant_sha256,
                    source_entity_id,
                    equipment_definition_sha256,
                    directivity_dataset_sha256,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model.semantic_sha256,
                    model.scene_revision_id,
                    model.scene_content_hash,
                    model.system_variant_id,
                    model.system_variant_sha256,
                    model.source_entity_id,
                    model.equipment_definition_sha256,
                    model.directivity_dataset_sha256,
                    model.model_dump_json(),
                    _utc_now(),
                ),
            )
        return model

    def get_model(
        self,
        semantic_sha256: str,
    ) -> R110CompiledSourceModel | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_r110_compiled_source_models
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        model = R110CompiledSourceModel.model_validate_json(row['payload_json'])
        return self._validate_exact_authorities(model)
