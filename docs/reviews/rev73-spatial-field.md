# REV73 — #999 SpatialFieldResult → CAD viewport overlay

Issue #999 (P0) asked to project the sealed `SpatialFieldResult` into the Room
3D CAD viewport (orthogonal slices + iso + optional volume + 3D probe) with
bounded budgets, sha-pinned staleness, and headless-testable overlay items.

## Confirmed defect fixed

`field_explorer_panel` judged session currency against the session's **own
pinned** `SceneRevision` (`scene_repository.get(session.scene_revision_id)`),
so STALE could never trigger — a field built on an old head stayed "latest"
forever. Both `refresh_sessions` and `_load_session` now compare against
`scene_repository.current_head(document_id)`. `refresh_sessions` also
re-validates the already-loaded session (head can advance while the panel is
open without a session switch).

## Layers

- **`field_volume_visual_adapter.py`** (new, Qt/VTK-free): sealed
  `SpatialFieldResult` + pinned `FieldExplorerSession` + **live head** →
  `FieldDisplayView`. Canonical order `(iz*ny+iy)*nx+ix` ≡ NumPy F-order;
  domain +Y maps to render −Y by reversing the sample axis and moving the
  render origin to `-(oy + (ny-1)*dy)` — VTK spacing stays positive.
  `clim` is computed on the **full-resolution** quantity values before
  decimation so a strided subset can't hide an extremum; display-only
  `float32`; `FIELD_DISPLAY_POINT_BUDGET` (1M) is separate from the canonical
  `MAX_FIELD_SAMPLES`; stride collapses fail closed. Phase is masked at
  nodes via the magnitude threshold and carries `cyclic_colormap`.
  `FieldOverlayScene`/`FieldSliceItem` are the headless-testable overlay
  item descriptors.
- **`room_field_overlay.py`** (new, Qt-free): `RoomFieldOverlayController`
  holds the armed `FieldOverlay3DRequest`; `resolve()` re-fetches the
  session and re-checks the live head **every frame** — a STALE/UNKNOWN
  result blocks instead of painting. `probe_world()` maps render-space
  picks back through `domain = (x, -y, z)` to the canonical `explorer_probe`.
  `generation` invalidates stale async-style callers.
- **`room_viewport.py`**: `render_field_overlay(scene)` /
  `clear_field_overlay()`; all actors under `acoustic-field-` (added to
  `_OVERLAY_ACTOR_PREFIXES`), `pickable=False`; content-keyed `ImageData`
  cache (slider moves don't rebuild the grid); scalar-bar title tracked for
  cleanup; status text names producer/mode/freq/run/revision/CURRENT +
  normalized-display honesty + stride + masked count. `add_volume` is
  try/except → honest fallback line (`ボリューム表示不可 ... → 断面のみ`),
  never a silent drop and never a "field is zero" implication.
- **`room_workspace.py`**: `field_overlay` controller +
  `show_field_overlay_3d(request)` / `clear_field_overlay_3d()`; probe clicks
  intercept `_entity_picked`/`_empty_clicked` only while `probe_armed`;
  Esc (armed-only `QShortcut`) disarms probe and emits
  `field3DProbeDisarmed`; leaving the acoustics context clears actors
  immediately; the armed request survives context switches so returning
  re-shows it.
- **`field_explorer_panel.py`**: staleness fix + `3D CAD表示` group
  (`音場を3D CADに重ねる` / 等値面 / 半透明ボリューム / 3Dプローブ);
  `field3DRequested`/`field3DCleared` signals; toggle gated on CURRENT.
- **`workflow_application.py`**: wires panel signals to the workspace.

## Tests

`backend/tests/test_issue_999_field_viewport.py` — 19 tests:
currency vs live head (CURRENT→STALE on head advance, UNKNOWN on missing
head), axes/Y-flip/positive-spacing/asymmetric-sample correspondence,
full-res clim under decimation, fail-closed budget/unsupported quantity/
phase gate/STALE+UNKNOWN build, slice items on all three planes vs
canonical samples, index snapping, iso fraction + phase-iso block,
controller resolve/probe/clear/cross-document, panel toggle following head
currency offscreen, viewport actor naming + non-pickable + complete cleanup
(`acoustic-field-` prefix swept by `_remove_overlay_actors`).

Also green: `test_cad_field_explorer.py`, `test_room_viewport_semantics.py`.

## Scope notes / known limits

- M1 (slices+slider+probe) and M2 (single iso contour) implemented; M3
  volume is conditional with the required GPU-dependent fallback line.
- The slider is grid-aligned only (issue's M1 requirement); oblique cuts
  deferred by design. The canonical stride governs grid fineness.
- Slice `coordinate_m` comes from the explorer's canonical plane
  coordinates (exact samples), snapped to the strided display grid.
- Real-GPU volume evidence is not verifiable offscreen; M3 falls back
  automatically and says why.
