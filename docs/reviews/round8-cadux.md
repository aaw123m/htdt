# Round 8 — CAD editing-experience depth (Qt surface)

Scope: the shipped `RoomWorkspace` editing surface (`room_workspace.py`,
`room_viewport.py`, `room_geometry_input.py`, `room_transform_input.py`,
`room_measure_input.py`, `cad_input.py`, `command_registry.py`,
`workflow_application.py`). Prior rounds 1–7 were read first — esp.
`round5-frontend` (React-SPA vertex nudge reference), `round6-ux`/`round6-spec`
(display-unit policy #496 wiring) — and their findings are not re-reported.
Branch `devin/rev8-cadux`. All verification is local (`pytest -q -n 4`,
Python 3.12.10, `QT_QPA_PLATFORM=offscreen`).

Note on scope: `room_editor.py`/`WallEditorWindow`/`TheaterWorkflowWindow`
are legacy standalone windows not reachable from the shipped product shell
(`build_workflow_application`/`WorkflowShellWindow`); their display-policy
gaps stay deferred, matching the round-6 treatment.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | Palette shows shortcuts (Del/H/L/T) for daily verbs but no QShortcut was ever materialized — keys dead | HIGH ux | FIXED |
| 2 | No keyboard nudge for entities/vertices/walls in the shipped Qt surface (React SPA parity gap) | MED ux | FIXED |
| 3 | "Zoom to selection" only recentred the camera focal point — no zoom/framing | MED ux | FIXED |
| 4 | `format_measure_result` hardcoded m/° — measure panel ignored the #496 display-unit policy (round6 D3) | MED spec | FIXED |
| 5 | Underlay click while not calibrating was swallowed — dead clicks, no deselect/free-point | MED ux | FIXED |
| 6 | No select-all / invert / none on the entity surface | LOW ux | FIXED |
| 7 | Esc stops modes/drag but leaves a stale selection — second Esc a no-op | LOW ux | FIXED |
| 8 | Delete/Backspace silent no-op in geometry-edit mode (vertex/wall selected) | MED ux | FIXED |
| 9 | No rubber-band box select | MED ux | DEFERRED — needs frustum-pick plumbing, sketched below |
| 10 | Clicking through occlusion cycles nothing — stacked entities unreachable by mouse | MED ux | DEFERRED — needs picker depth list cycling |
| 11 | Constraint add/remove bypasses the undo stack (`constraint_repository.save` persists immediately) | MED spec | DEFERRED — authority design call |
| 12 | Inspector keeps position/rotation following display policy (round6 verified); footprint readout stays metres | LOW spec | VERIFIED; footprint labelled "物体ローカル m" intentionally — local-frame axes, not world lengths |

## 1 — Declared-but-dead shortcuts for daily verbs (ux, fixed)

`command_registry` declares `shortcut='Delete'/'H'/'L'/'T'` on
`room.edit.delete`, `room.edit.toggle_hide`, `room.edit.toggle_lock`,
`room.measure` — and the command palette renders those badges — but
`CAD_SHORTCUT_COMMAND_IDS` in `cad_input.py` (the only list
`CommandShortcutBinder` materializes QShortcuts from) omitted all four.
The verbs were reachable only via palette/menu clicks.

Fix: extended `CAD_SHORTCUT_COMMAND_IDS` with the four ids plus the new
`room.select.all`/`room.select.invert`. This is safe where the legacy
editor never runs the workflow shell: the binder's `execute()` re-checks
live availability on every activation, and workspace `deactivate()`
unbinds the commands, so shortcuts become inert no-ops outside the room
page (`command.blocked.unavailable_in_context` — already the guarded
pattern for `project.save`).

`room.edit.delete` also gained `shortcut_aliases=('Backspace',)` for
compact keyboards.

Regression: `test_daily_edit_verbs_get_real_qshortcuts` asserts every new
id materializes a `QShortcut` and Delete binds both Delete and Backspace;
`test_shortcut_activation_noops_until_executor_bound` proves activation is
a no-op while unbound and fires once bound.

## 2 — Keyboard nudge parity (ux, fixed)

Round5 shipped vertex-drag nudge only in the React SPA; the shipped Qt
surface had no arrow-key path at all — `RoomEntityTransformController`
ignored `KeyPress` (early return when `self.mode is None`) and the
geometry input controller had no key handler.

Fix, two sides of one convention:

- `RoomEntityTransformController.nudge_selection(dx_m, dy_m)` — refuses
  while an armed transform/drag or geometry-edit is active, when `can_edit`
  is false, or when the selection contains locked entities (status names
  the locked ids). Routes through
  `working.begin_group_move → preview_group_move → commit_gate →
  commit_preview → propagate_constraints` — i.e. the exact same authority
  path as a mouse drag, so one keypress = one undo step and constraint
  ripple behaves identically. `_key_press` maps arrows to ±grid-step on
  X/Y in domain space; `Shift` multiplies by 10 (matching MetricSpinBox
  modifier semantics); Ctrl/Alt/Meta-modified presses pass through for
  other bindings. `eventFilter` was restructured so `KeyPress` is handled
  before the `mode is None` early return.
- `RoomGeometryInputController._key_press` — only while `mode == "edit"`:
  arrows nudge the selected vertex via `set_selected_vertex_coordinates`
  or the selected wall via `move_wall` + `replace_room_topology` — both
  already undoable commit paths — with the same step/Shift convention.
  `WallTopologyError`/`ValueError` surface as status errors and consume
  the key (fail-closed, no silent corrupt).

Regression: `test_entity_nudge_moves_selection_by_grid_step_as_one_undo`,
`test_entity_nudge_moves_whole_group_as_one_undo`,
`test_nudge_is_inert_while_geometry_edit_owns_keys`,
`test_vertex_nudge_commits_through_undoable_room_authority`,
`test_wall_nudge_moves_selected_edge_as_undoable_topology`.

## 3 — Zoom-to-selection framed nothing (ux, fixed)

`RoomViewport3D.focus_entity` did `camera.set_focal_point(center)` — a pan,
not a zoom; "zoom to selection" (F key and the focus-selection tool) never
changed the frame.

Fix: `focus_entities(entity_ids)` computes the union of render-space
bounds over `position ± size_m/2` (0.25 m per-axis floor so sizeless
measurement points always frame) and calls
`plotter.reset_camera(bounds=…)` — supported since pyvista 0.46, verified
against the vendored signature — keeping the current view direction.
`focus_entity` delegates with a one-tuple. `RoomWorkspace.fit_selection`
prefers `focus_entities` for multi-select and `getattr`-falls back to the
legacy `focus_entity` so fake viewports in the harness keep working.

Regression: `test_fit_selection_passes_full_selection_to_viewport`,
`test_focus_entities_frames_entity_bounds` (exact bounds against the F1
fixture).

## 4 — Measure text ignored the display-unit policy (spec, fixed — round6 D3)

`format_measure_result` emitted `'距離 %.3f m'`/`'ΔX %+.3f'` unconditionally
while positions elsewhere follow `LengthDisplayPolicy` (#496). The round-6
deferral is now safe to close: `format_measure_result(result, policy=None)`
keeps byte-identical SI output when no policy is passed (existing tests
pin `'距離 1.000 m'`/`'ΔX +1.000'`/`'方位 +90.0°'`), and formats through
`format_length_m` otherwise — `distance`, `ΔX/ΔY/ΔZ`, `水平` all carry the
display unit; angles stay degrees (the policy only covers lengths).
`RoomMeasurePanel.set_length_policy` re-renders the label and clipboard
copy; `bind_measure_display_length_policy` subscribes to the preference
store's `display_input.*` keys (weakref — panel teardown can't hold the
store) and is wired in `_make_room` beside the inspector binding.

Regression: `test_format_measure_result_follows_display_length_policy`,
`test_measure_panel_reformats_result_and_copy_under_policy`.

## 5 — Underlay clicks swallowed outside calibration (ux, fixed)

`_underlay_clicked` called `handle_underlay_click` and returned — when no
two-point calibration was armed it returned `None` and the click vanished:
no deselect, and measure mode couldn't take a free point over the plan
image. Now `None` falls through to `_empty_clicked` using the viewport's
last display position (guarded `getattr` — fake viewports without the
attribute still no-op cleanly).

Regression: `test_underlay_click_deselects_when_not_calibrating`,
`test_underlay_click_feeds_measure_free_point_when_not_calibrating`.

## 6–7 — Selection verbs + Esc deselect (ux, fixed)

Three new non-mutating commands (`mutates_managed_data=False`, added to
the fail-closed classification allowlist): `room.select.all` (Ctrl+A),
`room.select.invert` (Ctrl+I), `room.select.none` (palette only — no
shortcut so it can't collide with future bindings). All exclude hidden
entities — hidden items can't be clicked, so `select_all` keeping them
out preserves the invariant that every selected entity is actionable.
`cancel_active_operation` gains a final fallback: when nothing else is in
flight, Esc clears the selection and returns True.

Regression: `test_select_all_skips_hidden_and_invert_is_relative`,
`test_escape_clears_selection_once_nothing_is_in_flight`.

## 8 — Delete dead inside geometry edit (ux, fixed)

`delete_selection` only looked at the entity selection — while the
geometry editor owned `mode="edit"` with a vertex or wall selected,
Delete silently did nothing. The command's availability also blocked it
(`selected_id is None` during vertex-only selection).

Fix: `delete_selection` routes to `delete_selected_vertex` /
`delete_selected_wall` when geometry edit is active (EditStateError /
ValueError → `_set_operation_error`, fail-closed), and the command's
availability lambda now accepts a geometry vertex/edge selection while
`can_edit` holds and no transform drag is armed.

Regression: `test_delete_targets_selected_vertex_while_geometry_editing`
(commit + undo), `test_delete_without_geometry_selection_is_noop`.

## Deferred with sketches

**9. Rubber-band box select.** `RoomViewport3D` already has
`plotter.enable_rubber_band_style()` capability upstream, but a faithful
port needs a camera-space pick (`plotter.pick` over a rect is deprecated
in pyvista 0.49; use `vtkRenderedAreaPicker` + world→visibility test per
entity AABB in `focus_entities`-style bounds), plus an EntityTool toggle
in the palette. Sketch: add `RectSelectTool` emitting `entitiesPicked`
through the viewport controller → `view_state.set_selection(additive=…)`
respecting Shift.

**10. Click-through occlusion cycling.** The interactor picks the topmost
actor only. pyvista exposes `pick_all` on `vtkCellPicker` runs
(`plotter.renderer.pick`); a `pick_candidates(position)` → cycle buffer on
repeat clicks at the same screen point (~4 px hysteresis) would match
Blender Alt-click semantics. Needs a per-entity hit test over
`focus_entities` bounds first — defer until box-select plumbing exists
(the shared picker work).

**11. Constraint edits bypass undo.** `save_constraints` →
`constraint_repository.save` persists immediately; `RoomWorkingDocument`
only tracks scene mutations. Making constraints undoable means either
joining them into the document transaction (authority change) or a
parallel undo stack (UX hazard) — a design call, not a bug fix.

## Tests run

`pytest -q -n 4 -p no:warnings` (Python 3.12.10, QT_QPA_PLATFORM=offscreen):
full suite green — see the session report for the exact count.
New: `tests/test_room_cadux.py` (14 tests), plus one case appended to
`test_cad_measure.py` and the `room.select.*` ids added to the
mutation-classification allowlist in `test_command_registry.py`.
