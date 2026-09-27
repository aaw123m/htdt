from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_directivity import DirectivityDataset
from .cad_directivity_repository import CadDirectivityRepository
from .cad_equipment import EquipmentDefinition
from .cad_equipment_binding import EquipmentBindingSemantics
from .cad_equipment_binding_repository import CadEquipmentBindingRepository
from .cad_equipment_repository import CadEquipmentRepository
from .cad_installation_context import SpeakerInstallationContext
from .cad_installation_context_repository import (
    CadInstallationContextRepository,
)
from .cad_r110_source import (
    R110CompiledSourceModel,
    compile_r110_source_model,
)
from .cad_repository import SceneRepository
from .cad_source_response import CadSourceResponseRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,

)
from .cad_system_variant import SystemVariant, materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository
from .clock import utc_now_iso as _utc_now


class CadR110SourceRepository:
    """Append-only persistence for exact compiled R110 source-model authority."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository | None = None,
        equipment_repository: CadEquipmentRepository | None = None,
        directivity_repository: CadDirectivityRepository | None = None,
        binding_repository: CadEquipmentBindingRepository | None = None,
        installation_repository: CadInstallationContextRepository | None = None,
        source_response_repository: CadSourceResponseRepository | None = None,
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
        self.binding_repository = (
            binding_repository
            if binding_repository is not None
            else CadEquipmentBindingRepository(
                scene_repository,
                self.equipment_repository,
            )
        )
        self.installation_repository = (
            installation_repository
            if installation_repository is not None
            else CadInstallationContextRepository(
                scene_repository,
                self.equipment_repository,
            )
        )
        self.source_response_repository = (
            source_response_repository
            if source_response_repository is not None
            else CadSourceResponseRepository(
                scene_repository.path, self.equipment_repository
            )
        )
        self.path = Path(scene_repository.path)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_r110_compiled_source_models')

    def _resolve_exact_authorities(
        self,
        model: R110CompiledSourceModel,
    ) -> tuple[
        object,
        SystemVariant,
        EquipmentDefinition,
        DirectivityDataset | None,
        EquipmentBindingSemantics | None,
        SpeakerInstallationContext | None,
    ]:
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

        binding_semantics: EquipmentBindingSemantics | None = None
        if model.equipment_binding_semantics_sha256 is not None:
            binding_semantics = self.binding_repository.get_binding_by_hash(
                model.equipment_binding_semantics_sha256
            )
            if binding_semantics is None:
                raise ValueError(
                    'persisted R110 source references missing equipment '
                    'binding semantics'
                )
        installation_context: SpeakerInstallationContext | None = None
        if model.installation_context_sha256 is not None:
            installation_context = (
                self.installation_repository.get_context_for_entity(
                    scene_revision.document_id,
                    model.source_entity_id,
                )
            )
            if (
                installation_context is None
                or installation_context.semantic_sha256
                != model.installation_context_sha256
            ):
                raise ValueError(
                    'persisted R110 source references missing installation '
                    'context'
                )
        source_response = None
        if model.source_response_authority_sha256 is not None:
            source_response = (
                self.source_response_repository.get_response_by_sha256(
                    model.source_response_authority_sha256
                )
            )
            if (
                source_response is None
                or source_response.response_id
                != model.source_response_authority_id
                or source_response.authority_version
                != model.source_response_authority_version
                or source_response.capability_tier
                != model.source_response_capability_tier
            ):
                raise ValueError(
                    'persisted R110 source references missing source '
                    'response authority'
                )

        return (
            scene_revision,
            variant,
            definition,
            dataset,
            binding_semantics,
            installation_context,
            source_response,
        )

    def _validate_exact_authorities(
        self,
        model: R110CompiledSourceModel,
    ) -> R110CompiledSourceModel:
        (
            scene_revision,
            variant,
            definition,
            dataset,
            binding_semantics,
            installation_context,
            source_response,
        ) = self._resolve_exact_authorities(model)
        recompiled = compile_r110_source_model(
            scene_revision=scene_revision,
            system_variant=variant,
            source_entity_id=model.source_entity_id,
            equipment_definition=definition,
            directivity_dataset=dataset,
            binding_semantics=binding_semantics,
            installation_context=installation_context,
            source_response=source_response,
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
        source_response_sha256: str | None = None,
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

        source_response = None
        if source_response_sha256 is not None:
            source_response = (
                self.source_response_repository.get_response_by_sha256(
                    source_response_sha256
                )
            )
            if source_response is None:
                raise ValueError('SourceFrequencyResponseAuthority does not exist')

        binding_semantics = self.binding_repository.get_binding_for_entity(
            scene_revision.document_id,
            source_entity_id,
        )
        installation_context = (
            self.installation_repository.get_context_for_entity(
                scene_revision.document_id,
                source_entity_id,
            )
        )

        return compile_r110_source_model(
            scene_revision=scene_revision,
            system_variant=variant,
            source_entity_id=source_entity_id,
            equipment_definition=definition,
            directivity_dataset=dataset,
            binding_semantics=binding_semantics,
            installation_context=installation_context,
            source_response=source_response,
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
