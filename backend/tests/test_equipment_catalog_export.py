from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_catalog import EquipmentCatalogSnapshot
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Offset3, RoomPrism, SceneDocument, Size3
from htdt.equipment_catalog_export import (
    EquipmentCatalogExportResult,
    export_equipment_catalog_snapshot,
)


NOW = '2026-09-19T12:00:00+00:00'


def _provenance(kind: str, reference: str, sha: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind=kind,
        source_name='export-test',
        source_version='1',
        source_reference=reference,
        source_sha256=sha,
    )


def _definition(
    definition_id: str,
    *,
    identity_kind: str,
    manufacturer: str | None,
    model: str | None,
    user_label: str | None,
    provenance: EquipmentDataProvenance,
    x: float,
):
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind=identity_kind,
        manufacturer=manufacturer,
        model=model,
        user_label=user_label,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=x, y_m=0.2, z_m=0.3),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        ),
    )


def _save_equipment(repository: CadEquipmentRepository, definition) -> None:
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='equipment-export-test',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


def _scene(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(
        SceneDocument(
            document_id='export-fixture',
            room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.5),
            entities=(),
        ),
        parent_revision_id=None,
    )
    return repository


def test_export_writes_canonical_snapshot_and_reports_identity(
    tmp_path: Path,
) -> None:
    scene = _scene(tmp_path)
    equipment = CadEquipmentRepository(scene)
    _save_equipment(
        equipment,
        _definition(
            'export-sub',
            identity_kind='manufacturer',
            manufacturer='Export Acoustics',
            model='ES-10',
            user_label=None,
            provenance=_provenance('manufacturer', 'datasheet:export', 'a' * 64),
            x=0.32,
        ),
    )
    _save_equipment(
        equipment,
        _definition(
            'export-avr',
            identity_kind='user_defined',
            manufacturer=None,
            model=None,
            user_label='Rack AVR',
            provenance=_provenance('user_defined', 'manual:export', 'b' * 64),
            x=0.435,
        ),
    )

    output = tmp_path / 'capture' / 'equipment-catalog.json'
    result = export_equipment_catalog_snapshot(scene, output)

    assert isinstance(result, EquipmentCatalogExportResult)
    assert result.definition_count == 2
    assert result.authority_version == 'o100c-equipment-definition-1'
    assert result.byte_count == output.stat().st_size
    assert result.snapshot_sha256 == sha256(output.read_bytes()).hexdigest()

    decoded = json.loads(output.read_text(encoding='utf-8'))
    snapshot = EquipmentCatalogSnapshot.model_validate(decoded)
    assert snapshot.canonical_bytes() == output.read_bytes()
    assert {entry.definition_id for entry in snapshot.definitions} == {
        'export-sub',
        'export-avr',
    }


def test_export_is_deterministic_across_repeats(tmp_path: Path) -> None:
    scene = _scene(tmp_path)
    equipment = CadEquipmentRepository(scene)
    _save_equipment(
        equipment,
        _definition(
            'export-repeat',
            identity_kind='manufacturer',
            manufacturer='Export Acoustics',
            model='ER-1',
            user_label=None,
            provenance=_provenance('manufacturer', 'datasheet:repeat', 'c' * 64),
            x=0.2,
        ),
    )

    first = export_equipment_catalog_snapshot(
        scene, tmp_path / 'first.json'
    )
    second = export_equipment_catalog_snapshot(
        scene, tmp_path / 'second.json'
    )
    assert first.snapshot_sha256 == second.snapshot_sha256
    assert first.byte_count == second.byte_count


def test_export_empty_catalog_still_valid(tmp_path: Path) -> None:
    scene = _scene(tmp_path)
    output = tmp_path / 'empty.json'
    result = export_equipment_catalog_snapshot(scene, output)
    assert result.definition_count == 0
    decoded = json.loads(output.read_text(encoding='utf-8'))
    snapshot = EquipmentCatalogSnapshot.model_validate(decoded)
    assert snapshot.definitions == ()
