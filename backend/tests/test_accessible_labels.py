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
