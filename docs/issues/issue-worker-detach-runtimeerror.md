# Issue — worker detach RuntimeError on deleted QThread (Refs #1014)

## Scope

During REV73 a sibling session hit
`RuntimeError: Internal C++ object (QThread) already deleted` while a
`NativeWorkerPool` task record still referenced its finished QThread.
`_detach` (and every other path that touches the record) called methods on
the dead wrapper — `setParent`, `connect`, `isRunning`, `isFinished`,
`requestInterruption`, `quit`, `wait`, `property` all raise on PySide6 —
and worse, a mid-loop raise inside `destroyed() -> _detach_all` aborted
the detach of *sibling* live threads, leaving a running `QThread` parented
to a pool that was then destroyed: `~QThread` on a live native thread
terminates the process.

`data_management._detach_active_thread` had the same exposure: the worker
pin `_LINGERING_OP_THREADS[thread] = worker` succeeded (dict insert on a
dead wrapper only raises on a hash collision — verified), then
`setParent(None)` raised inside the `destroyed` handler, leaking the pin
and propagating out of teardown.

## Root cause

`NativeWorkerPool.start` connected `thread.finished` in the wrong order:

```
thread.finished.connect(thread.deleteLater)      # was first — kills the C++ object
thread.finished.connect(self._thread_finished)   # was second — drops the record
```

Both slots post metacalls to the pool's thread. A *typed* flush
`sendPostedEvents(thread, QEvent.Type.DeferredDelete)` — used by the
teardown paths and by anything that drains deferred deletes — delivers the
delete between the two queued metacalls: the C++ object dies while
`_tasks[key]` still references it. `data_management._start` already had
the correct order (`_thread_finished` first, `deleteLater` second); the
pool is now wired the same way.

Once a stale record exists, two honest-behavior requirements follow:
a vanished worker must never be reported as running, and detach must not
resurrect phantom workers — the record is simply released as gone.

## Fix shape

`backend/src/htdt/native_worker.py`:

- `start()`: connect `finished -> _thread_finished` **before**
  `finished -> thread.deleteLater` (record drop precedes object death).
- `_detach`: `shiboken6.isValid(thread)` early-out releases the stale
  record; the pin and the `setParent`/`connect`/`isFinished` block are
  each wrapped in `try/RuntimeError` — a mid-detach death pops the pin
  instead of leaking it.
- `_detach_all`: per-key `try/RuntimeError` so one stale record can never
  abort the loop and leave a live sibling thread to be destroyed under
  its pool.
- `_thread_finished`: when the queued `finished` arrives with a dead
  sender (`sender()` returns `None`) or `property()` raises, the pool
  sweeps `_tasks` for dead threads (`_drop_dead_records`) instead of
  leaving the stale record forever.
- `_stop_tracked`: `requestInterruption`/`quit`/`wait`/`isFinished`/
  `isRunning` guarded per record; a dead record is released as *stopped*
  (it cannot still be running — that is the honest classification).
- `drain_worker_threads` / `cancel_detached_threads`: a dead lingering
  key is popped on `RuntimeError` — no signal will ever fire to release
  it — instead of inflating every later count.
- `active_count`, `_release_task`, `_thread_finished` disconnect: dead
  wrappers skipped / disconnect guarded.

`backend/src/htdt/data_management.py`:

- `_detach_active_thread`: `isValid` early-out after `_active` is
  cleared; pin + `setParent` + `finished`/`destroyed` connects +
  `isFinished` inside one `try/RuntimeError` that pops the pin on a
  mid-detach death. `destroyed -> pop` added for the
  finished-before-connect race (same guarantee the pool had).
- `drain_operation_threads` / `cancel_detached_op_threads`: dead
  lingering keys popped on `RuntimeError`.

## Reproduction

Offscreen pytest reproduces both surfaces exactly
(`backend/tests/test_worker_teardown.py`):

- Kill the C++ object while the record is stale: finish the thread via a
  direct `thread.quit()` + `wait()` (keeps the queued `finished`
  metacalls unpumped), then `deleteLater()` +
  `sendPostedEvents(thread, DeferredDelete)`. The record still sits in
  `_tasks`/`_active` — the reported production shape.
- Old code: `_detach`/`_detach_active_thread` raise `RuntimeError` inside
  `destroyed()` handlers and leak the pin; `_stop_tracked` raises on
  `requestInterruption`; `destroyed()` on a pool holding a stale record
  plus a live sibling aborts the whole test process (exit 127 —
  `QThread destroyed while still running`).
- New code: all stale records release cleanly, lingering dicts return to
  baseline, live siblings still detach, and `report.stopped` honestly
  contains the vanished key.

Regression coverage: detach after natural finish, detach after C++
object deletion, `shutdown()`/`start()` over a dead record, pool destroy
with dead+live records, late `finished` metacall for a dead sender, and
the `_LINGERING_OP_THREADS` equivalents for `DataManagementController`
including the destroyed-mid-operation path.
