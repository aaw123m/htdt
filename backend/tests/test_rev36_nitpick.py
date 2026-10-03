"""REV36-NITPICK: sketch Enter-to-commit + ruler first tick (workflow path).

1. 「Enter で閉じる」 never fired: ``'Enter'`` parses to ``Qt.Key_Enter``
   (numpad) while the main keyboard emits ``Qt.Key_Return``. The workflow
   path carries a ``Return`` alias on ``room.edit.commit``.
2. The sketch cursor preview is a ``pv.Line`` whose ``'Distance'`` scalars
   auto-show a scalar bar; on first render the segment is ~0-length so the
   bar's first range is ~1e-7 — a stale tick. It opts out via
   ``show_scalar_bar=False``.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import htdt.data_relocation  # noqa: F401  (must precede PySide6 import)

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QWidget

from htdt.cad_input import (
    CadCommandBindings,
    CadInputController,
    bind_cad_input_commands,
)
from htdt.cad_scene import RoomVertex
from htdt.command_registry import (
    CommandRegistry,
    default_command_definitions,
    register_default_commands,
)
from htdt.room_geometry_input import RoomGeometryInputController

from test_cad_input import _ViewportPort


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# Enter/Return covers the main keyboard, not just the numpad
# ---------------------------------------------------------------------------


def test_commit_command_covers_main_and_numpad_enter() -> None:
    definitions = {item.command_id: item for item in default_command_definitions()}
    commit = definitions["room.edit.commit"]
    assert commit.shortcut == "Enter"
    assert "Return" in commit.shortcut_aliases
    # 'Enter' is Qt.Key_Enter (numpad); 'Return' is Qt.Key_Return (main).
    assert QKeySequence(commit.shortcut) == QKeySequence(Qt.Key.Key_Enter)
    assert QKeySequence("Return") == QKeySequence(Qt.Key.Key_Return)
    assert QKeySequence(commit.shortcut) != QKeySequence("Return")


def test_commit_binds_and_fires_on_both_enter_keys() -> None:
    app = _app()
    host = QWidget()
    registry = CommandRegistry()
    register_default_commands(registry)
    calls: list[str] = []
    bind_cad_input_commands(
        registry,
        CadCommandBindings(commit=lambda: calls.append("commit")),
    )
    controller = CadInputController(
        shortcut_parent=host,
        viewport=QWidget(host),
        registry=registry,
        viewport_port=_ViewportPort(),
    )
    host.show()
    app.processEvents()

    commit_shortcuts = [
        shortcut
        for command_id, shortcut in controller.shortcuts._shortcuts
        if command_id == "room.edit.commit"
    ]
    assert {shortcut.key() for shortcut in commit_shortcuts} == {
        QKeySequence(Qt.Key.Key_Enter),
        QKeySequence(Qt.Key.Key_Return),
    }
    for shortcut in commit_shortcuts:
        assert shortcut.isEnabled()
        shortcut.activated.emit()
    assert calls == ["commit", "commit"]

    controller.dispose()
    host.close()
    host.deleteLater()
    app.processEvents()


def test_commit_shortcuts_yield_to_text_input() -> None:
    from PySide6.QtWidgets import QLineEdit, QVBoxLayout

    app = _app()
    host = QWidget()
    layout = QVBoxLayout(host)
    viewport = QWidget(host)
    viewport.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    field = QLineEdit(host)
    layout.addWidget(viewport)
    layout.addWidget(field)
    registry = CommandRegistry()
    register_default_commands(registry)
    bind_cad_input_commands(registry, CadCommandBindings(commit=lambda: None))
    controller = CadInputController(
        shortcut_parent=host,
        viewport=viewport,
        registry=registry,
        viewport_port=_ViewportPort(),
    )
    host.show()
    app.processEvents()

    commit_shortcuts = [
        shortcut
        for command_id, shortcut in controller.shortcuts._shortcuts
        if command_id == "room.edit.commit"
    ]
    field.setFocus()
    app.processEvents()
    controller.refresh_shortcuts()
    assert all(not shortcut.isEnabled() for shortcut in commit_shortcuts)

    viewport.setFocus()
    app.processEvents()
    controller.refresh_shortcuts()
    assert all(shortcut.isEnabled() for shortcut in commit_shortcuts)

    controller.dispose()
    host.close()
    host.deleteLater()
    app.processEvents()


# ---------------------------------------------------------------------------
# Sketch cursor preview: no auto Distance scalar bar (stale ~1e-7 tick)
# ---------------------------------------------------------------------------


def test_workflow_sketch_cursor_line_hides_distance_scalar_bar(monkeypatch) -> None:
    from test_room_cadux import FakeRoomViewport

    _app()
    viewport = FakeRoomViewport()
    controller = RoomGeometryInputController(QWidget(), viewport)
    controller._sketch = [
        RoomVertex(vertex_id="sk-0", x_m=0.0, y_m=0.0),
        RoomVertex(vertex_id="sk-1", x_m=6.0, y_m=0.0),
    ]
    controller._cursor = (6.0, 0.0000001)
    calls: list[tuple[object, object]] = []

    def spy(_mesh, *args, **kwargs):
        calls.append((kwargs.get("name"), kwargs.get("show_scalar_bar")))

    monkeypatch.setattr(viewport.plotter, "add_mesh", spy)
    controller._render_sketch()
    # The cursor preview is the only 'Distance'-scalar line; it must opt out
    # of the scalar bar that shows a stale ~1e-7 tick on first render.
    assert ("ux120-room-sketch-cursor", False) in calls
    controller.dispose()
