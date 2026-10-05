"""Persistence for speaker equipment binding semantics (#476)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_equipment_binding import EquipmentBindingSemantics
from .cad_equipment_repository import CadEquipmentRepository
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite


#: ``_decode_binding_row(head=...)`` sentinel distinguishing "fetch the
#: document head now" from a caller-supplied head (which may be None).
_HEAD_UNSET = object()


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
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_equipment_binding_semantics')

    def _resolve_bound_definition(
        self,
        binding: EquipmentBindingSemantics,
        _cache: dict | None = None,
    ):
        """Exact authority ref: hash resolves AND id/version match (#841)."""
        key = binding.equipment.semantic_sha256
        if _cache is not None and key in _cache:
            definition = _cache[key]
        else:
            definition = self.equipment_repository.get_definition_by_hash(key)
            if _cache is not None:
                _cache[key] = definition
        if definition is None:
            raise ValueError(
                'binding semantics references an unpersisted EquipmentDefinition'
            )
        if (
            definition.definition_id != binding.equipment.authority_id
            or definition.version != binding.equipment.version
        ):
            raise ValueError(
                'binding semantics equipment ref resolves to a different '
                'EquipmentDefinition identity than declared'
            )
        return definition

    def save_binding(
        self,
        binding: EquipmentBindingSemantics,
    ) -> EquipmentBindingSemantics:
        binding = EquipmentBindingSemantics.model_validate(
            binding.model_dump(mode='python')
        )
        self._resolve_bound_definition(binding)
        head = self.scene_repository.current_head(binding.document_id)
        if head is None:
            raise ValueError(
                f'document {binding.document_id} has no scene revision to bind'
            )
        entities = {
            item.entity_id: item for item in head.document.entities
        }
        entity = entities.get(binding.entity_id)
        if entity is None:
            raise ValueError(
                f'scene entity {binding.entity_id} is not present in the '
                f'current scene revision of {binding.document_id}'
            )
        if entity.kind != 'speaker':
            raise ValueError(
                f'scene entity {binding.entity_id} is not a speaker; '
                'equipment binding semantics only bind speaker entities'
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

    def _decode_binding_row(
        self,
        row: sqlite3.Row,
        *,
        head=_HEAD_UNSET,
        _definition_cache: dict | None = None,
    ) -> EquipmentBindingSemantics:
        """Fail-closed read: row columns and exact refs re-validate (#841)."""
        binding = EquipmentBindingSemantics.model_validate_json(
            row['payload_json']
        )
        if (
            row['binding_id'] != binding.binding_id
            or row['semantic_sha256'] != binding.semantic_sha256
            or row['document_id'] != binding.document_id
            or row['entity_id'] != binding.entity_id
            or row['equipment_definition_sha256']
            != binding.equipment.semantic_sha256
        ):
            raise ValueError(
                'persisted equipment binding row disagrees with its payload'
            )
        # The exact definition pin must still resolve verbatim; a missing
        # definition means the stored authority is corrupt, never a
        # softer binding.
        self._resolve_bound_definition(binding, _definition_cache)
        # When the entity still exists in the document's head it must
        # remain a speaker; an entity removed from the scene is history,
        # but one retyped to a non-speaker can never re-resolve as a
        # speaker-equipment binding.
        if head is _HEAD_UNSET:
            head = self.scene_repository.current_head(binding.document_id)
        if head is not None:
            entity = next(
                (
                    item
                    for item in head.document.entities
                    if item.entity_id == binding.entity_id
                ),
                None,
            )
            if entity is not None and entity.kind != 'speaker':
                raise ValueError(
                    'persisted equipment binding entity is no longer a '
                    'speaker'
                )
        return binding

    def get_binding(self, binding_id: str) -> EquipmentBindingSemantics | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT binding_id, semantic_sha256, document_id, entity_id,
                    equipment_definition_sha256, payload_json
                FROM cad_equipment_binding_semantics
                WHERE binding_id=?
                """,
                (binding_id,),
            ).fetchone()
        return None if row is None else self._decode_binding_row(row)

    def latest_binding_for_current_entity(
        self,
        document_id: str,
        entity_id: str,
    ) -> EquipmentBindingSemantics | None:
        """Current-editing view: newest binding semantics for the entity."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT binding_id, semantic_sha256, document_id, entity_id,
                    equipment_definition_sha256, payload_json
                FROM cad_equipment_binding_semantics
                WHERE document_id=? AND entity_id=?
                ORDER BY seq DESC LIMIT 1
                """,
                (document_id, entity_id),
            ).fetchone()
        return None if row is None else self._decode_binding_row(row)

    def latest_bindings_for_document(
        self,
        document_id: str,
    ) -> dict[str, EquipmentBindingSemantics]:
        """Current-editing view for the whole document: newest binding
        semantics per entity id, resolved in one listing.

        Surfaces iterating every speaker (install summary, record lists)
        read each entity separately — each row then re-fetched the
        document head and re-resolved its EquipmentDefinition. This path
        shares one head and one definition cache across the batch; the
        fail-closed row checks are unchanged.
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT binding_id, semantic_sha256, document_id, entity_id,
                    equipment_definition_sha256, payload_json
                FROM cad_equipment_binding_semantics
                WHERE document_id=?
                ORDER BY seq DESC
                """,
                (document_id,),
            ).fetchall()
        head = self.scene_repository.current_head(document_id)
        definitions: dict = {}
        latest: dict[str, EquipmentBindingSemantics] = {}
        for row in rows:
            if row['entity_id'] in latest:
                continue
            latest[row['entity_id']] = self._decode_binding_row(
                row, head=head, _definition_cache=definitions
            )
        return latest

    def latest_binding_for_entity_as_of(
        self,
        document_id: str,
        entity_id: str,
        max_created_utc: str,
    ) -> EquipmentBindingSemantics | None:
        """Revision-aware view: newest binding semantics recorded at or
        before ``max_created_utc`` — the record visible at that revision."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT binding_id, semantic_sha256, document_id, entity_id,
                    equipment_definition_sha256, payload_json
                FROM cad_equipment_binding_semantics
                WHERE document_id=? AND entity_id=? AND recorded_at_utc<=?
                ORDER BY recorded_at_utc DESC, seq DESC LIMIT 1
                """,
                (document_id, entity_id, max_created_utc),
            ).fetchone()
        return None if row is None else self._decode_binding_row(row)

    def binding_by_exact_hash(
        self,
        semantic_sha256: str,
    ) -> EquipmentBindingSemantics | None:
        """Historical-replay view: resolve a binding by its semantic hash."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT binding_id, semantic_sha256, document_id, entity_id,
                    equipment_definition_sha256, payload_json
                FROM cad_equipment_binding_semantics
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        return None if row is None else self._decode_binding_row(row)

    def get_binding_for_entity(
        self,
        document_id: str,
        entity_id: str,
    ) -> EquipmentBindingSemantics | None:
        """Deprecated alias for ``latest_binding_for_current_entity``."""
        return self.latest_binding_for_current_entity(document_id, entity_id)

    def get_binding_by_hash(
        self,
        semantic_sha256: str,
    ) -> EquipmentBindingSemantics | None:
        """Deprecated alias for ``binding_by_exact_hash``."""
        return self.binding_by_exact_hash(semantic_sha256)


__all__ = ['CadEquipmentBindingRepository']
