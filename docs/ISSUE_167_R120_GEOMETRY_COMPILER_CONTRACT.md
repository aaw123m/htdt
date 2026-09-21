# Issue #167 / #101 — R120 geometry compiler contract and GA leak/portal diagnostics

## Scope

This slice adds a solver-neutral authority between `SemanticAcousticGeometry` and any future wave or geometrical-acoustics adapter.

It deliberately does **not**:

- mutate `RawVisualMesh`
- mutate `SemanticAcousticGeometry`
- auto-repair holes or topology
- infer regions, portals, or boundary terminations from room shape
- assign default materials
- modify PFFDTD/MFEM adapters
- select a production solver
- change R100B authority
- touch R110 source-model authority
- touch report generation or common roadmap documents

## Authority boundary

The upstream value `ready_for_r120_geometry_compiler_contract` remains only an input-contract readiness statement. It is not a solver-ready claim.

Normal compilation requires that upstream state. A blocked semantic geometry can only be represented with the explicit request policy `diagnostic_compile_unresolved`. Such output carries `input_semantic_geometry_not_compiler_contract_ready` and cannot become wave/GA ready.

The compiler authority records:

- exact SceneRevision id and content hash
- exact SemanticAcousticGeometry id and semantic hash
- compiler id/version
- target representation
- geometric tolerance
- explicit approximation and tiny-feature policy
- coplanar and triangulation policy
- coordinate/unit convention
- stable source SemanticSurface identity
- compiled triangle mapping
- topology identity
- bounding volume
- closed-shell / open-edge / non-manifold diagnostics
- approximation operations and dropped-feature evidence
- explicit approximation error status: dropped-feature extent is recorded, but no geometric error bound is fabricated when it has not been computed
- topology identity independent from external material/boundary-physics references
- compiler warnings and unresolved conditions
- deterministic compiled hash

## Region / portal / termination policy

`AcousticRegionAuthority`, `PortalAuthority`, and `BoundaryTerminationAuthority` are immutable/versioned explicit declarations.

Portal and termination authorities distinguish:

- `unknown`
- `explicit_none`
- `explicit_list`

No geometry-derived portal or absorbing termination is created. Unknown declarations remain blocked.

Material and boundary physics are represented only through `ExactExternalAuthorityRef`. Missing references remain `material_assignment_missing` / `boundary_physics_missing`; no default wall/material is introduced.

## Readiness

`R120GeometryReadiness` keeps independent fields:

- `geometry_compiled`
- `wave_geometry_ready`
- `geometric_acoustics_geometry_ready`
- `material_assignment_missing`
- `region_definition_missing`
- `portal_definition_missing`
- `boundary_physics_missing`
- exact unresolved conditions

This avoids a single `solver_ready=true` shortcut.

## GA leak / portal diagnostic

The diagnostic uses deterministic geometry evidence:

1. boundary-edge topology from the exact compiled triangle mesh;
2. explicit portal-edge declarations;
3. explicitly versioned ray samples from `LeakSamplingAuthority`.

It reports explicit portal edges separately from unintended openings, validates portal declarations against actual open edges, and records sampled ray escape evidence. A hole is evidence only; it is never repaired automatically.

The diagnostic result binds the exact compiled geometry id/hash, exact sampling authority, portal authority ref, findings, severities, unresolved conditions, and a deterministic diagnostic hash.

## Persistence

Canonical JSON serializers/deserializers are provided for:

- `R120CompiledGeometry`
- `R120LeakPortalDiagnostic`

`R120GeometryCompilerRepository` also provides append-only SQLite persistence in the native CAD database. Reopen validates the persisted compiled authority against the exact immutable SceneRevision id/content hash and the exact embedded SemanticAcousticGeometry id/hash. Diagnostic reopen validates the exact persisted compiled geometry id/hash before returning evidence.

Persistence additionally retains the exact non-scene compile inputs the compiled payload does not itself embed — `surface_boundary_bindings` plus any `AcousticRegionAuthority`/`PortalAuthority`/`BoundaryTerminationAuthority` — in `cad_r120_compile_inputs`, and the exact `PortalAuthority` each diagnostic consumed in `cad_r120_leak_diagnostic_inputs`. On every save and every authoritative read the repository resolves those retained authorities (a missing or mismatched ref fails closed), re-runs `compile_r120_geometry(...)`/`diagnose_r120_leak_and_portals(...)`, and requires exact equality with the stored payload, so a self-hash-valid but non-canonical derived result cannot persist or reload.

Model validators recompute identities on reopen, so stale/tampered hashes fail closed.

## Focused fixtures

`backend/tests/test_r120_geometry_compiler.py` covers:

1. valid closed semantic room;
2. unintended opening;
3. explicit portal;
4. portal declaration mismatch;
5. unknown semantic surface;
6. compiler-contract-ready geometry;
7. unresolved geometry and explicit diagnostic-only compilation;
8. stable SemanticSurface mapping;
9. approximation/drop metadata;
10. same input/settings -> same request/compiled hash;
11. compiled and diagnostic save/reopen;
12. original semantic geometry remains immutable.

## Validation

Validation target for this slice is GitHub Actions only. No RDC/Windows machine access is required.
