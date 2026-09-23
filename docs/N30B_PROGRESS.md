# N30b progress

Issue: #53  
PR: #54  
Branch: `feat/n30b-walls-openings`

## Current state

N30b implementation and A09 Windows real-mouse acceptance are complete. Remaining work is final CI confirmation and merge.

Completed:
- stable `WallSegment` identity and endpoint references;
- wall-local `WallOpening` model;
- wall-bound `WallConstraintBinding` for clearance-reference continuity;
- one-to-one RoomPrism boundary edge topology validation;
- opening wall-length / room-height validation;
- wall move preserving wall/opening/constraint IDs;
- split with source-wall lineage and deterministic opening/clearance migration;
- split rejection when an opening crosses the split point;
- merge restricted to ordered collinear same-direction walls with matching thickness;
- merge opening offsets and clearance references migrated atomically;
- conservative wall deletion with rejection while affected opening/clearance references remain;
- SceneDocument schema v3 persistence while preserving canonical hashes when `wall_topology` is absent;
- one CommandHistory transaction for room + topology changes;
- revision repository round-trip coverage;
- native wall/opening editor with Japanese-first toolbar, inspector, status messages and room/object labels;
- GUI actions for wall move, split, merge, delete, door opening and clearance binding;
- wall thickness edits preserving openings and clearance bindings;
- cross-tool `CadEditorWindow` composition: topology作成後もroom vertex move/dimension/heightを安全に再validateし、vertex数変更はwall split/deleteへ明示誘導;
- normal native launcher / package entry switched to `htdt.native_cad`;
- `scripts/validate_n30b_windows.py` A09 harness with the required F3 fixture and product composition window;
- Windows Actions forced to UTF-8 so Japanese UI/CLI text is portable;
- final A09 real Windows mouse acceptance PASS on commit `5ede848e8e0b0967a50c04c83ff679a649ca439b`;
- acceptance evidence: [N30b A09 Windows acceptance](N30B_ACCEPTANCE_2026-09-17.md).

## A09 result

The owned Windows PC passed Japanese UI, F3 fixture, real-mouse wall selection/move, split reference migration, merge reference migration, referenced-delete rejection, ambiguous-split rejection, unreferenced wall deletion, and exact Undo/Redo through the same `CadEditorWindow` used by the product launcher. The worktree was clean before and after acceptance.

## Remaining gate

1. latest PR-head GitHub Actions must be green;
2. mark PR #54 ready and merge;
3. verify Issue #53 closes;
4. proceed to N40 theater objects / A10.
