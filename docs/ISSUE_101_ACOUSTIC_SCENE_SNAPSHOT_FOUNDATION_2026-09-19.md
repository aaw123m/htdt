# Issue #101 — AcousticSceneSnapshot foundation

## Scope

This slice introduces the solver-neutral authority boundary:

```text
SceneRevision
  -> exact SemanticAcousticGeometry / R120CompiledGeometry
  -> exact R110CompiledSourceModel authorities
  -> AcousticSceneSnapshot
  -> AcousticPredictionRequest
  -> future solver-specific adapter
```

It does not select or execute a production acoustic solver.

## AcousticSceneSnapshot authority

`backend/src/htdt/cad_acoustic_snapshot.py` adds immutable/versioned `AcousticSceneSnapshot` with deterministic semantic identity.

The snapshot binds:

- exact document id
- exact SceneRevision id/content hash
- optional exact SystemVariant id/hash
- exact SemanticAcousticGeometry id/hash
- exact R120CompiledGeometry id/hash
- compiled topology hash
- geometric tolerance and approximation/error metadata
- exact AcousticRegion, Portal, and BoundaryTermination refs carried by R120
- exact surface material/boundary configuration and deterministic configuration hash
- sorted exact source list
- exact R110 compiled source semantic hash for every source
- exact EquipmentDefinition id/version/hash from every R110 source
- exact DirectivityDataset id/version/hash/source-asset hash when used
- source acoustic reference point and its authority semantics
- source axis and its authority semantics
- directivity capability and purpose-specific R110 readiness
- receiver id/entity/world acoustic reference position
- optional authoritative receiver orientation
- optional exact measurement authority ref without redesigning measurement authority
- exact environment authority ref and only explicitly authoritative environment values
- requested and optionally authoritative valid frequency domains
- requested observables
- purpose-specific readiness and unresolved conditions

No default material, rigid wall, sound speed, temperature, directivity phase, or wave excitation is synthesized.

## Wave-source boundary

Current R110 v1 intentionally reports wave excitation as `BLOCKED_FOR_WAVE_EXCITATION` because there is no exact electrical-input to acoustic wave-excitation normalization authority.

`AcousticSceneSnapshot` preserves that state. A magnitude-only or complex directivity dataset is never promoted into acoustic source strength or volume velocity.

Therefore a snapshot can remain valid as an exact geometry/source authority while wave observables remain blocked.

## Readiness

There is no aggregate `solver_ready`.

The snapshot records:

- `geometry_ready`
- `geometric_directivity_ready`
- `wave_source_ready`
- `wave_boundary_ready`
- `environment_ready`
- `receiver_ready`
- `requested_observable_ready`
- per-observable `READY`, `BLOCKED`, or `UNSUPPORTED` state with reasons

The initial observable contract recognizes:

- `complex_pressure`
- `magnitude_response`
- `phase_response`
- `impulse_response`
- `spatial_pressure_field`
- `deterministic_paths`

Unknown observables remain explicitly unsupported.

## Material and boundary semantics

The snapshot copies only exact material and boundary-physics refs already present on `R120CompiledGeometry.surface_mapping`.

Missing material/boundary authority does not prevent snapshot creation. It prevents the relevant boundary readiness. No default wall or implicit rigid assumption is inserted.

AcousticRegion, Portal, and BoundaryTermination identities are preserved as exact refs from the compiled R120 authority. Unknown or missing portal/termination state remains unresolved.

## Environment semantics

`SnapshotEnvironmentAuthorityRef` stores an exact environment authority id/version/hash and only values with exact source authority where applicable.

For sound speed, the value and exact source authority must be supplied together.

If environment authority is unavailable, it remains absent/unknown and environment readiness is blocked. The snapshot never inserts 343 m/s implicitly.

## Receiver contract

`AcousticReceiverBinding` is deliberately solver-neutral and contains:

- receiver id
- scene entity id
- exact world acoustic reference position
- optional authoritative orientation
- reference semantics
- optional exact measurement authority ref
- requested output capabilities

The existing scene/measurement authorities are not redesigned.

## Prediction request integration

`AcousticPredictionRequest` is a new request authority layered beside the existing rectangular request path.

It includes:

- exact AcousticSceneSnapshot id/hash
- solver/model role id
- requested frequency domain
- requested observables
- exact numerical/fidelity policy ref
- deterministic input hash using the existing canonical prediction JSON/hash functions

No solver adapter is invoked.

`cad_prediction_request.py` and the rectangular model identity remain unchanged.

## Persistence and reopen validation

`backend/src/htdt/cad_acoustic_snapshot_repository.py` adds append-only persistence for snapshots and acoustic prediction requests.

Snapshot reopen re-resolves and validates:

- SceneRevision id/hash
- embedded SemanticAcousticGeometry id/hash
- optional SystemVariant id/hash and baseline
- exact R120CompiledGeometry id/hash/topology/approximation metadata
- exact R120 surface material/boundary configuration and region/portal/termination refs
- every exact R110 source hash
- through the R110 repository, exact EquipmentDefinition and DirectivityDataset authorities followed by deterministic recompilation
- receiver entity acoustic reference positions and optional orientations

Material and boundary configuration is part of snapshot identity. Environment exact authority/value metadata is also part of snapshot identity. A changed source, geometry, material/boundary configuration, environment authority, receiver set, frequency request, or observable request produces a different snapshot semantic hash.

## Focused fixtures

`backend/tests/test_cad_acoustic_snapshot.py` covers:

1. closed R120 compiled geometry
2. magnitude-only R110 source
3. complex-directivity R110 source
4. wave-excitation-blocked R110 source
5. multiple sources
6. receiver set
7. missing material authority
8. unresolved portal/termination authority
9. unknown environment without implicit 343 m/s
10. SceneRevision mismatch
11. compiled geometry hash mismatch
12. source hash mismatch
13. same exact input -> same snapshot hash
14. append-only save/reopen with exact authority re-resolution
15. snapshot/environment identity change -> prediction input hash change
16. rectangular legacy request regression
17. exact persisted AcousticPredictionRequest
18. explicit unsupported-observable state

## Deliberately out of scope

- R130 wave solver
- R150 ray solver
- solver-selection ADR
- PFFDTD changes
- MFEM changes
- synthetic acoustic prediction
- mesh/grid generation
- GPU scheduling
- UI
- treatment boundary compiler changes
- common roadmap/status documents

## Validation

Validation for this slice is performed by GitHub Actions. No RDC or Windows host access is required.

RDC used: 0.
