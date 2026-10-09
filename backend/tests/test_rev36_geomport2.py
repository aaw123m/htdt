"""REV36-GEOMPORT2 regression coverage: remaining wall/opening edit parity.

Ports the deferred items from docs/reviews/rev35-geomport.md:

- wrap-around merge across the boundary seam (last wall -> first wall);
- arbitrary-offset vertex insertion / wall split (was midpoint-only);
- ``WallConstraintBinding`` update/delete authority (previously add-only,
  so a mistaken binding permanently blocked ``delete_wall``);
- the panel's binding selector + 適用/削除 row and the dynamic
  「先頭の壁と結合」 label on the last wall.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    RoomVertex,
    SceneDocument,
    make_polygon_room,
    room_vertices,
)
from htdt.cad_wall_models import WallConstraintBinding, WallOpening
from htdt.cad_walls import (
    WallTopologyError,
    add_constraint_binding,
    add_opening,
    delete_constraint_binding,
    delete_wall,
    make_wall_topology,
    merge_walls,
    update_constraint_binding,
    wall_length,
)
from htdt.room_geometry_input import RoomGeometryInputController
from htdt.room_geometry_panel import RoomGeometryPanel
from htdt.room_workspace import RoomWorkspace

from test_room_cadux import (  # noqa: E402
    FakeRoomViewport,
    _app,
    _workspace,
)


def _geometry(workspace) -> RoomGeometryInputController:
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    return geometry


def _teardown(app, workspace, geometry) -> None:
    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def _seam_room() -> "object":
    """Five-vertex room whose polygon start vertex sits mid-edge, so the
    last wall (``e`` -> ``a``) and the first wall (``a`` -> ``b``) are
    collinear across the boundary seam."""
    return make_polygon_room(
        [
            RoomVertex(vertex_id="a", x_m=2.0, y_m=0.0),
            RoomVertex(vertex_id="b", x_m=4.0, y_m=0.0),
            RoomVertex(vertex_id="c", x_m=4.0, y_m=4.0),
            RoomVertex(vertex_id="d", x_m=0.0, y_m=4.0),
            RoomVertex(vertex_id="e", x_m=0.0, y_m=0.0),
        ],
        height_m=2.5,
    )


def _seam_workspace(tmp_path):
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(
        SceneDocument(
            document_id=F1_DOCUMENT_ID,
            schema_version=4,
            room=_seam_room(),
            entities=(),
        ),
        parent_revision_id=None,
    )
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    return app, workspace


# -- domain: wrap-around merge ---------------------------------------------------


def test_merge_walls_wrap_pair_across_seam() -> None:
    room = _seam_room()
    topology = make_wall_topology(room)
    first = topology.walls[-1]  # e -> a
    second = topology.walls[0]  # a -> b

    merged_room, merged_topology = merge_walls(
        room,
        topology,
        first.wall_id,
        second.wall_id,
        merged_wall_id="wall-merged-seam",
    )

    assert len(room_vertices(merged_room)) == 4
    assert len(merged_topology.walls) == 4
    merged = merged_topology.walls[-1]
    assert merged.wall_id == "wall-merged-seam"
    assert merged.from_vertex_id == "e"
    assert merged.to_vertex_id == "b"
    assert wall_length(merged_room, merged) == pytest.approx(4.0)


def test_merge_walls_wrap_pair_still_requires_collinearity() -> None:
    # F1 rectangle: last wall (rear-left -> front-left) and first wall
    # (front-left -> front-right) meet at a right angle.
    room = make_polygon_room(
        [
            RoomVertex(vertex_id="fl", x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id="fr", x_m=4.0, y_m=0.0),
            RoomVertex(vertex_id="rr", x_m=4.0, y_m=4.0),
            RoomVertex(vertex_id="rl", x_m=0.0, y_m=4.0),
        ],
        height_m=2.5,
    )
    topology = make_wall_topology(room)

    with pytest.raises(WallTopologyError, match="collinear"):
        merge_walls(
            room,
            topology,
            topology.walls[-1].wall_id,
            topology.walls[0].wall_id,
            merged_wall_id="wall-merged-x",
        )


def test_merge_walls_wrap_migrates_openings_and_bindings() -> None:
    room = _seam_room()
    topology = make_wall_topology(room)
    first = topology.walls[-1]
    second = topology.walls[0]
    topology = add_opening(
        room,
        topology,
        WallOpening(
            opening_id="opening-last",
            wall_id=first.wall_id,
            offset_m=0.4,
            width_m=0.8,
            sill_m=0.0,
            height_m=2.0,
        ),
    )
    topology = add_opening(
        room,
        topology,
        WallOpening(
            opening_id="opening-first",
            wall_id=second.wall_id,
            offset_m=0.5,
            width_m=0.8,
            sill_m=0.0,
            height_m=2.0,
        ),
    )
    topology = add_constraint_binding(
        room,
        topology,
        WallConstraintBinding(
            binding_id="clearance-seam",
            wall_ids=(first.wall_id, second.wall_id),
            clearance_m=0.35,
        ),
    )

    merged_room, merged_topology = merge_walls(
        room,
        topology,
        first.wall_id,
        second.wall_id,
        merged_wall_id="wall-merged-seam",
    )

    openings = {item.opening_id: item for item in merged_topology.openings}
    # First wall keeps its coordinate origin; second wall offsets shift by
    # the first wall's length (2.0 m).
    assert openings["opening-last"].offset_m == pytest.approx(0.4)
    assert openings["opening-first"].offset_m == pytest.approx(2.5)
    assert all(item.wall_id == "wall-merged-seam" for item in openings.values())
    binding = merged_topology.constraint_bindings[0]
    assert binding.wall_ids == ("wall-merged-seam",)


def test_merge_walls_refuses_triangle() -> None:
    room = make_polygon_room(
        [
            RoomVertex(vertex_id="a", x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id="b", x_m=4.0, y_m=0.0),
            RoomVertex(vertex_id="c", x_m=0.0, y_m=4.0),
        ],
        height_m=2.5,
    )
    topology = make_wall_topology(room)
    with pytest.raises(WallTopologyError, match="at least three walls"):
        merge_walls(
            room,
            topology,
            topology.walls[0].wall_id,
            topology.walls[1].wall_id,
            merged_wall_id="wall-merged-x",
        )


def test_merge_walls_wrap_button_path_via_controller(tmp_path) -> None:
    app, workspace = _seam_workspace(tmp_path)
    geometry = _geometry(workspace)
    geometry.mode = "edit"
    assert geometry.ensure_wall_topology() is True

    geometry.select_edge(4)  # last wall e -> a
    assert geometry.merge_selected_wall_with_next() is True

    topology = workspace.controller.committed_document.wall_topology
    assert topology is not None
    assert len(topology.walls) == 4
    merged = topology.walls[-1]
    assert merged.from_vertex_id == "e"
    assert merged.to_vertex_id == "b"
    # The merged wall stays selected at the new boundary index.
    assert geometry.selected_wall is not None
    assert geometry.selected_wall.wall_id == merged.wall_id

    _teardown(app, workspace, geometry)


# -- domain: constraint-binding update/delete -----------------------------------


def test_delete_constraint_binding_removes_only_target() -> None:
    room = _seam_room()
    topology = make_wall_topology(room)
    keep = WallConstraintBinding(
        binding_id="clearance-keep",
        wall_ids=(topology.walls[0].wall_id,),
        clearance_m=0.3,
    )
    drop = WallConstraintBinding(
        binding_id="clearance-drop",
        wall_ids=(topology.walls[1].wall_id,),
        clearance_m=0.6,
    )
    topology = add_constraint_binding(room, topology, keep)
    topology = add_constraint_binding(room, topology, drop)

    updated = delete_constraint_binding(room, topology, "clearance-drop")
    assert [item.binding_id for item in updated.constraint_bindings] == ["clearance-keep"]

    with pytest.raises(WallTopologyError, match="unknown constraint binding"):
        delete_constraint_binding(room, topology, "clearance-missing")


def test_delete_constraint_binding_unblocks_wall_delete() -> None:
    room = make_polygon_room(
        [
            RoomVertex(vertex_id="fl", x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id="mid", x_m=2.0, y_m=0.0),
            RoomVertex(vertex_id="fr", x_m=4.0, y_m=0.0),
            RoomVertex(vertex_id="rr", x_m=4.0, y_m=4.0),
            RoomVertex(vertex_id="rl", x_m=0.0, y_m=4.0),
        ],
        height_m=2.5,
    )
    topology = make_wall_topology(room)
    bound = topology.walls[0]  # fl -> mid bottom-left stub
    topology = add_constraint_binding(
        room,
        topology,
        WallConstraintBinding(
            binding_id="clearance-block",
            wall_ids=(bound.wall_id,),
            clearance_m=0.3,
        ),
    )
    with pytest.raises(WallTopologyError, match="orphan constraint bindings"):
        delete_wall(room, topology, bound.wall_id, replacement_wall_id="wall-r")

    freed = delete_constraint_binding(room, topology, "clearance-block")
    deleted_room, deleted_topology = delete_wall(
        room, freed, bound.wall_id, replacement_wall_id="wall-r"
    )
    assert len(room_vertices(deleted_room)) == 4
    assert len(deleted_topology.walls) == 4


def test_update_constraint_binding_replaces_clearance() -> None:
    room = _seam_room()
    topology = make_wall_topology(room)
    binding = WallConstraintBinding(
        binding_id="clearance-edit",
        wall_ids=(topology.walls[0].wall_id,),
        clearance_m=0.3,
    )
    topology = add_constraint_binding(room, topology, binding)

    updated = update_constraint_binding(
        room,
        topology,
        binding.model_copy(update={"clearance_m": 0.75}),
    )
    assert updated.constraint_bindings[0].clearance_m == pytest.approx(0.75)
    assert updated.constraint_bindings[0].wall_ids == binding.wall_ids

    with pytest.raises(WallTopologyError, match="unknown constraint binding"):
        update_constraint_binding(
            room,
            topology,
            binding.model_copy(update={"binding_id": "clearance-missing"}),
        )

    # WallTopology itself refuses dangling references (same shape as
    # update_opening) before domain validation runs.
    with pytest.raises(ValueError, match="unknown walls"):
        update_constraint_binding(
            room,
            topology,
            binding.model_copy(update={"wall_ids": ("wall:nope",)}),
        )


# -- controller: arbitrary-offset vertex insertion --------------------------------


def test_insert_selected_edge_vertex_at_offset_no_topology(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    geometry.mode = "edit"
    geometry.select_edge(0)  # front-left -> front-right, 6 m wall

    assert geometry.insert_selected_edge_vertex(1.5) is True
    room = workspace.controller.committed_document.room
    vertices = room_vertices(room)
    assert len(vertices) == 5
    inserted = vertices[1]
    assert inserted.x_m == pytest.approx(1.5)
    assert inserted.y_m == pytest.approx(0.0)
    assert geometry.selected_vertex_id == inserted.vertex_id

    _teardown(app, workspace, geometry)


def test_insert_selected_edge_vertex_rejects_out_of_range(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    geometry.mode = "edit"
    geometry.select_edge(0)

    with pytest.raises(ValueError):
        geometry.insert_selected_edge_vertex(0.0)
    with pytest.raises(ValueError):
        geometry.insert_selected_edge_vertex(6.0)
    with pytest.raises(ValueError):
        geometry.insert_selected_edge_vertex(-0.5)

    _teardown(app, workspace, geometry)


def test_insert_selected_edge_vertex_with_topology_keeps_openings(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    geometry.mode = "edit"
    geometry.select_edge(0)
    assert geometry.ensure_wall_topology() is True

    room = geometry.room
    topology = geometry.topology
    wall = geometry.selected_wall
    topology = add_opening(
        room,
        topology,
        WallOpening(
            opening_id="opening-right-of-split",
            wall_id=wall.wall_id,
            offset_m=4.0,
            width_m=0.9,
            sill_m=0.0,
            height_m=2.0,
        ),
    )
    assert workspace.controller.replace_room_topology(room, topology)

    geometry.select_edge(0)
    assert geometry.insert_selected_edge_vertex(1.0) is True

    topology = workspace.controller.committed_document.wall_topology
    assert topology is not None
    opening = next(
        item for item in topology.openings if item.opening_id == "opening-right-of-split"
    )
    # The split happened at 1.0 m, so the opening moves onto the second
    # child wall with its offset shifted left.
    assert opening.offset_m == pytest.approx(3.0)
    second_child = topology.walls[1]
    assert opening.wall_id == second_child.wall_id

    _teardown(app, workspace, geometry)


# -- panel: offset split + wrap merge + binding selector ---------------------------


def test_panel_offset_spin_tracks_edge_length(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)
    geometry.mode = "edit"
    geometry.select_edge(0)
    panel.refresh()

    # f1 front wall is 6 m: the spin clamps to (0.001, 5.999) and
    # defaults to the midpoint.
    assert panel.split_offset.maximum() == pytest.approx(5.999)
    assert panel.split_offset.value() == pytest.approx(3.0)

    panel.split_offset.setValue(2.0)
    panel.insert_at_offset_button.click()
    app.processEvents()

    room = workspace.controller.committed_document.room
    vertices = room_vertices(room)
    assert len(vertices) == 5
    assert vertices[1].x_m == pytest.approx(2.0)
    assert vertices[1].y_m == pytest.approx(0.0)

    _teardown(app, workspace, geometry)


def test_panel_merge_button_labels_wrap_pair(tmp_path) -> None:
    app, workspace = _seam_workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)
    geometry.mode = "edit"
    assert geometry.ensure_wall_topology() is True

    geometry.select_edge(0)
    panel.refresh()
    assert panel.merge_wall_button.text() == "次の壁と結合"

    geometry.select_edge(4)
    panel.refresh()
    assert panel.merge_wall_button.text() == "先頭の壁と結合"

    panel.merge_wall_button.click()
    app.processEvents()
    # Issue #982: the dangerous op arms a pre-apply preview; committing is
    # a separate explicit 適用 click.
    assert panel._pending_preview is not None
    assert "壁" in panel.preview_title.text()
    panel.preview_apply_button.click()
    app.processEvents()

    topology = workspace.controller.committed_document.wall_topology
    assert topology is not None
    assert len(topology.walls) == 4
    assert topology.walls[-1].from_vertex_id == "e"
    assert topology.walls[-1].to_vertex_id == "b"

    _teardown(app, workspace, geometry)


def test_panel_binding_selector_apply_and_delete(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)
    geometry.mode = "edit"
    geometry.select_edge(0)
    assert geometry.ensure_wall_topology() is True
    panel.refresh()

    panel.clearance_value.setValue(0.45)
    panel.add_clearance_button.click()
    app.processEvents()

    topology = workspace.controller.committed_document.wall_topology
    binding = topology.constraint_bindings[0]

    # Selector lists the binding and apply/delete start enabled on it.
    index = panel.binding_selector.findData(binding.binding_id)
    assert index > 0
    panel.binding_selector.setCurrentIndex(index)
    app.processEvents()
    assert panel.apply_clearance_button.isEnabled()
    assert panel.delete_clearance_button.isEnabled()
    assert panel.clearance_value.value() == pytest.approx(0.45)

    panel.clearance_value.setValue(0.8)
    panel.apply_clearance_button.click()
    app.processEvents()
    topology = workspace.controller.committed_document.wall_topology
    assert topology.constraint_bindings[0].clearance_m == pytest.approx(0.8)

    panel.delete_clearance_button.click()
    app.processEvents()
    topology = workspace.controller.committed_document.wall_topology
    assert topology.constraint_bindings == ()
    assert panel.wall_clearance_count.text() == "0 件"
    assert not panel.apply_clearance_button.isEnabled()

    # Deleting the binding restores wall deletion for the formerly bound wall.
    geometry.select_edge(0)
    assert geometry.delete_selected_wall() is True

    _teardown(app, workspace, geometry)
