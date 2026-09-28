from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event

from PySide6.QtCore import QObject, QThread, Signal, Slot


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
        except Exception as exc:
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


def lingering_thread_count() -> int:
    """Worker threads currently detached after a shutdown timeout."""
    return len(_LINGERING_THREADS)


def _release_lingering(thread: QThread) -> None:
    _LINGERING_THREADS.pop(thread, None)


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
        return sum(
            1 for thread, _worker in self._tasks.values() if thread.isRunning()
        )

    def start(
        self,
        key: str,
        operation: Callable[[Event], object],
        on_completed: Callable[[object, object, object], None] | None = None,
        *,
        on_finished: Callable[[str], None] | None = None,
    ) -> tuple[QThread, NativeWorker]:
        """Create, wire and start one worker thread owned by this pool."""
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
            worker.completed.connect(on_completed)
        worker.completed.connect(thread.quit)
        # NOTE: the worker is deliberately never connected to deleteLater.
        # Deleting a moved-to-thread QObject while its QThread emits
        # ``finished`` races the native thread teardown (PySide6 on Windows:
        # sporadic access violation / abort). Python ownership keeps the C++
        # object alive via ``_tasks``/``_LINGERING_THREADS`` until the thread
        # has finished, then the reference drops on the owner thread.
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        self._tasks[key] = (thread, worker)
        self._callbacks[key] = (on_completed, on_finished)
        thread.start()
        return thread, worker

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
        result reports ``"cancelled"`` and cannot apply. Owner completion
        slots are then disconnected — a belt-and-suspenders measure, since
        already-queued emissions still require the owner's disposed guard —
        and each thread is interrupted and asked to quit. Threads get the
        remaining part of ``timeout_ms`` (default ``shutdown_timeout_ms``)
        to finish. A thread still running afterwards is detached to module
        ownership and surfaced through ``WorkerShutdownReport.lingering_keys``;
        its ``finished -> thread.deleteLater`` wiring stays connected and the
        worker stays referenced at module scope, so the pair cleans itself up
        whenever the callable returns.
        """
        self._shutdown_requested = True
        budget = (
            self._shutdown_timeout_ms if timeout_ms is None else int(timeout_ms)
        )
        deadline = time.monotonic() + max(0, budget) / 1000.0
        stopped: list[str] = []
        lingering: list[str] = []
        self.cancel_all()
        for _key, (thread, _worker) in tuple(self._tasks.items()):
            thread.requestInterruption()
            thread.quit()
        for key, (thread, _worker) in tuple(self._tasks.items()):
            if not thread.isFinished():
                remaining_ms = max(
                    0, round((deadline - time.monotonic()) * 1000)
                )
                thread.wait(remaining_ms)
            if thread.isFinished() and not thread.isRunning():
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
        """Drop bookkeeping and silence not-yet-emitted completions."""
        record = self._tasks.pop(key, None)
        callbacks = self._callbacks.pop(key, None)
        if record is None or callbacks is None:
            return
        _thread, worker = record
        on_completed = callbacks[0]
        if on_completed is not None:
            try:
                worker.completed.disconnect(on_completed)
            except (RuntimeError, TypeError):
                pass

    def _detach(self, key: str) -> None:
        """Move a still-running thread to module ownership until finished."""
        record = self._tasks.get(key)
        if record is None:
            return
        thread, worker = record
        # Pin the worker first: dropping the task record must never release
        # the last Python reference while the thread may still be running.
        _LINGERING_THREADS[thread] = worker
        self._release_task(key)
        thread.setParent(None)
        thread.finished.connect(lambda: _release_lingering(thread))
        if not thread.isRunning():
            # Finished between the shutdown check and the reparent.
            _LINGERING_THREADS.pop(thread, None)

    @Slot()
    def _detach_all(self) -> None:
        """Keep worker threads alive if the pool dies without shutdown()."""
        try:
            for key in tuple(self._tasks):
                self._detach(key)
        except RuntimeError:
            pass

    @Slot()
    def _thread_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        key = thread.property("htdtWorkerKey")
        if key is None:
            return
        record = self._tasks.get(str(key))
        if record is not None and record[0] != thread:
            # Stale ``finished`` for a task whose key was already reused;
            # the detached thread must not drop the new task's record.
            return
        self._tasks.pop(str(key), None)
        callbacks = self._callbacks.pop(str(key), None)
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
