"""Round-22 review: dialog & widget-state truth — the gaps round 14 did not
cover: destructive actions must confirm (naming the affected item, Cancel
truly cancels), action buttons must not sit enabled over a state the handler
silently rejects (dead click), and form wizards must gate Save on required
inputs up front instead of erroring late on a different page.

Every claim is verified by instantiating the widget offscreen and driving it
like a user: rows selected, buttons clicked, enabled state and effects read
back.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMessageBox,
)


from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.capture_receiver_settings import PairingDialog
from htdt.commissioning_wizard import CommissioningWizard
from htdt.equipment_library import EquipmentLibraryDialog
from htdt.standards_profile_editor import StandardsProfileEditorDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def qapp():
    return _app()


@pytest.fixture()
def msgboxes(monkeypatch):
    """Neutralize modal QMessageBox entry points and record what surfaced;
    the safe default returned is Cancel, like a cautious user."""
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


# -- デバイスペアリング revoke (PairingDialog) ---------------------------------


def _pairing(state: str = "active") -> SimpleNamespace:
    return SimpleNamespace(
        pairing_id="pair-1",
        confirmation_code="ABC123",
        state=state,
        receiver_instance_id="receiver-abcdef123456",
        project_ref="doc-1",
    )


def _pairing_dialog(*pairings) -> PairingDialog:
    service = Mock()
    service.list_pairings.return_value = list(pairings)
    controller = SimpleNamespace(service=service)
    return PairingDialog(controller, "doc-1")


def test_pairing_revoke_confirms_and_cancel_preserves(qapp, msgboxes):
    """DESTRUCTIVE CONFIRMS: revoke is irreversible — it must ask first, and
    the cautious-user Cancel must leave the pairing intact."""
    dialog = _pairing_dialog(_pairing())
    dialog.pairing_list.setCurrentRow(0)

    dialog.revoke_button.click()

    # A confirmation surfaced naming the affected device (the list label
    # carries the receiver instance id), and Cancel cancelled — the service
    # was never called.
    assert msgboxes, "revoke must surface a confirmation"
    kind, _title, text, _info = msgboxes[-1]
    assert "receiver-abcdef123456"[:8] in text
    dialog._controller.service.revoke_pairing.assert_not_called()


def test_pairing_revoke_confirmed_executes(qapp, monkeypatch):
    """Answering Yes on the confirm performs the revoke for the selected row."""
    monkeypatch.setattr(
        QMessageBox,
        "exec",
        lambda self: QMessageBox.StandardButton.Yes,
    )
    dialog = _pairing_dialog(_pairing())
    dialog.pairing_list.setCurrentRow(0)

    dialog.revoke_button.click()

    dialog._controller.service.revoke_pairing.assert_called_once_with("pair-1")
    assert "解除しました" in dialog.status_label.text()


# -- 機器ライブラリ dead-click gating (EquipmentLibraryDialog) ------------------


def _equipment_service(definitions=()) -> Mock:
    service = Mock()
    service.definitions.return_value = list(definitions)
    service.supported_directivity_adapters.return_value = []
    return service


def test_equipment_library_selection_actions_disabled_when_empty(qapp, msgboxes):
    """OK-ENABLED-INVALID: with no definition selected, 新バージョン保存 /
    指向性インポート must not sit enabled over a handler that silently
    returns — a click would be a dead click with no feedback."""
    dialog = EquipmentLibraryDialog(_equipment_service())

    assert dialog.definition_list.count() == 0
    assert not dialog.save_version_button.isEnabled()
    assert not dialog.import_button.isEnabled()


def test_equipment_library_actions_track_selection(qapp, msgboxes):
    """Selection state must drive the buttons both ways, and the capability
    preview must not keep showing a definition that is no longer selected."""
    definition = SimpleNamespace(
        semantic_sha256="b" * 64,
        user_label="spk-1",
        equipment_kind="speaker",
        version="1",
        definition_id="def-1",
        manufacturer=None,
        model=None,
        directivity=SimpleNamespace(tier="unknown", data_format=None),
        sensitivity=None,
        spl_capability=None,
        mounting=SimpleNamespace(mounting_modes=()),
        cabinet_envelope_m=SimpleNamespace(x_m=0.25, y_m=0.4, z_m=0.3),
    )
    service = _equipment_service([definition])
    service.equipment_repository.get_definition_by_hash.return_value = definition

    # Items listed but nothing selected → actions stay gated off.
    dialog = EquipmentLibraryDialog(service)
    assert dialog.definition_list.count() == 1
    assert not dialog.save_version_button.isEnabled()
    assert not dialog.import_button.isEnabled()

    # Selecting the definition arms both actions and fills the preview.
    dialog.definition_list.setCurrentRow(0)
    assert dialog.save_version_button.isEnabled()
    assert dialog.import_button.isEnabled()
    assert dialog.preview_label.text()

    # Library emptied on refresh (e.g. definition removed elsewhere):
    # buttons gate off and the stale preview clears.
    service.definitions.return_value = []
    dialog.refresh_definitions()
    assert not dialog.save_version_button.isEnabled()
    assert not dialog.import_button.isEnabled()
    assert dialog.preview_label.text() == ""


# -- 初期設定ウィザード Save gating (CommissioningWizard) -----------------------


def _wizard(tmp_path: Path) -> CommissioningWizard:
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    repository.save(make_empty_scene("seed"), parent_revision_id=None)
    return CommissioningWizard(repository, "seed", data_dir=tmp_path)


def test_wizard_save_gated_on_required_inputs(qapp, tmp_path):
    """UPFRONT GATING: 保存して閉じる stays disabled while the form is in a
    state _resolve_document_id would reject — no dead click, no late error
    dragging the user back to page 0."""
    wizard = _wizard(tmp_path)

    # New-project mode with an empty name → cannot save.
    assert wizard.new_radio.isChecked()
    assert wizard.name_edit.text() == ""
    assert not wizard.save_button.isEnabled()

    # Whitespace-only is still no name.
    wizard.name_edit.setText("   ")
    assert not wizard.save_button.isEnabled()

    # A real name unlocks Save.
    wizard.name_edit.setText("my-theater")
    assert wizard.save_button.isEnabled()

    # Clearing it again re-gates.
    wizard.name_edit.setText("")
    assert not wizard.save_button.isEnabled()


def test_wizard_save_enabled_for_existing_project(qapp, tmp_path):
    """Existing-project mode gates on a selected document, not the name."""
    wizard = _wizard(tmp_path)
    wizard.existing_radio.setChecked(True)
    assert wizard.existing_combo.currentData() == "seed"
    assert wizard.save_button.isEnabled()

    # Switching back to new-project mode re-applies the name requirement.
    wizard.new_radio.setChecked(True)
    assert not wizard.save_button.isEnabled()


# -- 基準削除 dead-click gating (StandardsProfileEditorDialog) ------------------


def _standards_service() -> Mock:
    criterion = SimpleNamespace(
        criterion_id="crit-1",
        quantity="viewing_angle",
        unit="deg",
        rule=SimpleNamespace(
            operator="range", minimum=1.0, maximum=5.0, expected=None
        ),
        source=SimpleNamespace(
            publisher="CTA",
            document_title="CTA-2034",
            document_version="1",
            reference="p.1",
        ),
    )
    profile = SimpleNamespace(
        name="custom",
        version="1",
        profile_id="prof-1",
        profile_kind="user_defined",
        criteria=[criterion],
    )
    service = Mock()
    service.profiles.return_value = [profile]
    return service


def test_standards_remove_criterion_gated_on_selection(qapp):
    """DEAD CLICK: 基準を削除 must not sit enabled with no row selected —
    the handler would silently no-op."""
    dialog = StandardsProfileEditorDialog(_standards_service())
    dialog.profile_combo.setCurrentIndex(1)

    # Profile loads → criteria table populated, nothing selected.
    assert dialog.criteria_table.rowCount() == 1
    assert not dialog.remove_criterion_button.isEnabled()

    # Selecting a row arms the button; removing it re-gates.
    dialog.criteria_table.selectRow(0)
    assert dialog.remove_criterion_button.isEnabled()
    dialog.remove_criterion_button.click()
    assert dialog.criteria_table.rowCount() == 0
    assert not dialog.remove_criterion_button.isEnabled()
