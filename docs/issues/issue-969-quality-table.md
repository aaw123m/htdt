# Issue #969 — 測定品質テーブル：組合せ絞り込み・選択詳細・適応レイアウト

## Scope

The 測定 → 品質 page's 保存済み測定 table (10 columns, one row per saved
measurement) gains combinable filters with honest counts, an id-keyed
selection model that survives filtering/sorting/refresh, and an adaptive
detail layout that stacks the selection detail under the table on narrow
or high-DPI windows instead of demanding horizontal scroll.

All changes are in `backend/src/htdt/measurement_page_workspace.py` (UI),
with filter help text in `measurement_explanations.py`. No authority or
storage change — filtering is a view concern over the already-fetched
`MeasurementView` listing; verdicts and evidence stay sealed upstream.

## Vocabulary

| Term | Meaning |
|---|---|
| `_quality_views` | The full fetched listing for the current document — order is store order, never visual order. |
| `_quality_views_by_id` | measurement_id → `MeasurementView` map; the ONLY row→record resolution path. |
| `_filtered_quality_views` | The listing after applying verdict/state/channel/position/search — AND-intersected. |
| Verdict filter | 品質セルの値で絞る: every `quality_status` present in the listing plus a `検証エラー` sentinel for `dataset_error` rows (a dataset read failure is not a quality_status). |
| 状態 facet | Derived per-row attention flags: 要対応 (any of 検証エラー/品質invalid/要再測定/レポート再評価/チェック不合格/除外), 要再測定 (`RETAKE`), レポート再評価 (`stale`/`error`), 校正が未確認・不合格 (evaluated calibration check ≠ PASS — missing evidence never claims uncalibrated), 検証エラー, 通常利用から除外. |
| Honest count | `quality_count_label` renders `N件中M件を表示` — hidden rows are stated, never silently dropped. |
| Selection note | `quality_selection_note` states why a previous selection is not shown: `絞り込み条件で非表示です` (exists but filtered) or `現在の一覧にありません（別のリビジョンまたは削除済み）` (vanished). The selection is never silently moved to a different measurement. |
| `_QUALITY_DETAIL_STACK_MIN_WIDTH` | 1180 px **viewport** width — below it `quality_split` goes Vertical (detail under the table). Chosen above the ~1160 px side-by-side minimum (both cards' minimum widths + margins) so horizontal mode never needs an h-scrollbar of its own. |

## Evidence model

- **Id-keyed, never index-keyed.** Filtering *and* column sorting
  (`setSortingEnabled(True)`) decouple the visual row index from
  `_quality_views` order. Every item stores `measurement_id` on
  `Qt.ItemDataRole.UserRole`; `_quality_row_index_for_id` /
  `_quality_view_for_row` resolve through it — a selection cannot drift to
  a different measurement after sort, filter or `refresh()`.
- **Rows always match the sort indicator.** After repopulating, refresh
  re-applies `horizontalHeader()`'s indicator via `sortItems` — otherwise
  the first user click only moves the indicator while the order lags one
  click behind (observed on the real GUI, now covered by a test).
- **Adaptive layout keys on the viewport, not the host.** The page host
  cannot shrink below its own minimum size (~1160 px), so its width never
  signals "narrow". `_relayout_quality_split` reads the scroll area's
  `viewport().width()` on Resize events via `eventFilter`; in vertical
  mode the host's minimum collapses to the wider card (~630 px), which
  fits ~640/900/1100 px windows without horizontal scrolling of the table.
- **Deep links reach their row.** `focus_entity` /
  `select_measurement_id` clear active filters when the target row is
  filter-hidden rather than selecting invisibly.
- **Filter choices are honest.** Verdict/channel/position combos rebuild
  from the current listing — a value that disappeared resets to すべて
  instead of filtering on a phantom key.
- **Empty states say so.** 0 total → `保存済み測定はありません`;
  filtered-to-zero → `絞り込み条件に一致する測定はありません`.
- **Selection detail keeps the strict identity.** The detail header now
  leads with `測定ID: <id>`, then quality rationale, evidence lines,
  missing evidence and the retake/disposition next-action path — never a
  single good/bad score.
- **JA accessibility.** Every filter control, the count label, the table
  and the detail region carry JA `accessibleName`s; the detail is
  `StrongFocus` + keyboard-selectable for screen-reader/tab flows. Field
  help is registered as `quality.filter.*` explanations (WhatsThis).

## Surface

- Filter row: 品質 → 状態 → 入力 → 測定位置 combos; search row:
  free-text (`測定ID・測定位置・入力役割で検索`, JA role labels included in
  the haystack) + 絞り込みをクリア + count.
- Combo `currentIndexChanged` and search `textChanged` re-filter
  in-process over `_quality_views` — a keystroke never re-verifies the
  store.
- `refresh()` restores the previous selection by id; if absent but the
  listing is non-empty it selects row 0, and if filtered-to-zero shows
  the honest empty text.

## Tests

`backend/tests/test_measurement_rev72_quality_table.py` (12 tests,
offscreen): combinable filters + counts, verdict/state facets,
JA-aware search, id-keyed selection across sort + refresh, rows match the
sort indicator after refresh, hidden/vanished selection notes, deep-link
filter reset, strict-ID detail + retake guidance, honest empty state,
narrow-width relayout (900 → Vertical, 1280 → Horizontal), a11y names.

## Honest gaps

- The 640/900/1280 × DPI 100-200 % matrix is exercised logically
  (viewport-width → orientation) and on the real GUI at ~900 and ~1280;
  per-DPI offscreen geometry sweeps are not separately parametrized.
- The 状態「校正が未確認/不合格」facet only counts *evaluated* calibration
  checks that did not pass; a measurement with no calibration evidence at
  all surfaces under 要対応 via its other flags, not this facet.
- #936's「要再測定の1件を探す」操作数 is not instrumented in code — the
  状態=要再測定 facet + count label makes it a two-control path
  (combo → read row), measured manually on the real GUI.
