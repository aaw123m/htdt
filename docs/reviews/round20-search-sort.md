# Round 20 — Search / Filter / Sort / Pagination Truth (REV20-SEARCH)

Scope: every list surface that *promises* an affordance — a typed search box,
a filter control, a clickable sort column, a paged result, a count label —
must keep that promise when exercised with constructed datasets. Round 14
seeded this dimension (`test_round14_search.py`); this round re-verified the
whole surface at round-20 depth and found three new verified gaps.

## Enumeration (interactive surfaces)

| Control | Widget | Mechanism audited |
|---|---|---|
| 位置候補 filter | `search_candidate_filter_field` | `_apply_search_candidate_filter` → `_candidate_matches_filter`, page-scoped `filter_note` disclosure |
| 拡張候補 filter | `extended_candidate_filter_field` | `_apply_extended_candidate_filter` — same shared helper |
| 位置候補 sort | `search_candidate_tree` (候補/番号/位置) | `CandidateTreeItem.__lt__`, `sortItems(1, Asc)` pinned |
| 拡張候補 sort | `extended_candidate_tree` (候補/元候補/位置/音響 yaw/筐体 yaw) | same `CandidateTreeItem`; previously defaulting to column-0 hash order |
| Command palette | `command_palette` box | `palette_search._normalized` = NFKC + casefold + strip, dedup by id, stable desc score, limit 24 |
| Candidate pagination | `前の候補`/`次の候補` + 拡張 equivalents | `generate_cad_candidates`/`generate_extended_candidates` offset slicing over cached deterministic enumeration, `candidate_set_sha256` anchor, `iter_cad_candidate_pages` |
| Project list | `ProjectLibraryPage` | repo order `last_opened DESC, created_at DESC, rowid DESC` — no sort/search promised |
| Capture inbox | `CaptureInboxPage` | `ORDER BY first_arrived_at_utc, lineage_digest` (FIFO arrival order, tiebreak present) |
| Activity | `ActivityPage` ops/events/revisions | `reversed(recent(30))` newest-first, `ORDER BY seq DESC LIMIT 50`; subtitle claims 最新順 — holds |
| Revision history | `RoomHistoryPanel` | `list_revisions ORDER BY seq ASC`, summary count = row count |
| API list endpoints | `main.py` `list_*` | `_page` slice over deterministic `ORDER BY … id DESC`, `Query(ge=0, le=MAX)` |
| Commissioning wizard picker | `existing_combo` | `SELECT … FROM scene_document_heads` — previously **no ORDER BY** |
| Authority inspector | `node_combo` + lineage lists | `_sorted_nodes` (domain, label, node_id) — deterministic |
| Equipment / reference library | definition lists | `ORDER BY seq ASC` (insertion order — deterministic) |
| Measurement lists & pickers | REW list, comparison combos, dataset combos | `imported_at DESC, measurement_id` / `created_at DESC, comparison_id` tiebreaks; selection preserved across rebuilds |
| Entity trees | `native_editor`, `RoomObjectsPanel` | document order / fixed kind-group order + honest 件数 labels |
| Backup pickers | retention/generation combos | `(created_at, name) DESC` deterministic merge |

## Verified failures → fixes

| # | Surface | Verified failure (constructed data) | Fix |
|---|---|---|---|
| 1 | `search_candidate_filter_field` + `extended_candidate_filter_field` | Filter normalized with `casefold()` only while the command palette uses NFKC+casefold. Typing **１２** (full-width digits) did **not** match `候補 12`; **ｽﾋﾟｰｶｰ** (half-width katakana) did not match **スピーカー** — two different search contracts in one app. | `_normalized_text` (NFKC + casefold + strip, palette-identical) now normalizes both the needle (at both `_apply_*` call sites) and the joined row text inside `_candidate_matches_filter`. |
| 2 | `CandidateTreeItem.__lt__` (both candidate trees) | Comparator compared trailing numbers only when *both* cells had one and fell back to text otherwise — a per-pair rule switch that is not a total order. Empirical check: the same 6-item set sorted by Qt produced **5 distinct orders across 720 insertion orders**. On the extended tree this is reachable daily (truncated hash ids randomly end in digits or not). | `_candidate_sort_key` maps every cell to one tuple: explicit sort key → trailing number → NFKC-folded text. Total order, insertion-proof, numbers still sort as numbers, equal numbers break deterministically on text. |
| 3 | `extended_candidate_tree` default view | `setSortingEnabled(True)` with no pinned sort left the tree sorting column 0 (opaque truncated candidate ids) — and on this build the initial sort direction is *Descending*. First render showed hash-ordered rows in reverse, not the canonical enumeration order the page metadata describes. | Each row now carries the canonical enumeration index (`page.offset + row_index`) under `_CANDIDATE_SORT_KEY_ROLE` on column 0, and `sortItems(0, AscendingOrder)` is pinned at construction — default view = generation order, ascending/descending both honest. |
| 4 | `CommissioningWizard._other_documents` | `SELECT document_id FROM scene_document_heads` without `ORDER BY` returns insertion order — verified `('doc-z','doc-a','doc-m')` for ids inserted in that order. The 既存プロジェクト picker's order depended on creation history. | `ORDER BY document_id` — deterministic alphabetical listing. |

## Verified honest (no change needed)

- **Pagination bounds/counts.** `generate_*_candidates` enumerate deterministically and select `offset <= feasible_index < offset + limit`; pages carry `raw/feasible/rejected/duplicate` counts rendered verbatim in the summary label (`総候補 · 有効 · 除外 · 重複 · 表示 X–Y`); prev/next buttons enable exactly when a page exists in that direction; `iter_cad_candidate_pages` walks `offset += len(page.candidates)` under a single `candidate_set_sha256` — no drops/dupes at boundaries (already pinned by `test_cad_candidate_pagination.py` over >500-candidate sets).
- **Filter scope disclosure.** `search_candidate_filter_note`/`extended_candidate_filter_note` state `このページ内で N 件一致` and the cross-page caveat — a zero-match read never claims "no such candidate exists".
- **Filter-vs-selection.** Hidden rows keep `*_selected_candidate_id`; filtering is view-only, preview/apply still target the same candidate.
- **Filter persistence.** Filter text survives page regeneration and applies to each new page — visible, declared behavior (the note says scope is the loaded page).
- **Count labels.** `RoomObjectsPanel` (オブジェクト/選択/非表示/ロック counts), `RoomHistoryPanel` (`N リビジョン · ラベル M 件`), candidate summaries, and the live match note all equal the rows actually shown.
- **Deterministic repository orders.** Audited every `ORDER BY` — seq/rowid/timestamp+unique-id tiebreaks are present everywhere rows reach a list (REV19-SWEEP2 swept most of these; `scene_document_heads` in the wizard picker was the leftover).
- **Palette search.** NFKC+casefold on query and fields, AND-tokens, per-id dedup keeping max score, stable descending sort, 24-result cap, honest '該当する項目がありません' empty row, '最近使用→ナビゲーション' suggested grouping.
- **Bounded lists.** Activity tables cap at 30 ops/50 events/50 revisions with a 最新順 subtitle — boundedness is the contract; candidate pages cap at `search_page_limit` (250) with prev/next reachability; no list surface renders unbounded 500+ rows unvirtualized.
- **API pagination.** `_page` is a plain slice over fully materialized deterministic ordering; `offset`/`limit` bounds enforced by `Query(ge=0, le=MAX_LIST_PAGE_SIZE)`; out-of-range offsets return `[]` honestly.

## Deferred / notes

- `ActivityPage`'s リビジョン履歴 filters `detached = 0` silently — detached
  (branched) revisions exist in authority but never appear in the activity
  view. They *are* visible in `RoomHistoryPanel` with a `◇detached` marker,
  so this is a scope choice, not a hidden lie; a "mainline only" disclosure
  label would be nicer still but is cosmetic.
- `commissioning_wizard._other_documents` reads the private
  `repository._connect()` — pre-existing seam, left as is (minimal-diff).

## Tests — `backend/tests/test_round20_search.py`

- `test_search_filter_matches_full_width_digits`, `test_extended_filter_matches_full_width`,
  `test_candidate_matches_filter_normalizes_both_sides` — the NFKC contract
  end-to-end (field → filter → visible rows) plus unit-level both-sides.
- `test_candidate_sort_total_order_insertion_proof` — 48 insertion
  permutations of the pathological mixed set produce exactly one order,
  equal to the canonical sort-key order.
- `test_candidate_sort_numeric_with_text_tiebreak` — numeric type
  awareness + deterministic tiebreak.
- `test_extended_tree_default_order_is_enumeration` — default view is the
  canonical enumeration order; asc/desc toggles reverse it exactly.
- `test_extended_tree_groups_base_candidates_when_sorted` — 元候補 text
  sort groups shared bases.
- `test_wizard_existing_project_combo_is_sorted` — shuffled insert order
  still yields an alphabetical picker.
- `test_search_filter_note_counts_visible_rows` — disclosed count equals
  visible rows after the normalized filter.

All green under `QT_QPA_PLATFORM=offscreen` with `-n 4`; round-14 search,
extended-search, workflow-workspace, wizard-navigation, dialogs, T18-UX and
candidate-pagination suites re-run clean (106 tests).
