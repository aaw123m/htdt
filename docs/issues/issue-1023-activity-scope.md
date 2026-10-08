# Issue #1023 — アクティビティ履歴：プロジェクト範囲のSQLスコープ・正直な見出し・ページング

## Scope

The アクティビティ page mixed every project's revisions into the current
project's history, and a hard 50-row cap could hide the entire past of
the bound project. This change scopes the revision SQL by `document_id`,
makes the listing scope an explicit 「このプロジェクト / 全プロジェクト」
choice stated in every section heading, and gives the timeline
kind/date-range/search filters plus explicit paging so no fixed cap can
bury history.

All changes are in `backend/src/htdt/application_pages.py` (page +
revision SQL) and `backend/src/htdt/workflow_application.py`
(`_make_activity` wiring). No new audit log — the timeline stays the
canonical `CadProjectActivityService.events` projection.

## Vocabulary

| Term | Meaning |
|---|---|
| `RevisionListScope` | `'project'` (SQL-filtered to one `document_id`) or `'global'` (explicit all-projects opt-in). Any other value, or `'project'` without a `document_id`, fails closed with `ValueError`. |
| `list_recent_revisions(repository, limit, *, scope, document_id, offset)` | The revision ledger reader. `WHERE detached = 0 AND document_id = ?` in project scope; `ORDER BY seq DESC LIMIT ? OFFSET ?`. |
| `count_recent_revisions` | Same scope, `SELECT COUNT(*)` — backs the `全N件 · X–Y件を表示` pager label and the last-page bound. |
| `list_known_document_ids` | `SELECT DISTINCT document_id` over non-detached revisions — the global timeline's project set, derived from the store, never hardcoded. |
| 表示範囲 | The page-level scope combo. With no bound project it is forced to 全プロジェクト and disabled — a project view without a project would be a lie. |
| `_project_refs` | Every identifier the bound project answers to: `document_id`, canonical `project_id`, display name. An operation joins the project section only on a match; `project_ref=None` and foreign refs stay in その他の操作. |
| Selection key | `(event_id, document_id)` on the timeline, `(revision_id, document_id)` on revisions, `(operation_id,)` on operations — never the row index. |

## Evidence model

- **Scoping happens in the query.** `list_recent_revisions` adds
  `document_id = ?` to the SQL under project scope; nothing is hidden by
  the UI afterward. `global` is the only path to an all-projects listing
  and is an explicit opt-in (`scope='global'` keyword), so a missing
  document id can never silently widen the query.
- **Headings state their scope.** 操作（このプロジェクト（<名前>）),
  プロジェクトタイムライン（…), リビジョン履歴（…) all carry the current
  表示範囲; その他の操作（アプリ全体・他のプロジェクト）holds every row the
  project filter rejected. The timeline's プロジェクト column is shown
  only in the global view, where it carries information.
- **Paging replaces the cap.** Both tables page at 50 rows with
  前へ/次へ buttons and `全N件 · X–Y件を表示`. Revisions page via SQL
  `LIMIT/OFFSET` against the scoped query; the timeline pages over the
  filtered projection — the past is walkable to the last row.
- **The global timeline is a merge of canonical projections.**
  `list_events(None)` folds each known document's own
  `CadProjectActivityService.events(document_id)` and sorts by
  `occurred_at_utc` descending — every row still comes from the canonical
  source and stays reconstructible; there is no parallel audit trail.
- **Filters are honest.** 種類 groups the closed `ActivityEventKind`
  vocabulary into JA labels (プロジェクト・保存, バリアント, キャプチャ,
  計測, 校正, チェックポイント, プリセット, 健康点検, AV同期, メモ,
  その他); 期間 offers 今日/過去7日間/過去30日間; search matches title,
  detail and project name. Unparseable timestamps pass the range filter
  rather than silently dropping a row.
- **Deep links carry the owning project.** Revision rows link to a
  `SCENE_REVISION` `NavigationTarget` (部屋 → history) whose `project_id`
  is the row's own document; foreign timeline rows get `project_id`
  stamped onto their link at activation. Either path routes through the
  guarded `_switch_project` (dirty-state policy intact) — a row from
  another project can never activate inside the current one unnoticed.
- **Selection rebinds by identity.** Filter, paging, scope and `refresh()`
  all re-render through `_preserve_selection`, which recaptures
  `(id, document_id)` keys and re-selects the same authority ids after
  the refill — a selection can only reappear on the same record or be
  honestly absent, never drift to a different row.
- **History stays distinct from HEAD.** The revisions table lists the
  sealed `scene_revisions` ledger (created_at, document, revision_id);
  activating a row opens that revision's own history/diff surface, not a
  re-load of the current head.

## Surface

- 表示範囲 combo at the top of the page; all section headings follow it.
- 操作 section split: project rows (状態/操作/更新時刻) under
  「このプロジェクト」, then a separate その他の操作 table for
  app-global and other-project rows.
- Timeline filter row: 種類 combo → 期間 combo → 検索 field; count +
  paging under the table.
- リビジョン履歴 table (時刻/プロジェクト/リビジョン) with the same
  pager; double-click opens the revision's history surface in its own
  project.

## Tests

`backend/tests/test_issue_1023_activity_scope.py` (20 tests, offscreen):
two documents in one store — zero cross-contamination at the SQL and the
page level, fail-closed scope contract, scope-aware counts, full-history
paging (121 revisions walked), scope toggle labels + column visibility,
operations split by `project_ref` including display-name matching,
kind/range/search filters, 50-row timeline + revision paging, selection
survival across refresh and filter changes, per-row project-stamped deep
links, foreign-event activation stamping, no accidental foreign
activation in project scope, and canonical-projection reconstructibility.

## Honest gaps

- The timeline pages over the filtered in-memory projection (the
  canonical service returns the full chronological list); only the
  revision ledger pages in SQL. Both are bounded views — no cap hides
  rows — but a multi-thousand-event document pays a per-refresh list
  build.
- その他の操作 groups foreign-project and unscoped operations together;
  it does not further split them per foreign project.
- The date-range filter interprets `occurred_at_utc` as UTC ISO-8601;
  rows whose timestamps cannot be parsed always pass the range filter
  instead of being silently dropped.
