"""REV44-STAGED — record-entry surfaces for write-only authority families.

rev43 finding 9: the operating-preset, equipment-binding, installation
context/datum and treatment source-asset families shipped complete models +
repositories + live readers but had ZERO production writers — the health
dialog's preset picks, the ``operating_preset_*`` activity events, the
概要 installation card + equipment notice, R110 source gating and the
installation handoff's 設置基準 section could only ever read empty.

These tests drive the new record-entry surfaces against the real persisted
stack: register → the persisted readers see rows; invalid input fails
closed with a JA status and nothing saved.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest  # noqa: E402

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QMessageBox,
)

from htdt.cad_acoustic_treatment_comparison import (  # noqa: E402
    CadAcousticTreatmentComparisonRepository,
)
from htdt.cad_acoustic_treatment_repository import (  # noqa: E402
    CadAcousticTreatmentRepository,
)
from htdt.cad_equipment_binding_repository import (  # noqa: E402
    CadEquipmentBindingRepository,
)
from htdt.cad_installation_context_repository import (  # noqa: E402
    CadInstallationContextRepository,
)
from htdt.cad_installation_datum_repository import (  # noqa: E402
    CadInstallationDatumRepository,
)
from htdt.cad_operating_preset_repository import (  # noqa: E402
    CadOperatingPresetRepository,
)
from htdt.cad_project_activity import CadProjectActivityService  # noqa: E402
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene  # noqa: E402
from htdt.equipment_library import EquipmentLibraryService  # noqa: E402
from htdt.installation_record_surfaces import (  # noqa: E402
    InstallationRecordDialog,
)
from htdt.measurement_record_surfaces import (  # noqa: E402
    HealthCheckDialog,
    OperatingPresetRecordDialog,
)
from htdt.room_acoustics_panel import (  # noqa: E402
    RoomTreatmentPanel,
    TreatmentDefinitionDialog,
)
from htdt.room_workspace import RoomWorkspaceController  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def _seed_equipment(repository: SceneRepository) -> None:
    EquipmentLibraryService(repository).create_user_definition(
        user_label='テストスピーカー',
        manufacturer='Acme',
        model='S-1',
        width_m=0.25,
        height_m=0.4,
        depth_m=0.3,
        evidence_kind='user_defined',
        source_name='現地メモ',
        source_version='1',
        source_reference='2026-10-04',
        actor='test',
    )


# ---------------------------------------------------------------------------
# Operating presets: register → health dialog pick → applied state → activity
# ---------------------------------------------------------------------------


def test_preset_dialog_registers_and_applies(tmp_path: Path) -> None:
    _app()
    repository = _repository(tmp_path)
    preset_repository = CadOperatingPresetRepository(repository)
    dialog = OperatingPresetRecordDialog(
        scene_repository=repository, document_id=F1_DOCUMENT_ID
    )

    dialog.name_edit.setText('映画（夜間）')
    dialog.device_mode_edit.setText('Movie')
    dialog.level_enabled_check.setChecked(True)
    dialog.level_spin.setValue(-18.0)
    dialog._register_preset()

    presets = preset_repository.list_presets(F1_DOCUMENT_ID)
    assert len(presets) == 1
    preset = presets[0]
    assert preset.name == '映画（夜間）'
    assert preset.category == 'movie'
    assert preset.nominal_playback_level_db == pytest.approx(-18.0)
    assert preset.scene_revision_id is not None

    # Live consumer 1: the health dialog's baseline preset pick now offers it.
    health = HealthCheckDialog(
        scene_repository=repository, document_id=F1_DOCUMENT_ID
    )
    offered = [
        health.baseline_preset_combo.itemText(i)
        for i in range(health.baseline_preset_combo.count())
    ]
    assert any('映画（夜間）' in text for text in offered)

    dialog.apply_preset_combo.setCurrentIndex(0)
    dialog.device_context_edit.setText('AVR=Movieモード')
    dialog._record_applied_state()

    applied = preset_repository.list_applied_states(preset.preset_id)
    assert len(applied) == 1
    assert applied[0].preset_id == preset.preset_id
    assert applied[0].preset_sha256 == preset.preset_sha256
    assert applied[0].device_context == 'AVR=Movieモード'

    # Live consumer 2: the activity feed emits both preset event kinds.
    activity = CadProjectActivityService(
        scene_repository=repository, preset_repository=preset_repository
    )
    kinds = {
        event.kind
        for event in activity.events(F1_DOCUMENT_ID)
    }
    assert 'operating_preset_created' in kinds
    assert 'operating_preset_applied' in kinds
    health.deleteLater()
    dialog.deleteLater()


def test_preset_dialog_fails_closed(tmp_path: Path) -> None:
    _app()
    repository = _repository(tmp_path)
    preset_repository = CadOperatingPresetRepository(repository)
    dialog = OperatingPresetRecordDialog(
        scene_repository=repository, document_id=F1_DOCUMENT_ID
    )

    # Blank name → nothing persisted, JA status.
    dialog._register_preset()
    assert preset_repository.list_presets(F1_DOCUMENT_ID) == ()
    assert dialog.status_label.text()

    # No presets → the apply action is disabled and record is a no-op.
    assert not dialog.apply_button.isEnabled()
    dialog._record_applied_state()
    assert dialog.status_label.text()
    dialog.deleteLater()


# ---------------------------------------------------------------------------
# Installation: binding + context + datum
# ---------------------------------------------------------------------------


def _installation_dialog(repository: SceneRepository):
    return InstallationRecordDialog(
        scene_repository=repository, document_id=F1_DOCUMENT_ID
    )


def _fill_provenance(editor) -> None:
    editor.name_edit.setText('現地確認メモ')
    editor.reference_edit.setText('2026-10-04')


def test_installation_dialog_records_binding_context_datum(
    tmp_path: Path,
) -> None:
    _app()
    repository = _repository(tmp_path)
    _seed_equipment(repository)
    dialog = _installation_dialog(repository)

    # 機材バインド — speaker pick + equipment pick + declared authorities.
    assert dialog.bind_entity_combo.count() >= 1
    assert dialog.bind_equipment_combo.count() >= 1
    _fill_provenance(dialog.bind_provenance)
    dialog._save_binding()

    entity_id = dialog.bind_entity_combo.currentData()
    binding_repository = CadEquipmentBindingRepository(
        repository,
        EquipmentLibraryService(repository).equipment_repository,
    )
    binding = binding_repository.get_binding_for_entity(
        F1_DOCUMENT_ID, entity_id
    )
    assert binding is not None
    assert binding.entity_id == entity_id
    assert binding.provenance[0].source_name == '現地確認メモ'

    # 設置コンテキスト — mounting/baffle/host/clearances + evidence.
    _fill_provenance(dialog.context_provenance)
    dialog.mounting_combo.setCurrentIndex(
        dialog.mounting_combo.findData('stand')
    )
    dialog.clearance_spins['front_m'].setValue(0.5)
    dialog._save_context()

    context_repository = CadInstallationContextRepository(
        repository, EquipmentLibraryService(repository).equipment_repository
    )
    context = context_repository.get_context_for_entity(
        F1_DOCUMENT_ID, entity_id
    )
    assert context is not None
    assert context.selected_mounting_mode == 'stand'
    assert context.measured_clearances.front_m == pytest.approx(0.5)

    # 設置基準 — vertex anchor + two distinct direction walls.
    assert dialog.anchor_pick_combo.count() >= 1
    assert dialog.x_wall_combo.count() >= 2
    dialog.anchor_label_edit.setText('前左角')
    dialog._save_datum()

    datums = CadInstallationDatumRepository(repository).list_datums(
        F1_DOCUMENT_ID
    )
    assert len(datums) == 1
    datum = datums[0]
    assert datum.primary_anchor.kind == 'room_vertex'
    assert datum.primary_anchor.vertex_id == 'front-left'
    assert datum.x_direction_wall_id != datum.y_direction_wall_id
    dialog.deleteLater()


def test_installation_dialog_fails_closed(tmp_path: Path) -> None:
    _app()
    repository = _repository(tmp_path)
    _seed_equipment(repository)
    dialog = _installation_dialog(repository)

    # Blank provenance → nothing persisted on either writer.
    dialog._save_binding()
    assert dialog.status_label.text()
    dialog._save_context()
    assert (
        CadInstallationContextRepository(
            repository,
            EquipmentLibraryService(repository).equipment_repository,
        ).get_context_for_entity(
            F1_DOCUMENT_ID, dialog.context_entity_combo.currentData()
        )
        is None
    )

    # Same wall for X and Y → validator-equivalent fail closed.
    _fill_provenance(dialog.context_provenance)
    dialog._save_context()
    dialog.anchor_label_edit.setText('前左角')
    wall_id = dialog.x_wall_combo.itemData(0)
    for index in range(dialog.y_wall_combo.count()):
        if dialog.y_wall_combo.itemData(index) == wall_id:
            dialog.y_wall_combo.setCurrentIndex(index)
            break
    dialog.x_wall_combo.setCurrentIndex(0)
    dialog._save_datum()
    assert CadInstallationDatumRepository(repository).list_datums(
        F1_DOCUMENT_ID
    ) == ()
    assert dialog.status_label.text()
    dialog.deleteLater()


# ---------------------------------------------------------------------------
# Treatment source asset: external source kinds need the retained file
# ---------------------------------------------------------------------------


def test_treatment_definition_dialog_external_source_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app()
    monkeypatch.setattr(
        QMessageBox,
        'warning',
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Ok),
    )
    dialog = TreatmentDefinitionDialog()
    dialog.name.setText('50mmパネル')
    dialog.layer_material.setText('グラスウール')

    # user_defined (default) needs no source file.
    assert dialog.source_kind.currentData() == 'user_defined'
    assert not dialog.source_file_button.isEnabled()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted

    # External kind without a retained file fails closed.
    dialog2 = TreatmentDefinitionDialog()
    dialog2.name.setText('50mmパネル')
    dialog2.layer_material.setText('グラスウール')
    dialog2.source_kind.setCurrentIndex(
        dialog2.source_kind.findData('manufacturer')
    )
    dialog2.accept()
    assert dialog2.result() != QDialog.DialogCode.Accepted
    dialog.deleteLater()
    dialog2.deleteLater()


def test_treatment_panel_saves_external_definition_with_retained_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app()
    repository = _repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    panel = RoomTreatmentPanel(controller)

    source_bytes = b'manufacturer-datasheet-bytes'
    values = {
        'name': 'メーカー50mmパネル',
        'version': '1',
        'treatment_type': 'porous_absorber',
        'width_m': 0.6,
        'height_m': 1.2,
        'thickness_m': 0.05,
        'air_gap_m': 0.0,
        'layer_material': 'グラスウール',
        'layer_density': 32.0,
        'source_kind': 'manufacturer',
        'source_name': 'メーカー資料 S-50',
        'source_version': '2',
        'source_reference': 'p.12',
        'source_bytes': source_bytes,
        'source_file_name': 'datasheet.pdf',
    }

    class _DialogStub:
        DialogCode = QDialog.DialogCode

        def __init__(self, parent=None):
            pass

        def exec(self):
            return TreatmentDefinitionDialog.DialogCode.Accepted

        def values(self):
            return dict(values)

    monkeypatch.setattr(
        'htdt.room_acoustics_panel.TreatmentDefinitionDialog', _DialogStub
    )
    panel._new_definition()

    treatment_repository = CadAcousticTreatmentRepository(repository)
    definitions = treatment_repository.list_definitions()
    assert any(
        definition.name == 'メーカー50mmパネル'
        for definition in definitions
    )
    # The evidence authority resolves and claims the retained asset's hash.
    definition = next(
        item
        for item in definitions
        if item.name == 'メーカー50mmパネル'
    )
    evidence, _model = treatment_repository.resolve_definition_evidence(
        definition
    )
    assert evidence is not None
    from hashlib import sha256

    assert evidence.source_sha256 == sha256(source_bytes).hexdigest()
    # The managed asset row retained the exact bytes.
    stored = (
        treatment_repository.assets_dir
        / sha256(source_bytes).hexdigest()
    )
    assert stored.read_bytes() == source_bytes
    panel.deleteLater()


def test_installation_dialog_lists_record_status(tmp_path: Path) -> None:
    _app()
    repository = _repository(tmp_path)
    _seed_equipment(repository)
    dialog = _installation_dialog(repository)
    # F1 has three speakers → three status rows, all 未記録.
    assert dialog.entity_status.topLevelItemCount() == 3
    first = dialog.entity_status.topLevelItem(0)
    assert '未記録' in first.text(1)
    dialog.deleteLater()
