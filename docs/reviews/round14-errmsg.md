# Round 14 — Error-Message Quality & Recovery Guidance Review

Scope: end-to-end audit of user-facing error paths in
`backend/src/htdt` (PySide6 desktop app). For each failure a real user
hits — missing/corrupt/locked file, store schema too new, invalid input,
worker failure, cancelled op, disk full, permission denied, port busy —
the failure was triggered honestly offscreen
(`QT_QPA_PLATFORM=offscreen`, `QMessageBox` exec/statics monkeypatched)
and the produced message was read and graded on three axes: (a) is it in
Japanese, (b) does it name what actually failed, (c) does it explain
what the user can do next. Round 6 swept the raw `str(exc)` *leaks*;
this round audits message *quality* — plus consistency across entry
points, actionable copy, detail-collapse behaviour, and handlers that
can themselves raise.

Headline finding: the `user_facing_error` contract
(`user_facing_error.py` — `to_user_facing_error` /
`operation_error_message` / `warn_user`) is sound, but several
high-traffic surfaces still **bypassed** it: launch-failure dialogs took
the English `concise_reason(exc)` (`"NativeSchemaError: schema v99 > …"`)
as their *primary* reason line, launch-intent routing details embedded
`f'{exc}'`, the data-management status card showed raw sqlite/backup
internals, the editor save-failure surface was statusbar-only with
English detail, the receiver's `last_error` held OS error strings, and
the migration/upgrade exception family
(`NativeUpgradeError`, `IncompatibleNewerSchemaError`,
`MigrationOpenError`, `NativeSchemaError`, …) was not mapped at all —
every one of them fell through to the generic
「操作を完了できませんでした」. A real crash-report dialog also had no
`setDetailedText`, so the technical context was lost entirely rather
than collapsed.

## Failure → observed message → verdict

Method: real triggers, real widgets, offscreen captures of
`{title, text, informative, detailed, buttons}`.

| Failure (triggered honestly) | Entry point | Message observed before fix | Grade | Verdict |
|------------------------------|-------------|-----------------------------|-------|---------|
| Store schema too new (`schema_version=99` in `cad-scenes.sqlite3`) | launch `report_launch_failure` | reason = `IncompatibleNewerSchemaError: HTDT cannot open…` (English) | names culprit ✓, JP ✗, next-action ~ | **Fixed** — dedicated JP schema-too-new dialog copy + English in Details |
| Upgrade execution fails (`NativeUpgradeError` / quarantine / verify / no-space) | launch upgrade path | generic '操作を完了できませんでした' equivalent — no family mapping | silent-generic | **Fixed** — 6 migration-class mappings with JP recovery steps |
| Data dir unavailable (`ManagedDataUnavailableError('root missing')`) | `--data-dir` launch | reason = `ManagedDataUnavailableError: root missing` (English) | JP ✗ | **Fixed** — mapped '管理データにアクセスできませんでした' + 'root missing' in Details |
| Renderer init failure (`QOpenGLWidget` raise) | launch generic `except` | reason = `RuntimeError: <english>` via `concise_reason` | JP ✗ | **Fixed** — `_launch_reason_ja` → '描画エンジン（GPU/ドライバ）の初期化に失敗しました' |
| Corrupt capture file routed | launch intent → `route_launch_intent` | activity detail `インポートに失敗しました: ValueError: …` | mixed JP/EN | **Fixed** — `operation_error_message` → 'インポートファイルを解析できませんでした' |
| Missing bundle/project file | launch intent | same raw `{exc}` tail | mixed | **Fixed** — mapped + exc logged |
| Backup preview on corrupt archive | DataManagement card | `詳細: sqlite3.DatabaseError: file is not a database` | raw internals as primary | **Fixed** — `detail` = mapped JP, `technical_detail` = raw for logs |
| Legacy `htdt.sqlite3` unreadable | DataManagement card | `レガシーデータベースを読み取れませんでした: database disk image is malformed` | English tail | **Fixed** — full-JP sentence naming file + cause + recovery |
| Editor save → `PermissionError` (locked repo) | `native_editor.save` | statusbar-only '保存に失敗しました · PermissionError: …' | JP ✗, no dialog | **Fixed** — JP statusbar + `warn_user` dialog ('…拒否されました' + 下書き保持 + Details) |
| Receiver bind on busy port (EADDRINUSE) | CaptureReceiver status/`last_error` | `OSError: [WinError 10048] …` | English, cryptic | **Fixed** — new `io.address_in_use` mapping: '指定のアドレス・ポートはすでに使用中です' + port-change advice |
| QR-code issuance failure | CaptureReceiverSettings | `QRコードを発行できませんでした · OSError …` | EN tail | **Fixed** — mapped tail |
| Optimization verify/robustness/adaptive worker failure | 3 controllers | `確認できません: <raw exc>` | raw tail | **Fixed** — mapped |
| Prediction unavailable | `room_prediction` reasons | `(str(exc),)` raw | raw | **Fixed** — mapped |
| Uncaught workflow op | `workflow_application` activity | `concise_reason` English tech line as the whole summary | EN primary | **Fixed** — '操作を完了できませんでした (RuntimeError: boom) — ログ: …' (JP lead, exception identity parenthetical, log pointer) |
| Invalid field input (scene validation) | editor inspector | 'シーンデータを検証できませんでした' + field list | JP ✓ names ✓ | Clean (unchanged) |
| Project open/create/import failure | menu + palette entry points | `to_user_facing_error().notice_text()` / `warn_user` — same contract both paths | consistent ✓ | Clean (unchanged) |
| Cancelled operation | workers | `cancelled` flag, no error emitted | correct silence | Clean |
| CLI capture-import failure | `htdt-capture-import` | JSON `{ok:false,error:…}` to stderr | machine contract, acceptable | Clean (documented) |

## Fixed this round

| ID | Severity | Area | Finding | Resolution |
|----|----------|------|---------|------------|
| R14-F1 | High | `user_facing_error.py` | The whole migration/upgrade/schema exception family (`NativeUpgradeError`, `NativeUpgradeQuarantineError`, `NativeUpgradeVerificationError`, `IncompatibleNewerSchemaError`, `InsufficientUpgradeSpaceError`, `MigrationOpenError`, `NativeSchemaError`) had no mapping → worst failures a user can hit (can't open own data after upgrade) showed the generic '操作を完了できませんでした' with no next step. | 7 name mappings with distinct JP reason + recovery (restart-retry / restore backup / open in newer build / free disk space), ordered before catch-alls so subclass suffixes still win. |
| R14-F2 | High | `native_diagnostics.py`, `native_cad.py` | Launch-failure dialog reason was `concise_reason(exc)` — `"NativeSchemaError: schema v99 > current v3"` as the primary user line; no `setDetailedText` anywhere, so technical context was either primary or gone. | `report_launch_failure` gained `technical_detail` kwarg rendered via `setDetailedText` (collapsed) + stderr fallback; all 4 launch call sites pass `_launch_reason_ja(exc)` (mapped message, with `classify_startup_failure` → JP per-class reason as fallback for unmapped launch classes: renderer→GPU, preferences→settings, integration→services, project_data, schema, migration). |
| R14-F3 | High | `launch_router.py` | 7 intent-routing failures embedded `f'{exc}'` in activity-feed details shown to the user (project-switch, bundle import, capture reject/fail, plan rebuild, inbox staging, backup validation). | All details → `operation_error_message(exc)`; raw exception moved to `_LOGGER.info` per handler so diagnostics are preserved, not dumped. |
| R14-F4 | High | `data_management.py`, `data_management_ui.py` | `DataOperationFailure.detail` carried raw `str(exc)` (sqlite 'file is not a database', backup internals) into the operator status card; legacy-unreadable label appended raw `report.detail`. | `DataOperationFailure` split: `detail` = `operation_error_message` (localized), new `technical_detail` field keeps `str(exc)` for the log/Details channel; all 3 emit sites updated; legacy label is now a full JP sentence (names `htdt.sqlite3`, '破損している可能性', restore-from-backup/next step). |
| R14-F5 | Medium | `native_editor.py` | Save failure = statusbar-only `f'…{exc}'` — transient message, English tail, no dialog, no explicit 'draft kept' recovery signal beyond implicit. | `warn_user` dialog (mapped JP + '下書きと復旧スナップショットは保持されました' effect + exc in Details) plus JP statusbar tail; `_sync_recovery`/`_persist_view_state` status lines mapped too. |
| R14-F6 | Medium | `capture_receiver_controller.py`, `capture_receiver_settings.py` | Receiver `last_error`/`status_lines` stored raw `OSError` text (EADDRINUSE → 'WinError 10048'); QR issuance failure appended raw exc. | `operation_error_message` at the two stores; new `io.address_in_use` mapping (errno EADDRINUSE + WinError 10048) → '指定のアドレス・ポートはすでに使用中です / ポート番号を変更するか、使用中のアプリを終了してください'. |
| R14-F7 | Low | `optimization_validation_controller.py`, `optimization_robustness_controller.py`, `optimization_adaptive_extended_controller.py`, `room_prediction.py`, `workflow_application.py` | Five scattered raw-exc tails/messages on worker-validation and activity surfaces; the uncaught-exception activity record was `concise_reason`-only (no mapped JP at all). | Each → `operation_error_message(exc)`; uncaught-exception summary = mapped JP + `(concise_reason)` parenthetical so the persistent record still names the exception class. |
| R14-F8 | Low | `backend/tests/test_native_diagnostics.py`, `test_round9_error_surfaces.py`, `test_round10_ux.py` | Three tests pinned the English-as-reason contract (`'schema v99' in reason`, `'root missing' in reason`, English-only uncaught summary). | Updated to assert JP reason/summary lead + English identity preserved (`technical_detail` / parenthetical) — locks the new contract. |

## Consistency check (same failure, different entry points)

- **File open/save**: menu `開く`, command-palette project-switch, and
  launch-intent descriptor routing now all resolve through
  `operation_error_message`/`to_user_facing_error` → identical mapped JP
  on every surface (verified by triggering a corrupt file through the
  intent path and the palette path).
- **Worker failures**: `NativeWorkerPool` emits the exception object on
  `completed`; all ~20 consumers verified to map it through
  `operation_error_message` — consistent by construction.
- **GUI vs CLI**: CLI paths (`htdt-capture-import` JSON contract,
  maintenance `--backup` stderr) keep machine-oriented output — English
  technical text there is the documented contract, not a regression;
  the same failure through the GUI now yields the JP dialog.

## Actionable copy / detail collapse / double-error audit

- **Retry buttons**: `warn_user` already attaches a retry button for
  `RETRYABLE_ERROR_CODES`; the main transient failures (I/O, busy,
  lock) map to retryable codes — retry affordance present where it
  helps; permanent failures (schema-too-new) correctly get 'open in
  newer build / restore backup' recovery text instead of a useless
  Retry.
- **Detail collapse**: `warn_user` and `report_launch_failure` both put
  `str(exc)`/`concise_reason` behind `setDetailedText` — technical text
  is secondary-collapsed everywhere and never the only content. Verified
  on the real dialogs (above table).
- **Error-in-error handlers**: `_launch_reason_ja` lazy-imports
  `classify_startup_failure` inside `try/except` because the generic
  launch `except` can fire before the in-try import completes (e.g.
  `QApplication()` itself raising) — a module-level name there would
  `NameError` inside the handler. `report_launch_failure` falls back to
  stderr when no `QApplication` exists. `native_diagnostics` excepthook
  paths guard their own logging. `data_management` failure emitters
  initialize `lifecycle_technical`/`lifecycle_detail` pre-try so a raise
  mid-path can't `UnboundLocalError` the handler. All audited handlers
  are now raise-safe.

## Verified clean (checked this round, no findings)

- **Worker crash contract**: `completed.emit(key, None, exc)` +
  per-consumer `operation_error_message` — no surface shows the raw
  object; consistent across all consumers sampled (~20).
- **Cancelled ops**: no error message emitted (correct — cancellation is
  user intent, not failure).
- **Validation errors**: `InspectorValidationError` and
  `SceneValidationError` paths produce field-named JP messages
  (`検証` surfaces, editor inspector) — already the best messages in the
  app.
- **Project open/create/import**: menu, palette, and library-card entry
  points share `to_user_facing_error().notice_text()` + `warn_user`.
- **REW/equipment errors**: `equipment_library.py` already on the
  contract; `CaptureImportError` internals stay English only on the
  HTTP-reject (machine-to-machine) boundary — GUI side is mapped.

## Deferred (deliberately not in this diff)

| Severity | Location | Finding | Why deferred |
|----------|----------|---------|--------------|
| Medium | `native_backup.py` (~30 raise sites) | Bare `ValueError('english detail')` on backup read/verify failures → maps to generic 'データを処理できませんでした' — loses 'it was the *backup*' specificity (surrounding context usually names it). | A dedicated `BackupError(ValueError)` subclass would let the mapper say 'バックアップデータを…' — ~30 mechanical raise-site swaps; own follow-up. |
| Low | `test_native_diagnostics.py` collection | Import-time circular-import flake (`pydantic._internal._validators` via shiboken lazy-import hook) reproduces on clean `main` — preexisting env/toolchain ordering issue, unrelated to this diff. | Worked around locally by pre-importing pydantic before pytest collection; not a repo-code change to land in this PR. |
| Low | `test_cad_hybrid_prediction_provider.py::test_evidence_lifecycle_rejects_illegal_promotions` | xdist concurrency flake — `FileNotFoundError` on a fixture `.tmp` authority file under `-n≥2`; fails identically on clean `main`, passes serially. | Preexisting test-harness race, unrelated to error-message changes; leave for a test-infra pass. |
