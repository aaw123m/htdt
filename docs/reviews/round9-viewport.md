# Round 9 — 3D viewport & visualization quality

Scope: the shipped PyVista/VTK surface — `room_viewport.py` (scene build,
picking, overlay rendering), `room_workspace.py` (selection wiring),
`field_explorer_panel.py` (field-slice display). Prior rounds 1–8 were read
first — esp. `round8-cadux` deferred items #9 (box select) and #10
(click-through cycling), both implemented here — and their findings are not
re-reported. Branch `devin/rev9-viewport`. All verification is local
(`pytest`, Python 3.12.10, `QT_QPA_PLATFORM=offscreen`); this box has no
working GL (wglChoosePixelFormatARB fails, no osmesa), so render assertions
are fake-plotter based, which is sufficient for every finding below.

Legacy standalone windows (`room_editor.py`, `WallEditorWindow`,
`TheaterWorkflowWindow`, `native_editor.py`) remain out of scope per round 8.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | Every `add_mesh`/`add_point_labels`/`add_text` call in `render_document` rendered the whole scene — one GL pass per actor (~dozens per rebuild, again per overlay layer) | HIGH perf | FIXED |
| 2 | `QtInteractor` default `auto_update=5.0` ran a 200 ms timer re-rendering the scene 5x/s forever while the widget was visible | MED perf | FIXED |
| 3 | Two pick coordinate conventions: `pick_actor_at`/`pick_world_position`/`emptyClicked` fed Qt top-down px into VTK bottom-up APIs → vertically mirrored picks; `entityPicked` emitted VTK coords while `emptyClicked` emitted Qt coords | HIGH correctness | FIXED |
| 4 | Rubber-band box select absent (round8 #9) | MED ux | FIXED — marquee implemented |
| 5 | Click-through occlusion cycling absent (round8 #10) | MED ux | FIXED — front-to-back candidate cycling |
| 6 | Hidden entities still drew labels + acoustic overlays (leak of the "hidden objects cannot be selected" contract #482) | MED correctness | FIXED |
| 7 | Field-slice elevation planes (xz/yz) drawn with the vertical axis horizontal | HIGH correctness | FIXED |
| 8 | Field slice had no colormap scale/units — gradient strip unmappable | MED ux | FIXED |
| 9 | `render_video_overlay` skipped the `scene_content_hash` staleness guard its sibling `render_prediction_results` had — stale evaluation drew a misaligned cone over the moved scene | MED correctness | FIXED |
| 10 | Composite overlay helpers (measure/constraint/video/search-domain/ghost) each triggered their own GL pass on top of the scene render | MED perf | FIXED — one render per helper |
| 11 | No hover highlight on entities | LOW ux | DEFERRED — product/design call, sketched below |
| 12 | Labels not collision-managed; dense scenes can overlap | LOW ux | DEFERRED — needs screen-space label layout |
| 13 | Empty scene / degenerate geometry / focus loss | — | VERIFIED OK — bounds probe falls back to unit cube, camera stays finite; no error spam |

## 1 — Per-actor render storm (perf, fixed)

pyvista's `Renderer.add_actor(render=True)` (the default) calls
`self.parent.render()` on every add. `render_document` issued ~30–80
`add_mesh`/`add_point_labels` calls per rebuild — floor, grid layers, shell,
per-entity body + glyphs + envelope + selection rays + labels — so one
entity edit re-rendered the whole framebuffer dozens of times before
returning. A 50-object scene multiplies that by glyph/envelope actors.

Fix: every `add_*` in `render_document`, `_render_underlays`,
`_render_guides`, `_render_acoustic_overlay`, `_render_labels`,
`render_measure_overlay`, `render_constraint_overlay`,
`render_video_overlay`, `render_search_domain`, and
`render_history_ghost` now passes `render=False`; each public render
entrypoint ends in exactly one `_render()` (compositing callers still
coalesce through `deferred_render`).

Regression: `test_scene_render_is_a_single_draw_not_one_per_actor`
(recording plotter asserts exactly one `render()` and `render=False` on
every add), `test_composite_overlay_helpers_end_in_one_render`.

## 2 — Idle re-render timer (perf, fixed)

`QtInteractor(auto_update=5.0)` is the pyvistaqt default: a 200 ms timer
that calls `render()` while the widget is visible — five full GL passes per
second at idle, indefinitely. Nothing relied on it: all mutation paths
render explicitly, and pyvistaqt removes VTK's move observers.

Fix: `QtInteractor(self, auto_update=False)`.
Regression: `test_idle_render_timer_is_off`.

## 3 — Mirrored pick coordinates (correctness, fixed)

pyvistaqt's `_setEventInformation` already feeds the interactor
bottom-up device-pixel coords (`(height−1−y)·dpr`). The viewport's public
pick entry points then passed Qt top-down DIP coords straight into
VTK-space APIs — so `pick_world_position`/`pick_actor_at` callers
(`optimization_search_domain` handle drag, `room_measure_input` snap)
picked at the vertically mirrored position. Worse, `entityPicked` emitted
VTK coords while `emptyClicked` emitted Qt coords, so consumers couldn't
tell which space a position was in.

Fix: the public surface is now uniformly Qt widget coords;
`_widget_to_display_position`/`_display_to_widget_position` convert at the
VTK boundary (×dpr, y-flip), applied inside `pick_actor_at`,
`pick_world_position`, `pick_actor_candidates`, `pick_entities_in_region`,
and `_last_display_position`. `room_transform_input.world_to_screen` is
self-consistent VTK space and untouched.

Regression: `test_pick_entry_points_share_one_coordinate_convention`
(flip formula + round-trip through both converters).

## 4 — Box select (ux, fixed)

Round8 sketched `vtkRenderedAreaPicker` + per-entity AABB visibility.
Implemented with a dependency-free variant that works without GL: left-drag
beyond `_MARQUEE_THRESHOLD_PX = 6` shows a `QRubberBand` on the interactor;
on release `pick_entities_in_region` projects each entity actor's
`GetBounds()` 8 corners through `world_to_screen` and keeps entities whose
projected AABB intersects the rect. Emits
`entitiesMarqueeSelected(ids, additive)`; `RoomWorkspaceController.
set_selection_many` applies ordered selection (Shift = additive merge,
primary stays current) and persists view state. Left-drag was previously
dead — pyvistaqt removes VTK's `MouseMoveEvent` observers, so the trackball
never sees moves. A release pick landing right after a marquee is
suppressed (`_suppress_next_pick`) so it can't collapse the region
selection.

Regression: `test_marquee_selects_entities_intersecting_projected_bounds`
(direct region query + full press/drag/release gesture incl. Shift-additive
and release-pick suppression).

## 5 — Click-through cycling (ux, fixed)

`vtkCellPicker.GetProp3Ds()` returns every hit prop front-to-back, so the
infrastructure was already in place. `pick_actor_candidates(position)`
maps that list to entity ids; `_cycle_pick_candidate` walks it on repeat
clicks within `_CYCLE_HYSTERESIS_PX = 4` of the same point (Blender-style
Alt-click semantics, minus the modifier), resetting when the cursor moves.

Regression: `test_click_through_cycles_front_to_back_candidates`
(e1→e2→e3→wrap, plus reset on moved position).

## 6 — Hidden entities leaked visuals (correctness, fixed)

`_render_labels` and `_render_acoustic_overlay` ignored `self._hidden_ids`
— a "hidden" speaker still drew its floating label and pressure-overlay
mesh, contradicting the hide verb and #482's "hidden objects cannot be
selected" contract (selectable-by-label but not by-body, and visually
present when supposedly gone).

Fix: both filter `entity.entity_id in self._hidden_ids`.
Regression: `test_labels_do_not_leak_for_hidden_entities`.

## 7 — Field-slice orientation (correctness, fixed)

`extract_field_slice` packs axes as `(axis_row, axis_col, axis_fixed)`; for
`xz`/`yz` planes the elevation axis lands in the row slot, and the pixmap
builder drew rows top-to-bottom — so a wall-elevation slice rendered lying
on its side with the floor on the left.

Fix: `_slice_pixmap` now picks the horizontal axis as `x_m` when present
else `y_m`, the vertical as `z_m` when present else the remaining axis, and
flips the image vertically for elevation planes so increasing z draws
upward. Plan (`xy`) views keep x-right/y-down. Display-side change only —
the hashed `FieldSliceView` authority artifact is untouched, so cached
slices stay valid.

Regression: `test_field_slice_vertical_planes_draw_elevation_up` (max-z
sample at image top), `test_field_slice_plan_view_x_right_y_down`.

## 8 — Colormap had no scale (ux, fixed)

The slice pixmap used a blue→red ramp with no legend — a viewer could not
map a color back to dB/Pa/deg.

Fix: extracted `_ramp_rgb`, added `_scale_bar_pixmap` (gradient strip +
`lo`/`hi unit` labels under the image) and `_slice_stats` (finite-sample
min/max + masked count); the status line now reports
`label · horizontal → / vertical ↑ · lo…hi unit · NxM samples · masked n`.
Units come from the view's `_QUANTITY_UNIT` mapping (dB SPL / Pa / deg).

Regression: `test_field_slice_stats_report_range_and_masked`.

## 9 — Stale video overlay (correctness, fixed)

`render_prediction_results` already skips evaluations whose
`target.scene_content_hash` predates the document; `render_video_overlay`
didn't, so after moving a source the camera cone drew at its stale pose
until re-run.

Fix: same hash guard at the top of `render_video_overlay`.
Regression: `test_video_overlay_skips_stale_scene_evaluation` (stale → no
actors added; current hash → cone meshes present).

## 10 — Overlay helpers each rendered (perf, fixed)

Same per-actor issue as #1, one layer down: each overlay helper issued its
own GL pass after its adds. All now pass `render=False` and end in a single
`_render()`; compositing callers stacking document + measure + constraint +
video + proposal now cost one render total via `deferred_render`.

## Deferred with sketches

**11. Hover highlight.** No hover affordance on entities. Doing it right is
a picker-per-mousemove problem: `vtkCellPicker` on every move is too heavy
for dense scenes; pyvistaqt has no hover pipeline and VTK move observers
are stripped for the trackball. Sketch: install a `QEvent::MouseMove`
filter with a ~30 ms throttle → `pick_actor_candidates` (already built for
#5) → drive a per-actor `Property` opacity/emissive flash via a dedicated
hover actor state, cleared on leave. Ship behind a palette toggle.

**12. Label collision management.** `add_point_labels` places one
vtkTextActor per point with no de-overlap; two stacked entities' labels
collide. Sketch: switch labels to `vtkPointSetToLabelHierarchy` +
`vtkLabelPlacementMapper` (pyvista exposes this through
`add_point_labels` internals — verify the mapper flag) or collect label
anchors and resolve overlaps in screen space post-layout. Product call on
density vs. clutter.

## Tests run

`pytest` (Python 3.12.10, `QT_QPA_PLATFORM=offscreen`, TMPDIR=/c/t):

- `backend/tests/test_room_viewport_r9.py` — 11 new tests, all pass.
- Affected suite: `test_room_viewport_semantics`, `test_room_viewport_r9`,
  `test_room_workspace`, `test_cad_spatial_field`, `test_cad_field_explorer`
  + 20 files importing the touched modules — **70 + 26 … all pass**; the
  only failure seen is
  `test_room_inspector.py::test_metric_spinbox_wheel_requires_focus`, which
  fails identically on clean `main` under the same file ordering —
  pre-existing order-dependent flake, unrelated to this change.
