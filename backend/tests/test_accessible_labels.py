"""Round-23 accessibility regression tests.

Covers the verified gaps the round fixed:
- orphan caption labels never reached assistive tech (buddy wiring)
- compact rail buttons announced only a truncated glyph
- Ctrl+S/project.save was dead outside the Room/Optimization workspaces
- the command palette swallowed focus on close
- the project menubar offered no keyboard mnemonics
- caption-less controls across workspaces had empty accessible names
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollBar,
    QTabWidget,
    QTextEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from htdt.accessible_labels import resolved_accessible_name, wire_label_buddies
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.workflow_application import WorkflowApplicationComposition
from htdt.workflow_navigation import ApplicationDestinationId, WorkspaceId


@pytest.fixture(autouse=True)
def _no_blocking_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offscreen runs can never dismiss modal dialogs; auto-accept them."""

    def _accept(*_args, **_kwargs):
        return QDialog.DialogCode.Accepted

    def _static(*_args, **_kwargs):
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QDialog, "exec", _accept)
    monkeypatch.setattr(QMessageBox, "exec", _accept)
    for name in ("question", "information", "warning", "critical", "about"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(_static))


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _composition(tmp_path: Path) -> WorkflowApplicationComposition:
    _app()  # QWidget construction requires an existing QApplication
    repository = SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3")
    return WorkflowApplicationComposition(repository, "document-1")


# --- helper unit tests -------------------------------------------------------


def test_caption_label_buddies_following_control() -> None:
    _app()
    host = QWidget()
    layout = QVBoxLayout(host)
    label = QLabel("長さ")
    field = QLineEdit()
    layout.addWidget(label)
    layout.addWidget(field)

    assert wire_label_buddies(host) == 1
    assert label.buddy() is field
    assert resolved_accessible_name(field) == "長さ"


def test_caption_buddies_control_inside_nested_row() -> None:
    _app()
    host = QWidget()
    layout = QVBoxLayout(host)
    label = QLabel("幅")
    row = QHBoxLayout()
    combo = QComboBox()
    row.addWidget(combo)
    layout.addWidget(label)
    layout.addLayout(row)

    assert wire_label_buddies(host) == 1
    assert label.buddy() is combo
    assert resolved_accessible_name(combo) == "幅"


def test_section_header_does_not_buddy_button() -> None:
    _app()
    host = QWidget()
    layout = QVBoxLayout(host)
    heading = QLabel("設定")
    layout.addWidget(heading)
    layout.addWidget(QPushButton("保存"))

    assert wire_label_buddies(host) == 0
    assert heading.buddy() is None


def test_separator_labels_never_become_names() -> None:
    _app()
    host = QWidget()
    layout = QHBoxLayout(host)
    low = QDoubleSpinBox()
    sep = QLabel("〜")
    high = QDoubleSpinBox()
    layout.addWidget(low)
    layout.addWidget(sep)
    layout.addWidget(high)

    assert wire_label_buddies(host) == 0
    assert resolved_accessible_name(low) == ""
    assert resolved_accessible_name(high) == ""


def test_form_row_container_names_inner_control() -> None:
    _app()
    host = QWidget()
    form = QFormLayout(host)
    band = QWidget()
    band_row = QHBoxLayout(band)
    band_row.setContentsMargins(0, 0, 0, 0)
    low = QDoubleSpinBox()
    band_row.addWidget(low)
    form.addRow("帯域 Hz", band)

    assert wire_label_buddies(host) == 1
    assert resolved_accessible_name(low) == "帯域 Hz"


def test_existing_buddy_is_preserved() -> None:
    _app()
    host = QWidget()
    layout = QHBoxLayout(host)
    label = QLabel("高さ")
    first = QLineEdit()
    second = QLineEdit()
    label.setBuddy(first)
    layout.addWidget(label)
    layout.addWidget(first)
    layout.addWidget(second)

    assert wire_label_buddies(host) == 0
    assert label.buddy() is first


def test_named_controls_are_not_renamed() -> None:
    _app()
    host = QWidget()
    layout = QVBoxLayout(host)
    label = QLabel("名前")
    field = QLineEdit()
    field.setAccessibleName("既存の名前")
    layout.addWidget(label)
    layout.addWidget(field)

    assert wire_label_buddies(host) == 0
    assert resolved_accessible_name(field) == "既存の名前"


# --- shell-level regressions -------------------------------------------------


def _all_widgets(root: QWidget):
    stack = [root]
    while stack:
        widget = stack.pop()
        yield widget
        stack.extend(widget.children())


def _inside_combo(widget: QWidget) -> bool:
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QComboBox):
            return True
        parent = parent.parentWidget()
    return False


def _unnamed_controls(root: QWidget):
    labelable = (
        QComboBox,
        QLineEdit,
        QAbstractSpinBox,
        QAbstractItemView,
        QTabWidget,
        QAbstractSlider,
        QTextEdit,
        QPlainTextEdit,
        QAbstractButton,
    )
    for widget in _all_widgets(root):
        if not isinstance(widget, labelable):
            continue
        if widget.objectName().startswith("qt_"):
            continue
        if isinstance(widget, (QScrollBar, QHeaderView)):
            continue  # part of its scroll area / table, not an independent control
        if _inside_combo(widget):  # popup view / inline editor are internals
            continue
        if isinstance(widget.parentWidget(), QLineEdit):
            continue  # Qt-internal line-edit action buttons
        # Hidden buttons that only receive text when shown are not gaps.
        if isinstance(widget, QAbstractButton) and widget.isHidden():
            continue
        if not resolved_accessible_name(widget):
            yield widget


def test_mounted_destinations_have_no_unnamed_controls(tmp_path: Path) -> None:
    app = _app()
    composition = _composition(tmp_path)
    shell = composition.shell
    for destination in (*WorkspaceId, *ApplicationDestinationId):
        shell.navigate(destination)
        app.processEvents()

    leftovers = []
    for _destination, mount in shell.router.mounts():
        leftovers.extend(_unnamed_controls(mount.widget))
    assert leftovers == []


def test_compact_rail_keeps_full_accessible_name(tmp_path: Path) -> None:
    app = _app()
    composition = _composition(tmp_path)
    rail = composition.shell.rail
    rail.set_compact(True)
    app.processEvents()

    for button in rail.findChildren(QAbstractButton):
        if button.accessibleName() == "プロジェクト":
            assert len(button.accessibleName()) > len(button.text())
            break
    else:
        raise AssertionError("project rail button missing accessible name")


def test_ctrl_s_binds_project_save_window_wide(tmp_path: Path) -> None:
    composition = _composition(tmp_path)
    shell = composition.shell

    matches = [
        shortcut
        for shortcut in shell.findChildren(QShortcut)
        if shortcut.key().toString() == "Ctrl+S"
    ]
    assert matches, "Ctrl+S shortcut missing on shell"
    assert any(
        shortcut.context() == Qt.ShortcutContext.WindowShortcut
        for shortcut in matches
    )


def test_project_menu_entries_have_unique_mnemonics(tmp_path: Path) -> None:
    composition = _composition(tmp_path)
    menubar = composition.shell.menuBar()

    project_action = next(
        action
        for action in menubar.actions()
        if action.menu() is not None and "プロジェクト" in action.text()
    )
    assert "&" in project_action.text()

    letters = []
    for action in project_action.menu().actions():
        text = action.text()
        if action.isSeparator() or not text:
            continue
        marker = text.find("&")
        assert marker >= 0, f"menu item lacks mnemonic: {text}"
        letters.append(text[marker + 1].upper())
    assert len(letters) == len(set(letters)), f"duplicate mnemonics: {letters}"


def test_palette_search_field_is_named(tmp_path: Path) -> None:
    composition = _composition(tmp_path)
    palette = composition.command_palette.palette
    assert palette.search_field.accessibleName() != ""


def test_palette_restores_focus_on_close(tmp_path: Path) -> None:
    app = _app()
    composition = _composition(tmp_path)
    shell = composition.shell
    shell.show()
    QApplication.setActiveWindow(shell)
    app.processEvents()

    shell.navigate(WorkspaceId.OVERVIEW)
    app.processEvents()

    target = shell.findChild(QPushButton)
    if target is None:
        target = shell.menuBar()
    target.setFocus()
    app.processEvents()
    remembered = QApplication.focusWidget()
    assert remembered is not None

    palette = composition.command_palette.palette
    palette.prepare_to_show()
    palette.show()
    app.processEvents()

    palette.hide()
    # Qt moves focus during hide; the deferred restore lands after settle.
    for _ in range(20):
        app.processEvents()
        if QApplication.focusWidget() is remembered:
            break
    assert QApplication.focusWidget() is remembered


def test_workspace_save_fallback_saves_dirty_room(tmp_path: Path) -> None:
    """Ctrl+S must reach project.save even with no Room/Opt binding."""

    app = _app()
    repository = SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    composition = WorkflowApplicationComposition(repository, F1_DOCUMENT_ID)
    shell = composition.shell
    shell.navigate(WorkspaceId.ROOM)
    app.processEvents()

    room_mount = None
    for destination, mount in shell.router.mounts():
        if destination is WorkspaceId.ROOM:
            room_mount = mount
    assert room_mount is not None

    controller = room_mount.widget.controller
    controller.add_object("speaker")
    assert controller.is_dirty

    # Overview activation rebinds the dispatcher fallback; executing the
    # command must flush the dirty workspace regardless of focus location.
    shell.navigate(WorkspaceId.OVERVIEW)
    app.processEvents()
    assert composition.registry.execute("project.save")
    assert not controller.is_dirty


# --- round-25 depth pass --------------------------------------------------
#
# REV25-A11Y2: standalone dialogs were never swept by wire_label_buddies,
# status-bar/status-label updates never reached screen readers, the VTK
# viewports were anonymous focus targets, and the Room context strip's
# view menu opened at the mouse cursor even when activated by keyboard.


def _names(widgets) -> str:
    return ", ".join(
        f"{type(w).__name__}#{w.objectName() or '?'}" for w in widgets
    )


def test_standalone_dialogs_have_no_unnamed_controls(tmp_path: Path) -> None:
    """Sweep every standalone dialog: caption-wired controls + named views."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from htdt.authority_graph import (
        AuthorityDomain,
        AuthorityLifecycle,
        AuthorityNode,
        StaticAuthoritySource,
        build_authority_graph,
    )
    from htdt.authority_inspector_ui import AuthorityInspectorDialog
    from htdt.cad_scene import make_empty_scene
    from htdt.capture_receiver_settings import PairingDialog
    from htdt.capture_retention_ui import RetentionPolicyWidget
    from htdt.commissioning_wizard import CommissioningWizard
    from htdt.equipment_library import EquipmentLibraryDialog
    from htdt.geometry_import_dialog import GeometryImportDialog
    from htdt.playback_chain_widgets import PlaybackChainDialog
    from htdt.solver_output_diagnostics_ui import (
        SolverOutputDiagnosticsDialog,
    )
    from htdt.solver_output_ledger import SolverOutputLedger
    from htdt.standards_profile_editor import StandardsProfileEditorDialog

    _app()
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    repository.save(make_empty_scene("doc-1"), parent_revision_id=None)

    pairing_service = Mock()
    pairing_service.list_pairings.return_value = []
    pairing_controller = SimpleNamespace(service=pairing_service)

    retention_service = Mock()
    retention_service.inventory.return_value = SimpleNamespace(
        capture_revision_count=0,
        ingestion_run_count=0,
        source_evidence_count=0,
        source_payload_bytes=0,
        content_blob_count=0,
        content_blob_bytes=0,
    )
    retention_service.list_capture_revisions.return_value = []

    playback_service = Mock()
    playback_service.amplifier_capabilities.return_value = []
    playback_service.speaker_loads.return_value = []
    playback_service.variants.return_value = []
    playback_service.speaker_entities.return_value = []
    playback_service.source_equipment_choices.return_value = []

    equipment_service = Mock()
    equipment_service.definitions.return_value = []
    equipment_service.supported_directivity_adapters.return_value = []

    standards_service = Mock()
    standards_service.profiles.return_value = []

    node = AuthorityNode(
        node_id="room:scene_revision:rev-1",
        domain=AuthorityDomain.ROOM,
        node_type="scene_revision",
        label="SceneRevision rev-1",
        lifecycle=AuthorityLifecycle.CURRENT,
    )
    graph = build_authority_graph([StaticAuthoritySource([node])])

    obj = tmp_path / "triangle.obj"
    obj.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")

    dialogs = {
        "solver diagnostics": SolverOutputDiagnosticsDialog(
            SolverOutputLedger(document_id="doc-1", entries=()), ()
        ),
        "authority inspector": AuthorityInspectorDialog(graph),
        "equipment library": EquipmentLibraryDialog(equipment_service),
        "standards editor": StandardsProfileEditorDialog(standards_service),
        "commissioning wizard": CommissioningWizard(repository, "doc-1"),
        "pairing": PairingDialog(pairing_controller, "doc-1"),
        "capture retention": RetentionPolicyWidget(retention_service),
        "geometry import": GeometryImportDialog(obj),
        "playback chain": PlaybackChainDialog(playback_service),
    }
    leftovers = {
        name: list(_unnamed_controls(dialog))
        for name, dialog in dialogs.items()
    }
    leftovers = {name: found for name, found in leftovers.items() if found}
    assert leftovers == {}, {
        name: _names(found) for name, found in leftovers.items()
    }


def _announcement_sink(monkeypatch: pytest.MonkeyPatch):
    """Capture QAccessible.updateAccessibility events instead of sending."""

    from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent

    events: list[QAccessibleAnnouncementEvent] = []
    monkeypatch.setattr(
        QAccessible, "updateAccessibility", lambda event: events.append(event)
    )
    return events, QAccessibleAnnouncementEvent


def test_status_bar_message_is_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every showMessage() call site is a live region via messageChanged."""
    from PySide6.QtGui import QAccessible

    events, announcement = _announcement_sink(monkeypatch)
    composition = _composition(tmp_path)
    composition.shell.statusBar().showMessage("保存しました")

    announcements = [
        event for event in events if isinstance(event, announcement)
    ]
    assert [event.message() for event in announcements] == ["保存しました"]
    assert all(
        event.politeness() == QAccessible.AnnouncementPoliteness.Polite
        for event in announcements
    )


def test_status_bar_clear_announces_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clearMessage() must not announce an empty string."""

    events, _announcement = _announcement_sink(monkeypatch)
    composition = _composition(tmp_path)
    composition.shell.statusBar().clearMessage()
    assert events == []


def test_native_editor_announces_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The N20b shell has its own status bar — wired the same way."""
    from htdt.native_editor import NativeEditorWindow

    events, announcement = _announcement_sink(monkeypatch)
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    window = NativeEditorWindow(repository, F1_DOCUMENT_ID)
    events.clear()  # construction announces its own load status first
    window.statusBar().showMessage("スナップ: 頂点")
    assert [
        event.message()
        for event in events
        if isinstance(event, announcement)
    ] == ["スナップ: 頂点"]


def test_native_editor_surfaces_are_named(tmp_path: Path) -> None:
    from htdt.native_editor import NativeEditorWindow

    _app()
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    window = NativeEditorWindow(repository, F1_DOCUMENT_ID)
    assert window.tree.accessibleName() == "シーンツリー"
    assert window.viewport.interactor.accessibleName() == "シーン3Dビュー"


def test_room_viewport_interactor_is_named(tmp_path: Path) -> None:
    app = _app()
    composition = _composition(tmp_path)
    composition.shell.navigate(WorkspaceId.ROOM)
    app.processEvents()
    mounts = dict(composition.shell.router.mounts())
    room = mounts[WorkspaceId.ROOM].widget
    assert room.viewport.interactor.accessibleName() == "部屋3Dビュー"


def test_pick_one_list_takes_prompt_as_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ad-hoc picker buddies its prompt label onto the list."""
    from PySide6.QtWidgets import QListWidget

    _app()
    composition = _composition(tmp_path)
    seen: dict[str, QDialog] = {}
    monkeypatch.setattr(
        QDialog,
        "exec",
        lambda self: seen.setdefault("dialog", self)
        and QDialog.DialogCode.Accepted,
    )
    composition._pick_one("選択", "対象を選択してください", [("項目A", "id-a")])
    listing = seen["dialog"].findChild(QListWidget)
    assert listing is not None
    assert resolved_accessible_name(listing) == "対象を選択してください"


def test_handoff_preview_text_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installation-handoff preview names its read-only editor."""
    from unittest.mock import Mock

    from PySide6.QtWidgets import QPlainTextEdit

    from htdt.cad_scene import make_empty_scene

    _app()
    repository = SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3")
    repository.save(make_empty_scene("document-1"), parent_revision_id=None)
    composition = WorkflowApplicationComposition(repository, "document-1")
    monkeypatch.setattr(
        "htdt.workflow_application.build_installation_handoff",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "htdt.workflow_application.handoff_preview_text",
        lambda handoff: "本文",
    )
    # An accepted preview proceeds to the OS directory picker — cut it off
    # after capture; the dialog itself is what this test inspects.
    monkeypatch.setattr(
        "htdt.workflow_application.file_dialog_memory.get_existing_directory",
        lambda *args, **kwargs: "",
    )
    # The two _pick_one dialogs precede the preview; the preview is the
    # last dialog exec'd.
    opened: list[QDialog] = []
    monkeypatch.setattr(
        QDialog,
        "exec",
        lambda self: opened.append(self) or QDialog.DialogCode.Accepted,
    )
    composition._export_installation_handoff()
    preview = opened[-1]
    preview_text = preview.findChild(QPlainTextEdit)
    assert preview_text is not None
    assert preview_text.accessibleName() == "設置ハンドオフ内容プレビュー"


def test_wizard_page_switch_announces_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wizard page changes are spoken — the page swap is silent otherwise."""
    from htdt.cad_scene import make_empty_scene
    from htdt.commissioning_wizard import CommissioningWizard

    events, announcement = _announcement_sink(monkeypatch)
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    repository.save(make_empty_scene("doc-1"), parent_revision_id=None)
    wizard = CommissioningWizard(repository, "doc-1")
    events.clear()
    wizard._show_page(1)
    announcements = [
        event.message()
        for event in events
        if isinstance(event, announcement)
    ]
    assert announcements == ["初期設定 — 部屋"]


def test_pairing_status_changes_announce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async pairing outcomes reach screen readers, not just the label."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from htdt.capture_receiver_settings import PairingDialog

    events, announcement = _announcement_sink(monkeypatch)
    service = Mock()
    service.list_pairings.return_value = []
    dialog = PairingDialog(SimpleNamespace(service=service), "doc-1")
    events.clear()
    dialog._set_status("確認コードが一致しません。")
    assert [
        event.message()
        for event in events
        if isinstance(event, announcement)
    ] == ["確認コードが一致しません。"]


def test_view_menu_pops_under_focused_tool_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keyboard activation of ビュー anchors the menu to the button."""
    from PySide6.QtWidgets import QMenu

    app = _app()
    composition = _composition(tmp_path)
    shell = composition.shell
    shell.show()
    QApplication.setActiveWindow(shell)
    shell.navigate(WorkspaceId.ROOM)
    app.processEvents()
    mounts = dict(shell.router.mounts())
    room = mounts[WorkspaceId.ROOM].widget

    room.tools.set_context("objects")
    view_button = next(
        button
        for button in room.tools.findChildren(QPushButton)
        if button.text() == "ビュー"
    )
    view_button.setFocus()
    app.processEvents()

    popped: list = []
    monkeypatch.setattr(
        QMenu, "popup", lambda self, pos, *args: popped.append(pos)
    )
    room._tool_requested("view-menu")
    assert popped
    expected = view_button.mapToGlobal(view_button.rect().bottomLeft())
    assert popped[0] == expected
