# R150 arbitrary bounded region graph / multi-Portal direct propagation

Status: **implementation in progress on a bounded deterministic direct-propagation slice. R150 remains incomplete and no production GA solver is selected.**

Issue: #101  
Base main: `a8491db973c6fcefc66e259903ca668936f63a2e`  
Adapter authority: `htdt.r150.deterministic-path@1`

## Scope

This slice extends the existing PR #265 two-region / one-Portal lane to an explicit bounded region graph:

```text
AcousticRegion A
  -> Portal AB
AcousticRegion B
  -> Portal BC
AcousticRegion C
  -> ...
```

The propagation lane remains **direct only**. Reflection-before-Portal, reflection-after-Portal, Portal reflection, diffraction, scattering, stochastic ray tracing, late-field synthesis, coherent phase synthesis, production GA adoption, and owned-room validation remain explicit non-claims.

## Graph authority

The implementation will preserve and validate:

- ordered region sequence;
- ordered Portal sequence;
- exact Portal identity and exact directed adjacency;
- explicit source and receiver region bindings;
- one exact crossing point per Portal;
- region-specific segment membership;
- segment occlusion result;
- deterministic topology graph identity;
- explicit maximum Portal crossing bound;
- deterministic path identity.

Portal adjacency is never inferred from coordinates. `PortalDeclaration.region_ids` plus its explicit directed side semantics is the graph authority.

## Bounded traversal policy

Initial policy:

- direct propagation only;
- **simple region path only**;
- repeated region traversal: rejected;
- repeated Portal traversal: rejected;
- cycles may exist in the declared graph, but traversal never revisits a region;
- deterministic outgoing-edge ordering by exact Portal identity;
- explicit `maximum_portal_crossings` configuration bound;
- disconnected source/receiver regions fail closed;
- graph/path enumeration is bounded by the crossing limit and simple-path policy.

The policy itself is included in deterministic execution identity so persisted artifacts become stale when topology policy changes.

## Geometry contract

For every traversed Portal:

- directed source-side / destination-side identity must match the graph edge;
- aperture is the exact R120 Portal polygon;
- the direct source-to-receiver segment must intersect each Portal plane in path order;
- each intersection must be inside the exact aperture polygon;
- each segment between source/crossings/receiver must remain in its declared AcousticRegion;
- each segment must pass exact R120 occlusion checks.

Portal-plane intersection outside the aperture is not a Portal crossing.

## Fail-closed contract

At minimum the slice rejects with explicit typed reasons:

- disconnected regions;
- incorrect Portal adjacency;
- invalid directed orientation;
- source/receiver region mismatch;
- aperture miss;
- intermediate region membership failure;
- occluded segment;
- duplicate/ambiguous Portal identity;
- crossing limit exceeded;
- unsupported BoundaryTermination interaction;
- unsupported/ambiguous topology.

## Persistence / staleness

On save/reopen, exact current authorities are re-resolved and the execution input/path is reproduced from:

- snapshot;
- R120 compiled geometry;
- AcousticRegion authority;
- Portal authority;
- exact Portal apertures and geometry;
- source and receiver authorities and region bindings;
- topology traversal policy and crossing limit.

Any mismatch prevents an old multi-Portal artifact from reopening as current.

## Required verification

Focused tests will cover:

- 3 regions / 2 Portals direct PASS;
- 4 regions / 3 Portals bounded PASS;
- disconnected graph FAIL;
- wrong adjacency FAIL;
- aperture miss FAIL;
- crossing limit FAIL;
- deterministic path identity;
- save/reopen;
- stale Portal rejection;
- existing 2-region / 1-Portal regression;
- existing single-region first/second reflection regressions.

The existing R150 focused workflow remains the primary gate and will be updated only as needed for this task-specific record.

## Non-claims

- reflection across Portal: not implemented;
- third-order reflection: not implemented;
- scattering: not implemented;
- diffraction: not implemented;
- stochastic ray tracing: not implemented;
- late decay: not implemented;
- coherent phase: `UNAVAILABLE_NOT_SYNTHESIZED`;
- production GA adoption: not claimed;
- owned-room validation: not claimed.

RDC usage: **0**.
