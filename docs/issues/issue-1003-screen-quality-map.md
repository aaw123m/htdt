# Issue #1003 — screen quality map (ANSI 9-point) over the real image surface

## Scope

P1 [映像×3D]: measured 9-point luminance/chromaticity/focus quality
evidence overlaid on the exact screen surface in the Room 3D viewport.

The spatial-image authority already stores sealed plans, measurement
sets, derived maps and uniformity evaluations
(`cad_spatial_image_authority.py` / `cad_spatial_image_repository.py`).
This slice renders that evidence onto the **evaluated image plane** —
`VideoGeometryEvaluation.projection.image_plane_corners` for
projection, `DirectViewGeometryEvaluation.surface.image_plane_corners`
for direct view — it never invents its own screen geometry.

## Model — `backend/src/htdt/room_screen_quality_map.py`

Qt-free resolver (mirrors `room_treatment_overlay.py`):

- `QualityMapSelection` — explicit operator selection: measurement set,
  quantity, viewpoint, heatmap toggle.
- `resolve_screen_quality_map(scene_repository, spatial_repository,
  document_id, selection, evaluation, asset_store=None)` resolves a
  `ScreenQualityMapScene` **against the current scene head on every
  call** — nothing renders from a stale revision.
- `ScreenQualityMapController` — workspace-side cache keyed on
  `(head.revision_id, head.content_hash, evaluation identity,
  selection, set sha)`.

### Surface mapping (R2)

`x_fraction`/`y_fraction` map onto the real quad by bilinear
interpolation (`_bilinear_on_quad`): `u = x_fraction` along the
aperture's local right axis, `s = 1 - y_fraction` up from the bottom
edge (the plan's y is measured *from the top*, ANSI convention).
Because corners are world-space, rotation, flips, portrait aspect and
off-axis projection all map exactly — nothing is re-projected.

A declared `physical_xyz_m` is **verified** against the computed
surface position: mismatch above
`max(0.05 m, 2 % of min surface dimension)` yields a `misaligned`
marker and a notice — never auto-corrected.

### Marker states

| State | Meaning |
|---|---|
| `measured` | an observation exists for (point, viewpoint, quantity) |
| `unmeasured` | plan declares the point; no observation — drawn hollow |
| `misaligned` | physical_xyz_m disagrees with the surface position |
| `unscaled` | observed but quantity+unit has no absolute color scale |

Observations bind strictly by `(point_id, viewpoint_id, quantity)` —
one point's reading is never reused for another point or another
viewpoint.

### Absolute color scales (R4)

`scale_color_for(quantity, units, value)` maps to a fixed 6-band blue
→ red palette keyed on absolute thresholds per quantity+unit
(`_ABSOLUTE_SCALES`): white/black luminance (nits or fL), ANSI
contrast (`:1`), color error (ΔE), EOTF/gamma tracking, focus
sharpness (%), convergence fringe (px/arcmin). Unit aliases
(`nits`→`cd/m²`, `ft-L`→`fL`, `ratio`→`:1` …) normalize before
lookup; a quantity+unit with no defined scale yields `unscaled`
markers + a read-only reason — the scale is never guessed.
`white_chromaticity` renders the measured x,y pair as its own swatch
(`xyY`→sRGB, peak-normalized) — chromaticity is never folded into a
luminance scalar.

### Heatmap honesty (R5)

The heatmap cell layer draws **only** when a persisted
`CadSpatialDerivedMap` for the selected set+quantity carries a
`rendered_artifact_ref` whose bytes verify:

1. `managed_assets.ManagedAssetStore.read_file` bounds the read.
2. `sha256(bytes) == ref_sha256` — re-verified, never trusted.
3. JSON payload must declare
   `kind == 'screen-quality-map-artifact'` and the same `quantity`.
4. `values[r][c]` maps bilinearly onto the same quad — one cell per
   grid element, `None` cells stay transparent.

Any failure → the notice explains why and **no colors are synthesized
from the record alone**. The scene carries algorithm, version, grid,
smoothing, extrapolation and uncertainty for the legend.

### Coverage verdict (R6)

Per-quantity coverage shows the persisted
`CadImageUniformityEvaluation` verdict when one exists for the exact
plan+set pair; otherwise a live `evaluate_spatial_uniformity` runs
uncoupled from criteria so `criterion_unbound` is reported honestly.
`expected_points`/`observed_points`/`missing_roles` are surfaced per
quantity — observations are shown, never re-judged.

### Stale-reuse guards

Resolved read-only when:

- `evaluation.target.scene_content_hash != scene_content_hash(head)` —
  the scene moved under the evaluation;
- `set.projector_state.projector_ref.ref_sha256` disagrees with the
  evaluation's spec sha — the projector state swapped;
- `plan.screen_state.screen_ref.ref_id` ≠ the bound surface entity —
  the screen swapped;
- `set.plan_ref` doesn't pin the loaded plan sha.

Plan-vs-set requirement drift (8 projector-state fields) and
`evidence_kind='predicted'` surface as non-blocking notices.

## Viewport — `backend/src/htdt/room_viewport.py`

- `'quality-map-'` registered in `_OVERLAY_ACTOR_PREFIXES`
  (auto-clear), all actors `pickable=False`.
- `render_screen_quality_overlay(scene)` draws measured disc markers +
  edge rings on the surface quad with polygon offset, optional heatmap
  cells, and an ASCII status line (`quality-map-status`); text-only
  scenes get an honesty banner. A `revision_content_hash` staleness
  guard aborts the draw before anything paints.
- `clear_screen_quality_overlay()` removes the prefix's actors.

## UI

- `RoomVideoPanel` gains a 「スクリーン品質マップ（9点測定）」 section:
  enable checkbox, measurement-set / quantity / viewpoint combos
  (quantities list only what the set observed), heatmap checkbox
  (disabled unless a verified derived map exists), per-point list,
  detail readout (value/unit or x,y pair, instrument, uncertainty,
  stimulus, viewpoint, timestamp, physical mismatch), coverage and
  binding labels, and a 「再測定手順へ…」 deep link (R1, R7).
- `RoomWorkspace` instantiates `ScreenQualityMapController` on the
  persisted `CadSpatialImageRepository`, wires
  `qualityMapChanged`→re-resolve+re-render and
  `qualityMapRemeasureRequested`→`WorkspaceDeepLink(WorkspaceId.VIDEO,
  'verify')` — the commissioning workspace's re-measure/compare page.
  The overlay resolves and renders in the deferred-render block only
  while the video context is active.
- `VideoCommissioningWorkspace` verify page gains a
  「スクリーン品質マップを開く」 button deep-linking back to the Room
  surface — the loop closes in both directions (R1).

## Tests — `backend/tests/test_issue_1003_screen_quality.py`

ANSI-9 exact quad positions, center-only, partial-corner unmeasured
cells, rotated/off-axis screen, portrait DirectView surface, quantity
dropdown membership, chromaticity-as-own-channel, stale evaluation →
read-only, projector-spec swap → read-only, screen swap → read-only,
per-viewpoint isolation, physical_xyz_m mismatch → misaligned (and
within-tolerance → measured), no evaluation → read-only, heatmap
artifact verified/tampered, controller cache re-resolve on new head,
absolute-scale invariance, unscaled quantity → read-only + reason.

Adjacent suites green: `test_rev57_proj`, `test_cad_video_geometry`,
`test_room_workspace`, `test_cad_video_commissioning`,
`test_issue_1009_treatment_overlay`, `test_cad_video_measurements`.
