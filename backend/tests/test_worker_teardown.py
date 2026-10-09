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
from shiboken6 import isValid

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
    """Deliver a deleteLater synchronously so ``destroyed`` handlers fire.

    The flush is scoped to this receiver: ``sendPostedEvents(None, ...)``
    would deliver every DeferredDelete queued in the process — including
    foreign strays left by earlier tests on the same xdist worker — and a
    stray ``~`` inside this pump is exactly the worker-crash class under
    test.
    """
    app = QApplication.instance() or QApplication([])
    obj.deleteLater()
    app.sendPostedEvents(obj, QEvent.Type.DeferredDelete)


def _seed_data(data_dir: Path) -> None:
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    CadMeasurementRepository(repository)

    raw = b'rev26-qtstab-asset\n'
    digest = sha256(raw).hexdigest()
    asset = data_dir / f'measurement-assets/{digest}'
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(raw)


def _controller(data_dir: Path) -> DataManagementController:
    backend = DataManagementBackend(data_dir)
    lifecycle = ApplicationDataLifecycle(
        freeze_mutations=lambda: None,
        release_data_handles=lambda: None,
        reopen_data_handles=lambda: None,
        thaw_mutations=lambda: None,
    )
    return DataManagementController(backend, lifecycle)


def _kill_thread_cpp(thread, app: QApplication) -> None:
    """Destroy the C++ QThread object while its task record still lives.

    The reported REV73 crash needs a *stale record*: ``_tasks``/``_active``
    referencing a QThread whose C++ object is already deleted. It happens
    when ``finished -> deleteLater`` is delivered before the record drop —
    reproduced here by never pumping the finished metacalls and flushing a
    receiver-typed DeferredDelete, the only delivery this Qt build honors.
    Every method call on the dead wrapper then raises ``RuntimeError:
    Internal C++ object (QThread) already deleted``.
    """
    thread.deleteLater()
    app.sendPostedEvents(thread, QEvent.Type.DeferredDelete)


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


def test_deferred_delete_needs_typed_delivery(app) -> None:
    """On this Qt build neither ``processEvents`` nor an unfiltered
    ``sendPostedEvents`` dispatches DeferredDelete: a queued delete
    survives ordinary pumps indefinitely, accumulating queued-but-alive
    strays that a later typed flush delivers all at once — mid-test in a
    foreign context, the worker-crash class. The shared teardown
    therefore uses DeferredDelete-typed sends per receiver.
    """
    from PySide6.QtWidgets import QWidget
    from shiboken6 import isValid

    widget = QWidget()
    widget.show()
    widget.deleteLater()

    app.sendPostedEvents()
    app.processEvents()
    assert isValid(widget)  # unfiltered delivery never lands the delete

    app.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
    assert not isValid(widget)  # typed delivery always does


def test_detach_releases_record_whose_thread_already_died(app) -> None:
    """REV73 defect: ``_detach`` on a record whose QThread C++ object is
    already deleted raised ``RuntimeError`` from the dict-pin line itself —
    leaving the stale record in ``_tasks`` and, inside ``_detach_all``,
    aborting the whole detach loop so live threads died with the pool.

    The fixed contract: release the stale bookkeeping, pin nothing (a dead
    key can never be released by finished/destroyed), and never let the
    vanished task count as running.
    """
    pool = NativeWorkerPool(shutdown_timeout_ms=50)
    baseline = lingering_thread_count()
    thread, _worker = pool.start("dead-on-detach", lambda _cancel: None)
    # worker.completed -> thread.quit is queued to the UI thread, so the
    # worker's exec loop only exits once quit() is requested directly —
    # finishing natively while its finished metacalls are still unpumped.
    thread.quit()
    assert thread.wait(5000)
    _kill_thread_cpp(thread, app)
    assert not isValid(thread)
    # The finished metacalls were never pumped, so the record outlived
    # the C++ object — the stale-record state behind the crash.
    assert "dead-on-detach" in pool.tasks

    pool._detach("dead-on-detach")

    assert "dead-on-detach" not in pool.tasks
    assert lingering_thread_count() == baseline
    # The vanished worker must not be re-reported as running.
    assert pool.active_count == 0
    app.processEvents()  # late finished metacall: dead sender, must not raise
    assert "dead-on-detach" not in pool.tasks
    _destroy(pool)


def test_detach_after_natural_finish_releases_without_linger(app) -> None:
    """A thread that finished before ``_detach`` is unpinned immediately —
    the record drops and nothing leaks into ``_LINGERING_THREADS``."""
    pool = NativeWorkerPool(shutdown_timeout_ms=50)
    baseline = lingering_thread_count()
    thread, _worker = pool.start("already-finished", lambda _cancel: None)
    thread.quit()
    assert thread.wait(5000)
    # C++ object still alive (deleteLater metacall not yet pumped): detach
    # must take the finished-check path, not the dead-object path.
    assert isValid(thread)
    assert "already-finished" in pool.tasks

    pool._detach("already-finished")

    assert "already-finished" not in pool.tasks
    assert lingering_thread_count() == baseline
    _destroy(pool)


def test_shutdown_releases_dead_record_as_stopped(app) -> None:
    """``_stop_tracked`` must not let a stale record raise RuntimeError out
    of ``shutdown()``: a thread whose C++ object is gone cannot be running
    (~QThread on a live thread aborts the process), so it counts as
    stopped — honestly, never as lingering."""
    pool = NativeWorkerPool(shutdown_timeout_ms=50)
    baseline = lingering_thread_count()
    thread, _worker = pool.start("dead-in-shutdown", lambda _cancel: None)
    thread.quit()
    assert thread.wait(5000)
    _kill_thread_cpp(thread, app)
    assert "dead-in-shutdown" in pool.tasks

    report = pool.shutdown()

    assert report.stopped_keys == ("dead-in-shutdown",)
    assert report.lingering_keys == ()
    assert report.all_stopped is True
    assert "dead-in-shutdown" not in pool.tasks
    assert lingering_thread_count() == baseline
    _destroy(pool)


def test_restart_releases_dead_record_before_start(app) -> None:
    """Key reuse calls ``_detach`` on the old record first: a dead old
    thread must release quietly so the fresh task starts — the old worker
    must not resurrect under the same key."""
    pool = NativeWorkerPool(shutdown_timeout_ms=50)
    baseline = lingering_thread_count()
    old_thread, _old_worker = pool.start("dup-dead", lambda _cancel: None)
    old_thread.quit()
    assert old_thread.wait(5000)
    _kill_thread_cpp(old_thread, app)
    assert "dup-dead" in pool.tasks

    started = Event()

    def fresh(cancel_event: Event) -> None:
        started.set()
        cancel_event.wait(5.0)

    new_thread, _new_worker = pool.start("dup-dead", fresh)
    assert _pump_until(started.is_set)
    assert new_thread is not old_thread
    record = pool.tasks["dup-dead"]
    assert record[0] is new_thread
    assert lingering_thread_count() == baseline

    report = pool.shutdown(timeout_ms=2000)
    assert report.stopped_keys == ("dup-dead",)
    _destroy(pool)


def test_pool_destroyed_with_dead_record_still_detaches_live_thread(app) -> None:
    """The reported crash path: inside ``_detach_all`` one stale record
    raised mid-loop, so the sibling *live* thread was never detached and
    ``~QThread`` then destroyed it while running — a fatal abort.

    Post-fix: the dead record is released and the live thread is detached
    to module ownership as designed — it keeps running after the pool dies.
    """
    pool = NativeWorkerPool(shutdown_timeout_ms=50)
    baseline = lingering_thread_count()
    started = Event()

    def watching(cancel_event: Event) -> None:
        started.set()
        cancel_event.wait(5.0)

    dead_thread, _dead_worker = pool.start("stale", lambda _cancel: None)
    dead_thread.quit()
    assert dead_thread.wait(5000)
    _kill_thread_cpp(dead_thread, app)
    assert "stale" in pool.tasks
    live_thread, live_worker = pool.start("live", watching)
    assert _pump_until(started.is_set)

    _destroy(pool)

    # The live thread survived its owner: still running, re-owned at
    # module scope — and the dead record is simply gone.
    assert live_thread.isRunning() is True
    assert lingering_thread_count() == baseline + 1
    assert live_worker.cancel_event.is_set()
    # The detached pair still cleans itself up once the callable returns.
    assert _pump_until(lambda: live_thread.isFinished())
    assert _pump_until(lambda: lingering_thread_count() == baseline)


def test_late_finished_metacall_for_dead_sender_drops_record(app) -> None:
    """A ``finished`` delivered after its sender died arrives with
    ``sender() is None`` (PySide6): the slot must release every stale
    record instead of leaving dead wrappers in ``_tasks`` to crash the
    next ``_detach``/``_stop_tracked``/``start``."""
    pool = NativeWorkerPool(shutdown_timeout_ms=50)
    baseline = lingering_thread_count()
    thread, _worker = pool.start("dead-sender", lambda _cancel: None)
    thread.quit()
    assert thread.wait(5000)
    _kill_thread_cpp(thread, app)
    assert "dead-sender" in pool.tasks

    app.processEvents()  # delivers the queued finished -> dead sender

    assert "dead-sender" not in pool.tasks
    assert lingering_thread_count() == baseline
    _destroy(pool)


def _instant_backup(data_dir, destination, *, allow_stale=False, is_cancelled=None):
    """Worker-side stand-in that returns immediately."""
    return object()


def test_detach_active_thread_releases_when_op_thread_already_died(
    app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same defect on the data-management twin: ``_detach_active_thread``
    with a stale ``_active`` (controller destroyed after the op thread's
    C++ object was deleted) pinned the dead wrapper then raised on
    ``setParent`` — RuntimeError out of a ``destroyed`` handler plus a
    leaked ``_LINGERING_OP_THREADS`` key."""
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    controller = _controller(data_dir)
    monkeypatch.setattr(
        'htdt.data_management.native_create_backup', _instant_backup
    )
    baseline = lingering_op_thread_count()

    controller.create_backup(tmp_path / 'out.htdt-backup')
    active = controller._active
    assert active is not None
    # worker.finished -> thread.quit is queued to the UI thread; quit()
    # directly so the op thread finishes natively while its finished
    # metacalls stay unpumped.
    active.thread.quit()
    assert active.thread.wait(5000)
    _kill_thread_cpp(active.thread, app)
    assert not isValid(active.thread)
    # The finished metacall was never pumped, so _active still holds the
    # dead thread — the stale-record state the sibling session hit.

    controller._detach_active_thread()

    assert controller._active is None
    assert lingering_op_thread_count() == baseline
    assert controller.can_close_application is True
    _destroy(controller)


def test_controller_destroyed_with_dead_op_thread_leaves_no_lingering(
    app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full crash path through ``destroyed``: the controller dies
    while ``_active`` references a deleted QThread — detach must release
    it without raising inside the destroyed handler and without leaking."""
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    controller = _controller(data_dir)
    monkeypatch.setattr(
        'htdt.data_management.native_create_backup', _instant_backup
    )
    baseline = lingering_op_thread_count()

    controller.create_backup(tmp_path / 'out.htdt-backup')
    active = controller._active
    assert active is not None
    active.thread.quit()
    assert active.thread.wait(5000)
    _kill_thread_cpp(active.thread, app)

    _destroy(controller)

    assert lingering_op_thread_count() == baseline
