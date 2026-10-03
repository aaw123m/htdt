"""REV35-GEOMPORT regression coverage: legacy wall/opening edit parity.

Ports from the legacy standalone editors (room_editor.py / wall_editor.py)
into the workflow path (room_geometry_input.py + room_geometry_panel.py):

- floor-plane picks honor the workspace grid-snap toggle and step
  (legacy ``RoomEditorWindow._screen_to_floor`` snapped; the workflow
  controller silently ignored it);
- a wall body is selectable along its whole segment, not only at the
  midpoint handle (legacy ``_hit_wall``); vertex grabs still win inside
  the vertex zone;
- edit mode renders opening outlines for every wall, not only the
  selected wall's (legacy ``_render_wall_overlay``);
- the wall inspector can author clearance ``WallConstraintBinding``s
  (legacy ``add_clearance_binding`` spin + button).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF

from htdt.cad_walls import (
    WallTopologyError,
    add_opening,
)
from htdt.cad_wall_models import WallOpening
from htdt.room_geometry_input import RoomGeometryInputController
from htdt.room_geometry_panel import RoomGeometryPanel
from htdt.ui_theme import DARK_THEME

from test_room_cadux import (  # noqa: E402
    FakeRoomViewport,
    _FakePlotter,
    _app,
    _workspace,
)


class _RecordingPlotter(_FakePlotter):
    """Plotter double that records add_mesh calls by actor name."""

    def __init__(self) -> None:
        super().__init__()
        self.meshes: dict[str, dict] = {}

    def add_mesh(self, *_args, **kwargs):
        name = kwargs.get("name")
        if name:
            self.meshes[name] = kwargs
        return None


class RecordingViewport(FakeRoomViewport):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.plotter = _RecordingPlotter()


def _geometry(workspace) -> RoomGeometryInputController:
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    return geometry


def _teardown(app, workspace, geometry) -> None:
    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


# -- grid snap on floor picks ---------------------------------------------------

def test_floor_pick_snap_rounds_to_grid_step(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    view_state = workspace.controller.view_state

    view_state.grid_snap_enabled = False
    assert geometry._snap_floor(1.12, 2.38) == (1.12, 2.38)

    view_state.grid_snap_enabled = True
    view_state.grid_step_m = 0.25
    assert geometry._snap_floor(1.12, 2.38) == (1.0, 2.5)

    view_state.grid_step_m = 0.0
    assert geometry._snap_floor(1.12, 2.38) == (1.12, 2.38)

    _teardown(app, workspace, geometry)


# -- whole-segment wall hit -----------------------------------------------------

def test_hit_handle_selects_wall_along_segment_not_only_midpoint(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    geometry.mode = "edit"
    # 1 domain metre -> 100 screen pixels; f1 room is 6x4 with edge 0 the
    # front-left -> front-right wall along y=0.
    geometry._project = lambda vertex: QPointF(vertex.x_m * 100.0, vertex.y_m * 100.0)

    # 150 px away from the edge midpoint: missed before this port, hits now.
    assert geometry._hit_handle(QPointF(450.0, 4.0)) == ("edge", 0)
    # The other walls are reachable the same way.
    assert geometry._hit_handle(QPointF(604.0, 200.0)) == ("edge", 1)

    _teardown(app, workspace, geometry)


def test_hit_handle_vertex_zone_still_wins_near_corner(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    geometry.mode = "edit"
    geometry._project = lambda vertex: QPointF(vertex.x_m * 100.0, vertex.y_m * 100.0)

    # 9px from front-left: inside the 12px vertex grab, so the segment
    # candidate is suppressed and the vertex resolves.
    assert geometry._hit_handle(QPointF(8.0, 5.0)) == ("vertex", 0)
    # Well off every wall: deselect.
    assert geometry._hit_handle(QPointF(300.0, 40.0)) is None

    _teardown(app, workspace, geometry)


# -- openings render for every wall ---------------------------------------------

def test_opening_markers_render_for_all_walls_in_edit_mode(tmp_path) -> None:
    app, workspace = _workspace(tmp_path, viewport_cls=RecordingViewport)
    geometry = _geometry(workspace)
    geometry.mode = "edit"
    assert geometry.ensure_wall_topology() is True

    room = geometry.room
    topology = geometry.topology
    assert room is not None and topology is not None
    first_wall, last_wall = topology.walls[0], topology.walls[-1]
    for wall in (first_wall, last_wall):
        opening = WallOpening(
            opening_id=f"opening-on-{wall.wall_id}",
            wall_id=wall.wall_id,
            offset_m=1.0,
            width_m=0.9,
            sill_m=0.0,
            height_m=2.0,
            kind="door",
        )
        topology = add_opening(room, topology, opening)
        assert workspace.controller.replace_room_topology(room, topology)

    geometry.select_edge(0)
    geometry._render_edit_handles()

    meshes = workspace.viewport.plotter.meshes
    selected_name = f"ux120-room-opening-opening-on-{first_wall.wall_id}"
    other_name = f"ux120-room-opening-opening-on-{last_wall.wall_id}"
    assert selected_name in meshes
    assert other_name in meshes
    assert (
        meshes[selected_name]["color"]
        == DARK_THEME.viewport.selection_outline.hex
    )
    assert (
        meshes[other_name]["color"] == DARK_THEME.viewport.geometry_edge.hex
    )

    _teardown(app, workspace, geometry)


def test_opening_markers_render_without_any_selection(tmp_path) -> None:
    app, workspace = _workspace(tmp_path, viewport_cls=RecordingViewport)
    geometry = _geometry(workspace)
    geometry.mode = "edit"
    assert geometry.ensure_wall_topology() is True

    room = geometry.room
    topology = geometry.topology
    wall = topology.walls[0]
    topology = add_opening(
        room,
        topology,
        WallOpening(
            opening_id="opening-solo",
            wall_id=wall.wall_id,
            offset_m=1.0,
            width_m=0.9,
            sill_m=0.0,
            height_m=2.0,
        ),
    )
    assert workspace.controller.replace_room_topology(room, topology)

    geometry._render_edit_handles()

    assert (
        "ux120-room-opening-opening-solo"
        in workspace.viewport.plotter.meshes
    )

    _teardown(app, workspace, geometry)


# -- clearance binding authoring --------------------------------------------------

def test_clearance_binding_add_from_panel_and_undo(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)
    geometry.mode = "edit"
    geometry.select_edge(0)
    assert geometry.ensure_wall_topology() is True
    panel.refresh()

    assert panel.wall_clearance_count.text() == "0 件"
    assert panel.add_clearance_button.isEnabled()

    panel.clearance_value.setValue(0.45)
    panel.add_clearance_button.click()
    app.processEvents()

    topology = workspace.controller.committed_document.wall_topology
    assert topology is not None
    assert len(topology.constraint_bindings) == 1
    binding = topology.constraint_bindings[0]
    assert binding.kind == "clearance"
    assert binding.clearance_m == pytest.approx(0.45)
    assert binding.wall_ids == (geometry.selected_wall.wall_id,)
    assert panel.wall_clearance_count.text() == "1 件"

    # Parity guard: a bound wall refuses deletion rather than orphaning the
    # reference — same fail-closed contract the legacy editor relies on.
    with pytest.raises(WallTopologyError):
        geometry.delete_selected_wall()

    assert workspace.undo() is True
    topology = workspace.controller.committed_document.wall_topology
    assert topology is not None
    assert topology.constraint_bindings == ()

    _teardown(app, workspace, geometry)


def test_clearance_widgets_follow_selection_state(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)
    geometry.mode = "edit"
    geometry.select_edge(0)
    assert geometry.ensure_wall_topology() is True
    panel.refresh()

    geometry.select_vertex("front-left")
    panel.refresh()
    assert panel.wall_host.isHidden()

    geometry.select_edge(1)
    panel.refresh()
    assert not panel.wall_host.isHidden()
    assert panel.add_clearance_button.isEnabled()

    _teardown(app, workspace, geometry)
