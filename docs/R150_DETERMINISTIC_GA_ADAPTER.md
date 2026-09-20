# R150 deterministic direct / first-specular adapter foundation

Status: **implemented as a bounded candidate-execution foundation; R150 is not complete and no production GA solver is selected.**

Issue: #101  
Adapter authority: `htdt.r150.deterministic-path@1`  
Candidate engine: pyroomacoustics 0.10.1 image-source model (reference/candidate only)

## Scope

This slice establishes the first solver-neutral geometrical-acoustics execution chain:

```text
AcousticSceneSnapshot
  -> AcousticPredictionRequest(observable=deterministic_paths)
  -> READY AcousticSolverDispatchBinding
  -> DeterministicGaExecutionInput
  -> candidate image-source execution
  -> direct + first-order specular candidates
  -> DeterministicPathArtifact
  -> AcousticSolverResultEnvelope
```

It deliberately does **not** select pyroomacoustics as the production solver and does not claim R150 validation.

## Exact authority boundary

The adapter consumes the existing exact authorities rather than introducing a second room model. The concrete engine must also present the exact solver-implementation authority bound by the READY dispatch; mismatched engines are rejected before path execution:

- R120 compiled vertices / triangles / semantic surface identity
- exact acoustic-region authority
- exact portal authority
- exact boundary-termination authority
- R110 source identity and source reference axis
- exact DirectivityDataset identity
- exact receiver world position
- exact environment sound-speed authority
- exact external solver implementation and solver configuration refs

The current candidate-native room compiler accepts only an exact axis-aligned, closed shoebox shell with six separate semantic room-boundary surfaces and exactly one explicit acoustic region. Portal and boundary-termination authorities must both be `explicit_none`. Any other topology fails closed; it is not simplified into a shoebox. The six R120 room-boundary surfaces are additionally checked as their own closed manifold and their enclosed volume must match the bounding shoebox within the declared geometry tolerance; a hole or partial face is never filled by the candidate room constructor. Every source acoustic-reference point and receiver point must also lie strictly inside that sole explicit region (outside or boundary-ambiguous points are rejected), so unmodeled external space is never treated as an implicit propagation region.

Non-shoebox semantic surfaces remain part of the exact R120 visibility geometry. They can block direct/reflection segments, but this foundation does not synthesize native image sources for them. A first-order candidate on such a surface is recorded as `UNSUPPORTED_GEOMETRY`, so unsupported reflection coverage is visible rather than silently discarded.

## Typed deterministic path artifact

Every accepted path keeps:

- deterministic path identity/hash
- source and receiver identity
- `direct` or `specular_reflection`
- ordered semantic surface ids and reflection points
- geometric path length and propagation delay
- world departure and arrival propagation directions
- exact center-frequency band definition
- source-directivity evaluation identity and magnitude contribution
- exact material/boundary refs plus absorption/scattering contribution for reflections
- adapter and exact solver-implementation provenance

The path quantity is intentionally phase-free:

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

This is a declared geometrical-energy transport quantity, not calibrated SPL, pressure transfer, or a coherent reflection coefficient. Scalar absorption/scattering data is never promoted to phase. The artifact explicitly carries `UNAVAILABLE_NOT_SYNTHESIZED` for coherent phase.

## Capability gates

Source directivity is evaluated only through the exact bound `DirectivityDataset`. If a requested path angle/frequency is outside that authority, the candidate is rejected as `UNSUPPORTED_DIRECTIVITY`; there is no implicit omnidirectional fallback. This foundation also requires an explicit horizontal source aim axis and uses the HTDT z-up convention with the dataset's declared positive-left / positive-up convention; elevated/rolled source-frame reconstruction is not inferred.

R110 v1 currently has no numerical evaluator authority for analytic directivity names, including analytic omnidirectional declarations, so this slice does not silently add one. Supporting explicit analytic omnidirectional sources requires a separate exact numerical evaluator authority.

A reflection is accepted only when the exact surface material resolves to `AcousticMaterial.geometric_model == "banded"` and an exact requested center-frequency band exists. Missing or unsupported GA material data is `UNSUPPORTED_BOUNDARY_QUANTITY`. Wave impedance/admittance is not converted into a GA reflection quantity.

Attached-treatment composition is not flattened into a replacement wall coefficient in this slice. The existing treatment authority intentionally preserves base construction and selected treatment material separately, but it does not yet define the GA composition law needed to turn those authorities into one specular-energy factor. Therefore any snapshot carrying active `treatment_boundary_bindings` is rejected by this adapter foundation rather than silently ignoring the treatment or fabricating a composite reflection quantity.

## Determinism

Canonical path ordering is source, receiver, direct-before-reflection, ordered surface identity, path id. Rejected candidates are also canonically ordered.

The default configuration records:

- geometry comparison tolerance: `1e-9 m`
- candidate image-source match tolerance: `1e-8 m`
- semantic numeric identity rounding: 12 decimal places

The implementation does not claim raw floating-point bitwise portability across arbitrary runtimes. Artifact semantic identity is based on the recorded rounded typed values and exact authority hashes.

## Persistence

`CadDeterministicPathArtifactRepository` adds append-only persistence for both:

- `DeterministicGaExecutionInput`
- `DeterministicPathArtifact`

On reopen the execution input is regenerated from persisted snapshot/request/READY dispatch/descriptor/R120 geometry/region/portal/termination/source/directivity/receiver/config authorities. Artifact reopen also re-resolves material band contributions and directivity evaluations. Missing or mismatched authority fails closed.

The repository exposes exact external refs for the typed artifact and execution provenance so the existing `CadAcousticSolverResultRepository` can re-resolve the completed `AcousticSolverResultEnvelope`.

## Focused validation

`backend/tests/test_cad_geometric_acoustics_adapter.py` covers:

- direct path length / delay against analytic geometry
- first-order reflection point / length and exact surface identity against image-source geometry
- blocked direct path removal using exact R120 triangle visibility
- deterministic artifact identity and ordering
- exact candidate engine / READY-dispatch solver-implementation identity
- unsupported directivity angle without omnidirectional fill
- missing GA boundary quantity without fabricated reflection
- execution-input -> artifact -> result save/reopen and missing-authority fail-closed
- the same direct/first-reflection fixture with the actual pyroomacoustics 0.10.1 candidate when installed

`.github/workflows/r150-deterministic-ga-adapter.yml` runs the focused authority tests and a separate Linux candidate-execution job that installs pyroomacoustics 0.10.1 and executes the actual candidate adapter fixture.

## Explicitly deferred

The following remain open R150/R160 work:

- production GA solver selection
- general arbitrary planar/polyhedral first-order candidate execution
- explicit multi-region portal propagation
- non-`explicit_none` boundary terminations
- higher-order reflections
- late reverberation / diffuse tail
- stochastic ray tracing
- directional scattering transport
- diffraction
- coherent reflection phase
- hybrid R160 stitching
- GPU ray tracing
- optimization integration

