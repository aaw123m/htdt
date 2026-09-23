# R120B explicit polyhedral acoustic geometry implementation record

Date: 2026-09-20
Base authority: `main@a8491db973c6fcefc66e259903ca668936f63a2e`
Branch: `agent/r120b-polyhedral-geometry-20260920-1740`
Draft PR: #272

## Bounded scope

This slice establishes solver-neutral R120B authority for explicit general-3D polyhedral acoustic geometry beyond a simple RoomPrism. It deliberately does not introduce an arbitrary CAD kernel or an analytic curved-surface solver.

The authority path remains explicit:

`raw/imported source identity -> semantic polyhedral acoustic geometry -> topology validation -> compiled representation`

Visual mesh data is never promoted directly to solver-ready geometry.

## Authority model

`backend/src/htdt/r120_polyhedral_geometry.py` defines an immutable, hash-bound authority for:

- canonical 3D vertices;
- planar polygon surfaces with arbitrary orientation;
- explicit per-surface material authority references;
- closed polyhedral air-volume declarations;
- explicit Portal-to-region/surface relationships;
- bounded planar-tessellation approximation authority;
- deterministic indexed-triangle compilation;
- topology reports bound to exact semantic geometry and validation tolerance;
- independent wave and geometric-acoustics representation readiness.

The established `r120_geometry_compiler.py` v1 SceneRevision/SemanticAcousticGeometry compiler remains backward-compatible. R120B is exported from that module without changing existing persisted v1 model fields or hashes.

## Canonicalization and determinism

Semantic construction canonicalizes vertex order lexicographically, remaps polygon indices, canonically rotates directed loops without reversing semantic orientation, sorts surfaces by stable surface key, and canonicalizes region, Portal, and approximation declarations.

Surface identity is bound to the canonical surface key, directed polygon topology, and referenced canonical vertex coordinates. Reordering equivalent input must not change semantic or compiled identity, while geometry with different coordinates must not collapse to the same surface identity.

Deterministic ear-clipping triangulation is used for hole-free planar polygons. Valid polygon-hole semantics are topology-authorized, but the current indexed-triangle backend representation reports them unsupported rather than silently filling or deleting holes.

## Topology gate

Validation is fail-closed for:

- open volume;
- inconsistent surface orientation;
- duplicate face;
- zero-area/degenerate face;
- non-planar face beyond topology tolerance;
- polygon self-intersection;
- detectable closed-volume self-intersection;
- non-manifold edge;
- invalid polygon hole;
- missing material reference;
- detectable region overlap;
- invalid Portal/surface/region relationship.

No shape-changing auto-repair is performed. The topology report stores the exact semantic geometry id/hash and the exact validation tolerance so the gate itself is identity-bearing evidence.

## Approximation authority

Curved-source handling is intentionally bounded to explicit planar tessellation. Every approximation record preserves:

- source geometry identity;
- tolerance;
- measured maximum deviation;
- generated surface keys and count;
- algorithm id and version;
- approximation status.

A bounded tessellation whose measured maximum deviation exceeds its declared tolerance is rejected. Approximation is never silent.

## Wave / GA readiness

Compiled readiness exposes `wave_representation` and `ga_representation` independently.

The bounded polyhedral indexed-triangle representation can be READY for both when topology and representation constraints are satisfied. Current wave representation is explicitly UNSUPPORTED for multi-region or Portal-bearing geometry unless separately authorized. Polygon holes are independently UNSUPPORTED for both current indexed-triangle targets.

`wave_numerical_validation_status` remains `NOT_VALIDATED`. This slice does not claim production wave numerical validation.

## Focused invariants

`backend/tests/test_r120_polyhedral_geometry.py` covers:

- valid sloped-ceiling volume and enclosed-volume evidence;
- valid stepped geometry with concave polygon triangulation;
- deterministic semantic and compiled identity under input reordering;
- distinct-coordinate identity separation;
- topology-report identity binding to validation tolerance;
- explicit per-surface material identity preservation;
- missing material reference diagnostic/fail-closed behavior;
- non-manifold failure;
- open-volume failure;
- bounded tessellation/error authority and tolerance rejection;
- semantic/compiled serialize-reopen identity;
- rectangular RoomPrism regression;
- independent Portal GA/wave readiness;
- invalid Portal/surface relationship;
- overlapping-region diagnostics.

## Validation authority

The final PR head is validated through repository-native GitHub Actions. The relevant workflows are the main `CI` backend test/preflight workflow plus the automatically triggered R130A candidate-wave and R150 deterministic-GA compatibility workflows. Final run ids and PASS conclusions are summarized in the Draft PR metadata after the final head is stable; they are not pinned in this file because updating that evidence document itself creates a new PR head and new workflow runs.

## Explicit exclusions

This task does not modify:

- HTDT-Capture;
- the R130 numerical executor;
- `cad_geometric_acoustics_portal.py`;
- `cad_geometric_acoustics_adapter.py`;
- `docs/IMPLEMENTATION_STATUS.md`;
- `docs/IMPLEMENTATION_ROADMAP.md`.

It does not perform production wave numerical validation, R150 propagation expansion, or owned-room validation.

RDC usage: **0 calls**.
