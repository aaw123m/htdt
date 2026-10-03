"""REV36-NITPICK: sketch Enter-to-commit on both paths + ruler first tick.

1. 「Enter で閉じる」 never fired: ``'Enter'`` parses to ``Qt.Key_Enter``
   (numpad) while the main keyboard emits ``Qt.Key_Return``, and the
   legacy editor's interactor-level key filter never ran because the
   QVTK interactor never takes Qt focus. The workflow path now carries a
   ``Return`` alias on ``room.edit.commit``; the legacy path binds real
   window-level QShortcuts.
2. The bounds ruler (CubeAxesActor) used to be created before any scene
   content existed, so its first sampled extent was the post-clear
   degenerate one — a stale ~1e-7 tick until follow-scene recalibrated.
   ``_add_scene_axes`` now runs after the scene and falls back to the
   default floor patch when the scene has no usable bounds.
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
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, RoomVertex, make_f1_scene
from htdt.command_registry import (
    CommandRegistry,
    default_command_definitions,
    register_default_commands,
)
from htdt.native_editor import NativeEditorWindow
from htdt.room_editor import RoomEditorWindow
from htdt.room_geometry_input import RoomGeometryInputController
from htdt.room_viewport import (
    DEFAULT_EMPTY_SCENE_BOUNDS,
    scene_has_usable_bounds,
)
from htdt.theater_editor import TheaterEditorWindow

from test_cad_input import _ViewportPort


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _repository(tmp_path, scene=None):
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(scene or make_f1_scene(), parent_revision_id=None)
    return repository


def _window(repository, cls):
    _app()
    window = cls(repository, F1_DOCUMENT_ID)
    assert window.working is not None
    return window


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
# Legacy RoomEditorWindow: window-level shortcuts replace the dead interactor
# eventFilter (the QVTK interactor never takes Qt focus).
# ---------------------------------------------------------------------------


def _shortcut_for_key(window: RoomEditorWindow, key: Qt.Key):
    return next(
        shortcut
        for shortcut, _modes in window._room_mode_shortcuts
        if shortcut.key() == QKeySequence(key)
    )


def test_legacy_return_and_enter_close_the_sketch(tmp_path) -> None:
    window = _window(_repository(tmp_path), RoomEditorWindow)
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        window.cancel_preview()
        window.start_room_sketch()
        assert window.room_mode == 'sketch'
        window.room_sketch_vertices = [
            RoomVertex(vertex_id=f"sk-{i}", x_m=x, y_m=y)
            for i, (x, y) in enumerate(
                ((0.0, 0.0), (6.0, 0.0), (6.0, 4.0), (0.0, 4.0))
            )
        ]
        shortcut = _shortcut_for_key(window, key)
        assert shortcut.isEnabled()
        shortcut.activated.emit()
        assert window.room_mode == 'edit'
    window.deleteLater()


def test_legacy_enter_shortcuts_only_live_in_sketch_mode(tmp_path) -> None:
    window = _window(_repository(tmp_path), RoomEditorWindow)
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        assert _shortcut_for_key(window, key).isEnabled() is False
    window.start_room_edit()
    assert window.room_mode == 'edit'
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        assert _shortcut_for_key(window, key).isEnabled() is False
    window.finish_room_edit()
    window.deleteLater()


def test_legacy_escape_cancels_sketch_and_edit_cancel(tmp_path) -> None:
    window = _window(_repository(tmp_path), RoomEditorWindow)
    escape = _shortcut_for_key(window, Qt.Key.Key_Escape)
    assert escape.isEnabled() is False

    window.start_room_sketch()
    assert escape.isEnabled()
    calls: list[str] = []
    window.cancel_preview = lambda: calls.append("cancel")
    escape.activated.emit()
    assert calls == ["cancel"]
    window.deleteLater()


def test_legacy_room_shortcuts_yield_to_text_input(tmp_path, monkeypatch) -> None:
    import htdt.room_editor as room_editor_module

    window = _window(_repository(tmp_path), RoomEditorWindow)
    window.start_room_sketch()
    shortcuts = [shortcut for shortcut, _modes in window._room_mode_shortcuts]
    assert all(shortcut.isEnabled() for shortcut in shortcuts)

    # Text-focus gating is driven by the same predicate the workflow binder
    # uses; focus delivery to dock children is unreliable offscreen, so the
    # predicate is stubbed while the predicate itself is checked for real.
    assert room_editor_module.is_text_input_widget(window.room_height)
    monkeypatch.setattr(
        room_editor_module, "is_text_input_widget", lambda _widget: True
    )
    window._refresh_room_mode_shortcuts()
    assert all(not shortcut.isEnabled() for shortcut in shortcuts)

    monkeypatch.undo()
    window._refresh_room_mode_shortcuts()
    assert all(shortcut.isEnabled() for shortcut in shortcuts)
    window.deleteLater()


# ---------------------------------------------------------------------------
# Bounds ruler: first tick samples real bounds (or the default floor patch)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "window_cls",
    [NativeEditorWindow, RoomEditorWindow, TheaterEditorWindow],
)
def test_scene_grid_first_tick_has_real_bounds(tmp_path, window_cls) -> None:
    window = _window(_repository(tmp_path), window_cls)
    actor = window.viewport.renderer.cube_axes_actor
    assert actor is not None
    bounds = tuple(float(value) for value in actor.bounds)
    extents = (bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4])
    # The pre-fix ruler was born on the post-clear degenerate extent and
    # showed a ~1e-7 tick; the F1 scene spans several metres on every axis.
    assert all(extent > 0.5 for extent in extents)
    window.deleteLater()


def test_scene_grid_created_after_content(tmp_path, monkeypatch) -> None:
    window = _window(_repository(tmp_path), RoomEditorWindow)
    calls: list[tuple[bool, object]] = []
    original = window.viewport.show_grid

    def spy(*args, **kwargs):
        calls.append(
            (scene_has_usable_bounds(window.viewport), kwargs.get("bounds"))
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(window.viewport, "show_grid", spy)
    window._rebuild()
    assert calls == [(True, None)]
    window.deleteLater()


def test_sketch_cursor_line_hides_distance_scalar_bar(tmp_path) -> None:
    window = _window(_repository(tmp_path), RoomEditorWindow)
    window.start_room_sketch()
    window.room_sketch_vertices = [
        RoomVertex(vertex_id="sk-0", x_m=0.0, y_m=0.0),
        RoomVertex(vertex_id="sk-1", x_m=6.0, y_m=0.0),
    ]
    window.room_cursor_xy = (6.0, 0.0000001)
    window._render_room_sketch_overlay()
    # pv.Line's 'Distance' scalars auto-show a scalar bar whose first range
    # is ~1e-7 — the stale tick the report flagged. It must stay hidden.
    assert "Distance" not in window.viewport.scalar_bars
    window.deleteLater()


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


def test_empty_scene_grid_uses_default_floor_bounds(tmp_path, monkeypatch) -> None:
    scene = make_f1_scene().model_copy(update={"room": None, "entities": ()})
    window = _window(_repository(tmp_path, scene), RoomEditorWindow)
    calls: list[tuple[bool, object]] = []
    original = window.viewport.show_grid

    def spy(*args, **kwargs):
        calls.append(
            (scene_has_usable_bounds(window.viewport), kwargs.get("bounds"))
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(window.viewport, "show_grid", spy)
    window._rebuild()
    # No scene actors: the ruler is initialized to the default floor patch
    # instead of sampling the degenerate ~1e-9 extent.
    assert calls == [(False, DEFAULT_EMPTY_SCENE_BOUNDS)]
    actor = window.viewport.renderer.cube_axes_actor
    assert actor is not None
    assert tuple(float(v) for v in actor.bounds) == pytest.approx(
        DEFAULT_EMPTY_SCENE_BOUNDS
    )
    window.deleteLater()
