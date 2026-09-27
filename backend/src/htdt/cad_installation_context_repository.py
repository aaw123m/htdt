"""Persistence for speaker installation context authority (#540)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_installation_context import (
    InstallationEvaluation,
    SpeakerInstallationContext,
    evaluate_installation_context,
)
from .cad_equipment_repository import CadEquipmentRepository
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite


class CadInstallationContextRepository:
    """Append-only installation-context persistence.

    Contexts bind an entity + exact EquipmentDefinition inside one project
    document. Evaluations are replayed from the persisted context, entity and
    definition on every authoritative read — never trusted as stored payloads.
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
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_installation_contexts')

    def _check_context(self, context: SpeakerInstallationContext) -> None:
        if self.equipment_repository.get_definition_by_hash(
            context.equipment.equipment_definition_sha256
        ) is None:
            raise ValueError(
                'installation context references an unpersisted '
                'EquipmentDefinition'
            )

    def save_context(
        self,
        context: SpeakerInstallationContext,
    ) -> SpeakerInstallationContext:
        context = SpeakerInstallationContext.model_validate(
            context.model_dump(mode='python')
        )
        self._check_context(context)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_installation_contexts
                WHERE context_id=? AND version_key=?
                """,
                (context.context_id, context.authority_version),
            ).fetchone()
            if existing is not None:
                persisted = SpeakerInstallationContext.model_validate_json(
                    existing['payload_json']
                )
                if persisted != context:
                    raise ValueError(
                        'installation context id already exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_installation_contexts(
                    context_id, version_key, semantic_sha256, document_id,
                    entity_id, equipment_definition_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.context_id,
                    context.authority_version,
                    context.semantic_sha256,
                    context.document_id,
                    context.entity_id,
                    context.equipment.equipment_definition_sha256,
                    context.model_dump_json(),
                ),
            )
        return context

    def get_context(self, context_id: str) -> SpeakerInstallationContext | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_installation_contexts
                WHERE context_id=? ORDER BY seq DESC LIMIT 1
                """,
                (context_id,),
            ).fetchone()
        return (
            None
            if row is None
            else SpeakerInstallationContext.model_validate_json(
                row['payload_json']
            )
        )

    def get_context_for_entity(
        self,
        document_id: str,
        entity_id: str,
    ) -> SpeakerInstallationContext | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_installation_contexts
                WHERE document_id=? AND entity_id=?
                ORDER BY seq DESC LIMIT 1
                """,
                (document_id, entity_id),
            ).fetchone()
        return (
            None
            if row is None
            else SpeakerInstallationContext.model_validate_json(
                row['payload_json']
            )
        )

    def evaluate_for_entity(
        self,
        document_id: str,
        entity_id: str,
    ) -> InstallationEvaluation | None:
        """Replay the persisted context's evaluation on current authority."""
        context = self.get_context_for_entity(document_id, entity_id)
        if context is None:
            return None
        definition = self.equipment_repository.get_definition_by_hash(
            context.equipment.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'installation context references an unpersisted '
                'EquipmentDefinition'
            )
        head = self.scene_repository.current_head(document_id)
        if head is None:
            raise ValueError('document has no current scene revision')
        entity = next(
            (
                item
                for item in head.document.entities
                if item.entity_id == entity_id
            ),
            None,
        )
        if entity is None:
            raise ValueError(
                'installation context entity is absent from the current '
                'scene revision'
            )
        return evaluate_installation_context(
            document=head.document,
            entity=entity,
            equipment_definition=definition,
            context=context,
        )


__all__ = ['CadInstallationContextRepository']
