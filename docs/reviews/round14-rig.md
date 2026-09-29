# Round 14 — Entity-hierarchy / attachment transform truth

Scope: mounted/attached/parented entities (speaker→stand, speaker→wall, sub→position-group, projector→mount). When a parent moves/rotates, children must follow with correct composed transforms; detached children must land in a sane world pose; relationship data must survive undo/persistence/display truthfully. Verified in code and numerically via probes (`scratch/probe_rig.py`, `scratch/probe_rig2.py`) — composed positions computed from the rotation matrix, never trusting intermediate stored values.

## Enumerated mechanisms

| Mechanism | Where | Parent→child coupling |
|---|---|---|
| `EntityAttachment` edge (stand_on / mounted_to / racked_in / placed_inside / stacked_on, `parent_anchor` + `child_anchor_offset_m`) | `cad_attachment_models.py`, resolved by `physical_attachment.py` | Data-only infrastructure: **no production code path created edges or invoked the resolver** before this round |
| Wall → opening (`WallOpening.wall_id`, `offset_m`) | `cad_wall_models.py` / `cad_walls.py` | Live; split/merge migrate openings honestly; `delete_wall` refuses orphaning them |
| `topology.constraint_bindings.wall_ids` | `cad_wall_models.py` / `cad_walls.py` | Live; migrated to child walls on split/merge |
| `ConstructionAssembly.element_ref` (wall-bound material assemblies) | `cad_attachment_models.py` / `cad_construction_assembly.py` | Validation existed but was **never called in production** — could dangle |
| `CadConstraintSet` members: `CadWallClearanceConstraint.wall_id`, `CadPairDistanceConstraint.entity_a/b`, linked-placement/region entity refs | `cad_constraints.py` | Evaluated live at the move-commit gate; dangling refs raised uncaught adapter errors |
| `mountable` / `support_surface` capability flags | entity capability metadata | Declarative only — no transform coupling (correct as designed) |
| `semantic_bindings`, `aim_xyz`, `acoustic_reference_offset_m` | `cad_semantic_bindings.py`, entity fields | Self-contained references — no parent refs |
| Scene tree / objects panel | `RoomObjectsPanel` | Flat list — shows every entity once, no hierarchy claims |

## Findings and fixes

**F1 — Parent rotation dropped from the composed transform (bug).**
`resolve_attached_positions` / `attached_world_position` computed `anchor_point + child_anchor_offset_m` axis-aligned, ignoring `parent.orientation` — even though `EntityAttachment` documents the offset as applied *in the parent's local frame*. Verified numerically: parent yaw 90°, `front_face` mount produced (8.0, 1.75, 0.8) where the true composed pose is (8.25, 2.0, 0.8). Fixed in `physical_attachment.py`: anchor-local offset + child offset are rotated by `quaternion_to_matrix3(parent.orientation)` before translating by the parent position. Identity orientation output is unchanged; yaw-90 tests pin exact values.

**F2 — Children did not follow parents at all (dead resolver).**
Nothing called `resolve_attached_positions`: moving a stand left an attached speaker at its stored position, and drag previews rendered the same stale pose. Fixed in `cad_document.py`: `CommandHistory` normalizes derived child positions after every push/undo/redo via `apply_attachments`; all four preview paths (move/rotate preview + commit-preview writes) normalize the preview document so the render shows children following in real time; `replace_document` normalizes too.

**F3 — Deleting a parent left zombie relationship data.**
`delete_entities` and entity set-edits removed entities but left `attachments` untouched → persisted documents carried edges pointing at deleted parents/children. Fixed via `_detach_attachment_orphans`: commands now record `before_attachments` and drop every edge touching removed ids; orphaned children land at their current derived world pose (`orphaned_positions` recorded for exact revert).

**F4 — Undo did not restore relationship state.**
Reverting a delete restored entities but not the removed edges or the orphan's prior pose. Fixed: `DeleteEntitiesCommand` / `EntitySetEditCommand` apply/revert now restore attachments and orphan landings exactly; a move→delete→undo→undo chain returns the document to the exact prior state (pinned in `test_round14_rig.py`).

**F5 — Invalid graphs and dangling bindings were constructible.**
`SceneDocument.valid_document` never ran `attachment_graph` or `validate_construction_assemblies`: ghost-parent edges, cycles, and wall-assemblies bound to nonexistent `element_ref`s passed schema validation and could persist. Fixed: `valid_document` now fails closed on both (lazy imports, same pattern as `cad_walls`).

**F6 — Wall ops could orphan assemblies; constraint eval crashed the move gate.**
`split_wall`/`merge_walls`/`delete_wall` migrate openings and `constraint_bindings` but never `construction_assemblies.element_ref`, and `CadWallClearanceConstraint.wall_id` still points at the removed wall. Previously both produced silent divergence or an uncaught `CadConstraintAdapterError` on the next move commit. Fixed: `RoomWorkingDocument._commit_room_snapshot` keeps its existing re-validation via `SceneDocument.model_validate` — with assemblies now inside the document validator, orphan-producing wall edits fail closed with `ValueError` *before* the snapshot is committed (surfaced in the status bar by `wall_editor._commit_room_topology` and the `cad_composition._replace_room` catch); `move_commit_gate` and `evaluate_constraints` catch `CadConstraintAdapterError` and return the honest message `配置制約が参照先を失っています` instead of crashing or returning None; the existing `evaluate_error` panel path displays it.

**F7 — History/diff could show 変化なし for relationship changes.**
`SceneDiff` compared room/entities only; an attachment- or assembly-only change produced an empty diff and a false "no changes" summary. Fixed: `attachments_changed` and `construction_assemblies_changed` flags plus JP summary lines (`取付・マウント関係を変更`, `構造アセンブリを変更`).

## Verified correct already

- Self-attachment validator (`an entity cannot attach to itself`) and forest/cycle checks in `attachment_graph` — now also enforced at document validation.
- Wall→opening migration on split/merge preserves honest offsets; `delete_wall` refuses to orphan openings (fail-closed, not silent).
- Authoring constraints mark broken members rather than crashing — the delete flow does not scrub constraint references silently; missing members now surface as `evaluate_error` + the move-gate message, matching the mark-broken doctrine.
- Persistence round-trips attachments and assemblies verbatim; normalization keeps stored child positions consistent with the graph after every history op.

## Deferred / not applicable

- No production UI creates `EntityAttachment` edges yet — the graph is data-only infrastructure. The machinery now enforces, normalizes, and truthfully persists edges once they exist; authoring UI for attach/detach is out of scope.
- No reparent operation exists (edges are rewritten, never re-seated); "reparent preserves world pose" is therefore vacuous — documented as honest re-offset by construction.
- Scene display has no hierarchy view; the flat objects list makes no parent/child claims, so nothing to correct — a tree view is a feature, not a truth bug.
- Auto-pruning dangling `CadConstraintSet` members on delete is deferred; the chosen semantics surface them honestly (same precedent as authoring constraints) rather than silently mutating constraint data.

## Files changed

- `physical_attachment.py` — parent-local offset rotated by parent orientation (F1)
- `cad_scene.py` — `valid_document` fails closed on attachment graph + assembly bindings (F5)
- `cad_document.py` — history normalization, preview normalization, orphan landing + relationship-aware command revert, `replace_document` normalization (F2/F3/F4)
- `cad_room.py` — `_commit_room_snapshot` fail-closed validation + normalization (F6)
- `room_workspace.py` — `CadConstraintAdapterError` surfaced at `evaluate_constraints` + `move_commit_gate` (F6)
- `wall_editor.py` — `_commit_room_topology` helper surfaces `EditStateError` in status bar (F6)
- `cad_composition.py` — catch widened to `EditStateError` (F6)
- `cad_scene_history.py` — `attachments_changed` / `construction_assemblies_changed` + JP summary lines (F7)
- `backend/tests/test_round14_rig.py` — new pins: composed transform under yaw, move/rotate-follow, orphan landing, undo chains, preview truth, persistence, diff truth, cycle rejection, fail-closed assembly orphans
- `backend/tests/test_cad_construction_assembly.py`, `backend/tests/test_physical_attachment.py` — invalid fixtures rebuilt via `model_copy` bypass now that documents validate
