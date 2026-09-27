# Round 8 — app lifecycle & recovery UX

Scope: startup/exit/project-switching as a product surface — splash &
progress honesty, first-run experience, startup failures, exit/dirty-state
completeness, stale state across project switches, concurrent-open safety,
crash recovery affordances, and the backup/update paths (per the round-8
brief). Prior rounds 1–7 read first (`round7-workflow` covers navigation
integrity; `round6-ux` covers the report_launch_failure modal). Branch
`devin/rev8-lifecycle`. Verification is Qt-offscreen (`QT_QPA_PLATFORM=
offscreen`) under Python 3.12.10, plus `pytest -q -n 4`.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | Recovery dialog silently dropped 3 of the 5 choices `decide_launch` offers — `verify_data`, `choose_another_project` were unreachable from the only UI that advertises them | High UX | FIXED — dialog now renders every offered choice; each choice routes to a post-launch action |
| 2 | Menu-driven project switch closed the shell *before* validating the open — a `ProjectLibraryError` left zero windows → silent process exit with no message | High UX | FIXED — open is validated first; on failure a warning shows and the current window stays |
| 3 | `AutomaticBackupScheduler` ran only from tests/CLI-adjacent code — the shipped app never triggered a periodic backup even when the policy demanded one | High data-safety | FIXED — `AutomaticBackupRunner` fires a `NativeWorkerPool` tick at launch; result surfaces in the Activity Center + status bar |
| 4 | No persisted window state — geometry, last workspace, and per-workspace context selections were lost every launch | Medium UX | FIXED — `window-state.json` saved on close, restored on next launch, skipped under Safe Mode |
| 5 | `_open_document` spawned project windows without `capture_receiver`/`preferences` — the second window silently lost the capture panel and delivery announcements | Medium UX | FIXED — dependencies propagate; the old composition's delivery binding is disconnected first |
| 6 | `explicit_safe_mode` was dead API — `decide_launch` accepted it but no CLI flag existed; a crash-looping user had to click through the dialog every launch | Medium UX | FIXED — `--safe-mode` flag; explicit flag skips the dialog entirely |
| 7 | Launch record's `last_project_ref` stayed `None` — the "open a different project" recovery affordance had no memory of what failed | Low | FIXED — `annotate_launch` back-fills the resolved project after `resolve_startup_document` |
| 8 | `quitOnLastWindowClosed` risk during close-then-open project switch | — | VERIFIED SAFE — Qt does not quit when a new window is shown in the same slot (checked offscreen, Qt 6.11.2) |
| 9 | Startup does real I/O work synchronously before the window appears — migrations check, repository open, capture init — with no splash or progress surface | Medium UX | DEFERRED — see sketch |
| 10 | Two instances can open the same data dir concurrently — SQLite serializes writes but nothing tells the second instance a session is already live | Medium UX | DEFERRED — see sketch |
| 11 | Corrupted-store recovery offers "open diagnostics / safe mode" but never "restore from backup", even when backup generations exist | Medium UX | DEFERRED — see sketch |
| 12 | Window state is global, not per-project — reopening project B inherits project A's layout | Low | DEFERRED — see sketch |

## 1 — Recovery dialog dropped most of its offered choices (fixed)

`decide_launch` already emitted a `choices` tuple — e.g.
`('open_normal', 'open_safe_mode', 'open_diagnostics', 'verify_data',
'choose_another_project')` for an unclean exit — but `_run_gui` hard-coded
three QMessageBox buttons and mapped only `open_normal` /
`open_safe_mode` / `open_diagnostics`. A user who picked the recovery
dialog saw no way to verify data integrity or to open a different project
than the one that crashed, even though the decision model computed both.

`_choose_recovery_action` now builds the dialog from
`launch_decision.choices` itself, with a `_RECOVERY_CHOICE_PRESENTATION`
table giving each choice its label/role (verify-data and
choose-another-project added as `'action'`/`'accept'` roles). Choices that
aren't launch modes become *post-launch actions*: `verify_data` opens the
Settings → data-management page after the shell shows;
`choose_another_project` lands the shell on the Projects destination so the
failed project is not re-entered. `open_diagnostics` keeps its original
behavior (info box + continue into safe mode). Tests drive the real
dialog via `QMessageBox.exec` doubles that click a button by label, so the
button↔choice mapping is exercised end to end.

## 2 — Menu project switch could exit the app silently (fixed)

`_switch_to_project` ran `_can_close_application()` → `shell.close()` →
`open_project(project_id)` → `_open_document(...)`. If `open_project`
raised `ProjectLibraryError` (entry deleted on disk, corrupted index,
permission loss), the close had already happened: the process was left
with zero windows and exited — on a Windows desktop this looks like the
app killing itself with no diagnostic.

Reordered to guards → `open_project` → `shell.close()` →
`_open_document`: the library call is now made while the current window is
still alive; on failure a `QMessageBox.warning` explains the failure and
the current project keeps running. `quitOnLastWindowClosed` was verified
not to be a hazard for the close-then-open sequence (Qt swaps windows in
the same slot without quitting — tested offscreen), so the close stays
ahead of the new window's `show()`.

## 3 — Automatic backups never ran inside the app (fixed)

`native_backup.py` has a complete `AutomaticBackupScheduler` —
`evaluate('periodic')` decides from the backup policy whether a generation
is due, `run_due('periodic')` performs an online SQLite `backup()` —
but nothing in `native_cad.py` or `workflow_application.py` ever called
it. Users with automatic backups enabled got none until they touched the
data-management page manually.

`AutomaticBackupRunner` (new, `automatic_backup_runner.py`) runs the
evaluate+run_due pair once per launch on `NativeWorkerPool`: the evaluate
pre-check keeps non-due launches silent and cheap; when a backup actually
runs the composition submits an Activity Center operation
(`operation_kind='automatic_backup'`, `OperationClass.DATA_MANAGEMENT`,
`NavigationPolicy.BACKGROUNDABLE`, title '自動バックアップ') so progress
is visible alongside every other data-management job, then completes or
fails it — failure surfaces a status-bar message instead of a silent log
line. Skipped entirely under Safe Mode (`live_integrations` is False
there); the runner's pool is shut down through a close hook. The SQLite
online backup API makes this safe against the app's open connection. An
end-to-end test on a real data dir verifies a generation file is actually
written.

## 4 — Window/state persistence (fixed)

Every launch rebuilt a default-layout shell: geometry, the workspace the
user left on, and each workspace's selected context (room/page/tab) were
all session-only. For a desktop app whose users return to the same project
day after day this is a daily-friction loss.

`window_state.py` adds a tiny `PersistedWindowState` model
(`schema_version`, base64 `saveGeometry()` blob, workspace id, context
map) stored at `<data_dir>/window-state.json`. The shell gained
`register_close_hook(hook)` — hooks run in `closeEvent` after the
dispose-all pass and before `router.shutdown()`, are skipped when a close
is vetoed by an existing close-guard, and hook failures are logged rather
than aborting the close. The composition registers one hook that writes
the state, and `_restore_window_state()` applies it at construction:
`restoreGeometry`, `seed_selected_contexts` (a new shell method that
ignores contexts for destinations that no longer exist — old schema
versions and removed pages degrade to their defaults instead of
KeyErrors), then navigates to the saved workspace. Restore is skipped in
Safe Mode — stale layout must not be able to re-break a recovering
launch — and the window title carries '— セーフモード' so the degraded
session is visibly distinct.

## 5 — Project windows spawned without live dependencies (fixed)

`_open_document` built the new `WorkflowApplicationComposition` with only
`(repository, document_id, project_library)` — the parent's
`capture_receiver` and `preferences` were dropped. The spawned window had
no capture receiver panel, no delivery-staged announcement wiring, and a
second `ApplicationPreferenceStore` — stale settings on one side,
conflicting writes on the other.

Both are now passed through. The old composition's
`delivery_staged → _announce_capture_delivery` binding is disconnected
before construction so a staged delivery isn't announced by two windows
at once.

## 6 — `--safe-mode` CLI flag (fixed)

`decide_launch` took `explicit_safe_mode` since Round-7-era wiring but
nothing ever passed it: users in a crash loop had to re-answer the
recovery dialog on every launch. `--safe-mode` now reaches the flag; when
it is set the decision short-circuits to `mode='safe_mode'` and the
dialog is skipped entirely — the flag *is* the choice.

## 7 — Launch records know which project failed (fixed)

`record_launch` writes the launch record before the project is resolved,
so `last_project_ref` was always `None` and the recovery metadata could
not say "the failed session was bound to project X". `annotate_launch(
data_dir, launch_id, project_ref=..., workspace=...)` updates the matching
record post-resolve (model_copy + rewrite, same atomic-write path). The
recovery dialog's `choose_another_project` now has a real referent, and
diagnostics listings name the project the session died in.

## 8 — Verified, not bugs

- `quitOnLastWindowClosed`: a project switch must not exit the app when
  the old window closes before the new one shows. Offscreen check
  (Qt 6.11.2): close last window → show a new window in the same slot →
  process stays alive (`'swapped', 'still-alive'`).
- Backup-while-running: `create_backup` uses SQLite's online `backup()`
  API, safe against the app's own open connection — no additional locking
  needed for the in-app tick.
- Round-6's `report_launch_failure` modal path is unchanged and still the
  terminal surface for startup failures that happen before the shell
  exists.

## Deferred (concrete integration sketches)

| Gap | Why deferred | Sketch |
|---|---|---|
| Startup has no progress surface: schema verify, migration plan, repository open, capture-receiver init all run synchronously before the window appears — on a slow/large data dir the user sees nothing for seconds. | Needs a product decision on what the surface is (QSplashScreen vs. a lightweight "starting" window) and which milestones get labels — the long poles are inside already-tested functions that would need progress callbacks threaded through. | `QSplashScreen` between `decide_launch` and `window.show()` with `showMessage` at the four existing seams (upgrade plan → repository → document resolve → integrations); each seam already exists as a distinct call in `_run_gui`, so the wiring is local. |
| Concurrent-open: two instances on one data dir have no user-visible collision handling — SQLite serializes the writes, but both shells mutate window-state.json last-writer-wins and both may run the automatic-backup tick. | A real single-instance policy (lock file + pid, or `QLockFile` + "already running" dialog offering read-only/choose-another) is a product choice about whether multi-instance is even allowed. | `QLockFile` at `<data_dir>/.htdt.lock` in `_run_gui` before `record_launch`; on `LockFailedError` show a dialog reusing `_choose_recovery_action`'s pattern with choices 'open_another' / 'quit'. The recovery-metadata `records` already track concurrent sessions — the check slots into the same read path. |
| Corrupted-store recovery: `classify_startup_failure` → `report_launch_failure` explains the failure but never offers "restore from last backup" even when `native_backup` can list generations. | Restore is destructive (overwrites the live DB); needs a confirmation flow + post-restore relaunch, i.e. a small feature, not a wiring fix. | Extend `LaunchDecision.choices` with `'restore_backup'` emitted when `failure_class` is a store class AND `AutomaticBackupScheduler.list_generations()` is non-empty; the post-launch action calls the restore entry point and quits+relaunches. |
| Window state is global: last-closed project's layout wins for the next project opened; per-project layouts would need the file keyed by `last_project_ref`. | The composition doesn't know its own project id at construction (only document_id); keying requires threading `project_ref` through `build_workflow_application` — a signature change across `native_cad`, tests, and the composition. | `annotate_launch` already gives us the project ref at launch — persist `window-state.<project_ref>.json` and fall back to the global file when absent; migrate in one release window. |
| Backup progress byte-level feedback: the Activity op shows running/failed/done but no progress bar for very large data dirs. | `create_backup` is a single SQLite call — it doesn't yield progress; real progress needs a paged copy loop. | `sqlite3.Connection.backup(target, pages=…, progress=cb)` exposes (remaining, total) — wire `cb` to `ActivityCenter.update_progress` once the backup outgrows interactive latency. |
| First-run empty state (brand-new user, no projects): lands on OVERVIEW of the default document — functional but unexplained; no guided "create your first project" affordance was found in this pass. | Product design (what the empty state *says* and where CTA buttons point) rather than a fix; Overview already renders without data. | A Projects-destination empty-state panel — "プロジェクトがありません — 最初のプロジェクトを作成" wired to the existing create flow — gated on `project_library.list()` being empty. |

## Files changed

`src/htdt/startup_recovery.py` (`annotate_launch`),
`src/htdt/window_state.py` (new — persisted shell state),
`src/htdt/automatic_backup_runner.py` (new — in-app periodic backup tick),
`src/htdt/workflow_shell.py` (`register_close_hook`, `selected_contexts`, `seed_selected_contexts`),
`src/htdt/workflow_application.py` (`safe_mode` kwarg, window-state restore/save hooks, automatic-backup wiring, `_switch_to_project` ordering, `_open_document` dependency propagation),
`src/htdt/native_cad.py` (`_choose_recovery_action` renders all choices + post-launch actions, `--safe-mode`, `annotate_launch`, capture receiver skipped under safe-mode policy, `start_automatic_backup` kickoff),
`tests/test_native_launch.py` (recovery-dialog choice coverage, safe-mode wiring, project-ref annotation),
`tests/test_startup_recovery.py` (`annotate_launch`),
`tests/test_window_state.py` (new),
`tests/test_automatic_backup_runner.py` (new),
`tests/test_project_switch_lifecycle.py` (new).

## Tests

- `tests/test_window_state.py` — 10 tests: state roundtrip, missing/corrupt
  file degradation, unknown-destination filtering, close-hook firing and
  veto interaction, context seeding, composition restore, close→persist
  cycle, Safe Mode skipping restore and the backup tick.
- `tests/test_automatic_backup_runner.py` — 4 tests: due-run completion,
  not-due silence, failure propagation, real-generation end-to-end.
- `tests/test_project_switch_lifecycle.py` — 3 tests: failed open keeps
  the window, open-then-close ordering, receiver/preferences propagation.
- `tests/test_native_launch.py` — 5 new tests: safe-mode choice skips
  integrations + marks the launch record, explicit `--safe-mode` skips
  the dialog, `verify_data` opens data management, `choose_another_project`
  lands on Projects, launch-record annotation.
- `tests/test_startup_recovery.py` — 2 new tests for `annotate_launch`.
- Full suite: `cd backend && TMPDIR=/c/t C:/devin/python/python.exe -m pytest -q -n 4`.
