# Round 14 — Help / Onboarding / Context-Help Truth

Scope: every user-facing help surface must describe what the control actually does now — tooltips (`setToolTip`), `setStatusTip`, help-menu/F1 handling, '?' buttons, placeholder texts, empty-state guidance, onboarding/first-run text, and in-app doc links. Stale help is a spec inconsistency users hit immediately. Verified by reading each help string then tracing the control it describes (registry, binder, parsers, gating code) under `QT_QPA_PLATFORM=offscreen`.

## Findings

### 1. F1 shortcut list dropped alias bindings — FIXED

`HelpDialog.shortcuts()` (workflow_help.py) built registry rows as
`f"{definition.shortcut}  {definition.display_name}"`, skipping
`definition.shortcut_aliases`. `CommandShortcutBinder` binds every alias as a
live QShortcut, so two real keys were missing from the F1 reference:

- **Backspace** — alias of `edit.delete` (`Delete`)
- **Ctrl+Shift+Z** — alias of `edit.redo` (`Ctrl+Y`)

This was exactly the drift `shortcut_reference()` (help_registry.py) exists to
prevent — it includes `(definition.shortcut, *definition.shortcut_aliases)`
per definition — but the dialog re-implemented the row loop instead of calling
it. Fix: registry rows now come from `shortcut_reference(registry)`; the
dialog and the doc generator share one source.

### 2. Arrow-key nudge absent from every help surface — FIXED

`room_transform_input._key_press` (round-8 parity work) moves the selection by
one grid step on arrow keys (Shift = 10×), as an undoable group move through
the normal preview/commit path. It's a real keyboard semantic bound outside
the `CommandRegistry` (viewport `eventFilter`), so neither the registry-driven
rows nor the manual block mentioned it. Fix: added a manual line to the same
non-registry block that documents Ctrl+K/Alt+←/Alt+→/F1:

`矢印キー（部屋ビュー）  選択項目をグリッド1ステップ移動（Shiftで10倍）`

The scope qualifier is honest — nudge only fires inside the room viewport and
is rejected during mode/drag/geometry-input/locked-selection states.

## Verified accurate (no change needed)

| Surface | Check performed |
|---|---|
| `shortcut_reference()` consumers | Registry inventory re-derived offscreen; every listed key matches a `default_command_definitions()` entry (Ctrl+S/Z/Y, M/R/F/Home/Esc/Enter, Ctrl+D, X/Y/Z, Ctrl+A/I, Delete, H/L/T) |
| Context-menu shortcut badges (room + optimization) | Render `definition.shortcut` live from the registry — cannot drift |
| Shell QShortcuts | `QShortcut('Ctrl+K')` palette, `Alt+Left/Right` nav history, `F1` help — all bound in code, all listed in the dialog |
| Palette usage text | "Esc で検索語を消去、もう一度 Esc で閉じます" matches `_escape_pressed` (clear-then-close) |
| Viewport nav hint + 吸着 tooltip | "中ボタン=画面移動 / Shift+中ボタン=回転 / ホイール=拡大縮小 / 右クリック=メニュー" matches `pointer_gesture_for` (Middle→PAN, Shift+Middle→ORBIT, wheel→zoom, right→menu); "Shiftで一時解除" matches `_snap_enabled` |
| Underlay import status guidance | "ビューメニューから「2点で校正」" — `2点で校正…` exists in the view-menu underlay submenu |
| Onboarding link table (機器の準備) | "ダブルクリックすると対象のページに移動" — `itemActivated` → `_onboarding_step_activated` jumps to assignment/campaign |
| First-run path | Empty library → `resolve_startup_document` creates `My Home Theater` and opens it; project page empty-label "「新規プロジェクト…」から作成できます" → button → `CommissioningWizard`; honest and discoverable |
| ~160 tooltips/statusTips across workspaces | Sampled every `setToolTip` site in bulk, then verified non-trivial claims: gating reasons render `availability.disabled_reason` live; robustness `1+2*len(axes)` sample count matches the stencil; "1回のUndoで全位置を戻します" matches `apply_candidate_positions` (one `transform_entities` command); REW cancel tooltip correctly discloses cancel-discards-result semantics; intervention-family gating checkbox claims match `_family_checks` capability evaluation |
| Placeholder texts | All parse-claimed formats verified against real parsers: aisles `seat:width`, screen-transfer `freq[,angle[,mag[,phase[,reflection]]]]`, impedance/band point tables, material layer fields, filter fields match all columns |
| PENDING preference keys tooltip | All 11 pending keys grep-verified consumer-free — "まだ実装されていないため、現在は変更できません" honest |
| Help topics (`help_registry`) | All 17 topics: reason codes exist, content is conceptual (no stale key claims); glossary generated from `HTDT_TERMINOLOGY` |
| Help palette destinations | `help.shortcuts` / `help.palette` / `help-topics:*` all resolve to real dialogs |
| Legacy `--legacy-ui` windows | Own QAction shortcuts (W/R/Ctrl+D/Delete) internally consistent with their own status strings — standalone shells, no drift |
| JP tone | Consistent established voice (JP UI + retained domain nouns like authority/SceneRevision) |

## Deferred / intentionally not changed

- Mouse gestures (middle-drag pan etc.) are documented in the viewport hint label, not the keyboard dialog — `ショートカット一覧` scope stays keyboard, matching its title.
- Mixed `Undo`/`元に戻す` usage: "元に戻す" is the action label while "1回のUndoで…" describes undo steps — established convention, left as is.
- No doc/URL links exist in-app (no `QDesktopServices.openUrl` to docs/ or web) — nothing to validate.

## Files changed

- `backend/src/htdt/workflow_help.py` — registry rows via `shortcut_reference()` (aliases restored); arrow-key nudge line added to the non-registry block.
- `backend/tests/test_workflow_help.py` — new test asserting alias rows and the nudge line render.

## Tests

`TMPDIR=/c/t PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen C:/devin/python/python.exe -m pytest backend/tests -q -n 4`

Full suite: green except one pre-existing parallel-order flake —
`test_cad_hybrid_prediction_provider.py::test_evidence_lifecycle_rejects_illegal_promotions`
fails under `-n 4` on a clean `main` worktree too (verified) and passes
serially; unrelated to this change (workflow_help vs capture/prediction
provider). `test_workflow_help.py`: 4/4 pass.
