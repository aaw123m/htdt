# Round 12 — undo/redo, dirty-state & document lifecycle coherence

Scope: the correctness contract of the editing model itself — every
user-triggerable mutation path enumerated and classified (command/undo
stack vs. direct bypass vs. read-only), dirty-state coverage across all
mounted pages, the undo/redo contract property-tested over every command
type, selection/focus behavior after undo, and document lifecycle writes
that run *outside* the owning workspace's `WorkingDocument` (head writes
that can orphan drafts or silently drop edits). Prior rounds were read
first: `round6-spec` (constraint edits bypassed undo — root cause found
and fixed here), `round8-cadux`, `round9-errors` (raising commands no
longer corrupt the stack — verified, not re-reported), `round10-optimizer`
(Ctrl+Z/Y deferred — verified still deferred). Branch `devin/rev12-dirty`.
Verification is Qt-offscreen (`QT_QPA_PLATFORM=offscreen`) under Python
3.12.10 plus pytest.

## The editing model (as verified)

`WorkingDocument` (`cad_document.py`) is the single mutable scene
authority inside a mounted workspace. `CommandHistory.push` applies the
command **before** truncating/appending to the stack and **before**
moving `_index` — a raising apply leaves history and document
byte-identical (round 9). `undo`/`redo` likewise revert/apply before the
index moves. `CompositeEditCommand` wraps an inner entity-set edit plus
optional `apply_side`/`revert_side` callables for sidecar state, so a
side-effect-only change (no entity edit) is still a first-class undo
step (`is_noop` requires a noop inner *and* no sides).

`is_dirty` = memoized `scene_content_hash` of the working document vs.
`_saved_hash`, ORed with the sidecar digest (`_capture_sidecars`) that
covers materials/environment/poses/transfers/video-workspace/proposed-
placements. `constraint_set` (hard placement constraints) is **not** in
the digest — it is a persisted authority beside the scene and now gets
its own undo step instead of dirty-state participation (see #1).

Recovery: `save_recovery` writes a draft snapshot keyed by
`source_revision_id` whenever `working.is_dirty`; `clear_recovery` on
clean. A snapshot whose `source_revision_id` no longer matches head is a
stale draft — `save()` on it raises `SceneRevisionConflictError`.
`recovery_candidate` holds an unresolved snapshot at bind time; `_sync_
recovery` early-returns while one is pending so a stale draft is never
silently cleared by unrelated edits.

## Coverage map — every mutation path → classification

Undoable command-stack paths (all verified to restore exact prior state —
see the property test):

| Path (user surface) | Command type | Result |
|---|---|---|
| Move entity (viewport drag commit, property spinboxes) | `EntityMoveCommand` | undo ✓ |
| Rotate entity | `EntityRotateCommand` | undo ✓ |
| Transform entity (handles) | `EntityTransformCommand` | undo ✓ |
| Add object (toolbar/palette/drag-in) | `EntityAddCommand` | undo ✓ |
| Duplicate selection | `EntityDuplicateCommand` | undo ✓ |
| Property edits (name/kind/size/role/position) | `EntityUpdateCommand` | undo ✓ |
| Delete (Del key, context menu) | `EntityDeleteCommand` | undo ✓ |
| Replace document (bulk re-seed) | `DocumentReplaceCommand` | undo ✓ |
| Preview commit (rubber-band transform apply) | `CompositeEditCommand` over preview | undo ✓ |
| Composite entity-set edits (multi-object ops) | `EntitySetEditCommand` | undo ✓ |
| Authoring constraints add/remove/toggle (#843) | `CompositeEditCommand` side-only | undo ✓ |
| **Placement constraints add/delete (#486)** | was **dead code + bypass** | **FIXED** — now side-only composite |

Bypass paths that write document head *outside* the owning workspace —
each could orphan a persisted draft (stale-parent conflict on the
draft's next save) or silently discard uncommitted edits:

| Path | Before | Verdict |
|---|---|---|
| History → "このリビジョンを復元" (`restore_revision`) | silently reverted dirty edits + dropped pending recovery | **FIXED** — vetoes while dirty/preview/recovery |
| SystemVariant "この提案を適用…" (`SystemExpansionWorkflowService.apply`) | wrote new head while the scene workspace could be dirty | **FIXED** — `apply_guard` veto wired in both owning workspaces |
| 測定点の追加 `derive_measurement_point_from_seat` | wrote new head with a persisted draft present | **FIXED** — recovery guard |
| Target pattern materialize (`materialize_target_pattern`) | wrote new head with a persisted draft present | **FIXED** — recovery guard |

Eager authorities — append-only record stores outside the undo model by
design (each persists on commit; "undo" of an authority record is
explicit deletion, matching how measurements/variants are lifecycled):

| Authority | Store | Justification |
|---|---|---|
| Measurements & datasets | `CadMeasurement*` repos | authority records, not scene state |
| REW import | REW repositories | external-source ingest, append-only |
| System variant records | `CadSystemVariantRepository` | typed proposal authority |
| Search specs / evaluations | search/eval repositories | derived records, recomputable |
| Named views / underlays | view/underlay repositories | reference records |
| Listener poses / materials / environment / transfers / video workspace / proposed placements | sidecar transaction (#915) | design-authority state; dirty via sidecar digest, persisted atomically with the scene save |
| View state (selection, hidden, locked, snap toggles) | `save_view_state` | workspace chrome, not document content |
| Project lifecycle (create/rename/delete document) | `application_pages` authority | lifecycle, not scene edit; delete refuses the open document |
| Capture inbox dispositions, data-management ops | capture/data repositories | operational records |
| Recovery snapshots | `save_recovery`/`clear_recovery` | internal crash-safety mechanism — undoing a draft save would defeat its purpose |

Read-only surfaces verified: overview/status pages, standards/report
panes, comparison viewers, history browsing (until restore — now gated),
and all compute/evaluate actions (solvers write to result stores, not
the document).

Legacy note: `native_editor.py`, `constraint_editor.py`,
`measurement_editor.py`, `measurement_workspace.py` are unmounted
(`native_cad.py` lazy registry) — audited for awareness only.

## Dirty-state coherence

| Page mount | before_deactivate / dirty gate | Verdict |
|---|---|---|
| Room workspace | `is_dirty` → save/discard prompt; `keep_draft` persists snapshot | coherent |
| Measurement workspace | `busy`/`pending_import` gates; keep/discard vocabulary | coherent |
| Optimize workspaces | `activate()` → `reload_if_clean`; dirty blocks rebind | coherent — plus apply veto added here |
| Overview | read-only | n/a |

Rule verified: every document mutation goes through `working` → sets
dirty; every `save()` clears it (`_saved_hash` refresh + sidecar
digest reset + `clear_recovery`); every head-writing path that could
diverge working-vs-head now refuses while a draft/dirty state exists.

## Undo/redo contract — property-tested

`test_round12_dirty.py::_verb_cases` parametrizes all ten command types:
apply → document state A; undo → exact prior state B (entity set,
positions, hashes); redo → A again; `is_dirty` toggles correctly at each
step. Composite side-effect commands: apply-side failure leaves nothing
recorded; revert-side failure leaves the command applied with the index
unmoved (round-9 contract extended to side effects). No remaining
partial-state path: `push`/`undo`/`redo` all mutate indices only after
the apply/revert succeeds, and the two new side-effect paths persist
**before** swapping in-memory state.

Selection/focus after undo: `view_state.sanitize` runs on every
undo/redo — dangling selected/hidden/locked ids are dropped against the
restored document, so a deleted-then-undone entity is immediately
selectable. View-state assertions (hidden/locked/selection) are
*forward-only* by design: a delete drops them and undo does not restore
them — they are persisted chrome over live entities, not document
state. The contract is "revived entities exist and are selectable,"
verified in `test_undo_redo_keeps_selection_sanitized_across_deletes`.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | Placement-constraint panel buttons were **dead code**: the handler matched kinds `walkway`/`allowed`/`wall_clearance`/`pair_distance` while the panel emits `add_walkway`/`add_allowed`/`add_wall`/`add_pair`, and even on a match the `make_*` calls used stale signatures `(document, room, constraint_set, entity_id=...)`. On top of that, edits assigned `controller.constraint_set` directly — **bypassed the undo stack entirely** (round6-spec's constraint gap). Every add/delete was a silent no-op for the add buttons and an un-undoable mutation for delete | High | FIXED — handler rewritten to the real kinds + correct `make_*` signatures; all five actions route through new `controller.update_placement_constraints` → side-only `CompositeEditCommand` (undo label 配置制約), persist-first ordering, noop edits not recorded |
| 2 | `restore_revision` rewrote the working document with no dirty-state guard: undoable edits since last save were silently discarded, and a pending recovery draft was dropped — "history restore" was a data-loss path | High | FIXED — refuses while `recovery_candidate`/`has_preview`/`is_dirty` with JP messages naming the required action |
| 3 | `SystemExpansionWorkflowService.apply` writes a new head revision outside the owning workspace. With the scene dirty (or previewing, or holding a recovery candidate), the new head orphaned the draft — its next save hit a stale-parent conflict | High | FIXED — `apply_guard: Callable[[], str | None]` vetoes apply; wired in `OptimizationWorkflowWorkspace` (covers compare/intervention/robustness panels, one shared service) and `RoomWorkspace` (defensive — the room panel has no apply affordance yet) |
| 4 | `_apply_constraint_state` swapped the in-memory authoring-constraint set **before** persisting: a failed save left the controller showing a state that was never written (in-memory/disk divergence inside an undo side effect) | Med | FIXED — persist-first ordering; the same pattern applied to the new placement-constraint path |
| 5 | Two measurement-side head writes (`derive_measurement_point_from_seat`, `materialize_target_pattern`) had no draft guard — same orphan trap as #3 | Med | FIXED — refuse with JP message while `recovery()` reports a draft |
| 6 | View-state chrome (hidden/locked/selection) does not restore through undo — delete drops it permanently | — | verified forward-only contract is coherent; documented above, not changed |
| 7 | `SystemExpansionRoomPanel` exposes no apply affordance (authoring only) — room-side `applied` handling is unnecessary | — | verified; only the guard wiring kept |

## 1 — Placement constraints: dead buttons + undo bypass (fixed)

`_constraint_action` matched `walkway`/`allowed`/`wall_clearance`/
`pair_distance` — the panel emits `add_walkway`/`add_allowed`/`add_wall`/
`add_pair`/`delete`, so every add button silently fell to the `else:
return`. The `make_*` calls that would have run also used pre-refactor
signatures (`(document, room, constraint_set, entity_id=...)` vs. the
real `(document, entity_id, *, wall_id=, min_m=)`), so even a kind fix
alone would have raised on every add. And `delete` — the only path that
could actually fire — assigned `constraint_set` directly and called
`save_constraints()`: a mutation with no undo step.

New `RoomWorkspaceController.update_placement_constraints(new_set)`
wraps the change in a side-only `CompositeEditCommand`
(`apply_entity_set_edit` with only apply/revert sides) — undo/redo move
the persisted `CadConstraintSet` between revisions of the tuple, label
"配置制約", noop edits (`new_set == before`) return `False` without
recording. The panel gained `selected_wall_id()` / `distance_m()`
accessors so wall/pair constraints take the UI's real inputs instead of
nothing at all.

## 2 — `restore_revision` silently discarded dirty state (fixed)

History restore replaced the working document with an older revision
unconditionally: unsaved edits vanished (not undoable — the working doc
was re-seeded), and a pending recovery draft was orphaned. Now guarded,
in order: missing revision → preview active → dirty working doc → the
user is told exactly which state to resolve first ("未保存の変更を保存
または元に戻してから履歴を復元してください"). Clean restores are
unchanged.

## 3 — Variant apply could orphan a draft (fixed)

`apply` → `variant_repository.apply_variant` writes a new head
revision. The optimize workspace rebinds via `controller.activate()`
→ `reload_if_clean` — which **refuses to reload while dirty**, leaving
the workspace bound to a stale document whose next save would
conflict. Same trap if a persisted draft existed. The service now
exposes `apply_guard`; both workspaces that hold a `SystemExpansion
WorkflowService` install a veto checking (in order) recovery candidate,
live preview, dirty. All three optimize panels (compare, intervention
planner, robustness) share one service instance — one wiring covers all.

## 4 & 5 — Persist-first ordering + measurement draft guards (fixed)

`_apply_constraint_state` (and the new `_apply_placement_constraints`)
now call `save*` **before** swapping the in-memory set, so a failed
write inside an undo side effect cannot leave in-memory state diverged
from disk. `derive_measurement_point_from_seat` and
`materialize_target_pattern` — the two non-workspace head writes on the
measurement side — now refuse while `scene_repository.recovery(
document_id)` reports a draft, with the same save-or-discard message.
