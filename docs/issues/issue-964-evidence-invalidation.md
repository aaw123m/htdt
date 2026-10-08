# Issue #964 — Change-Diff Driven Evidence Invalidation：変更差分から再検証キューへ

## Scope

A sealed **change-diff → revalidation queue** authority that turns one exact
`SceneRevision`-to-`SceneRevision` change into a deterministic, executable
list of revalidation work items. It synthesizes — never reimplements — the
existing authorities: the scene diff (`cad_dependency_impact.diff_scene_
documents`), the #729 dependency/staleness machinery
(`cad_authority_dependency`), and the watched-artifact impact report
(`cad_dependency_impact.build_dependency_impact_report`).

Lives in `backend/src/htdt/cad_evidence_invalidation.py` (models +
composer + verify operation), `cad_evidence_invalidation_repository.py`
(sealed persistence, native schema **v114** tables `cad_change_diff_records`,
`cad_revalidation_queues`, `cad_revalidation_queue_runs`), and
`revalidation_queue_panel.py` (比較 page surface wired into
`optimization_workflow_workspace.py`).

## Vocabulary

| Term | Meaning |
|---|---|
| `ChangeDiffRecord` | Sealed record (`chdiff-…`) pinning both revision ids + content hashes, the exact `SceneChange` tuple, resolved axes, and the emitted `SemanticChangeEvent` refs. |
| `RevalidationQueueItem` | One work item: subject `AuthorityRef`, `stale`/`uncertain` state, action, execution class, route, prepared refs, reason. |
| `RevalidationQueue` | Sealed queue (`revqueue-…`) pinning the diff ref, impact report hash, assessment/plan refs, the `to_revision` head pin, items, and `software_sequence` — the exact software item keys the verify operation may run. |
| `RevalidationQueueRun` | Sealed run (`revrun-…`) pinning pre/post head revision identity + content hash, per-item `QueueItemOutcome`s, and a verdict. |
| Actions | `recompute`, `re_evaluate`, `re_import` (software); `remeasure`, `re_commission` (physical); `review` (fail-closed human judgment). |
| Routes | `prediction_recompute`, `evidence_re_evaluate`, `source_reimport`, `measurement_position_plan` (#956), `commissioning_authorization` (#946), `evidence_review`. |
| Run verdicts | `already_current`, `software_complete`, `awaiting_human`, `drift_detected`, `failed`. |

## Fail-closed edges

- **Unknown scope → review.** Any `uncertain` impact state, and any
  `unknown_dependency` staleness subject — whether or not a plan carried it —
  becomes a `review` item. Unknown dependencies never silently skip and never
  blanket-invalidate unrelated history.
- **Head-pin fencing.** The verify operation compares the live head's revision
  id AND content hash against the queue's `to_revision` pin before and after
  software execution; any drift produces `drift_detected` with items recorded
  `skipped`, never a false PASS.
- **Physical work never auto-starts.** `remeasure`/`re_commission` items are
  always `awaiting_physical`; the panel routes the operator to the #956
  position plan or #946 authorization surface with prepared refs.
- **Honest unavailability.** A software item with no wired runner records
  `unavailable` (verdict `awaiting_human`) rather than fabricating a result;
  a runner failure fails the run and skips the remaining software items.

## Determinism

`compose_revalidation_queue` pins every timestamp to
`to_revision.created_at_utc` (overridable for tests), so recomposing the same
pair is bit-identical — append-only idempotent saves make restart, cancel,
and re-edit safe. No duplicate diff/queue records can accumulate.

## Surface

The 比較 page mounts `RevalidationQueuePanel` below the Decision Brief:
「影響を再評価」 recomposes the queue for head-vs-parent; 「変更の影響を検証」
runs the sealed verify operation; each item renders one JA line
(state「name」— action — reason → route) and each route exposes a
navigation button into the owning workspace (room prediction, measurement
campaign, comparison review). A stale queue shows a freshness warning and
never claims current validity.

## Tests

`backend/tests/test_rev72_evidence_invalidation.py` — sealed diff/queue
round-trip + tamper, per-axis staleness scope (material-only → geometry
untouched, target → raw measurement preserved, unknown → review),
idempotent recomposition, verify-operation fencing, and the panel smoke test.
