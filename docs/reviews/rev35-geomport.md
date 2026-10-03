# REV35-GEOMPORT — wall/opening geometry-editing parity: enumeration + scoping

Scope: enumerate every wall/opening edit operation the legacy standalone
editors (`room_editor.py`, `wall_editor.py`) offer versus the workflow Room
workspace (`room_geometry_input.py`, `room_geometry_panel.py`, `room_viewport.py`,
`room_workspace.py`), implement the cheap unported items, and scope the rest.

## Implemented this session

| Item | Legacy source | Workflow landing | Commit |
|---|---|---|---|
| Grid snap on floor picks | `room_editor.py::_screen_to_floor` rounds to `view_state.grid_step_m` when `grid_snap_enabled` (~L557) | `room_geometry_input.py::_screen_to_floor` now calls new `::_snap_floor` reading the same `EditorViewState` fields (`cad_document.py::EditorViewState.grid_snap_enabled`/`grid_step_m`); covers sketch picks, vertex drags, and wall drags | `0686bb96` |
| Whole-segment wall select | `wall_editor.py::_hit_wall` — `_point_segment_distance` ≤12 px anywhere on the wall body | `room_geometry_input.py::_hit_handle` — same math via new `::_point_segment_distance` ≤10 px; clicks within 14 px of either endpoint stay suppressed so the 12 px vertex grab keeps winning | `6fdff7ac` |
| Openings drawn for every wall | `wall_editor.py::_render_wall_overlay` renders all openings whenever topology exists | `room_geometry_input.py::_render_edit_handles` — all topology walls drawn; selected wall keeps `selection_outline`, others `geometry_edge`; endpoints resolve by `vertex_id` against the preview set so drags track | `4a09b72e` |
| Clearance-binding authoring | `wall_editor.py::add_clearance_binding` — spin 0–10 m default 0.30 + 「クリアランス参照を追加」 | `room_geometry_panel.py` wall host — 参照件数 label + 「追加するクリアランス」 spin (same range/default) + 「クリアランス参照を追加」 button → `cad_walls::add_constraint_binding` → `controller.replace_room_topology` | `833e064f` |

Regression coverage: `backend/tests/test_rev35_geomport.py` (7 tests) —
snap rounding/toggle/step-0, segment hit + vertex-zone priority,
all-wall + unselected opening rendering incl. accent/muted colors,
binding add + undo + bound-wall `WallTopologyError` delete refusal,
and selection-state visibility.

## Full parity enumeration

Already ported before this session (verified, no change forced):

| Operation | Legacy | Workflow |
|---|---|---|
| Room sketch + stacked-vertex / self-intersection guard | `room_editor.py::start_room_sketch` / `close_room_sketch` | `room_geometry_input.py::start_sketch` / `_close_sketch` (same shapely pre-validation; refuses redraw when openings/bindings exist) |
| Vertex drag with preview | `room_editor.py` mouse handlers | `room_geometry_input.py::_mouse_press/move/release` |
| Insert vertex at edge (midpoint) | `room_editor.py::_insert_vertex_toggled`/`_insert_room_vertex` | `insert_selected_edge_midpoint` — topology-aware `split_wall` with `{wall_id}:a:{token}` ids |
| Delete vertex | `room_editor.py::delete_room_vertex` | `delete_selected_vertex` — topology-aware via predecessor `delete_wall` |
| Numeric vertex X / Y | `room_editor.py::_numeric_room_vertex_edited` | `set_selected_vertex_coordinates` + panel 頂点 X/Y fields |
| Numeric edge length | `room_editor.py::_numeric_room_edge_edited` | `set_selected_edge_length` + panel 辺の長さ |
| Ceiling height | `room_editor.py::_numeric_room_height_edited` | `set_room_height` + panel 天井高 |
| Derive/ensure wall topology | `wall_editor.py::_ensure_wall_topology` | `ensure_wall_topology` + panel 壁編集を有効化 button |
| Wall drag move | `wall_editor.py` drag via `move_wall` | `_mouse_press/move/release` wall drag preview → `move_wall` |
| Split wall | `wall_editor.py::split_selected_wall` (midpoint) | `insert_selected_edge_midpoint` (same domain `split_wall`, midpoint-only UI parity) |
| Merge with next wall | `wall_editor.py::merge_selected_wall_with_next` | `merge_selected_wall_with_next` (raises on wrap: 「末尾と先頭の壁結合は現在のUIでは未対応です」) |
| Delete wall | `wall_editor.py::delete_selected_wall` | `delete_selected_wall` (domain refuses when <3 walls would remain, on thickness mismatch, or referenced openings/bindings) |
| Wall thickness | `wall_editor.py::_numeric_wall_thickness_edited` (0.001–2.0 m) | panel 厚さ field (0.001–5.0 m — superset) |
| Door opening add (auto-centered) | `wall_editor.py::add_door_opening` — door only | panel opening host — full CRUD: kind door/window/passage/other, offset/width/sill/height, 開放として扱う, add/apply/delete via `add_opening`/`update_opening`/`delete_opening` (superset) |
| Undo / recovery | shared | `workspace.undo()` + `recovery_candidate` + `mark_pending_editor_rejected` |
| Arrow-key nudge by grid step | — (not in legacy) | `room_geometry_input.py::_key_press` (workflow-only extra; Shift ×10) |
| Acoustic treatment authoring | `room_editor.py` acoustic treatment dock | `room_acoustics_panel.py::RoomTreatmentPanel` (#451 — separate ported surface, out of this scope) |
| Wall clearance **solver** constraint | — (bindings are dormant data) | `room_constraints_panel.py` + `CadWallClearanceConstraint` — the live constraint system solvers read; `WallConstraintBinding` remains the topology payload this task ports |

## REV36-GEOMPORT2 — final parity state

Follow-up wave landed the remaining implementable items (PR pending, branch
`devin/*-rev36-geomport2`):

| Item | Resolution |
|---|---|
| E2 binding delete authority | `cad_walls::delete_constraint_binding` + panel selector/削除 — bound walls are deletable once the binding is removed |
| E3 wrap-around merge | `merge_walls` accepts the last→first pair (seam vertex dropped, merged wall becomes the last boundary edge); panel relabels 「先頭の壁と結合」 on the last wall |
| E4 arbitrary-offset split | `insert_selected_edge_vertex(offset_m)` on the controller (midpoint is now `None`); panel 「分割位置」 spin + 「指定位置に頂点追加」 |
| E5 binding edit | `cad_walls::update_constraint_binding` + panel 適用 (clearance value only; `wall_ids` membership editing stays out of scope — no UI can create multi-wall bindings yet) |
| E1 always-on overlay | **Skipped intentionally** — legacy draws prisms/openings into the normal render, workflow deliberately keeps normal view uncluttered and edit mode already draws all openings (#494). Toggle vs always-on is a product/UX call, not a port |

Regression coverage: `backend/tests/test_rev36_geomport2.py` (14 tests) —
wrap merge (collinear seam, non-collinear refusal, opening/binding
migration, triangle guard, controller path), binding update/delete
(targeted removal, unknown-id refusal, unblocked wall delete, dangling
refusal), offset split (position, out-of-range, opening side-migration),
and panel coverage for the spin bounds, merge relabel, selector
apply/delete.

## Unported items — scoped for next wave

### E1 — Always-on wall prism + opening overlay in normal view (MEDIUM, ~1 session)

Legacy `wall_editor.py::_render_wall_overlay` draws `_wall_prism` thickness
prisms for all walls plus all opening outlines whenever topology exists —
i.e. the overlay is part of the *normal* render. The workflow viewport
(`room_viewport.py::render_document`) renders `_room_wireframe` +
`_room_floor_mesh` only; openings/thickness are invisible outside edit mode.

Decomposition:
1. Decide the UX contract first: always-on overlay, or tied to a
   "壁・開口を表示" view toggle in overlay controls. Legacy behavior argues
   always-on; viewport decluttering argues toggle — needs a product call.
2. Port `_wall_prism` prism generation into `room_viewport` (or a small
   `room_wall_overlay.py` helper): box meshes per wall using
   `WallSegment.thickness_m`, room height, boundary direction.
3. Reuse the `_render_edit_handles` opening-rectangle math (extract shared
   helper rather than duplicating).
4. Actor lifecycle: names under a `wall-overlay-*` namespace removed on
   re-render, same pattern as `_opening_actor_names`.
5. Tests: recording-plotter assertions on actor names/counts + one
   real-GUI screenshot pass (Mesa).

Effort: 0.5–1 session depending on the toggle decision.

### E2 — `WallConstraintBinding` delete/update authority (SMALL-MEDIUM, ~0.5 session) — **DONE in REV36**

`cad_walls.py` has `add_constraint_binding` but **no delete or update** —
nothing anywhere can remove a binding, and `delete_wall` fail-closes on
bound walls (`constraint_bindings` dangling check). A user who adds a
binding by mistake must undo or is permanently blocked from deleting that
wall.

Decomposition:
1. `cad_walls.py`: `delete_constraint_binding(room, topology, binding_id)`
   returning validated `WallTopology` (mirror `delete_opening`'s shape).
2. Panel: bindings list row on the wall host (or a selector like the
   opening one) + 削除 button; JA strings.
3. Decide whether multi-wall bindings (`wall_ids` tuple len > 1) get a
   different row presentation — model allows them; neither UI can create
   them yet.
4. Tests: delete removes only the target binding, undo restores,
   bound-wall delete succeeds after binding removal.

Effort: 0.5 session. Recommend doing before any UI that creates bindings
at volume — currently only one button exists so urgency is moderate.

### E3 — Wrap-around merge (last ↔ first wall) (SMALL, ~0.5 session) — **DONE in REV36**

Domain `merge_walls` requires *ordered* neighbors and both UIs refuse the
wrap pair — legacy: 「この壁には順方向の結合対象がありません」; workflow:
「末尾と先頭の壁結合は現在のUIでは未対応です」. A rectangle edited down to
4 walls can never merge across the seam.

Decomposition:
1. `merge_walls` — either accept the wrap pair explicitly (normalizing
   order) or add `merge_walls_wrapped`/`cyclic=True`; must still enforce
   collinearity + same thickness and migrate openings/bindings with
   correct offset math (the merged wall's coordinate origin changes).
2. UI: enable the button for the last wall (call it 「先頭の壁と結合」 or
   route through the same action).
3. Tests: wrap merge on a rectangle corner (non-collinear → error stays),
   collinear L→rectangle merge, opening offset migration across the seam.

Effort: 0.5 session; the domain change is the risky half (offset math).

### E4 — Arbitrary-offset wall split (SMALL, ~0.5 session) — **DONE in REV36**

`split_wall(room, topology, wall_id, offset_m=...)` accepts any strictly
interior offset; both UIs expose only midpoint (`_insert_room_vertex` /
`insert_selected_edge_midpoint`). A user wanting an asymmetric split must
split-then-drag.

Decomposition: panel 分割位置 spin (0 < offset < length, default midpoint)
on the edge/wall host → pass through to `split_wall`; opening migration is
already by-side in the domain; test boundary offsets and opening-crossing
refusal. Effort: 0.5 session, mostly UI.

### E5 — Binding editing beyond add/delete (SMALL, ~0.5 session, follow-on to E2) — **DONE in REV36** (clearance only; multi-wall membership editing deferred — nothing can author such bindings)

Legacy had no binding edit either, but a complete inspector wants
clearance value updates + multi-wall membership. Needs domain `update`
verb + selector UI. Defer until E2 lands; design with the
`CadWallClearanceConstraint` panel's distance field for consistency.

### Explicit non-goals (parity requires domain-model work, out of scope)

- Curved walls, arcs, chamfers — `WallSegment` is a straight from/to pair;
  adding curvature is a domain-model change, not a UI port.
- Multi-select / box-select wall ops — single-selection model in both
  paths.
- Non-rectangular openings (arched doors, circular windows) —
  `WallOpening` models offset/width/sill/height rectangles only.
- Legacy numeric wall thickness upper bound divergence is intentional:
  panel range (5.0 m) is a deliberate superset of legacy (2.0 m).
