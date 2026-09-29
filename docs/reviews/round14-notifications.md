# Round 14 — Notification & Status-Message Truth

Scope: does every user-visible signal — statusBar messages, Activity Center
ops, panel status/notice labels, progress text, completion dialogs — fire
when the thing actually happened, say what actually happened (real counts,
real destination, real cause), and point at the real next step? All 299
`statusBar().showMessage` sites, the `ActivityCenter` op registry +
`activity_history.json` persistence, per-panel notice systems, progress
labels, and dialog completion surfaces were audited; long-running actions
were exercised offscreen where the emitted text could be observed.

Two notification tiers exist by design and were evaluated against their
contract: ephemeral `statusBar`/in-panel labels for trivia, and the
persistent `ActivityCenter` op registry for things worth returning to.

## Action → notice → verdict

| Action | Start signal | Done signal | Fail signal | Verdict |
|---|---|---|---|---|
| Project create/open/save/export (`workflow_application`) | modal/status message before work | dialog/status with real path, once | `warn_user`/status with `operation_error_message(exc)` | **Honest** |
| Deliverable exports (analysis CSV/HTML/JSON, drawing packs, handoff docs) | command-gated (disabled_reason shown) | completion dialog with real output path(s) | `warn_user` with cause | **Honest** |
| Automatic backup | ActivityCenter op submitted at real start | `complete(result_summary='…: {path}')` + statusBar | op `fail(error_summary)` + statusBar — **statusBar dropped the cause; fixed** | **FIXED** |
| Automatic backup "not due" check | — | silent by design (must not nag every launch) | — | **Honest by design** (docstring-declared) |
| Data mgmt: backup/restore/validate/relocate/scan/GC (`data_management*`) | EXCLUSIVE op + progress card '処理を開始しています' + indeterminate bar | UI dialogs/status with real path, destination, counts | `operation_failed` → status card w/ `message_ja` + detail; restart-required card when data left inconsistent | UI **honest**; persisted op summary was generic `'{title}が完了しました'` — **fixed** to carry real result fields |
| REW fetch/refresh (`measurement_page_workspace`) | '読み込み中です。' | 'REWから {count} 件を確認しました。' real count | `_operation_error_notice` mapped cause | **Honest** |
| Batch assignment commit | 'バッチを保存しています… {done}/{total}' live | '{committed} 件を保存、{reused} 件は既存測定を利用、{failed} 件失敗…' real counts | WARNING on cancel/partial; cancel click → '保存をキャンセルしています…' | **Honest** |
| Prediction run (`room_prediction`) | stateChanged → panel state label + busy gating | final honest state emitted once from `_task_thread_finished` | named cause; cancel → 'キャンセル処理中です。遅延結果は保存・適用しません' → terminal cancelled | **Honest** |
| Candidate generation (`optimization_search_controller`) | '候補を生成しています… · N件目から' | '候補を生成しました · 有効 {feasible}件' real count | named; stale-spec discard + wrong-spec-select announced | **Honest** |
| Joint optimization (`joint_optimization_panel`) | live `processed/total · 生成/再利用/ブロック` | real summary incl. Pareto count + budget_limited/cancelled qualifiers | named | **Honest** |
| Measure tool (`room_measure_input`) | '計測: 端点をクリックしてください…' | — result showed but prompt kept asking for endpoints; post-result clicks silently padded `_endpoints` | Esc → **stale 'click to measure' text stayed after tool ended** | **FIXED** (result → '計測結果を表示しました…'; cancel → '計測を中止しました') |
| Capture delivery (`_announce_capture_delivery`) | — | statusBar '受信しました → {staging_ref}（受信ボックスで確認）' real ref | — | **Honest** |
| Capture inbox defer/reject/resume/assign | row-level actions | disposition column updates + refresh | `warn_user` on failure | **Honest**; '割り当てへ進む' action → real `assignment` context |
| Uncaught exception | — | ActivityCenter 'uncaught_exception' op with `concise_reason + ' — ログ: {path}'` + statusBar w/ log path | n/a | **Honest** (bounded 20/session) |
| Command execution (`command_registry`/`command_palette`/`CommandShortcutBinder`) | unavailable → palette rows + menus show coded disabled_reason | command runs | `execute()`→False is a **silent no-op via keyboard shortcut** | **Deferred** (conventional; discoverability covered by menus/palette) |
| Preferences commit (`workflow_settings`) | per-field immediate | value reflected in control | status banner w/ cause + WARNING severity | **Honest** (no "saved" toast needed — commit is instant) |
| Video evaluation (`room_video_panel`) | — | '評価しました（映像面/視線/衝突）' | status label + `_set_operation_error` | **Honest** |
| Settings dialog close-while-busy | — | — | close refused w/ QMessageBox naming reason | **Honest** (QDialog has no statusBar — dialog is the right surface) |
| Activity page ops table | rows on activate | — | — | pull-only refresh left '実行中' stale while page open — **FIXED** via `activity_center.subscribe` → `QTimer.singleShot(0, page, page.refresh)` |

## Findings & fixes

1. **Measure tool left a stale "click endpoints" prompt after both result and
   cancel** — `_after_pick` computes the result then kept emitting the
   begin-time instruction (`計測: 端点をクリック…` / `計測中: …`); clicks after
   the result only appended to `_endpoints` and silently recomputed the same
   answer. `cancel()` emitted `stateChanged` but never rewrote the status, so
   the prompt survived the end of the interaction. Verified offscreen: status
   still read "端点をクリックしてください" with `is_active == False`. Now the
   terminal pick sets `計測結果を表示しました · 新しい計測は「計測開始」で開始`
   and `cancel()` sets `計測を中止しました`.
   `backend/src/htdt/room_measure_input.py`

2. **Automatic-backup failure statusBar dropped the cause** —
   `_on_automatic_backup_completed` failed the Activity Center op with the
   real `operation_error_message(error)` but showed the bare
   '自動バックアップを作成できませんでした' with no timeout and no reason.
   Now appends `· {operation_error_message(error)}`, matching the convention
   used by every other failure notice.
   `backend/src/htdt/workflow_application.py`

3. **Activity Center persisted a generic completion line for data ops** —
   `_complete_success` wrote `'{title}が完了しました'` for every op kind while
   `active.result` already carried the real destination and counts the UI
   dialogs display. The persistent record — the thing a user returns to later —
   now gets a per-kind `_result_summary`: backup → archive path; validate →
   path + file count; restore → path + schema migration + pre-restore backup;
   relocate → destination + parked dir; scan → orphan count + reclaimable
   bytes; GC → deleted count + freed bytes + skipped count.
   `backend/src/htdt/data_management.py`

4. **Activity page ops table showed stale state while open** — rows were
   built only on `refresh()` bound to `on_activate`, so a RUNNING row never
   settled while the user watched. The page now refreshes via
   `activity_center.subscribe` → `QTimer.singleShot(0, page, page.refresh)` —
   queued onto the page's thread (safe from worker-thread mutations) and
   dropped automatically if the page is deleted.
   `backend/src/htdt/workflow_application.py`

## Deferred / intentionally not changed

- `CommandShortcutBinder` discards `execute()`'s False — a keyboard shortcut
  on an unavailable command is a silent no-op. Conventional Qt pattern, and
  the palette/menus already advertise availability reasons; a status-bar
  nudge per blocked shortcut would need a registry↔statusBar coupling that
  doesn't exist yet.
- Automatic-backup "not due" evaluation stays silent — deliberate ("must not
  nag every launch" per `automatic_backup_runner` contract). Only a real run
  surfaces an entry.
- Preferences have no "saved" toast — commit is synchronous and the edited
  control reflects the value immediately; failure surfaces a banner.
- `_mirror_progress` mirrors worker `message_ja` as INDETERMINATE stage text —
  the ops don't emit fractions, so the indeterminate bar + stage label is
  honest rather than a fabricated percentage.

## Verification

- `backend/tests/test_notifications_r14.py` (new, 3 tests): measure-status
  lifecycle (begin→prompt, result→"result shown", cancel→"中止", second Esc
  stays put); `_result_summary` real-field coverage for all six op kinds;
  ActivityPage live-refresh via subscribe→queued refresh.
- Offscreen probe: `RoomMeasureController.begin/pick/cancel` against a
  status-sink stub confirmed the old stale text and the new terminal texts.
- Full suite: `pytest backend/tests -q -n 4` (QT_QPA_PLATFORM=offscreen).
