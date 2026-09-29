# Round 19 — Worker offload contracts (REV19-WORKERS)

This round closes the round-13 deferrals D1/D2/D3/D6: operations whose worker
plumbing lacked a cancel seam or an escalation path now use the established
contract (`NativeWorkerPool.start` + cancel_event kwarg + `_disposed` guards +
honest Japanese failure surfaces), and the bundle export/import lane — the last
verified UI-blocking file I/O — moved onto a worker pool.

## The contract applied everywhere

- **Job signature.** Pool jobs take `(cancel_event)`; data-management worker
  jobs take `(emit, cancel_event, on_commit_point)`. Tests that stubbed the
  internals were updated to the new signature — the seam is the contract.
- **Cancel seam.** Backend functions accept `is_cancelled: Callable[[], bool] |
  None` and raise a per-module `*CancelledError` at loop/committee checkpoints.
- **Cancellability classes.** Read-only / temp-dir ops are `CANCELLABLE`
  (backup create, restore validate, storage scan, storage GC). Restore and
  relocate are `CANCEL_UNTIL_COMMIT`: once the durable commit point is passed
  the operation always completes. Commit points are explicit:
  restore = `_write_restore_journal` + directory fsync; relocation = the
  STAGED_VERIFIED journal write. Both are reported to the ActivityCenter via
  `mark_commit_point` on the UI thread (`_OperationWorker.commit_reached`
  signal → `_operation_commit_reached` slot).
- **Honest cancel surface.** `_OperationWorker` maps only the
  `_OPERATION_CANCEL_EXCEPTIONS` tuple to its `cancelled` signal — a real
  exception is never mislabeled as a cancel. The UI cancel button
  (`dataManagementCancelButton`) reflects the state: "中止" → "中止を要求しています…"
  after a request, or "この処理は安全に中止できる段階を過ぎています" when the
  request arrived post-commit. Cancellation lands as
  `operation_cancelled` + status card "処理を中止しました · 途中までの結果は適用されていません".
- **Registry truth.** The ActivityCenter gets `request_cancel` →
  `CANCELLATION_REQUESTED` → worker callback → `confirm_cancelled` →
  `CANCELLED`; a cancel that lands after completion is a legal no-op
  (cancel-too-late → normal `COMPLETED`).

## Fixed

### D2 — data-management ops are now cancellable end-to-end

- `native_backup`: `BackupCancelledError` + checkpoints in asset-copy, aux,
  legacy-member, zip-write and extract loops of
  `create_backup`/`validate_backup`/`inspect_backup`/`restore_backup`. The
  restore path's forensic `except Exception` re-raises
  `BackupCancelledError` first so a cancel is never swallowed as a forensic
  fallback. `on_commit_point` fires right after the restore journal is durable.
- `data_relocation`: `DataRelocationCancelledError`; checkpoints across the
  live-assets, carried-components and root-file loops; the last gate sits
  after `assert_native_authority_graph(staged_database)` — still inside the
  PREPARED window where `except` performs the full staging cleanup —
  `on_commit_point` fires after the STAGED_VERIFIED journal.
- `storage_maintenance`: `StorageMaintenanceCancelledError`; checkpoints in
  the diagnostics rglob, registry and iterdir loops of `scan_storage` and the
  per-candidate txn loop of `run_storage_gc` (in-txn cancel rolls back via the
  existing `except`; post-commit unlink cancel leaves `pending` rows that the
  next GC pass resumes — designed, not leaked).
- `data_management.DataManagementController`: `_OperationWorker` gains
  `cancelled`/`commit_reached` signals, `request_cancel()` (thread-safe Event
  set), and `run()` calls `job(emit, cancel_event, on_commit_point)`.
  `_submit_operation` computes each kind's `Cancellability` and wires the
  registry `cancel_callback` to the live worker; the immediate-failure path
  stays `NOT_CANCELLABLE` (nothing runs to cancel). Public `request_cancel()`
  goes through the ActivityCenter when present (honoring `can_cancel_now`)
  and otherwise falls back to the active worker directly — still refusing
  once `commit_reached`.
- `data_management_ui`: progress card now pairs a `中止` pushbutton with the
  bar; `operation_cancelled` shows the honest "cancelled — partial results not
  applied" card including the restart-required flag.

### D3 — cancel_event reaches the REW jobs

`_start_job` / `_start_rew_task` lambdas in `measurement_page_workspace`,
`optimization_workflow_controller` and `measurement_editor` used to discard
the pool's `cancel_event` (`lambda _cancel_event: call()`). They now thread it
into the callable; `MeasurementWorkflowController.list_rew_measurements` /
`fetch_rew_snapshot` accept `cancel_event` and forward `is_cancelled` to the
client; `RewApiClient` gains `RewApiCancelledError` and checks before/between
the four sequential snapshot requests (list → get → snapshot), so a stopped
job releases mid-pipeline rather than after the final response.

### D1 — wedged worker no longer vetoes the session forever

The `busy` dirty state is now resolvable: `dirty_state_prompt('busy', …)`
offers a single destructive choice, `stop_busy` ("処理を中止して続行"). Each
consumer resolves it by draining its pools without disposing them:

- `NativeWorkerPool.stop_all(timeout_ms)` — the same bounded drain as
  `shutdown()` (cancel_all → requestInterruption/quit → bounded wait →
  detach lingerers, owner slots disconnected) but `_shutdown_requested`
  stays unset so the pool — and its owner — keep working.
- `MeasurementPageWorkspace.resolve_dirty_state` → `_job_pool.stop_all()` +
  job bookkeeping reset + refresh; lingering workers report
  "停止が遅延している処理の結果は適用されません".
- `OptimizationWorkflowWorkspace.resolve_dirty_state` → cancels the
  controller's job-guard tokens, stops the three panel-owned pools
  (`joint_optimization_panel`, `robustness_authoring_panel`,
  `SystemExpansionOptimizePanel` — each gained `stop() -> WorkerShutdownReport`)
  and the controller's own pools; detached late results stay discarded via
  the guard/generation checks.
- `RoomPredictionController.stop()` (reached through the room mount's
  resolve wrapper) → cancels tokens + `_pool.stop_all()` + clears specs and
  emits a `stateChanged` row saying the run was aborted / late results are
  not saved or applied.

Every existing `before_deactivate` busy-veto stays — the veto is still the
default; `stop_busy` is the operator's explicit, destructive escape, so a
wedged or simply unwanted worker no longer deadlocks navigation, project
switch, or exit.

### Bundle export/import moved off the UI thread

`WorkflowApplication._export_project_bundle` / `_import_project_bundle`
previously ran `export_project_bundle` / `import_project_bundle` /
`import_project_bundle_as_copy` inline — multi-second zip + DB I/O that
froze the window. They now run on a dedicated `_bundle_pool`
(`NativeWorkerPool`) with:

- `_bundle_busy` serial gating (one bundle op at a time — the ops mutate the
  same managed tree), a `WaitCursor`, and a status-bar line while running.
- A close-hook drain (`_shutdown_bundle_pool`) plus a
  `can_close_application` veto ("プロジェクトバンドル処理が完了してから終了してください")
  so a running bundle op still blocks exit honestly.
- Success/failure surfaces identical to the old synchronous path, now driven
  by `_bundle_job_completed`: export shows row/asset counts + the manifest
  SHA-256 in details; `WORKER_CANCELLED` reports "プロジェクトバンドル処理を中止しました";
  import still offers the `BundleImportConflictError` "import as copy" retry,
  which re-enters through the same busy-gated pool.
- Repository calls underneath are worker-safe because `SceneRepository`
  opens a per-call connection (`_connect()` per operation), verified in an
  earlier round.

`resolve_dirty_state` for data management deliberately does **not** get a
detach-and-unblock variant: these ops mutate one managed tree under a serial
lifecycle — the honest escape is `request_cancel()` on the cancel button,
not abandoning the thread (see D6).

## Deferred (documented, unchanged)

- **D6 — workers are never `deleteLater`d.** Deliberate: `QThread.terminate`
  on Windows while a thread holds the GIL inside SQLite or a native solver
  corrupts the process; detached threads live in `_LINGERING_THREADS` under
  module ownership until the callable returns — a designed, bounded leak
  visible via `lingering_thread_count()`. New code keeps the contract.
- **`complete_measurement_plan` / `build_and_save_selected_campaign_validation`
  stay synchronous** — round11-tail already adjudicated: both iterate
  user-authored lists (selected measurements, preregistered campaign
  candidates — realistically tens of rows) with indexed gets; a worker +
  cancel surface would be dead UI weight. Revisit only if campaigns grow to
  hundreds of evidence rows.
- **The ~60 ms/row importer-replay floor (round11-scale #8)** stays: skipping
  it would weaken the fail-closed evidence contract; the fix is a
  revision-signature-keyed read model or a background refresh worker — a
  product-level redesign, not plumbing.
- **Data-management `stop_busy`** is limited to `request_cancel()`: ops are
  serial mutations of one managed tree, so "leave anyway" cannot detach —
  the worker owns the tree until it finishes or acknowledges cancellation.

## Regression tests

`tests/test_round19_worker_contracts.py` (10 tests):

- REW snapshot/list raise `RewApiCancelledError` before any request.
- `create_backup`/`validate_backup`/`restore_backup` raise
  `BackupCancelledError` on flag; restore pre-commit cancel leaves the live
  store untouched and never calls `on_commit_point`; a clean restore fires
  `on_commit_point` exactly once.
- `scan_storage` and `execute_data_relocation` raise their cancel errors;
  relocation leaves source intact and nothing promoted at the destination.
- `stop_all` drains yet leaves the pool usable — a second `start` completes.
- `dirty_state_prompt('busy')` is resolvable with the destructive
  `stop_busy` choice.
- Controller end-to-end: `create_backup` on the real worker →
  `request_cancel()` → `operation_cancelled` + ActivityCenter `CANCELLED`,
  no `operation_failed`.

Existing fixtures updated to the new job signatures (`(emit, cancel_event,
on_commit_point)` worker jobs; `cancel_event`-taking `_start_job`/
`_start_rew_task` callables; `_FakeController` `operation_cancelled` +
`request_cancel`) — behavior assertions unchanged except
`test_dirty_state_prompt_clean_and_busy`, which now asserts the resolvable
`stop_busy` contract.

Scoped suite: 243 tests across the worker/data-management/workspace files —
all green.
