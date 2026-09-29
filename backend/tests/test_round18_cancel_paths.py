"""REV18-CANCEL: cancel/abort/close-mid-operation unhappy paths.

Each test pins a verified gap (or a verified-good contract) in the
cancel dimension — see docs/reviews/round18-cancel-paths.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QLabel

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.data_management import (
    DataOperationKind,
    DataOperationPhase,
    DataOperationProgress,
)
from htdt.data_management_ui import build_data_management_component
from htdt.data_relocation import plan_data_relocation
from htdt.measurement_editor import MeasurementEditorWindow
from htdt.room_prediction import RoomPredictionController, RoomPredictionPanel
from htdt.room_workspace import RoomWorkspaceController
from htdt.workflow_application import WorkflowApplicationComposition
from htdt.workflow_navigation import WorkspaceId
from htdt.workflow_shell import (
    WorkflowShellWindow,
    WorkspaceMount,
    build_canonical_workspace_registrations,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


# --- F-A: run button must not re-enable while a prediction is running --------


def _prediction_panel(tmp_path: Path):
    repository = _repository(tmp_path)
    room = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    prediction = RoomPredictionController(repository, room)
    return repository, room, prediction, RoomPredictionPanel(prediction)


def test_run_button_stays_disabled_while_prediction_runs(tmp_path) -> None:
    app = _app()
    _repository, _room, prediction, panel = _prediction_panel(tmp_path)
    panel.show()
    app.processEvents()

    assert panel.run_button.isEnabled()

    # A live job: the controller stays busy until the worker finishes.
    prediction._current_job_id = "inflight"
    panel._option_changed()
    assert prediction.is_busy
    assert not panel.run_button.isEnabled()

    prediction._current_job_id = None
    panel._option_changed()
    assert panel.run_button.isEnabled()

    panel.close()
    panel.deleteLater()
    prediction.dispose()
    app.processEvents()


# --- F-B: cancel_rew_read cancels the worker and reports "cancelled" ---------


def _measurement_window(tmp_path: Path) -> MeasurementEditorWindow:
    return MeasurementEditorWindow(_repository(tmp_path), F1_DOCUMENT_ID)


def _submit_fake_rew_read(
    window: MeasurementEditorWindow,
    monkeypatch,
    pool_cancels: list[str],
) -> str:
    """Submit a REW read token with no real worker; return its job_id."""

    window.selected_id = "point-mlp"
    window.rew_combo.addItem("REW A", "rew-uuid-1")
    # Never start — the read stays "in flight" for the unhappy path.
    monkeypatch.setattr(window, "_start_rew_task", lambda *a, **k: None)
    window.read_selected_rew_async()

    job_id = next(iter(window._rew_tokens.keys()))
    original_cancel = window._rew_pool.cancel

    def recorded_cancel(key: str) -> bool:
        pool_cancels.append(key)
        return original_cancel(key)

    monkeypatch.setattr(window._rew_pool, "cancel", recorded_cancel)
    return job_id


def test_cancel_rew_read_cancels_worker_and_labels_cancel(tmp_path, monkeypatch) -> None:
    app = _app()
    window = _measurement_window(tmp_path)
    window.show()
    app.processEvents()
    pool_cancels: list[str] = []

    job_id = _submit_fake_rew_read(window, monkeypatch, pool_cancels)
    token = window._rew_tokens[job_id]

    window.cancel_rew_read()

    # The pool cancel reaches the worker record so a queued read never
    # starts and a finishing read reports WORKER_CANCELLED.
    assert pool_cancels == [job_id]
    assert window.rew_job_guard.is_cancelled(token)
    assert "待機をやめました" in window.statusBar().currentMessage()

    # The late successful result is discarded as a CANCEL — not blamed on
    # a revision/constraint change that never happened.
    window._rew_task_completed(job_id, object(), None)
    message = window.statusBar().currentMessage()
    assert "キャンセル" in message
    assert "制約が変更" not in message
    assert "revision/document" not in message
    # Nothing applied.
    assert window.measurement_repository.list_measurements(F1_DOCUMENT_ID) == ()

    window.close()
    window.deleteLater()
    app.processEvents()


def test_cancelled_rew_worker_error_is_silent(tmp_path, monkeypatch) -> None:
    """A WORKER_CANCELLED completion must not surface a failure message."""
    app = _app()
    window = _measurement_window(tmp_path)
    window.show()
    app.processEvents()
    pool_cancels: list[str] = []

    job_id = _submit_fake_rew_read(window, monkeypatch, pool_cancels)
    window.cancel_rew_read()
    window.statusBar().clearMessage()

    window._rew_task_completed(job_id, None, "cancelled")
    assert window.statusBar().currentMessage() == ""

    window.close()
    window.deleteLater()
    app.processEvents()


# --- F-C: project-switch close uses the project_switch context ----------------


def test_project_switch_close_reports_switch_context(tmp_path, monkeypatch) -> None:
    app = _app()
    composition = WorkflowApplicationComposition(
        _repository(tmp_path), "document-1"
    )
    contexts: list[str] = []
    original_resolve = composition.shell.router.resolve_dispose_all

    def recorded_resolve(context):
        contexts.append(context)
        return original_resolve(context)

    monkeypatch.setattr(
        composition.shell.router, "resolve_dispose_all", recorded_resolve
    )
    monkeypatch.setattr(
        composition.project_library,
        "open_project",
        lambda project_id: SimpleNamespace(
            document_id="doc-2", project_id=project_id
        ),
    )
    composition._open_project_callback = lambda document_id: None

    entry = SimpleNamespace(document_id="doc-2", project_id="proj-2")
    composition._switch_to_project(entry)
    app.processEvents()

    assert "project_switch" in contexts
    # The switch restores the default so a later app exit is labelled exit.
    assert composition.shell._deactivation_context == "exit"

    composition.shell.deleteLater()
    app.processEvents()


def test_shell_close_uses_deactivation_context_for_prompts(
    tmp_path, monkeypatch
) -> None:
    """closeEvent forwards _deactivation_context into dirty resolution."""
    app = _app()
    state = {"dirty": True}
    prompt_contexts: list[str] = []

    def factory(workspace_id: WorkspaceId):
        def build() -> WorkspaceMount:
            if workspace_id is not WorkspaceId.ROOM:
                return WorkspaceMount.from_widget(QLabel(workspace_id.value))
            return WorkspaceMount.from_widget(
                QLabel(workspace_id.value),
                before_deactivate=lambda: (
                    (False, "未保存の変更があります")
                    if state["dirty"]
                    else (True, None)
                ),
                dirty_state=lambda: (
                    "dirty_recoverable" if state["dirty"] else "clean"
                ),
                resolve_dirty_state=lambda action: (
                    (state.update(dirty=False) or (True, "破棄しました"))
                    if action == "discard"
                    else (False, None)
                ),
            )

        return build

    import htdt.dirty_state_dialog as dialog

    def auto_discard(mount, context, parent):
        prompt_contexts.append(context)
        mount.resolve_dirty_state("discard")
        return True

    monkeypatch.setattr(dialog, "resolve_mount_dirty_state", auto_discard)

    window = WorkflowShellWindow(
        build_canonical_workspace_registrations(
            {workspace_id: factory(workspace_id) for workspace_id in WorkspaceId}
        )
    )
    assert window.navigate(WorkspaceId.ROOM)

    window._deactivation_context = "project_switch"
    window.close()

    assert prompt_contexts == ["project_switch"]
    assert state["dirty"] is False

    window.deleteLater()
    app.processEvents()


# --- F-D: dead can_cancel field removed from DataOperationProgress -----------


def test_data_operation_progress_has_no_dead_cancel_field() -> None:
    progress = DataOperationProgress(
        operation_id="op",
        kind=DataOperationKind.CREATE_BACKUP,
        phase=DataOperationPhase.BACKING_UP,
        message_ja="処理中",
    )
    assert not hasattr(progress, "can_cancel")


# --- F-F: a widget mounted mid-operation shows the live progress card --------


class _BusyFakeController(QObject):
    """Minimal data-management controller, constructed already busy."""

    busy_changed = Signal(bool)
    progress_changed = Signal(object)
    backup_created = Signal(object)
    restore_preview_ready = Signal(object)
    restore_completed = Signal(object)
    relocation_completed = Signal(object)
    storage_scan_completed = Signal(object)
    storage_gc_completed = Signal(object)
    operation_failed = Signal(object)

    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self.backend = SimpleNamespace(
            data_dir=data_dir,
            current_native_schema_version=lambda: 5,
            plan_relocation=lambda destination: plan_data_relocation(
                data_dir, Path(destination)
            ),
        )
        self.lifecycle = SimpleNamespace(restart_required=False)
        self._busy = True

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def can_close_application(self) -> bool:
        return False

    def create_backup(self, destination, *, allow_stale=False):
        return "op"

    def preview_restore(self, backup_path):
        return "op"

    def restore(self, preview):
        return "op"

    def relocate(self, destination):
        return "op"

    def scan_storage(self):
        return "op"

    def gc_storage(self):
        return "op"

    def revalidate(self):
        return SimpleNamespace(summary_ja=lambda: "stub")


def test_remount_while_busy_shows_progress_card(tmp_path) -> None:
    app = _app()
    controller = _BusyFakeController(tmp_path / "data")

    component = build_data_management_component(controller)
    widget = component.widget
    widget.show()
    app.processEvents()

    assert not widget.progress_card.isHidden()
    assert not widget.backup_button.isEnabled()

    widget.close()
    widget.deleteLater()
    app.processEvents()
