# Round 13 — background worker & job lifecycle truth

Scope: every mechanism that moves work off the GUI thread — `NativeWorker`/`NativeWorkerPool` (QThread,
move-to-thread), `DataManagementController._OperationWorker` (custom QThread), `BoundedR140Executor`
(`ThreadPoolExecutor`), `CaptureReceiverService` (daemon `threading.Thread`), subprocess launches, and
the `ActivityCenter` operation registry that is supposed to account for all of them at exit. For each:
who owns it, how progress is reported, how completion is delivered, what kills it, and what happens on
app close / project switch / document dispose with a worker mid-flight. Verification is Qt-offscreen
(`QT_QPA_PLATFORM=offscreen`) under Python 3.12.10 plus pytest. Branch `devin/rev13-worker`.

## The lifecycle contract (as verified)

The app has one uniform contract, implemented in `workflow_shell.py` + `native_worker.py`:

1. Every mounted workspace's `before_deactivate` returns `False` while its pool has
   `active_count > 0`. This one check gates navigation, project switch
   (`resolve_dispose_all("project_switch")`), document dispose (`"dispose"`), and app exit
   (`"exit"` context inside `closeEvent`) identically — a running worker vetoes all of them.
2. When guards pass, `dispose_mounts`/`closeEvent` run the owning `pool.shutdown()`:
   `cancel_all()` (cooperative `cancel_event` flags) + `requestInterruption()` + `quit()` +
   a single bounded wait (`DEFAULT_WORKER_SHUTDOWN_TIMEOUT_MS = 1800 ms`, shared across the
   pool's tasks).
3. A worker that outlives the budget is `_detach()`ed: the `(QThread, NativeWorker)` pair is
   pinned into module-scoped `_LINGERING_THREADS`, `setParent(None)`, and released when
   `finished` finally fires. Workers are never `deleteLater`d — deliberate, to avoid the
   PySide6/Windows teardown access-violation race. A `destroyed` fallback (`_detach_all`)
   covers owner deletion mid-flight.
4. Completion delivery is a queued `completed(key, result, error)` emission guarded by
   `record[0] is thread` at the receiver, so a stale `finished` from a detached thread can
   never deliver into a reused key or a new task. `WORKER_CANCELLED` is reported when the
   cooperative flag was set; handlers on the owner side drop it.

Verified properties of this contract (pre-existing tests in `test_native_worker.py` confirm):
detach-on-budget-exceeded, cooperative stop, logical cancel + record release, late completion
never reaching the owner, post-shutdown start rejected, duplicate-key detach, 120-cycle stress,
dispose-detach for prediction/REW/measurement workspaces.

## Worker → lifecycle → verdict

| Worker / mechanism | Owner & progress | Completion path | Kill path | Verdict |
|---|---|---|---|---|
| `MeasurementPageWorkspace._job_pool` (REW list, REW snapshot read, batch commit) | Pool per workspace; notice text + `batch_commit_progress(done,total)` | `_job_completed` → per-key handler; commit key clears `_commit_job_key` | `closeEvent` → `pool.shutdown()`; batch commit accepts the pool's `cancel_event` cooperatively | **Fixed**: REW list/read lacked any latest-wins guard — concurrent submissions applied in finish order, so a slow first read could stage an import behind a newer selection. Added purpose-keyed staleness discard (mirrors `measurement_editor._latest_rew_list_key`). |
| `MeasurementEditor._rew_pool` (REW list/read) | Status-bar line; `list:` sequence keys | `_rew_task_completed` → latest-key check + `MeasurementJobGuard.can_apply` (revision/content/constraint hashes) | `closeEvent` → token cancels + `pool.shutdown()` | Honest — the guard convention the fix above mirrors. |
| `OptimizationWorkflowController` pools (`_search_pool`, `_extended_pool`, `_adaptive_pool`, `_rew_pool`) | `statusChanged`/`rewBusyChanged`; REW list keys | Guarded completions (`rew_job_guard`, stale-spec discard) + `_disposed` checks | `dispose()` → 4 pool shutdowns + JP lingering warning + `scene.close()` | Honest — cooperative `cancelled=cancel_event.is_set` on generation jobs. |
| `RoomPredictionController._pool` | `is_busy` = job id or active count; second run rejected | Guarded via `PredictionJobGuard` (revision/content hash) | `dispose()` → token cancels + `pool.shutdown()` + JP lingering warning | Honest — duplicate run honestly refused, not queued. |
| `JointOptimizationPanel._pool` | `is_running` → active count | `_on_execution_completed` (no `_disposed` guard — see D5) | `dispose()` → shutdown + warning | Mostly honest — see D5 residual. |
| `SystemExpansionWidgets` proposal/evaluation pools | Fixed keys (`create_topology_proposal`, `evaluate_proposals`) | `_proposal_completed`/`_evaluation_completed` (no `_disposed` guard — D5) | `dispose()` → `pool.shutdown()` | Mostly honest — see D5 residual. |
| `OptimizationWorkspace`/`PredictionWorkspace`/`RobustnessAuthoringPanel` (legacy windows) | Status bar | `_disposed` guards on completion (prediction/optimization windows) | `closeEvent`/`dispose` → shutdown + JP warning | Honest — same drain contract. |
| `AutomaticBackupRunner._pool` (`automatic-backup.periodic`) | ActivityCenter op + status message | `backup_started`/`backup_completed` queued emissions | `runner.shutdown()` in close hooks; evaluate→run seam checks `cancel_event` | **Fixed (×2)**: (a) `run_due` re-evaluating as not-due returned `result=None` and the RUNNING op never reached terminal — now completes with an honest summary. (b) Ops still active at exit were never accounted — `prepare_shutdown` existed but was never called; now wired as a close hook + `persist_history`. |
| `DataManagementController._OperationWorker` (backup/restore/relocate/scan/gc) | `busy_changed`, `operation_failed`/`*_completed` signals, phase emissions | `_thread_finished` → `_complete_success`/`_complete_failure` (always terminal; lifecycle finish/resume/restart on both paths) | `_assert_idle` blocks a second op; close guard via `can_close_application`; `_detach_active_thread` on controller death | Honest — but `_job(emit)` has no cancel seam (D2). |
| `BoundedR140Executor` (`ThreadPoolExecutor`, `htdt-r140` prefix) | Per-task `ExecutionCancellationToken` + `Future` map; blocking `run_schedule` loop | Futures; queued-cancel recording | `close()` → `tokens.request()` + `shutdown(wait=True, cancel_futures=True)` | Honest — script/test only, not UI-wired. |
| `CaptureReceiverService` thread (`capture-receiver`, daemon) | `delivery_staged` signal | `stop()` → `server.shutdown()` + `server_close()` + `join(5)` | `CaptureReceiverController.shutdown()` post-`app.exec()` in `native_cad.py` | Honest — bounded join + daemon fallback; openssl subprocess `timeout=30`. |
| `RewApiClient` I/O inside pool jobs | — | `urlopen(timeout=1.5)`, localhost-only validation | Bounded per-request; no mid-fetch abort | Honest — bounded blocking I/O only. |
| Subprocesses | — | `build_info` git calls `timeout=10`; `capture_receiver` openssl `timeout=30` | — | Honest — except `acoustic_pffdtd_adapter` git `check_output` (no timeout, non-UI path — D4). |
| `ActivityCenter` registry | Terminal transitions archive to `_history` (limit 200); `persist_history` on every terminal event | `prepare_shutdown` report + `active_operations` persistence | `request_cancel` only for declared-cancellable pre-commit-point ops | **Fixed**: `prepare_shutdown` was dead code — now a registered close hook, so ops that outlive their drain are recorded instead of vanishing. |
| `__main__.py` browser-open daemon thread | — | one-shot | daemon, dies with process | Trivial — fine. |

## Shutdown-drain enumeration

| Path with a worker mid-flight | Behavior | Verdict |
|---|---|---|
| App close, guarded workspace busy | `closeEvent` → `resolve_dispose_all("exit")` → `before_deactivate` False → `event.ignore()` + status line | Honest block — the app refuses to close rather than orphan the worker. |
| App close, `AutomaticBackupRunner` busy | Close is **not** vetoed (runner has no mount); close hook `shutdown()` cancels + drains ≤1.8 s, lingers detached; op now accounted via `prepare_shutdown` + `persist_history` (fix 2b) | Honest drain + now-honest accounting. |
| Project switch / document dispose, workspace busy | Same `before_deactivate` veto via `resolve_dispose_all` context — switch/dispose refused with a reason string | Honest block. |
| Workspace `closeEvent` reached with busy pool (unmounted/legacy window) | `pool.shutdown()` bounded drain → lingering detach + JP warning "遅延結果は適用しません" | Honest bounded drain; detached worker can never deliver (queued slot disconnected + stale-finished guard). |
| `DataManagementController` death mid-op | `_detach_active_thread` re-owns pair at module scope; result delivery dropped deliberately | Honest detach; latent because close guard blocks first — and any still-registered op is now recorded at exit. |
| Worker that **never finishes** | `before_deactivate` vetoes forever — navigation, project switch, and exit all deadlock | See D1 — no watchdog; the veto is honest but has no escalation. |

## Cancel semantics (who is *actually* cancellable)

| Path | Cooperative? | Cancel → UI truth |
|---|---|---|
| `commit_batch` (`_commit_job_key`) | Yes — `cancel_event` threaded into `controller.commit_batch` | Emits `WORKER_CANCELLED` → "保存をキャンセルしました。保存済みの項目はそのまま残っています。" — partial commits kept and stated. Correct. |
| Search/extended/adaptive generation (`generate_cad_candidates` etc.) | Yes — `cancelled=cancel_event.is_set` | Job-guard marks cancelled; late results rejected by `can_apply`. Correct. |
| Prediction/`PredictionExecutionController` (R140 executor) | Yes — `ExecutionCancellationToken` per task | `progress_view` shows CANCELLING → CANCELLED via `can_apply`. Correct. |
| REW list/read (all 3 consumers) | Flag set but callable ignores it — bounded by 1.5 s `urlopen` per request | Completion reports `WORKER_CANCELLED`; handlers drop it. Cancel is logical-only; honest enough because I/O is bounded. |
| Generic `_start_job` callables | **No** — `lambda _cancel_event: call()` discards the event | Completion marks `WORKER_CANCELLED` but the callable can't see it — logical-only (D3). |
| Data-management ops (`_OperationWorker`) | **No** — `_job(emit)` signature has no cancel channel at all | UI correctly offers no cancel affordance; busy is stated via `busy_changed`/`can_close_application` (D2). |
| Automatic backup run | Partial — `evaluate→run` seam checks the flag once; `run_due` itself ignores it | Op now completed-as-not-due or recorded as active-at-exit (fixes). |

## Progress truth

- `OperationProgress` kinds are INDETERMINATE/DETERMINATE/STAGE/BYTES/ITEMS — no fabricated
  percentage anywhere (registry design + comment "never a fabricated percentage").
- Batch commit reports `done/total` per persisted item — real progress.
- REW jobs are INDETERMINATE (bounded I/O, unknown duration) — honest.
- No progress was found to be capped, monotonic-jumped, or invented. ✓

## Queue behavior

- Same-key resubmission on `NativeWorkerPool.start` detaches the predecessor — the old completion
  can never deliver into the new key (stale-finished guard). Deliberate, tested.
- `DataManagementController._assert_idle` raises `DataManagementBusyError` on a second submit —
  honest rejection, not silent drop or interleave.
- `RoomPredictionController.start` returns `False` while busy — honest rejection.
- `PredictionJobGuard`/`MeasurementJobGuard` per-operation latest tracking + `can_apply` hash checks
  — stale results are rejected, never interleaved.
- **Fixed**: `MeasurementPageWorkspace` REW list/read had none of these guards — submissions were
  not serialized, rejected, or staleness-checked; completions interleaved by finish order.

## Zombie detection

- `lingering_thread_count()` enumerates module-detached workers; existing tests assert the count
  returns to baseline after threads finish. Detached workers are pinned until `finished` — a
  deliberate bounded "leak" (worker may outlive owner, never outlives process).
- `_LINGERING_OP_THREADS` mirrors the same contract for the data-management worker.
- `QThreadPool` is not used anywhere (`threading.enumerate` leaks limited to the daemon capture
  receiver, which `stop()` joins with a 5 s bound).

## Fixes this round

1. **`workflow_application._on_automatic_backup_completed`** — `result=None` (runner re-evaluated
   as not-due after `backup_started`) left the submitted op RUNNING forever; it now completes with
   `バックアップは不要と再評価されました`.
2. **`workflow_application._account_for_exit_operations`** (new close hook) — `ActivityCenter.
   prepare_shutdown()` was dead code; it is now called during close-hook teardown and, when
   operations are still active (e.g. a cancelled-undelivered backup), `persist_history` records
   them under `active_operations` for next-launch recovery diagnostics. The op is reported — never
   fabricated terminal.
3. **`MeasurementPageWorkspace` job staleness** — `_start_job` accepts `purpose=`;
   `_job_completed` discards a completion whose key is no longer the latest for its purpose
   (REW list/read share the pool and their buttons never disable). Mirrors the
   `measurement_editor`/`optimization_workflow_controller` latest-key convention.

Tests: `tests/test_round13_worker_lifecycle.py` — superseded-purpose completion discarded,
independent purposes both apply, exit accounting persists `active_operations`, idle exit writes
nothing. All four failed before the fixes (missing API/behavior) and pass after.

## Deferred / residual (documented, not fixed)

- **D1 — wedged worker = permanent veto.** A worker that never finishes deadlocks navigation,
  project switch, and exit forever (`before_deactivate` blocks before `pool.shutdown` is ever
  reachable). This is a deliberate design ("navigation is refused while a worker runs"), and every
  reachable job today is either cooperative or bounded-I/O — but there is no watchdog and no
  UI-level "cancel and leave" escalation for jobs that ignore their flag. Fix would be a UI
  contract change, not a small diff.
- **D2 — data-management ops are uncancellable.** `_OperationWorker._job(emit)` has no cancel
  seam; adding one means plumbing a token through `native_backup`/relocate internals. Honest
  because the UI never offers a cancel and the close guard refuses exit — but a hung copy wedges
  the session (D1 applies).
- **D3 — generic `_start_job` jobs ignore `cancel_event`.** `lambda _cancel_event: call()` drops
  the flag; `pool.cancel` marks the completion cancelled but cannot shorten the run. Bounded in
  practice by REW 1.5 s request timeouts.
- **D4 — `acoustic_pffdtd_adapter` git `check_output` has no timeout.** Script/test-side helper,
  not UI-thread or pool-job code.
- **D5 — post-dispose queued completions in `joint_optimization_panel`,
  `system_expansion_widgets`, `robustness_authoring_panel`.** Their `_on_*_completed` handlers lack
  the `_disposed` guard siblings have; a completion already queued at `dispose()` can still run.
  Persistence happens inside the worker and repository writes sit under try/except, so the residue
  is bounded — but the guard would be the consistent contract.
- **D6 — workers are never `deleteLater`d** (deliberate PySide6/Windows teardown-race avoidance);
  detached workers stay in `_LINGERING_THREADS` until their thread finishes — a designed, bounded
  "leak", visible via `lingering_thread_count()`.
