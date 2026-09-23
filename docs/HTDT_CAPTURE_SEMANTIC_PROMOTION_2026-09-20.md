# Capture ingestion -> semantic SceneRevision promotion

Date: 2026-09-20

Related:
- HTDT-Capture Issue #7
- HTDT PR #251 capture ingestion transaction
- Issue #167 RawVisualMesh / SemanticAcousticGeometry
- Issue #101 arbitrary-room acoustics

## Purpose

This slice adds the explicit promotion boundary after production Capture
ingestion.

It does **not** automatically treat an ingested ARKit mesh as HTDT semantic room
geometry.

```text
committed capture ingestion
  -> select exact RawVisualMesh binding
  -> inspect raw triangle IDs + diagnostics
  -> provide explicit T_scene_from_capture_world authority
  -> compose with exact T_world_from_mesh_anchor
  -> existing SemanticGeometryConversionRequest
  -> existing SemanticAcousticGeometry
  -> exact parent SceneRevision
  -> atomic SceneRevision + promotion lineage commit
```

## Coordinate authority

Capture `T_world_from_mesh_anchor` maps one ARMeshAnchor into one exact Capture
`coordinate_space_id`. That does not by itself define HTDT scene axes or
origin.

Promotion therefore requires a versioned
`CaptureWorldToSceneAuthority` bound to that exact Capture coordinate-space
ID. HTDT never guesses an ARKit-world -> HTDT-scene transform.

The effective source transform is:

`T_scene_from_mesh_anchor = T_scene_from_capture_world * T_capture_world_from_mesh_anchor`

The resulting matrix is passed into the existing semantic geometry conversion
authority, preserving its explicit provenance and rationale.

## Semantic authoring

`inspect_capture_mesh()` exposes the exact raw triangle IDs and diagnostic
authority before promotion. Callers can therefore author explicit
`SurfaceSemanticAssignment` records against stable triangle IDs.

Unassigned triangles remain `unknown`. No wall/room/material class is inferred.

Promotion requests explicitly choose one readiness policy:

- `allow_blocked_semantic_authority`: persist the semantic authority even when
  geometry or surface semantics still block R120 compiler-contract readiness;
- `require_r120_compiler_contract_ready`: fail unless the resulting geometry is
  already ready for the R120 geometry compiler contract.

Neither policy makes the geometry solver-ready.

## SceneRevision binding

The request names an exact parent SceneRevision and document ID.

Promotion updates only the optional `r120_semantic_geometry` authority and
raises scene schema_version to at least 4. SceneRepository's existing parent
binding rules remain authoritative.

The new SceneRevision and the capture-promotion audit record commit inside the
same SQLite transaction. Failed promotion leaves no promotion record and no new
SceneRevision.

Re-executing the same deterministic promotion request is idempotent.

## Deferred

Still explicit follow-on work:

- semantic surface editor UX;
- automatic mapping from capture annotations/RoomPlan objects into surface
  semantics;
- bounded raw-mesh repair selection before semantic promotion;
- material/boundary/region/portal authoring;
- real captured-room promotion evidence.
