# Round 12 — adversarial / out-of-order user journeys

Scope: what happens when a user does things in the "wrong" order or
interrupts mid-operation — cancel mid-operation, stale-context dialog
actions, rapid repeated actions, undo × non-command mutations, and
focus/keyboard traps. Traced in code; offscreen Qt tests reproduce the
fixed behaviors. Branch `devin/rev12-adrv`. Python 3.12.10 + pytest,
`QT_QPA_PLATFORM=offscreen`.

## Verdict table

| # | Journey | Verdict |
|---|---------|---------|
| 1 | Launch-intent pump × open modal dialogs | **BROKEN → FIXED** — the 800 ms `intent_pump` drained forwarded/file-association intents regardless of modal state. A queued `.htdtc` open during a dirty-state dialog, equipment dialog, or any `QDialog.exec()` could run `_switch_project` → `resolve_dispose_all` → `dispose_mounts` underneath the open dialog — destroying its parent workspace, nesting a second modal, or mutating `_mounts` mid-iteration. `_drain_queued_launch_intents` now defers the whole drain while `QApplication.activeModalWidget()` is set; the queue file is never consumed, so the intent routes on the next tick. |
| 2 | `WorkspaceRouter.navigate` reentrancy | **FIXED** — `navigate` → `_resolve_or_keep` → modal `exec()` ran a nested event loop in which a queued navigate (forwarded intent, deep link) could re-enter the same transition: `before_deactivate` re-checked mid-resolution, `_current_workspace_id` re-written mid-switch. A `_navigating` flag now refuses reentrant calls with "画面を切り替え中です"; navigation to the already-current workspace stays idempotent. |
| 3 | Measurement workspace busy reason | **FIXED (copy)** — `before_deactivate` blamed "REWの読み込み処理" for *any* `_job_pool` job, including a batch commit mid-flight; now operation-generic ("バックグラウンド処理が完了してから…"). |
| 4 | Cancel mid-operation (all lanes) | **VERIFIED** — batch commit: cooperative `cancel_event` + progress + honest partial-keep notice ("適用された分は残ります" semantics), `_commit_job_key` reentrancy guard. Backup/restore/relocate: no user cancel *by design* — journaled ops with rollback, `_assert_idle` reentrancy guard, mutations frozen + handles released for lifecycle modes, `can_close_application` refuses exit, close reason shown in status bar. Optimization lanes: `is_running()` start-guards + cancel buttons → `pool.cancel_all()` on every panel; workspace+controller `before_deactivate`/`dirty_state`/`dispose` cover all four controller pools (search/extended/rew/adaptive) and all three panels (joint, robustness_authoring, system_expansion_compare). Prediction: `is_busy` start-guard, `cancel()` cancels guard+pool with "遅延結果は保存・適用しません", `before_deactivate`+`dispose` wired via the room mount (workflow_application.py:2500-2523). Room system-expansion panel: `is_running` gated by RoomWorkspace.before_deactivate + `dispose` in closeEvent. |
| 5 | Stale-context actions (open dialog → data changes → OK) | **VERIFIED** — the only asynchronous mutators inside the app are the intent pump (now modal-gated, finding 1) and worker completions, which write their own domain stores and never mutate scene documents a dialog is editing. Restore preview → confirm re-validates the archive (`RestorePreviewStaleError` → honest re-preview). `choose_snapshot_action` asks save-vs-last-saved explicitly instead of guessing. Launch intents carry stale-age expiry (round 11). Remaining holes deferred (see below). |
| 6 | Rapid repeated actions | **VERIFIED** — every worker-start path guards synchronously before `pool.start` (`is_busy`/`is_running`/`_commit_job_key`/button-disable), and Qt serializes slots on the UI thread, so a double-click cannot interleave between check and start. Inbox defer/reject/resume/assign are synchronous repository writes with disposition preconditions — a second click raises and maps through `warn_user`. `_switch_project`'s dirty-state dialog is modal, so a second project-open click can't reach the library row. |
| 7 | Undo/redo × non-command mutations | **VERIFIED w/ deferral** — every `WorkingDocument` mutation routes through `_history.push` (move/rotate/add/delete/update/duplicate/batch/transform/replace/preview-commit/`push_command`/`apply_entity_set_edit`); undo/redo are preview-gated and `edit_idle`-gated in the registry. Sidecar-authority edits still bypass undo — see Deferred. Hide/lock are per-session `view_state`, not document state — correctly outside undo. |
| 8 | Focus/keyboard traps | **VERIFIED** — `_choose` (dirty-state dialog) maps Escape to cancel → outer navigation/exit stays blocked with the original reason; the `busy` state is an unresolvable info dialog, not a trap. App close during restore is refused by `can_close_application` + close-guard status message. DataManagementDialog's `closeEvent` consults `before_deactivate`; Escape/`reject()` hides it without closeEvent, but hiding is safe — the operation continues and reopening reflects `_active`. |

## Finding 1 — the pump vs. the modal (HIGH)

`_run_gui` runs an unparented 800 ms `QTimer` that drains the
single-instance forward queue and dispatches each intent through
`_route_launch_intent` → `_switch_project` / `navigate_to_target` /
`settings_dialog.open_settings`. Qt keeps delivering timer events inside a
nested `exec()` loop, so the pump fired while *any* modal was up:

- User navigating away answers the dirty-state dialog → a forwarded
  file-open drains → `_switch_project` → *nested* `resolve_dispose_all`
  over the same mounts → inner `dispose_mounts` clears `_mounts` while the
  outer call iterates it.
- An `QMessageBox.information`/`warning` from the routed outcome stacked
  over the dialog the user was answering.
- Any `QDialog.exec()` (SeatingLayout, EquipmentLibrary, Deliverables,
  wizard) could lose its parent workspace mid-edit; a later `accept()`
  would commit through stale controllers.

The drain body is now `_drain_queued_launch_intents(data_dir, dispatch)`
(module-level, still `_self`-resolved for test patching). It returns 0 —
without touching the queue — whenever `QApplication.activeModalWidget()`
is not None, so an in-flight modal is never surprised by a project switch,
a disposal, or a stacked dialog; the same intent routes on the next tick.
Initial launch-time intents still dispatch immediately (no modal can exist
at t=0).

## Finding 2 — reentrant navigate (MEDIUM)

`WorkspaceRouter.navigate` was unguarded: anything delivered inside its
own nested modal (`_resolve_or_keep`'s dirty-state `exec()`) could call
`navigate` again and interleave with the in-flight transition. Finding 1
removes the only asynchronous caller, but the contract itself is now
enforced: `_navigating` refuses reentry with an honest reason, reset in
`finally` so an exception mid-transition never wedges the router.

## Finding 3 — busy reason named the wrong job (LOW)

`MeasurementPageWorkspace.before_deactivate` returned
"REWの読み込み処理が完了してから…" for any `_job_pool.active_count` —
including a batch commit or analysis job. During commit-cancel the user
was told to wait for a REW import that never ran. The reason is now
operation-generic; `dirty_state` already classified these as `busy`
correctly.

## Deferred

- **Native dialogs escape the modal gate.** `QFileDialog.getOpenFileName`
  etc. don't register a Qt modal widget, so a forwarded intent can still
  fire while a native picker is open; the picker itself survives (native
  window) but its completion handler can land on a disposed workspace.
  Closing this needs per-opener busy tracking, not a global check —
  low likelihood (second-instance forward timed inside a native dialog).
- **Sub-dialogs die silently on project dispose.** Non-mounted dialogs
  (e.g. `SeatingLayoutDialog` inside the room workspace) are destroyed
  with their parent on `_switch_project`/restore dispose — pending input
  is lost without a dialog-specific notice. The dirty-state resolution
  covers the *document*, not open sub-dialogs; honest but abrupt.
- **Sidecar-authority edits bypass undo** (constraints, materials, poses,
  transfers, video workspace, installed/proposed placements, variants,
  equipment definitions). By design per round 8: `CompositeEditCommand`
  couples atomic scene+sidecar steps where atomicity matters (#843), and
  a parallel undo stack is a documented UX hazard; sidecars do join the
  dirty baseline (#915), so unsaved sidecar work still blocks navigation.

## Files changed

- `src/htdt/native_cad.py` — `_drain_queued_launch_intents` extracted to
  module level with the `activeModalWidget` gate; `_drain` closure calls it.
- `src/htdt/workflow_shell.py` — `WorkspaceRouter._navigating` reentrancy
  guard in `navigate`.
- `src/htdt/measurement_page_workspace.py` — operation-generic busy reason.
- `tests/test_round12_adversarial.py` — 4 regression tests.

## Tests

- `test_launch_intent_drain_dispatches_and_completes_each_queued_intent` —
  drain dispatches every queued intent and retires each file only after
  its outcome.
- `test_launch_intent_drain_defers_while_a_modal_dialog_is_open` — with an
  application-modal dialog open, drain returns 0 without touching the
  queue; after the dialog closes the same intent dispatches.
- `test_router_navigate_refuses_reentrant_navigation` — a mount's
  `before_deactivate` reentering `navigate` is refused with a reason; the
  outer transition completes.
- `test_measurement_busy_reason_is_operation_generic` — the busy block
  reason no longer names REW for a generic pool job.
