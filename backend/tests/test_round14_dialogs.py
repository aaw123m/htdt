"""Round-14 review: dialog & form honesty — every QDialog must validate
honestly (clear JP feedback, no silent no-op OK), apply completely
(displayed defaults == applied values, no dropped fields), and discard
cleanly (cancel applies nothing). Dialogs are instantiated offscreen and
driven like a user would: fields filled, buttons clicked, effects read
back — nothing here claims behavior from code reading alone.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMessageBox,
    QWidget,
)


from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.cad_seating import AisleSpec, SeatRowSpec, SeatingLayoutSpec
from htdt.capture_receiver_settings import PairingDialog
from htdt.capture_retention_ui import RetentionPolicyWidget
from htdt.commissioning_wizard import CommissioningWizard
from htdt.equipment_library import EquipmentLibraryDialog
from htdt.playback_chain_widgets import PlaybackChainDialog
from htdt.room_acoustics_panel import (
    MaterialDialog,
    TreatmentDefinitionDialog,
)
from htdt.room_prediction import EnvironmentProfileDialog
from htdt.room_video_panel import (
    DisplaySpecDialog,
    ProjectorSpecDialog,
    ScreenTransferDialog,
)
from htdt.room_workspace import SeatingLayoutDialog
from htdt.standards_profile_editor import StandardsProfileEditorDialog
from htdt.system_expansion_widgets import _MeasurementPlanDialog
from htdt.system_expansion_workflow import (
    MeasurementPlanOptions,
    MeasurementPointOption,
    MeasurementSourceOption,
)
from htdt.workflow_settings import DataManagementDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def qapp():
    return _app()


@pytest.fixture()
def msgboxes(monkeypatch):
    """Neutralize modal QMessageBox entry points and record what surfaced.

    ``warn_user`` constructs a QMessageBox then calls ``exec()`` on it, so
    patching ``exec`` captures those as well as direct ``.exec()`` calls;
    the static helpers (``warning``/``question``/…) are patched to return
    the *safe* default (Cancel) the same way a cautious user would.
    """
    calls: list[tuple[str, str, str, str]] = []

    def _exec(self):
        calls.append(
            ("exec", self.windowTitle(), self.text(), self.informativeText())
        )
        return QMessageBox.StandardButton.Cancel

    def _static(kind):
        def _fn(parent, title, text, *args, **kwargs):
            calls.append((kind, str(title), str(text), ""))
            return QMessageBox.StandardButton.Cancel

        return staticmethod(_fn)

    monkeypatch.setattr(QMessageBox, "exec", _exec)
    monkeypatch.setattr(QMessageBox, "warning", _static("warning"))
    monkeypatch.setattr(QMessageBox, "information", _static("information"))
    monkeypatch.setattr(QMessageBox, "question", _static("question"))
    monkeypatch.setattr(QMessageBox, "critical", _static("critical"))
    return calls


_ACCEPTED = QDialog.DialogCode.Accepted
_REJECTED = QDialog.DialogCode.Rejected


# -- 音響マテリアル登録 (MaterialDialog) --------------------------------------


def test_material_dialog_blocks_missing_required_honestly(qapp, msgboxes):
    dialog = MaterialDialog()
    dialog.accept()
    assert dialog.result() == _REJECTED
    dialog.label.setText("25mmグラスウール")
    dialog.accept()
    assert dialog.result() == _REJECTED
    assert msgboxes, "blocked OK must surface a JP reason, not silently stay"
    dialog.provenance.setText("メーカー公表値")
    # Defaults are unsupported+unsupported; the backend authority rejects
    # that combination, so the dialog must block in-dialog, honestly.
    dialog.accept()
    assert dialog.result() == _REJECTED
    dialog.wave_model.setCurrentIndex(dialog.wave_model.findData("rigid"))
    dialog.accept()
    assert dialog.result() == _ACCEPTED
    assert dialog.values() == {
        "label": "25mmグラスウール",
        "wave_model": "rigid",
        "impedance_text": "",
        "geometric_model": "unsupported",
        "bands_text": "",
        "provenance": "メーカー公表値",
    }


def test_material_dialog_requires_data_matching_capability(qapp, msgboxes):
    # specific_impedance_table without impedance rows → backend rejects;
    # the dialog must block before closing instead of losing the form.
    dialog = MaterialDialog()
    dialog.label.setText("ガラスウール")
    dialog.provenance.setText("現場実測")
    dialog.wave_model.setCurrentIndex(
        dialog.wave_model.findData("specific_impedance_table")
    )
    dialog.accept()
    assert dialog.result() == _REJECTED
    dialog.impedance.setPlainText("100,500,0\n200,450,10")
    dialog.accept()
    assert dialog.result() == _ACCEPTED

    # Impedance rows while wave is not the table model are rejected too —
    # stray data must not silently ride along under a different model.
    other = MaterialDialog()
    other.label.setText("m")
    other.provenance.setText("p")
    other.wave_model.setCurrentIndex(other.wave_model.findData("rigid"))
    other.impedance.setPlainText("100,500,0")
    other.accept()
    assert other.result() == _REJECTED

    # banded without bands → same story.
    third = MaterialDialog()
    third.label.setText("m")
    third.provenance.setText("p")
    third.geometric_model.setCurrentIndex(
        third.geometric_model.findData("banded")
    )
    third.accept()
    assert third.result() == _REJECTED
    third.bands.setPlainText("125,0.2,0.1")
    third.accept()
    assert third.result() == _ACCEPTED

    values = third.values()
    assert values["geometric_model"] == "banded"
    assert values["bands_text"] == "125,0.2,0.1"


def test_material_dialog_cancel_applies_nothing(qapp):
    dialog = MaterialDialog()
    dialog.label.setText("typed but cancelled")
    dialog.provenance.setText("出典")
    dialog.reject()
    assert dialog.result() == _REJECTED


def test_material_dialog_enter_accepts_escape_rejects(qapp):
    dialog = MaterialDialog()
    dialog.label.setText("x")
    dialog.provenance.setText("y")
    dialog.wave_model.setCurrentIndex(dialog.wave_model.findData("rigid"))
    dialog.show()
    dialog.label.setFocus()
    QTest.keyClick(dialog.label, Qt.Key.Key_Return)
    assert dialog.result() == _ACCEPTED

    again = MaterialDialog()
    again.show()
    QTest.keyClick(again, Qt.Key.Key_Escape)
    assert again.result() == _REJECTED


# -- 音響処理定義 (TreatmentDefinitionDialog) ---------------------------------


def test_treatment_dialog_required_fields_and_complete_values(qapp, msgboxes):
    dialog = TreatmentDefinitionDialog()
    dialog.accept()
    assert dialog.result() == _REJECTED
    dialog.name.setText("50mm多孔質パネル")
    dialog.accept()
    assert dialog.result() == _REJECTED
    assert msgboxes, "blocked OK must surface a JP reason"
    dialog.layer_material.setText("グラスウール")
    dialog.accept()
    assert dialog.result() == _ACCEPTED
    values = dialog.values()
    assert values["layer_density"] is None  # 「不明」at 0.0 → None, consistent
    for key in (
        "name", "version", "treatment_type", "width_m", "height_m",
        "thickness_m", "air_gap_m", "layer_material", "layer_density",
    ):
        assert key in values


def test_treatment_dialog_cancel(qapp):
    dialog = TreatmentDefinitionDialog()
    dialog.reject()
    assert dialog.result() == _REJECTED


# -- プロジェクター仕様 (ProjectorSpecDialog) ----------------------------------


def test_projector_spec_dialog_validates_required_and_ranges(qapp, msgboxes):
    dialog = ProjectorSpecDialog()
    dialog.accept()
    assert dialog.result() == _REJECTED  # 仕様ID required in-dialog
    dialog.spec_id.setText("proj-1")
    dialog.throw_min.setValue(4.0)
    dialog.throw_max.setValue(2.0)
    dialog.accept()
    assert dialog.result() == _REJECTED  # throw min > max
    dialog.throw_max.setValue(6.0)
    dialog.shift_h_enabled.setCurrentIndex(1)
    dialog.shift_h_min.setValue(0.5)
    dialog.shift_h_max.setValue(0.0)
    dialog.accept()
    assert dialog.result() == _REJECTED  # shift range inverted
    dialog.shift_h_max.setValue(0.6)
    dialog.accept()
    assert dialog.result() == _ACCEPTED
    assert msgboxes
    values = dialog.values()
    assert values["specification_id"] == "proj-1"
    assert values["horizontal_lens_shift"] == (0.5, 0.6)
    assert values["vertical_lens_shift"] is None  # 「不明」→ None
    assert values["throw_ratio_min"] == 4.0
    assert values["throw_ratio_max"] == 6.0


def test_projector_spec_dialog_defaults_roundtrip(qapp):
    dialog = ProjectorSpecDialog()
    dialog.spec_id.setText("p")
    dialog.accept()
    values = dialog.values()
    assert values["version"] == "1"
    assert values["publisher"] == "HTDTユーザー"
    assert values["document_title"] == "手動入力"
    assert values["throw_ratio_min"] == 1.0
    assert values["throw_ratio_max"] == 2.0


# -- スクリーン伝達権威 (ScreenTransferDialog) ---------------------------------


def test_screen_transfer_dialog_required_and_tier_coupling(qapp, msgboxes):
    dialog = ScreenTransferDialog()
    dialog.accept()
    assert dialog.result() == _REJECTED
    dialog.label.setText("メインスクリーン AT-2000")
    dialog.provenance.setText("現場実測")
    # UNKNOWN tier needs no samples — the honest default accepts.
    dialog.accept()
    assert dialog.result() == _ACCEPTED
    values = dialog.values()
    assert values["capability_tier"] == "UNKNOWN"
    assert values["frequency_minimum_hz"] == 20.0
    assert values["frequency_maximum_hz"] == 8000.0

    # A sampled tier without samples is rejected by the sealed model;
    # the dialog must block before losing the form.
    other = ScreenTransferDialog()
    other.label.setText("m")
    other.provenance.setText("p")
    other.tier.setCurrentIndex(
        other.tier.findData("MAGNITUDE_NORMAL_INCIDENCE")
    )
    other.accept()
    assert other.result() == _REJECTED
    other.samples.setPlainText("500,0,0.8")
    other.accept()
    assert other.result() == _ACCEPTED

    # Inverted frequency domain must not close silently.
    third = ScreenTransferDialog()
    third.label.setText("m")
    third.provenance.setText("p")
    third.freq_min.setValue(9000.0)
    third.freq_max.setValue(500.0)
    third.accept()
    assert third.result() == _REJECTED
    assert msgboxes


def test_screen_transfer_dialog_cancel(qapp):
    dialog = ScreenTransferDialog()
    dialog.reject()
    assert dialog.result() == _REJECTED


# -- ディスプレイ仕様 (DisplaySpecDialog) --------------------------------------


def test_display_spec_dialog_required_fields(qapp, msgboxes):
    dialog = DisplaySpecDialog()
    dialog.accept()
    assert dialog.result() == _REJECTED
    dialog.spec_id.setText("tv-1")
    dialog.accept()
    assert dialog.result() == _REJECTED  # user_label still missing
    assert msgboxes
    dialog.user_label.setText("リビングのテレビ")
    dialog.accept()
    assert dialog.result() == _ACCEPTED
    values = dialog.values()
    for key in (
        "specification_id", "version", "user_label", "display_class",
        "chassis_width_m", "chassis_depth_m", "chassis_height_m",
        "active_image_width_m", "active_image_height_m",
        "source_name", "source_reference",
    ):
        assert key in values


# -- 環境プロファイル (EnvironmentProfileDialog) -------------------------------


def test_environment_profile_dialog_required_label(qapp, msgboxes):
    dialog = EnvironmentProfileDialog()
    dialog.accept()
    assert dialog.result() == _REJECTED
    assert msgboxes, "blocked OK must surface a JP reason"
    dialog.label.setText("室内 22 °C")
    dialog.accept()
    assert dialog.result() == _ACCEPTED
    kwargs = dialog.profile_kwargs()
    assert kwargs["sound_speed_m_s"] == pytest.approx(343.0)
    assert kwargs["temperature_c"] == pytest.approx(20.0)


def test_environment_profile_unknown_kind_declares_no_speed(qapp):
    dialog = EnvironmentProfileDialog()
    dialog.label.setText("不明プロファイル")
    index = dialog.source_kind.findData("unknown")
    dialog.source_kind.setCurrentIndex(index)
    dialog.accept()
    kwargs = dialog.profile_kwargs()
    assert kwargs["sound_speed_source_kind"] == "unknown"
    assert "sound_speed_m_s" not in kwargs


def test_environment_profile_temperature_display_matches_applied(qapp):
    """−40 °C must not display 「不明」while a real −40 °C is applied."""
    dialog = EnvironmentProfileDialog()
    index = dialog.source_kind.findData("derived_from_temperature")
    dialog.source_kind.setCurrentIndex(index)
    dialog.temperature.setValue(-40.0)
    assert "不明" not in dialog.temperature.text()
    dialog.label.setText("寒冷")
    dialog.accept()
    kwargs = dialog.profile_kwargs()
    assert kwargs["temperature_c"] == pytest.approx(-40.0)


# -- 座席レイアウト (SeatingLayoutDialog) --------------------------------------


def _seat_document():
    return SimpleNamespace(room=None, entities=())


def test_seating_layout_dialog_defaults_apply_completely(qapp):
    dialog = SeatingLayoutDialog(document=_seat_document())
    dialog.accept()
    assert dialog.result() == _ACCEPTED
    spec = dialog.spec()
    assert spec is not None
    assert spec.name == "座席ブロック"  # empty name → documented default
    assert len(spec.rows) == 2
    assert {row.count for row in spec.rows} == {4}
    assert spec.facing == "front"
    assert spec.aisles == ()


def test_seating_layout_dialog_invalid_aisle_stays_open(qapp, msgboxes):
    dialog = SeatingLayoutDialog(document=_seat_document())
    dialog.aisle_field.setText("bogus")
    dialog.accept()
    assert dialog.result() == _REJECTED  # dialog stays open post-fix
    assert msgboxes
    dialog.aisle_field.setText("2:0.9")
    dialog.accept()
    assert dialog.result() == _ACCEPTED
    spec = dialog.spec()
    assert spec is not None
    assert len(spec.aisles) == 1
    assert spec.aisles[0].after_index == 2
    assert spec.aisles[0].width_m == pytest.approx(0.9)


def test_seating_layout_dialog_roundtrip_existing(qapp):
    existing = SeatingLayoutSpec(
        spec_id="layout-1",
        name="VIPブロック",
        anchor_x_m=1.5,
        anchor_y_m=2.0,
        rows=(
            SeatRowSpec(
                row_id="r1",
                name="A",
                count=6,
                spacing_m=0.9,
                row_spacing_m=1.2,
                stagger=True,
            ),
        ),
        aisles=(AisleSpec(after_index=3, width_m=0.8),),
        facing="rear",
    )
    dialog = SeatingLayoutDialog(document=_seat_document(), existing=existing)
    assert dialog.name_field.text() == "VIPブロック"
    assert dialog.rows_field.value() == 1
    assert dialog.count_field.value() == 6
    assert dialog.spacing_field.value() == pytest.approx(0.9)
    assert dialog.row_spacing_field.value() == pytest.approx(1.2)
    assert dialog.stagger_field.isChecked()
    assert dialog.aisle_field.text() == "3:0.8"
    assert dialog.facing_field.currentData() == "rear"
    spec = dialog.spec()
    assert spec is not None
    assert spec.spec_id == "layout-1"  # re-save keeps the identity


# -- 測定計画 (_MeasurementPlanDialog) -----------------------------------------


def _plan_options():
    return MeasurementPlanOptions(
        ready=True,
        reason=None,
        measurement_points=(
            MeasurementPointOption("p1", "聴取点", "(1.0,2.0,0.0)"),
        ),
        sources=(
            MeasurementSourceOption("s1", "フロントL", "FL"),
            MeasurementSourceOption("s2", "サブ", "LFE"),
        ),
    )


def test_measurement_plan_dialog_requires_target(qapp, msgboxes):
    dialog = _MeasurementPlanDialog(_plan_options())
    dialog.accept()
    assert dialog.result() == _REJECTED  # no source selected
    assert msgboxes
    dialog.sources.item(0).setSelected(True)
    dialog.role_combo.setCurrentText("")
    dialog.accept()
    assert dialog.result() == _REJECTED  # channel role required
    dialog.role_combo.setCurrentText("FL")
    dialog.accept()
    assert dialog.result() == _ACCEPTED
    assert dialog.selected_source_ids() == ["s1"]


def test_measurement_plan_dialog_repeatability_requires_two(qapp, msgboxes):
    dialog = _MeasurementPlanDialog(_plan_options())
    dialog.sources.item(0).setSelected(True)
    dialog.repeatability_check.setChecked(True)
    dialog.accept()
    assert dialog.result() == _REJECTED  # count=1 cannot demand repeatability
    dialog.count_spin.setValue(2)
    dialog.accept()
    assert dialog.result() == _ACCEPTED


# -- 基準プロファイル (StandardsProfileEditorDialog) ---------------------------


def _fill_criterion_form(dialog: StandardsProfileEditorDialog) -> None:
    dialog.criterion_id_edit.setText("crit-1")
    dialog.criterion_name_edit.setText("視野角")
    dialog.criterion_quantity_edit.setText("viewing_angle")
    dialog.criterion_unit_edit.setText("deg")
    dialog.domains_edit.setText("video")
    dialog.publisher_edit.setText("me")
    dialog.doc_title_edit.setText("notes")
    dialog.doc_version_edit.setText("1")
    dialog.reference_edit.setText("p.1")


def test_standards_editor_min_rule_with_untouched_max(qapp, msgboxes):
    """「上限 なし」must produce maximum=None — not a bogus -1e6 bound that
    either rejects every 'min' rule or silently saves a corrupt value."""
    service = Mock()
    service.profiles.return_value = []
    dialog = StandardsProfileEditorDialog(service)
    _fill_criterion_form(dialog)
    index = dialog.operator_combo.findData("min")
    dialog.operator_combo.setCurrentIndex(index)
    dialog.minimum_spin.setValue(5.0)
    dialog._apply_criterion()
    assert len(dialog._criteria) == 1
    rule = dialog._criteria[0].rule
    assert rule.minimum == pytest.approx(5.0)
    assert rule.maximum is None


def test_standards_editor_max_rule_bounds(qapp, msgboxes):
    service = Mock()
    service.profiles.return_value = []
    dialog = StandardsProfileEditorDialog(service)
    _fill_criterion_form(dialog)
    index = dialog.operator_combo.findData("max")
    dialog.operator_combo.setCurrentIndex(index)
    dialog.maximum_spin.setValue(50.0)
    dialog._apply_criterion()
    assert len(dialog._criteria) == 1
    assert dialog._criteria[0].rule.maximum == pytest.approx(50.0)

    # Untouched 「なし」with a 'max' rule → honest validation error.
    dialog.criterion_id_edit.setText("crit-2")
    dialog.maximum_spin.setValue(dialog.maximum_spin.minimum())
    dialog._apply_criterion()
    assert len(dialog._criteria) == 1  # nothing added
    assert msgboxes

    # A real 1e6 entry must be a real bound, not collapse to None.
    dialog.criterion_id_edit.setText("crit-3")
    dialog.maximum_spin.setValue(1_000_000.0)
    dialog._apply_criterion()
    assert len(dialog._criteria) == 2
    assert dialog._criteria[1].rule.maximum == pytest.approx(1_000_000.0)


def test_standards_editor_range_rule(qapp):
    service = Mock()
    service.profiles.return_value = []
    dialog = StandardsProfileEditorDialog(service)
    _fill_criterion_form(dialog)
    index = dialog.operator_combo.findData("range")
    dialog.operator_combo.setCurrentIndex(index)
    dialog.minimum_spin.setValue(1.0)
    dialog.maximum_spin.setValue(5.0)
    dialog._apply_criterion()
    assert len(dialog._criteria) == 1
    assert dialog._criteria[0].rule.minimum == pytest.approx(1.0)
    assert dialog._criteria[0].rule.maximum == pytest.approx(5.0)


# -- 再生チェーン (PlaybackChainDialog) ----------------------------------------


def _playback_service() -> Mock:
    service = Mock()
    service.amplifier_capabilities.return_value = []
    service.speaker_loads.return_value = []
    service.variants.return_value = []
    service.speaker_entities.return_value = []
    service.source_equipment_choices.return_value = []
    service.create_amplifier_capability.side_effect = (
        lambda **kw: SimpleNamespace(
            semantic_sha256="a" * 64, version="1", **kw
        )
    )
    return service


def test_playback_chain_gain_display_matches_applied(qapp, msgboxes):
    """−40 dB must not display 「不明」while being saved as a real bound."""
    dialog = PlaybackChainDialog(_playback_service())
    dialog.amp_gain.setValue(-40.0)
    assert "不明" not in dialog.amp_gain.text()
    dialog.amp_label.setText("amp-1")
    dialog.amp_output_id.setText("front-l")
    dialog.amp_source_name.setText("測定")
    dialog.amp_source_version.setText("1")
    dialog.amp_source_reference.setText("p.1")
    dialog._save_amplifier()
    kwargs = dialog.service.create_amplifier_capability.call_args.kwargs
    assert kwargs["gain_db"] == pytest.approx(-40.0)

    # The documented sentinel stays: 0 dB → unknown (label says 0=不明扱い).
    other = PlaybackChainDialog(_playback_service())
    other.amp_label.setText("amp-2")
    other.amp_output_id.setText("front-r")
    other.amp_source_name.setText("測定")
    other.amp_source_version.setText("1")
    other.amp_source_reference.setText("p.1")
    other._save_amplifier()
    kwargs = other.service.create_amplifier_capability.call_args.kwargs
    assert kwargs["gain_db"] is None


def test_playback_chain_missing_required_warns(qapp, msgboxes):
    service = _playback_service()
    service.create_amplifier_capability.side_effect = ValueError(
        "ラベルを入力してください。"
    )
    dialog = PlaybackChainDialog(service)
    dialog._save_amplifier()
    assert msgboxes  # service-level rejection surfaced via warn_user


# -- デバイスペアリング (PairingDialog) ----------------------------------------


def _pairing_dialog() -> PairingDialog:
    pairing = SimpleNamespace(
        pairing_id="pair-1",
        confirmation_code="ABC123",
        state="offered",
    )
    payload = Mock()
    payload.model_dump.return_value = {"endpoints": {}}
    payload.expires_at = "2030-01-01T00:00:00+00:00"
    service = Mock()
    service.begin_pairing.return_value = (pairing, payload)
    service.list_pairings.return_value = []
    service.confirm_pairing.return_value = SimpleNamespace(
        **{**vars(pairing), "state": "active"}
    )
    controller = SimpleNamespace(service=service)
    return PairingDialog(controller, "doc-1")


def test_pairing_confirm_requires_entered_code(qapp):
    """The confirmation ceremony must not be bypassed by an empty code."""
    dialog = _pairing_dialog()
    dialog.offer_button.click()
    assert dialog._pairing is not None
    dialog.confirm_code_edit.setText("")
    dialog.confirm_button.click()
    dialog._controller.service.confirm_pairing.assert_not_called()
    assert "確認コード" in dialog.status_label.text()

    dialog.confirm_code_edit.setText("ZZZ999")
    dialog.confirm_button.click()
    dialog._controller.service.confirm_pairing.assert_not_called()
    assert "一致" in dialog.status_label.text()

    dialog.confirm_code_edit.setText("ABC123")
    dialog.confirm_button.click()
    dialog._controller.service.confirm_pairing.assert_called_once_with(
        "pair-1"
    )


def test_pairing_confirm_without_offer_explains(qapp):
    dialog = _pairing_dialog()
    dialog.confirm_button.click()
    assert "発行" in dialog.status_label.text()
    dialog._controller.service.confirm_pairing.assert_not_called()


# -- プロジェクト初期設定 (CommissioningWizard) ---------------------------------


def test_commissioning_wizard_new_name_collision_warns(qapp, tmp_path, msgboxes):
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    repository.save(make_empty_scene("taken"), parent_revision_id=None)
    wizard = CommissioningWizard(repository, "taken")

    wizard.name_edit.setText("   ")
    assert wizard._resolve_document_id(create_document=True) is None

    # 「新しいプロジェクトを作成」with a name that already exists must not
    # silently attach the existing document.
    wizard.name_edit.setText("taken")
    assert wizard._resolve_document_id(create_document=True) is None
    assert msgboxes

    wizard.name_edit.setText("fresh")
    assert wizard._resolve_document_id(create_document=True) == "fresh"
    assert repository.latest("fresh") is not None


def test_commissioning_wizard_existing_radio_selects(qapp, tmp_path):
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    repository.save(make_empty_scene("taken"), parent_revision_id=None)
    wizard = CommissioningWizard(repository, "taken")
    wizard.existing_radio.setChecked(True)
    assert wizard._resolve_document_id(create_document=True) == "taken"


def test_commissioning_wizard_cancel_saves_nothing(qapp, tmp_path):
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    repository.save(make_empty_scene("taken"), parent_revision_id=None)
    wizard = CommissioningWizard(repository, "taken")
    wizard.name_edit.setText("never-created")
    # Browse to the summary page — building the plan preview must not
    # persist anything; then cancel.
    for _ in range(4):
        wizard.next_button.click()
    wizard.reject()
    assert wizard.result() == _REJECTED
    assert repository.latest("never-created") is None


# -- 機器ライブラリ (EquipmentLibraryDialog) ------------------------------------


def _equipment_service() -> Mock:
    service = Mock()
    service.definitions.return_value = []
    service.supported_directivity_adapters.return_value = []
    return service


def test_equipment_dialog_save_warns_and_emits(qapp, msgboxes):
    service = _equipment_service()
    service.create_user_definition.side_effect = ValueError("ラベルを入力してください。")
    dialog = EquipmentLibraryDialog(service)
    emitted: list[None] = []
    dialog.definitionsChanged.connect(lambda: emitted.append(None))
    dialog.save_new_button.click()
    assert not emitted
    assert any(kind == "warning" for kind, *_ in msgboxes)

    definition = SimpleNamespace(
        semantic_sha256="b" * 64, user_label="spk-1", equipment_kind="speaker"
    )
    service.create_user_definition.side_effect = None
    service.create_user_definition.return_value = definition
    dialog.label_edit.setText("spk-1")
    dialog.save_new_button.click()
    assert emitted
    service.create_user_definition.assert_called()


# -- データ管理 (DataManagementDialog close guard) ------------------------------


def test_data_management_close_guard_explains(qapp, msgboxes):
    busy = SimpleNamespace(
        widget=QWidget(),
        before_deactivate=lambda: (False, "バックアップを実行中です"),
    )
    dialog = DataManagementDialog(busy)
    event = QCloseEvent()
    dialog.closeEvent(event)
    assert not event.isAccepted()
    assert any(kind == "information" for kind, *_ in msgboxes)

    idle = SimpleNamespace(
        widget=QWidget(), before_deactivate=lambda: (True, None)
    )
    dialog2 = DataManagementDialog(idle)
    event2 = QCloseEvent()
    dialog2.closeEvent(event2)
    assert event2.isAccepted()


# -- キャプチャ保持管理 (RetentionPolicyWidget purge confirm) --------------------


def _retention_widget() -> RetentionPolicyWidget:
    revision = SimpleNamespace(
        capture_series_id="series-1",
        capture_revision_id="rev-abcdef1234",
        ingestion_run_count=2,
        latest_recorded_at_utc="2026-09-01T00:00:00Z",
    )
    plan = SimpleNamespace(
        status="ready",
        deletable_source_evidence_ids=("e1",),
        deletable_mesh_binding_ids=(),
        deletable_authority_record_ids=(),
        deletable_coordinate_authority_ids=(),
        roomplan_record_count=0,
        retained_source_evidence_ids=(),
        retained_mesh_binding_ids=(),
        retained_authority_record_ids=(),
        retained_coordinate_authority_ids=(),
        reclaimable_bytes=1024,
        reclaimed_blob_sha256=(),
        blocking_dependents=(),
    )
    service = Mock()
    service.inventory.return_value = SimpleNamespace(
        capture_revision_count=1,
        ingestion_run_count=2,
        source_evidence_count=3,
        source_payload_bytes=2048,
        content_blob_count=1,
        content_blob_bytes=1024,
    )
    service.list_capture_revisions.return_value = [revision]
    service.plan_capture_revision_purge.return_value = plan
    service.purge_capture_revision.return_value = plan
    return RetentionPolicyWidget(service)


def test_capture_purge_confirm_names_revision(qapp, msgboxes):
    widget = _retention_widget()
    widget.plan_button.click()
    assert widget.purge_button.isEnabled()
    widget.purge_button.click()
    # The safe default (Cancel) was recorded by the fixture → nothing purged.
    widget._service.purge_capture_revision.assert_not_called()
    assert any(
        "rev-abcdef1234" in text or "rev-abcd" in text
        for _kind, _title, text, _info in msgboxes
    ), "purge confirmation must name the actual revision"


# -- バックアップ復元 (restore confirmation names the archive) ------------------


def test_restore_confirmation_names_backup(qapp, msgboxes):
    from htdt.data_management_ui import _default_restore_confirmation

    preview = SimpleNamespace(
        metadata=SimpleNamespace(
            backup_path=Path("HTDT-backup-2026-01-01-1200.htdtbackup"),
            created_at_utc="2026-01-01T12:00:00+00:00",
        ),
        manifest=SimpleNamespace(),
    )
    assert _default_restore_confirmation(None, preview) is False
    joined = "\n".join(
        f"{title}\n{text}\n{info}" for _k, title, text, info in msgboxes
    )
    assert "HTDT-backup-2026-01-01-1200" in joined
