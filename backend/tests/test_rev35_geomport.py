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
