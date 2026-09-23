from __future__ import annotations

import json
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import (
    EQUIPMENT_DEFINITION_AUTHORITY_VERSION,
    EquipmentDefinition,
)


class EquipmentCatalogEntry(BaseModel):
    """Portable exact reference to one persisted EquipmentDefinition."""

    model_config = ConfigDict(frozen=True)

    definition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    identity_kind: Literal['manufacturer', 'user_defined']
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    user_label: str | None = Field(default=None, min_length=1)

    @property
    def display_name(self) -> str:
        if self.manufacturer is not None and self.model is not None:
            return f'{self.manufacturer} {self.model}'
        if self.user_label is not None:
            return self.user_label
        return self.definition_id


class EquipmentCatalogSnapshot(BaseModel):
    """Deterministic offline picker snapshot; not an equipment authority itself."""

    model_config = ConfigDict(frozen=True)

    schema: Literal['htdt.equipment.catalog-snapshot'] = (
        'htdt.equipment.catalog-snapshot'
    )
    schema_version: Literal[1] = 1
    authority_version: Literal[
        'o100c-equipment-definition-1'
    ] = EQUIPMENT_DEFINITION_AUTHORITY_VERSION
    definitions: tuple[EquipmentCatalogEntry, ...]

    @model_validator(mode='after')
    def unique_exact_references(self) -> 'EquipmentCatalogSnapshot':
        identities = [
            (item.definition_id, item.version)
            for item in self.definitions
        ]
        hashes = [item.semantic_sha256 for item in self.definitions]
        if len(identities) != len(set(identities)):
            raise ValueError('equipment catalog contains duplicate id/version')
        if len(hashes) != len(set(hashes)):
            raise ValueError('equipment catalog contains duplicate semantic hash')
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode='json'),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')


def build_equipment_catalog_snapshot(
    definitions: Sequence[EquipmentDefinition],
) -> EquipmentCatalogSnapshot:
    entries = [
        EquipmentCatalogEntry(
            definition_id=item.definition_id,
            version=item.version,
            semantic_sha256=item.semantic_sha256,
            identity_kind=item.identity_kind,
            manufacturer=item.manufacturer,
            model=item.model,
            user_label=item.user_label,
        )
        for item in definitions
    ]
    entries.sort(
        key=lambda item: (
            item.manufacturer or '',
            item.model or '',
            item.user_label or '',
            item.definition_id,
            item.version,
            item.semantic_sha256,
        )
    )
    return EquipmentCatalogSnapshot(definitions=tuple(entries))
