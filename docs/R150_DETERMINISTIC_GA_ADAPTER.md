# R150 deterministic direct / first-specular adapter foundation

Status: **implemented as a bounded deterministic execution foundation for exact shoebox and general planar single-region geometry; R150 is not complete and no production GA solver is selected.**

Issue: #101  
Adapter authority: `htdt.r150.deterministic-path@1`  
Shoebox reference candidate: pyroomacoustics 0.10.1 image-source model  
General-planar construction authority: `adapter-kernel:htdt-r150-general-planar-first-order@1`

## Scope

The adapter establishes this solver-neutral geometrical-acoustics execution chain:

```text
AcousticSceneSnapshot
  -> AcousticPredictionRequest(observable=deterministic_paths)
  -> READY AcousticSolverDispatchBinding
  -> DeterministicGaExecutionInput
  -> deterministic direct / first-order image construction
  -> exact finite-surface + visibility capability checks
  -> DeterministicPathArtifact
  -> AcousticSolverResultEnvelope
```

Two explicit geometry policies exist:

- `exact_axis_aligned_closed_shoebox_v1`: the PR #245 compatibility/reference lane. It retains the pinned pyroomacoustics 0.10.1 shoebox image-source bridge.
- `general_planar_closed_polyhedral_v1`: the current arbitrary-planar first-order lane. HTDT mirrors the exact source across each accepted semantic plane and computes the first-order intersection itself. It does not approximate the room as a shoebox and does not use pyroomacoustics native shoebox image generation for this lane.

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

## Visibility / occlusion semantics

Direct and reflected visibility both use exact R120 triangles.

For a reflection candidate, both segments are checked independently:

- source -> reflection point
- reflection point -> receiver

No reflecting semantic surface is globally removed from visibility. Instead, triangle hits within the declared geometric distance tolerance of a segment endpoint are excluded. This prevents the intended touch at the reflection point from self-blocking while still allowing another interior intersection with the same semantic surface to block the path.

The segment-triangle test derives a normalized endpoint parameter epsilon from `geometric_tolerance_m / segment_length`; its parallel/barycentric tolerance is likewise scaled from the declared geometry tolerance and segment/triangle extent. Therefore the numerical policy remains tied to the versioned configuration rather than an unrecorded magic epsilon.

Shared-edge/touching semantics are intentionally bounded: an intersection that occurs only within the endpoint epsilon is treated as the intended endpoint contact; an intersection strictly inside either segment with any triangle, including another surface sharing an edge, blocks the candidate. Coplanar/grazing cases that cannot establish an unambiguous finite first-order interaction are not promoted into a separate inferred path truth.

## Boundary / material contribution

The existing R150 geometrical-energy semantics are unchanged:

```text
relative_energy_transport_per_m2
  = (1 / path_length^2)
    * source_directivity_magnitude_linear^2
    * specular_energy_factor
```

For direct paths, `specular_energy_factor = 1`. For supported reflected paths:

```text
specular_energy_factor = (1 - absorption) * (1 - scattering)
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

`DeterministicPathArtifact` remains schema/version 1. The existing path truth already represents arbitrary ordered semantic surface ids and interaction points, so general geometry does not introduce a second artifact type.

`DeterministicGaExecutionInput` receives additive optional general-planar fields. Legacy shoebox semantic payloads omit those fields, and legacy plane payloads without point/normal/triangle-index fields remain valid. This preserves old shoebox execution semantics while allowing new general-planar inputs to carry exact plane/triangle authority.

`CadDeterministicPathArtifactRepository` continues append-only persistence for execution inputs and path artifacts. Reopen regenerates the execution input from exact persisted snapshot/request/dispatch/R120/region/portal/termination/source/directivity/config authorities and re-resolves material/directivity contributions. General-planar save/reopen is covered by a focused exact-identity test.

## Determinism

Canonical accepted-path ordering remains:

1. source id
2. receiver id
3. direct before reflection
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

## Explicitly deferred

The following remain open R150/R160 work:

- production GA solver selection
- explicit multi-region / Portal propagation
- non-`explicit_none` BoundaryTermination propagation
- qualified general-concave-room coverage and validation
- second and higher-order reflections
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
