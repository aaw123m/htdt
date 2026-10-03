"""Round-8 CAD editing-experience depth regression coverage.

Covers the daily-use surface that was dead/incomplete before this round:
- real QShortcuts for the daily edit verbs (Delete/H/L/T, Ctrl+A/Ctrl+I);
- select-all / invert / none;
- Esc falls back to clearing the selection once nothing is in flight;
- zoom-to-selection actually frames bounds (not just recentering);
- Delete targets the selected vertex/wall while the geometry editor is active;
- arrow-key nudge parity for entities (was React-SPA vertices only);
- measure result text + clipboard follow the #496 display-unit policy;
- underlay clicks fall through to deselect/free-point when not calibrating.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QFrame, QWidget

from htdt.cad_display_units import display_length_policy
from htdt.cad_input import CAD_SHORTCUT_COMMAND_IDS
from htdt.cad_measure import build_distance_result, MeasureEndpoint
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    RoomVertex,
    SceneDocument,
    domain_to_render,
    make_f1_scene,
    room_vertices,
)
from htdt.command_palette import CommandShortcutBinder
from htdt.command_registry import CommandRegistry, register_default_commands
from htdt.room_geometry_input import RoomGeometryInputController
from htdt.room_measure_input import RoomMeasureController, RoomMeasurePanel
from htdt.room_transform_input import RoomEntityTransformController
from htdt.room_viewport import (
    DEFAULT_EMPTY_SCENE_BOUNDS,
    RoomViewport3D,
    reset_camera_or_floor_default,
)
from htdt.room_workspace import RoomWorkspace


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _f1_repository(tmp_path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


class _FakePlotter:
    def __init__(self) -> None:
        self.reset_bounds = []

    def remove_actor(self, *_args, **_kwargs) -> None:
        pass

    def render(self) -> None:
        pass

    def add_mesh(self, *_args, **_kwargs):
        return None

    def view_xy(self, **_kwargs) -> None:
        pass

    def enable_parallel_projection(self, *_args, **_kwargs) -> None:
        pass

    def reset_camera(self, render=True, bounds=None) -> None:
        self.reset_bounds.append(bounds)


class FakeRoomViewport(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.interactor = self
        self.plotter = _FakePlotter()
        self.render_calls = []
        self.focused: list[str] = []
        self.focused_ids: list[tuple] = []
        self.fit_count = 0
        self._last_pos = QPointF(10.0, 10.0)
        self.pick_result = (1.0, -2.0, 0.0)

    def render_document(self, document, **kwargs) -> None:
        self.render_calls.append(kwargs)

    def fit_scene(self) -> None:
        self.fit_count += 1

    def focus_entity(self, entity_id: str) -> None:
        self.focused.append(entity_id)

    def focus_entities(self, entity_ids) -> None:
        self.focused_ids.append(tuple(entity_ids))

    def _last_display_position(self) -> QPointF:
        return QPointF(self._last_pos)

    def pick_world_position(self, _position):
        return self.pick_result


def _workspace(tmp_path, viewport_cls=FakeRoomViewport):
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: viewport_cls(parent),
    )
    return app, workspace


def _empty_workspace(tmp_path, viewport_cls=FakeRoomViewport):
    """Workspace backed by a document with no room and no entities — the
    first-run state a brand-new project starts in."""
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(
        SceneDocument(document_id=F1_DOCUMENT_ID, room=None, entities=()),
        parent_revision_id=None,
    )
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: viewport_cls(parent),
    )
    return app, workspace


class MeasureViewport(FakeRoomViewport):
    """Viewport double that applies the real overlay's Position3 contract."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.measure_calls: list[tuple] = []

    def render_measure_overlay(self, result, *, draft_endpoints=()) -> None:
        for position in draft_endpoints:
            domain_to_render(position)
        self.measure_calls.append((result, tuple(draft_endpoints)))


def _key(key, modifiers=Qt.KeyboardModifier.NoModifier) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers)


# -- shortcuts ----------------------------------------------------------------

def test_daily_edit_verbs_get_real_qshortcuts() -> None:
    app = _app()
    registry = CommandRegistry()
    register_default_commands(registry)
    host = QWidget()
    binder = CommandShortcutBinder(
        host,
        registry,
        command_ids=CAD_SHORTCUT_COMMAND_IDS,
        shortcut_context=Qt.ShortcutContext.WidgetWithChildrenShortcut,
    )
    keyed = {
        command_id
        for command_id, _shortcut in binder._shortcuts
    }
    for command_id in (
        "room.edit.delete",
        "room.edit.toggle_hide",
        "room.edit.toggle_lock",
        "room.measure",
        "room.select.all",
        "room.select.invert",
    ):
        assert command_id in keyed, f"{command_id} has no live QShortcut"
    # Delete also answers Backspace (compact keyboards / laptop parity).
    delete_keys = {
        shortcut.key()
        for command_id, shortcut in binder._shortcuts
        if command_id == "room.edit.delete"
    }
    from PySide6.QtGui import QKeySequence

    assert delete_keys == {QKeySequence("Delete"), QKeySequence("Backspace")}


def test_shortcut_activation_noops_until_executor_bound() -> None:
    app = _app()
    registry = CommandRegistry()
    register_default_commands(registry)
    host = QWidget()
    binder = CommandShortcutBinder(host, registry, command_ids=CAD_SHORTCUT_COMMAND_IDS)
    fired: list[str] = []
    shortcut = next(s for cid, s in binder._shortcuts if cid == "room.edit.delete")
    # Unbound: the activation is a safe no-op (command.blocked.unavailable_in_context).
    shortcut.activated.emit()
    app.processEvents()
    assert fired == []
    registry.bind("room.edit.delete", execute=lambda: fired.append("delete"))
    shortcut.activated.emit()
    assert fired == ["delete"]


# -- selection verbs -----------------------------------------------------------

def test_select_all_skips_hidden_and_invert_is_relative(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    hidden_id = workspace.controller.document.entities[0].entity_id
    workspace.controller.set_entities_hidden((hidden_id,), True)

    workspace.select_all()
    selection = set(workspace.controller.view_state.selection)
    expected = {
        entity.entity_id
        for entity in workspace.controller.document.entities
        if entity.entity_id != hidden_id
    }
    assert selection == expected

    workspace.select_entity("speaker-fl")
    workspace.select_invert()
    inverted = set(workspace.controller.view_state.selection)
    assert inverted == expected - {"speaker-fl"}

    workspace.clear_selection()
    assert workspace.controller.view_state.selection == ()

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_escape_clears_selection_once_nothing_is_in_flight(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    workspace.select_entity("speaker-fl")
    assert workspace.controller.view_state.selection
    assert workspace.cancel_active_operation() is True
    assert workspace.controller.view_state.selection == ()
    # Second Esc with nothing left is a no-op.
    assert workspace.cancel_active_operation() is False

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


# -- delete in geometry edit mode ---------------------------------------------

def test_delete_targets_selected_vertex_while_geometry_editing(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    geometry.mode = "edit"
    geometry.select_vertex("front-left")

    before = workspace.controller.committed_document.room
    assert len(room_vertices(before)) == 4
    workspace.select_entity("speaker-fl")
    # Entity selection is cleared when edit mode starts; emulate reality.
    workspace.controller.view_state.set_selection(())

    assert workspace.delete_selection() is True
    after = workspace.controller.committed_document.room
    assert len(room_vertices(after)) == 3

    assert workspace.undo() is True
    assert len(room_vertices(workspace.controller.committed_document.room)) == 4

    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_delete_without_geometry_selection_is_noop(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    geometry.mode = "edit"
    before = workspace.controller.committed_document.room
    assert workspace.delete_selection() is False
    assert workspace.controller.committed_document.room == before

    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


# -- arrow-key nudge -----------------------------------------------------------

def test_entity_nudge_moves_selection_by_grid_step_as_one_undo(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    workspace.select_entity("speaker-fl")
    before = workspace.controller.committed_document.entity("speaker-fl").position
    transform = RoomEntityTransformController(workspace, workspace.viewport)
    workspace.attach_transform_input(transform)

    assert transform._key_press(_key(Qt.Key.Key_Right)) is True
    moved = workspace.controller.committed_document.entity("speaker-fl").position
    step = float(workspace.controller.view_state.grid_step_m)
    assert moved.x_m == pytest.approx(before.x_m + step)
    assert moved.y_m == pytest.approx(before.y_m)

    # Shift = x10 step, matching MetricSpinBox modifier semantics.
    assert transform._key_press(
        _key(Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    ) is True
    moved = workspace.controller.committed_document.entity("speaker-fl").position
    assert moved.y_m == pytest.approx(before.y_m + step * 10.0)

    # Each keypress is its own undo step through the working document.
    assert workspace.undo() is True
    assert workspace.controller.committed_document.entity("speaker-fl").position.y_m == pytest.approx(before.y_m)
    assert workspace.undo() is True
    assert workspace.controller.committed_document.entity("speaker-fl").position == before

    transform.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_entity_nudge_moves_whole_group_as_one_undo(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    workspace.controller.view_state.set_selection(("speaker-fl", "speaker-fr"))
    transform = RoomEntityTransformController(workspace, workspace.viewport)
    workspace.attach_transform_input(transform)

    fl_before = workspace.controller.committed_document.entity("speaker-fl").position
    fr_before = workspace.controller.committed_document.entity("speaker-fr").position
    step = float(workspace.controller.view_state.grid_step_m)

    assert transform._key_press(_key(Qt.Key.Key_Left)) is True
    fl = workspace.controller.committed_document.entity("speaker-fl").position
    fr = workspace.controller.committed_document.entity("speaker-fr").position
    assert fl.x_m == pytest.approx(fl_before.x_m - step)
    assert fr.x_m == pytest.approx(fr_before.x_m - step)

    assert workspace.undo() is True
    assert workspace.controller.committed_document.entity("speaker-fl").position == fl_before
    assert workspace.controller.committed_document.entity("speaker-fr").position == fr_before

    transform.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_nudge_is_inert_while_geometry_edit_owns_keys(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    geometry.mode = "edit"
    transform = RoomEntityTransformController(workspace, workspace.viewport)
    workspace.attach_transform_input(transform)

    before = workspace.controller.committed_document
    assert transform._key_press(_key(Qt.Key.Key_Right)) is False
    assert workspace.controller.committed_document == before

    transform.dispose()
    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_vertex_nudge_commits_through_undoable_room_authority(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    geometry.mode = "edit"
    geometry.select_vertex("front-left")
    before = workspace.controller.committed_document.room
    step = float(workspace.controller.view_state.grid_step_m)

    assert geometry._key_press(_key(Qt.Key.Key_Right)) is True
    after = workspace.controller.committed_document.room
    moved = next(v for v in room_vertices(after) if v.vertex_id == "front-left")
    assert moved.x_m == pytest.approx(step)
    assert moved.y_m == pytest.approx(0.0)

    assert workspace.undo() is True
    assert workspace.controller.committed_document.room == before

    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_wall_nudge_moves_selected_edge_as_undoable_topology(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    geometry.mode = "edit"
    geometry.select_edge(0)
    assert geometry.ensure_wall_topology() is True
    before = workspace.controller.committed_document.room
    step = float(workspace.controller.view_state.grid_step_m)

    # Edge 0 = front wall (front-left -> front-right); nudge it -Y (frontwards).
    assert geometry._key_press(_key(Qt.Key.Key_Down)) is True
    after = workspace.controller.committed_document.room
    assert after is not None
    front = {v.vertex_id: v for v in room_vertices(after)}
    assert front["front-left"].y_m == pytest.approx(-step)
    assert front["front-right"].y_m == pytest.approx(-step)
    # Rear wall untouched.
    assert front["rear-left"].y_m == pytest.approx(4.0)

    assert workspace.undo() is True
    assert workspace.controller.committed_document.room == before

    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


# -- zoom-to-selection ----------------------------------------------------------

def test_fit_selection_passes_full_selection_to_viewport(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    workspace.controller.view_state.set_selection(("speaker-fl", "speaker-fr"))
    workspace.fit_selection()
    assert workspace.viewport.focused_ids == [("speaker-fl", "speaker-fr")]

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_focus_entities_frames_entity_bounds() -> None:
    app = _app()
    viewport = RoomViewport3D()
    plotter = _FakePlotter()
    viewport.plotter = plotter
    viewport._document = make_f1_scene()

    viewport.focus_entities(("speaker-fl", "point-mlp"))
    assert plotter.reset_bounds, "focus_entities must frame bounds, not recenter"
    bounds = plotter.reset_bounds[-1]
    xmin, xmax, ymin, ymax, zmin, zmax = bounds
    # speaker-fl center render-space (x=1.35, y=-0.75, z=1.05), size 0.24/0.28/0.42
    # — each axis clamps to the 0.25 minimum pad since half-extents are < 0.25.
    assert xmin == pytest.approx(1.35 - 0.25)
    assert xmax == pytest.approx(3.25)  # MLP at x=3.0, sizeless pad 0.25
    assert zmax == pytest.approx(1.35)  # MLP z=1.1 + 0.25 pad > speaker z-extent
    assert ymin == pytest.approx(-3.25)  # MLP domain y=3.0 -> render y=-3.0 - pad

    viewport.deleteLater()
    app.processEvents()


# -- measure display policy -----------------------------------------------------

def test_measure_panel_reformats_result_and_copy_under_policy(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    controller = RoomMeasureController(workspace, workspace.viewport)
    panel = RoomMeasurePanel(controller)

    a = MeasureEndpoint(
        position=Position3(x_m=0.0, y_m=0.0, z_m=0.0),
        entity_id=None,
        entity_name=None,
        reference_kind="free_point",
        reference_label="自由点",
    )
    b = MeasureEndpoint(
        position=Position3(x_m=1.5, y_m=0.0, z_m=0.0),
        entity_id=None,
        entity_name=None,
        reference_kind="free_point",
        reference_label="自由点",
    )
    controller.result = build_distance_result(a, b)
    controller.measurementChanged.emit(controller.result)
    assert "距離 1.500 m" in panel.result_label.text()

    panel.set_length_policy(display_length_policy("cm"))
    assert "距離 150.00 cm" in panel.result_label.text()

    panel._copy()
    assert "150.00 cm" in QApplication.clipboard().text()

    panel.deleteLater()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


# -- underlay click fall-through --------------------------------------------------

def test_underlay_click_deselects_when_not_calibrating(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    workspace.select_entity("speaker-fl")
    workspace._underlay_clicked("u-1", 1.0, 2.0)
    assert workspace.controller.view_state.selection == ()

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_underlay_click_feeds_measure_free_point_when_not_calibrating(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    workspace.measure_controller.begin()
    workspace._underlay_clicked("u-1", 1.0, 2.0)
    assert len(workspace.measure_controller._endpoints) == 1
    point = workspace.measure_controller._endpoints[0]
    assert point.position.x_m == pytest.approx(1.0)
    assert point.position.y_m == pytest.approx(2.0)

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_workspace_render_feeds_positions_to_draft_measure_overlay(tmp_path) -> None:
    """Refresh during an in-progress measure must not crash the overlay.

    ``render_measure_overlay`` consumes ``Position3`` draft endpoints; the
    workspace render path used to forward ``MeasureEndpoint`` records, which
    ``domain_to_render`` cannot map — every refresh after the first pick
    raised AttributeError.
    """
    app, workspace = _workspace(tmp_path, viewport_cls=MeasureViewport)
    workspace.measure_controller.begin()
    workspace.measure_controller.pick_free_point(QPointF(3.0, 4.0))
    assert len(workspace.measure_controller._endpoints) == 1

    seen = [
        endpoint
        for _result, endpoints in workspace.viewport.measure_calls
        for endpoint in endpoints
    ]
    assert seen
    assert all(isinstance(endpoint, Position3) for endpoint in seen)

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


# --- REV34-DIALOGUX: first-run camera framing + sketch error guidance ----


class _BoundsStubPlotter:
    def __init__(self, bounds) -> None:
        self.bounds = bounds
        self.calls: list = []

    def reset_camera(self, render=True, bounds=None) -> None:
        self.calls.append(bounds)


def test_reset_camera_frames_scene_bounds_when_usable() -> None:
    plotter = _BoundsStubPlotter((0.0, 5.0, 0.0, 3.0, 0.0, 2.4))
    reset_camera_or_floor_default(plotter)
    assert plotter.calls == [None]


def test_reset_camera_covers_vtk_empty_scene_sentinel() -> None:
    """A renderer with no scene actors reports the exact ±1 sentinel —
    framing it left first-run sketching at sub-metre scale."""
    plotter = _BoundsStubPlotter((-1.0, 1.0, -1.0, 1.0, -1.0, 1.0))
    reset_camera_or_floor_default(plotter)
    assert plotter.calls == [DEFAULT_EMPTY_SCENE_BOUNDS]


def test_reset_camera_covers_degenerate_scene_extent() -> None:
    """Overlay-only scenes can report a ~1e-9 m extent."""
    plotter = _BoundsStubPlotter((0.0, 1e-9, 0.0, 1e-9, 0.0, 1e-9))
    reset_camera_or_floor_default(plotter)
    assert plotter.calls == [DEFAULT_EMPTY_SCENE_BOUNDS]


def test_view_top_on_empty_scene_frames_default_floor_patch(tmp_path) -> None:
    """Sketch entry on a brand-new project must land at metre scale."""
    app, workspace = _empty_workspace(tmp_path)
    controller = RoomGeometryInputController(workspace, workspace.viewport)
    controller.start_sketch()
    assert workspace.viewport.plotter.reset_bounds[-1] == DEFAULT_EMPTY_SCENE_BOUNDS

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_close_sketch_rejects_self_intersecting_outline(tmp_path) -> None:
    """A bowtie sketch must say *why* it failed, not a generic
    'データを処理できませんでした' — the domain validator's English text
    maps to the generic fallback, so the sketch layer names the two
    drawable failures explicitly."""
    app, workspace = _empty_workspace(tmp_path)
    controller = RoomGeometryInputController(workspace, workspace.viewport)
    controller.start_sketch()
    controller._sketch = [
        RoomVertex(vertex_id="v1", x_m=0.0, y_m=0.0),
        RoomVertex(vertex_id="v2", x_m=4.0, y_m=4.0),
        RoomVertex(vertex_id="v3", x_m=4.0, y_m=0.0),
        RoomVertex(vertex_id="v4", x_m=0.0, y_m=4.0),
    ]
    assert controller._close_sketch() is False
    assert "自己交差" in workspace.status.text()

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_close_sketch_rejects_stacked_vertices(tmp_path) -> None:
    app, workspace = _empty_workspace(tmp_path)
    controller = RoomGeometryInputController(workspace, workspace.viewport)
    controller.start_sketch()
    controller._sketch = [
        RoomVertex(vertex_id="v1", x_m=0.0, y_m=0.0),
        RoomVertex(vertex_id="v2", x_m=4.0, y_m=0.0),
        RoomVertex(vertex_id="v3", x_m=4.0, y_m=0.0),
        RoomVertex(vertex_id="v4", x_m=0.0, y_m=3.0),
    ]
    assert controller._close_sketch() is False
    assert "重なっています" in workspace.status.text()

    workspace.close()
    workspace.deleteLater()
    app.processEvents()
