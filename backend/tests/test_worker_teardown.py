from __future__ import annotations

"""REV26-QTSTAB: teardown-safety contracts for orphaned worker threads.

A shell/controller destroyed without its orderly shutdown hooks (widgets
never shown in tests never fire ``closeEvent``) must still cancel its
in-flight work cooperatively at detach — otherwise an un-cancelled job
keeps churning through process teardown and ``~QThread`` terminates it
mid-operation, the nondeterministic xdist ``worker 'gwN' crashed`` class.
"""

import os
import time
from hashlib import sha256
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.data_management import (
    ApplicationDataLifecycle,
    DataManagementBackend,
    DataManagementController,
    drain_operation_threads,
    lingering_op_thread_count,
)
from htdt.native_backup import BackupCancelledError
from htdt.native_worker import (
    WORKER_CANCELLED,
    NativeWorkerPool,
    drain_worker_threads,
    lingering_thread_count,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


def _pump_until(predicate, timeout_s: float = 5.0) -> bool:
    app = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _destroy(obj: object) -> None:
    """Deliver a deleteLater synchronously so ``destroyed`` handlers fire."""
    app = QApplication.instance() or QApplication([])
    obj.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def _seed_data(data_dir: Path) -> None:
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    CadMeasurementRepository(repository)

    raw = b'rev26-qtstab-asset\n'
    digest = sha256(raw).hexdigest()
    asset = data_dir / f'measurement-assets/{digest}'
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(raw)


def test_pool_destroyed_without_shutdown_cancels_workers(app) -> None:
    """_detach_all must request cooperative cancel: a dead pool's work stops.

    Before this, a pool destroyed without ``shutdown()`` detached tasks
    with ``cancel_event`` unset — orphaned jobs ran to natural completion
    through interpreter teardown.
    """
    pool = NativeWorkerPool(shutdown_timeout_ms=50)
    baseline = lingering_thread_count()
    started = Event()
    completions: list[tuple[object, object, object]] = []

    def watching(cancel_event: Event) -> str:
        started.set()
        cancel_event.wait(5.0)
        return "should-never-deliver"

    thread, worker = pool.start("orphan", watching)
    worker.completed.connect(lambda *args: completions.append(args))
    assert _pump_until(started.is_set)

    _destroy(pool)

    assert worker.cancel_event.is_set()
    assert lingering_thread_count() >= baseline
    # Pumping delivers the queued ``completed -> thread.quit``: the map
    # entry drops synchronously once the worker unwinds.
    assert _pump_until(lambda: lingering_thread_count() == baseline)
    # The cancelled late completion still lands on test-local slots.
    _pump_until(lambda: bool(completions))
    assert completions == [("orphan", None, WORKER_CANCELLED)]


def test_drain_worker_threads_stops_live_pool_task(app) -> None:
    """drain_worker_threads covers pools still referenced at session end —
    the registry path for pools whose owner never ran close hooks."""
    pool = NativeWorkerPool(shutdown_timeout_ms=50)
    started = Event()

    def watching(cancel_event: Event) -> None:
        started.set()
        cancel_event.wait(30.0)

    thread, _worker = pool.start("drain-me", watching)
    assert _pump_until(started.is_set)

    still_running = drain_worker_threads(2000)

    assert still_running == 0
    assert thread.isFinished() is True
    assert _pump_until(lambda: "drain-me" not in pool.tasks)
    _destroy(pool)


def test_drain_worker_threads_stops_detached_thread(app) -> None:
    """A worker already detached into module ownership gets cancelled and
    waited on — the process-exit window shrinks to the drain budget."""
    pool = NativeWorkerPool(shutdown_timeout_ms=50)
    baseline = lingering_thread_count()
    started = Event()

    def watching(cancel_event: Event) -> None:
        started.set()
        cancel_event.wait(30.0)

    thread, _worker = pool.start("detached", watching)
    assert _pump_until(started.is_set)
    _destroy(pool)
    # The cancelled worker may already have finished — the map entry drops
    # synchronously on ``finished``, so no intermediate count is asserted.

    still_running = drain_worker_threads(2000)

    assert still_running == 0
    assert _pump_until(lambda: lingering_thread_count() == baseline)


def _slow_backup(
    data_dir: Path,
    destination: Path,
    *,
    allow_stale: bool = False,
    is_cancelled=None,
):
    """Worker-side stand-in that only finishes via the cancel seam."""
    for _ in range(500):
        if is_cancelled is not None and is_cancelled():
            raise BackupCancelledError('stopped by test')
        time.sleep(0.01)
    raise AssertionError('cancel flag was never observed')


def test_controller_destroyed_mid_operation_requests_cancel(
    app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_detach_active_thread must request cooperative cancel: a dead
    controller's job unwinds at its next checkpoint instead of running to
    natural completion through interpreter teardown."""
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    backend = DataManagementBackend(data_dir)
    lifecycle = ApplicationDataLifecycle(
        freeze_mutations=lambda: None,
        release_data_handles=lambda: None,
        reopen_data_handles=lambda: None,
        thaw_mutations=lambda: None,
    )
    controller = DataManagementController(backend, lifecycle)
    monkeypatch.setattr(
        'htdt.data_management.native_create_backup', _slow_backup
    )

    baseline = lingering_op_thread_count()
    controller.create_backup(tmp_path / 'out.htdt-backup')
    assert controller.is_busy
    active = controller._active
    assert active is not None
    worker = active.worker

    _destroy(controller)

    assert worker._cancel_event.is_set()
    assert _pump_until(lambda: lingering_op_thread_count() == baseline)


def test_drain_operation_threads_stops_live_controller_op(
    app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """drain_operation_threads covers controllers still referenced at
    session end — the registry path for owners that never ran close hooks."""
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    backend = DataManagementBackend(data_dir)
    lifecycle = ApplicationDataLifecycle(
        freeze_mutations=lambda: None,
        release_data_handles=lambda: None,
        reopen_data_handles=lambda: None,
        thaw_mutations=lambda: None,
    )
    controller = DataManagementController(backend, lifecycle)
    monkeypatch.setattr(
        'htdt.data_management.native_create_backup', _slow_backup
    )

    controller.create_backup(tmp_path / 'out.htdt-backup')
    assert controller.is_busy

    still_running = drain_operation_threads(5000)

    assert still_running == 0
    assert _pump_until(lambda: not controller.is_busy)
    _destroy(controller)


def test_deferred_delete_repost_drains_in_bounded_loop(app) -> None:
    """A widget with pending events survives ONE deferred-delete flush:
    Qt reposts the delete behind the pending events, and the stray is then
    destroyed inside whichever code path next pumps events — a mid-test
    destructor in an unrelated context. The shared teardowns therefore
    loop ``sendPostedEvents`` + ``processEvents`` until the batch is gone.
    """
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QWidget
    from shiboken6 import isValid

    widget = QWidget()
    widget.show()
    # A pending posted event forces Qt to repost this widget's
    # DeferredDelete behind it, so one flush is not enough.
    QCoreApplication.postEvent(widget, QEvent(QEvent.Type.User))
    widget.deleteLater()

    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    # isValid may still be True here: the delete reposts behind the user
    # event — the exact stray the bounded loop exists to cover.

    for _ in range(4):
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        if not isValid(widget):
            break

    assert not isValid(widget)
