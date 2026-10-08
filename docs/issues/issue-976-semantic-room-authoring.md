# Issue #976 — Room UIからのセマンティック形状編集（傾斜天井・下がり天井・段床・腰壁・隣接室）

## Scope

The 「部屋形状」 panel gains a progressive-disclosure **「高度な形状」**
section that authors `RoomAuthoringModel` — the single semantic authority —
directly on `SceneDocument`. No UI-specific geometry format exists: the panel
writes spec objects, the R120 compiler turns them into acoustic geometry, and
the 3D viewport draws the compiled surfaces.

- `SceneDocument.room_authoring` (optional, `schema_version >= 6`):
  `RoomAuthoringModel` re-embeds the document `room` and must equal it —
  enforced on every snapshot validation. Canonical serialization omits the
  field when absent, so every pre-existing document hash is unchanged.
- All five primitive kinds are editable: 傾斜天井 (`SlopedCeilingSpec`),
  下がり天井 (`SoffitSpec`), 段床 (`RiserSpec`), 腰壁
  (`PartialHeightWallSpec`), 隣接領域 (`AdjacentRegionSpec` + shared-edge
  portal).
- Every commit goes through
  `RoomWorkspaceController.replace_room_authoring` →
  `RoomWorkingDocument.replace_room_authoring` → the same
  `_commit_room_snapshot` path as `replace_room`/`replace_room_topology`:
  history entries (「高度な形状を編集」), Undo/Redo, recovery sync, and the
  SceneRevision record are free.
- `replace_room` / `replace_room_topology` **rebind** the model to the new
  room (`rebind_room_authoring`); edits that break a primitive's references
  (e.g. a shared-edge index that no longer exists) raise `RoomAuthoringError`
  before any commit — the stored document is untouched.

## Vocabulary

| Term | Meaning |
|---|---|
| `RoomAuthoringModel` | Semantic model over the base `RoomPrism`: optional sloped ceiling + tuples of soffits, risers, partial-height walls, adjacent regions. |
| `(kind, primitive_id)` | Shared selection identity across panel selector, 3D controller (`selected_authoring`), overlay actors and the saved revision. `('ceiling','ceiling')` for the single sloped ceiling. |
| `RoomAuthoringIssue` | Typed validation finding (`category`, `severity`, `code`, `target`) — `geometry_invalid`/`unsupported` fail the commit; warnings (e.g. `material_binding_unsupported`) surface in the panel notice without blocking. |
| `rebind_room_authoring` | Re-validates a stored model against a replaced room/topology; errors → `RoomAuthoringError` naming each broken primitive (fail-closed). |

## Validation & fail-closed edges

`validate_room_authoring_model(model, wall_topology=…)` runs inside the
controller before every commit and reports:

- footprint-outside-room, primitive overlap (riser/soffit/wall plan
  interference), riser/soffit vs local sloped-ceiling clearance, partial-wall
  top vs ceiling, duplicate shared edges, region-vs-region overlap,
  region opening vs local ceiling height, opening conflicts with
  `WallTopology` openings — all `geometry_invalid` errors naming the target;
- `unsupported` warnings where authority coverage ends: authoring surfaces
  are not bound to the `semantic-surface:` material store
  (`material_binding_unsupported`), which is stated honestly rather than
  faked.

Model-level invariants (unique ids, `main` reserved, shared-edge bounds,
riser/soffit/wall height limits, non-degenerate specs) are pydantic
validators on `RoomAuthoringModel` itself — an invalid model cannot be
constructed, and an invalid commit leaves the document byte-identical.

## Rendering & selection

- `render_document` draws compiled surfaces as translucent named actors
  (`authoring-surface-{surface_key}`) after the room shell — risers,
  soffits, knee walls, split wall flanks/lintels, portal openings, region
  volumes, and the tilted ceiling plane. The shell wireframe follows the
  slope (`ceiling_height_at`) instead of drawing a flat rectangle that does
  not exist.
- Edit mode draws per-primitive outline loops (same shapes the compiler
  emits); clicking any segment selects the primitive, the panel selector
  follows, and the field inspector shows a **ghost preview** (amber) of the
  edited-but-uncommitted candidate — preview→commit parity, no second
  format.

## Staleness hookup

`diff_scene_documents` treats an authoring change as the existing
`room_geometry` `SceneChange` (axes `geometry` + `material_boundary`), so
REV72's `resolve_change_events`/`build_revalidation_queue` (#964) marks
dependent evidence stale without any new authority.

## Compatibility & explicit limits

- Flat-ceiling/single-room documents load unchanged (field optional,
  canonical-omitted, hash-stable) — verified by the round-trip test.
- Material assignment to authoring surfaces is declared unsupported
  (warning) until the material authority learns `semantic-surface:` ids for
  compiled primitives.
- Region footprints are edge-extruded rectangles on one shared edge —
  arbitrary polygon regions are out of scope and cannot be expressed by the
  model itself.
