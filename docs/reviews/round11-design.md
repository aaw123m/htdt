# Round 11 — Visual & design consistency audit

Scope: design breakage the test-driven rounds cannot see — JP text
truncation/elision, dialog sizing, button-row overflow, hardcoded widths,
missing size policies, unreadable combos, crammed tables, padding
inconsistency, disabled-looking-enabled, error text overflowing dialogs,
hardcoded colors that defeat the theme. Branch `devin/rev11-design`. No
GitHub Actions — verified locally with pytest and offscreen renders.

## Method

Every major surface was instantiated under `QT_QPA_PLATFORM=offscreen` at
1366×768 and 1024×768 (Windows CJK fonts loaded into the empty offscreen
font database — `YuGothR.ttc` etc. — so JP glyphs render truthfully), then
grabbbed to PNG and inspected (`scratch/render_qt.py`,
`scratch/render_dialogs2.py`; 58 pre-fix PNGs in `scratch/shots/`, post-fix
verification PNGs in `scratch/shots_v2/`). In parallel, each surface's
`minimumSizeHint` was measured to find clipping that a screenshot alone
under-reports (a `grab()` shows what *was* painted; the hint shows what the
widget *needs*). The SPA was code-reviewed for the same defect classes and
`npm run build` (`tsc --noEmit && vite build`) passes.

## The two structural findings

### 1. The whole window could grow past the screen — permanently

`QStackedWidget.minimumSizeHint` is the **max over all children**, and the
router mounts workspaces lazily — so each tall page a user visited raised
the application window's minimum height *forever*. Measured on a fresh
session at 1366×768:

| After visiting | Shell minimum | Result |
|---|---|---|
| (start) | 529×388 | fits |
| Room | 1366×**950** | overflows a 768px screen |
| Optimization → 比較 | 1366×**1267** | badly overflows |

Offenders: the optimization **比較** page (`minimumSizeHint().height()`
1021 — it was the only page in its scroll chain not wrapped in
`_scroll_page`) and the acoustics tab stack (703 — `QTabWidget` keeps its
tab bar outside the scroll area and each page needed it). The fix wraps
both in `widgetResizable` scroll areas: shell minimum is now **885×544**
with every destination and context mounted — fits 768px screens again.
Post-fix renders: `scratch/shots_v2/v2-opt-comparison.png`,
`v2-opt-robustness.png`, `v2-room-acoustics.png` all show the shell still
at exactly 1366×768 (previously forced taller).

### 2. The right dock silently hard-clipped content

The dock is fixed at 260/280/300px by breakpoint (min 248). Several dock
pages used `ScrollBarAlwaysOff` horizontally, and the geometry/acoustics
panels had **no scroll wrap at all** — any content wider than the dock was
clipped with no way to reach it. Content floors measured:

| Widget | Min width needed | Dock gives |
|---|---|---|
| Standards criterion combos | 641 | ≤300 |
| `Vector3Editor` (inspector) | ~570 (see below) | 248–320 |
| SystemExpansion panel | 436 | " |
| RoomPrediction 環境 combo | 386 | " |
| Video geometry heading | 346 | " |
| Objects 5-button row | 289 | " |

Fixes, smallest first:

- `objects_page`, `placement_panel`, `history_page`:
  `ScrollBarAlwaysOff` → `ScrollBarAsNeeded` — overflow now scrolls
  honestly instead of clipping.
- New `_dock_scroll_page()` helper wraps the geometry and acoustics
  panels in identical scroll pages (previously attached raw — the only
  dock pages with zero scrolling). `set_context` now targets the wrapper
  pages, not the raw panels.
- **`Vector3Editor` had a circular layout bug**: `_relayout` switched to
  its narrow stacked layout below `_WIDE_BREAKPOINT = 278`, but the *wide*
  layout's own ~500px minimum kept the widget at ~570px wide inside the
  dock — so the widget could never observe itself below the breakpoint and
  stayed wide+clipped forever. Now `minimumSizeHint()` reports the narrow
  floor (162px measured) and `_relayout` compares against
  `_wide_minimum()` derived from the groups' hints, so the stacked layout
  actually engages at dock widths.
- Standards `profile_combo`/`target_combo` and prediction
  `model`/`receiver`/`environment` combos: `setMinimumContentsLength(12)`
  + `AdjustToMinimumContentsLengthWithIcon` — long items like
  `標準仮定 (343 m/s, 20 °C) · 343 m/s (nominal…)` no longer set a 386–641px
  floor (dropdown still shows full text; the closed box elides).
- Objects panel's 隠す/表示/ロック/解除/削除 row: `ControlSize.COMPACT`
  on all five buttons.
- Video geometry section heading: `setWordWrap(True)` (was a 346px
  single-line minimum).
- Prediction 診断 checkbox: long inline caption replaced by the short
  `診断: 音源を受音点として使う` + a wrapped note label; the full
  explanation stays in the tooltip.

Remaining overflow is ~25–80px at the 260px compact dock — reachable via
the scrollbar now instead of invisible. A true reflow of the densest form
panels (`RoomVideoPanel`, `SystemExpansionRoomPanel`) into a narrow
variant would be a redesign; deferred as not worth the churn when the
scroll contract is honest.

## Other confirmed defects fixed

- **`TreatmentDefinitionDialog` shadowed `QWidget.width()`/`height()`**
  with `self.width`/`self.height` `QDoubleSpinBox` members — any caller
  doing `dlg.width()` raised `TypeError: 'QDoubleSpinBox' object is not
  callable`. Renamed to `width_m`/`height_m`; only `dialog.values()`
  consumed them externally.
- **Projects table elided timestamps**: only column 0 was `Stretch`, so
  作成日時 (ISO `2026-09-28T1…`) truncated at its fixed default width.
  Non-stretch data columns are now `ResizeToContents` in the Projects
  table and in Activity's 操作 (`更新時刻`) and タイムライン (`時刻`)
  tables — the same defect shape in all three.
- **Hardcoded color**: `authority_inspector_ui.py` painted stale-warning
  text `color: #b45309` — the only untokenized UI-text color in the app;
  now `set_semantic_state(SemanticState.WARNING)` so it follows the theme.
  The splash's brand palette (`#1b1d23`/`#e8eaed`…) is intentionally
  fixed brand chrome, not a defect.

## Per-surface verdicts

| Surface | Verdict | Evidence |
|---|---|---|
| Shell after mounting all workspaces | **Fixed** (was 950–1267 minH; now 544) | `shots_v2/v2-*.png` |
| Room dock — objects | **Fixed** (scrollable; compact buttons) | `v2-room-objects.png` |
| Room dock — placement | **Fixed** (scrollable; heading wraps) | `v2-room-placement.png` |
| Room dock — acoustics | **Fixed** (scroll wrap; combos capped; 診断 wraps) | `v2-room-acoustics.png` |
| Room dock — geometry/history | **Fixed** (scroll wrap / AsNeeded) | `v2-room-geometry.png`, `v2-room-history.png` |
| Optimization 比較 | **Fixed** (scroll page) | `v2-opt-comparison.png` |
| Optimization ばらつき耐性 (round-9 panel) | Clean | `v2-opt-robustness.png` |
| Application pages (プロジェクト/取り込み/アクティビティ/ライブラリ/サポート) | **Fixed** (timestamp columns) | `shots/app-*.png` |
| Settings dialog 860×720 | Clean | `shots/dlg-settings.png` |
| Data Management (backup-policy card, round-10) | Clean | `shots/dlg-settings.png` tabs |
| Command palette 640×460 | Clean | `shots/dlg-palette.png` |
| Startup splash 460×240 | Clean (brand colors intentional) | `shots/dlg-splash.png` |
| Deliverables / lifecycle / measure / recovery dialogs | Clean | `shots/dlg-*.png` |
| `warn_user` message box | Clean (258×114, wraps) | `shots/dlg-warn.png` |
| React SPA (`frontend/src`) | Clean build; 2 nits deferred | `npm run build` pass |

## Deferred (recorded, not fixed)

- SPA `.speaker-row` fixed-px grid relies on the 850px breakpoint fallback;
  harmless but could use `minmax(0, …)` to let tracks shrink before the
  breakpoint.
- SPA workflow-links bar uses `scrollbar-width: none`, hiding the scroll
  affordance on narrow screens.
- `RoomVideoPanel` / `SystemExpansionRoomPanel` still need ~350–440px of
  form width — honestly scrollable now, but a compact reflow variant would
  remove the horizontal scroll entirely.
- Menubar clip point sits around ~700px window width; below that the
  rightmost menus elide (normal behavior, observed while measuring).

## Verification

- `backend/tests/test_round11_design.py` — 6 new regression tests:
  shell minimum ≤1366×768 after mounting every destination+context,
  dock pages are `widgetResizable` scroll areas and context-switching
  targets the wrappers, `Vector3Editor` min width ≤248 (compact dock),
  `TreatmentDefinitionDialog.width()`/`height()` remain callable, the
  prediction combos cap their minimum, project-table data columns are
  `ResizeToContents`.
- Affected suites re-run: `test_room_workspace`, `test_application_pages`,
  `test_room_objects_panel`, `test_standards_workspace`,
  `test_room_prediction`, `test_prediction_interpretation`,
  `test_round10_ux`, `test_round11_design` — all pass (one assertion in
  `test_room_workspace` updated for the scroll wrapper).
- `frontend`: `npm ci` + `npm run build` (`tsc --noEmit` + `vite build`)
  pass on node 20 (the `engines >=22.12` warning is preexisting and
  unrelated).
