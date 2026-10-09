# Issue #974 — Activity Centerに予測・取込・最適化の進捗/中止/結果を統合

## Scope

`ActivityCenter` (`submit` / `update_progress` / `request_cancel` /
`retry` / `note_authorities_changed`) was already implemented and the
REW auto-ingest already registered its jobs — but the shell had no
place to *see* them, and the other long operations (acoustic
prediction, optimization candidate/extended/adaptive search, REW/IFC
import, measurement batch commit, acceptance checks) still lived as
per-workspace busy flags. This issue wires the existing registry into
the existing UI: no new top-level workspace, no new compute or
verification authority, and `ActivityCenter` itself was not rebuilt —
only extended (retry id binding, payload accessor) where the contract
needed it.

## What an operator sees now

### Shell status strip (`ActivityStatusStrip`, `workflow_shell.py`)

A compact strip under the context bar, bound to the shared center via
`shell.activity_strip.bind(center)`:

- running counts by state (`実行中 N件 / 中止要求中 N件 / 待機中 N件`),
- latest failure (`直近: <title> — 失敗 <error_summary>`),
- navigation-block line (`<title> — <block reason>`) while an
  EXCLUSIVE op is live, telling the operator why the shell refuses to
  switch and what is still possible (stay, or open the Activity page),
- 開く button → `openRequested` → the existing Activity page
  (`ApplicationDestinationId.ACTIVITY`).

Screen-reader: state transitions announce once each
(`<title>: 実行中 → キャンセル要求中`) through `announce_status`
(polite). Progress text repeats are not re-announced — only real state
changes are.

### Activity page rows (`application_pages.py` `ActivityPage`)

The operations table gained a 進捗 column and a per-row 対応 cell:

- 中止 — only while `can_cancel_now`; calls `cancel_operation(op_id)`
  which routes to `ActivityCenter.request_cancel` → the registered
  cooperative callback. The row then shows キャンセル要求中 until the
  executor reports back; the UI never marks a job cancelled on its own.
- 再試行 — `RetryPolicy.SAFE_NEW_ATTEMPT` terminal records only.
- 再試行（要確認）— `RetryPolicy.UNSAFE` records: a `QMessageBox`
  re-authorization (`_confirm_retry`) runs before the stored `rerun`
  payload is invoked, so apply/overwrite re-runs are never silent.
- 開く — the op's `WorkspaceDeepLink` URI (`htdt://...`) routes back to
  the originating workspace/context. Row double-click activates the
  same link.

## Canonical registrations

One operation = one record, deep-linked back to its origin:

| kind | class | nav | cancel | retry |
|---|---|---|---|---|
| `prediction.run` | COMPUTE | BACKGROUNDABLE | cooperative | SAFE_NEW_ATTEMPT |
| `optimization.candidate_search` | COMPUTE | BACKGROUNDABLE | cooperative | SAFE_NEW_ATTEMPT |
| `optimization.extended_search` | COMPUTE | BACKGROUNDABLE | cooperative | SAFE_NEW_ATTEMPT |
| `optimization.adaptive_build` / `.adaptive_extended_build` | COMPUTE | EXCLUSIVE (計画の保存を伴う) | cooperative | none |
| `optimization.rew_import` | EXTERNAL_IO | EXCLUSIVE (取込結果の保存を伴う) | cooperative | none |
| `measurement.batch_commit` | EXTERNAL_IO | EXCLUSIVE (保存・適用を伴う) | cooperative | UNSAFE (再試行に要確認) |
| `measurement.rew_list` / `measurement.rew_read` | EXTERNAL_IO | BACKGROUNDABLE | cooperative | UNSAFE |
| `room.ifc_import` / `room.ifc_diff_import` | EXTERNAL_IO | EXCLUSIVE (メインスレッドで実行中) | not cancellable (sync) | none |
| `acceptance.auto_check` | COMPUTE | EXCLUSIVE (チェック結果の記録を伴う) | not cancellable | none |

EXCLUSIVE ops are the ones that commit sealed records on completion
(batch commit, adaptive plan builds, REW/IFC imports, acceptance
results) — their existing navigation guards are preserved, now with a
real reason and a visible path to the live record. Pure compute/fetch
ops are BACKGROUNDABLE, so `before_deactivate` no longer blocks the
operator from leaving a workspace that only reads — that is the
UI-block-time reduction the issue asks for.

`MeasurementPageWorkspace.before_deactivate` and
`OptimizationWorkflowController.before_deactivate` now consult
`center.navigation_blockers()` and refuse with
`<title>: <block reason>` — or let navigation through when only
backgroundable work is live.

## Honesty rules kept

- Progress column echoes only real `items/bytes/stage/fraction`
  (`operation_progress_text`): `4/10 件`, `2/5 ステージ`, 進行中 —
  never a fabricated % or ETA.
- Terminal results carry real summaries (`有効 12件（総 48）`,
  `REW測定 <id> を保存しました`, `<file> · 差分 N 行`); discard
  branches record *why* (`部屋または制約が変更されたため古い結果を破棄しました`)
  as failures rather than current results.
- `completed_for_historical_input` stays a distinct state — never
  rendered as verified/current.
- Dead workers cannot masquerade: `_terminate_operations()` /
  `_terminate_measure_ops()` request-cancel + confirm every live
  record on stop/dispose, and `persist_history` /
  `load_active_operations` keep only last-known diagnostics after
  restart — nothing displays as still running.

## Center extensions (minimal)

- `retry(..., new_operation_id=)` — adapters pre-bind the fresh attempt
  id to their executor key so the completion handler resolves it
  directly; `domain_payload_of(op_id)` exposes the live record's
  payload for the retry/rerun adapters.
- `OPERATION_STATE_LABELS` / `operation_state_label` /
  `operation_progress_text` — the shared vocabulary the strip, page
  and tests render from.

## Tests

`backend/tests/test_issue_974_activity_integration.py` — 18 tests:

- **Failure/timeout:** `error_summary` recorded and surfaced (timeout
  reports as failure, never as cancel).
- **Cancel:** `request_cancel` invokes the real callback; row shows
  キャンセル要求中; detached drain (`confirm_cancelled` on stop)
  terminates every live record.
- **Old revision:** `note_authorities_changed` marks intersecting
  inputs `completed_for_historical_input` + `current_for_input=False`.
- **Restart:** persisted active ops reload as diagnostics only — no
  phantom running rows.
- **Navigation:** EXCLUSIVE op reports its block reason;
  `navigation_blockers()` only carries exclusive work.
- **Retry:** SAFE attempts re-dispatch with a fresh id + fresh cancel
  callback (`retry_of`/`attempt` linkage); UNSAFE requires the
  confirmation dialog before `rerun` fires.
- **Progress honesty:** items progress renders `4/10 件`, no `%`.
- **Deep link:** rows carry `htdt://` URIs back to the origin
  workspace; the 開く button and double-click both navigate.
- **Strip:** running counts, latest failure + block reason,
  announce-once-per-change, open-button signal.

Regression re-runs (all green): `test_activity_center`,
`test_issue_1017_activity_history`, `test_issue_1023_activity_scope`,
`test_application_pages`, `test_optimization_workflow_workspace`,
`test_optimization_workspace`, `test_measurement_workspace_composition`,
`test_measurement_rew_auto`, `test_room_workspace`,
`test_cad_acceptance`, `test_measurement_workflow_extensions`,
`test_measurement_workflow_ux130`, `test_cad_search`,
`test_cad_extended_search`, `test_cad_adaptive_planner`,
`test_cad_adaptive_extended`, `test_e2e_workflow`,
`test_issue_970_adaptive_design`, `test_rew_snapshot_import`,
`test_issue_977_ifc_locate`, `test_issue_981_ifc_diff_review`.

## Known gaps

- `list:`-prefixed REW combo-refresh jobs and `rew_auto_job_keys`
  auto-polls stay unregistered — the auto-ingest path registers
  separately already; registering the poll loops would double-count.
- IFC imports are synchronous on the UI thread, so their records are
  honest submitted→completed markers, not live progress — real
  progress requires moving them off-thread (out of scope here).
- `MeasurementPageWorkspace`'s pre-registration busy count still
  excludes `_rew_auto_job_keys`; unregistered legacy job kinds, if any
  are added later, must opt into the center explicitly.
