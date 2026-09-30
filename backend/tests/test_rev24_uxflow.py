"""REV24-UXFLOW — workflow-friction regressions.

Persistent dirty indicators (the modified badge must never lie) and dialog
keyboard defaults (Enter picks a non-destructive choice).

The bundle-status, list-picker, and name-prompt regressions live in
test_workflow_application.py: WorkflowApplicationComposition cannot be
constructed in a pytest-xdist worker whose process has already imported
htdt.native_editor/htdt.room_workspace — PyVista/Qt interactor state then
crashes the worker (serially everything passes).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QMessageBox,
)

from htdt import dirty_state_dialog
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    SceneEntity,
    Size3,
    make_f1_scene,
)
from htdt.native_editor import NativeEditorWindow
from htdt.room_workspace import RoomWorkspace
from htdt.workspace_dirty_state import dirty_state_prompt


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


@pytest.fixture
def room_workspace(tmp_path: Path):
    workspace = _workspace(tmp_path)
    yield workspace
    workspace.close()
    workspace.deleteLater()
    _app().processEvents()


@pytest.fixture
def native_editor(tmp_path: Path):
    window = NativeEditorWindow(_repository(tmp_path), F1_DOCUMENT_ID)
    yield window
    window.close()
    window.deleteLater()
    _app().processEvents()


def _new_entity(entity_id: str, *, name: str = "追加") -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind="furniture",
        name=name,
        position=Position3(x_m=4.0, y_m=3.0, z_m=1.0),
        size_m=Size3(x_m=0.3, y_m=0.3, z_m=0.4),
    )


class _FakePlotter:
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
        pass


class _FakeViewport(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.interactor = self
        self.plotter = _FakePlotter()

    def render_document(self, document, **kwargs) -> None:
        pass

    def fit_scene(self) -> None:
        pass

    def focus_entity(self, entity_id: str) -> None:
        pass

    def focus_entities(self, entity_ids) -> None:
        pass

    def _last_display_position(self) -> QPointF:
        return QPointF(10.0, 10.0)

    def pick_world_position(self, _position):
        return (1.0, -2.0, 0.0)


def _workspace(tmp_path: Path) -> RoomWorkspace:
    _app()
    repository = _repository(tmp_path)
    return RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: _FakeViewport(parent),
    )


# ---------------------------------------------------------------------
# Dirty indicators must be persistent and truthful.


def test_room_workspace_dirty_badge_survives_notices(room_workspace) -> None:
    """The notice strip is reused for last-action text; before the dedicated
    badge existed, the first notice suppressed the dirty line forever and
    '保存しました' stayed up while edits were pending."""
    workspace = room_workspace
    assert workspace.dirty_status_label.text() == "保存済み"

    workspace._set_status("一覧を更新しました")
    workspace.controller.working.add_entity(_new_entity("speaker-extra"))
    workspace._refresh()

    assert workspace.controller.is_dirty
    assert "未保存の変更があります" in workspace.dirty_status_label.text()
    # The notice itself is untouched — the badge carries the state.
    assert workspace.status.text() == "一覧を更新しました"

    workspace.save()
    assert workspace.dirty_status_label.text() == "保存済み"
    assert "保存しました" in workspace.status.text()


def test_native_editor_dirty_badge_survives_transient_notices(
    native_editor,
) -> None:
    """'未保存'/'保存済み' lived in the transient status-bar message; any
    later showMessage clobbered it permanently. It must sit in the
    permanent zone so notices cannot hide it."""
    app = _app()
    window = native_editor
    window.show()
    app.processEvents()

    assert window._dirty_indicator.text() == "保存済み"

    window.working.add_entity(_new_entity("speaker-extra"))
    window._set_dirty_status()
    assert window._dirty_indicator.text() == "未保存"

    window.statusBar().showMessage("任意の通知")
    app.processEvents()
    assert window._dirty_indicator.text() == "未保存"

    window.save()
    app.processEvents()
    assert window._dirty_indicator.text() == "保存済み"


# ---------------------------------------------------------------------
# Dialog keyboard defaults.


def test_dirty_state_dialog_enter_defaults_to_non_destructive(
    tmp_path: Path, monkeypatch
) -> None:
    """Enter must never trigger the destructive choice."""
    _app()
    captured: dict[str, QMessageBox] = {}

    def fake_exec(self):
        captured["box"] = self
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    prompt = dirty_state_prompt("dirty_recoverable", "exit")
    assert prompt is not None
    dirty_state_dialog._choose(prompt, None)

    box = captured["box"]
    default = box.defaultButton()
    assert default is not None
    # The default must be the save choice, not 破棄.
    assert "保存" in default.text()
    assert "破棄" not in default.text()


def test_dirty_state_dialog_all_destructive_defaults_to_cancel(
    tmp_path: Path, monkeypatch
) -> None:
    """'busy' offers only stop_busy — Enter must land on キャンセル."""
    _app()
    captured: dict[str, QMessageBox] = {}

    def fake_exec(self):
        captured["box"] = self
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    prompt = dirty_state_prompt("busy", "navigate")
    assert prompt is not None
    dirty_state_dialog._choose(prompt, None)

    assert captured["box"].defaultButton().text() == "キャンセル"


def test_snapshot_action_dialog_enter_saves_first(
    tmp_path: Path, monkeypatch
) -> None:
    _app()
    captured: dict[str, QMessageBox] = {}

    def fake_exec(self):
        captured["box"] = self
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    dirty_state_dialog.choose_snapshot_action("エクスポート", None)

    assert captured["box"].defaultButton().text() == "保存してエクスポート"


def test_room_workspace_resolve_dirty_state_refreshes_badge(
    room_workspace,
) -> None:
    """Snapshot-dialog save used to keep showing 未保存: the mount resolved
    through controller.resolve_dirty_state, which mutates the working
    document without a workspace refresh."""
    workspace = room_workspace
    workspace.controller.working.add_entity(_new_entity("speaker-extra"))
    workspace._refresh()
    assert workspace.dirty_status_label.text() == "未保存の変更があります"

    resolved, _message = workspace.resolve_dirty_state("save")

    assert resolved
    assert not workspace.controller.is_dirty
    assert workspace.dirty_status_label.text() == "保存済み"


def test_room_workspace_resolve_dirty_state_failure_keeps_badge(
    room_workspace,
) -> None:
    """An unresolved action (no preview to commit) must not re-render or
    flip the badge."""
    workspace = room_workspace
    assert workspace.dirty_status_label.text() == "保存済み"

    resolved, _message = workspace.resolve_dirty_state("commit_preview")

    assert not resolved
    assert workspace.dirty_status_label.text() == "保存済み"


# (Composition-dependent regressions — bundle status lifecycle, list
# pickers, project-name prompts — live in test_workflow_application.py.)
