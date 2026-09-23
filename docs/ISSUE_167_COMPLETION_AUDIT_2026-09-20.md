# Issue #167 completion audit — raw import to R120 compiler

Date: 2026-09-20

## Scope

This audit closes the acceptance chain for Issue #167 without adding another geometry truth or a new repair engine.

The merged implementation already provides:

- PR #195: immutable OBJ/GLB `RawVisualMesh`, exact original asset bytes/SHA-256, deterministic diagnostics.
- PR #196: explicit RawVisualMesh → `SemanticAcousticGeometry` conversion, stable semantic surface identity and SceneRevision lineage.
- PR #207: solver-neutral `R120CompiledGeometry`, exact SceneRevision/SemanticAcousticGeometry binding, topology/leak/portal diagnostics and fail-closed authority readiness.
- PR #220: explicit/versioned bounded `RawMeshRepairPlan`, independent `RepairedRawMesh`, post-repair diagnostics, append-only persistence/reopen, and exact repair lineage into semantic conversion.

## Final acceptance fixture

`backend/tests/test_raw_mesh_repair_r120_acceptance.py` exercises one representative imperfect imported mesh end-to-end.

The source OBJ contains:

- one duplicate vertex used by a face, which initially splits exact topology;
- one unreferenced vertex;
- otherwise the four faces of a closed tetrahedron.

The fixture then explicitly requests only:

1. exact duplicate vertex consolidation;
2. unreferenced vertex removal.

No automatic hole filling, non-manifold surgery, reconstruction, remeshing or room inference is used.

The acceptance chain is:

```text
exact source bytes/SHA-256
→ RawVisualMesh
→ source diagnostics
→ explicit RawMeshRepairPlan
→ RepairedRawMesh
→ post-repair diagnostics
→ explicit room-boundary semantic assignment
→ SemanticAcousticGeometry
→ SceneRevision
→ R120GeometryCompilationRequest
→ R120CompiledGeometry
```

The fixture verifies:

- original source bytes and original asset SHA-256 are unchanged;
- repaired geometry has an independent deterministic identity;
- post-repair diagnostics pass while still requiring semantic conversion;
- semantic conversion retains exact raw/plan/repaired/post-diagnostic lineage;
- semantic geometry reaches `ready_for_r120_geometry_compiler_contract` but is not itself promoted to solver-ready;
- R120 compilation binds exact SceneRevision and SemanticAcousticGeometry identities;
- explicit material, boundary-physics, AcousticRegion and `explicit_none` Portal authorities are required for complete wave/GA geometry readiness;
- no unresolved condition remains for this exact closed-shell fixture.

## Persistence

Persistence is already covered by focused merged tests:

- PR #220 verifies append-only raw repair bundle save/reopen and exact recomputation/hash validation.
- PR #207 verifies append-only R120 compiled-geometry persistence/reopen against exact SceneRevision and SemanticAcousticGeometry identity.

No sidecar geometry authority is introduced.

## Remaining non-goals

Issue #167 does not make arbitrary scan healing automatic. The following remain explicit unsupported/future work where applicable:

- arbitrary hole filling;
- non-manifold surgery;
- self-intersection remeshing;
- point-cloud surface reconstruction;
- large-gap closure;
- semantic room inference.

These limitations do not block Issue #167 acceptance because imported meshes are required to remain fail-closed unless an explicit bounded repair and semantic conversion establish the R120 input contract.

RDC usage: 0.
