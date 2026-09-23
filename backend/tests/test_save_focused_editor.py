from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame

import htdt.workflow_application as workflow_application
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, is_unassigned_speaker_role, make_f1_scene, room_vertices
from htdt.command_registry import CommandRegistry, register_default_commands
from htdt.native_editor import NativeEditorWindow
from htdt.room_geometry_input import RoomGeometryInputController
from htdt.room_geometry_panel import RoomGeometryPanel
from htdt.room_workspace import RoomWorkspace


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _FakePlotter:
    def remove_actor(self, *_args, **_kwargs) -> None:
        pass

    def render(self) -> None:
        pass

    def add_mesh(self, *_args, **_kwargs):
        return None


class FakeRoomViewport(QFrame):
    """Viewport stand-in exposing the signals _make_room() wires up."""

    entitySelected = Signal(object)
    proposedEntitySelected = Signal(object)
    contextMenuRequested = Signal(object, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.interactor = self
        self.plotter = _FakePlotter()

    def render_document(
        self,
        document,
        *,
        selected_id,
        selected_ids=(),
        hidden_ids=frozenset(),
        locked_ids=frozenset(),
        overlays=None,
        reset_camera: bool = False,
    ) -> None:
        pass

    def render_proposed_entities(self, *_args, **_kwargs) -> None:
        pass

    def render_prediction_results(self, *_args, **_kwargs) -> None:
        pass

    def fit_scene(self) -> None:
        pass

    def focus_entity(self, entity_id) -> None:
        pass


def _room_composition(repository: SceneRepository, monkeypatch) -> object:
    """Bare WorkflowApplicationComposition whose Room workspace uses a fake viewport."""

    monkeypatch.setattr(workflow_application, "RoomViewport3D", FakeRoomViewport)
    monkeypatch.setattr(
        workflow_application,
        "RoomWorkspace",
        lambda repo, document_id: RoomWorkspace(
            repo,
            document_id,
            viewport_factory=lambda parent: FakeRoomViewport(parent),
        ),
    )
    # #627: the F1 fixture is explicit test content now, never auto-seeded.
    repository.save(make_f1_scene(), parent_revision_id=None)
    composition = object.__new__(workflow_application.WorkflowApplicationComposition)
    composition.repository = repository
    composition.document_id = F1_DOCUMENT_ID
    composition.registry = CommandRegistry()
    register_default_commands(composition.registry)
    return composition


def _room_workspace(repository: SceneRepository) -> RoomWorkspace:
    # #627: the F1 fixture is explicit test content now, never auto-seeded.
    repository.save(make_f1_scene(), parent_revision_id=None)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    workspace.resize(1200, 800)
    workspace.show()
    return workspace


def _pending_text(field, text: str) -> None:
    """Type ``text`` into a focused QDoubleSpinBox without leaving the field."""

    field.setFocus()
    field.lineEdit().setText(text)


def test_ctrl_s_commits_focused_inspector_position(tmp_path, monkeypatch) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    composition = _room_composition(repository, monkeypatch)
    mount = composition._make_room()
    assert mount.on_activate is not None
    mount.on_activate()
    workspace = mount.widget
    workspace.resize(1200, 800)
    workspace.show()
    app.processEvents()
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None

    workspace.select_entity("speaker-fl")
    field = workspace.inspector.position_fields["X"]
    _pending_text(field, "2.22")
    app.processEvents()

    # The pending edit has not reached the WorkingDocument yet, but the global
    # shortcut must remain usable while the field owns focus.
    assert not workspace.controller.is_dirty
    assert composition.registry.availability("project.save").enabled

    QTest.keyClick(field, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    app.processEvents()

    saved = repository.latest(F1_DOCUMENT_ID)
    assert saved.revision_id != original.revision_id
    assert saved.document.entity("speaker-fl").position.x_m == pytest.approx(2.22)
    assert not workspace.controller.is_dirty
    assert mount.on_close is not None
    mount.on_close()
    app.processEvents()


def test_save_commits_focused_inspector_name(tmp_path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    workspace = _room_workspace(repository)
    app.processEvents()
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None

    workspace.select_entity("speaker-fl")
    field = workspace.inspector.name_field
    field.setFocus()
    field.setText("FL renamed")
    app.processEvents()
    assert not workspace.controller.is_dirty

    assert workspace.save() is True
    saved = repository.latest(F1_DOCUMENT_ID)
    assert saved.revision_id != original.revision_id
    assert saved.document.entity("speaker-fl").name == "FL renamed"

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_save_commits_focused_geometry_height(tmp_path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    workspace = _room_workspace(repository)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)
    app.processEvents()
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None

    _pending_text(panel.height, "3.05")
    app.processEvents()
    assert not workspace.controller.is_dirty

    assert workspace.save() is True
    saved = repository.latest(F1_DOCUMENT_ID)
    assert saved.revision_id != original.revision_id
    assert saved.document.room is not None
    assert saved.document.room.height_m == pytest.approx(3.05)

    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_save_commits_focused_vertex_coordinate(tmp_path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    workspace = _room_workspace(repository)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)
    geometry.mode = "edit"
    geometry.select_vertex("front-left")
    panel.refresh()
    app.processEvents()
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None

    _pending_text(panel.vertex_x, "1.5")
    app.processEvents()
    assert not workspace.controller.is_dirty

    assert workspace.save() is True
    saved = repository.latest(F1_DOCUMENT_ID)
    assert saved.revision_id != original.revision_id
    vertices = {v.vertex_id: v for v in room_vertices(saved.document.room)}
    assert vertices["front-left"].x_m == pytest.approx(1.5)
    assert vertices["front-left"].y_m == pytest.approx(0.0)

    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_save_commits_cleared_speaker_role_as_unassigned(tmp_path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    workspace = _room_workspace(repository)
    app.processEvents()
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None

    workspace.select_entity("speaker-fl")
    field = workspace.inspector.role_field
    field.setFocus()
    field.lineEdit().setText("")
    app.processEvents()

    # Clearing the role is a valid explicit choice: the pending edit commits,
    # storing a unique unassigned placeholder instead of a fake channel role.
    assert workspace.save() is True
    saved = repository.latest(F1_DOCUMENT_ID)
    assert saved is not None
    assert saved.revision_id != original.revision_id
    stored = saved.document.entity("speaker-fl").speaker_role
    assert is_unassigned_speaker_role(stored)
    assert stored != "FL"
    assert field.currentText() == "未設定"

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_save_refuses_rejected_geometry_edit(tmp_path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    workspace = _room_workspace(repository)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    panel = RoomGeometryPanel(geometry)
    workspace.attach_geometry_panel(panel)
    geometry.mode = "edit"
    geometry.select_vertex("front-left")
    panel.refresh()
    app.processEvents()
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None

    # (6.0, 0.0) duplicates front-right — the polygon rebuild is rejected.
    _pending_text(panel.vertex_x, "6.0")
    app.processEvents()

    assert workspace.save() is False
    assert repository.latest(F1_DOCUMENT_ID).revision_id == original.revision_id
    assert not workspace.controller.is_dirty

    geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_save_without_pending_edit_reports_no_changes(tmp_path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    workspace = _room_workspace(repository)
    app.processEvents()
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None

    assert workspace.save() is False
    assert repository.latest(F1_DOCUMENT_ID).revision_id == original.revision_id
    assert not workspace.controller.is_dirty

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_pending_commit_creates_single_undo_entry(tmp_path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    workspace = _room_workspace(repository)
    app.processEvents()
    workspace.select_entity("speaker-fl")
    before = workspace.controller.working.history_length

    field = workspace.inspector.position_fields["Y"]
    _pending_text(field, "1.75")
    app.processEvents()
    assert workspace.save() is True

    assert workspace.controller.working.history_length == before + 1
    assert workspace.undo() is True
    assert repository.latest(F1_DOCUMENT_ID).document.entity("speaker-fl").position.y_m == pytest.approx(1.75)
    assert workspace.controller.document.entity("speaker-fl").position.y_m == pytest.approx(0.75)

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_native_editor_save_commits_focused_position_field(tmp_path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    window = NativeEditorWindow(repository, F1_DOCUMENT_ID)
    window.show()
    app.processEvents()
    original = repository.latest(F1_DOCUMENT_ID)
    assert original is not None
    assert not window.save_action.isEnabled()

    window.view_state.set_selection(("speaker-fl",), primary_id="speaker-fl")
    window.selected_id = "speaker-fl"
    window._inspect("speaker-fl")

    field = window.position_fields["X"]
    _pending_text(field, "2.34")
    app.processEvents()
    assert not window.working.is_dirty
    # Save (toolbar click and Ctrl+S share save_action) stays reachable while
    # the field holds the pending edit.
    assert window.save_action.isEnabled()

    window.save_action.trigger()
    app.processEvents()

    saved = repository.latest(F1_DOCUMENT_ID)
    assert saved.revision_id != original.revision_id
    assert saved.document.entity("speaker-fl").position.x_m == pytest.approx(2.34)
    assert not window.working.is_dirty
    # Focus is restored to the field so the user can keep editing after Save.
    assert app.focusWidget() is field

    window.close()
    window.deleteLater()
    app.processEvents()
