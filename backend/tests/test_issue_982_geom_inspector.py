"""Issue #982: selection-target inspector sections + pre-apply previews.

Coverage:
- target header （頂点/辺/壁/開口) + collapsible non-selected sections;
- dangerous ops arm an honest impact preview (affected openings named by
  ID), and only 適用 commits — one Undo step, no intermediate entries;
- 取り消し (preview cancel) ≠ 編集終了 (mode exit) ≠ Undo (history);
- stale SceneRevision rejection (revision-id + content-hash binding);
- inspector selector ↔ 3D selection share ``selected_opening_id``;
- field captions carry unit/range/dependency; invalid opening commits
  refuse before a preview is even armed;
- operation_events log feeds the #936 op-count / error-rate flow;
- Esc cancels an armed preview before edit-mode cancel (workspace chain);
- narrow width + 200% font readability.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from htdt.cad_scene import F1_DOCUMENT_ID, room_vertices, scene_content_hash
from htdt.cad_wall_models import WallConstraintBinding, WallOpening
from htdt.cad_walls import add_constraint_binding, add_opening, wall_length
from htdt.room_geometry_input import RoomGeometryInputController
from htdt.room_geometry_panel import RoomGeometryPanel

from test_room_cadux import (  # noqa: E402
    FakeRoomViewport,
    _app,
    _f1_repository,
)
from htdt.room_workspace import RoomWorkspace


def _workspace(tmp_path):
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    return app, workspace


def _geometry(workspace):
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    return geometry


def _panel(geometry, workspace):
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)
    return panel


def _teardown(app, workspace, geometry) -> None:
    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def _enter_edit(geometry) -> None:
    geometry.start_edit()
    assert geometry.mode == "edit"


def _topology_room(geometry):
    assert geometry.ensure_wall_topology() is True
    return geometry.topology


def _add_opening(geometry, wall, **overrides) -> WallOpening:
    room = geometry.room
    topology = geometry.topology
    opening = WallOpening(
        opening_id=overrides.pop("opening_id", "door-1"),
        wall_id=wall.wall_id,
        offset_m=overrides.pop("offset_m", 0.5),
        width_m=overrides.pop("width_m", 0.9),
        sill_m=overrides.pop("sill_m", 0.0),
        height_m=overrides.pop("height_m", 2.0),
        kind=overrides.pop("kind", "door"),
        is_open=overrides.pop("is_open", False),
    )
    candidate = add_opening(room, topology, opening)
    assert geometry.workspace.controller.replace_room_topology(room, candidate)
    return opening


def test_sections_follow_selection_target(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = _panel(geometry, workspace)
    _enter_edit(geometry)

    # Nothing selected: every target section collapsed.
    panel.refresh()
    for section in (
        panel.vertex_section,
        panel.edge_section,
        panel.wall_section,
        panel.opening_section,
    ):
        assert not section.header.isChecked()
        assert section.body.isHidden()
    assert panel.selection_title.text() == "選択: なし"

    geometry.select_vertex(room_vertices(geometry.room)[0].vertex_id)
    panel.refresh()
    assert panel.vertex_section.header.isChecked()
    assert not panel.wall_section.header.isChecked()
    assert panel.selection_title.text() == "選択: 頂点"
    assert "頂点" in panel.selection_context.text()
    assert "編集可" in panel.selection_context.text()

    _topology_room(geometry)
    geometry.select_edge(0)
    panel.refresh()
    assert panel.wall_section.header.isChecked()
    assert panel.edge_section.header.isChecked()
    assert not panel.vertex_section.header.isChecked()
    assert not panel.opening_section.header.isChecked()
    assert panel.selection_title.text() == "選択: 壁"
    context = panel.selection_context.text()
    assert "壁 1" in context and "長さ" in context and "開口 0 件" in context

    _teardown(app, workspace, geometry)


def test_opening_selection_targets_opening_section(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = _panel(geometry, workspace)
    _enter_edit(geometry)
    topology = _topology_room(geometry)
    opening = _add_opening(geometry, topology.walls[0])

    # Inspector→3D: picking the opening in the selector selects + highlights.
    geometry.select_edge(0)
    panel.refresh()
    index = panel.opening_selector.findData(opening.opening_id)
    assert index >= 0
    panel.opening_selector.setCurrentIndex(index)
    app.processEvents()
    assert geometry.selected_opening_id == opening.opening_id
    assert panel.selection_title.text() == "選択: 開口"
    assert panel.opening_section.header.isChecked()
    assert opening.opening_id in panel.selection_context.text()

    # 3D→inspector: select_opening picks the parent wall + opening.
    geometry.select_opening(None)
    geometry.select_edge(2)
    geometry.select_opening(opening.opening_id)
    assert geometry.selected_edge_index == 0
    panel.refresh()
    assert panel.opening_selector.currentData() == opening.opening_id

    _teardown(app, workspace, geometry)


def test_wall_delete_preview_lists_affected_openings_by_id(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = _panel(geometry, workspace)
    _enter_edit(geometry)
    topology = _topology_room(geometry)
    wall = topology.walls[0]
    opening = _add_opening(geometry, wall, opening_id="door-x")

    before = workspace.controller.committed_document
    geometry.select_edge(0)
    panel.refresh()
    panel.delete_wall_button.click()
    app.processEvents()

    preview = panel._pending_preview
    assert preview is not None and preview.kind == "wall_delete"
    assert not panel.preview_host.isHidden()
    text = panel.preview_lines.text()
    assert "door-x" in text  # affected opening named by ID
    assert "不明" in text  # solver readiness honestly marked unknown
    # Not feasible while an opening orphans — apply stays disabled.
    assert preview.feasible is False
    assert not panel.preview_apply_button.isEnabled()
    # Nothing committed: document untouched.
    assert workspace.controller.committed_document == before

    panel.preview_cancel_button.click()
    app.processEvents()
    assert panel._pending_preview is None
    assert panel.preview_host.isHidden()
    assert workspace.controller.committed_document == before

    _teardown(app, workspace, geometry)


def test_wall_delete_preview_apply_is_one_undo_step(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = _panel(geometry, workspace)
    _enter_edit(geometry)
    topology = _topology_room(geometry)
    assert len(topology.walls) >= 4

    before = workspace.controller.committed_document
    geometry.select_edge(0)
    panel.refresh()
    panel.delete_wall_button.click()
    app.processEvents()
    preview = panel._pending_preview
    assert preview is not None and preview.feasible
    assert panel.preview_apply_button.isEnabled()
    assert panel.preview_apply_button.text() == "この内容で適用"
    assert panel.preview_cancel_button.text() == "変更を取り消す"
    assert panel.finish_button.text() == "編集終了"

    panel.preview_apply_button.click()
    app.processEvents()
    committed = workspace.controller.committed_document
    assert len(committed.wall_topology.walls) == len(topology.walls) - 1
    assert panel._pending_preview is None
    # Selection cleared (target no longer exists) — no stale highlight.
    assert geometry.selected_edge_index is None

    # One undo restores the pre-delete document exactly.
    assert workspace.undo() is True
    assert workspace.controller.committed_document == before
    _teardown(app, workspace, geometry)


def test_stale_preview_rejected_on_apply(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = _panel(geometry, workspace)
    _enter_edit(geometry)
    _topology_room(geometry)
    geometry.select_edge(0)
    panel.refresh()
    panel.delete_wall_button.click()
    app.processEvents()
    preview = panel._pending_preview
    assert preview is not None and preview.feasible

    # Someone else commits an unrelated change — different committed doc.
    geometry.select_edge(1)
    assert geometry.set_selected_edge_length(
        wall_length(geometry.room, geometry.topology.walls[1]) - 0.2
    )
    app.processEvents()
    # The armed preview was dropped by refresh (base document moved).
    assert panel._pending_preview is None
    assert panel.preview_host.isHidden()
    assert "変更" in panel.notice.text()

    _teardown(app, workspace, geometry)


def test_opening_out_of_bounds_refuses_to_arm(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = _panel(geometry, workspace)
    _enter_edit(geometry)
    topology = _topology_room(geometry)
    opening = _add_opening(geometry, topology.walls[0])
    geometry.select_edge(0)
    panel.refresh()
    index = panel.opening_selector.findData(opening.opening_id)
    panel.opening_selector.setCurrentIndex(index)

    length = wall_length(geometry.room, topology.walls[0])
    panel.opening_width.set_value_m(0.5)
    panel.opening_offset.set_value_m(length - 0.1)  # offset+width > length
    panel.apply_opening_button.click()
    app.processEvents()

    assert panel._pending_preview is None
    assert "壁長" in panel.notice.text()
    assert ("commit_rejected", "opening_span") in panel.operation_events

    # Captions exposed the same dependent bound before the commit attempt.
    assert "有効" in panel.opening_offset_caption.text()
    _teardown(app, workspace, geometry)


def test_opening_apply_preview_shows_diff_and_applies(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = _panel(geometry, workspace)
    _enter_edit(geometry)
    topology = _topology_room(geometry)
    opening = _add_opening(geometry, topology.walls[0], offset_m=0.5)
    geometry.select_edge(0)
    panel.refresh()
    index = panel.opening_selector.findData(opening.opening_id)
    panel.opening_selector.setCurrentIndex(index)

    panel.opening_offset.set_value_m(0.9)
    panel.apply_opening_button.click()
    app.processEvents()

    preview = panel._pending_preview
    assert preview is not None and preview.kind == "opening_update"
    assert preview.feasible
    assert "開始位置" in panel.preview_lines.text()
    # Not committed yet.
    assert geometry.topology.openings[0].offset_m == pytest.approx(0.5)
    panel.preview_apply_button.click()
    app.processEvents()
    assert geometry.topology.openings[0].offset_m == pytest.approx(0.9)

    _teardown(app, workspace, geometry)


def test_escape_cancels_preview_then_edit_mode(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = _panel(geometry, workspace)
    _enter_edit(geometry)
    _topology_room(geometry)
    geometry.select_edge(0)
    panel.refresh()
    panel.delete_wall_button.click()
    app.processEvents()
    assert panel._pending_preview is not None

    # Workspace Esc chain drops the preview, not edit mode.
    assert workspace.cancel_active_operation() is True
    assert geometry.mode == "edit"
    assert panel._pending_preview is None
    # Second Esc exits edit mode.
    assert workspace.cancel_active_operation() is True
    assert geometry.mode == "idle"

    _teardown(app, workspace, geometry)


def test_vertex_delete_uses_preview_without_topology(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = _panel(geometry, workspace)
    _enter_edit(geometry)
    room = geometry.room
    vertex = room_vertices(room)[0]
    geometry.select_vertex(vertex.vertex_id)
    panel.refresh()

    before_walls = geometry.topology
    panel.delete_vertex_button.click()
    app.processEvents()
    preview = panel._pending_preview
    assert preview is not None and preview.kind == "vertex_delete"
    assert vertex.vertex_id in panel.preview_lines.text()
    panel.preview_apply_button.click()
    app.processEvents()
    assert len(room_vertices(geometry.room)) == len(room_vertices(room)) - 1

    _teardown(app, workspace, geometry)


def test_operation_events_measure_wall_select_to_undo_flow(tmp_path) -> None:
    """#936: 壁選択→開口編集→Undo op-count accounting."""
    app, workspace = _workspace(tmp_path)
    geometry = _geometry(workspace)
    panel = _panel(geometry, workspace)
    _enter_edit(geometry)
    topology = _topology_room(geometry)
    opening = _add_opening(geometry, topology.walls[0])

    geometry.select_edge(0)
    panel.refresh()
    index = panel.opening_selector.findData(opening.opening_id)
    panel.opening_selector.setCurrentIndex(index)
    panel.opening_width.set_value_m(0.7)
    panel.apply_opening_button.click()
    app.processEvents()
    panel.preview_apply_button.click()
    app.processEvents()
    assert workspace.undo() is True

    kinds = [kind for kind, _ in panel.operation_events]
    assert "preview_armed" in kinds and "preview_applied" in kinds
    _teardown(app, workspace, geometry)


def test_narrow_and_200pct_readability(tmp_path) -> None:
    app = _app()
    old_font = app.font()
    bigger = app.font()
    bigger.setPointSizeF(old_font.pointSizeF() * 2.0)
    app.setFont(bigger)
    try:
        app2, workspace = _workspace(tmp_path)
        geometry = _geometry(workspace)
        panel = _panel(geometry, workspace)
        _enter_edit(geometry)
        _topology_room(geometry)
        geometry.select_edge(0)
        panel.refresh()
        panel.resize(300, 900)  # narrow dock width
        app2.processEvents()

        assert panel.selection_context.wordWrap()
        assert panel.preview_lines.wordWrap()
        assert panel.preview_cancel_button.text()  # cancel stays labelled
        assert panel.wall_thickness_caption.wordWrap()
        panel.delete_wall_button.click()
        app2.processEvents()
        assert not panel.preview_host.isHidden()
        assert not panel.preview_cancel_button.isHidden()
        assert panel.preview_cancel_button.sizeHint().width() > 0
        assert panel.apply_opening_button.isEnabled() is False
    finally:
        app.setFont(old_font)
    _teardown(app2, workspace, geometry)
