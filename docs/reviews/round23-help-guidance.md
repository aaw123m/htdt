# Round 23 — Error-message actionability & in-app guidance

Scope: every user-facing failure/guidance surface across six dimensions —
(1) error dialogs/banners/notices (do they name what failed, the cause in
user terms, and the concrete next step?), (2) empty states (do they say
why empty and what to do first?), (3) tooltip/Whats-This coverage on
non-obvious controls, (4) in-app help entry points and dead links, (5)
progress honesty (indeterminate vs deterministic), (6) warning/confirm
copy honesty (scope and counts named). Method: audit of every
`QMessageBox`, `statusBar().showMessage`, `_show_status`, empty-label and
disabled-command path in `backend/src/htdt`, reading each verbatim string
an operator would see and tracing each displayed value back to its
producer to verify whether raw backend text reaches it. The #903 contract
(`user_facing_error.py`: `warn_user`, `operation_error_message`,
`to_user_facing_error`) is the baseline every error surface is measured
against. Rounds 1–22 assumed; their findings are not re-reported.
Branch `devin/rev23-help`.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | Post-update revalidation failure dialog interpolated raw exception text — `詳細: {exc}` | MED guidance | FIXED |
| 2 | Data-management `_run_revalidation` status card detail took `str(exc)` raw | MED guidance | FIXED |
| 3 | Campaign pre-registration warning showed bare `str(exc)` — the only handler in its file not using `warn_user` | MED guidance | FIXED |
| 4 | Dirty-state resolution failures surfaced raw `str(error)` from the mount (`dirty_state_dialog._apply`, `RoomWorkspaceController.resolve_dirty_state`) | MED guidance | FIXED |
| 5 | `move_commit_gate` appended raw `CadConstraintAdapterError` text (`wall clearance requires room wall topology` …) to a status message | MED guidance | FIXED |
| 6 | Status-bar prediction rejects showed English internals verbatim: `result contract mismatch`, `immutable input identity mismatch` | LOW copy | FIXED |
| 7 | Reason → help-topic bridge fully built (`AvailabilityReasonSpec.help_topic_id`, `reason_help_topic_id`, `HelpTopic.reason_codes`, `HelpRegistry.topic_for_reason`) but inert: unreachable from any UI surface — disabled palette results dead-ended on the reason sentence | HIGH guidance | FIXED |
| 8 | Capture-inbox listing showed raw authority codes — `pending`/`deferred`/`new_series`/`capture-inbox-unassigned` — while the detail pane already localized some of them | LOW copy | FIXED |
| 9 | `native_cad.py` stderr diagnostics for maintenance paths print `{exc}` | — | VERIFIED OK — operator/CLI channel, GUI path already uses `report_launch_failure` with mapped reason + recovery + Details expander |
| 10 | `capture_receiver_settings` `{error}` interpolations, `workflow_application` `str(exc)[:500]` export metadata, `AnalysisExportMeta` diagnostics | — | VERIFIED OK — controller already maps via `operation_error_message`; export metadata is a diagnostics field, not the primary message |
| 11 | `equipment_library.py` validation dialogs interpolate `{exc}` into a JP lead-in | — | VERIFIED OK (borderline) — every raise behind it is an authored JP `ValueError`; `_looks_localized` would preserve them anyway |
| 12 | Empty states (project library, activity, reference library, inbox detail, palette empty query) | — | VERIFIED OK — all name why-empty + first action |
| 13 | Progress honesty across backup/restore/prediction/optimization/capture load | — | VERIFIED OK — `setRange(0,0)` only where fraction unknown; deterministic bars where progress exists; honest "処理中…" labels elsewhere |
| 14 | Destructive-confirm copy (project delete, purge retention, seat-orphan delete, intervention apply, dirty-state dialog) | — | VERIFIED OK — names scope, count, tombstone, and archive/back-up posture |

## 1 — Post-update revalidation failure dialog (fixed)

`native_cad.py` runs a data-format revalidation at startup after an
upgrade; on failure it warned with the raw exception inline:

```python
QMessageBox.warning(
    None, "HTDT 再検証",
    "再検証を完了できませんでした。データは変更されていません。\n"
    f"詳細: {exc}",          # raw backend text verbatim
)
```

The dialog now goes through `warn_user` — title
「再検証を完了できませんでした」, mapped localized message, effect line
「データは変更されていません。」, raw class+text preserved under the
Details expander and logged via `log_operation_error` (the existing
`diagnostics.logger.warning` also stays).

## 2 — Revalidation status card in data management (fixed)

`DataManagementWidget._run_revalidation` caught all exceptions and put
`str(exc)` straight into the status card detail — the only status path in
the file that skipped the contract (`_save_backup_policy` five hundred
lines down already used `operation_error_message`). One-word fix.

## 3 — Campaign pre-registration warning (fixed)

`system_expansion_widgets._preregister_campaign` was the file's only
`QMessageBox.warning(..., str(exc))` — siblings all use `warn_user` /
`operation_error_message`. Now `warn_user(self,
"キャンペーンを事前登録できませんでした", exc)`.

## 4 — Dirty-state resolution failures (fixed)

`dirty_state_dialog._apply` showed
`f"処理を完了できませんでした · {error}"`, and
`RoomWorkspaceController.resolve_dirty_state` returned
`return False, str(error)` — both hands an operator raw English such as
`EditStateError('cannot remove unknown entities: e9')` or
`'document replacement after state does not match'`. Both now map through
`operation_error_message`; `EditStateError` resolves to
「現在の編集状態では完了できませんでした」 and authored JP `ValueError`s
still pass through under `_looks_localized`.

## 5 — Move-commit gate (fixed)

`move_commit_gate` returned
`f"配置制約が参照先を失っています · {exc}"` — the JP lead-in is honest and
specific, but the appended adapter message is English internals
(`constraints/search axes reference unknown CAD entities: wall:x`).
Now `operation_error_message(exc)` supplies the suffix.

## 6 — English internals on the prediction status line (fixed)

`prediction_workspace.py` rejected bad worker results with verbatim
English: `予測結果を拒否しました · result contract mismatch` and
`· immutable input identity mismatch`. Now 「結果の形式が一致しません」
and 「実行時と入力が一致しません」 — consistent with the file's other
guards (「古い予測結果を破棄しました · scene/constraintが変更されています」).

## 7 — The "why" dead-end: reason → help bridge wired end-to-end (fixed)

The biggest structural finding. The app already had every piece of an
actionable "why is this disabled?" affordance:

- `AVAILABILITY_REASONS` carries `help_topic_id` per catalog code
  (`command.blocked.*` → `trouble.command_unavailable`,
  `prediction.run.*` → `trouble.prediction_unavailable`,
  `measurement.import.workspace_unbound` etc.);
- `reason_help_topic_id()` resolves it;
- `HelpRegistry` ships the target topics with reason-code bindings and
  `topic_for_reason`;
- the command palette renders disabled results with the localized reason.

But nothing consumed it — `reason_help_topic_id` had exactly one caller
(tests), and `PaletteResult` had no field to carry the binding. A user
hitting Enter on a greyed-out command got the reason sentence and nothing
else — a dead end.

Fix (minimal, additive):

- `PaletteResult.help_topic_id: str | None`, populated by
  `CommandPaletteProvider._result_for` / `.suggested` from
  `item.availability.reason` via `reason_help_topic_id` (fails closed on
  unknown codes; prose-only `CommandAvailability.unavailable` reasons get
  `None`);
- `CommandPalette` accepts `on_help_topic`; selecting a bound-unavailable
  row appends 「Enterで「なぜ実行できないか」のヘルプを表示」 to the detail
  line, and activating it opens the topic (the reason stays readable in
  the detail line);
- `CommandPaletteController` passes it through; `workflow_application`
  wires `self._open_help_topic`, so the same help dialogs reachable from
  F1 / palette help entries now serve the "why" path too.

Regression tests cover the field population (catalog-bound vs
prose-only), activation opening the topic, and the selection hint.

## 8 — Capture-inbox raw codes on the primary surface (fixed)

The inbox table — the surface the operator actually scans — rendered raw
authority enums in three of five columns: scope `capture-inbox-unassigned`,
classification `new_series`/`exact_duplicate`/…, disposition
`pending`/`deferred`/…. The detail pane already localized disposition and
scope; classification, gate-facet values (`validated`, `not_evaluated`,
`unresolved`…), and promotability authority kinds (`raw_visual_evidence`,
`semantic_geometry`…) were raw everywhere.

Added `_INBOX_CLASSIFICATION_LABELS`, `_INBOX_GATE_STATE_LABELS`, and
`_INBOX_AUTHORITY_KIND_LABELS`, plus `_inbox_scope_label` /
`_classification_label` helpers; the table's スコープ/分類/状態 columns and
the detail pane's 分類 / ゲート / 昇格 lines now all render Japanese. The
pre-existing test asserting `"new_series" in detail` was updated to the
localized string — that assertion codified the bug.

## Verified-OK surfaces (spot-checked verbatim text)

- **Empty states**: `「まだプロジェクトはありません。「新規プロジェクト…」から作成できます。」`,
  `「まだ記録はありません。保存や昇格を行うとここに表示されます。」`,
  `「まだ定義はありません。「機材ライブラリを管理…」で機材や素材を登録できます。」`,
  inbox `「取り込み待ちの配送はありません。配送が到着するとここに表示されます。」`,
  palette empty query `「該当する項目がありません」` + curated Suggested
  sections — all name why-empty and first action.
- **Tooltips**: disabled toolbar/menu actions carry the availability
  reason (`setToolTip(availability.disabled_reason)` +
  `WA_AlwaysShowToolTips`); joint-optimization checkboxes get per-option
  tooltips; run buttons update tooltips on state ('実行中です。',
  '揺らす軸を1つ以上選択してください。').
- **Confirm/destructive copy**: project delete lists blockers, archive
  note, and tombstone count; capture retention purge names the revision
  id and states 「外部の .htdtcapture バンドルは削除されません」; the
  dirty-state dialog presents explicit Save/Discard/Recover-Draft choices
  rather than a generic confirm; intervention-apply names the effect.
- **Progress**: `data_management_ui` uses `setRange(0,0)` only when
  `progress.fraction is None` else `0..100`; prediction runs show
  honest `予測中… revision … · input …` in the status bar with a real
  Cancel; capture/REW loads show `読み込み中` labels.
- **In-app help**: F1 opens the shortcut reference; the palette searches
  help topics via `HelpTopicPaletteProvider` and opens them through
  `_open_help_topic`; no stale doc links found (help is in-app, not URL
  based).
- **Diagnostics, not errors**: `native_cad` launch failures use
  `report_launch_failure` (localized reason + recovery + Details
  expander); `AnalysisExportMeta` `str(exc)[:500]` values are export-file
  diagnostics, not displayed strings; `capture_receiver_settings`
  `last_error` is already mapped by the controller.

## Deferred

- Menu/toolbar disabled `QAction`s surface their reason only as a tooltip
  — a genuinely better "why" affordance there (e.g. a status-tip or
  disabled-click explainer) needs a Qt-level workaround (disabled actions
  don't fire); the palette path added in #7 covers the same content.
- `equipment_library.py` `{exc}` interpolation and misc `{exc}` text in
  JP lead-ins — the raises behind them are authored JP messages today;
  if a raw English raise ever reaches them the contract's
  `_looks_localized` fallback covers presentation, so no fix was made.
- The remaining detail-pane identifiers (`item.arrival_source`,
  `capture_series_id`, revision ids) are left raw — they are entity
  identifiers an operator quotes back, not enum chips.

## Tests

`backend/tests/test_round23_help_guidance.py` — 10 tests driving the real
surfaces (palette activation + selection detail, dirty-state `_apply`,
`RoomWorkspaceController.resolve_dirty_state`, `move_commit_gate`,
`DataManagementWidget._run_revalidation`, `CaptureInboxPage` table +
detail). `test_application_pages.py` assertion updated for the localized
classification string.
