# Round 18 — cancel / abort / close-mid-operation UX

Scope: every long-running or modal operation walked down its unhappy
paths — Cancel mid-operation (does it actually stop, is partial state
rolled back or honestly labelled, does a late worker result still apply),
close-with-X / Esc / Back / project-switch during an operation (is the
operation orphaned), confirmation prompts (does "Discard" discard, does
"Keep editing" return clean), operations with no cancel affordance a user
would wait >30s on, and indeterminate progress that can never finish on
some failure path. Verification is Qt-offscreen (`QT_QPA_PLATFORM=
offscreen`) under Python 3.12 + pytest. Branch `devin/rev18-cancel`.

## The worker lifecycle contract (verified, holds)

Every mounted workspace's `before_deactivate` returns `False` while its
`NativeWorkerPool` has active workers — navigation, project switch,
dispose and app exit share the same gate, so no mounted workspace can be
torn down with a worker still writing. `NativeWorkerPool.shutdown()` is
`cancel_all()` + `requestInterruption()` + `quit()` + a bounded 1800 ms
wait; over-budget threads are detached to `_LINGERING_THREADS` and their
completions discarded. `pool.cancel(key)` sets a cooperative
`cancel_event`: a still-queued callable never runs, and a callable that
finishes anyway reports `error == WORKER_CANCELLED` instead of a success
that downstream code would have to disown. `commit_batch` (measurement
workflow) re-checks the flag before every item, returns the partial
outcome, and the UI restores `_batch_committing` and reports honest
counts of committed vs. remaining items.

Verified honest on the unhappy path (no change needed):

- **Room prediction** — `cancel()` writes a cancelled-result token; the
  late worker result is refused by the guard and the token is reported
  `WORKER_CANCELLED` ("予測を中止しました"). The panel stays busy until
  the thread actually finishes (`is_busy` covers the pool), and
  `before_deactivate` veto-holds navigation/close mid-run.
- **Joint optimization, search, adaptive plan, REW/response reads** —
  each cancel lands on `pool.cancel` or `cancel_all`, completion messages
  state what survived honestly (e.g. "評価を中止しました · 作成済みの
  仕様は保存されています", adaptive "Adaptive Plan計算を中止しました").
- **Editors** — Esc cancels preview / drag / finish-edit in room and wall
  editors and closes the command palette; `closeEvent` cancels all REW
  tokens then shuts the pool.
- **Dirty-state prompts** — `resolve_mount_dirty_state` re-checks
  `before_deactivate` after the chosen action, so "保存/破棄" that leaves
  the mount blocked still vetoes the transition, and the dialog's Cancel
  is a non-resolving no-op. `busy` maps to a resolvable=False info
  prompt; `pending_import` offers keep-draft only where the mount
  survives ('navigate').
- **Data management ops** — worker `run()` releases busy in a `finally`,
  so a failed op can never wedge the widget or leave the spinner running;
  `_assert_idle` refuses a second concurrent op; the workspace vetoes
  close while busy. Partial-write honesty for restore/relocate is carried
  by the ops themselves (staged copy + switch).
- **Automatic backup** — a `_cancel` seam sits between due-evaluation and
  `run_due`, and `WORKER_CANCELLED` completions are swallowed, so an
  exit-time shutdown cannot report a bogus backup failure.

## Findings fixed this round

1. **Run button lied while busy (room_prediction).**
   `RoomPredictionPanel._option_changed` re-enabled `run_button` on
   `option.state == 'READY'` without checking `controller.is_busy`. The
   model/receiver combos stay live during a run, so changing an option
   mid-run lit a button whose click looked like it would start a new
   prediction (the controller's busy guard actually refused it).
   Fixed: enable only when `READY and not is_busy`.
   Test: `test_run_button_stays_disabled_while_prediction_runs`.

2. **REW cancel was guard-only + misattributed (measurement_editor).**
   `cancel_rew_read` cancelled the job-guard token but never called
   `_rew_pool.cancel` — a queued read still ran, and a finishing read
   reported success that `_rew_task_completed` then discarded with
   "…revision/document/制約が変更されています", blaming a scene or
   constraint change that never happened. Fixed: `cancel_rew_read` also
   cancels the pool record, and the completion handler checks
   `is_cancelled` before the staleness check and reports
   "REW読込はキャンセルされました · 遅延結果は適用しません".
   Tests: `test_cancel_rew_read_cancels_worker_and_labels_cancel`,
   `test_cancelled_rew_worker_error_is_silent`.

3. **Project-switch close prompted "アプリケーションの終了".**
   `WorkflowShellWindow.closeEvent` hardcoded
   `resolve_dispose_all("exit")`, but `_switch_to_project` tears the
   shell down through `close()` — dirty-state prompts were titled and
   worded as app exit during a project switch. Fixed: the shell carries a
   `_deactivation_context` (default `'exit'`) that `closeEvent` forwards;
   `_switch_to_project` sets `'project_switch'` for its close attempt
   and restores `'exit'` in a finally.
   Tests: `test_project_switch_close_reports_switch_context`,
   `test_shell_close_uses_deactivation_context_for_prompts`.

4. **Dead `can_cancel` field (data_management).**
   `DataOperationProgress.can_cancel` was never set `True` and never
   read — a phantom capability. Removed; data-management ops are
   intentionally non-cancellable atomic store mutations (see below).
   Test: `test_data_operation_progress_has_no_dead_cancel_field`.

5. **Remount-while-busy hid the progress card (data_management_ui).**
   `DataManagementWidget` snapshots `controller.is_busy` at construction
   but only un-hides `progress_card` from live `busy_changed` signals —
   a widget mounted while an op runs showed disabled buttons with no
   progress card and no message. Fixed: construction ends with
   `_on_busy_changed(self._busy)`, which also handles the disabled-state
   refresh `_refresh_actions` performed.
   Test: `test_remount_while_busy_shows_progress_card`.

6. **Bundle export/import froze the UI unlabelled (workflow_application).**
   `export_project_bundle` / `import_project_bundle` run synchronously on
   the UI thread — on a large project an unmarked multi-second (past-30s
   on big data) hang invites a force-kill mid-write. Fixed as far as a
   minimal diff allows: status-bar busy message + wait cursor around the
   call, restored in `finally`; the conflict-retry leg of import gets the
   same treatment. True async (worker + cancel affordance) is deferred —
   the ops take no cancellation seam and moving them off-thread changes
   the dialog/file-pick flow.

## Deferred — documented, not silent

- **ActivityCenter `request_cancel` is wired to nothing.** Ops are only
  cancellable when submitted with `cancellability=CANCELLABLE |
  CANCEL_UNTIL_COMMIT` plus a live `cancel_callback`; no production
  submitter passes either, so `request_cancel` and the retry path's
  re-cancellation never fire. There is no UI lie (no cancel affordance
  calls it), so this is a dead capability, not a bug — flag for whoever
  wires cancel into ActivityCenter later.
- **Data-management ops are not cancellable.** `create_backup`,
  `preview_restore`, `restore`, `relocate`, `scan_storage`, `gc_storage`
  take no `is_cancelled`/`cancel_event`, so the worker has no cooperative
  seam and the progress card correctly shows no cancel button. Backup of
  a multi-GB data dir can exceed 30 s; making these cancellable means
  threading a seam through the archive/copy machinery (out of minimal-
  diff scope). Mitigations verified: the ops run off the UI thread with
  live progress text, busy is released in `finally`, and window close /
  app exit is vetoed while they run.
- **Bundle export/import async** — see finding 6.
- **Optimization controller's `_rew_task_completed`** has the same
  cancel-vs-staleness shape as finding 2, but its only cancel path is the
  dispose-time `cancel_all` — unreachable while the panel is alive and
  interactive; left as-is (minimal diffs).

## Stuck-progress audit (dimension 6)

No indeterminate spinner can outlive its operation: every worker path
audited emits `finished` in `finally` (data management `_OperationWorker`)
or reports through `NativeWorkerPool` task completion, which `shutdown`
bounds at 1800 ms. Failure paths surface a message rather than a frozen
"loading" — verified by the cancel-completion tests above and by the
existing busy-release guarantees.

## Files changed

- `backend/src/htdt/room_prediction.py` — busy-aware run-button enable.
- `backend/src/htdt/measurement_editor.py` — pool cancel + honest cancel
  message on late REW results.
- `backend/src/htdt/workflow_shell.py` — `_deactivation_context` threaded
  into `closeEvent`'s dirty-state resolution.
- `backend/src/htdt/workflow_application.py` — project-switch context +
  wait-cursor/status labelling on synchronous bundle export/import.
- `backend/src/htdt/data_management.py` — dead `can_cancel` removed.
- `backend/src/htdt/data_management_ui.py` — remount-while-busy shows the
  live progress card.
- `backend/tests/test_round18_cancel_paths.py` — 7 regression tests.
