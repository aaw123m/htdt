# Issue #464 — Rich SceneEntity body geometry

Physical `SceneEntity` objects were historically rotated cuboids: `size_m` was
the only shape authority for rendering, collision/clearance, placement
constraints, and snapping. This change keeps `size_m` as the persisted
bounding envelope and adds an optional `body_geometry` refinement.

## Model (`htdt/cad_scene.py`)

`SceneEntity.body_geometry: EntityBodyGeometry | None` with kinds:

- `box` — explicit opt-in to the legacy rectangular envelope (no extra params).
- `cylinder` — vertical circular prism of `radius_m` spanning `size_m.z_m`
  (round tables, cylindrical cabinets).
- `extruded_polygon` — entity-local XY `footprint_vertices` extruded over
  `size_m.z_m` (L-shaped sofas, irregular furniture, risers).
- `mesh_asset` — `BodyMeshAsset`: parsed entity-local vertices/triangles plus
  immutable provenance (`asset_sha256`, `source_name`, `asset_format`,
  `original_size_bytes`) and a local transform (`uniform_scale`,
  `local_offset_m`). Original bytes are stored in the project
  content-addressed blob store via `SceneRepository.store_blob`/`read_blob`.

Validation enforces envelope fit: cylinder radius and polygon vertices must
stay inside the `size_m` XY envelope, so `size_m` remains a sound broad-phase
bound for every body.

`canonical_scene_json` omits `body_geometry` when absent, so pre-#464 scene
documents keep byte-identical canonical payloads and content hashes.

## Consumers

- `entity_horizontal_footprint` / `entity_exact_body_footprint` /
  `entity_collision_geometry_authority` (`cad_orientation_constraints.py`):
  upright cylinder/polygon bodies contribute their exact XY footprint;
  tilted bodies (pitch/roll ≠ 0), `box`, `mesh_asset`, and legacy entities use
  the oriented bounding-envelope hull. Placement constraints and
  `cad_constraints._entity_profile` inherit this automatically.
- `cad_video_geometry._collision_results`: pairs where at least one side has
  exact body geometry evaluate extruded-footprint intersection with per-side
  clearance inflation; `CollisionResult.geometry_authority` reports
  `exact_body_geometry` / `bounding_envelope` / `mixed_body_geometry`.
- `room_viewport._entity_mesh` (canonical) renders cylinders, extruded
  footprints, and mesh assets; a wireframe `size_m` envelope overlay is drawn
  whenever an entity opts into non-box geometry. `native_editor.py` and
  `theater_editor.py` reuse the same builders.
- `cad_snap`: vertex/edge/midpoint snap candidates follow the authored body
  (cylinder rim compass points, prism rings + verticals, mesh vertices).
- `report.py` (installation output v4 / `installation-output-4`): entities
  report `body_geometry_kind` and `collision_geometry_authority`; collision
  summaries report `geometry_authority`; CSV and HTML expose both columns.
- `room_workspace.SelectionInspector`: shape picker, cylinder radius,
  polygon footprint text editor, mesh import (OBJ/GLB/HTDTMSH1) with blob
  persistence, and a live "衝突・クリアランス" basis readout
  (実形状（厳密）/包絡近似).

## Boundary with issue #470

This change adds no orientation/rotation fields and no acoustic aiming
fields. Existing `orientation` is only *consumed* (extruded footprints are
exact only while the body is upright; tilted bodies fall back to the
envelope). Numeric orientation/aim editing is owned by #470.

Body geometry is display/collision authority only. It is **not** acoustic
solver geometry: acoustic geometry enters a scene exclusively through
`SceneDocument.r120_semantic_geometry`, and `mesh_asset` bodies keep their
`bounding_envelope` collision basis.
