from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .build_info import get_build_info
from .cad_equipment_repository import CadEquipmentRepository
from .cad_repository import SceneRepository


class EquipmentCatalogExportResult(BaseModel):
    """Operator-facing record of one catalog-snapshot export.

    The snapshot file itself stays byte-exact ``EquipmentCatalogSnapshot``;
    provenance identifying the generating HTDT release/database authority is
    reported here so the export can be audited without changing the exact
    equipment reference tuple semantics Capture binds against.
    """

    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    schema_: Literal['htdt.equipment.catalog-export-result'] = Field(default='htdt.equipment.catalog-export-result', alias='schema')
    schema_version: Literal[1] = 1
    snapshot_schema: Literal['htdt.equipment.catalog-snapshot'] = (
        'htdt.equipment.catalog-snapshot'
    )
    authority_version: str = Field(min_length=1)
    definition_count: int = Field(ge=0)
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    byte_count: int = Field(ge=0)
    output_path: str = Field(min_length=1)
    generated_by: str = Field(min_length=1)


def export_equipment_catalog_snapshot(
    scene_repository: SceneRepository,
    output_path: Path,
) -> EquipmentCatalogExportResult:
    """Write the deterministic HTDT -> HTDT-Capture picker snapshot.

    This is the production export authority behind both the operator command
    and the ``scripts/export_equipment_catalog_snapshot.py`` entry point: the
    snapshot enumerates the exact EquipmentDefinition authority visible to the
    current data store, writes one deterministic JSON file, and reports the
    definition count plus the exact snapshot identity/hash. Repeating the
    export against unchanged equipment authority produces identical bytes.
    """

    snapshot = CadEquipmentRepository(scene_repository).catalog_snapshot()
    payload = snapshot.canonical_bytes()

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic save path: write to a sibling temporary file then rename so an
    # interrupted export never leaves a truncated catalog at the target.
    with tempfile.NamedTemporaryFile(
        mode='wb',
        dir=output_path.parent,
        prefix=f'.{output_path.name}.',
        suffix='.tmp',
        delete=False,
    ) as handle:
        handle.write(payload)
        temp_name = handle.name
    os.replace(temp_name, output_path)

    return EquipmentCatalogExportResult(
        authority_version=snapshot.authority_version,
        definition_count=len(snapshot.definitions),
        snapshot_sha256=sha256(payload).hexdigest(),
        byte_count=len(payload),
        output_path=str(output_path),
        generated_by=get_build_info().display_version,
    )
