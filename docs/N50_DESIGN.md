# N50 constraint spatial visualization — design

> Tracking: Issue #59  
> Base: `main` at `5dfd20aa6b7eec5d50e6f8ab94ba41db8fc79fb0`  
> Acceptance: A11 in `CAD_EDITOR_ACCEPTANCE.md`

## 1. Goal

N50 makes placement constraints visible and actionable inside the native CAD scene. A user must be able to create or inspect a walkway / clearance violation, select the reason, and see the constrained object, counterpart/region/wall, actual distance, and required distance or range in the same workspace.

`feasible` means only **all hard placement constraints passed**. It is never a sound-quality score, recommendation, ranking, or acoustic verdict.

The browser UI remains frozen. N50 is native PySide6/PyVista only.

## 2. Reuse existing G10 instead of replacing it

`backend/src/htdt/placement_constraints.py` remains the placement-constraint authority. It already supports:

- allowed regions
- exclusion regions
- wall clearance
- axis range
- movement budget
- pair distance / envelope clearance
- linked placement
- room-boundary checking
- `observations` with `actual`, `required`, and `passed`
- `rejections` with constraint ID, involved entity IDs, message, and numeric details
- top-level `feasible`

`search_space.py` already consumes the same evaluation result and filters infeasible candidates. N50 must not fork or reinterpret that engine.

The native CAD integration is therefore an **adapter + presentation layer**.

## 3. Native constraint workspace

N50 adds a CAD-facing constraint workspace whose IDs are stable and whose definitions are independent from Qt/VTK.

Initial native definition scope:

- `allowed_region`
- `exclusion_region`
- `wall_clearance`
- `pair_distance`

These cover the N50 roadmap and A11. Axis/movement/linked constraints remain evaluable through G10 but do not need dedicated N50 authoring UI in this milestone.

Each definition stores:

- stable `constraint_id`
- affected stable CAD `entity_id` values
- region vertices or stable `wall_id` / counterpart entity ID as appropriate
- minimum/maximum distance values where applicable
- short user-facing name

The authoring workspace is persisted beside native scene storage in the same SQLite file, keyed by `document_id`, rather than relying on a live legacy HTTP Context. It is intentionally separate from `SceneDocument` geometry revisions in N50, matching the existing G10 separation between Context and ConstraintSet. Before N60+ asynchronous jobs consume constraints, the job input must bind an immutable constraint-workspace hash/snapshot together with the exact SceneRevision; mutable latest workspace state must never be substituted for a running job's captured input.

## 4. G10 adapter

`cad_constraints.py` is a pure adapter module with no Qt dependency.

### 4.1 Scene → G10 context payload

The adapter builds the minimal dictionary required by `evaluate_constraint_set()`:

- room exact footprint and height from `SceneDocument.room`
- one CAD measurement point is exposed through legacy `measurement_point`
- every other constrained CAD entity is exposed through the legacy `speakers` collection as a compatibility carrier (`speaker_id` + `position` only)

This does **not** mean seats/furniture become speakers semantically. `placement_constraints._entity_baselines()` uses that collection only as an ID/position carrier. The adapter is the only place allowed to rely on this legacy shape.

If no native measurement point exists, the adapter uses a private synthetic adapter-only measurement-point ID that is never surfaced as a CAD entity and is not targetable by native constraints.

### 4.2 Physical footprint → G10 profile

G10 currently models horizontal object envelope with a radius. For N50 physical rectangular bodies are conservatively mapped to a circumscribed horizontal radius:

`radius = hypot(size_x / 2, size_y / 2)`

This is rotation-independent and prevents an object that physically reaches a wall/region from being treated as clear. It can be conservative near corners; UI must call it a clearance envelope, not exact mesh collision.

Measurement points use radius 0.

### 4.3 Stable wall ID → legacy edge ID

G10 wall-clearance uses `edge_id = "from_vertex_id->to_vertex_id"`. N30b uses stable `wall_id` plus endpoint vertex IDs.

The adapter owns a bidirectional map:

- CAD `wall_id` → G10 legacy edge ID
- G10 legacy edge ID → CAD `wall_id`

Mapping is generated from current `WallTopology`. A wall-clearance native definition stores only `wall_id`; the transient legacy edge ID is regenerated for each evaluation and is never persisted.

A full N50 wall-clearance constraint is semantically stronger than the N30b `WallConstraintBinding`. When its referenced wall is split, merged, or deleted, automatically choosing whether the rule belongs to one replacement wall or several would be ambiguous. N50 therefore blocks those topology edits until the full constraint is explicitly removed/redefined. N30b lightweight bindings keep their own existing split/merge migration contract independently.

The adapter rejects missing, duplicate, or direction-mismatched mappings rather than guessing.

## 5. Evaluation presentation model

Raw G10 dictionaries are converted into immutable CAD-facing results.

A result contains:

- `constraint_id`
- native kind
- stable involved entity IDs
- optional stable `wall_id`
- region role/geometry when relevant
- `passed`
- normalized reason code
- Japanese short reason
- `actual_m` where a meaningful scalar distance exists
- `required_min_m` / `required_max_m` as applicable
- raw G10 `actual` / `required` retained for diagnostics

A top-level evaluation exposes `constraints_satisfied: bool`; UI wording uses `制約を満たす / 制約違反あり`. It must not display `良い`, `最適`, acoustic stars/scores, or equivalent quality language based on feasibility.

## 6. Regions and walkway representation

N50 treats a walkway as an exclusion region for placement unless a later milestone requires richer semantic routing.

Native regions are polygonal XY regions inside the exact room footprint:

- allowed region: object envelope must stay inside
- exclusion / walkway: object envelope must not intersect

Viewport rendering uses translucent filled polygons plus a boundary/pattern. State remains distinguishable without color alone: exclusion/walkway regions add line patterns and selected violations add explicit labels/detail text.

## 7. Native UI

A `制約` dock is layered over `TheaterWorkflowWindow` through `ConstraintEditorWindow`, keeping previous editor behavior inherited rather than duplicating it.

Initial UI:

- summary: `制約を満たす` or `制約違反 N件`
- list of constraints/violations with short Japanese labels
- buttons for `通路`, `許可領域`, `壁離隔`, `物体間離隔`
- selected violation detail: actual vs required
- viewport overlay for regions, selected wall/counterpart, rejected candidate, and distance segment

Selecting a violation must:

1. select/highlight the subject entity,
2. highlight its counterpart wall/region/object,
3. draw a distance segment or closest-point indicator when distance semantics exist,
4. keep the numeric actual/required values visible.

## 8. Edit/commit behavior

Normal object drag stays on the accepted N20/N40 interaction path.

During preview, the constraint adapter evaluates the candidate position. On release:

- passed candidate: commit normally
- newly introduced hard violation: reject the commit and retain concrete rejection evidence
- existing scalar-distance violation: reject only if the candidate worsens it
- existing region violation: allow motion that does not introduce a new violation, so an object already inside a forbidden region is not trapped and can be moved out

Rejected movement restores the prior committed pose, does not add history, and keeps the rejected candidate/actual-required explanation visible until the next accepted edit or constraint change.

Hide/lock are editor view/edit state and are not inputs to constraint evaluation. Hidden objects still participate in constraints. Locked objects remain constrained; lock only prevents editing.

## 9. Persistence and authority

`CadConstraintRepository` persists the document-scoped authoring workspace in the same native SQLite file as `SceneRepository`. Reopening the document restores the exact current constraint definitions. Constraint evaluation results are derived data and are **not** persisted as authoritative state; they are recalculated from current scene + current workspace.

The N50 workspace is not a SceneRevision payload and does not pretend to be one. This avoids silently changing the established scene revision/hash contract merely to deliver spatial visualization. The consequence is explicit: future jobs/optimization must capture both an exact SceneRevision identifier/hash and an immutable constraint-workspace hash/snapshot at submission time. N60/N70 stale-result guards must compare those captured inputs before applying results.

Constraint-definition history/Undo is outside N50; geometry/object Undo remains unchanged. Constraint authoring operations are explicit and persist immediately to the workspace.

## 10. Focused automated verification

Add tests only for invariants with meaningful regression cost:

- CAD room/entity payload → G10 evaluation preserves HTDT coordinates
- physical body → conservative radius mapping
- stable `wall_id` ↔ legacy edge ID mapping, including split-created topology
- native wall-clearance rejection maps actual/required values and stable wall ID correctly
- exclusion/walkway violation maps stable subject/region IDs
- hide/lock state is absent from adapter inputs and cannot change evaluation
- violating-candidate policy blocks new/worsened violations without trapping recovery from an existing region violation
- persisted native constraint definitions reopen exactly beside scene storage

Do not add pixel/color snapshot tests.

## 11. A11 Windows gate

RDC remains unused until the final A11 hardware gate.

A11 fixture uses the normal native product composition and real Win32 mouse input:

1. open a scene whose current placement satisfies a walkway/exclusion region and a wall-clearance constraint,
2. drag an object into a wall-clearance violation,
3. verify the move is not silently committed and history does not grow,
4. select the wall-clearance reason with the real mouse,
5. verify subject + wall + rejected candidate + actual/required distance indication,
6. drag another object into the walkway/exclusion region,
7. select that reason with the real mouse and verify subject + region highlighting,
8. verify the UI uses constraint language only and does not describe feasibility as sound quality,
9. verify normal editing still works after rejection,
10. verify the persisted workspace reopens unchanged.

## 12. Explicit non-goals

- no new acoustic quality score
- no optimization/ranking in N50
- no generic collision engine rewrite
- no browser implementation
- no replacement of G10 placement constraint semantics
- no automatic semantic migration of full wall-clearance constraints across topology changes
- no constraint-definition Undo/history in N50
- no RDC coding
