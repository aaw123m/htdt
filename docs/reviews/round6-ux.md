# Round 6 — Usability & Design Consistency Review

Scope: PySide6 surface of `backend/src/htdt` and the React SPA
(`frontend/src`, React 19 + TS + Vite 8). Hunt axes: error-message quality,
empty/loading/disabled states, workflow friction, design breakage
(spacing/typography/focus/contrast/hardcoded pixels), and locale
(JP/EN mixing). Prior rounds 3–5 findings are not re-reported.

Headline finding: the `#903` user-facing error contract
(`user_facing_error.py`) existed but was wired into only a handful of
surfaces — ~90 sites across ~30 files still printed raw `str(exc)` /
`f'… · {exc}'` (sqlite internals, pydantic dumps, English exception text)
as the *primary* operator message. That is now closed end-to-end: the
mapper learned a localized-Japanese passthrough (see R6-F1 caveat), a
`warn_user()` dialog helper was added, and every raw-leak site was moved
onto the contract.

## Fixed this round

| ID | Severity | Area | Finding | Resolution |
|----|----------|------|---------|------------|
| R6-F1 | High | ~30 backend files | Operator surfaces (statusBar, notice labels, `QMessageBox.information`, `_set_notice`, `_set_status`, `statusChanged.emit`) appended or printed raw exception text — `f'… · {exc}'`, `str(exc)`. Users saw `sqlite3.OperationalError near line 4821`, pydantic `ValidationError` dumps, or English internals instead of a cause + next step. | `user_facing_error.py` gained `_looks_localized()` (single-line, ≤160 chars, contains kana/kanji → keep authored text; domain code raises many bare `ValueError('日本語…')` so a blanket remap would have hidden *good* messages) and `warn_user(parent, title, exc)` (warning dialog: mapped message + recovery + optional effect, raw text preserved under Details + logged). Mass-swap: every `showMessage`/`setText`/`_set_notice`/`_set_status`/`_on_status`/`statusChanged.emit`/`QMessageBox` raw-exc site → `operation_error_message(exc)` / `warn_user(...)`. |
| R6-F2 | High | `PlacementConstraints.tsx`, `SearchSpace.tsx`, `App.tsx`, `FeatureCandidates.tsx` | Buttons silently disabled with no reason anywhere: 「統合度を評価」, 「候補数を確認」, 「SearchSpecを保存」, 「候補を生成」, 「複製して編集」, 「この測定を保存」, 「添付原本を保存」, 「選択版を解析」, 「特徴と幾何候補を解析」 — e.g. the generate button was dead whenever `integrity_valid` was false and the user had no way to learn why. | `title=` reason on every disabled button ("このConstraintSetは整合性エラーのため判定できません", "ConstraintSetを選択してください", "可動軸を1つ以上追加してください", "測定ファイルを選択してプレビューを確認してください", …). |
| R6-F3 | Medium | `styles.css` | `.unit-input` (every numeric with-unit input) used `var(--line-strong)` which is never defined in `:root` — the whole border declaration was dropped, rendering those inputs borderless. | `var(--line-strong)` → `var(--line)`. |
| R6-F4 | Medium | `api.ts` | Fetch failures surfaced raw browser text (`Failed to fetch`) and bare `HTTP 500` — no cause, no next step. | Connection failures → 「サーバーに接続できません。バックエンドが起動しているか確認してください。」; HTTP errors → 「要求を処理できませんでした (HTTP N)」. |
| R6-F5 | Medium | `App.tsx`, `FeatureCandidates.tsx` | All mutating actions (createProject, saveContext, importMeasurement, uploadAttachment, compare, runAcoustics, feature analyze) were re-entrant — a double-click fired two writes (immutable records still duplicate work). | Single `busy` flag; in-flight guard + button disable + 「保存中…」「処理中…」 labels. |
| R6-F6 | Medium | `PlacementConstraints.tsx`, `SearchSpace.tsx`, `App.tsx`, `application_pages.py` | Empty state = blank panel: `#constraints`/`#search` panels rendered *nothing* before a Context exists (first-run dead end); measurements/attachments lists empty grids; Qt ProjectLibrary / CaptureInbox / Activity pages blank. | Empty-state guidance on every panel ("Contextがまだありません…Step 2でContextを保存すると…", "まだプロジェクトはありません…", "取り込み待ちの配送はありません…", "まだ記録はありません…", "添付原本はまだありません…"). |
| R6-F7 | Medium | `ui_theme.py` | Focus styles existed for QPushButton/QLineEdit only; item views had `QAbstractItemView { outline: 0 }` with nothing replacing the suppressed platform focus rect, and tabs/checkboxes had no indicator — keyboard users could not see focus on trees, lists, tables, tabs, or checkboxes. | Focus rings for `QTreeView/QListView/QTableView::item:focus`, `QTabBar::tab:focus`, `QCheckBox:focus` using the existing `focus_ring` token. |
| R6-F8 | Low | `measurement_editor.py`, `App.tsx`, `PlacementConstraints.tsx`, `SearchSpace.tsx`, `application_preferences.py` | JP/EN jargon mixing inside Japanese surfaces: 'target patternなし', 'stale(要rebase)', 'materializeするpatternがありません', 'rebaseできません', 'hard gate', 'linked placementなし', 'Checking…', 'Generating…', '条件版' (Context), 'マイクsample rate', '多角形room', and the preferences `load_error` detail tail shown inside a Japanese notice. | Translated to product vocabulary (ターゲットパターン, 古い基準(要再基準化), 実体化, 必須条件, 連動条件なし, 判定中…, 生成中…, Context, マイクサンプルレート, 多角形の部屋, JP load_error bodies). |
| R6-F9 | Low | `ConstraintBuilder.tsx`, `application_pages.py` | Icon-only `↶` undo-vertex button had no `aria-label`/`title`; ProjectLibrary 「開く」 disabled with no explanation. | `aria-label`/`title` "直前の頂点を取り消す"; `WA_AlwaysShowToolTips` + tooltip "一覧からプロジェクトを選択すると開けます". |
| R6-F10 | Low | `RewReadonly.tsx` | Preview button hardcoded `96 PPO FRをプレビュー` while the "Requested PPO" input above it is editable — label lied whenever ppo ≠ 96. | Label now reads `{ppo} PPO FRをプレビュー`. |

## Verified clean (checked this round, no findings)

- **Destructive-action confirmation (Qt).** ~290 `QMessageBox.question` /
  `StandardButton.Yes` gates across `data_management_ui.py`,
  `intervention_planner_panel.py`, `room_workspace.py`,
  `system_expansion_widgets.py`, `workflow_application.py`; library
  deletes are additionally guarded server-side (`LibraryDeleteBlocked`
  maps to a dedicated JP message) and room-editor vertex ops advertise
  Undo. No un-gated destructive paths found in the common flows.
- **Slow-operation affordance.** `NativeWorkerPool` +
  `statusChanged`/`_set_busy` is the established async pattern; slow Qt
  operations run off the GUI thread with status text. The SPA now labels
  in-flight actions (「判定中…」「生成中…」「解析中…」「保存中…」).
- **Hardcoded pixel sizes.** Only structural fixed widths remain
  (right-stack 260–300 responsive tiers, a 12px single-glyph axis badge,
  `workflow_shell` rail) — none are text-bearing labels that clip, so no
  round-3-style clipped-label regression found this round.
- **SPA error surfaces.** Every `api()` call site sits in `try/catch`
  with a `.notice.error` presentation (carried from round 4/5 merges).

## Deferred (deliberately not in this diff)

| Severity | Location | Finding | Why deferred |
|----------|----------|---------|--------------|
| High | ~20 `QFileDialog` call sites (`geometry_import_dialog.py`, `theater_editor.py`, `workflow_settings.py`, …) | Every file open starts at cwd — no last-used-directory memory, the biggest workflow-friction item found. | Needs a shared `remember_dir` helper + per-dialog keys; signatures vary (getOpenFileName/getSaveFileName/getExistingDirectory). Own PR. |
| High | `room_editor.py` | The native room-edit canvas surface is English-majority (~26k EN chars): 'Draw Room · click vertices…', 'Vertex inserted · one Undo restores…' inside an otherwise-Japanese app. | ~60 strings to translate coherently; piecemeal translation would *increase* inconsistency. Own pass. (Its `{exc}` leaks were still mapped this round.) |
| Medium | remaining Qt dialogs | `warn_user` is applied to the 13 highest-traffic `QMessageBox.information(str(exc))` sites; ~30 more `QMessageBox` sites in lower-traffic panels still build dialogs inline (they no longer leak raw text, but don't get Details/recovery affordance). | Mechanical follow-up, low marginal risk. |
| Medium | Qt disabled buttons generally | `title=`/`WA_AlwaysShowToolTips` pattern is only established on ProjectLibrary 「開く」; other enable/disable Qt buttons rely on adjacent statusBar text. | Needs a sweep + a tiny helper (e.g. `set_disabled_hint`) rather than per-button one-offs. |
| Low | `analysis_export.py`, `report.py` | Export/HTML report templates keep English CSS-copy fragments. | Export artifacts, not interactive UI — vocabulary decision belongs with the report-format owner. |
| Low | `application_preferences` store internals | `PreferenceLoadState` detail strings translated; `load_state` enum names remain English by design (typed diagnostics). | Intentional — machine tokens, not prose. |

## Summary stats

- 42 files changed: 33 Qt backend files, 8 frontend files, 1 test file
  (`backend/tests/test_user_facing_error.py`: +3 tests covering the
  localized passthrough, dump-guard, and `warn_user` dialog contract),
  plus this doc; ≈ +430/−160 lines.
- Fixed: 2 high, 4 medium, 3 low + folded nits. Deferred: 2 high,
  2 medium, 2 low.
- Verification: `pytest -q -n 4` green; `tsc --noEmit` + `vite build`
  clean; every edited backend module `py_compile`s; offscreen Qt smoke
  test exercises `warn_user` end-to-end.
