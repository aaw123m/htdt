# R170A low-band integration — 2026-09-20

## Scope

R170A establishes a solver-neutral product prediction provider over exact immutable R-series acoustic results. This slice is capability-gated and fail-closed. It does not select a production solver and does not convert candidate solver output into production or owned-room truth.

## Starting authority

- base branch: `main`
- starting commit: `a8491db973c6fcefc66e259903ca668936f63a2e`
- R100B production-adoption decision: `NO_GO`
- RDC usage: **0**
- HTDT-Capture changes: **none**

## Implemented bounded lane

The provider adapter consumes one exact R130 `AcousticSolverResultEnvelope` whose sole observable is `complex_pressure` and publishes a typed product-facing contract with:

- exactly one source
- explicit ordered receiver set
- exact SceneRevision id/content hash
- exact AcousticSceneSnapshot id/hash
- exact AcousticPredictionRequest id/hash/deterministic input hash
- exact solver-result id/hash and deterministic solver-input hash
- exact solver implementation ref
- exact source/R110 identity and optional wave-excitation binding identity
- exact receiver bindings and positions
- exact environment authority
- exact valid frequency domain
- low-band frequency-response magnitude in Pa and dB SPL
- phase only from explicit complex pressure with the artifact phasor convention
- exact artifact/schema/provenance refs
- explicit candidate/validated/production state plus unvalidated/synthetic/owned-room evidence scope
- explicit CURRENT/STALE resolution against a supplied current authority

The adapter is the only R170A layer that parses the R130 external complex-pressure payload. N70/O-series consumers receive the typed provider or typed `FrequencyResponse`, not raw solver JSON.

## Capability gates

R170A marks these observables READY in the current bounded lane:

- `frequency_response_magnitude`
- `frequency_response_phase`

The following remain explicit `UNSUPPORTED` and cannot be replaced with a bad numerical score:

- impulse response
- RT60
- EDT
- C50
- C80
- arrival timing
- spatial pressure field
- broadband hybrid result

Requests outside the exact valid frequency band also fail closed.

## Product integration

### N70

`provider_frequency_response(...)` exposes a typed, band-checked frequency response without exposing the raw solver payload.

### O30 / O40

`target_objective_evaluation_from_provider(...)` evaluates only READY frequency-response magnitude capability. The resulting immutable objective input ref identifies the exact R170A provider. `PredictionProviderObjectiveConnection` persists the exact provider/binding/evaluation relationship; normal O40 Pareto authority then consumes those immutable objective evaluations unchanged.

### O50

`CadMeasurementPlan` has an optional exact R170A provider binding id/hash. The fields are omitted from the identity payload when absent, preserving the existing RoomSim/legacy plan hash shape. Binding is allowed only while the plan is still planned and only for an O50 binding requiring FR magnitude.

### O60

`build_provider_measurement_validation(...)` compares the typed provider frequency response against measured frequency-response evidence using existing O60 residual authority. It requires the measurement and provider to share the exact document/SceneRevision/content hash and keeps the prediction source id as the R170A provider id.

### O70

O70 can bind the same exact O60 validation id/hash through an `O70_ADAPTIVE` provider binding. Existing O70 validation/residual authority remains unchanged; R170A does not bypass its owned-room gates.

## Persistence and stale/tamper behavior

`CadPredictionProviderRepository` stores immutable providers and consumer bindings in the native CAD SQLite database. Reopen validation reconstructs the provider from the exact underlying SceneRevision, snapshot, request, solver result, artifact, schema and provenance authorities.

It fails closed when any required authority is missing, externally tampered, or no longer reproduces the stored provider identity.

`ProviderCurrentAuthority` / `require_provider_current(...)` rejects changes to:

- SceneRevision
- scene content
- AcousticSceneSnapshot
- prediction request/deterministic input
- solver result/result hash
- deterministic solver input
- source
- receiver set/order
- valid band
- solver implementation

## Production-adoption boundary

Current R130 candidate provenance contains `candidate_only=true` and `production_solver_selected=false`. R170A therefore refuses a production provider even if a caller supplies validation/adoption refs. A production provider additionally requires explicit owned-room evidence scope and production provenance; this slice creates no such authority.

R100B remains `NO_GO`. No owned-room validation is claimed.

## Backward compatibility

R130 output is not stored in or re-labelled as a `CadRoomSimAttempt`. Existing RoomSim types/repositories are unchanged. The focused test workflow runs the existing RoomSim result regression suite alongside R170A and O50 tests.

## Verification

Focused workflow:

- workflow: `R170A Low-Band Prediction Provider`
- run: `35500786214`
- head: `5302e90dd52f6cdd81db4a8ac486293383018c51`
- result: **PASS**
- pytest: **15 passed**
- compile check: **PASS**

Covered invariants include deterministic provider identity, exact R130 binding, save/reopen, external tamper rejection, stale SceneRevision/solver-input rejection, receiver mismatch rejection, unsupported-observable rejection, candidate-to-production rejection, O30/O40 connection persistence, O50 exact binding, O60 typed residual comparison, O70 validation binding, and RoomSim legacy regression.

A final normal `CI` run and focused workflow run are required on the PR head after this implementation-record-only commit. Their final run ids/results are recorded in the Draft PR description so that this file does not require another evidence-only commit.

## Remaining gates / non-goals

Still intentionally unresolved:

- production solver selection/adoption
- owned-room validation
- R180
- R150 expansion
- R160 numeric stitching
- GPU backend
- HTDT-Capture
- UX160 Windows visual acceptance

These gates must not be inferred from R170A software integration.
