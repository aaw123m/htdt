# R150 explicit multi-region / Portal propagation

Status: **implemented as a bounded deterministic two-region / one-Portal direct-propagation slice. R150 remains incomplete and no production GA solver is selected.**

> Snapshot record as of 2026-09-20 (PR #265). PR #432 (2026-09-30) later added multi-Portal cross-region reflected paths, so the "maximum Portal crossings: 1" and "reflection-before/after-Portal: not implemented" bounds below no longer describe current main. [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) is the canonical current state.

Issue: #101  
PR: #265  
Adapter authority: `htdt.r150.deterministic-path@1`  
Portal execution authority: `adapter-kernel:htdt-r150-explicit-portal-direct@1`

## Supported topology

This slice supports exactly:

```text
explicit AcousticRegion A
  -> one exact open Portal aperture
  -> explicit adjacent AcousticRegion B
```

The Portal lane is selected by
`general_planar_multi_region_portal_v1`.

The bounded execution policy is:

- exactly **2** explicit `AcousticRegionDeclaration` entries;
- exactly **1** explicit `PortalDeclaration`;
- exactly **1** maximum Portal crossing;
- `maximum_reflection_order=0` explicitly in the Portal configuration authority;
- direct propagation only in this multi-region lane;
- explicit source-region and receiver-region bindings are mandatory;
- the Portal must bind exactly those two regions;
- no implicit exterior or inferred AcousticRegion is created.

A source or receiver region is never inferred from position. The caller must provide the exact region identity, and the point must independently resolve as strictly inside that region and outside the other region.

## Exact Portal authority

R120 `PortalDeclaration` has additive optional authority for this slice:

- `state = open | closed`;
- `region_side_semantics = directed_boundary_edge_loop_normal_from_first_to_second`.

Legacy Portal declarations omit these fields and retain their previous semantic hashes. The R150 multi-region lane does **not** upgrade legacy declarations implicitly; it requires both fields explicitly.

For the supported Portal:

- `region_ids[0]` is the source side;
- `region_ids[1]` is the destination side;
- the ordered boundary-edge loop determines the aperture polygon and its normal;
- the directed loop normal must point from the first region to the second;
- the boundary edges must be exact single-incidence R120 geometric opening edges;
- all aperture vertices must be coplanar within the existing R120/R150 `geometric_tolerance_m`;
- the temporary exact region-cap lane accepts a simple strictly-convex aperture polygon only; concave, self-intersecting, self-touching, or tolerance-degenerate polygons fail closed rather than being silently fan-triangulated;
- the two regions' shared semantic boundary surfaces must equal the Portal surface set exactly.

A closed Portal, reversed orientation, wrong adjacency, unknown region, malformed/open loop, stale authority, or aperture mismatch fails closed.

## Region topology

The combined semantic surface representation for adjacent regions can contain interface seams that appear non-manifold if treated as one global shell. R150 therefore does not flatten both rooms into one global volume.

Instead, each explicit AcousticRegion is validated independently:

1. select only that region's declared R120 boundary surfaces;
2. close its declared Portal opening with the **exact Portal polygon** as a temporary membership/topology cap;
3. require every edge in that region-specific shell to have exactly two incidences;
4. use that same capped shell for deterministic point-in-region membership.

This cap is a validation construction only. It is not added to the propagation geometry and is not treated as an acoustic wall.

Any region-specific open/non-manifold/ambiguous topology is rejected. For this bounded topology only, R120 may retain the global diagnostic markers `input_semantic_geometry_not_compiler_contract_ready` and `compiled_non_manifold_edges` while independently marking `geometric_acoustics_geometry_ready=true` after the exact per-region manifold proof succeeds. Those markers are **not** cleared or reinterpreted as wave readiness: `wave_geometry_ready` remains false. All other unresolved R120 conditions remain fail-closed. Because snapshot schema v1 cannot represent distinct wave/GA boundary readiness, snapshot construction selects v2 whenever those readiness values differ; existing single-region snapshots remain on their previous schema.

## Direct Portal path

For an explicitly bound source in region A and receiver in region B:

```text
source
  -> straight free-space segment
  -> exact Portal plane intersection
  -> exact aperture polygon membership
  -> straight free-space segment
  -> receiver
```

The crossing must be strictly internal to the source/receiver segment, use the declared A -> B orientation, and lie in the exact Portal polygon. Intersecting the Portal plane outside the polygon is rejected.

The Portal and the two segments are checked separately. Each propagation segment is tested against exact R120 triangles, so an opaque object/surface on either side blocks the candidate. A solid part of the interface outside the aperture is not treated as a hole.

This slice does not enumerate arbitrary region graphs or arbitrary Portal sequences.

## Typed path artifact

No parallel GA result authority was introduced.

`DeterministicPathArtifact` and `DeterministicAcousticPath` remain the persisted authorities. The path model has additive optional fields:

- `ordered_interactions`;
- `ordered_region_ids`.

A typed interaction is either:

- `reflection(surface_id, point)`; or
- `portal_crossing(portal_id, point, from_region_id, to_region_id)`.

For the current Portal lane a valid path has exactly one `portal_crossing`, zero reflection interactions, and an ordered two-region sequence.

Legacy direct / first-order / second-order paths omit the additive fields from their canonical semantic payload. Their semantic hashes and persisted reader behavior therefore remain compatible.

On save/reopen, the repository re-resolves the exact SceneRevision/R120/region/Portal/configuration authorities, recompiles the execution input, recomputes the Portal crossing from exact endpoints and aperture, and requires the persisted crossing point and ordered region sequence to reproduce exactly.

## BoundaryTermination

Nontrivial `BoundaryTerminationAuthority` propagation is **unsupported** in this slice.

It is never:

- reclassified as a Portal;
- transmitted through;
- converted into an implicit exterior;
- assigned an assumed absorption/transmission law.

Any non-`explicit_none` BoundaryTermination is a typed fail-closed capability error.

## Material / energy semantics

The Portal crossing itself has no invented transmission loss.

For the supported direct Portal path:

```text
relative_energy_transport_per_m2
  = (1 / path_length^2)
    * source_directivity_magnitude_linear^2
```

No Portal transmission coefficient is inserted because no such authority exists yet.

Existing surface-reflection material semantics are unchanged for the existing single-region lanes. This PR does not implement reflection-before-Portal or reflection-after-Portal paths.

## Identity and staleness

The execution/path identity is bound to the existing exact authority chain, including:

- SceneRevision / scene content hash;
- semantic acoustic geometry and R120 compiled geometry hashes;
- AcousticRegion authority;
- Portal authority, including state, adjacency, edge loop, and side semantics;
- ordered region sequence and typed Portal interaction;
- source / receiver identities and exact endpoint positions;
- directivity authority;
- environment sound speed;
- GA configuration and existing numeric tolerance authority;
- GA implementation authority.

Changing Portal/region geometry or authority produces a different input identity. If the old exact authority can no longer resolve, an old saved path does not reopen as current.

## Focused tests

`backend/tests/test_cad_geometric_acoustics_adapter.py` covers:

- two-region open Portal direct path;
- exact ordered region sequence / Portal interaction identity;
- closed Portal rejection;
- Portal-plane crossing outside the aperture rejection;
- wrong region adjacency rejection;
- source wrong-region rejection;
- receiver wrong-region rejection;
- opaque-surface occlusion;
- directed orientation/reversal rejection;
- deterministic path identity;
- exact save/reopen;
- stale Portal authority rejection;
- unsupported BoundaryTermination fail-closed;
- existing single-region direct / first-order / second-order regressions.

The existing `.github/workflows/r150-deterministic-ga-adapter.yml` remains the focused R150 workflow and also executes the pinned pyroomacoustics single-region regression.

## Explicit non-claims

- maximum Portal crossings: **1**;
- supported/configured reflection order in the Portal lane: **0** (direct only);
- single-region first-/second-order reflection support remains unchanged;
- third+ reflection order: **not implemented**;
- reflection-before/after-Portal: **not implemented**;
- late field / stochastic ray tracing: **not implemented**;
- scattering transport: **not implemented**;
- diffraction: **not implemented**;
- nontrivial BoundaryTermination propagation: **not implemented**;
- coherent phase: **UNAVAILABLE_NOT_SYNTHESIZED**;
- production GA solver selection: **not claimed**;
- production / owned-room validation: **not claimed**.

RDC usage: **0**.
