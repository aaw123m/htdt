# Issue #1017 — アクティビティ履歴：任意期間・カーソル継続読み込み・出来事インスペクタ

## Scope

The アクティビティ page must let the operator reach every row of a
project's history — the original 50-row fixed listing with no next page,
no date bounds and no per-event typing could silently bury the past.
#1023 landed the surrounding frame (表示範囲 scope, 種類/期間/search,
selection by authority ids, guarded deep links); this change finishes
the residual: operator-entered start/end date bounds, cursor-driven
「さらに読み込む」continuation into deep history, and a read-only detail
inspector that types each event by its evidentiary class.

All changes are in `backend/src/htdt/application_pages.py` (page +
revision keyset reader + inspector) and
`backend/src/htdt/workflow_application.py` (`_make_activity` wiring).
The timeline stays the canonical `CadProjectActivityService.events`
projection — no parallel audit log.

## Vocabulary

| Term | Meaning |
|---|---|
| `期間を指定` (`"custom"`) | Fifth 期間 option next to the #1023 presets. Arms the two `QDateEdit` bounds; any other option disables them so a stale custom range can never silently narrow a preset view. |
| `list_revisions_page(repository, limit, *, scope, document_id, after)` | Keyset reader beside `list_recent_revisions`: `WHERE detached = 0` + scope + `AND seq < ?` when `after` is given, `ORDER BY seq DESC LIMIT limit+1`. Returns `(rows, 'seq:<n>' | None)`. |
| Revision cursor | Opaque `'seq:<n>'` token = the smallest `seq` already returned. New commits land above the cursor, so a continuation never skips or duplicates rows under concurrent writes — an OFFSET window would. Garbage tokens fail closed with `ValueError`. |
| Events cursor | `(occurred_at_utc, event_id)` of the last shown row. The next 「さらに読み込む」locates that pair in the freshly filtered projection and continues after it — arrivals at the top of the list can never repeat or swallow a row. |
| `event_evidence_class(event)` | The read-only typing: `evidence` (default), `assumption` (operator-attested applies: as-built, calibration/preset applied), `historical` (inherited rows, 非ヘッド履歴 detached revisions, superseded captures), `failed` (rejected capture), `note` (project_note). |
| 出来事の詳細 | The inspector frame: 時刻/プロジェクト/種類, 区分, 内容, 詳細, ソース権威 (primary `source_ref` kind + ref_id + sha256 prefix), 相関 (remaining refs), event_id, and a class gloss. Read-only, keyboard-selectable, focusable. |

## Evidence model

- **「メモ」is not evidence.** `project_note` types as `note` — 区分 メモ
  — and its gloss states it plainly: a note is a document, not canonical
  proof (メモはドキュメントであり、正準の証拠としては扱いません). It is
  never rolled up under 証跡.
- **Typing is honest, not decorative.** `assumption` marks records that
  are operator attestations rather than ground truth (as-built saves,
  applied calibrations/presets); `historical` marks rows that are part
  of the record but no longer current (inherited history, non-head
  detached revisions, superseded captures); `failed` marks rejected
  captures. The default is `evidence` — a sealed authority row.
- **The inspector shows the source authority.** Primary `source_ref`
  renders as `kind: ref_id（sha256 …）`; additional refs land in the
  相関 lane so correlated authorities stay visible; inherited rows are
  labelled （継承元の記録）on the event id. Selection — not the current
  cell — drives the panel, so clearing the selection empties it.
- **Deep history is reachable, latest-first.** Both tables still open on
  the newest 50 rows; さらに読み込む extends the window in 50-row steps
  with `全N件 · X–Y件を表示`. The revisions table walks the SQL keyset
  cursor; the timeline walks the filtered canonical projection. A
  5000-row document walks to the last row with no fixed cap.
- **`activity_focus` walks to depth.** A deep link whose target is older
  than the shown window keeps loading more until the row exists, then
  selects it — links to vanished sources still report honest
  not-found; a stalled cursor (no row growth) stops the walk.
- **#1023 invariants kept.** Selection rebinds by
  `(event_id/revision_id, document_id)` across refresh, filters and
  load-more; revision filtering happens in SQL, not the UI; the scope
  contract stays fail-closed (`project` without `document_id`, unknown
  scope, malformed cursor → `ValueError`); 全プロジェクト remains an
  explicit opt-in only.

## Surface

- Timeline filter row: 種類 → 期間 → 検索, then 開始/終了 `QDateEdit`
  bounds (yyyy/MM/dd, calendar popup) that enable only under 期間を指定.
- さらに読み込む button + `全N件 · X–Y件を表示` under both the timeline
  and the リビジョン履歴 table; the button disables at the last row.
- 出来事の詳細 inspector under the timeline: nine read-only,
  keyboard-selectable labels; clears when the selection is empty.

## Tests

`backend/tests/test_issue_1017_activity_history.py` (24 tests,
offscreen): custom-range bounds inclusive/reversed/preset-disabled;
222-event walk to the oldest row with no duplicates under a mid-browse
arrival; 221-revision keyset walk, stability under concurrent commits,
garbage-cursor rejection; evidence-class mapping over all 26 kinds;
inspector population, correlation lane, empty-selection clear,
read-only labels; selection survival across load-more + refresh;
`activity_focus` reaching deep event and deep revision rows; projection
cache between filter changes; scope contract unchanged; narrow 640x480
UI with accessibility names, tooltips and focus policies.

## Honest gaps

- The timeline's load-more walks the filtered in-memory projection; the
  canonical service still builds the full chronological list per scope
  (cached per scope until `refresh()`). Only the revision ledger pages
  in SQL — a multi-thousand-event document pays one projection build,
  not one per page.
- The events cursor locates the last-shown pair in the re-filtered
  list; if the operator edits bounds mid-browse the walk restarts from
  the new filtered top, which is the intended honest reset.
- The inspector types from the canonical event fields only; it does not
  open the referenced authority's payload — double-click still deep
  links to the owning surface for that.
