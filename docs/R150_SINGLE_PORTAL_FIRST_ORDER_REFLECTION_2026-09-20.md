# R150 bounded one-Portal first-order specular reflection

Issue #101 follow-up to PR #270.

Task-start authority: `main@c77b8b50d7e4f9706c08e64a1daa7a7a51e8ed96`.

## Scope

This slice authorizes exactly:

- two explicit `AcousticRegion` declarations;
- one explicit directed open `Portal`;
- exactly one Portal crossing;
- exactly one ordinary finite planar R120 reflection surface;
- exactly one specular reflection.

Two physical event topologies are supported:

1. source → source-region reflection → Portal aperture → receiver;
2. source → Portal aperture → receiver-region reflection → receiver.

The Portal aperture is never a reflection surface.

Multi-Portal reflected paths, two-or-more reflected events, Portal reflection, diffraction, scattering, stochastic ray tracing, late decay, hybrid crossover, production validation, and owned-room validation remain unsupported.

## Geometry and topology authority

No independent geometry model is introduced.

The execution input is still compiled from the exact current R120 geometry, region authority, Portal authority, and boundary-termination authority. For the reflected Portal lane, the compiler additionally materializes `GeometricSurfacePlane` adapters only for ordinary region-boundary surfaces. Exact Portal aperture surface identities are removed from that candidate set.

Every accepted reflection must reproduce on the exact finite R120 semantic surface using the existing triangle-domain test. Infinite-plane image-source solutions outside that finite extent are rejected.

The Portal crossing is resolved with the existing directed exact-aperture intersection. A crossing outside the aperture, with reversed region direction, or outside the physical event order is rejected.

Parallel/ambiguous Portal topology is rejected by the existing `GeometricPortalGraph` compiler. Reflection order 1 additionally requires exactly two regions, one Portal, and `maximum_portal_crossings=1`.

## Reflection construction

A Portal contributes no refraction in this geometry-only path construction.

For a source-region surface, the source is mirrored across the exact plane and intersected against the receiver ray. The resulting finite reflection point must then connect to the receiver through the exact Portal aperture.

For a receiver-region surface, the receiver is mirrored and the source-to-image ray reconstructs the finite receiver-side reflection point. The source-to-reflection propagation must cross the exact Portal aperture first.

The emitted typed event sequence is therefore part of path identity:

- `reflection, portal_crossing`, or
- `portal_crossing, reflection`.

The legacy ordered reflection-surface fields remain present for backward-compatible first-order material semantics, while typed events distinguish topology even if coordinates coincide.

## Region membership and occlusion

Each physical segment is proved independently with existing capped-region authority.

Source-side topology requires segment-region sequence:

`source region, source region, receiver region`.

Receiver-side topology requires:

`source region, receiver region, receiver region`.

`region_segment_membership_with_portal_caps` must return `valid` for every segment. The exact existing R120 triangle occlusion test is then run for every segment. Endpoint tolerance handles the expected contact at the selected reflection surface; a distinct blocking triangle remains a rejection.

## Material and response authority

The geometry artifact continues to reuse the existing R150 material-band evaluator. Unknown or stale material authority produces an explicit rejected candidate and is never converted to zero response.

No new complex reflection model is introduced in this slice. PR #276's `cad_geometric_acoustics_response.py` already evaluates one path by multiplying the established source transfer, propagation, exact reflection transfer authority, and exact Portal acoustic transfer authority. A focused regression now exercises a path carrying one reflection event and one Portal event together.

Thus complex pressure normalization, source capability, phase convention, frequency-grid authority, reflection coefficient authority, and Portal transfer authority remain owned by the existing path-response layer.

## Identity and persistence

The new reflected path semantic identity includes:

- source/receiver identities;
- reflective surface identity and exact reflection point;
- ordered typed reflection/Portal events;
- directed source/receiver region identities;
- exact Portal crossing point;
- total path length and propagation delay;
- source directivity contribution/frequency grid;
- exact material contribution;
- solver implementation authority;
- the exact execution-input semantic SHA-256.

The execution-input hash itself binds exact R120 compiled geometry/topology identity, region and Portal authorities, aperture/graph identity, source/receiver region bindings, frequency centers, numerical tolerances, and solver configuration.

The artifact repository handles the new `single_portal_first_order_specular` scope by replaying the deterministic construction from the exact persisted/current authorities on reopen. A changed/missing R120 geometry, Portal authority, material authority, directivity dataset, solver input, or event result cannot reopen as the same current artifact.

Legacy direct Portal artifacts retain their previous identity and validation path.

## Fail-closed boundary

Explicitly rejected or unsupported cases include:

- reflection outside exact finite surface;
- exact Portal aperture miss or wrong crossing direction/order;
- source/receiver region mismatch;
- segment leaving its assigned region;
- R120 segment occlusion;
- stale R120 geometry / Portal / material authority;
- ambiguous parallel Portal topology;
- reflection-order 1 with more than one Portal crossing;
- second-order Portal reflection request;
- Portal aperture surface used as reflection candidate;
- arbitrary multi-Portal reflected topology.

Unsupported states are represented by exceptions or typed rejected candidates; they are not emitted as an empty-success response or zero pressure.

## Analytic fixture

The focused two-room fixture is two adjacent boxes:

- region A: `x=[0,2]`;
- region B: `x=[2,4]`;
- common `y=[0,3]`, `z=[0,2]`;
- Portal at `x=2`, `y=[1,2]`, `z=[0.5,1.5]`;
- source `(1,1,1)`;
- receiver `(3,2,1)`.

Analytic accepted paths:

- source-side left wall: reflection `(0,1.25,1)`, Portal `(2,1.75,1)`;
- receiver-side right wall: Portal `(2,1.25,1)`, reflection `(4,1.75,1)`;
- both total lengths: `sqrt(17) m`.

At 500 Hz the fixture material has absorption 0.2 and scattering 0.1, reusing the established geometry-layer specular energy factor `(1-0.2)(1-0.1)=0.72`.

Additional cases cover finite-surface rejection, Portal miss, occlusion, event-order/region-evidence rejection, R120 identity mismatch, stale material, stale Portal, deterministic save/reopen, and material identity binding.

The pre-existing complete R150 adapter and path-response suites remain regression authorities for same-region direct/first/second reflection and multi-Portal direct propagation.

## Ownership and non-claims

Changed ownership is limited to R150 geometric acoustics implementation/tests, a task-specific workflow, and this document.

`backend/src/htdt/r120_geometry_compiler.py` is unchanged.

HTDT-Capture is unchanged.

R100B, R130D, R140, R160, R170, shared implementation roadmap/status documents, production GA validation, and owned-room validation are unchanged.

RDC usage: **0**.
