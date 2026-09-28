# Round 14 — Visualization & Render Truth

Scope: do the app's 2D/3D visual surfaces draw the right thing, at the right
position, with the right data? Verification was done at the actor/data level —
mesh bounds, polyline endpoints, axis modes, tick positions, colormap input
ranges — not just "renders without crashing". The frame convention is
`domain_to_render = (x, −y, z)` and `domain_pose_to_render_matrix = C4·Td·C4`
with C4=diag(1,−1,1); everything below was checked for consistency against it.

## Surface → verified

| Surface | Check | Result |
|---|---|---|
| Entity envelope actors (`_entity_envelope_mesh`) | pv.Cube at `size_m` → C4-conjugated pose transform → world bounds vs model | **Verified**: F1 fixture entities match `size_m` extents at `position` exactly (bounds error 0.00000; float32 storage noise ~1e-7) — new regression test `test_entity_mesh_world_bounds_match_model` |
| Authored bodies (cylinder / extruded polygon / mesh asset) | `_entity_local_mesh` local-space construction + shared pose transform | **Verified**: prism verts `(x,−y,−z/2)` extruded +Z; mesh-asset verts `*scale+offset` with Y negated before pose matrix — consistent chain |
| Semantic glyphs (speaker baffle/driver, screen panel, projector lens, seat back, rack slats) | Glyph must sit on the entity's *facing* side (domain local +Y) and follow orientation | **Verified**: yaw-90° speaker glyph center lands at domain (−x side), i.e. rotates with facing; identity speaker glyph at +Y. Measurement crosshair+bead symmetric |
| Speaker aim/selection rays (`_selection_direction_rays`, `_render_acoustic_overlay`) | Ray origin = `acoustic_reference_position` (rotated offset), direction = aim or local +Y | **Verified**: origin uses the same rotated-offset helper as the evaluator; direction negates render-Y correctly |
| Room shell / floor / grids / guides / search-domain | Convention consistent | **Verified**: floor verts negated Y; guides negate both endpoints; search-domain negates y min/max — no mirrored draws |
| Wall extrusions (`wall_editor.py`) | Domain (x,y)→render (x,−y) | **Verified** |
| Camera: standard views (`apply_standard_view`) | Ortho views (top/front/rear/left/right) get parallel projection + correct look directions | **Verified**: directions/ups negated Y consistently; FRONT=rear→front per documented convention; perspective offset extent-scaled |
| Camera: zoom-to-fit (`fit_scene`, `focus_entities`) | Framing margin | **Verified**: `fit_scene` = reset_camera + zoom(0.92); `focus_entities` tight-fit (no margin — design intent for single entity) |
| Camera: state capture/restore (`capture_camera_state`/`apply_camera_state`) | Round-trip symmetry incl. parallel_scale/view_angle | **Verified**: symmetric Y negation both directions |
| Picking (`pick_actor_at`, `pick_entities_in_region`, `_widget_to_display_position`) | Qt↔VTK Y flip, DPR scaling, marquee interval-overlap of projected actor bounds | **Verified**: `(height−1−y)*dpr` consistent; marquee compares actor projected bounds in the same VTK display space |
| 2D plots — comparison/quality/phase/import (`measurement_page_workspace`) | Log-x where claimed, dB y, all series drawn | **Verified**: all go through `apply_scientific_appearance(log_x=True)` → real pyqtgraph `setLogMode`; ticks at `log10(Hz)` positions with "20/50/100/200/500/1k/…" labels (correct under log mode — verified against pyqtgraph 0.14.0 source); cursor `_display_value` does `10**raw`; difference plot re-adds y=0 line after clear; x-axes linked |
| 2D plots — legacy measurement editor (`measurement_editor`) | Log-x | **Verified**: `setLogMode(x=True)`, default log ticks |
| 2D plots — robustness (`optimization_robustness_controller`) | Sensitivity bar chart category ticks; distribution histogram bins/weights | **Verified**: categorical x with axis-label ticks (not pretending log); histogram uses `np.histogram` with `sampled_weights` only when `probability_supported` |
| Robustness 3D overlay (`optimization_robustness_overlay`) | min/nominal/max markers & aim rays at model positions; infeasible markers | **Verified**: `_render_point` = (x,−y,z) everywhere; infeasible markers cover angular-only samples too (marker at entity position) — initial suspicion retracted on read |
| Video overlay (`render_video_overlay`) | Lens cone, aperture ring, sightlines, collision chords at evaluated geometry | **FIXED**: sightline eye was drawn at `seat.position + local eye offset` *unrotated* — the evaluator (`_seat_eye`→`_world_offset`) rotates the offset by seat orientation, so a yaw-90° seat drew its sightline from a point **0.42 m** away (proven numerically). Also, seats with no eye authority (legacy bindings, `eye_position=None`) drew a fabricated sightline from a guessed point. Now uses `evaluation.viewing[seat].eye_position` and skips seats without eye authority. Covers `DirectViewGeometryEvaluation` too (same `.viewing` shape) |
| Measurement point / direction overlay (`render_measurement_overlay`) | Marker at acoustic reference; direction only when recorded | **Verified**: no direction invented |
| Floor-plan underlays (`_render_underlays`, `room_underlay.py`) | Quad corners (u→right, v→down) ↔ texture coords; calibration scale honesty | **Verified**: per-vertex texcoords pin image corners to domain corners; uncalibrated preview uses a declared 100 px/m display-only scale and never feeds snap hints |
| Field explorer (`field_explorer_panel`) | Slice orientation + scale-bar honesty + masked samples | **Verified**: plan keeps room front at image top (matches TOP view); vertical sections z-up; `lo/hi` = min/max of *rendered* (unmasked, finite) samples; masked/NaN cells render as a separate gray, not through the ramp |
| Frontend Plotly surfaces (`frontend/src/plots.tsx`) | Log axis claim; axis remapping honesty | **Verified**: `xaxis.type:'log'` real log; RoomPlot maps (x_m→x, z_m→y-up, y_m→z-rear) — a deliberate y/z swap that is internally consistent and labeled |
| Selection↔inspector sync | Actor pick → `entitySelected` → same entity highlighted/inspected; marquee actor→entity map | **Verified**: id-keyed maps (`_actor_entity_ids`, `actor_ids`) throughout; glyph/ghost actors map back to their entity |
| Gizmo (`cad_gizmo`, `native_editor`) | Axis colors, drag deltas, rotation sign | **Verified**: translation axes = render C4 axes (Y arrow points domain +Y); `render_delta_to_domain` round-trips the drag delta; rotation uses axial-vector convention −C so +angle = +domain rotation |
| Empty/partial data (`scientific_plot_style.show_plot_state`, trace pipeline) | Honest gaps vs fabricated content | **Verified**: designed empty states; smoothing windows always contain the center sample (no empty windows); phase absent → trace omitted, panel hidden; measurement direction ghost only when recorded |

## Findings & fixes

1. **Sightline overlay drew the eye point unrotated and fabricated eyes for legacy bindings** —
   `render_video_overlay` computed `eye = seat.position + eye_reference_offset_local_m`
   in the world frame, ignoring seat orientation; the evaluator's `_seat_eye`
   applies `_world_offset` (orientation-rotated). Effect: every rotated seat's
   sightline started at a wrong world point (0.42 m lateral error for a yaw-90°
   seat with a 0.30 m forward eye offset — a typical angled theater seat), and
   seats whose bindings carry no eye authority still got a line drawn from a
   guessed point, contradicting the evaluator's UNKNOWN. Fixed by consuming
   `evaluation.viewing[seat].eye_position` (the evaluator's own eye) and
   skipping seats where it is `None`. `backend/src/htdt/room_viewport.py`

## Deferred / intentionally not changed

- `focus_entities` zooms tight to bounds without the 0.92 margin `fit_scene`
  adds — reads as design intent (single-entity focus), not flagged.
- The analytic field ramp (`_ramp_rgb`) tops out at orange-ish (255,127,0)
  rather than a deep red — cosmetic nuance inside an already-honest sequential
  ramp; not a truth defect.
- `_widget_to_display_position`'s y flip is `(height−1−y)*dpr`, up to ~1 px
  off at non-1.0 DPR — well below pick tolerance; left as-is.

## Verification

- `backend/tests/test_room_viewport_r14.py` (new, 3 tests): rotated-eye
  sightline endpoint asserted against `_seat_eye` output *and* asserted ≠ the
  old unrotated point; legacy seat → zero sightline actors; envelope actor
  bounds == model extents.
- Targeted suite: `test_room_viewport_r9.py`, `test_room_viewport_r14.py`,
  `test_room_viewport_semantics.py`, `test_cad_video_geometry.py`,
  `test_cad_direct_view.py` — all pass.
- Numeric actor audit (this session): F1 fixture — every envelope bounds equals
  `position ± size_m/2` (render-y negated) with 0.00000 error; semantic glyphs
  land on the facing side under 90° yaw; evaluator-vs-old-drawn eye = 0.4243 m.
- Full suite: see PR body / CI note.
