# Round 10 — convergence: deferred-item implementation sweep

Scope: implement the deferred sketches from rounds 8–9 that round out the
failure/startup/reporting experience — uncaught-error persistence,
cancel-label honesty, a retry affordance on `warn_user`, the
`LocalizationService` wire-or-drop decision, the automatic-backup policy
UI, a startup splash, and print rules for HTML reports. Each sketch was
verified against current code first; where the sketch no longer matched,
the implementation follows the current code, not the doc. Branch
`devin/rev10-uxfin`. Verification is Qt-offscreen under Python 3.12.10
plus pytest.

## Verdict table

| # | Deferred item (source) | Verdict |
|---|------------------------|---------|
| 1 | Uncaught-exception notice is transient — a 10 s status-bar line with no persistent record (round9-errors) | FIXED — `push_uncaught_sink` in `native_diagnostics.py` lets each live composition register a sink; `_sys_hook` hands the exception to the newest sink after the status-bar notice. `WorkflowApplicationComposition._record_uncaught_operation` posts a `failed` pseudo-operation (`operation_kind='uncaught_exception'`, class COMPUTE, `error_summary = concise_reason(exc) + log path`). Capped at 20/session so a crash-looping slot cannot flood history. The close hook pops the registration |
| 2 | Cancel affordances overstate what cancel does (round9-errors) | PARTIAL — audit below. `measurement_editor` 読込キャンセル relabelled 読込の待機をやめる (+tooltip: the request still finishes, the result is discarded). `automatic_backup_runner.job` now polls `_cancel` between `evaluate` and `run_due`. Remaining non-pollers are honest "stop waiting" paths with no cancel-labelled UI |
| 3 | No retry affordance on failure dialogs (round9-errors) | FIXED — `warn_user(..., on_retry=, retry_label=)`: a 再試行 button (ApplyRole) appears only when `error.code ∈ RETRYABLE_ERROR_CODES` (`rew.unavailable`, `rew.api`, `io.error`, `io.permission`, `io.no_space`, `authority.conflict`, `authority.stale_head`). The registry error handler passes `lambda: registry.execute(command_id)` — retry re-runs the same command. Measurement-page REW jobs (`_refresh_rew_async`, `_read_rew_async`) pass `on_retry` through `_start_job`; a retryable worker failure shows the modal *and* keeps the inline notice |
| 4 | `LocalizationService` exists but is unwired (round9-i18n) | FIXED (minimal wire, per doc verdict "product call") — the composition now owns one service (`policy = general.language` pref, `system_locale = detect_system_locale()`), `HelpTopicPaletteProvider`/`HelpDialog.topic` resolve the locale through `resolve_locale` instead of raw `detect_system_locale`, and `action.retry` ('再試行'/'Retry') is the first real `tr()` call site. Removal was rejected: `HTDT_TERMINOLOGY`/`term_text` are already consumed by `result_trust.py`/`help_registry.py`. Full call-site retrofit stays a separate ~1-session product decision |
| 5 | Automatic-backup policy has no UI (round9-prefs) | FIXED — new "自動バックアップ" card in `DataManagementWidget` (after 操作): enabled checkbox, interval `QDoubleSpinBox` (0.5–2160 h), keep-generations `QSpinBox` (1–100), daily-generations `QSpinBox` (0–366), backup-dir label + 変更…/既定に戻す via `file_dialog_memory` key `backup.policy_dir`. Commit-on-change through `AutomaticBackupScheduler.save_policy` (atomic tmp+replace); failures report via `_show_status` with a mapped message. Intro text honestly states changes apply from the next launch check (runner builds a fresh scheduler per tick). Controls participate in `_refresh_actions` busy/restart gating |
| 6 | Startup splash deferred (round8-lifecycle) | FIXED — `_create_startup_splash` builds a programmatic 460×240 dark splash (no image asset; `QApplication.instance() is None` → `None`, since `QPixmap` without a GUI app is a process-level abort, not an exception). Shown only after `record_launch` — recovery dialogs precede it — hidden while the two upgrade `QMessageBox` calls own the screen (a splash is always-on-top), `_splash_status` updates at each seam (upgrade check → repository → project resolution → integrations → window build), `finish(window)` on success, `_close_splash` in all three failure paths. Every call is exception-safe |
| 7 | HTML reports have no print treatment (round9-report) | FIXED — `@media print` in `render_report_html`, `render_installation_report_html`, `render_analysis_html`: white background, `break-inside:avoid` on sections/metrics/SVG, collapsed `<details>` JSON dumps hidden (machine-readable payload stays in the `<script type="application/json">` tag). No nav bars exist on these pages — nothing to hide |

## Cancel-poll audit (item 2 detail)

Grep over every `NativeWorkerPool.start`/`QThread` submission:

| Submitted callable | Class |
|---|---|
| `joint_optimization_panel.operation` (`is_cancelled=cancel_event.is_set`) | checks-event |
| `optimization_extended_controller` / `optimization_search_controller` (`cancelled=cancel_event.is_set`) | checks-event |
| `prediction_workspace` / `room_prediction._operation` (event threaded through) | checks-event |
| `automatic_backup_runner.job` — now polls between `evaluate` and `run_due` | checks-event (added this round) |
| `measurement_editor._start_rew_task` / `measurement_page_workspace._start_job` / `optimization_workflow_controller._start_rew_task` — `lambda _cancel_event: call()` wrapping REW HTTP reads | can't-check (third-party blocking call); the job-guard discards late results — the affordance is relabelled "stop waiting" rather than "cancel"; page-workspace and workflow-controller surfaces expose no cancel-labelled UI, so nothing dishonest remains |
| `NativeWorkerPool` internals (`cancel`/`cancel_all`/`_linger`) | boundary checks verified by existing tests |

## Files changed

- `src/htdt/native_diagnostics.py` — `UncaughtSink`, `push_uncaught_sink`,
  `_post_uncaught_to_sink` wired into `_sys_hook`.
- `src/htdt/user_facing_error.py` — `RETRYABLE_ERROR_CODES`, `warn_user`
  gains `on_retry`/`retry_label`.
- `src/htdt/workflow_application.py` — `LocalizationService` per
  composition, `_language_policy`/`_presentation_locale`, registry retry
  wiring, `_record_uncaught_operation` + sink push/pop on the close hook,
  help locales routed through the policy.
- `src/htdt/measurement_page_workspace.py` — `_start_job` gains `on_retry`;
  both REW reads pass re-invokers; retryable worker failures show the
  dialog alongside the notice.
- `src/htdt/data_management_ui.py` — 自動バックアップ policy card.
- `src/htdt/native_cad.py` — splash helpers + launch seams.
- `src/htdt/automatic_backup_runner.py` — cooperative poll before
  `run_due`.
- `src/htdt/measurement_editor.py` — honest stop-waiting label + tooltip.
- `src/htdt/report.py`, `src/htdt/analysis_export.py` — `@media print`.
- `tests/test_round10_ux.py` — 15 regression tests.

## Verified

- `tests/test_round10_ux.py` (15 tests): pseudo-op carries reason + log
  path and is capped at 20; sink pop is idempotent and disables delivery;
  worker-thread excepthooks do not reach the sink; 再試行 re-invokes only
  for retryable codes (registry re-executes the same command id);
  localization policy resolves locale and `action.retry` renders
  JA/EN correctly; the policy card loads and commits
  `automatic-backup-policy.json`; splash helpers survive offscreen and
  no-op on `None`; all three HTML renderers emit the print rules.
- Existing touched suites green: `test_round9_error_surfaces`,
  `test_user_facing_error`, `test_native_diagnostics`,
  `test_automatic_backup(_runner)`, `test_report`, `test_analysis_export`,
  `test_installation_report`, `test_activity_center`,
  `test_command_registry`, `test_localization`.
- Full suite (`pytest backend/tests -q -n 4`) run after all edits — see
  session notes for the count.

## Still deferred (with reason)

- **Per-command `on_error` override** (round9-errors sketch 4): the global
  handler + retry affordance covers every current failure surface; a
  per-command override stays speculative API until a command actually
  needs custom failure UX. Documented, intentionally unbuilt.
- **Full call-site localization retrofit** (round9-i18n): the service is
  wired and proven on `action.retry` + help locales; migrating ~300
  hardcoded JA literals is the product decision the i18n doc itself
  defers. No change this round by design.
- **Activity Center "cancel" button**: the cancel machinery
  (`request_cancel`/`can_cancel_now`) is verified but still has no
  production UI wiring; adding a Cancel affordance to ActivityPage is a
  product-side surfacing decision (which ops declare cancellability),
  not a sketch to blindly implement.
- **Legacy `measurement_editor` REW jobs**: get the honest relabel; the
  retry dialog is wired on the shell's measurement page workspace (the
  maintained surface), not duplicated into the legacy editor.
