# Round 14 — List / Search / Filter / Sort Truth

Scope: every QListView/QTableView/QTreeView/model + search field + filter combo + sort control in the shipped app must do what it presents. Verified by populating real model data and asserting view contents under `QT_QPA_PLATFORM=offscreen` (`backend/tests/test_round14_search.py`).

## Enumeration

The complete interactive surface (record lists with no sort/filter affordance are excluded — they render what the model holds, verified honest):

| Control | Widget | Mechanism |
|---|---|---|
| 位置候補 list | `search_candidate_tree` (QTreeWidget, 3 cols: 候補/番号/位置) | `setSortingEnabled(True)`, `CandidateTreeItem.__lt__`, page of `search_page_limit` (250) |
| 位置候補 filter | `search_candidate_filter_field` (QLineEdit) | `_apply_search_candidate_filter` — casefold match over row text, hides non-matching items |
| 拡張候補 list | `extended_candidate_tree` (5 cols: 候補/元候補/位置/音響 yaw/筐体 yaw) | same `CandidateTreeItem`, same paging |
| 拡張候補 filter | `extended_candidate_filter_field` | `_apply_extended_candidate_filter` |
| Field explorer 断面/断面位置 | `plane_combo`, `coordinate_combo` | combos drive `extract_field_slice` |
| Command palette | `command_palette` search box | `palette_search` NFKC + casefold, AND-tokens, disabled-last |
| Activity 最新順 lists | `ActivityPage` operations/events/revisions | `recent()` chronological + `ORDER BY seq DESC` |

Unsorted record trees (pareto / axis / linked / spec / measurement_plan / campaign / validation / adaptive / objective_list / measurement_match_list / comparison / robustness / room revisions / project library / capture inbox / reference library / measurement list) have no sort or filter affordance — audited and honest (order = repository/chronological order, documented empty labels where present). Picker combos (measurement compare, project pickers) store exact ids in `UserRole` — honest.

## Findings & fixes

| # | Control | Verified failure | Fix |
|---|---|---|---|
| 1 | `CandidateTreeItem.__lt__` (both candidate trees) | Sorting any column ≠ 番号 → `RecursionError` crash: `super().__lt__(other)` re-enters the Python override under PySide6. Also 候補 column sorted "候補 10" < "候補 2" lexically. | Compare own text per column, no `super()` call; 候補/番号 columns compare on trailing integer so 候補 2 < 候補 10. `optimization_search_controller.py` |
| 2 | `search_candidate_filter_field` / `extended_candidate_filter_field` | Filter only inspects the loaded 250-row page but the view claimed nothing about scope — "no matches" looked like "no such candidate" and matches elsewhere were silent. | Added `search_candidate_filter_note` / `extended_candidate_filter_note` labels under each filter: live match count, honest "このページに一致する候補はありません" zero state, and "他のページにある可能性があります" when more pages exist. Hidden while the filter is empty. |
| 3 | Field explorer 断面 combo | `currentIndexChanged → _refresh_view` skipped `_refresh_coordinates`: switching plane left stale fixed-axis coordinates in 断面位置 → wrong-axis slice or '断面を表示できません'. | `plane_combo` now connects to `_plane_changed` → `_refresh_coordinates()` then `_refresh_view()`. `field_explorer_panel.py` |

## Verified honest (no change needed)

- **Filter correctness**: `_candidate_matches_filter` joins all column texts casefolded — hides non-matching rows only; hidden rows keep `search_selected_candidate_id` (filter is view-only, never deselection).
- **Stale-view**: `_refresh_search_candidate_tree` / `_refresh_extended_candidate_tree` re-call the filter after repopulating — filter survives page regeneration and row changes.
- **Empty results**: command palette inserts a non-selectable '該当する項目がありません' row; candidate filters now show the scoped zero state.
- **Case/locale**: palette `_normalized` = `unicodedata.normalize('NFKC').casefold().strip()` — verified 'FL'/'fl'/'ＦＬ' all find `entity:sp-fl`; JP '部屋' hits room entities; 'シート' correctly does not match speaker entities (no hidden-field leakage).
- **Pagination**: both candidate trees paginate with 前/次 buttons; the filter scope is now disclosed (fix 2) so the loaded-page limit is honest.
- **Ordering**: Activity 最新順 = `*active(), *reversed(recent(30))` and `scene_revisions ORDER BY seq DESC` (global autoincrement) — genuinely chronological across documents.
- **Sort stability**: candidate sort is deterministic per column; the numeric trap on the 候補 label column is fixed (fix 1). Text columns (位置, yaw) compare lexically — honest for formatted position strings like "(1.0, 1.0, 1.0)".

## Tests

`backend/tests/test_round14_search.py` — 8 tests: numeric label sort asc/desc, sort round-trip across columns (recursion regression), page-scope disclosure + zero state (250-loaded/600-feasible), single-page count state, extended filter+sort, field-explorer plane switch repopulation, palette NFKC/case/width insensitivity, palette empty state.
