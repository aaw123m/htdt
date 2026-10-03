"""REV32 room/CAD authoring understandability regression coverage.

Every user-facing element on the room surface must explain itself:
the context tab buttons carry hints, the tool strip renders a
per-context guidance line plus action tooltips, and the panels'
fields, headers, and empty states expose tooltips so no label is
left unexplained.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene  # noqa: E402
from htdt.room_constraints_panel import RoomConstraintsPanel  # noqa: E402
from htdt.room_geometry_input import RoomGeometryInputController  # noqa: E402
from htdt.room_geometry_panel import RoomGeometryPanel  # noqa: E402
from htdt.room_history_panel import RoomHistoryPanel  # noqa: E402
from htdt.room_objects_panel import RoomObjectsPanel  # noqa: E402
from htdt.room_video_panel import RoomVideoPanel  # noqa: E402
from htdt.room_workspace import (  # noqa: E402
    ContextToolStrip,
    ObjectPalette,
    ROOM_CONTEXT_IDS,
    RoomWorkspace,
    SelectionInspector,
)
from htdt.standards_workspace import StandardsCriterionPanel  # noqa: E402
from htdt.workflow_navigation import (  # noqa: E402
    CANONICAL_WORKSPACE_CONTEXTS,
    WorkspaceId,
)

from test_room_cadux import FakeRoomViewport, _f1_repository  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _workspace(tmp_path) -> tuple[QApplication, RoomWorkspace]:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    return app, workspace


def test_room_context_tabs_explain_themselves() -> None:
    contexts = CANONICAL_WORKSPACE_CONTEXTS[WorkspaceId.ROOM]
    assert {context.context_id for context in contexts} == set(ROOM_CONTEXT_IDS)
    for context in contexts:
        assert context.hint, f"{context.context_id} is missing a tab hint"


def test_tool_strip_guides_every_room_context(tmp_path) -> None:
    _app, workspace = _workspace(tmp_path)
    strip = workspace.tools

    assert set(strip.pages) == set(ROOM_CONTEXT_IDS)
    assert set(strip.GUIDANCE) == set(ROOM_CONTEXT_IDS)

    defined_actions = {
        tool_id
        for definitions in strip.DEFINITIONS.values()
        for tool_id, _label in definitions
    }
    assert defined_actions <= set(strip.ACTION_HINTS)
    for tool_id in defined_actions:
        assert strip.ACTION_HINTS[tool_id]

    for context_id, page in strip.pages.items():
        labels = page.findChildren(QLabel)
        assert any(label.text() == strip.GUIDANCE[context_id] for label in labels)
        buttons = page.findChildren(QPushButton)
        assert buttons, f"{context_id} has no actions"
        for button in buttons:
            assert button.toolTip(), f"{context_id}: button {button.text()!r} lacks a tooltip"


def test_object_palette_explains_each_kind(tmp_path) -> None:
    _app, workspace = _workspace(tmp_path)
    palette = workspace.object_palette

    assert {kind for kind, _label in palette.ITEMS} == set(palette.KIND_HINTS)
    buttons = palette.findChildren(QPushButton)
    assert len(buttons) == len(palette.ITEMS)
    for button in buttons:
        kind = button.property("objectKind")
        assert button.toolTip() == palette.KIND_HINTS[kind]


def test_geometry_panel_explains_fields_and_empty_sections(tmp_path) -> None:
    _app, workspace = _workspace(tmp_path)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)

    assert panel.wall_hint.toolTip() or panel.wall_hint.text()
    assert panel.opening_hint.toolTip() or panel.opening_hint.text()
    for widget in (
        panel.edit_button,
        panel.finish_button,
        panel.height,
        panel.vertex_x,
        panel.vertex_y,
        panel.edge_length,
        panel.delete_vertex_button,
        panel.insert_midpoint_button,
        panel.ensure_walls_button,
        panel.wall_thickness,
        panel.merge_wall_button,
        panel.delete_wall_button,
        panel.opening_kind,
        panel.add_opening_button,
        panel.apply_opening_button,
        panel.delete_opening_button,
    ):
        assert widget.toolTip(), f"{widget.objectName() or widget!r} lacks a tooltip"


def test_inspector_row_labels_carry_the_field_hint() -> None:
    _app()
    inspector = SelectionInspector()
    for text in ("種類", "名前", "位置", "寸法", "役割"):
        labels = [
            label
            for label in inspector.findChildren(QLabel)
            if label.text() == text
        ]
        assert labels, f"{text} row label missing"
        assert any(label.toolTip() for label in labels), (
            f"{text} row label lacks a tooltip"
        )


def test_objects_panel_explains_columns_and_actions() -> None:
    _app()
    panel = RoomObjectsPanel()
    header = panel.tree.headerItem()
    assert all(header.toolTip(index) for index in range(panel.tree.columnCount()))
    for button in (
        panel.hide_button,
        panel.show_button,
        panel.lock_button,
        panel.unlock_button,
        panel.delete_button,
    ):
        assert button.toolTip()


def test_measure_panel_explains_modes_and_references(tmp_path) -> None:
    _app, workspace = _workspace(tmp_path)
    panel = workspace.measure_panel

    assert panel.mode_combo.toolTip()
    assert panel.reference_combo.toolTip()
    for index in range(panel.mode_combo.count()):
        assert panel.mode_combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
    for index in range(panel.reference_combo.count()):
        assert panel.reference_combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
    for button in (panel.start_button, panel.copy_button, panel.cancel_button):
        assert button.toolTip()


def test_history_panel_explains_revision_workflow() -> None:
    _app()
    panel = RoomHistoryPanel()
    texts = [label.text() for label in panel.findChildren(QLabel)]
    assert any("リビジョン" in text for text in texts)
    header = panel.tree.headerItem()
    assert all(header.toolTip(index) for index in range(panel.tree.columnCount()))
    for widget in (
        panel.label_field,
        panel.note_field,
        panel.label_button,
        panel.preview_button,
        panel.diff_button,
        panel.restore_button,
    ):
        assert widget.toolTip()


def test_constraints_panel_explains_rules_and_results() -> None:
    _app()
    panel = RoomConstraintsPanel()
    results_header = panel.results_tree.headerItem()
    assert all(
        results_header.toolTip(index)
        for index in range(panel.results_tree.columnCount())
    )
    for widget in (
        panel.add_walkway_button,
        panel.add_allowed_button,
        panel.add_wall_button,
        panel.add_pair_button,
        panel.distance_field,
        panel.delete_button,
        panel.optimize_button,
    ):
        assert widget.toolTip()


def test_video_panel_explains_bindings_and_policy() -> None:
    _app()
    panel = RoomVideoPanel()
    for widget in (
        panel.target_combo,
        panel.projector_combo,
        panel.spec_combo,
        panel.new_spec_button,
        panel.display_combo,
        panel.display_spec_combo,
        panel.new_display_spec_button,
        panel.sightline_clearance,
        panel.max_axis_deviation,
        panel.evaluate_button,
        panel.variant_combo,
        panel.seat_view_combo,
        panel.view_seat_button,
        panel.restore_camera_button,
    ):
        assert widget.toolTip()


def test_standards_panel_explains_columns(tmp_path) -> None:
    _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    panel = StandardsCriterionPanel(repository, F1_DOCUMENT_ID)

    assert panel.profile_combo.toolTip()
    assert panel.target_combo.toolTip()
    assert panel.evaluate_button.toolTip()
    header = panel.tree.headerItem()
    assert all(header.toolTip(index) for index in range(panel.tree.columnCount()))
