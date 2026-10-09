# Issue #1025 — キャプチャ保持管理：削除候補の検索・グループ化一覧

## Scope

The 設定 → 保持管理 panel's deletion-candidate picker was a single flat
QComboBox (series id, 8-char revision id, import count, last date) — with
hundreds of revisions and colliding id prefixes, picking the wrong series or
revision was a real risk. The picker is now a searchable/sortable/groupable
`QTreeWidget` fed by an extended listing, and the dry-run report is itemized
per category so a blocked or shared revision can never look "safe".

All changes are in `backend/src/htdt/capture_retention_ui.py` (UI) and
`backend/src/htdt/capture_retention.py` (listing fields only). No purge
authority changed: `plan_capture_revision_purge`, the in-transaction
re-verification in `purge_capture_revision`, and the explicit confirmation
dialog are untouched — the list is a view concern over the same store reads.

## Vocabulary

| Term | Meaning |
|---|---|
| `_revisions` | The full `list_capture_revisions()` result for the current project — order is store order, never visual order. |
| `_REVISION_ROLE` | `Qt.ItemDataRole.UserRole` slot carrying the exact `capture_revision_id` on every leaf row; the ONLY row→revision resolution path. |
| `_pinned_revision_id` | The revision id the user last selected explicitly. Filter/sort/group/refresh re-resolve the row BY ID — a selection never drifts to a neighbouring row, and nothing is auto-selected. |
| `_planned_revision_id` | The id a successful dry-run armed. Any scope change (search text edit, refresh) or selection change disarms it; sort/group regrouping alone does not — the referenced set is unchanged. |
| Selection note | `selection_note` states why the pinned revision is not shown: `絞り込み条件で非表示です` (exists but filtered) or `現在の一覧にありません（削除済みまたは別プロジェクト）` (vanished). |
| Honest count | `count_label` renders `表示 N 件 / 全 M 件` — hidden candidates are stated, never silently dropped. |
| Group headers | Non-selectable `series-…（N 件）` / `doc-…` / `YYYY-MM` / `（プロジェクト未割当）` parent rows; leaves always carry the full revision id, never the ambiguous 8-char prefix. |

## Evidence model

- **Id-keyed, never index-keyed.** Every leaf stores
  `capture_revision_id` on `_REVISION_ROLE`; selection, arming and purge
  all resolve through it. Filtering, sorting and grouping rebuild the
  visual order — they cannot move the selection.
- **Project assignment is exact authority only.** Grouping/filtering by
  プロジェクト uses `assigned_document_ids`, resolved upstream via
  `capture_semantic_promotions.scene_revision_id → scene_revisions.
  document_id` — an actual FK join. Revisions with no promotion land in
  `（プロジェクト未割当）`; nothing is guessed from series ids or names.
- **Itemized dry-run, never a verdict word.** `_plan_summary_lines`
  renders every category the plan computes: リンクされた証拠
  （削除対象 / 共有のため保持）, 削除対象レコード (bindings / 権威 /
  座標 / ルームプラン), 保持義務, 回収可能 bytes (+ blob count), and
  参照・再利用先 listing up to 8 blocking dependents with kind labels.
  `削除可能` appears only as the plan's own status string — a `blocked`
  or `absent` plan says so explicitly and never arms the purge button.
- **Purge stays re-verified.** The button is enabled only while
  `_planned_revision_id` equals the current selection and no busy gate is
  up; `purge_capture_revision` re-plans inside its transaction exactly as
  before, and the confirmation dialog (Cancel as default) is unchanged.
- **Unknown/blocked/referenced rows are inert.** They list and describe,
  but the dry-run reports their status and the purge button never arms —
  unset-policy or blocked candidates cannot be promoted into deletion.
- **Empty states say so.** 0 revisions → `登録済みのキャプチャリビジョン
  はありません`; filtered-to-zero → `絞り込み条件に一致するリビジョンは
  ありません`; both leave the count honest.

## Surface

- Toolbar row: search (`リビジョンID・シリーズ・プロジェクトで検索`,
  matches id/series/document ids/date/counts/formatted bytes), グループ
  combo （なし / シリーズ / プロジェクト / 取り込み日）, ソート combo
  （最終取り込み / リビジョンID / シリーズ / 取り込み件数 /
  リンク証拠サイズ） + 表示件数 label.
- Tree: 6 columns — リビジョン (full id), シリーズ, プロジェクト,
  取り込み, 最終取り込み(UTC), 証拠・サイズ — group headers expand by
  default and are not selectable.
- Buttons: `ドライランで削除内容を確認` enabled iff a leaf is selected and
  not busy; `このリビジョンを削除` additionally requires the armed plan to
  still name the selected id.
- `refresh()` re-reads the store, re-pins the selection by id (or explains
  why it vanished) and always disarms any computed plan — new imports or
  deletions may have changed the underlying references.

## Tests

`backend/tests/test_issue_1025_retention_search.py` (17 tests, offscreen):

- **Listing fields**: evidence count/bytes join (7-item fixture),
  exact promotion→document assignment, un-promoted sibling stays
  unassigned, 1000-revision listing via direct run-table seeding.
- **Widget**: filter narrows and keeps the pinned id; hidden pinned
  selection shows the note and auto-selects nothing; sort and group keep
  the pin; colliding 8-char prefixes resolve by full id and dry-run the
  exact row; project grouping emits document/unassigned headers.
- **Plan discipline**: itemized text, scope edit disarms, refresh disarms
  but keeps selection, vanished revision clears selection without moving
  it, blocked/absent never arms, busy gate blocks plan+purge, confirmed
  purge calls `purge_capture_revision` with only the selected id,
  project switch (two repositories) cannot carry a stale selection.
- **Scale/layout**: 1000 rows filter+pin+regroup; 360×640 window keeps
  all controls reachable and every interactive control named
  (`_unnamed_controls` sweep).

`test_review_round8_capture_retention_ui.py` and
`test_round14_dialogs.py` updated for the tree API (`select_revision`,
`listed_revision_ids`); the `test_accessible_labels.py` standalone-dialog
sweep now covers the widget as mounted.

## Honest gaps

- Date grouping buckets by UTC calendar date of `latest_recorded_at_utc`
  (`YYYY-MM`) — a wall-clock grouping, not a retention-policy concept.
- The 参照・再利用先 itemization lists at most 8 dependents with a
  `…ほか N 件` tail — the full set remains in the plan object.
- DPI-200 is covered by the narrow-window + UIA sweep and the
  minimum-height/resize policies; a dedicated per-DPI geometry run was
  not parametrized.
- `test_accessible_labels.py` intermittently hard-crashes this Windows
  box's interpreter mid-file (pre-existing, identical without this
  change); the standalone-dialog test covering this widget passes
  individually.
