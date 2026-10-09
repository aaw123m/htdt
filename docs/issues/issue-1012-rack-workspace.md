# Issue #1012 — ラック配置ワークスペース（2D RUエレベーション + 適合/競合表示）

## Scope

The #562 rack infrastructure authority (`cad_rack_infrastructure.py`) had
full models and evaluation — `RackDefinition`, `EquipmentPowerProfile`,
`RackLayout`, `evaluate_rack_fit`, `summarize_load`,
`summarize_endpoint_loads`, `summarize_heat` — but no GUI surface. This
adds `RackWorkspacePanel` on the room workspace placement page so a
first-time designer can see, on one screen, which RU rows are free,
which device does not fit, and what a proposed move would change —
before anything is written.

Lives in `backend/src/htdt/rack_workspace.py`; tests in
`backend/tests/test_issue_1012_rack_workspace.py`. Mounted in
`room_workspace.py` (`placement_layout`, refreshed on placement-context
switch) and bound to the display-length policy in
`workflow_application.py`. Repository gains `list_rack_definitions` /
`list_rack_layouts` read helpers in
`cad_feature_authority_repository.py` — no schema change (append-only
`cad_rack_definitions` / `cad_rack_layouts` already existed).

## Elevation shape

`RackElevationView` — a painted 2D elevation built ONLY from
`RackDefinition` / `RackLayout` / `EquipmentPowerProfile`:

```
 前面 (front)                背面 (rear, mirrored)
┌─────┬──────────┬──────────┬─────┐
│ RU24│          │          │     │   ← RU gutter, 1-based, RU 1 at bottom
│ RU23│          │          │     │
│  .. │  amp-1   │          │     │   ← device block = declared ru_height rows
│ RU5 │  4RU     │          │     │
│ RU4 │          │          │     │
│  .. │          │          │     │
│ RU1 │          │          │     │
├─────┴──────────┴──────────┴─────┤
│ 棚板レーン: [棚板A: appliance-1]  │   ← shelf placements (ru_position=None)
├─────────────────────────────────┤
│ 奥行き整合 (per device):        │
│ amp-1    0.30m ≤ 0.45m  合格     │   ← depth row, UNKNOWN stays blank-ish
└─────────────────────────────────┘
```

- RU rows come straight from `RackPlacement.ru_position` (1-based,
  bottom RU = 1) and `EquipmentPowerProfile.ru_height`; a device with no
  declared `ru_height` renders as one hatched row — never stretched.
- With `ru_capacity=None` the grid spans only occupied rows — undeclared
  capacity never invents an extent.
- Shelf-only placements (`shelf_label`, `ru_position=None`) render in a
  shelf lane below the faces so wall-mounted racks stay readable.
- Front and rear faces paint side by side; the rear face mirrors
  left/right as rack drawings do. Overlapping devices share a face slot
  side-by-side so both parties stay visible.
- Drag on a device block emits `movePreviewRequested(device_id, ru)` —
  the ghost block follows the cursor; Esc or release-cancel discards it
  with zero writes. `block_rect` / `device_at` / `ru_at` are exposed as
  test seams.

## Fit surfacing

Every row of the fit table and every painted block is colored from
`evaluate_rack_fit` output verbatim — the widget never re-derives fit:

- Columns: 機器 | 位置 (RU n–m または 棚板) | RU占有 | 奥行き | クリアランス |
  理由 (conflicts + reasons joined).
- `PASS/FAIL/UNKNOWN` map to 合格/不合格/不明 through
  `ui_theme.SemanticState` colors; FAIL cells carry the error color,
  conflict rows are highlighted on ALL columns — for BOTH parties
  (`DeviceFitResult.conflicts` is symmetric in the authority).
- `ru_occupancy` covers double-booking and capacity overflow;
  `depth_fit` covers `chassis_depth_m > usable_depth_m`;
  `clearance_fit` covers front/rear/service requirements vs the rack's
  declared clearances. Any undeclared input yields `UNKNOWN` — the panel
  never paints a guessed PASS onto a blank panel.

## Move flow — preview → re-evaluate → confirm → explicit commit

Drag (or the 移動プレビュー row: device combo + RU spinbox) builds a
candidate `RackLayout` in memory only:

1. `evaluate_rack_fit(candidate, rack, devices)` re-evaluates the
   proposed layout.
2. `RackMoveConfirmDialog` shows the placement diff (device, RU n → RU
   m), the before/after fit verdict, and any conflicts the move would
   create — with the commit button labeled レイアウトを更新.
3. Accept → `_commit_layout` re-verifies the candidate is still
   "current layout + exactly one moved placement" on the selected rack
   (`_candidate_matches_current`); if the rack or layout changed since
   the preview, the commit is refused and the user is told to re-check.
4. Commit writes a NEW append-only `cad_rack_layouts` row via
   `save_rack_layout` (content-keyed `rack-layout:<digest>`); the
   latest row becomes current. No silent mutation ever — cancel,
   unknown dims, drag-out-of-grid, and double booking all leave the
   persisted layout untouched, and a conflicting move still requires
   the same explicit confirm so the designer sees the booking before
   writing it.

## Summaries — honest labels

Power/heat summaries render for the CURRENTLY evaluated layout only:

- 負荷タブ: `summarize_load` per scenario — scenario selector lists only
  scenarios whose `device_ids ⊆ layout placements`; the segmented bar +
  known/unknown totals label derived vs declared values.
- 端子タブ: `summarize_endpoint_loads` tree when session-supplied
  endpoints/assignments match the layout.
- 熱タブ: `summarize_heat` totals, labeled 記載値/導出値 honestly.
- `EquipmentPowerProfile` / `CircuitEndpoint` / `CircuitAssignment` /
  `ElectricalScenario` have no persisted table — the panel takes them
  via `set_authority_context(...)` or a fail-closed
  コンテキストJSONを読み込む import, and the header always states the
  source: `コンテキスト: …（テスト注入・未保存）` or
  `コンテキスト: 未提供（プロファイルの無い機器は不明として表示）`.

## Reachability

The whole panel lives inside the placement `QScrollArea` — elevation
keeps its natural size and scrolls at narrow widths (≥72px policy
controls, `Ignored` size policies where the surface allows); every
interactive control carries an accessible name (`_unnamed_controls` UIA
check). No electrical/building/cooling compliance claims are made —
fit rows state what the authority evaluated, labeled as such.

## Tests

`backend/tests/test_issue_1012_rack_workspace.py` — 11 tests:
RU numbering (1-based grid, RU 1 at bottom, capacity-bounded and
occupancy-bounded extents), both-parties conflict highlighting in the
elevation set and fit table, per-column depth/RU FAIL surfacing,
UNKNOWN honesty for undeclared height/depth/clearances,
preview→confirm→append-only commit with a refuse-on-diverged-candidate
guard, cancel-writes-nothing, context-JSON import labeling, narrow
(360px) layout, and UIA accessible-name coverage.
