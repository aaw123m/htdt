# R150 arbitrary bounded region graph / multi-Portal direct propagation

Status: **software vertical slice implemented; focused R150 authority workflow passes. R150 remains incomplete and no production GA solver is selected.**

> Snapshot record as of 2026-09-20 (PR #270). PR #432 (2026-09-30) later added multi-Portal cross-region reflected paths, so the "reflection before/after/across Portal: not implemented" non-claims below no longer describe current main. [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) is the canonical current state.

Issue: #101  
Draft PR: #270  
Base main: `a8491db973c6fcefc66e259903ca668936f63a2e`  
Adapter authority: `htdt.r150.deterministic-path@1`  
Portal graph execution kernel authority: `adapter-kernel:htdt-r150-explicit-portal-direct@2`

## Scope

This slice extends the PR #265 two-region / one-Portal direct lane to a bounded explicit directed AcousticRegion graph:

```text
AcousticRegion A
  -> Portal AB
AcousticRegion B
  -> Portal BC
AcousticRegion C
  -> ...
```

Propagation remains **direct only**. The implementation does not add reflections before/after a Portal, Portal reflection, diffraction, scattering, stochastic ray tracing, late-field synthesis, or coherent phase synthesis.

## Implemented graph authority

`GeometricPortalGraph` is an immutable R150 authority derived from exact R120 geometry plus exact `AcousticRegionAuthority` and `PortalAuthority`. It binds:

- exact R120 compiled geometry id/hash;
- exact AcousticRegion authority id/hash;
- exact Portal authority id/hash;
- canonical AcousticRegion identity set;
- canonical directed Portal edges;
- exact Portal aperture id/hash per edge;
- traversal policy;
- repeated-region and repeated-Portal policy;
- deterministic traversal ordering;
- maximum crossing count;
- deterministic search-state ceiling.

Each emitted direct path preserves:

- ordered region sequence;
- ordered Portal interactions;
- exact Portal identity and directed adjacency;
- explicit source/receiver region bindings;
- one exact crossing point per traversed Portal;
- exact path identity;
- exact execution/topology identity.

Portal adjacency is never inferred from coordinates. `PortalDeclaration.region_ids` and
`directed_boundary_edge_loop_normal_from_first_to_second` are authoritative.

## Bounded traversal semantics

Current policy is `simple_region_path_v1`:

- direct propagation only;
- repeated AcousticRegion traversal: rejected;
- repeated Portal traversal: rejected;
- cycles may exist in the declared graph, but one candidate path never revisits a region;
- outgoing edges are explored deterministically by exact Portal id, then destination region id;
- `maximum_portal_crossings` is explicit and bounded to 1..16;
- hard graph-search ceiling: 4096 visited search states;
- disconnected directed graph: explicit rejected candidate;
- a route that exists only beyond the crossing bound: explicit crossing-limit rejection;
- multiple Portals between the same unordered region pair are currently treated as ambiguous topology and rejected rather than guessed.

The traversal policy and crossing limit are part of graph/execution identity. Changing either makes a persisted execution input non-reproducible as current authority.

## Geometry proof

For every declared Portal, R150 verifies:

- exactly two explicit adjacent regions;
- explicit `state=open`;
- exact directed side semantics;
- exact ordered aperture boundary loop;
- boundary-edge incidence against compiled R120 geometry;
- exact semantic surface identity;
- aperture surfaces are boundary surfaces of both declared adjacent regions;
- the two adjacent regions share exactly the declared Portal surfaces;
- coplanarity and strict convexity of the aperture;
- directed loop normal points from the first region to the second;
- every AcousticRegion becomes a closed manifold when all incident exact Portal apertures are capped.

For every direct graph candidate, R150 then verifies:

1. each Portal plane intersects the source-to-receiver segment;
2. the intersection lies inside the exact aperture polygon;
3. crossings occur in declared region/Portal order;
4. each source/crossing/receiver sub-segment is valid inside its declared capped AcousticRegion;
5. each sub-segment is unoccluded by exact R120 triangle geometry.

A plane intersection outside the aperture is not classified as a Portal crossing.

## Fail-closed behavior

The implementation rejects or blocks with explicit reasons for:

- disconnected directed regions: `DISCONNECTED_REGION_GRAPH`;
- route requiring more crossings than configured: `PORTAL_CROSSING_LIMIT_EXCEEDED`;
- aperture miss / invalid crossing: `INVALID_PORTAL_CROSSING`;
- crossing order / region order mismatch: `INVALID_REGION_SEQUENCE`;
- intermediate region segment failure: `INTERMEDIATE_REGION_MEMBERSHIP_FAILURE`;
- opaque triangle occlusion: `BLOCKED_VISIBILITY`;
- graph search/topology failure: `UNSUPPORTED_PORTAL_TOPOLOGY`;
- invalid Portal state: `UNSUPPORTED_PORTAL_STATE`;
- invalid directed orientation: `UNSUPPORTED_PORTAL_ORIENTATION`;
- source/receiver region mismatch or non-exclusive membership: `UNSUPPORTED_REGION_MEMBERSHIP`;
- unsupported Portal aperture geometry: `UNSUPPORTED_PORTAL_APERTURE`;
- nontrivial `BoundaryTermination`: `UNSUPPORTED_BOUNDARY_TERMINATION`.

Duplicate Portal ids are already rejected by the exact R120 `PortalAuthority` model. R150 additionally rejects duplicate aperture identity, ambiguous parallel adjacency, reused Portal surfaces, and unknown graph-region references.

## R120 boundary and snapshot preflight bridge

`backend/src/htdt/r120_geometry_compiler.py` is intentionally unchanged.

At the base commit for this task, R120's special GA readiness proof is still bounded to exactly two regions and one Portal. That means a valid 3+ region graph can be proven exactly by R150 but would otherwise be blocked before R150 execution-input compilation.

A minimal backward-compatible snapshot extension therefore adds optional
`geometric_acoustics_topology_preflight_ref`. It is accepted only when:

- it is an exact `r150-portal-graph:<sha256>@1` authority ref;
- requested observables are bounded to `deterministic_paths`;
- R120 geometry is exact-preservation with no dropped/approximated features;
- only the existing Portal-interface diagnostic unresolved markers remain;
- exact region, Portal, and BoundaryTermination authority refs exist.

Existing snapshots omit this optional field from semantic identity when it is `None`, preserving old persisted snapshot hashes. Wave readiness is not promoted.

The final R150 execution input still recompiles and embeds the exact graph authority from current R120/region/Portal authorities; the snapshot preflight ref is not a substitute for execution-time proof.

## Backward compatibility

The PR #265 one-Portal models remain readable:

- legacy persisted configuration without `portal_traversal_policy` keeps its prior semantic payload;
- legacy persisted execution input without `portal_graph` / `region_declarations` keeps its prior semantic payload;
- `compile_single_portal_aperture`, `region_membership_with_portal_cap`, and `HtdtPortalDirectEngine` remain available;
- old single-Portal execution inputs reproduce through the legacy compilation branch;
- new builds use `simple_region_path_v1` and the v2 Portal graph kernel authority.

Existing single-region direct/first-order/second-order geometric-acoustics behavior remains on its previous path.

## Persistence and stale rejection

On save/reopen, the repository re-resolves and regenerates the exact execution input from:

- snapshot;
- prediction request and READY dispatch;
- solver configuration;
- R120 compiled geometry;
- AcousticRegion authority;
- Portal authority;
- BoundaryTermination authority;
- source/receiver authorities and region bindings;
- topology policy and crossing limit.

For graph paths it additionally re-evaluates:

- the bounded graph route;
- ordered Portal identities;
- every exact crossing point;
- crossing order;
- every region segment membership proof;
- every segment occlusion result;
- direct geometric length.

A stale/missing Portal authority or any regenerated-input mismatch prevents the old artifact from reopening as current.

## Verification

The focused workflow runs the complete
`backend/tests/test_cad_geometric_acoustics_adapter.py` suite, including the existing single-region first/second-order reflection regressions and the new graph invariants.

Observed focused result after implementation:

```text
41 passed, 1 skipped
```

New/extended coverage includes:

- 3 regions / 2 Portals direct PASS;
- 4 regions / 3 Portals bounded PASS;
- disconnected directed graph FAIL;
- wrong explicit adjacency FAIL;
- exact aperture miss FAIL;
- crossing-limit FAIL;
- deterministic multi-Portal artifact/path identity;
- multi-Portal save/reopen;
- stale multi-Portal authority rejection;
- existing 2-region / 1-Portal regression;
- opaque-segment rejection;
- source/receiver explicit-membership rejection;
- invalid orientation/state rejection;
- unsupported BoundaryTermination rejection;
- existing single-region first/second-order regression.

The pyroomacoustics candidate execution job also passes, confirming this slice did not regress that existing R150 candidate lane.

## Non-claims

- reflection before/after/across Portal: not implemented;
- Portal reflection: not implemented;
- third-order reflection: not implemented;
- scattering: not implemented;
- diffraction: not implemented;
- stochastic ray tracing: not implemented;
- late decay: not implemented;
- coherent phase: `UNAVAILABLE_NOT_SYNTHESIZED`;
- production GA adoption: not claimed;
- owned-room validation: not claimed.

RDC usage: **0**.
