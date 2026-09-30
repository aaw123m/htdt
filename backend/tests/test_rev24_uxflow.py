"""REV24-UXFLOW — workflow-friction regressions.

Status-bar truth (busy lines must not outlive their job), persistent dirty
indicators (the modified badge must never lie), dialog keyboard defaults
(Enter picks a non-destructive choice; Enter/double-click accepts list
pickers), and re-prompting name validation (a blank name must not dead-end
the flow).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QInputDialog,
    QListWidget,
    QMessageBox,
)

import htdt.workflow_application as workflow_application
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
from htdt.native_worker import WORKER_CANCELLED
from htdt.room_workspace import RoomWorkspace
from htdt.workspace_dirty_state import dirty_state_prompt
from htdt.workflow_application import WorkflowApplicationComposition


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def _composition(tmp_path: Path) -> WorkflowApplicationComposition:
    repository = SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3")
    return WorkflowApplicationComposition(repository, "document-1")


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


def test_room_workspace_dirty_badge_survives_notices(tmp_path: Path) -> None:
    """The notice strip is reused for last-action text; before the dedicated
    badge existed, the first notice suppressed the dirty line forever and
    '保存しました' stayed up while edits were pending."""
    workspace = _workspace(tmp_path)
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
    tmp_path: Path,
) -> None:
    """'未保存'/'保存済み' lived in the transient status-bar message; any
    later showMessage clobbered it permanently. It must sit in the
    permanent zone so notices cannot hide it."""
    app = _app()
    repository = _repository(tmp_path)
    window = NativeEditorWindow(repository, F1_DOCUMENT_ID)
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

    window.close()
    window.deleteLater()
    app.processEvents()


# ---------------------------------------------------------------------
# Status-bar truth: a finished job must not keep its "running" line.


def test_bundle_completion_clears_busy_status(tmp_path: Path) -> None:
    composition = _composition(tmp_path)
    bar = composition.shell.statusBar()

    composition._begin_bundle_job("プロジェクトバンドルをエクスポートしています…")
    assert "エクスポートしています" in bar.currentMessage()

    composition._bundle_job_completed(
        "project.bundle.export", None, WORKER_CANCELLED
    )
    assert composition._bundle_status_message is None
    assert "エクスポートしています" not in bar.currentMessage()


def test_bundle_completion_keeps_fresher_status(
    tmp_path: Path, monkeypatch
) -> None:
    """A notice posted while the job ran must survive its cleanup."""
    monkeypatch.setattr(
        QMessageBox, "exec", lambda self: QDialog.DialogCode.Accepted
    )
    composition = _composition(tmp_path)
    bar = composition.shell.statusBar()

    composition._begin_bundle_job("プロジェクトバンドルをエクスポートしています…")
    bar.showMessage("別の通知")

    result = SimpleNamespace(row_count=3, asset_count=2, manifest_sha256="abc")
    composition._bundle_job_completed("project.bundle.export", result, None)

    assert bar.currentMessage() == "別の通知"
    assert composition._bundle_busy is False


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


# ---------------------------------------------------------------------
# List pickers accept on Enter / double-click.


def _exec_activating_first_row(self: QDialog) -> int:
    """Stand-in for QDialog.exec: fire the list's primary gesture."""
    listing = self.findChild(QListWidget)
    if listing is not None and listing.count():
        listing.setCurrentRow(0)
        listing.itemActivated.emit(listing.item(0))
    return int(self.result())


def test_pick_one_accepts_on_item_activation(
    tmp_path: Path, monkeypatch
) -> None:
    composition = _composition(tmp_path)
    monkeypatch.setattr(QDialog, "exec", _exec_activating_first_row)

    picked = composition._pick_one(
        "対象を選択", "対象:", (("Alpha", "id-a"), ("Beta", "id-b"))
    )
    assert picked == "id-a"


def test_choose_project_accepts_on_item_activation(
    tmp_path: Path, monkeypatch
) -> None:
    composition = _composition(tmp_path)
    monkeypatch.setattr(QDialog, "exec", _exec_activating_first_row)

    entries = (
        SimpleNamespace(project_id="p-1", display_name="リビング"),
        SimpleNamespace(project_id="p-2", display_name="シアター"),
    )
    picked = composition._choose_project(entries, "開く", "対象:")
    assert picked is entries[0]


# ---------------------------------------------------------------------
# Project-name prompts re-prompt instead of dead-ending on blank input.


def test_prompt_project_name_reprompts_until_named(
    tmp_path: Path, monkeypatch
) -> None:
    composition = _composition(tmp_path)
    answers = iter([("", True), ("   ", True), ("  リビング  ", True)])
    warnings: list[str] = []

    monkeypatch.setattr(
        QInputDialog, "getText", lambda *a, **k: next(answers)
    )
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: warnings.append(a[2])
    )

    assert composition._prompt_project_name("新規プロジェクト", "プロジェクト名:") == "リビング"
    assert warnings == ["プロジェクト名を入力してください"] * 2


def test_prompt_project_name_cancel_aborts(
    tmp_path: Path, monkeypatch
) -> None:
    composition = _composition(tmp_path)
    answers = iter([("", True), ("", False)])
    monkeypatch.setattr(
        QInputDialog, "getText", lambda *a, **k: next(answers)
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)

    assert (
        composition._prompt_project_name("新規プロジェクト", "プロジェクト名:")
        is None
    )
