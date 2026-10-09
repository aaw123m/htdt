# Issue #981 — IFC Diff Intake: Element-Correspondence Review：IFC差分取込・要素対応レビュー

## Scope

#866 landed real IFC import: a bounded subject (`gis-`), entity mappings
(`iem-`, sha-pinned, append-only), intake diagnostics + repair proposals,
and derived revisions (`gdv-`). A revised IFC could be imported, but only
as a brand-new unrelated subject — nothing compared it against the
already-imported model, nothing let the operator review element-by-element
what changed before committing.

This issue adds the diff path: re-importing a revised IFC against the
existing bound subject computes an honest element correspondence, presents
it in a review panel, and applies the operator's decisions transactionally
into a NEW derived subject — never mutating the pinned import, never
silently overwriting an operator decision, never counting "unknown" as
removed.

Lives in `backend/src/htdt/cad_ifc_diff.py` (correspondence + merge +
sealed apply authority), `geometry_intake_controller.py` (revised-import,
decision, apply, resume plumbing), `cad_geometry_intake.py`
(`build_ifc_diff_subject`, `source_kind='ifc_diff_merge'`),
`cad_ifc_repository.py` (`save_diff_apply` / `list_diff_applies` /
`latest_diff_apply` + `*_in_connection` shared-transaction helpers),
`cad_geometry_intake_repository.py` (`_SealedStore.save_in_connection`),
`ifc_diff_review_panel.py` (the review table), `room_workspace.py`
(wiring), and `workflow_application.py` (panel mount).

## Vocabulary

| Term | Meaning |
|---|---|
| `IfcDiffRow` | One correspondence row: `category` × `change_kind` with both revisions' mapping ids, geometry fingerprints, semantic fingerprints, part ids, and an `acoustically_relevant` flag. |
| `category` | `matched` | `changed` | `added` | `removed` | `ambiguous` | `unknown`. Primary key is IFC `GlobalId`; orphan pairs and collision cases get their own honest bucket. |
| `ambiguous` | GlobalId collisions (duplicate gid either side → `duplicate_global_id`) or renames/re-exports where prior and new orphans pair heuristically by (type, name, parent) — `ambiguous_rebind` (one candidate) or multiple candidates. Always `decision_required`. |
| `unknown` | Elements with no `GlobalId` and no unique (type, name, parent) pairing on either side → `unmatched_identity`. Painted gray; **never counted as removed or added**, and the summary line says so. |
| `IfcDiffDecision` | Per-row `accepted` | `skipped` | `pending` (implicit). Rows the operator skips keep their row state and are re-flagged on reload. |
| `IfcDiffApply` (`ida:`) | Sealed record of one apply: delta ref, prior/new artifact refs, prior subject sha, merged subject id + sha, every decision row, applied/skipped/pending row keys, per-category counts, operator + UTC. |
| `cad_ifc_diff_applies` | New native table (schema v117) persisting the sealed apply records; replay-probed by `native_authority_audit`. |
| `ifc_diff_merge` | `GeometrySourceKind` of the merged derived subject; its `source_refs` pin prior artifact + new artifact + delta. |

## Correspondence rules

- **GlobalId is primary.** Same gid both sides → `matched` (both
  fingerprints equal) or `changed` (`geometry_changed` when the geometry
  fingerprint differs, `semantics_changed` otherwise; material layers and
  openings flip `acoustically_relevant`).
- **Collisions are honest.** A gid appearing twice on either side never
  auto-pairs: every contesting row becomes `ambiguous` /
  `duplicate_global_id`.
- **Orphans pair cautiously.** Prior-only and new-only gids are first
  tried against each other on (type, name, parent): a single pairing →
  `ambiguous` / `ambiguous_rebind` (a rename is a *rebind decision*, not
  an add+remove); multiple candidates → `ambiguous`; unpairable →
  `removed` / `added`.
- **No-GlobalId elements** pair only on a unique (type, name, parent)
  key; anything else lands in `unknown` — displayed, flaggable, but never
  silently removed nor injected.
- **Coordinate revision.** When the two artifacts' world coordinate
  authority differs, a project-level `semantics_changed` row is appended
  so the unit/axis change is reviewed like any element.

## Apply semantics

- **Accept takes the new state; skip keeps the prior.** Merged mapping set
  = per-row decision: accepted rows use the new mapping, skipped/pending
  rows keep the prior side (removed-skip keeps it alive, added-skip keeps
  it out, unknown prior-side survives a skip and drops only on accept,
  unknown new-side enters only on accept). Mappings no row covers survive
  untouched.
- **Re-seal before subject build.** `reconciliation_marks` runs through
  `mark_mapping_reconciliation` *before* the merged subject is assembled,
  so the new subject's parts pin the marked (stale / new-state) mappings.
- **Same fail-closed chain as #866.** The merged subject flows through
  `diagnose_geometry_intake` + `propose_geometry_repairs` and the report
  and proposal records are persisted with it.
- **All-or-nothing.** Marked mappings + `IfcDiffApply` + intake report +
  repair proposal commit inside one `BEGIN IMMEDIATE` on a shared
  connection (the #996 pattern via `*_in_connection` helpers); any failure
  rolls the whole apply back — the store never observes a half-applied
  diff.
- **Pinned import immutable.** The prior artifact, its mappings, and its
  subject rows are never rewritten; the apply only appends re-sealed
  mapping rows and writes a *new* `ifc_diff_merge` subject.
- **Successive revisions.** After apply, the diff base becomes the merged
  set + new artifact, so a third revision diffs against what the operator
  actually sees.
- **Cancel / reload.** `resume_diff_state` recomputes rows
  deterministically from stored artifacts and restores the operator's
  decisions from the latest apply on that delta — skipped rows stay
  visibly flagged. Nothing is persisted before the operator clicks 適用.

## Panel

`IfcDiffReviewPanel` mounts next to the geometry-intake panel with
「改訂 IFC を取り込む」 / 「差分を再読込」 / 「全て承認」 / 「全てスキップ」 /
「選択を適用」 actions. Each row shows category, name, GlobalId, both
geometry fingerprints, the decision state, and per-row 承認 / スキップ /
位置 buttons (locate reuses #977 anchors). `unknown` rows paint gray,
`ambiguous` amber, accepted green, skipped brown; the header line reports
all six category counts — 不明 (unknown) explicitly noted as *not*
counted as removed — plus "決定済み n/m".

## Tests

`backend/tests/test_issue_981_ifc_diff_review.py` — category honesty
(matched/changed/added/removed/ambiguous/unknown incl. the
ambiguous-rebind paired row and the never-removed unknown rule),
coordinate-change project row, sealed-apply persistence, transactional
rollback on mid-write failure, pinned-import immutability, cancel leaves
no trace, skipped rows re-flagged after `resume_diff_state`, and panel
render/signal forwarding incl. the gray brush.
