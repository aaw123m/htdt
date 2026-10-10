from __future__ import annotations

import atexit
import logging
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event

from PySide6.QtCore import QObject, QThread, Signal, Slot
from shiboken6 import isValid

_LOGGER = logging.getLogger('htdt.native_worker')

#: Cooperative shutdown budget shared by every native worker pool. It keeps
#: the 1.8 s per-task wait the duplicated prediction/search/REW paths used to
#: inline, but expiry is now a first-class outcome instead of silent teardown.
DEFAULT_WORKER_SHUTDOWN_TIMEOUT_MS = 1800

#: ``NativeWorker.completed`` error payload emitted when the owner requested
#: logical cancellation or the worker observed the flag before finishing.
WORKER_CANCELLED = "cancelled"


class NativeWorker(QObject):
    """Move-to-thread worker that runs one Python callable cooperatively.

    ``operation`` receives the worker's ``threading.Event`` cancellation flag.
    Cancellation is *logical*: the callable may keep running until it reaches
    a safe point, and a completion observed after cancellation is reported as
    ``"cancelled"`` so owners discard the result instead of applying it.
    Physical termination is owned by :class:`NativeWorkerPool`.
    """

    completed = Signal(object, object, object)  # key, result, error

    def __init__(
        self,
        key: str,
        operation: Callable[[Event], object],
    ) -> None:
        super().__init__()
        self.key = key
        self.operation = operation
        self.cancel_event = Event()

    def cancel(self) -> None:
        """Request cooperative cancellation; the callable may still finish."""
        self.cancel_event.set()

    @Slot()
    def run(self) -> None:
        if self.cancel_event.is_set():
            self.completed.emit(self.key, None, WORKER_CANCELLED)
            return
        try:
            result = self.operation(self.cancel_event)
        except Exception as exc:  # error-boundary: job dispatch — every operation failure must cross the thread boundary as the completion payload; the exception object preserves the consumer's type mapping (noqa: BLE001)
            if self.cancel_event.is_set():
                self.completed.emit(self.key, None, WORKER_CANCELLED)
            else:
                # Emit the exception itself, not str(exc): consumer surfaces
                # map by exception class (operation_error_message), and a
                # stringified payload would erase that type information
                # across the thread boundary.
                self.completed.emit(self.key, None, exc)
            return
        if self.cancel_event.is_set():
            self.completed.emit(self.key, None, WORKER_CANCELLED)
        else:
            self.completed.emit(self.key, result, None)


@dataclass(frozen=True, slots=True)
class WorkerShutdownReport:
    """Physical shutdown outcome of one ``NativeWorkerPool.shutdown`` call.

    ``stopped_keys`` finished within the wait budget. ``lingering_keys``
    ignored cooperative cancellation past the timeout: their threads were
    detached to module ownership and will delete themselves when the Python
    callable finally returns.
    """

    stopped_keys: tuple[str, ...] = ()
    lingering_keys: tuple[str, ...] = ()

    @property
    def all_stopped(self) -> bool:
        return not self.lingering_keys


# Threads that outlive an owner's shutdown budget are re-owned here so a
# disposed window/controller never destroys a still-running QThread. The map
# also pins each worker's Python reference until ``finished`` fires: a moved-
# to-thread object must never be deleted while its thread still runs, so the
# C++ object is owned by Python (never ``deleteLater`` — see ``start``) and is
# only released once the thread has actually finished. Python and SQLite work
# is never force-terminated: QThread.terminate() can leave the GIL, SQLite
# transactions and native solver state corrupt, so detaching is the only safe
# fallback and is therefore deliberate rather than accidental.
_LINGERING_THREADS: dict[QThread, NativeWorker] = {}

#: Every live pool, weakly — process/test teardown drains tasks whose owner
#: was destroyed without its orderly shutdown hooks (e.g. a shell deleted
#: while hidden, where ``close()`` never fires close hooks).
_LIVE_POOLS: "weakref.WeakSet[NativeWorkerPool]" = weakref.WeakSet()


def lingering_thread_count() -> int:
    """Worker threads currently detached after a shutdown timeout."""
    return len(_LINGERING_THREADS)


def _release_lingering(thread: QThread) -> None:
    _LINGERING_THREADS.pop(thread, None)


def drain_worker_threads(
    timeout_ms: int = DEFAULT_WORKER_SHUTDOWN_TIMEOUT_MS,
) -> int:
    """Best-effort cooperative stop of every pool task and detached thread.

    Shared teardown path for pytest session finish and interpreter exit:
    every live pool gets ``stop_all`` (cancel → interrupt → quit → bounded
    wait → detach), then each already-detached thread is cancelled and
    waited on inside the same budget. Returns the number of threads still
    running afterwards — a remainder is unkillable by design
    (``QThread.terminate`` corrupts the GIL/SQLite/native state) and is
    left detached rather than force-killed.
    """
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
    for pool in list(_LIVE_POOLS):
        remaining_ms = max(0, round((deadline - time.monotonic()) * 1000))
        try:
            pool.stop_all(remaining_ms)
        except RuntimeError:
            continue
    for thread, worker in list(_LINGERING_THREADS.items()):
        try:
            worker.cancel()
            thread.requestInterruption()
            thread.quit()
        except RuntimeError:
            # C++ object already gone: no signal can ever fire to release
            # the key, so a dead entry must be dropped here instead of
            # inflating every later count and iteration.
            _LINGERING_THREADS.pop(thread, None)
            continue
    still_running = 0
    for thread in list(_LINGERING_THREADS):
        remaining_ms = max(0, round((deadline - time.monotonic()) * 1000))
        try:
            if not thread.isFinished() and not thread.wait(remaining_ms):
                still_running += 1
        except RuntimeError:
            _LINGERING_THREADS.pop(thread, None)
            continue
    return still_running


def cancel_detached_threads() -> None:
    """Cooperative stop request on already-detached threads — no wait.

    Per-test teardown path: threads here are orphaned by definition (their
    owner died), so cancelling cannot disturb live fixtures. A detached
    thread that keeps running emits ``finished``/metacall posts which
    repost a stray widget's queued DeferredDelete behind each arrival —
    starving those emissions is what lets the widget drain converge.
    """
    for thread, worker in list(_LINGERING_THREADS.items()):
        try:
            worker.cancel()
            thread.requestInterruption()
            thread.quit()
        except RuntimeError:
            _LINGERING_THREADS.pop(thread, None)
            continue


def _drain_worker_threads_at_exit() -> None:
    """atexit hook: shrink the window where a detached thread outlives teardown.

    An interpreter exiting with a worker QThread still running lets
    ``~QThread`` terminate that thread mid-operation — a nondeterministic
    abort seen as ``worker 'gwN' crashed`` under xdist. Draining here
    gives cooperative jobs their last chance to unwind while Python is
    still fully alive.
    """
    try:
        drain_worker_threads()
    except Exception:  # error-boundary: exit teardown — drain must never raise into interpreter teardown; the failure identity is logged (noqa: BLE001)
        _LOGGER.exception('worker drain at interpreter exit failed')


atexit.register(_drain_worker_threads_at_exit)


class _WorkerCompletionRelay(QObject):
    """Queued per-task receiver for ``worker.completed``.

    Lives on the pool's owner thread: ``completed`` emits inside
    ``NativeWorker.run`` on the worker thread and PySide only queues
    delivery to receivers that have thread affinity — a plain callable
    connected directly would run on the worker thread, where its Qt calls
    (dialogs, status surfaces) deadlock or ghost. Each task gets its own
    relay, which forwards the payload tagged with the Python worker object
    it was created for, so the pool can gate delivery on *object identity*
    rather than ``QObject.sender()``. The sender pointer is unsafe to
    compare: the queued metacall stores a raw C++ pointer, and if the
    emitting worker is freed before dispatch its address can be reused by
    the next task's worker — a stale emission would then look live. The
    strong reference here keeps the emitting C++ object alive until the
    metacall has been consumed, so such reuse cannot happen at all.

    Releasing a task pops the relay's Python entry and *disconnects* the
    signal — never ``deleteLater``. The relay's wrapper is the last owner
    of ``self._worker``, so a deferred delete of the relay would cascade
    into destroying a moved-to-thread worker inside posted-event
    delivery — the same Windows abort the worker wiring itself avoids
    (see ``start``). The orphan C++ relay instead lives out its days as a
    pool child and dies with the pool.
    """

    def __init__(self, pool: "NativeWorkerPool", worker: NativeWorker) -> None:
        super().__init__(pool)
        self._pool = pool
        self._worker = worker

    @Slot(object, object, object)
    def receive(self, key: object, result: object, error: object) -> None:
        self._pool._dispatch_completed(self._worker, key, result, error)


class NativeWorkerPool(QObject):
    """Own the ``QThread`` + ``NativeWorker`` pairs of one controller or window.

    ``start`` encapsulates the move-to-thread wiring the prediction, search and
    REW paths used to duplicate. ``cancel``/``cancel_all`` perform *logical*
    cancellation only: the cooperative flag is set while the callable keeps
    running to its next safe point. ``shutdown`` performs the *physical* side —
    cancel, interrupt, quit every event loop and wait a bounded time. A worker
    that ignores cancellation past the timeout is detached to module ownership
    until it actually stops, so teardown is deterministic and no running
    QThread is destroyed together with its owner.

    ``on_completed``/``on_finished`` callbacks are delivered on the thread
    that owns this pool (normally the UI thread) through the pool's own
    queued slots — a plain callable is never invoked inside the worker,
    so GUI-touching handlers are safe without a per-call-site relay. A
    completion whose task record was already released (shutdown, stop_all,
    key restart) or that arrives from a superseded worker is dropped at
    dispatch, and a queued emission can therefore never resurrect a
    released task's callback.
    """

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        shutdown_timeout_ms: int = DEFAULT_WORKER_SHUTDOWN_TIMEOUT_MS,
    ) -> None:
        super().__init__(parent)
        self._shutdown_timeout_ms = int(shutdown_timeout_ms)
        self._tasks: dict[str, tuple[QThread, NativeWorker]] = {}
        self._callbacks: dict[
            str,
            tuple[
                Callable[[object, object, object], None] | None,
                Callable[[str], None] | None,
            ],
        ] = {}
        self._relays: dict[str, _WorkerCompletionRelay] = {}
        _LIVE_POOLS.add(self)
        self._shutdown_requested = False
        self._last_shutdown_report = WorkerShutdownReport()
        # destroy() must go through a plain callable: PySide6 silently never
        # delivers the signal to a bound method of the object being
        # destroyed (verified on PySide6 6.11), so a lambda keeps the detach
        # path live instead of dead-connected.
        self.destroyed.connect(lambda: self._detach_all())

    @property
    def tasks(self) -> dict[str, tuple[QThread, NativeWorker]]:
        """Live ``key -> (QThread, worker)`` records, dropped on ``finished``."""
        return self._tasks

    @property
    def last_shutdown_report(self) -> WorkerShutdownReport:
        return self._last_shutdown_report

    def __len__(self) -> int:
        return len(self._tasks)

    def __contains__(self, key: object) -> bool:
        return key in self._tasks

    @property
    def active_count(self) -> int:
        total = 0
        for thread, _worker in self._tasks.values():
            try:
                if thread.isRunning():
                    total += 1
            except RuntimeError:
                # Stale record: the C++ object is gone, so the thread
                # cannot be running — never report it as active.
                continue
        return total

    def start(
        self,
        key: str,
        operation: Callable[[Event], object],
        on_completed: Callable[[object, object, object], None] | None = None,
        *,
        on_finished: Callable[[str], None] | None = None,
    ) -> tuple[QThread, NativeWorker]:
        """Create, wire and start one worker thread owned by this pool.

        ``on_completed`` runs on this pool's owner thread (queued through
        the pool itself — plain callables are never invoked on the worker
        thread) and only while the task record is still live; ``on_finished``
        likewise runs on the owner thread when the thread finishes.
        """
        if self._shutdown_requested:
            raise RuntimeError("worker pool is shut down")
        if key in self._tasks:
            # A previous task with this key is still tracked. Its thread may
            # not have finished yet, so the worker must not lose its last
            # Python reference — detach it into module ownership first.
            self._detach(key)
        thread = QThread(self)
        worker = NativeWorker(key, operation)
        worker.moveToThread(thread)
        thread.setProperty("htdtWorkerKey", key)
        thread.started.connect(worker.run)
        if on_completed is not None:
            # Relay through a per-task QObject living on this pool's owner
            # thread: ``completed`` emits inside ``NativeWorker.run`` on
            # the worker thread, and PySide only queues delivery to
            # receivers that have thread affinity — a plain callable
            # (lambda, bound method of a non-QObject owner) would run
            # directly on the worker thread, where its Qt calls (dialogs,
            # status surfaces) deadlock or ghost.
            relay = _WorkerCompletionRelay(self, worker)
            self._relays[key] = relay
            worker.completed.connect(relay.receive)
        worker.completed.connect(thread.quit)
        # NOTE: the worker is deliberately never connected to deleteLater.
        # Deleting a moved-to-thread QObject while its QThread emits
        # ``finished`` races the native thread teardown (PySide6 on Windows:
        # sporadic access violation / abort). Python ownership keeps the C++
        # object alive via ``_tasks``/``_LINGERING_THREADS`` until the thread
        # has finished, then the reference drops on the owner thread.
        # ``_thread_finished`` first, ``deleteLater`` second — matching
        # ``data_management._start``: the record must be dropped before a
        # DeferredDelete can destroy the C++ object, or a typed
        # ``sendPostedEvents(DeferredDelete)`` flush between the two queued
        # metacalls leaves ``_tasks`` pointing at a dead thread.
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._tasks[key] = (thread, worker)
        self._callbacks[key] = (on_completed, on_finished)
        thread.start()
        return thread, worker

    def _dispatch_completed(
        self,
        worker: NativeWorker,
        key: object,
        result: object,
        error: object,
    ) -> None:
        """Invoke ``on_completed`` for a still-live task on the owner thread.

        Reached only through the task's own ``_WorkerCompletionRelay`` — a
        queued call, so this always runs on the pool's owner thread. The
        gate is at dispatch time rather than connect time: an emission
        queued before the task record was released
        (``shutdown``/``stop_all``/``_detach``) or emitted by a worker
        whose key has been restarted is dropped by the worker-identity
        check, which is strictly stronger than ``disconnect`` — Qt cannot
        retract an event that is already posted. Identity is compared as
        Python objects, never via ``QObject.sender()`` — that raw-pointer
        lookup can resolve to a different task's worker after an
        allocator-reuse, which is why the relay carries the worker itself
        and pins it alive. An exception escaping the callback propagates
        out to ``sys.excepthook`` like any other slot failure
        (diagnostics + uncaught surface); it is not swallowed here.
        """
        task_key = str(key)
        record = self._tasks.get(task_key)
        if record is None or record[1] is not worker:
            return
        callbacks = self._callbacks.get(task_key)
        on_completed = None if callbacks is None else callbacks[0]
        if on_completed is not None:
            on_completed(task_key, result, error)

    def cancel(self, key: str) -> bool:
        """Logically cancel one task; False when the key is unknown."""
        record = self._tasks.get(key)
        if record is None:
            return False
        record[1].cancel()
        return True

    def cancel_all(self) -> None:
        """Request logical cancellation on every tracked worker."""
        for _thread, worker in tuple(self._tasks.values()):
            worker.cancel()

    def shutdown(self, timeout_ms: int | None = None) -> WorkerShutdownReport:
        """Physically stop tracked workers with one bounded wait.

        Every worker is logically cancelled first so a late ``completed``
        result reports ``"cancelled"`` and cannot apply, then each thread is
        interrupted and asked to quit. Completions emitted before a task's
        record is released — including ones the worker already posted to the
        owner thread's queue — are dropped at dispatch by
        ``_dispatch_completed``, which Qt cannot retract once posted (see
        ``_release_task``). Threads get the
        remaining part of ``timeout_ms`` (default ``shutdown_timeout_ms``)
        to finish. A thread still running afterwards is detached to module
        ownership and surfaced through ``WorkerShutdownReport.lingering_keys``;
        its ``finished -> thread.deleteLater`` wiring stays connected and the
        worker stays referenced at module scope, so the pair cleans itself up
        whenever the callable returns.
        """
        self._shutdown_requested = True
        return self._stop_tracked(timeout_ms)

    def stop_all(self, timeout_ms: int | None = None) -> WorkerShutdownReport:
        """Physically drain every tracked worker WITHOUT shutting the pool down.

        Same bounded stop as :meth:`shutdown` (logical cancel → interrupt →
        quit → bounded wait → detach lingerers, queued completions dropped
        at dispatch), but ``_shutdown_requested`` stays unset: the pool — and therefore its
        owner — remains usable and a later ``start`` is still accepted. Used
        by the stop-busy deactivation escalation so an operator can abandon
        wedged work and keep working in the same window instead of being
        permanently vetoed by a worker that ignores its cancel flag.
        """
        return self._stop_tracked(timeout_ms)

    def _stop_tracked(self, timeout_ms: int | None) -> WorkerShutdownReport:
        budget = (
            self._shutdown_timeout_ms if timeout_ms is None else int(timeout_ms)
        )
        deadline = time.monotonic() + max(0, budget) / 1000.0
        stopped: list[str] = []
        lingering: list[str] = []
        self.cancel_all()
        for _key, (thread, _worker) in tuple(self._tasks.items()):
            try:
                thread.requestInterruption()
                thread.quit()
            except RuntimeError:
                # Stale record: the C++ object is gone, nothing to
                # interrupt — the record is released as stopped below.
                continue
        for key, (thread, _worker) in tuple(self._tasks.items()):
            done = False
            try:
                if not thread.isFinished():
                    remaining_ms = max(
                        0, round((deadline - time.monotonic()) * 1000)
                    )
                    thread.wait(remaining_ms)
                done = thread.isFinished() and not thread.isRunning()
            except RuntimeError:
                # The C++ object is gone: the thread cannot still be
                # running (~QThread on a live native thread aborts the
                # process), so the stale record is released as stopped
                # rather than detached.
                done = True
            if done:
                stopped.append(key)
                self._release_task(key)
            else:
                # The callable is still running (or the thread is still
                # starting up). Keep ownership at module scope: destroying
                # the QThread now would kill a live native thread.
                lingering.append(key)
                self._detach(key)
        report = WorkerShutdownReport(tuple(stopped), tuple(lingering))
        self._last_shutdown_report = report
        return report

    def _release_task(self, key: str) -> None:
        """Drop bookkeeping and silence not-yet-delivered completions.

        The owner callback is never connected to the worker directly — it
        dispatches through ``_dispatch_completed``, which requires the task
        record to still be live and the emitting worker to still own the
        key. Popping the record here therefore silences every later
        delivery, including emissions the worker already posted to the
        owner thread's queue (``disconnect`` does not retract those — the
        dead-receiver skip plus the record gate below does).
        """
        self._tasks.pop(key, None)
        self._callbacks.pop(key, None)
        relay = self._relays.pop(key, None)
        if relay is not None:
            # Disconnect, never deleteLater: freeing the relay's C++ side
            # would cascade into destroying the pinned worker inside
            # posted-event delivery (see _WorkerCompletionRelay).
            try:
                relay._worker.completed.disconnect(relay.receive)
            except RuntimeError:
                pass

    def _detach(self, key: str) -> None:
        """Move a still-running thread to module ownership until finished."""
        record = self._tasks.get(key)
        if record is None:
            return
        thread, worker = record
        if not isValid(thread):
            # The record outlived the thread's C++ object (``finished ->
            # deleteLater`` delivered before the record drop). Nothing can
            # still be running and no finished/destroyed signal will ever
            # fire to release a pin — and every call on the dead wrapper
            # raises RuntimeError — so releasing the stale bookkeeping is
            # the entire job. Dropping the record must not resurrect the
            # task as running either: it is simply gone.
            self._release_task(key)
            return
        # Pin the worker first: dropping the task record must never release
        # the last Python reference while the thread may still be running.
        try:
            _LINGERING_THREADS[thread] = worker
        except RuntimeError:
            # Died between the validity check and the pin — same outcome
            # as the dead-record branch above.
            self._release_task(key)
            return
        self._release_task(key)
        try:
            thread.setParent(None)
            thread.finished.connect(lambda: _release_lingering(thread))
            # The thread can finish in the window between the bounded wait
            # and this connect — its earlier finished->deleteLater wiring
            # still destroys the C++ object, so released-on-destroyed is
            # the guaranteed drop path for that race.
            thread.destroyed.connect(lambda *_args: _release_lingering(thread))
            finished = thread.isFinished()
        except RuntimeError:
            # The C++ object died mid-detach. It cannot still be running
            # (a running thread destroyed under itself aborts the process,
            # which is what this path exists to prevent), so the pin is
            # pointless — drop it instead of leaking a dead key.
            _LINGERING_THREADS.pop(thread, None)
            return
        if finished:
            # Finished between the shutdown check and the reparent.
            _LINGERING_THREADS.pop(thread, None)

    @Slot()
    def _detach_all(self) -> None:
        """Keep worker threads alive if the pool dies without shutdown().

        A dying pool cancels first: an owner destroyed without its orderly
        shutdown hooks (a hidden shell whose ``close()`` never fired close
        hooks) would otherwise abandon work with the cooperative flag
        unset, leaving jobs like backup scans churning to natural
        completion through process teardown — the xdist worker-crash
        class. Cancellation is flag-only and thread-safe here.
        """
        try:
            self.cancel_all()
        except RuntimeError:
            pass
        for key in tuple(self._tasks):
            try:
                self._detach(key)
            except RuntimeError:
                # One stale record must never abort the whole detach loop
                # — an undetached live thread is destroyed together with
                # the pool, which is the abort this handler prevents.
                continue

    def _drop_dead_records(self) -> None:
        """Release records whose QThread's C++ object is already gone.

        A queued ``finished`` whose sender died before delivery arrives
        here with ``sender() is None`` (verified on PySide6 6.11); the
        stale record would otherwise sit in ``_tasks`` forever and crash
        the next ``_detach``/``_stop_tracked``/``start`` that touches it.
        """
        for key, (thread, _worker) in tuple(self._tasks.items()):
            if not isValid(thread):
                self._release_task(key)

    @Slot()
    def _thread_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            self._drop_dead_records()
            return
        try:
            key = thread.property("htdtWorkerKey")
        except RuntimeError:
            self._drop_dead_records()
            return
        if key is None:
            return
        record = self._tasks.get(str(key))
        if record is not None and record[0] != thread:
            # Stale ``finished`` for a task whose key was already reused;
            # the detached thread must not drop the new task's record.
            return
        self._tasks.pop(str(key), None)
        callbacks = self._callbacks.pop(str(key), None)
        relay = self._relays.pop(str(key), None)
        if relay is not None:
            try:
                relay._worker.completed.disconnect(relay.receive)
            except RuntimeError:
                pass
        on_finished = None if callbacks is None else callbacks[1]
        if on_finished is not None:
            try:
                on_finished(str(key))
            except RuntimeError:
                pass


__all__ = [
    "DEFAULT_WORKER_SHUTDOWN_TIMEOUT_MS",
    "NativeWorker",
    "NativeWorkerPool",
    "WORKER_CANCELLED",
    "WorkerShutdownReport",
    "lingering_thread_count",
]
