# Round 17 — Threading / concurrency correctness (REV17-CONCUR)

Scope: every `QThread`/worker surface in the shipped native stack —
`native_worker.py` (`NativeWorkerPool`), `data_management.py`
(`_OperationWorker`), `cad_r140_executor.py` (`BoundedR140Executor` +
`ExecutionCancellationToken`), `room_prediction.py`, all `pool.start`
consumers (joint optimization, robustness, system expansion, search /
extended / adaptive controllers, prediction & measurement workspaces,
measurement editor, automatic backup runner), `capture_receiver*`
(`ThreadingHTTPServer`), `launch_intents`/`native_cad` pumps &
`QTimer` drains, `native_diagnostics` hooks, module-level shared state.
Rounds 1–16 (esp. `round13-workers.md`) were read first; their findings
are not re-reported. Branch `devin/rev17-concur`.

Base: `origin/main @ 434a624f` ("Merge REV16-CONSIST"). All verification
local: Python 3.12.10, `QT_QPA_PLATFORM=offscreen`, pytest.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | `AdaptiveControllerMixin._adaptive_build_completed` and `AdaptiveExtendedControllerMixin._adaptive_extended_build_completed` were the only worker-completion slots without the `_disposed` guard — a completion queued at `dispose()` still delivered and ran `refresh_adaptive_plans`/`statusBar` against the torn-down controller (repo reads + tree rebuild post-`scene.close()`) | MED | FIXED |
| 2 | Six `pool.start` launch paths lacked the `_disposed` check every `_start_*` helper enforces — a late click/queued signal after `dispose()` hit `NativeWorkerPool.start` on a shut-down pool → `RuntimeError` escaped the slot into the uncaught-exception surface: `_execute_selected` (joint), `_run` (robustness), `build_selected_adaptive_plan`, `build_selected_adaptive_extended_plan`, `_create_proposal`, `_evaluate` | MED | FIXED |
| 3 | `MeasurementWorkflowController.commit_batch` mutates shared `_BatchEntry` fields on the worker thread; the per-row resolution combo (`_batch_resolution_changed` → `set_batch_resolution`) stayed enabled during a commit → GUI write racing worker reads of `entry.resolution`/`entry.duplicate_kind` → nondeterministic per-item commit path | MED | FIXED — table disabled for commit duration |
| 4 | `AutomaticBackupRunner._on_completed` had no post-`shutdown()` gate — a completion queued before `shutdown()` still emitted `backup_completed` into the closing composition (Activity Center write + `statusBar` on a dying shell) | LOW-MED | FIXED |
| 5 | `PredictionExecutionController` binds `executor.progress_sink` only when `None` and has no release path — a second controller (or one built after its predecessor is gone) silently starves its progress view | LOW | DEFERRED — authority not yet UI-wired; the right fix is an explicit lifecycle (close/release), not a subtle rebind |
| 6 | `cad_directivity._sample_map_cache` — module-level `id(dataset) -> mapping` dict written from workers, read cross-thread, no lock | — | VERIFIED OK — pure function of the pinned dataset; CPython dict ops atomic; worst case a duplicate recompute. Unbounded growth noted for a later memory round |
| 7 | `native_diagnostics` module state (`_uncaught_sinks`, `_QT_LEVELS`, hook globals) | — | VERIFIED OK — sinks pushed/popped/read on GUI thread only; `_post_uncaught_to_sink` gates on `QThread.currentThread() is app.thread()`; `_QT_LEVELS` lazy-fill is idempotent under the GIL |
| 8 | `capture_receiver` `ThreadingHTTPServer` + `stop()` | — | VERIFIED OK — daemon threads, per-call sqlite connections, `shutdown()`+`server_close()`+`join(5)`, delivery re-emitted through a queued signal |
| 9 | `native_cad` `intent_pump`/`QFileSystemWatcher` drain closing over the first `window` | — | VERIFIED OK — `_route_launch_intent` re-resolves `application.live_composition()` before touching the window; timers only fire while the app event loop runs |
| 10 | `BoundedR140Executor` lock discipline (`_tokens`/`_futures`/`_closed`) | — | VERIFIED OK — all mutations under `self._lock`, no Qt/I-O calls inside it, `ExecutionCancellationToken` callbacks run outside the token lock |
| 11 | `NativeWorkerPool` completed/finished stale-drop + lingering detach | — | VERIFIED OK — task-record identity guard drops rekeyed completions; `_LINGERING_THREADS` re-owns late threads; workers are never `deleteLater`d |
| 12 | `DataManagementController` op-thread lifecycle | — | VERIFIED OK — owner-thread asserts, `_detach_active_thread` to `_LINGERING_OP_THREADS`, generation-bounded `ApplicationDataLifecycle` quiesce |
| 13 | `launch_intents` cross-process queue | — | VERIFIED OK — per-file `os.replace` atomicity, `incoming`/`done`/`failed`/`dead` taxonomy, malformed intents retired not dropped |
| 14 | `runtime_instance` / `__main__` `InstanceLock` single-instance | — | VERIFIED OK — byte-range OS lock with a non-locked metadata tail; loser's `read_metadata` reuses/close the unowned handle; daemon browser-opener dies with the process |
| 15 | `room_prediction` late-result application | — | VERIFIED OK — token/spec pop + `job_guard.can_apply` + `accept_results` hash binding; `_completion_states` deferred to `_task_thread_finished`; `dispose()` cancels + drains |

## 1 — Adaptive completions missing the disposed guard (fixed)

`_adaptive_build_completed` (`optimization_adaptive_controller.py`) and
`_adaptive_extended_build_completed` (`optimization_adaptive_extended_controller.py`)
apply their result straight into `refresh_adaptive_plans` (repository read
+ `QTreeWidget` rebuild) and `statusBar()` — and both mixins live on
`OptimizationWorkflowController`, whose `dispose()` sets `_disposed`,
shuts down `_adaptive_pool`, and closes the scene. `NativeWorkerPool` is
explicit that disconnect does not retract an already-queued emission
("already-queued emissions still require the owner's disposed guard"), so
a completion emitted just before `dispose()` was still delivered onto the
torn-down controller — the exact D5-class gap round 13 closed in every
sibling (`_extended_task_completed`, `_rew_task_completed`,
`_prediction_task_completed`, `_job_completed`, `_search_task_completed`,
`_proposal_completed`, `_evaluation_completed`, `_run_completed`,
`_on_execution_completed` all open with `if self._disposed: return`).

Fix: `if self._disposed: return` at the top of both handlers.

## 2 — Start paths racing the shut-down pool (fixed)

`dispose()` → `pool.shutdown()` flips the pool permanently; `start()` then
raises `RuntimeError('native worker pool is shut down')`. Panels that
route starts through a helper all check `_disposed` first
(`_start_rew_task`, `_start_prediction_task`, `_start_extended_task`,
`_start_search_task`, `_start_job`). Six entry points called
`self.*_pool.start` directly with no guard:

- `JointOptimizationPanel._execute_selected`
- `RobustnessAuthoringPanel._run`
- `AdaptiveControllerMixin.build_selected_adaptive_plan`
- `AdaptiveExtendedControllerMixin.build_selected_adaptive_extended_plan`
- `SystemExpansionRoomPanel._create_proposal`
- `SystemExpansionOptimizePanel._evaluate`

`dispose()` ≠ widget destruction, so a queued signal or a click on a
still-alive (but disposed) panel reached `pool.start` → the `RuntimeError`
escaped the slot into `sys.excepthook` → diagnostics + a spurious
"unexpected error" notice on a closing shell. Fix: `_disposed` guard at
the top of each, matching the helper convention.

## 3 — Batch-resolution combo mutating shared entries mid-commit (fixed)

`_commit_batch_assignment` runs `controller.commit_batch(...)` on
`_job_pool`. The worker mutates `_BatchEntry` fields (`resolution`,
`assignment`, `committed`, `committed_measurement_id`, `error`,
`attachments`) that GUI-thread code also reads (`batch_items`,
`_batch_item_view`) and writes (`set_batch_resolution`,
`set_batch_item_assignment`, `attach_to_batch_item`) — there is no lock on
`_batch`. `_set_batch_committing(True)` disabled the add/attach/clear
buttons and the assignment-save path, but the per-row **resolution
combo** inside `batch_table` stayed live: `_batch_resolution_changed` →
`controller.set_batch_resolution` → `entry.resolution = ...` while the
worker was mid-`commit_batch` reading `entry.resolution` /
`entry.duplicate_kind` to pick `reuse_existing` vs `import_as_new`. A
flip landing between the check and the commit makes the persisted outcome
nondeterministic — the row visibly shows the new resolution while the
commit took the old path.

Fix: `self.batch_table.setEnabled(not running)` inside
`_set_batch_committing` — the table is the sole un-gated mutation surface
(buttons were already covered); entries staged via still-enabled import
buttons only append to `_batch` and are not in the snapshotted commit
set, so they stay benign.

## 4 — Backup runner surfacing a queued completion post-shutdown (fixed)

`AutomaticBackupRunner.shutdown()` pools down, but a `completed`
emission already queued to the GUI thread still ran `_on_completed`,
which emitted `backup_completed` → `workflow_application`
`_on_automatic_backup_completed` → Activity Center `complete/fail` +
`self.shell.statusBar()` on the closing composition. Fix: `_closed` flag
set in `shutdown()`, gated at the top of `_on_completed` (same contract
as the panel `_disposed` guards).

## 5 — Deferred: prediction-execution progress sink ownership

`PredictionExecutionController.__init__` does
`if executor.progress_sink is None: executor.progress_sink = self._on_progress`
and never releases it. Two consequences: a second controller on the same
executor silently receives no progress (its `_submitted` tasks render
QUEUED forever), and a controller whose UI is gone keeps being invoked by
the executor's worker threads. The controller has no `close()`/lifecycle
hook today and no UI consumer is wired yet; the honest fix is an explicit
ownership/lifecycle contract (bind + release on close), not a subtle
rebind — deferred rather than guessed.

## Verification

`backend/tests/test_round17_concurrency.py` (5 tests):

- `test_disposed_start_paths_are_no_ops` — all six launch paths return on
  a `_disposed` host (pre-fix they fell through into widget/pool access);
- `test_disposed_adaptive_completions_are_no_ops` — both adaptive
  completion slots swallow a post-dispose invocation;
- `test_execute_after_dispose_does_not_raise` — real
  `JointOptimizationPanel` with a selected spec, `dispose()`, then
  `_execute_selected()` — pre-fix raised `RuntimeError` out of the slot;
- `test_batch_table_inert_while_committing` — `_set_batch_committing`
  disables the table (plus the existing button gates) for the run;
- `test_runner_drops_queued_completion_after_shutdown` — a completion
  invoked post-`shutdown()` emits nothing on `backup_completed`.

Result: 5 passed. Broader regression: the affected suites
(`test_joint_optimization_panel.py`, `test_automatic_backup_runner.py`,
`test_measurement_workflow_extensions.py`, adaptive/system-expansion
suites) run clean alongside.
