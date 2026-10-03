"""REV30: direct tests for the product editor windows.

REV27's suite coverage report flagged five GUI editor classes with zero
direct test references — they were only exercised indirectly through the
workflow-application composition. These tests construct each window
offscreen against a seeded F1 document and assert each layer's distinctive
contract:

- RoomEditorWindow: sketch/edit mode lifecycle and vertex mutation guards
- WallEditorWindow: topology derivation, wall split/merge lifecycle
- CadEditorWindow: routing of vertex-count operations to wall-edit guidance
- TheaterEditorWindow: object palette creation/duplication persistence
- ConstraintEditorWindow: constraint-set authoring, evaluation, deletion
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import htdt.data_relocation  # noqa: F401  (must precede PySide6 import)

from PySide6.QtWidgets import QApplication

from htdt.cad_composition import CadEditorWindow
from htdt.cad_constraint_models import (
    CadAllowedRegionConstraint,
    CadExclusionRegionConstraint,
    CadPairDistanceConstraint,
    CadWallClearanceConstraint,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    RoomVertex,
    make_f1_scene,
    make_polygon_room,
    room_vertices,
)
from htdt.constraint_editor import ConstraintEditorWindow
from htdt.tree_item_role import ROLE
from htdt.room_editor import RoomEditorWindow
from htdt.theater_editor import TheaterEditorWindow
from htdt.wall_editor import WallEditorWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _repository(tmp_path):
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    # #627: the F1 fixture is explicit test content, never auto-seeded.
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def _window(repository, cls):
    _app()
    window = cls(repository, F1_DOCUMENT_ID)
    assert window.working is not None
    return window


# ---------------------------------------------------------------------------
# RoomEditorWindow
# ---------------------------------------------------------------------------


def test_room_editor_constructs_and_exposes_f1_room(tmp_path) -> None:
    window = _window(_repository(tmp_path), RoomEditorWindow)
    room = window._current_room()
    assert room is not None
    assert (room.width_m, room.depth_m) == pytest.approx((6.0, 4.0))
    assert len(window._room_vertices()) >= 3
    window.deleteLater()


def test_room_editor_sketch_cannot_close_below_three_vertices(tmp_path) -> None:
    window = _window(_repository(tmp_path), RoomEditorWindow)
    window.start_room_sketch()
    assert window.room_mode == 'sketch'
    assert window.room_sketch_vertices == []

    window.room_sketch_vertices = [(0.0, 0.0), (1.0, 0.0)]
    window.close_room_sketch()
    # Still sketching — the close guard rejected a degenerate polygon.
    assert window.room_mode == 'sketch'

    window.cancel_preview()
    window.deleteLater()


def test_room_editor_edit_lifecycle_and_vertex_mutation(tmp_path) -> None:
    window = _window(_repository(tmp_path), RoomEditorWindow)
    original = window._room_vertices()
    assert len(original) == 4  # the F1 fixture is a rectangular prism

    window.start_room_edit()
    assert window.room_mode == 'edit'

    window._insert_room_vertex(0)
    grown = window._room_vertices()
    assert len(grown) == 5
    # The inserted vertex is the midpoint of the first edge.
    start, end = original[0], original[1]
    assert grown[1].x_m == pytest.approx((start.x_m + end.x_m) / 2.0)
    assert grown[1].y_m == pytest.approx((start.y_m + end.y_m) / 2.0)
    assert window.selected_room_vertex_id == grown[1].vertex_id

    window.delete_room_vertex()
    assert len(window._room_vertices()) == 4
    assert window.selected_room_vertex_id is None

    window.finish_room_edit()
    assert window.room_mode == 'idle'
    window.deleteLater()


def _repository_with_room(tmp_path, room) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    scene = make_f1_scene().model_copy(update={"room": room})
    repository.save(scene, parent_revision_id=None)
    return repository


def test_room_height_focus_out_with_display_noise_does_not_commit(tmp_path) -> None:
    # A stored value with finer precision than the field's decimals used to
    # commit display-rounding noise as a silent "部屋形状を変更" entry on
    # focus-out (e.g. exiting 形状編集 while the field was focused).
    room = make_polygon_room(
        (
            RoomVertex(vertex_id="v1", x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id="v2", x_m=6.0, y_m=0.0),
            RoomVertex(vertex_id="v3", x_m=6.0, y_m=4.0),
            RoomVertex(vertex_id="v4", x_m=0.0, y_m=4.0),
        ),
        height_m=2.7000004,
    )
    window = _window(_repository_with_room(tmp_path, room), RoomEditorWindow)
    window.start_room_edit()
    assert not window.working.can_undo

    window._numeric_room_height_edited()
    assert not window.working.can_undo
    assert window._current_room().height_m == pytest.approx(2.7000004)

    # A user-meaningful change still commits and becomes undoable.
    window.room_height.setValue(3.0)
    window._numeric_room_height_edited()
    assert window.working.can_undo
    assert window._current_room().height_m == pytest.approx(3.0)
    window.deleteLater()


def test_room_vertex_focus_out_with_display_noise_does_not_commit(tmp_path) -> None:
    noisy_x = 1.23456789
    room = make_polygon_room(
        (
            RoomVertex(vertex_id="v1", x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id="v2", x_m=6.0, y_m=0.0),
            RoomVertex(vertex_id="v3", x_m=6.0, y_m=4.0),
            RoomVertex(vertex_id="v4", x_m=noisy_x, y_m=4.0),
        ),
        height_m=2.4,
    )
    window = _window(_repository_with_room(tmp_path, room), RoomEditorWindow)
    window.start_room_edit()
    window.selected_room_vertex_id = "v4"
    window._refresh_room_inspector()

    window._numeric_room_vertex_edited()
    assert not window.working.can_undo
    edited = next(v for v in room_vertices(window._current_room()) if v.vertex_id == "v4")
    assert edited.x_m == pytest.approx(noisy_x)

    window.room_vertex_x.setValue(2.0)
    window._numeric_room_vertex_edited()
    assert window.working.can_undo
    window.deleteLater()


# ---------------------------------------------------------------------------
# WallEditorWindow
# ---------------------------------------------------------------------------


def test_wall_editor_derives_topology_for_fixture_room(tmp_path) -> None:
    window = _window(_repository(tmp_path), WallEditorWindow)
    assert window._current_topology() is None

    assert window._ensure_wall_topology() is True
    topology = window._current_topology()
    assert topology is not None
    assert len(topology.walls) == 4
    window.deleteLater()


def test_wall_editor_point_segment_distance_is_exact() -> None:
    distance = WallEditorWindow._point_segment_distance(
        0.5, 0.5, 0.0, 0.0, 1.0, 0.0
    )
    assert distance == pytest.approx(0.5)
    # Beyond the segment end the distance clamps to the endpoint.
    distance = WallEditorWindow._point_segment_distance(
        2.0, 0.0, 0.0, 0.0, 1.0, 0.0
    )
    assert distance == pytest.approx(1.0)


def test_wall_editor_split_then_merge_restores_wall_count(tmp_path) -> None:
    window = _window(_repository(tmp_path), WallEditorWindow)
    window.start_wall_edit()
    assert window.wall_edit_active is True

    topology = window._current_topology()
    assert topology is not None and len(topology.walls) == 4
    window.selected_wall_id = topology.walls[0].wall_id

    window.split_selected_wall()
    split = window._current_topology()
    assert split is not None and len(split.walls) == 5
    # Selection follows the first child wall so further edits stay explicit.
    assert window.selected_wall_id in {wall.wall_id for wall in split.walls}
    assert window.selected_wall_id != topology.walls[0].wall_id

    window.merge_selected_wall_with_next()
    merged = window._current_topology()
    assert merged is not None and len(merged.walls) == 4

    window.finish_wall_edit()
    assert window.wall_edit_active is False
    window.deleteLater()


# ---------------------------------------------------------------------------
# CadEditorWindow
# ---------------------------------------------------------------------------


def test_cad_editor_blocks_vertex_count_ops_once_topology_exists(tmp_path) -> None:
    window = _window(_repository(tmp_path), CadEditorWindow)
    assert window._ensure_wall_topology() is True
    window._update_actions()

    # With a stable wall topology, vertex-count changes must go through wall
    # split/delete — the sketch and vertex actions route to guidance instead.
    assert window.draw_room_action.isEnabled() is False

    vertex_count = len(window._room_vertices())
    window.start_room_sketch()
    assert window.room_mode != 'sketch'

    window.start_room_edit()
    assert window.room_mode == 'edit'
    assert window.insert_vertex_action.isEnabled() is False
    assert window.delete_vertex_action.isEnabled() is False

    window._insert_room_vertex(0)
    assert len(window._room_vertices()) == vertex_count
    window.selected_room_vertex_id = window._room_vertices()[0].vertex_id
    window.delete_room_vertex()
    assert len(window._room_vertices()) == vertex_count
    window.deleteLater()


def test_cad_editor_allows_vertex_ops_without_topology(tmp_path) -> None:
    window = _window(_repository(tmp_path), CadEditorWindow)
    assert window._current_topology() is None

    window.start_room_edit()
    assert window.room_mode == 'edit'
    vertex_count = len(window._room_vertices())
    window._insert_room_vertex(0)
    assert len(window._room_vertices()) == vertex_count + 1
    window.finish_room_edit()
    window.deleteLater()


# ---------------------------------------------------------------------------
# TheaterEditorWindow
# ---------------------------------------------------------------------------


def test_theater_editor_make_object_builds_typed_entity(tmp_path) -> None:
    window = _window(_repository(tmp_path), TheaterEditorWindow)
    entity = window._make_object('seat')
    assert entity.entity_id.startswith('seat-')
    assert entity.kind == 'seat'
    assert entity.size_m is not None
    window.deleteLater()


def test_theater_editor_add_object_persists_entity(tmp_path) -> None:
    window = _window(_repository(tmp_path), TheaterEditorWindow)
    before = window.working.committed_document.entities
    window.add_object('seat')
    after = window.working.committed_document.entities
    assert len(after) == len(before) + 1
    added = next(e for e in after if e not in before)
    assert added.kind == 'seat'
    # The new object becomes the selection for immediate editing.
    assert window.selected_id == added.entity_id
    window.deleteLater()


def test_theater_editor_duplicate_preserves_source_entity(tmp_path) -> None:
    window = _window(_repository(tmp_path), TheaterEditorWindow)
    window._select('speaker-fl')
    assert window._object_edit_available() is True

    window.duplicate_selected_object()
    entities = window.working.committed_document.entities
    assert len(entities) == 6
    copy = window.working.committed_document.entity(window.selected_id)
    assert copy.entity_id != 'speaker-fl'
    assert copy.kind == 'speaker'
    assert copy.speaker_role == 'FL'
    source = window.working.committed_document.entity('speaker-fl')
    assert copy.position.x_m == pytest.approx(source.position.x_m + 0.25)
    assert copy.position.y_m == pytest.approx(source.position.y_m + 0.25)
    window.deleteLater()


def test_theater_editor_add_302_template_adds_speaker_set(tmp_path) -> None:
    window = _window(_repository(tmp_path), TheaterEditorWindow)
    expected = len(window._make_302_entities())
    before = len(window.working.committed_document.entities)
    window.add_302_template()
    entities = window.working.committed_document.entities
    assert len(entities) == before + expected
    window.deleteLater()


# ---------------------------------------------------------------------------
# ConstraintEditorWindow
# ---------------------------------------------------------------------------


def test_constraint_editor_starts_satisfied_with_no_constraints(tmp_path) -> None:
    window = _window(_repository(tmp_path), ConstraintEditorWindow)
    evaluation = window._active_constraint_evaluation()
    assert evaluation.constraints_satisfied is True
    assert evaluation.results == ()
    assert window._constraint_subject_id() is None
    window.deleteLater()


def test_constraint_editor_walkway_and_allowed_region_for_selected(tmp_path) -> None:
    window = _window(_repository(tmp_path), ConstraintEditorWindow)
    window._select('point-mlp')
    assert window._constraint_subject_id() == 'point-mlp'

    window.add_walkway_for_selected()
    walkway = window.constraint_set.constraints[-1]
    assert isinstance(walkway, CadExclusionRegionConstraint)
    assert walkway.region_role == 'walkway'
    assert walkway.entity_ids == ('point-mlp',)
    assert len(walkway.vertices) >= 3

    window.add_allowed_region_for_selected()
    allowed = window.constraint_set.constraints[-1]
    assert isinstance(allowed, CadAllowedRegionConstraint)
    assert allowed.entity_ids == ('point-mlp',)

    assert window._entity_constraint_ids('point-mlp') == (
        walkway.constraint_id,
        allowed.constraint_id,
    )
    window.deleteLater()


def test_constraint_editor_wall_clearance_targets_nearest_wall(tmp_path) -> None:
    window = _window(_repository(tmp_path), ConstraintEditorWindow)
    assert window._ensure_wall_topology() is True
    window._select('furniture-left')  # at (0.55, 2.2) — nearest wall is x=0

    window.add_wall_clearance_for_selected()
    clearance = window.constraint_set.constraints[-1]
    assert isinstance(clearance, CadWallClearanceConstraint)
    assert clearance.entity_ids == ('furniture-left',)
    topology = window._current_topology()
    wall_ids = {wall.wall_id for wall in topology.walls}
    assert clearance.wall_id in wall_ids
    # (0.55, 2.2) inside the 6×4 room is closest to the x=0 wall segment.
    start, end = window._wall_points(clearance.wall_id)
    assert start[0] == pytest.approx(0.0) and end[0] == pytest.approx(0.0)
    assert window._wall_constraint_ids(clearance.wall_id) == (clearance.constraint_id,)
    window.deleteLater()


def test_constraint_editor_pair_clearance_requires_two_selected(tmp_path) -> None:
    window = _window(_repository(tmp_path), ConstraintEditorWindow)
    # One entity selected: the action declines instead of guessing a pair.
    window._select('speaker-fl')
    before = len(window.constraint_set.constraints)
    window.add_pair_clearance_for_selection()
    assert len(window.constraint_set.constraints) == before

    window._set_selection(('speaker-fl', 'speaker-fr'), primary_id='speaker-fl')
    window.add_pair_clearance_for_selection()
    pair = window.constraint_set.constraints[-1]
    assert isinstance(pair, CadPairDistanceConstraint)
    assert pair.entity_a == 'speaker-fl'
    assert pair.entity_b == 'speaker-fr'
    window.deleteLater()


def test_constraint_editor_delete_selected_via_tree(tmp_path) -> None:
    window = _window(_repository(tmp_path), ConstraintEditorWindow)
    window._select('point-mlp')
    window.add_walkway_for_selected()
    assert len(window.constraint_set.constraints) == 1

    # _refresh_constraint_state populates one tree row per evaluation result.
    window._refresh_constraint_state()
    assert window.constraint_tree.topLevelItemCount() == 1
    window.constraint_tree.setCurrentItem(window.constraint_tree.topLevelItem(0))
    window.delete_selected_constraint()
    assert window.constraint_set.constraints == ()
    window.deleteLater()


def test_constraint_editor_distance_and_result_helpers(tmp_path) -> None:
    window = _window(_repository(tmp_path), ConstraintEditorWindow)
    distance, closest = ConstraintEditorWindow._distance_to_segment(
        (0.5, 0.5), (0.0, 0.0), (1.0, 0.0)
    )
    assert distance == pytest.approx(0.5)
    assert closest == pytest.approx((0.5, 0.0))
    assert window._find_result(None) is None
    assert window._find_result('missing-result-id') is None
    window.deleteLater()
