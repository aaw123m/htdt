from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from htdt.activity_center import (
    ACTIVITY_HISTORY_FILENAME,
    ActivityCenter,
    NavigationPolicy,
    OperationClass,
    OperationState,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import MeasurementWorkflowController
from htdt.workflow_application import WorkflowApplicationComposition


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _pump_until(predicate, timeout_s: float = 5.0) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _measurement_workspace(tmp_path: Path) -> MeasurementPageWorkspace:
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    controller = MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=CadMeasurementRepository(scene_repository),
    )
    return MeasurementPageWorkspace(controller)


def test_superseded_job_completion_is_discarded(tmp_path: Path) -> None:
    """A second request on the same purpose wins; the late first result drops.

    Before the guard both completions applied in finish order — whichever
    finished last overwrote the other, so a slow first read could stage data
    behind a newer selection.
    """
    app = _app()
    workspace = _measurement_workspace(tmp_path)

    applied: list[object] = []
    first_started = Event()
    release_first = Event()

    def first_read() -> str:
        first_started.set()
        release_first.wait(5.0)
        return "first-result"

    workspace._start_job(
        first_read, applied.append, "job", purpose="rew_read"
    )
    assert _pump_until(lambda: first_started.is_set())
    workspace._start_job(
        lambda: "second-result", applied.append, "job", purpose="rew_read"
    )
    release_first.set()

    assert _pump_until(lambda: workspace._job_pool.active_count == 0)
    app.processEvents()
    assert applied == ["second-result"]

    workspace._job_pool.shutdown()
    workspace.deleteLater()


def test_unrelated_purpose_jobs_both_apply(tmp_path: Path) -> None:
    """Purposes are independent — a list refresh must not kill a read."""
    app = _app()
    workspace = _measurement_workspace(tmp_path)

    applied: list[object] = []
    workspace._start_job(
        lambda: "list-result", applied.append, "job", purpose="rew_list"
    )
    workspace._start_job(
        lambda: "read-result", applied.append, "job", purpose="rew_read"
    )

    assert _pump_until(lambda: workspace._job_pool.active_count == 0)
    app.processEvents()
    assert sorted(applied) == ["list-result", "read-result"]

    workspace._job_pool.shutdown()
    workspace.deleteLater()


def test_exit_accounting_persists_active_operations(tmp_path: Path) -> None:
    """Ops still RUNNING at close land in ``active_operations`` of history.

    The automatic-backup worker is not close-guarded: ``shutdown()`` cancels
    it and the cancelled completion is deliberately never delivered, so the
    registered op would otherwise stay RUNNING in memory and vanish without
    a trace for recovery diagnostics.
    """
    _app()
    center = ActivityCenter()
    op_id = center.submit(
        operation_kind="automatic_backup",
        operation_class=OperationClass.DATA_MANAGEMENT,
        title="自動バックアップ",
        navigation_policy=NavigationPolicy.BACKGROUNDABLE,
    )
    center.mark_running(op_id)

    composition = WorkflowApplicationComposition.__new__(
        WorkflowApplicationComposition
    )
    composition.activity_center = center
    composition.data_dir = tmp_path
    WorkflowApplicationComposition._account_for_exit_operations(composition)

    payload = json.loads(
        (tmp_path / ACTIVITY_HISTORY_FILENAME).read_text(encoding="utf-8")
    )
    assert [op["operation_id"] for op in payload["active_operations"]] == [
        op_id
    ]
    # The op is accounted, not fabricated terminal — it stays RUNNING.
    assert center.require(op_id).state == OperationState.RUNNING


def test_exit_accounting_writes_nothing_when_idle(tmp_path: Path) -> None:
    _app()
    composition = WorkflowApplicationComposition.__new__(
        WorkflowApplicationComposition
    )
    composition.activity_center = ActivityCenter()
    composition.data_dir = tmp_path
    WorkflowApplicationComposition._account_for_exit_operations(composition)
    assert not (tmp_path / ACTIVITY_HISTORY_FILENAME).exists()
