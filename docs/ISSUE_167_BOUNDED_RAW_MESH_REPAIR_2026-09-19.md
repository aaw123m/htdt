# Issue #167 — bounded raw-mesh repair authority

Date: 2026-09-19

## Scope

This slice adds a bounded, deterministic repair authority between `RawVisualMesh`
diagnostics and semantic acoustic geometry conversion:

```
original asset
  -> RawVisualMesh
  -> RawMeshDiagnosticResult
  -> explicit RawMeshRepairPlan
  -> RepairedRawMesh
  -> RepairedRawMeshDiagnosticResult
  -> SemanticAcousticGeometry
```

No repair operation is selected automatically. A plan must be supplied by an
explicit user selection or an explicitly identified upper-level workflow.

## Authority design

`RawMeshRepairPlan` is immutable and versioned. Its semantic identity binds:

- exact source `RawVisualMesh` id and semantic hash;
- exact source diagnostic id and semantic hash;
- repair algorithm id/version;
- requested-by semantics and request reason;
- ordered, versioned repair operations and their explicit parameters;
- deliberately unsupported operations.

`RepairedRawMesh` is a separate immutable derived authority. It does not reuse
the original raw asset identity. It binds the exact source mesh, source
diagnostics, repair plan, original asset SHA-256, repaired vertices/triangles,
per-operation results, before/after counts, displacement/error metadata,
unresolved conditions, and ordered repair lineage.

The original `RawVisualMesh` remains frozen and its original asset bytes and
SHA-256 remain unchanged.

## Supported bounded operations

Version 1 supports only operations with bounded semantics:

- exact duplicate vertex consolidation, retaining the first exact coordinate as
  the canonical vertex;
- vertex weld with an explicitly supplied source-unit tolerance;
- unreferenced vertex removal;
- exact duplicate face removal, retaining the first exact geometric face;
- relative winding consistency correction on connected orientable manifold
  components, without inferring a global outward room orientation;
- degenerate-face removal only when explicitly requested with an area tolerance.

Tolerance welding records the explicit tolerance in the operation authority and
records moved-vertex count, maximum displacement, and topology-change count in
the operation result. A weld/consolidation that creates collapsed faces is not
silently healed: an explicit degenerate-face removal operation is required.

## Deliberately unsupported repairs

The first slice does not execute:

- arbitrary hole filling;
- non-manifold surgery;
- self-intersection remeshing;
- Boolean reconstruction;
- point-cloud surface reconstruction;
- large-gap closure;
- unknown overlapping-surface resolution;
- acoustic room inference.

Such operations are represented as versioned `unsupported` operations and are
reported in unresolved findings. They do not modify geometry.

## Deterministic identity

Plan, operation-result, repaired-mesh, and post-repair-diagnostic identities are
SHA-256 hashes over canonical JSON authority payloads. Repeating the same source
mesh, exact diagnostic authority, and exact plan produces the same repaired
mesh identity. Changing a repair parameter (including weld tolerance) changes
the plan identity and therefore the derived identity.

## Diagnostics integration

`diagnose_repaired_raw_mesh` reuses the existing PR #195
`diagnose_raw_visual_mesh` semantics over the derived geometry. It produces a
separate immutable post-repair diagnostic identity bound to the repaired mesh.

Repair success is not solver readiness. Post-repair diagnostic results still
carry `solver_ready=False` and `semantic_conversion_required=True`.
Open boundaries, non-manifold findings, slivers, overlap, winding, and other
unresolved findings remain visible when they remain present.

## SemanticAcousticGeometry integration

Semantic conversion can optionally consume an exact `RepairedRawMesh` plus its
exact post-repair diagnostic. In that case the embedded conversion request
contains a `RawMeshRepairLineageRef` binding:

- source raw mesh id/hash;
- source diagnostic id/hash;
- repair plan id/hash;
- repaired mesh id/hash;
- post-repair diagnostic id/hash.

The converter rejects mismatched/stale repaired inputs and re-runs the exact
post-repair diagnostic before accepting the lineage. Existing semantic-stage
guided repairs remain a separate downstream mechanism.

No second solver-geometry truth is introduced. `SemanticAcousticGeometry`
continues to be the SceneRevision R120 semantic authority, and its existing
compiler-readiness and `solver_ready=False` semantics are preserved.

For requests with no raw-mesh repair lineage, the new optional field is omitted
from canonical semantic/Scene JSON so existing pre-slice identities remain
canonical.

## Native SQLite persistence / reopen

Native schema version 3 adds `cad_raw_mesh_repair_bundles`. It is append-only:
there is no update path. The persisted bundle contains the exact source raw
mesh, source diagnostics, repair plan, repaired mesh, and post-repair
diagnostics.

On save and reopen the implementation revalidates stored ids/hashes and
deterministically recomputes:

- source diagnostics;
- repaired mesh;
- post-repair diagnostics.

A stale or mismatched raw-mesh identity/hash is rejected. No sidecar database is
introduced.

## Focused tests

`backend/tests/test_raw_mesh_repair.py` covers the requested authority
behaviour:

1. same raw mesh + same plan -> same repaired identity;
2. original `RawVisualMesh` bytes/hash remain unchanged;
3. exact duplicate vertex/face cleanup;
4. explicit tolerance weld with displacement/topology accounting;
5. explicit degenerate removal after weld-created collapse;
6. bounded consistent-winding correction;
7. unsupported hole/non-manifold repair remains unexecuted;
8. post-repair diagnostics reuse existing semantics and unresolved findings
   remain;
9. repaired mesh -> `SemanticAcousticGeometry` exact lineage;
10. native SQLite save/reopen and exact identity revalidation;
11. stale raw source mismatch rejection;
12. repair parameter change -> different plan/repaired identity.

`backend/tests/test_cad_schema.py` is updated for the native schema v3
migration.

Repository CI runs the backend test suite before the existing repository
preflight checks, so failures in these focused fixtures stop later preflight
steps.

## Deferred scope

This slice deliberately defers production mesh healing, photogrammetry or
point-cloud reconstruction, arbitrary hole closure, non-manifold surgery,
self-intersection repair, material inference, acoustic-room inference, GUI work,
production solver work, and R100B/R110/O-series changes.

RDC usage count: **0**.
