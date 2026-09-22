from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, acoustic_reference_position, make_f1_scene
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import MeasurementWorkflowController
from htdt.native_worker import (
    DEFAULT_WORKER_SHUTDOWN_TIMEOUT_MS,
    WORKER_CANCELLED,
    NativeWorkerPool,
    lingering_thread_count,
)
from htdt.optimization_workflow_controller import OptimizationWorkflowController
from htdt.room_prediction import RoomPredictionController
from htdt.room_workspace import RoomWorkspaceController


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


def test_shutdown_detaches_worker_that_ignores_cancellation() -> None:
    """A worker past the shutdown budget must outlive its owner, not die with it."""
    _app()
    pool = NativeWorkerPool(shutdown_timeout_ms=100)
    baseline = lingering_thread_count()
    started = Event()

    def stubborn(_cancel_event: Event) -> str:
        started.set()
        time.sleep(0.6)  # intentionally ignores cancellation past the budget
        return "late-result"

    thread, _worker = pool.start("slow", stubborn)
    assert _pump_until(lambda: started.is_set())
    report = pool.shutdown()

    assert report.all_stopped is False
    assert report.lingering_keys == ("slow",)
    assert "slow" not in pool.tasks
    # The QThread is still alive and running — accessing it proves the C++
    # object was not destroyed together with the pool.
    assert thread.isRunning() is True
    assert lingering_thread_count() == baseline + 1

    pool.deleteLater()
    # The detached thread still stops itself once the Python callable returns.
    assert thread.wait(5000) is True
    assert _pump_until(lambda: lingering_thread_count() == baseline)


def test_default_budget_detaches_worker_running_past_1800ms() -> None:
    """Issue #301: a worker ignoring cancellation past the legacy 1.8 s wait
    must not be destroyed with its owner — the pool detaches it instead."""
    _app()
    pool = NativeWorkerPool()  # default 1800 ms shutdown budget
    baseline = lingering_thread_count()
    started = Event()

    def stubborn(_cancel_event: Event) -> None:
        started.set()
        time.sleep(DEFAULT_WORKER_SHUTDOWN_TIMEOUT_MS / 1000 + 0.4)

    thread, _worker = pool.start("slow-default", stubborn)
    assert _pump_until(lambda: started.is_set())

    report = pool.shutdown()

    assert report.lingering_keys == ("slow-default",)
    assert thread.isRunning() is True
    assert lingering_thread_count() == baseline + 1
    assert thread.wait(5000) is True
    pool.deleteLater()


def test_shutdown_stops_cooperative_worker_within_budget() -> None:
    _app()
    pool = NativeWorkerPool(shutdown_timeout_ms=1500)
    started = Event()

    def cooperative(cancel_event: Event) -> None:
        started.set()
        cancel_event.wait(5.0)

    thread, _worker = pool.start("coop", cooperative)
    assert _pump_until(lambda: started.is_set())

    report = pool.shutdown()

    assert report.all_stopped is True
    assert report.stopped_keys == ("coop",)
    assert thread.isFinished() is True
    assert "coop" not in pool.tasks
    pool.deleteLater()


def test_logical_cancel_reports_cancelled_and_releases_record() -> None:
    """Normal cancellation stays logical: cancelled completion, physical stop."""
    _app()
    pool = NativeWorkerPool()
    completions: list[tuple[object, object, object]] = []
    finished_keys: list[str] = []
    started = Event()

    def cooperative(cancel_event: Event) -> None:
        started.set()
        cancel_event.wait(5.0)

    pool.start(
        "cancel-me",
        cooperative,
        lambda key, result, error: completions.append((key, result, error)),
        on_finished=finished_keys.append,
    )
    assert _pump_until(lambda: started.is_set())

    assert pool.cancel("cancel-me") is True
    assert pool.cancel("unknown-key") is False

    assert _pump_until(lambda: "cancel-me" not in pool.tasks)
    assert completions == [("cancel-me", None, WORKER_CANCELLED)]
    assert finished_keys == ["cancel-me"]
    pool.deleteLater()


def test_late_completion_never_reaches_owner_after_shutdown() -> None:
    """A result emitted after shutdown must not hit the owner's slot."""
    _app()
    pool = NativeWorkerPool(shutdown_timeout_ms=100)
    completions: list[tuple[object, object, object]] = []
    started = Event()

    def slow(_cancel_event: Event) -> str:
        started.set()
        time.sleep(0.5)
        return "late"

    thread, _worker = pool.start(
        "late",
        slow,
        lambda key, result, error: completions.append((key, result, error)),
    )
    assert _pump_until(lambda: started.is_set())
    report = pool.shutdown()
    assert report.lingering_keys == ("late",)

    assert thread.wait(5000) is True
    _app().processEvents()
    assert completions == []
    pool.deleteLater()


def test_pool_rejects_new_work_after_shutdown() -> None:
    _app()
    pool = NativeWorkerPool(shutdown_timeout_ms=100)
    pool.shutdown()
    try:
        pool.start("nope", lambda _event: None)
    except RuntimeError:
        pass
    else:
        raise AssertionError("a shut-down pool must refuse new workers")
    pool.deleteLater()


def test_lingering_worker_stays_alive_until_thread_finishes() -> None:
    """A detached worker must keep its Python-owned C++ object alive until
    its thread finishes — the completion it finally emits must still land."""
    _app()
    pool = NativeWorkerPool(shutdown_timeout_ms=50)
    baseline = lingering_thread_count()
    started = Event()
    completions: list[tuple[object, object, object]] = []

    def stubborn(_cancel_event: Event) -> str:
        started.set()
        time.sleep(0.4)  # ignores cancellation past the shutdown budget
        return "late-result"

    thread, worker = pool.start("linger", stubborn)
    # A test-local slot survives _release_task: only the owner callback is
    # disconnected on detach, so this still proves the worker object lives.
    worker.completed.connect(lambda *args: completions.append(args))
    assert _pump_until(lambda: started.is_set())

    report = pool.shutdown()
    assert report.lingering_keys == ("linger",)
    assert lingering_thread_count() == baseline + 1

    assert thread.wait(5000) is True
    assert _pump_until(lambda: lingering_thread_count() == baseline)
    _app().processEvents()
    # Cancellation was requested, so the late completion reports cancelled —
    # the point is that the moved-to-thread object was still alive to emit it.
    assert completions == [("linger", None, WORKER_CANCELLED)]
    pool.deleteLater()


def test_restarting_a_busy_key_detaches_the_previous_task() -> None:
    """Reusing a task key while the old thread runs must not orphan the old
    worker's last Python reference — it is detached into module ownership."""
    _app()
    pool = NativeWorkerPool(shutdown_timeout_ms=50)
    baseline = lingering_thread_count()
    first_started = Event()
    second_started = Event()

    def stubborn(_cancel_event: Event) -> None:
        first_started.set()
        time.sleep(0.4)

    def quick(cancel_event: Event) -> None:
        second_started.set()
        cancel_event.wait(5.0)

    old_thread, _old_worker = pool.start("dup", stubborn)
    assert _pump_until(lambda: first_started.is_set())

    pool.start("dup", quick)
    assert lingering_thread_count() == baseline + 1
    new_thread, _new_worker = pool.tasks["dup"]
    assert new_thread is not old_thread
    assert old_thread.isRunning() is True

    assert _pump_until(lambda: second_started.is_set())
    pool.shutdown(timeout_ms=500)
    # The detached thread was never quit() directly — its event loop exits
    # when the queued ``completed -> thread.quit`` is delivered, which needs
    # the main event loop to keep pumping.
    assert _pump_until(lambda: old_thread.isFinished())
    assert old_thread.wait(5000) is True
    assert _pump_until(lambda: lingering_thread_count() == baseline)
    pool.deleteLater()


def test_repeated_cancel_shutdown_cycles_do_not_corrupt_teardown() -> None:
    """Regression for the Windows native crash seen in CI: repeated
    cooperative-cancel + bounded-shutdown cycles over pooled QThreads.
    The crash (fatal abort/access violation in Qt teardown) was bisected to
    deferred deletion of the moved-to-thread worker during ``finished``;
    the pool now releases workers through Python ownership instead."""
    app = _app()
    for index in range(120):
        pool = NativeWorkerPool(shutdown_timeout_ms=150)
        op_started = Event()

        def watching(cancel_event: Event) -> None:
            op_started.set()
            cancel_event.wait(5.0)

        pool.start(f"stress-{index}", watching)
        assert _pump_until(op_started.is_set)
        assert pool.cancel(f"stress-{index}") is True
        pool.shutdown(timeout_ms=500)
        del pool
        if index % 10 == 0:
            app.processEvents()
    app.processEvents()


def _prediction_controller(tmp_path: Path, operation):
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    # #627: the F1 fixture is explicit test content now, never auto-seeded.
    repository.save(make_f1_scene(), parent_revision_id=None)
    room = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    prediction = RoomPredictionController(repository, room, operation=operation)
    receiver = next(
        entity.entity_id
        for entity in room.committed_document.entities
        if acoustic_reference_position(entity) is not None
    )
    return repository, prediction, receiver


def test_dispose_keeps_noncooperative_prediction_thread_alive(tmp_path: Path) -> None:
    """Disposal must not destroy a running QThread — it detaches instead."""
    _app()
    started = Event()

    def stubborn_operation(_spec, _cancel_event: Event):
        started.set()
        time.sleep(0.6)
        return ()

    _repository, prediction, receiver = _prediction_controller(tmp_path, stubborn_operation)
    prediction._pool._shutdown_timeout_ms = 100

    assert prediction.start(receiver) is True
    job_id = prediction._current_job_id
    assert job_id is not None
    thread, _worker = prediction._tasks[job_id]
    assert _pump_until(lambda: started.is_set())

    prediction.dispose()

    # The running native thread survives the disposed controller.
    assert thread.isRunning() is True
    assert thread.wait(5000) is True
    _app().processEvents()


def test_dispose_prevents_late_prediction_result_from_persisting(tmp_path: Path) -> None:
    """A prediction that lands after disposal must never reach the repository."""
    _app()
    started = Event()

    def slow_operation(_spec, _cancel_event: Event):
        started.set()
        time.sleep(0.5)
        return None

    repository, prediction, receiver = _prediction_controller(tmp_path, slow_operation)
    prediction._pool._shutdown_timeout_ms = 100

    assert prediction.start(receiver) is True
    job_id = prediction._current_job_id
    assert job_id is not None
    thread, _worker = prediction._tasks[job_id]
    assert _pump_until(lambda: started.is_set())

    prediction.dispose()
    assert thread.wait(5000) is True
    _app().processEvents()

    assert prediction.prediction_repository.list_results(F1_DOCUMENT_ID) == ()


def test_cancelled_prediction_stays_cooperative(tmp_path: Path) -> None:
    """Cancellation is logical first; the worker still stops physically."""
    _app()
    op_started = Event()
    observed_cancel = Event()

    def watching_operation(_spec, cancel_event: Event):
        op_started.set()
        cancel_event.wait(5.0)
        observed_cancel.set()
        return None

    _repository, prediction, receiver = _prediction_controller(tmp_path, watching_operation)

    assert prediction.start(receiver) is True
    assert prediction.is_busy is True
    assert _pump_until(lambda: op_started.is_set())
    assert prediction.cancel() is True

    assert _pump_until(lambda: observed_cancel.is_set())
    assert _pump_until(lambda: not prediction.is_busy)
    prediction.dispose()


def test_workflow_controller_dispose_leaves_slow_rew_thread_alive(
    tmp_path: Path,
) -> None:
    """Controller disposal during an in-flight REW read detaches the thread."""
    _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    controller = OptimizationWorkflowController(repository, F1_DOCUMENT_ID)
    controller._rew_pool._shutdown_timeout_ms = 100
    started = Event()

    def slow_list() -> list:
        started.set()
        time.sleep(0.5)
        return []

    controller._start_rew_task("list:1", slow_list)
    assert _pump_until(lambda: started.is_set())
    thread, _worker = controller._rew_tasks["list:1"]

    controller.dispose()

    assert thread.isRunning() is True
    assert thread.wait(5000) is True
    _app().processEvents()


def test_measurement_page_workspace_close_detaches_running_job(tmp_path: Path) -> None:
    """Closing during active background work is deterministic, not destructive."""
    app = _app()
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    controller = MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=CadMeasurementRepository(scene_repository),
    )
    workspace = MeasurementPageWorkspace(controller)
    workspace._job_pool._shutdown_timeout_ms = 100

    applied: list[object] = []
    started = Event()

    def slow_call() -> str:
        started.set()
        time.sleep(0.5)
        return "late"

    workspace._start_job(slow_call, applied.append, "job")
    assert _pump_until(lambda: started.is_set())
    job_key = next(iter(workspace._job_pool.tasks))
    thread, _worker = workspace._job_pool.tasks[job_key]

    workspace.closeEvent(QCloseEvent())

    assert workspace._disposed is True
    assert thread.isRunning() is True
    assert thread.wait(5000) is True
    app.processEvents()
    assert applied == []
    workspace.deleteLater()
    app.processEvents()
