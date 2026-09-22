from __future__ import annotations

from contextlib import closing
import logging
import os
import sqlite3

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QDockWidget, QFrame

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    RoomVertex,
    is_unassigned_speaker_role,
    make_f1_scene,
)
from htdt.cad_wall_models import WallOpening
from htdt.cad_walls import add_opening
from htdt.cad_input import CadAxis
from htdt.room_geometry_input import RoomGeometryInputController
from htdt.room_geometry_panel import RoomGeometryPanel
from htdt.room_transform_input import RoomEntityTransformController
from htdt.room_viewport import RoomOverlayState, _grid_mesh, _room_floor_mesh
from htdt.room_workspace import (
    RoomWorkspace,
    RoomWorkspaceController,
    build_room_workspace_mount,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _f1_repository(tmp_path) -> SceneRepository:
    """Repository where the F1 fixture content was persisted explicitly.

    Since #627 the F1 document id no longer auto-seeds synthetic content, so
    tests exercising the fixture scene save it themselves.
    """

    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


class FakeRoomViewport(QFrame):
    entitySelected = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.render_calls: list[tuple[str | None, RoomOverlayState, bool]] = []
        self.focused: list[str] = []
        self.fit_count = 0

    def render_document(
        self,
        document,
        *,
        selected_id: str | None,
        overlays: RoomOverlayState,
        reset_camera: bool = False,
    ) -> None:
        self.render_calls.append((selected_id, overlays, reset_camera))

    def fit_scene(self) -> None:
        self.fit_count += 1

    def focus_entity(self, entity_id: str) -> None:
        self.focused.append(entity_id)


class _FakePlotter:
    def remove_actor(self, *_args, **_kwargs) -> None:
        pass

    def render(self) -> None:
        pass

    def add_mesh(self, *_args, **_kwargs):
        return None


class GeometryFakeViewport(FakeRoomViewport):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.interactor = self
        self.plotter = _FakePlotter()


class _FakeRenderWindow:
    def GetSize(self):
        return (1200, 1000)


class _FakeCamera:
    def GetPosition(self):
        return (0.0, -10.0, 0.0)

    def GetFocalPoint(self):
        return (0.0, 0.0, 0.0)

    def GetViewUp(self):
        return (0.0, 0.0, 1.0)

    def GetParallelProjection(self):
        return True

    def GetParallelScale(self):
        return 5.0

    def GetViewAngle(self):
        return 30.0


class _TransformFakePlotter(_FakePlotter):
    def __init__(self) -> None:
        self.camera = _FakeCamera()
        self.render_window = _FakeRenderWindow()


class TransformFakeViewport(GeometryFakeViewport):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.plotter = _TransformFakePlotter()


def test_room_controller_reuses_repository_working_document_and_recovery(tmp_path) -> None:
    repository = _f1_repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None

    added = controller.add_object("speaker")

    assert controller.is_dirty
    assert controller.selected_id == added.entity_id
    assert repository.latest(F1_DOCUMENT_ID).revision_id == original.revision_id
    recovery = repository.recovery(F1_DOCUMENT_ID)
    assert recovery is not None
    assert recovery.document.entity(added.entity_id).name == "スピーカー"

    view_state = repository.view_state(F1_DOCUMENT_ID)
    assert view_state is not None
    assert view_state.selected_id == added.entity_id

    assert controller.save() is True
    saved = repository.latest(F1_DOCUMENT_ID)
    assert saved is not None
    assert saved.revision_id != original.revision_id
    # A new speaker starts unassigned: a reserved placeholder token, never a
    # plausible-looking fake channel role like 'SPK'.
    assert is_unassigned_speaker_role(
        saved.document.entity(added.entity_id).speaker_role
    )
    assert repository.recovery(F1_DOCUMENT_ID) is None
    assert not controller.is_dirty


def test_room_controller_assigns_distinct_unassigned_roles_to_new_speakers(tmp_path) -> None:
    repository = _f1_repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)

    first = controller.add_object("speaker")
    second = controller.add_object("speaker")

    assert first.speaker_role != second.speaker_role
    assert is_unassigned_speaker_role(first.speaker_role)
    assert is_unassigned_speaker_role(second.speaker_role)
    roles = [
        entity.speaker_role
        for entity in controller.document.entities
        if entity.kind == "speaker"
    ]
    assert len(roles) == len(set(roles))


def test_room_controller_role_edit_accepts_choice_and_returns_to_unassigned(tmp_path) -> None:
    repository = _f1_repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    added = controller.add_object("speaker")
    controller.set_selection(added.entity_id)

    assert controller.update_selected(
        name="Front",
        position=added.position,
        size_m=added.size_m,
        speaker_role="FL2",
    )
    assert controller.document.entity(added.entity_id).speaker_role == "FL2"

    # Clearing the role is an explicit unassign: a fresh reserved placeholder is
    # stored instead of an empty string or a literal 'SPK'.
    assert controller.update_selected(
        name="Front",
        position=added.position,
        size_m=added.size_m,
        speaker_role="",
    )
    cleared = controller.document.entity(added.entity_id).speaker_role
    assert is_unassigned_speaker_role(cleared)
    assert cleared == added.speaker_role

    # Typing a placeholder token is normalized to a canonical placeholder too
    # (a no-op here because the speaker is already unassigned).
    controller.update_selected(
        name="Front",
        position=added.position,
        size_m=added.size_m,
        speaker_role="SPK",
    )
    normalized = controller.document.entity(added.entity_id).speaker_role
    assert is_unassigned_speaker_role(normalized)
    assert normalized != "SPK"


def test_room_controller_opens_with_default_view_state_when_row_corrupt(tmp_path, caplog) -> None:
    repository = _f1_repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    controller.set_selection("speaker-fl")
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None
    assert repository.view_state(F1_DOCUMENT_ID) is not None

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            "UPDATE editor_view_states SET hidden_ids_json='{corrupt' WHERE document_id=?",
            (F1_DOCUMENT_ID,),
        )

    with caplog.at_level(logging.WARNING, logger="htdt.native"):
        reopened = RoomWorkspaceController(repository, F1_DOCUMENT_ID)

    # Only the disposable UI state resets; the document still opens.
    assert reopened.view_state.selection == ()
    assert reopened.selected_id is None
    assert reopened.view_state.hidden_ids == set()
    assert reopened.view_state.locked_ids == set()
    assert reopened.committed_document == original.document
    latest = repository.latest(F1_DOCUMENT_ID)
    assert latest is not None
    assert latest.revision_id == original.revision_id
    assert latest.content_hash == original.content_hash
    assert any("editor view state" in record.message for record in caplog.records)


def test_room_controller_sanitizes_stale_entity_ids_in_valid_view_state(tmp_path) -> None:
    repository = _f1_repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None
    repository.save_view_state(
        F1_DOCUMENT_ID,
        selected_id="ghost",
        selected_ids=("ghost", "speaker-fl"),
        hidden_ids={"ghost", "speaker-fr"},
        locked_ids={"ghost"},
    )

    reopened = RoomWorkspaceController(repository, F1_DOCUMENT_ID)

    # Well-formed rows keep flowing through EditorViewState.sanitize(): stale
    # ids drop out without discarding the rest of the persisted state.
    assert reopened.view_state.selection == ("speaker-fl",)
    assert reopened.selected_id == "speaker-fl"
    assert reopened.view_state.hidden_ids == {"speaker-fr"}
    assert reopened.view_state.locked_ids == set()


def test_room_controller_deactivation_fails_closed_during_preview(tmp_path) -> None:
    repository = _f1_repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    entity_id = controller.document.entities[0].entity_id

    controller.working.begin_move(entity_id)
    allowed, reason = controller.before_deactivate()

    assert allowed is False
    assert reason is not None
    assert "プレビュー" in reason

    controller.working.cancel_preview()
    assert controller.before_deactivate() == (True, None)


def test_room_controller_deactivation_blocks_dirty_and_recovery(tmp_path) -> None:
    repository = _f1_repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)

    controller.add_object("seat")
    allowed, reason = controller.before_deactivate()
    assert allowed is False
    assert reason is not None
    assert "未保存" in reason

    controller.save()
    assert controller.before_deactivate() == (True, None)

    controller.add_object("speaker")
    restored = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    assert restored.recovery_candidate is not None
    allowed, reason = restored.before_deactivate()
    assert allowed is False
    assert reason is not None
    assert "復旧" in reason


def test_room_workspace_is_component_composition_and_contextual(tmp_path) -> None:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    workspace.resize(1100, 700)
    workspace.show()
    app.processEvents()
    viewport = workspace.viewport

    assert workspace.findChildren(QDockWidget) == []
    assert workspace.current_context == "geometry"
    assert workspace.object_palette.isHidden()

    workspace.set_context("objects")
    assert not workspace.object_palette.isHidden()

    before = len(workspace.controller.document.entities)
    workspace.object_palette.addRequested.emit("seat")
    assert len(workspace.controller.document.entities) == before + 1
    selected_id = workspace.controller.selected_id
    assert selected_id is not None

    workspace.overlay_controls.focus.setChecked(True)
    assert viewport.render_calls[-1][1].focus_selection is True

    workspace.set_context("placement")
    workspace.tools.toolRequested.emit("focus-selection")
    assert viewport.focused[-1] == selected_id

    workspace.set_context("acoustics")
    assert workspace.overlay_controls.acoustics.isChecked()
    assert viewport.render_calls[-1][1].acoustics is True

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_room_workspace_mount_matches_ux110_shell_contract(tmp_path) -> None:
    app = _app()
    repository = _f1_repository(tmp_path)
    mount = build_room_workspace_mount(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    workspace = mount.widget
    assert isinstance(workspace, RoomWorkspace)

    assert mount.on_context_changed is not None
    mount.on_context_changed("placement")
    assert workspace.current_context == "placement"

    entity_id = workspace.controller.document.entities[0].entity_id
    assert mount.on_entity_requested is not None
    mount.on_entity_requested(entity_id)
    assert workspace.controller.selected_id == entity_id

    assert mount.before_deactivate is not None
    assert mount.before_deactivate() == (True, None)

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_room_geometry_sketch_commits_through_working_document_and_recovery(tmp_path) -> None:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: GeometryFakeViewport(parent),
    )
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None
    original_room = workspace.controller.document.room

    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    geometry.mode = "sketch"
    geometry._sketch = [
        RoomVertex(vertex_id="v1", x_m=0.0, y_m=0.0),
        RoomVertex(vertex_id="v2", x_m=5.0, y_m=0.0),
        RoomVertex(vertex_id="v3", x_m=5.0, y_m=3.0),
        RoomVertex(vertex_id="v4", x_m=0.0, y_m=3.0),
    ]

    assert geometry.commit() is True
    assert workspace.controller.is_dirty
    assert workspace.controller.document.room != original_room
    assert repository.latest(F1_DOCUMENT_ID).revision_id == original.revision_id
    recovery = repository.recovery(F1_DOCUMENT_ID)
    assert recovery is not None
    assert recovery.document.room == workspace.controller.document.room

    assert workspace.undo() is True
    assert workspace.controller.document.room == original_room

    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_room_move_preview_is_visible_and_commits_as_one_undoable_command(tmp_path) -> None:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: TransformFakeViewport(parent),
    )
    workspace.select_entity("speaker-fl")
    before = workspace.controller.committed_document.entity("speaker-fl").position

    transform = RoomEntityTransformController(workspace, workspace.viewport)
    workspace.attach_transform_input(transform)
    transform.arm_move()
    transform.set_axis(CadAxis.X)

    assert transform.begin_at(workspace.rect().center()) is True
    start = workspace.rect().center()
    target = type(start)(start.x() + 100, start.y())
    assert transform.drag_to(target) is True

    preview = workspace.controller.document.entity("speaker-fl").position
    committed_before_release = workspace.controller.committed_document.entity("speaker-fl").position
    assert preview.x_m == pytest.approx(before.x_m + 1.0)
    assert preview.y_m == before.y_m
    assert committed_before_release == before

    assert transform.finish_at(target) is True
    committed = workspace.controller.committed_document.entity("speaker-fl").position
    assert committed == preview
    assert repository.recovery(F1_DOCUMENT_ID) is not None

    assert workspace.undo() is True
    assert workspace.controller.committed_document.entity("speaker-fl").position == before

    transform.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_room_rotate_respects_axis_constraint_and_escape_cancels(tmp_path) -> None:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: TransformFakeViewport(parent),
    )
    workspace.select_entity("speaker-fl")
    before = workspace.controller.committed_document.entity("speaker-fl").orientation

    transform = RoomEntityTransformController(workspace, workspace.viewport)
    workspace.attach_transform_input(transform)
    transform.arm_rotate()
    transform.set_axis(CadAxis.Z)
    start = workspace.rect().center()
    target = type(start)(start.x() + 60, start.y())
    assert transform.begin_at(start) is True
    assert transform.drag_to(target) is True

    preview = workspace.controller.document.entity("speaker-fl").orientation
    assert preview != before
    assert transform.cancel() is True
    assert workspace.controller.committed_document.entity("speaker-fl").orientation == before
    assert not workspace.controller.working.has_preview

    transform.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()



def test_room_geometry_midpoint_split_preserves_opening_references_and_undo(tmp_path) -> None:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: GeometryFakeViewport(parent),
    )
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    geometry.mode = "edit"
    geometry.select_edge(0)

    assert geometry.ensure_wall_topology() is True
    room = workspace.controller.committed_document.room
    topology = workspace.controller.committed_document.wall_topology
    assert room is not None and topology is not None
    wall = geometry.selected_wall
    assert wall is not None

    with_opening = add_opening(
        room,
        topology,
        WallOpening(
            opening_id="window-before-split",
            wall_id=wall.wall_id,
            offset_m=0.5,
            width_m=1.0,
            sill_m=0.8,
            height_m=1.0,
            kind="window",
        ),
    )
    assert workspace.controller.replace_room_topology(room, with_opening)
    geometry.select_edge(0)

    assert geometry.insert_selected_edge_midpoint() is True
    split = workspace.controller.committed_document
    assert split.wall_topology is not None
    assert len(split.wall_topology.walls) == len(with_opening.walls) + 1
    migrated = next(
        item
        for item in split.wall_topology.openings
        if item.opening_id == "window-before-split"
    )
    assert migrated.wall_id != wall.wall_id
    assert migrated.offset_m == pytest.approx(0.5)

    assert workspace.undo() is True
    restored = workspace.controller.committed_document
    assert restored.room == room
    assert restored.wall_topology == with_opening

    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_room_numeric_edge_edit_fails_closed_when_opening_would_exceed_wall(tmp_path) -> None:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: GeometryFakeViewport(parent),
    )
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    geometry.mode = "edit"
    geometry.select_edge(0)
    assert geometry.ensure_wall_topology() is True

    room = workspace.controller.committed_document.room
    topology = workspace.controller.committed_document.wall_topology
    assert room is not None and topology is not None
    wall = geometry.selected_wall
    assert wall is not None
    topology = add_opening(
        room,
        topology,
        WallOpening(
            opening_id="door-near-end",
            wall_id=wall.wall_id,
            offset_m=4.7,
            width_m=1.0,
            height_m=2.0,
        ),
    )
    assert workspace.controller.replace_room_topology(room, topology)
    geometry.select_edge(0)
    before = workspace.controller.committed_document

    with pytest.raises(ValueError):
        geometry.set_selected_edge_length(5.0)

    assert workspace.controller.committed_document == before

    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_geometry_context_panel_mounts_and_adds_opening_through_wall_authority(tmp_path) -> None:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: GeometryFakeViewport(parent),
    )
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)

    geometry.mode = "edit"
    geometry.select_edge(0)
    workspace.set_context("geometry")
    panel.refresh()

    assert workspace.right_stack.currentWidget() is panel
    assert not panel.ensure_walls_button.isHidden()
    panel.ensure_walls_button.click()
    app.processEvents()

    assert workspace.controller.committed_document.wall_topology is not None
    panel.refresh()
    assert panel.add_opening_button.isEnabled()
    assert panel.opening_kind.isEnabled()

    window_index = panel.opening_kind.findData("window")
    panel.opening_kind.setCurrentIndex(window_index)
    panel.add_opening_button.click()
    app.processEvents()

    topology = workspace.controller.committed_document.wall_topology
    assert topology is not None
    assert len(topology.openings) == 1
    assert topology.openings[0].kind == "window"

    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()



def test_room_workspace_compact_layout_prioritizes_viewport_and_toggles_palette(tmp_path) -> None:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    workspace.resize(1100, 700)
    workspace.show()
    workspace.set_context("objects")
    app.processEvents()

    assert not workspace.object_palette.isHidden()
    assert workspace.right_stack.width() == 300

    workspace.resize(820, 600)
    app.processEvents()
    assert workspace._responsive_compact
    assert workspace.object_palette.isHidden()
    assert workspace.right_stack.width() == 280
    assert workspace.overlay_controls.is_compact
    assert workspace.overlay_controls.labels.isHidden()
    assert workspace.overlay_controls.focus.isHidden()
    assert not workspace.overlay_controls.more_button.isHidden()

    workspace.tools.toolRequested.emit("show-palette")
    app.processEvents()
    assert not workspace.object_palette.isHidden()

    workspace.resize(680, 520)
    app.processEvents()
    assert workspace.width() < 720
    assert workspace.object_palette.isHidden()
    assert workspace.right_stack.width() == 260

    workspace.set_context("geometry")
    app.processEvents()
    assert workspace.object_palette.isHidden()

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_room_inspector_role_picker_shows_unassigned_and_commits_choice(tmp_path) -> None:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    workspace.resize(1100, 700)
    workspace.show()
    app.processEvents()

    added = workspace.controller.add_object("speaker")
    workspace.select_entity(added.entity_id)
    app.processEvents()

    # A newly added speaker is visibly unassigned until the user picks a role.
    assert workspace.inspector.role_field.currentText() == "未設定"

    index = workspace.inspector.role_field.findText("SL")
    assert index >= 0
    workspace.inspector.role_field.setCurrentIndex(index)
    workspace.inspector.role_field.activated.emit(index)
    app.processEvents()
    assert (
        workspace.controller.document.entity(added.entity_id).speaker_role == "SL"
    )

    # Custom roles remain free-form text without touching the suggestion list.
    workspace.inspector.role_field.setEditText("WIDE")
    workspace.inspector.role_field.lineEdit().editingFinished.emit()
    app.processEvents()
    assert (
        workspace.controller.document.entity(added.entity_id).speaker_role
        == "WIDE"
    )

    # Choosing 未設定 again stores a fresh reserved placeholder.
    workspace.inspector.role_field.setCurrentIndex(0)
    workspace.inspector.role_field.activated.emit(0)
    app.processEvents()
    role = workspace.controller.document.entity(added.entity_id).speaker_role
    assert is_unassigned_speaker_role(role)
    assert workspace.inspector.role_field.currentText() == "未設定"

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_room_workspace_exposes_cad_navigation_hint_in_all_layouts(tmp_path) -> None:
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    workspace.resize(820, 600)
    workspace.show()
    app.processEvents()

    hint = workspace.overlay_controls.navigation_hint
    assert hint.isVisible()
    assert "中ボタン" in hint.text()
    assert "Shift+中ボタン" in hint.text()
    assert "ホイール" in hint.text()
    assert "右クリック" in hint.text()
    assert "視点回転" in hint.toolTip()

    workspace.close()
    workspace.deleteLater()
    app.processEvents()



def test_room_viewport_visual_foundation_has_floor_and_major_minor_grid() -> None:
    document = make_f1_scene()
    floor = _room_floor_mesh(document)
    minor = _grid_mesh(document, step_m=0.5)
    major = _grid_mesh(document, step_m=2.0, z_m=0.004)

    assert floor is not None
    assert floor.n_cells >= 2
    assert minor is not None and major is not None
    assert minor.n_lines > major.n_lines
    assert minor.bounds[4] == pytest.approx(0.003)
    assert major.bounds[4] == pytest.approx(0.004)
