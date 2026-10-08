# Issue #996 — All-Boundary Material Apply as a Single Transaction：全境界面適用を1トランザクションに

## Scope

The 「全部屋境界面へ適用」 flow assigns one `AcousticMaterialAuthority` to
every `room_boundary` semantic surface. It previously ran one
`assign_material` upsert per surface — each in its own SQLite transaction —
so a mid-loop failure or interrupt committed a **partial** assignment while
the UI still read "applied".

This issue makes the whole apply a single sealed write
(`CadAcousticMaterialRepository.assign_material_bulk`): every boundary
commits inside one `BEGIN IMMEDIATE` transaction or none does, and the
operator sees the named failure with 「変更された面: 0」 — never
"some applied, status says applied".

Lives in `backend/src/htdt/cad_acoustic_material.py` (bulk authority +
preview + honest state aggregate + #964-facing change events) and
`backend/src/htdt/room_acoustics_panel.py`
(`SurfaceMaterialPanel._apply_to_all_boundaries` rewrite, readiness
surface). No schema change — reuses `cad_surface_material_assignments`.

## Vocabulary

| Term | Meaning |
|---|---|
| `MaterialBulkApplyError` | Fail-closed refusal (`ValueError`) carrying `reason` — `material_not_persisted`, `material_sha_drift`, `surface_missing`, `scene_revision_drift`. |
| `MaterialBulkApplyPreview` | Read-only pre-commit snapshot: document head pin, material id + sha, per-surface rows, and target / unassigned / overwrite / kept / dangling counts. |
| `MaterialBulkApplyResult` | Committed outcome: `applied` / `kept` / `skipped` surface id tuples plus the head pin the commit verified against. |
| `BoundaryMaterialState` | Honest read-side aggregate: per-surface `assigned` / `unassigned` / `unresolved_reference`, `stale_surface_ids` for rows outside the geometry, and a boundary summary — `no_boundaries` / `unassigned` / `partial` / `uniform` / `mixed` / `unresolved`. |
| `material_assignment_changes` | Before/after rows → `SceneChange` events on the `material_boundary` axis, the `extra_changes` vocabulary #964 revalidation consumes. |
| `restore_assignment_rows` | Verbatim row restore in one transaction — the revert leg of the single Undo step (restores rows a fresh `assign_material` would refuse). |

## Fail-closed edges

- **All-or-nothing.** Material check, head pin, row read, and every upsert
  share one `BEGIN IMMEDIATE` transaction. A mid-write SQL error (disk
  full, locked DB), a vanished material, a re-authored material hash, or a
  concurrent head advance rolls the whole batch back — the authority never
  observes a half-applied boundary set.
- **Commit-time re-verification.** The persisted material payload hash and
  the preview-pinned `scene_revision_id` are re-checked *inside* the write
  transaction; drift between preview and commit refuses the batch with a
  named reason, not a silent rebind.
- **Dangling rows stay visible.** A stored row whose material record is
  gone reports `unresolved_reference` — never coerced to `unassigned` — and
  a hash-corrupted row still raises `ValueError` like the pre-existing
  `assignments_for_document` contract.
- **Unknown material never auto-passes.** Only the exact persisted
  authority (id + `semantic_sha256`) binds; an unpersisted or drifted
  material refuses before any write.
- **No historical evidence touched.** The bulk write only upserts
  `cad_surface_material_assignments`; SceneRevision payloads and
  `cad_acoustic_materials` records stay immutable.

## Determinism

Targets are deduplicated and sorted by `surface_id` before commit, so
identical calls write identical rows in identical order regardless of the
caller’s input ordering. Previews enumerate entries in the same sorted
order the commit writes.

## Surface

「全部屋境界面へ適用」 now: previews the apply → asks only when existing
assignments would be overwritten (「すべてに適用（上書き）」 /
「未割当のみに適用」 / cancel, with exact counts) → commits one atomic
`assign_material_bulk` inside a single `CompositeEditCommand` Undo step →
reports 適用 / 維持 / 既存割当を保持 counts, or the named failure with
「変更された面: 0」. The readiness line renders the
`BoundaryMaterialState` summary — 混在 / 一部のみ割当 / 参照解決不能 are
shown honestly — and unresolved rows appear as a 「（解決不能な割当: …）」
combo entry that a fresh assignment or clear can repair.

## Undo

One `push_command` = one Undo step. `apply_side` runs the atomic bulk
apply; `revert_side` restores the exact pre-apply rows via
`restore_assignment_rows` (including rows that would refuse a fresh
assign). One undo reverts the whole batch, never per-surface.

## Boundaries / honest gaps

- The `material_boundary` change-event vocabulary is wired *toward* #964:
  `material_assignment_changes` produces the exact `SceneChange` the
  revalidation composer accepts via `extra_changes`, but no consumer feeds
  sidecar diffs into `compose_revalidation_queue` yet — the compose path
  is head-vs-parent and sidecars do not advance the head. Landing the
  consumption side is #964 scope.
- Per-surface combo assigns still write one row each by design; only the
  all-boundary apply is transactional.

## Tests

`backend/tests/test_issue_996_material_bulk_apply.py` (25 tests) —
all-or-nothing under injected mid-transaction failure at 2 / 100 / 1000
surfaces (Nth-statement SQL error → zero rows), deterministic sorted
ordering + dedupe, `material_not_persisted` / `material_sha_drift` /
`surface_missing` / `scene_revision_drift` refusals with zero rows,
overwrite / unassigned-only diffs and preview counts, mixed / partial /
uniform / unresolved / no_boundaries read-side honesty, dangling and stale
row reporting, single-step undo/redo restoring verbatim prior rows, the
single `material_boundary` change event, and panel tests covering the
confirm dialog paths (all / unassigned-only / cancel), injected-failure
readiness (named failure + 変更された面: 0), and honest mixed/unresolved
status text.
