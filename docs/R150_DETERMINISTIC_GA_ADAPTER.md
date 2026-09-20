# R150 deterministic direct / bounded specular adapter foundation

Status: **implemented as a bounded deterministic execution foundation for exact shoebox direct/first-order and general-planar single-region direct/first-/second-order specular geometry; R150 is not complete and no production GA solver is selected.**

Issue: #101  
Adapter authority: `htdt.r150.deterministic-path@1`  
Shoebox reference candidate: pyroomacoustics 0.10.1 image-source model  
General-planar construction authorities: `adapter-kernel:htdt-r150-general-planar-first-order@1` and `adapter-kernel:htdt-r150-general-planar-second-order@2`

## Scope

The adapter establishes this solver-neutral geometrical-acoustics execution chain:

```text
AcousticSceneSnapshot
  -> AcousticPredictionRequest(observable=deterministic_paths)
  -> READY AcousticSolverDispatchBinding
  -> DeterministicGaExecutionInput
  -> deterministic direct / first-/second-order image construction
  -> exact finite-surface + multi-segment visibility capability checks
  -> DeterministicPathArtifact
  -> AcousticSolverResultEnvelope
```

Two explicit geometry policies exist:

- `exact_axis_aligned_closed_shoebox_v1`: the PR #245 compatibility/reference lane. It retains the pinned pyroomacoustics 0.10.1 shoebox image-source bridge.
- `general_planar_closed_polyhedral_v1`: the arbitrary-planar lane. With `maximum_reflection_order=1`, it preserves the existing first-order kernel/provenance. With `maximum_reflection_order=2`, it uses the second-order kernel/provenance and enumerates deterministic ordered surface pairs. It does not approximate the room as a shoebox and does not use pyroomacoustics native shoebox image generation for this lane.

The general-planar kernel is an adapter execution authority, not a production GA solver selection. This slice does not claim full R150 numerical validation or general concave-room qualification.

## Exact geometry authority

The adapter consumes existing exact R120 authority rather than constructing a second room model:

- R120 compiled vertices, triangles, and semantic-surface identity
- exact `AcousticRegionAuthority`
- exact `PortalAuthority`
- exact `BoundaryTerminationAuthority`
- exact surface material / boundary-physics refs
- R110 source identity, reference point, and source axis
- exact `DirectivityDataset`
- exact receiver world position
- exact environment sound-speed authority
- exact solver/adapter implementation and configuration refs

The general-planar lane requires exactly one explicit acoustic region, no Portal declarations, and no BoundaryTermination declarations. The exact region boundary surface ids must equal the R120 `room_boundary` subset, and that subset must itself be a closed manifold with non-zero enclosed volume. Multi-region and Portal topology remain explicitly unsupported; they are not flattened into one room.

Every region boundary semantic surface must be planar within the configuration's `geometric_tolerance_m`. A semantic surface may contain multiple coplanar triangles; those triangles remain one surface authority and one reflection candidate. Plane extraction uses the exact compiled triangles and preserves their indices. A region-boundary surface that is nonplanar or degenerate fails closed for the whole execution input.

Planar non-region semantic surfaces are also eligible first-order candidates. A nonplanar or degenerate non-region semantic surface is retained in the exact triangle visibility geometry but is listed as an unsupported reflection surface; it is never silently planarized.

Source and receiver region membership is evaluated against the exact region triangle shell. Boundary or numerically ambiguous membership fails closed as `UNSUPPORTED_REGION_MEMBERSHIP`; unmodeled external space is never treated as an implicit propagation region.

## First-order construction and finite-surface membership

For each accepted plane, the general-planar lane deterministically computes:

```text
source
  -> mirror source across exact plane
  -> line from mirrored source to receiver
  -> exact plane intersection
  -> reflection point
  -> source-to-reflection segment
  -> reflection-to-receiver segment
  -> path length
  -> delay = path_length / exact sound_speed
```

The infinite-plane intersection is only a candidate. The reflection point must lie in the union of the exact R120 triangles mapped to that semantic surface. An AABB or plane bounding box is not sufficient. A point outside the finite semantic triangle domain is rejected as `UNSUPPORTED_GEOMETRY`.

The deterministic trapezoidal-prism fixture includes a non-axis-aligned wall with plane `3x + y = 12`; its first-order reflection point, path length, delay, and exact semantic surface id are checked analytically.

## Second-order construction and finite-surface rule

Second-order support is intentionally bounded to `maximum_reflection_order=2` on the general-planar lane. The shoebox/pyroomacoustics compatibility lane remains first-order only.

For an ordered physical interaction sequence `(A, B)`:

```text
source
  -> mirror across A = image_A
  -> mirror image_A across B = image_AB
  -> intersect image_AB -> receiver with plane B = reflection_2
  -> intersect image_A -> reflection_2 with plane A = reflection_1
  -> source -> reflection_1 -> reflection_2 -> receiver
```

The ordered semantic surface sequence is authoritative. `(A, B)` and `(B, A)` are generated and evaluated independently and may produce different accepted paths, different points, different lengths, or different rejection reasons.

Each reconstructed point must lie in the exact R120 triangle union for its corresponding semantic surface. Infinite-plane success is not sufficient. The implementation also rejects a point that is simultaneously on another semantic surface within the declared geometry tolerance, treating shared-edge or multi-surface contact as ambiguous rather than inventing an interaction assignment.

Degenerate second-order candidates fail closed:

- immediate repeat `(A, A)` is rejected;
- distinct semantic surfaces whose planes are coincident within tolerance are rejected;
- nonplanar/degenerate surfaces never enter the ordered planar candidate set;
- failed reverse reconstruction, reflection-point collapse, and zero-length segments are rejected;
- grazing / plane-parallel contact within the versioned tolerance is rejected;
- parallel but distinct planes are supported when reverse reconstruction, finite membership, and visibility all remain unambiguous.

The analytical rectangular fixture fixes source `(1,1,1)` and receiver `(3,2,1)`. For ordered pair `left x=0 -> right x=4`, the expected reflection points are `(0, 7/6, 1)` and `(4, 11/6, 1)`, total path length is `sqrt(37) m`, and delay is `sqrt(37) / 343 s`. The reverse order is separately represented and has different reflection points and path identity.

## Visibility / occlusion semantics

Direct and reflected visibility both use exact R120 triangles.

For a first-order reflection candidate, both segments are checked independently:

- source -> reflection point
- reflection point -> receiver

For a second-order candidate, all three segments are checked independently:

- source -> reflection point 1
- reflection point 1 -> reflection point 2
- reflection point 2 -> receiver

In the general-planar lane, no reflecting semantic surface is globally removed from visibility. Instead, triangle hits within the declared geometric distance tolerance of a segment endpoint are excluded. This prevents the intended touch at the reflection point from self-blocking while still allowing another interior intersection with the same semantic surface to block the path. The legacy shoebox lane retains the PR #245 reflecting-surface exclusion behavior for backward-compatible replay.

For general-planar visibility, the segment-triangle test derives a normalized endpoint parameter epsilon from `geometric_tolerance_m / segment_length`; its parallel/barycentric tolerance is likewise scaled from the declared geometry tolerance and segment/triangle extent. Therefore the new numerical policy remains tied to the versioned configuration rather than an unrecorded magic epsilon. Legacy shoebox visibility keeps the original PR #245 tolerance semantics.

Shared-edge/touching semantics are intentionally bounded: an intersection that occurs only within the endpoint epsilon is treated as the intended endpoint contact; an intersection strictly inside either segment with any triangle, including another surface sharing an edge, blocks the candidate. Coplanar/grazing cases that cannot establish an unambiguous finite first-order interaction are not promoted into a separate inferred path truth.

## Boundary / material contribution

The existing R150 geometrical-energy semantics are unchanged:

```text
relative_energy_transport_per_m2
  = (1 / path_length^2)
    * source_directivity_magnitude_linear^2
    * specular_energy_factor
```

For direct paths, `specular_energy_factor = 1`. For each supported reflection interaction:

```text
specular_energy_factor_i = (1 - absorption_i) * (1 - scattering_i)
```

First-order transport uses one factor. Second-order transport multiplies the two ordered interaction factors exactly:

```text
relative_energy_transport_per_m2
  = (1 / path_length^2)
    * source_directivity_magnitude_linear^2
    * specular_energy_factor_1
    * specular_energy_factor_2
```

A reflection is accepted only when the exact material resolves to `AcousticMaterial.geometric_model == "banded"` and one exact requested center-frequency band exists. Unsupported or missing material data becomes `UNSUPPORTED_BOUNDARY_QUANTITY`.

This quantity is deliberately phase-free. Scalar absorption/scattering is not converted into a complex reflection coefficient, impedance, or coherent phase. `DeterministicPathArtifact` continues to record `UNAVAILABLE_NOT_SYNTHESIZED` for coherent phase.

Attached-treatment composition remains unsupported in this adapter foundation. A snapshot with active `treatment_boundary_bindings` is rejected rather than inventing a composite law.

## Directivity

Source directivity is evaluated only through the exact bound `DirectivityDataset`.

- angle/frequency outside exact authority -> `UNSUPPORTED_DIRECTIVITY`
- no implicit omnidirectional fallback
- no phase synthesized from magnitude-only data
- analytic directivity names are not silently converted into a numerical evaluator
- an explicit horizontal source axis is still required by this bounded evaluator

The general-planar lane reuses this same authority; it does not create a second directivity path.

## Artifact and persistence compatibility

`DeterministicPathArtifact` remains schema/version 1. The existing path truth already represents arbitrary ordered semantic surface ids and interaction points, so second-order support does not introduce a parallel artifact type. `DeterministicPathBandQuantity` adds an optional ordered `boundary_materials` tuple only for second-order paths; legacy direct/first-order semantic payloads omit that field so existing hashes and persisted payload validation remain compatible.

`DeterministicGaExecutionInput` receives additive optional general-planar fields. Legacy shoebox semantic payloads omit those fields, and legacy plane payloads without point/normal/triangle-index fields remain valid. This preserves old shoebox execution semantics while allowing new general-planar inputs to carry exact plane/triangle authority.

`CadDeterministicPathArtifactRepository` continues append-only persistence for execution inputs and path artifacts. Reopen regenerates the execution input from exact persisted snapshot/request/dispatch/R120/region/portal/termination/source/directivity/config authorities and re-resolves material/directivity contributions. General-planar save/reopen is covered by a focused exact-identity test.

## Determinism

Canonical accepted-path ordering remains:

1. source id
2. receiver id
3. path order (`0` direct, `1` first-order, `2` second-order)
4. ordered interaction surface ids
5. path id

Rejected candidates are also canonically ordered.

The default numeric configuration remains:

- geometry tolerance: `1e-9 m`
- shoebox candidate image match tolerance: `1e-8 m`
- semantic numeric identity rounding: 12 decimal places

The image-match tolerance is relevant only to the legacy pyroomacoustics shoebox lane. The general-planar lane records the same configuration contract but constructs its image analytically from exact R120 planes.

## Focused validation

`backend/tests/test_cad_geometric_acoustics_adapter.py` covers the existing PR #245 regressions plus:

- analytical rectangular second-order reflection points / `sqrt(37)` path length / delay
- ordered `(A,B)` vs `(B,A)` path identity
- second reflection point outside exact finite triangle domain rejection
- intermediate second-order segment occlusion by exact R120 triangles
- same-surface immediate-repeat rejection and deterministic rerun/hash/save-reopen
- two-interaction material-factor multiplication with phase remaining unavailable
- non-axis-aligned arbitrary planar first-order reflection point
- analytic first-order path length and delay
- exact semantic surface identity
- finite semantic polygon rejection when the infinite-plane point lies outside
- reflected-path occlusion by another exact R120 surface
- nonplanar semantic object retained as visibility geometry but rejected as a reflection candidate
- typed unsupported multi-region and Portal states
- legacy execution-input payload loading without additive general-planar fields
- general-planar execution-input / artifact save-reopen exact identity

Existing focused regressions continue to cover deterministic ordering/hash, directivity capability failure, material capability failure, old shoebox analytic behavior, result-envelope persistence, and actual pyroomacoustics 0.10.1 shoebox execution.

`.github/workflows/r150-deterministic-ga-adapter.yml` runs the focused authority tests and the separate pinned pyroomacoustics shoebox candidate job.

Accepted second-order code head validation:

- R150 Deterministic GA Adapter run `35489481199`: **PASS**
- `focused-authority-tests`: **18 passed, 1 skipped**
- pinned pyroomacoustics legacy fixture: **1 passed**
- an earlier run `35489400087` exposed an additive optional-field canonicalization bug: unused `boundary_materials=null` entered path digest construction while validation omitted it. The code was changed so unused second-order fields are absent from legacy direct/first-order canonical payloads; the accepted run above then passed both jobs.
- no workflow expansion was required: the existing focused R150 job already executes the whole adapter fixture file and the separate legacy pyroomacoustics job remains sufficient for compatibility.


## Explicitly deferred

The following remain open R150/R160 work:

- production GA solver selection
- explicit multi-region / Portal propagation
- non-`explicit_none` BoundaryTermination propagation
- qualified general-concave-room coverage and validation
- third and higher-order reflections
- late reverberation / diffuse tail
- stochastic ray tracing
- directional scattering transport
- diffraction
- coherent reflection phase
- active treatment composition law
- R160 hybrid stitching
- GPU ray tracing
- optimization integration
- owned-room validation

R150 and Issue #101 therefore remain incomplete.


RDC usage: **0**.
