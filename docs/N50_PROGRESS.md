# N50 implementation progress

Tracking: #59  
Branch: `feat/n50-constraint-visualization`

## 2026-09-17 — implementation complete

- Confirmed `main` head `5dfd20aa6b7eec5d50e6f8ab94ba41db8fc79fb0` after N40 merge/status updates.
- Re-read N50 roadmap and A11 acceptance contract.
- Reviewed `placement_constraints.py`, `search_space.py`, `cad_scene.py`, and `cad_wall_models.py` before implementation.
- Reused the existing G10 placement engine unchanged as the feasibility authority.
- Added native immutable constraint models for allowed region, exclusion/walkway, wall clearance, and pair distance.
- Added `CadConstraintRepository` as a document-scoped authoring workspace beside scene storage in the same SQLite file.
- Added pure CAD→G10 adapter and typed CAD evaluation results with Japanese reason codes/text.
- Stable wall constraints persist N30b `wall_id`; transient G10 `from_vertex_id->to_vertex_id` edge IDs are generated only at evaluation time.
- Physical rectangular entities use a conservative circumscribed horizontal radius for the existing G10 envelope model; the UI does not describe this as exact mesh collision.
- Added focused tests for coordinate/profile mapping, stable wall mapping after split topology, wall/walkway reason mapping, position override immutability, unknown-wall rejection, workspace round-trip, candidate blocking policy, and hide/lock feasibility independence.
- Added `ConstraintEditorWindow` on top of the accepted N40 workflow. Normal launcher `htdt.native_cad` now uses this product composition.
- Added Japanese-first `制約` dock with current status, violation list, actual/required detail, authoring buttons, and explicit non-quality wording.
- Added viewport region patterns, selected entity/counterpart highlighting, selected wall line, rejected-candidate marker, and distance segment.
- Integrated candidate evaluation into the existing move preview path. No second transform engine was added.
- Commit policy blocks newly introduced hard violations and worsened scalar-distance violations, while allowing recovery motion from an already-existing region violation.
- Rejected move restores the committed pose, does not grow history, and preserves rejection evidence for inspection.
- Entity deletion and ambiguous wall split/merge/delete are blocked while referenced by full N50 constraints. Full wall-clearance constraints are not guessed across topology changes; users must explicitly remove/redefine them.
- `feasible` is shown only as placement-constraint satisfaction (`制約を満たす / 制約違反`). It is not surfaced as acoustic quality, recommendation, ranking, or score.
- Browser UI remains frozen.

## Verification

- Domain slice CI #230: PASS.
- Product UI / commit-policy CI #235: PASS.
- Product-code head `8762c5e15c1f8c9442ce6ef6378f24881e0f5236` passed CI #238 / run `35201888464`, including the A11 harness compile and hide/lock feasibility invariant.
- Final Windows A11 ran on branch head `a8ba8d0507c404a9058c0ffe12475fce4dca968c`; commits after the product-code head were documentation-only.
- Owned Windows 11 PC, 2880×1800 at 200% DPI, Python 3.12.10, PySide6 6.11.2, PyVista 0.49.0, VTK 9.7.0.
- Real Win32 mouse input verified right-wall-clearance rejection, reason selection, walkway rejection, reason selection, normal editing after rejection, constraint persistence, and non-quality wording.
- `A11_RESULT PASS`, `A11_EXIT=0`.
- Local worktree was clean before and after A11 (`PRE_STATUS_COUNT=0`, `POST_STATUS_COUNT=0`).
- Formal record: `docs/N50_ACCEPTANCE_2026-09-17.md`.

## Persistence boundary

N50 constraint definitions are document-scoped authoring state, persisted beside `SceneRepository`, not embedded in `SceneRevision`. This follows the existing G10 Context/ConstraintSet separation and avoids silently changing the established scene hash contract for N50. Future N60/N70 jobs must capture an immutable constraint-workspace hash/snapshot together with the exact SceneRevision at submission time; mutable latest workspace state must never replace captured job input.

## Merge

- PR #60 marked ready after A11 and accepted-code CI were complete.
- PR #60 merged to `main` as merge commit `f37ffdf8a4c9e67894afb52d7750562812313b0d`.
- Issue #59 closed automatically with state reason `completed`.
- N50 is complete. Next roadmap milestone is N60.

No additional product test was run for the final documentation-only status updates.

## Known intentional limits

- N50 does not provide constraint-definition Undo/history; constraint authoring is explicit and persists immediately to the workspace.
- N50 does not automatically migrate full wall-clearance semantics across split/merge because the intended successor scope is ambiguous.
- G10 footprint constraints use a conservative radial envelope rather than oriented-box collision.
- Optimization/ranking remains a later milestone; N50 only exposes placement feasibility and reasons.
