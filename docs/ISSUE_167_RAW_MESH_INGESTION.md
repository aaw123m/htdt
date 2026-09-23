# Issue #167 — raw mesh ingestion / provenance / diagnostics slice

This record covers the first implementation slice of Issue #167. It is intentionally limited to raw visual mesh ingestion and deterministic diagnostics. It does **not** promote imported geometry into Scene/R120 solver-ready acoustic geometry.

## Implemented scope

- Immutable `RawVisualMesh` snapshots for OBJ and binary GLB 2.0 assets.
- Exact preservation of the original asset bytes (base64 in the serializable snapshot), SHA-256, source format/name, byte size, importer id/version, and explicit `raw_visual_only` / `acoustic_semantics=unassigned` provenance.
- OBJ vertex/face ingestion with deterministic fan triangulation, positive/negative vertex indices, and fail-closed malformed-index handling.
- GLB 2.0 ingestion for the active scene with embedded buffer 0, FLOAT/VEC3 POSITION accessors, unsigned scalar indices, TRIANGLES primitives, node hierarchy/TRS/matrix transforms, and deterministic flattening. Unsupported external buffers, sparse accessors, non-triangle primitives, malformed references, and invalid scene graphs fail closed.
- Versioned deterministic diagnostic profile and identity bound to the exact raw-mesh semantic hash and profile semantic hash. No timestamps or generated metadata participate in identity.
- Diagnostics for open boundaries, non-manifold edges, exact duplicate faces, positive-area coplanar overlapping faces, inconsistent/inverted winding, sliver faces, tiny features, and topological watertightness.
- Diagnostic-only vertex canonicalization uses versioned tolerances; it never rewrites the raw vertex/face data or original asset.
- `acoustic_volume_readiness` is deliberately restricted to `not_ready` or `geometry_checks_pass_but_semantic_conversion_required`. `solver_ready` is always `false` in this slice.
- Canonical JSON serialization/reopen for both the raw snapshot and diagnostic result, with validation of original bytes/hash and deterministic identity on reopen.
- Representative imperfect OBJ fixture exercising the required defect classes, plus a generated GLB fixture for ingestion coverage.

## Authority boundary

The imported object is visual/raw evidence only. A clean/watertight diagnostic result is **not** an acoustic-region authority, material/surface authority, or solver input. This slice creates no implicit Scene geometry, R120 compiled representation, material assignment, portal/boundary semantics, or R100B adapter input.

No automatic repair is performed. Canonicalization and orientation analysis are read-only diagnostic operations over the immutable raw snapshot.

## Deferred to later Issue #167 slices

- Raw visual mesh -> semantic acoustic geometry conversion and exact SceneRevision lineage/binding.
- Guided repair actions and explicit repaired-geometry lineage; no source asset will be overwritten.
- Semantic surface/material IDs and material assignment.
- AcousticRegion/Portal/BoundaryTermination construction and readiness qualification beyond raw topology diagnostics.
- GA ray-leak / portal diagnostics.
- R120 wave/ray compiler integration, approximation/error metadata, and solver-ready compiled representations.
- Native database/project persistence once the semantic ownership/binding is defined; this first slice provides deterministic canonical serialization without changing the shared repository schema.
- Broader GLB features such as external/data-URI buffers, sparse accessors, skinning/morph targets, and non-TRIANGLES primitive modes. These remain explicit import errors rather than silent approximations.

## Verification

Focused implementation checks before PR passed:

```text
python -m py_compile raw_mesh.py test_raw_mesh.py
pytest -q test_raw_mesh.py
6 passed
```

The repository PR CI is the authoritative integration check. No RDC/Windows GUI access was used for this slice.
