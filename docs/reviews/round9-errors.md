# Round 9 — error & failure journey

Scope: what the user experiences when things go wrong — cancel paths and
progress honesty on long-running operations, per-item failure reporting in
batch operations, Qt main-loop safety (uncaught slot/worker exceptions),
startup/corruption paths beyond round-8 lifecycle, network-ish surfaces
(capture delivery, handoff writes, disk-full), and undo/redo under partial
failure. Prior rounds were read first (`round6-ux`/`round7-*` swept raw
error strings into the `user_facing_error` contract — not re-audited,
only regressions fixed; `round8-lifecycle` owns corrupted-store recovery —
not re-reported; `round9-report` owns export atomicity — not re-reported).
Branch `devin/rev9-errors`. Verification is Qt-offscreen
(`QT_QPA_PLATFORM=offscreen`) under Python 3.12.10 plus pytest.

## Coverage map — surface → how it was exercised

| Surface | Path | Result |
|---|---|---|
| Undo/redo under failing commands | `CommandHistory.push/undo/redo` driven with raising commands | was inconsistent → FIXED |
| Command dispatch failures (palette / shortcut / edit-menu) | `CommandRegistry.execute` raising executor | vanished to excepthook → FIXED |
| Uncaught slot exceptions | `sys.excepthook` probe under offscreen Qt 6.11.2 | logged but invisible → FIXED |
| Worker-task failures | `NativeWorker.completed` error payload | `str(exc)` erased type → FIXED |
| Batch staging/commit/dataset failures | `measurement_workflow` driven with broken repo | `str(exc)` leak → FIXED |
| Import/export write failures | `workflow_application` bundle import + three export actions | unguarded raise → FIXED |
| Automatic-backup failure summary | `workflow_application` backup tick | raw summary → FIXED |
| Status-bar error embeds | room_workspace evaluate, optimization extended/search, room_prediction, diagnostics page | `str(exc)`/`{exc}` leaks → FIXED |
| Data-dir unavailable at launch | `native_cad.main` with `assert_managed_root_available` raising OSError | GUI exit silent → FIXED |
| REW transport unavailable | `RewApiUnavailable` through `operation_error_message` | mislabeled → FIXED |
| Disk-full during write | ENOSPC `OSError` through `operation_error_message` | generic io message → FIXED |
| Cancel semantics | `NativeWorkerPool` Event + bounded shutdown, lingering threads | VERIFIED honest |
| Progress honesty | `ActivityCenter` ProgressKind + data-management mirroring | VERIFIED honest |
| Launch intents | malformed `.htdtproject` descriptor → dead-letter queue | VERIFIED honest |
| Capture delivery | `capture_receiver.handle_delivery` hash/length/idempotency | VERIFIED honest |

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | `CommandHistory` mutated `_commands`/`_index` **before** `apply`/`revert`: a command that raised left the failed command recorded, truncated the redo tail, or consumed an undo step for work that never happened — the stack then reverted the wrong documents | High data-integrity | FIXED — apply/revert precede all index mutation |
| 2 | `NativeWorker` emitted `str(exc)` as the error payload: every consumer lost the exception class across the thread boundary, so `operation_error_message` could only fall to the generic catch-all — typed failures (permission, ingress, REW) were indistinguishable | Med UX | FIXED — payload is now the exception object; every consumer verified compatible |
| 3 | Uncaught slot exceptions were log-only: `install_exception_hooks` wrote `htdt-native.log` but the operator saw nothing — the failure was invisible unless the user went looking for diagnostics | High UX | FIXED — excepthook now posts a non-modal status-bar notice naming the log path |
| 4 | `CommandRegistry.execute` let executor exceptions propagate: the one dispatch point covering palette, QShortcut, and edit-menu commands had no failure surface — in GUI builds the exception died in the excepthook with no message | Med UX | FIXED — `set_error_handler` wired to `warn_user` in the composition |
| 5 | Permission-denied / non-`ManagedDataUnavailableError` failures resolving the data dir bypassed the failure path entirely: GUI mode exited 1 with only stderr output — invisible for a packaged desktop launch | High startup | FIXED — GUI mode routes to `report_launch_failure`; maintenance modes keep stderr+exit 1 |
| 6 | `RewApiUnavailable` (REW not running / API off) mapped to the generic `rew.api` "データを取得できませんでした" — told the user data fetch failed, not that the connection was down; no recovery hint | Low | FIXED — dedicated `rew.unavailable` message + recovery; ordering verified against `RewApiError` base |
| 7 | ENOSPC (disk full during export/handoff write) fell into generic `io.error` "ファイルにアクセスできませんでした" — wrong recovery advice for an exhaustible-resource failure | Low | FIXED — dedicated `io.no_space` message + recovery |
| 8 | Batch measurement failures leaked `str(exc)` into per-item error fields (`dataset_error`, stage/commit `entry.error`) — raw English exception text reached the batch report the user reads | Med honesty | FIXED — `operation_error_message`; failures also logged at warning |
| 9 | Bundle import + catalog/handoff/analysis export write calls in `workflow_application` were unguarded or re-raised raw — a write failure (locked file, permission, ENOSPC) vanished into the excepthook with no user message | Med UX | FIXED — all wrapped to `warn_user`; retry path covered |
| 10 | Status-bar and label embeds of raw `{exc}` across `room_workspace` (evaluate), `optimization_extended_controller` (5 sites), `optimization_search_controller`, `room_prediction` (5 sites), `application_pages` (diagnostics export) | Low honesty | FIXED — all routed through `operation_error_message` |
| 11 | Automatic-backup tick failure surfaced `error_summary` unmapped | Low | FIXED — `operation_error_message` |
| 12 | Compute operations (solve/optimization) are not on the Activity Center model — cancel/progress for those is per-panel, not unified | — | already deferred round-7 — not re-reported; verified the deferral still stands |
| 13 | Uncaught-exception notice is a transient 10 s status-bar message — no persistent place to revisit "what just failed" | — | DEFERRED — see sketch |

## 1 — Undo stack corrupted by failing commands (fixed)

`push` ran `self._commands = self._commands[: self._index];
self._commands.append(command); self._index += 1` and *then* returned
`command.apply(document)` — a raising command still recorded itself and
truncated the redo tail. `undo`/`redo` likewise decremented/incremented
`_index` around a `revert`/`apply` that could raise, so a failed revert
consumed the step: the next undo reverted a command whose apply never
happened. Document and history diverged silently — the worst kind of
partial-failure state because every subsequent undo/redo operates on a
shifted history.

All three methods now call `apply`/`revert` **before** touching
`_commands` or `_index`: an exception leaves the history object
byte-identical to its pre-call state. Success-path semantics unchanged
(noop commands still skip recording; redo-tail truncation still happens —
just only after the apply succeeded). Regression tests drive duck-typed
commands that raise on apply and on revert: after failure, `can_undo`/
`can_redo`, stack contents, and the document are all unchanged.

## 2 — Worker error payload erased the exception type (fixed)

`NativeWorker._run` emitted `self.completed.emit(key, None, str(exc))`.
Every downstream consumer then saw a plain string: `operation_error_message`
maps by exception *class name* (`_NAME_PATTERNS`/`_SUFFIX_PATTERNS`), so a
`PermissionError` from a worker arrived as an arbitrary English sentence
and fell through to the generic "operation failed" — or, worse, the raw
string was embedded straight into a label.

The payload is now the exception object itself. Consumers audited: the
`error == WORKER_CANCELLED` sentinel comparisons stay correct (an
exception is never the sentinel), and all surfaced sites were converted to
`operation_error_message` in this round anyway, so they now get typed
mapping for free. A regression test raises `PermissionError` inside a
worker and asserts the emitted payload is the exception and maps to
`io.permission`.

## 3 — Uncaught exceptions invisible to the operator (fixed)

PySide6 6.11.2 routes exceptions escaping a slot to `sys.excepthook`
(verified empirically offscreen — a slot that raises lands in the hook).
`install_exception_hooks` logged them to `diagnostics/htdt-native.log`
and chained to the previous hook — but produced *no user-visible signal*.
A slot bug manifested as "nothing happened" or a half-updated view.

`_sys_hook` now also calls `_surface_uncaught_on_statusbar`, which — only
on the GUI thread with a live QApplication — finds the active window (or
first visible top-level with a `statusBar`) and posts
`予期しないエラーが発生しました（詳細: <log path>）` for 10 s. Every step
is exception-safe: worker-thread exceptions no-op (they already surface
through the `completed` channel — thread check verified by test), missing
windows no-op. Log-only was judged the *right* failure surface for
non-GUI contexts; the status bar is the honest minimum for the GUI one.

## 4 — Command dispatch had no failure surface (fixed)

`CommandRegistry.execute` is the single dispatch point for the palette
(`command_palette.py`), QShortcut lambdas, and edit-menu actions
(`workflow_application.py`) — and it called `command.execute()` bare.
Any command raising propagated through a Qt slot to the (previously
invisible, now status-bar) excepthook: no dialog, no message naming the
failed command.

`CommandRegistry.set_error_handler(handler)` installs one failure
surface; `execute` catches executor exceptions, calls
`handler(command.definition, exc)`, and still returns True (the dispatch
succeeded — reporting is the handler's job). With no handler installed,
exceptions propagate exactly as before, so the registry stays honest for
non-GUI callers/tests that want the raise. The composition wires it to
`warn_user(self.shell, '「<command display name>」', exc)` — the user
gets the localized dialog naming *which* command failed. Menu
`addAction` items that bypass the registry (`_export_project_bundle`
etc.) got their own `warn_user` guards — see #9.

## 5 — Permission-denied data dir exited the GUI silently (fixed)

`main` caught only `ManagedDataUnavailableError` around
`resolve_data_dir` + `assert_managed_root_available`. The resolution can
equally raise raw `OSError` (permission denied reading the bootstrap
config, relocation-journal I/O): GUI mode then hit `return 1` after a
`print` to stderr — a packaged desktop launch shows the user *nothing*;
the app simply doesn't start.

The catch is now `except Exception` and the block moved after launch-mode
resolution: maintenance modes (`--backup`, `--restore`, `--verify`,
intent replay) keep the stderr print + `return 1` (correct for CLI), but
GUI mode calls `report_launch_failure` — which creates its own
QApplication and shows a real dialog ("データディレクトリを開けません",
concise reason, recovery pointing at `--data-dir`). Tests monkeypatch
`assert_managed_root_available` to raise `PermissionError` and assert the
GUI path calls `report_launch_failure` while the maintenance path prints
and returns 1.

## 6–11 — Mapping and leak fixes (fixed)

- `RewApiUnavailable` precedes `RewApiError` in `_NAME_PATTERNS` (lookup
  is MRO-membership based) → `rew.unavailable` "REWに接続できませんでした"
  + "REWが起動していてAPIが有効か確認してください" instead of the
  data-fetch message. Verified by a test asserting the specific mapping
  wins over the base class.
- ENOSPC checked before generic `OSError` → `io.no_space`
  "ディスク容量が不足しています" + recovery — disk-full during an export
  or handoff write now tells the user what actually happened.
- `measurement_workflow`: `dataset_error`, both batch-staging `error`
  fields, and both `commit_batch` `entry.error` fields now carry
  `operation_error_message(exc)` — the per-item report a user reads after
  a partial batch failure is localized and class-mapped instead of raw
  English exception text; each site also logs at warning so the technical
  detail is not lost.
- `workflow_application`: `_import_project_bundle` (retry + outer),
  `_export_capture_equipment_catalog`, `_export_installation_handoff`,
  `_export_analysis_bundle` write calls all route failures to
  `warn_user`; the automatic-backup tick's `error_summary` is mapped.
- `room_workspace.evaluate_error`, five
  `optimization_extended_controller` status-bar embeds (incl. the
  Extended-candidate worker-error banner), `optimization_search_controller`
  preset-add, five `room_prediction` embeds (start ×2, reject, save,
  worker-error banner), and `application_pages` diagnostics-export label
  all use the mapped message.

## Verified, not bugs

- **Cancel honesty**: `NativeWorkerPool` cancel is cooperative — the
  `cancel_event` is checked inside the worker loop and post-work, the
  pool does a bounded physical shutdown, and non-cooperative threads are
  detached to `_LINGERING_THREADS` instead of hanging the app or lying
  about having stopped. `WORKER_CANCELLED` is a distinct payload, not an
  exception. The honest limitation (a thread that never checks its event
  keeps running in the background) is inherent to Python threads and is
  now documented in the worker itself.
- **Progress honesty**: `ActivityCenter` carries an explicit
  `ProgressKind` (INDETERMINATE/DETERMINATE/…) and data-management ops
  mirror worker progress as indeterminate + stage label — the spinner
  never pretends to be a percentage. Compute-op integration remains the
  round-7 deferral.
- **Batch partial failure**: `data_management` reports per-item
  `DataOperationFailure` with `message_ja` (localized) + `detail`
  (technical) — one bad row does not poison the report; after this round
  `measurement_workflow` batch items meet the same standard.
- **Launch intents**: malformed `.htdtproject` descriptors get a
  localized detail message and route to `dead/`/`done/`/`failed/`
  dead-letter dirs; `_route_launch_intent` maps every outcome to honest
  QMessageBox copy and catches dispatch exceptions.
- **Capture delivery**: `handle_delivery` validates hash/length,
  idempotent re-delivery, and name conflicts; listener exceptions are
  logged, not fatal.
- **REW taxonomy**: `RewApiUnavailable`/`RewApiResponseTooLarge` are
  distinct classes with a 1.5 s default timeout — the failure modes the
  UX needs to distinguish actually exist upstream (and now map correctly,
  #6).
- **Pre-QApplication safety**: `report_launch_failure` creates its own
  QApplication and falls back to stderr — safe for the new data-dir
  catch, which runs before the app exists; `configure_diagnostics`
  degrades to `log_path=None` on an unwritable dir.
- **Export partial writes**: `round9-report` already moved the writers
  to staged/atomic paths; the remaining gap was the *message* when the
  write still fails — covered by #7/#9.

## Deferred (concrete sketches)

| Gap | Why deferred | Sketch |
|---|---|---|
| Uncaught-exception notice is transient: a 10 s status-bar line with no way to revisit "what failed" after it fades; a crash-looping slot produces a message stream but no list | Choosing the persistent surface (modal per crash vs. an Activity Center entry vs. a dedicated error inbox) is a product decision — a modal-per-exception is actively hostile in a crash loop | Post each uncaught exception to the Activity Center as a `failed` pseudo-operation carrying the log path and `concise_reason(exc)` — reuses the existing persistent surface, no new UI concept, and the status-bar notice keeps its "look here" role |
| Cooperative cancel is only as strong as each op's `cancel_event` checks — a worker body that never polls runs to completion in `_LINGERING_THREADS` while the UI already reported "cancelled" | Per-op audit of every long-running callable (solve, optimization sweeps, batch commit) is a sweep of its own; the mechanism itself is verified correct | Grep-based pass marking each submitted callable as checks-event / can't-check (third-party call) / trivially short; for can't-check ops, label the cancel affordance "stop waiting" honestly instead of "cancel" |
| REW timeout is a fixed 1.5 s (`RewApiClient`) with no surfaced retry affordance — a slow REW instance fails fast but the user can't ask for a second attempt from the error surface | Retry policy (in-place vs. dialog button vs. preference) is product UX | The `rew.unavailable` message's recovery string already tells the user to check REW; a "再試行" button on `warn_user` for retryable error codes is a small extension of the existing dialog contract |
| `CommandErrorHandler` is single/global — a command needing custom failure UX (e.g. "offer to retry with different args") has no per-command override | No current command needs it; adding a per-command `on_error` field is speculative API | `CommandDefinition.on_error` consulted before the global handler, both defaulted the same way |

## Files changed

`src/htdt/cad_document.py` (history ordering),
`src/htdt/native_worker.py` (exception-typed error payload),
`src/htdt/native_diagnostics.py` (status-bar uncaught notice),
`src/htdt/command_registry.py` (`set_error_handler` + `execute` catch),
`src/htdt/workflow_application.py` (handler wiring, warn_user on
import/export writes, mapped backup summary),
`src/htdt/user_facing_error.py` (`rew.unavailable`, `rew.api`, `io.no_space`),
`src/htdt/native_cad.py` (data-dir failure → report_launch_failure in GUI mode),
`src/htdt/measurement_workflow.py` (mapped per-item errors + warning logs),
`src/htdt/room_workspace.py`, `src/htdt/optimization_extended_controller.py`,
`src/htdt/optimization_search_controller.py`, `src/htdt/room_prediction.py`,
`src/htdt/application_pages.py` (mapped messages at embed sites),
`tests/test_round9_error_surfaces.py` (new).

## Tests

- `tests/test_round9_error_surfaces.py` — 13 tests: failed `push` neither
  records nor truncates redo; failed `undo`/`redo` do not consume steps;
  worker error payload keeps the exception type and maps to
  `io.permission`; registry routes executor failures to the handler and
  re-raises without one; uncaught exception posts the status-bar notice;
  the notice is a thread-safe no-op off the GUI thread; batch
  stage/commit/dataset failures report mapped messages; GUI launch
  reports data-dir failure via `report_launch_failure`; maintenance
  launch prints to stderr without a dialog; ENOSPC maps to the dedicated
  disk-full message; `RewApiUnavailable` maps before its `RewApiError`
  base.
- Touched-surface suites run clean: `test_native_worker`,
  `test_command_registry`, `test_cad_document`,
  `test_measurement_workflow_extensions`, `test_measurement_workflow_ux130`,
  `test_native_diagnostics`, `test_room_prediction`,
  `test_workflow_application`, `test_workflow_shell`,
  `test_workflow_integration`, `test_optimization_workflow_workspace`,
  `test_e2e_workflow`, `test_palette_search`,
  `test_review_round8_palette_providers`, `test_ui_workflows`,
  `test_launch_intents`, `test_launch_router`, `test_native_launch`,
  `test_application_pages`, `test_room_workspace`,
  `test_optimization_workspace`, `test_support_diagnostics`.
- Full suite: `cd backend && TMPDIR=/c/t PYTHONIOENCODING=utf-8
  QT_QPA_PLATFORM=offscreen C:/devin/python/python.exe -m pytest -q -n 4`
  — one failure, `test_cad_measured_modal_analysis::
  test_reconstruct_mode_shape_normalizes_by_peak`, which fails
  identically on a detached `origin/main` worktree (preexisting, from the
  round-9 accuracy merge — `reconstruct_mode_shape` now requires explicit
  `evaluation_position_ids` and the test was not updated). A second
  `-n 4` failure, `test_evidence_lifecycle_rejects_illegal_promotions`,
  passes standalone and in-file on this branch — a parallel-isolation
  flake, not caused by this diff (none of the touched files are in its
  import path).
