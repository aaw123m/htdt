"""Speaker equipment binding semantics (#476).

Generic scene geometry (placeholder ``size_m``, placeholder
``acoustic_reference_offset_m``) and EquipmentDefinition authority are two
different authorities that previously collided ambiguously — e.g. R110 source
compilation raised on any offset mismatch and a placeholder envelope could
silently masquerade as the modeled speaker.

This module makes the binding explicit:

- ``EquipmentBindingSemantics`` — an append-only project record declaring, per
  bound speaker entity, which authority owns body geometry and the acoustic
  reference point.
- ``reconcile_speaker_binding`` — derives the updated scene entities for a new
  revision: equipment authority can be adopted (size/reference rewritten) or
  the scene values can be declared as-authored/as-built — but placement anchors
  (``position``, ``orientation``, ``aim_xyz``) are never rewritten.
- ``compile_r110_source_model`` honors the semantics: with an explicit binding,
  equipment-derived references no longer error on a stale scene offset, while a
  scene-explicit override still fails closed instead of silently overriding
  equipment authority.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_amplifier_headroom import AuthorityRef
from .cad_equipment import EquipmentDataProvenance, EquipmentDefinition
from .cad_scene import SceneDocument, SceneEntity


EQUIPMENT_BINDING_SCHEMA_VERSION = 1
EQUIPMENT_BINDING_SEMANTICS_VERSION = 'speaker-equipment-binding-semantics-1'

BodyGeometryAuthority = Literal[
    'generic_placeholder',
    'equipment_nominal',
    'user_authored',
    'observed_asbuilt',
]
AcousticReferenceAuthority = Literal[
    'equipment_derived',
    'scene_explicit',
    'approximate_placeholder',
]


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


class EquipmentBindingSemantics(BaseModel):
    """Explicit authority split between scene geometry and equipment."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = EQUIPMENT_BINDING_SCHEMA_VERSION
    authority_version: Literal[
        'speaker-equipment-binding-semantics-1'
    ] = EQUIPMENT_BINDING_SEMANTICS_VERSION
    binding_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    equipment: AuthorityRef
    body_geometry_authority: BodyGeometryAuthority
    acoustic_reference_authority: AcousticReferenceAuthority
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_binding(self) -> 'EquipmentBindingSemantics':
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('EquipmentBindingSemantics semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'binding_id': self.binding_id,
            'document_id': self.document_id,
            'entity_id': self.entity_id,
            'equipment': self.equipment.model_dump(mode='json'),
            'body_geometry_authority': self.body_geometry_authority,
            'acoustic_reference_authority': self.acoustic_reference_authority,
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'created_at_utc': self.created_at_utc,
        }

    @property
    def equipment_owns_acoustic_reference(self) -> bool:
        return self.acoustic_reference_authority in (
            'equipment_derived',
            'approximate_placeholder',
        )


def build_equipment_binding_semantics(
    *,
    binding_id: str,
    document_id: str,
    entity_id: str,
    equipment_definition: EquipmentDefinition,
    body_geometry_authority: BodyGeometryAuthority,
    acoustic_reference_authority: AcousticReferenceAuthority,
    provenance: Sequence[EquipmentDataProvenance],
    created_at_utc: str,
) -> EquipmentBindingSemantics:
    provenance_items = tuple(provenance)
    equipment = AuthorityRef(
        authority_id=equipment_definition.definition_id,
        version=equipment_definition.version,
        semantic_sha256=equipment_definition.semantic_sha256,
    )
    payload = {
        'schema_version': EQUIPMENT_BINDING_SCHEMA_VERSION,
        'authority_version': EQUIPMENT_BINDING_SEMANTICS_VERSION,
        'binding_id': binding_id,
        'document_id': document_id,
        'entity_id': entity_id,
        'equipment': equipment.model_dump(mode='json'),
        'body_geometry_authority': body_geometry_authority,
        'acoustic_reference_authority': acoustic_reference_authority,
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
        'created_at_utc': created_at_utc,
    }
    return EquipmentBindingSemantics(
        binding_id=binding_id,
        document_id=document_id,
        entity_id=entity_id,
        equipment=equipment,
        body_geometry_authority=body_geometry_authority,
        acoustic_reference_authority=acoustic_reference_authority,
        provenance=provenance_items,
        created_at_utc=created_at_utc,
        semantic_sha256=_digest(payload),
    )


def reconcile_speaker_binding(
    *,
    document: SceneDocument,
    entity_id: str,
    equipment_definition: EquipmentDefinition,
    semantics: EquipmentBindingSemantics,
) -> SceneDocument:
    """Return a new SceneDocument with the declared authority applied.

    Only ``size_m`` (body geometry authority) and
    ``acoustic_reference_offset_m`` (acoustic reference authority) may change;
    the placement anchor — position, orientation, aim axis — is preserved
    verbatim so adopting equipment authority never moves the physical object.
    """
    if semantics.document_id != document.document_id:
        raise ValueError('binding semantics document mismatch')
    if semantics.entity_id != entity_id:
        raise ValueError('binding semantics entity mismatch')
    if (
        semantics.equipment.authority_id != equipment_definition.definition_id
        or semantics.equipment.version != equipment_definition.version
        or semantics.equipment.semantic_sha256
        != equipment_definition.semantic_sha256
    ):
        raise ValueError(
            'binding semantics does not reference the supplied '
            'EquipmentDefinition exactly'
        )
    entity = document.entity(entity_id)
    if entity.kind != 'speaker':
        raise ValueError('equipment binding reconciliation requires a speaker')

    size_m = entity.size_m
    offset = entity.acoustic_reference_offset_m
    if semantics.body_geometry_authority == 'equipment_nominal':
        size_m = equipment_definition.cabinet_envelope_m
    if semantics.equipment_owns_acoustic_reference:
        offset = equipment_definition.acoustic_reference_point_m

    updated = entity.model_copy(
        update={
            'size_m': size_m,
            'acoustic_reference_offset_m': offset,
        }
    )
    entities = tuple(
        updated if item.entity_id == entity_id else item
        for item in document.entities
    )
    return document.model_copy(update={'entities': entities})


__all__ = [
    'AcousticReferenceAuthority',
    'BodyGeometryAuthority',
    'EQUIPMENT_BINDING_SCHEMA_VERSION',
    'EQUIPMENT_BINDING_SEMANTICS_VERSION',
    'EquipmentBindingSemantics',
    'build_equipment_binding_semantics',
    'reconcile_speaker_binding',
]
