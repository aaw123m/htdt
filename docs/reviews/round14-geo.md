# Round 14 — geometry & spatial-modeling correctness

Scope: the 3D spatial truth of the app — unit consistency (mm/cm/m/in) across dialog input →
model → scene snapshot → solver adapter → render → measurement anchor; axis/coordinate
conventions (`+X right / +Y rear / +Z up` domain vs the `(x, −y, z)` render world); rotation
math (quaternion ↔ Euler, world-axis vs local edits, seat eye/ear positions); transform
composition (preview/commit/undo honesty); mesh QA → solver-bound gating; sightline segment
geometry; and precision across canonical JSON round-trips and near-degenerate inputs.
Verification is Qt-offscreen (`QT_QPA_PLATFORM=offscreen`) under Python 3.12 + pytest.
Every expected number in `test_round14_geometry.py` was hand-computed from the conventions
below before being asserted. Branch `devin/rev14-geo`.

## The coordinate contract (as verified)

| Layer | Convention | Where enforced |
|---|---|---|
| Domain model | SI metres; `+X` right, `+Y` rear (away from screen), `+Z` up; `coordinate_system='htdt-x-right-y-rear-z-up-m'` | `SceneDocument` field + `Position3`/`Offset3`/`Size3` `_m` suffixed fields |
| Render world | VTK is `(x, −y, z)`; pose matrices conjugate by `C = diag(1, −1, 1)` (`C·R·C`); render deltas re-negate Y only | `domain_to_render`, `domain_pose_to_render_matrix`, `render_delta_to_domain` in `cad_scene.py` |
| Rotation | Quaternions `(w, x, y, z)`; `quaternion_from_euler_deg` = intrinsic Z-Y-X (yaw⊗pitch⊗roll); matrix columns are images of local axes | `cad_scene.py` quaternion/Euler/matrix helpers — hand-verified expansion |
| Entity frame | Local `+Y` is the entity's forward/facing axis; world pose = `R(quat) · local + position` | `acoustic_reference_position`, `_world_offset`, `_entity_local_to_world_xy`, `_equipment_reference_world` — identical convention everywhere |
| Seat facing `'front'` | yaw = 180° so local `+Y` maps to world `−Y` (toward the screen) | `cad_seating.plan_seating` |

| Boundary | What crosses | Check | Verdict |
|---|---|---|---|
| Dialog input → model | `display_to_si(value, unit)` divides by exact integer denominators — `120 mm == 0.12` bit-for-bit; inches via exact `0.0254`; `parse_length_input` handles m/cm/mm/in/ft/`'`/`"` forms; `LengthSpinBox._exact_m` caches SI so display quantization can't drift | Type `650 mm` ear height → `Position3`/`Offset3` holds `0.65` | Honest — metres by construction, no precision leak |
| Model → scene snapshot | `canonical_scene_json` serializes `model_dump(mode='json')`; `scene_content_hash` guards it | Round-trip awkward floats (`0.1+0.2`, `1/3`) | Honest — `model_validate_json` round-trip is **bit-exact**, `==` on entities, identical content hash |
| Snapshot → solver adapter | `AcousticReceiverBinding.world_position` / `source_reference_point` / `CompiledVertex` — all `x_m/y_m/z_m` metres, vertices copied verbatim | Assert binding's `world_position` is the same metres the model computed | Honest — `bind_prediction_request_to_solver_adapter` carries hashes, never rescales |
| Domain → render | `(x, −y, z)` on points and `C·R·C` on rotations | Asymmetric seat at yaw 30°: apply render matrix to render image of local `+Y` ≡ render image of domain-rotated `+Y` | Honest — conjugation identity holds; no mirror/permute/sign flip anywhere in the draw path |
| Render → domain (drag) | `render_delta_to_domain` re-negates Y | `(0.3, 0.4, 0.1)` drag on `(1, 2, .5)` → `(1.3, 1.6, .6)` | Honest |
| Rotated seat → ear/eye | `pose_*_position` rotate-then-translate, same convention as speakers | yaw-90 seat, eye offset `(0, 0.05, 1.1)` → world `(1.95, 3.0, 1.65)` | Honest — matches hand computation to the last ulp |
| Transform composition | previews are absolute from `_preview_before_entities`; commit pushes one `TransformEntitiesCommand`; apply fails closed on before-state mismatch | Two committed 30° group rotates of `(1,0)` about origin → exactly `(0.5, √3/2)`, yaw 60°; a repeated in-gesture preview re-bases rather than accumulating | Honest — composes, never squares, never drops |
| `aim_xyz` vs body edits | aim is a **world** direction by contract (`scene_speaker_aim_xyz_world_direction_required`); orientation edits deliberately never rotate it — cabinet realignment is the separate `align_selected_cabinet_to_aim` action | — | Honest — the decoupling is documented and intentional, not a bug |
| Mesh QA → solver | `diagnose_raw_visual_mesh` (non-manifold, duplicate, 2D-coplanar overlap, sliver `4√3A/Σe²`, inverted-normal via winding BFS + signed volume); `convert_raw_visual_mesh_to_semantic_geometry` re-diagnoses the *derived* mesh and sets `blocked_by_geometry` → compiler `input_policy` rejects; R120 drops sub-tolerance triangles only by explicit request policy with a recorded `DroppedFeature` | Inspect path + `require_contract_ready` | Honest — flagged geometry cannot silently reach the compiler |
| Sightline geometry | segment–sphere clearance clamps to `[0,1]` in t but reports true distance; blockers counted only for interior t | Trace `_sightline_results` | Honest — see deferred note on endpoint grazing |
| Degenerate input | `RoomPrism` `Field(gt=0)`; `polygon_from_vertices` min-3/finite/no-dup/no-closing-dup/`is_valid`/min-area; `unit_direction` rejects zero vectors; `SemanticCoordinateTransform` requires affine non-singular | Construct each bad input | Honest — all fail closed at validation, no NaN propagation |
| Operational zones | `door_swing`/`clearance`/`reach`/`rotate`/`reserved` zones are entity-local; footprints rotate with the cabinet | Point-in-sector tests at yaw 0/90/180 | **Two real bugs fixed here (F1, F2)** |

## What "wrong" looked like, concretely

- **Door-swing leaf direction**: `operational_zone_footprint` opened a `door_swing` sector
  along `atan2(hinge_y − y, hinge_x − x)` — the direction of the *hinge offset vector*, i.e.
  wherever the hinge happens to sit relative to the body center. A door hinged off local `+X`
  swept a quadrant around `+X`; the same door hinged off `+Y` swept a different world quadrant.
  The leaf's rest direction is the entity-local `+Y` axis (`_entity_local_forward_angle`), not
  the hinge placement. Footprints were only area-tested (`test_cad_operational_geometry.py`
  asserts area equality), so the directional lie survived.
- **Rotate zone anchored to world `+X`**: a partial `rotate` zone built its sector with
  `start_angle_rad = 0.0`, ignoring `entity.orientation` — the sector drew the same quadrant no
  matter which way the entity faced. Now anchored to entity-local `+Y` like the other zones
  (the `≥ 360°` buffer-disk path was already orientation-free and is unchanged).
- **`quaternion_from_matrix3` docstring said "rows"** where the convention is columns —
  exactly the kind of comment that misleads the next audit into a "bug" that isn't.

## Fixes (small diffs)

| # | File | Change |
|---|---|---|
| F1 | `cad_operational_geometry.py` | `door_swing` rest angle = `_entity_local_forward_angle(entity)` (was `atan2` of the hinge offset). Hinge still places the arc center via `_entity_local_to_world_xy`; only the leaf direction was wrong. |
| F2 | `cad_operational_geometry.py` | `rotate` partial sector starts at `_entity_local_forward_angle(entity)` instead of `0.0`; helper added next to `_entity_local_to_world_xy`; full-turn path untouched. |
| F3 | `cad_scene.py` | `quaternion_from_matrix3` docstring: rows → columns. |

All three were reproduced as failing tests first (F1/F2 fail on the stashed pre-fix file).

## Deferred / known gaps

- **Sightline endpoint grazing**: `_sightline_results` skips blocker spheres whose closest-point
  parameter lands outside the open segment (`t ≤ ε` or `t ≥ 1 − ε`). A head-sized sphere just
  past the eye or just beyond the screen plane is never counted — deliberate (endpoints aren't
  occluders), but a sphere *straddling* the eye point itself could silently pass. Marginal;
  noted for the render team.
- **Open-mesh inverted normals** use a majority-winding heuristic — reported as
  `state='unknown'` when a component isn't orientation-complete, which is honest, but means a
  lone flipped face in a consistent open shell is detected only relative to the majority.
- **Measurement anchors** are `Position3` world positions — correct, but they don't inherit
  entity frames; moving a `measurement_point`'s parent doesn't move the point. Accepted model
  (explicit world anchors), noted.
- **Negative-determinant import transforms** are allowed by design (explicit authority for
  opposite-handed assets) — a mirrored closed shell flips all winding, and the post-diagnostic
  honestly reports it as inverted normals (`blocked_by_geometry`). Verified honest.

## Test evidence

- New: `backend/tests/test_round14_geometry.py` — 8 tests: UI-unit→solver binding, asymmetric
  render conjugation, rotated seat eye/ear, transform composition, door-swing direction,
  rotate-zone orientation, bit-exact round-trip, degenerate rejection. F1/F2/F3 regressions
  fail pre-fix, all pass post.
- Regression: `test_cad_operational_geometry.py`, `test_cad_document.py`,
  `test_cad_video_geometry.py`, `test_cad_display_units.py`, `test_cad_units.py`,
  `test_room_geometry.py` — green under `-n 4`.
