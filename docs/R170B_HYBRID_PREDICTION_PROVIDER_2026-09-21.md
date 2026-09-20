# R170B solver-neutral HybridPredictionProvider vertical slice

Date: 2026-09-21  
Issue: #101 / R170B hybrid integration  
Task-start main: `9e6066259ec58c093e9a7587550ccf907f28402f`

## Scope

This slice connects the existing R170A `LowBandPredictionProvider` identity authority to the R160 `NumericalHybridResponseArtifact` without exposing R160 raw JSON to N70/O-series consumers.

Implemented ownership is intentionally bounded to:

- `backend/src/htdt/cad_hybrid_prediction_provider.py`
- `backend/src/htdt/cad_hybrid_prediction_provider_integration.py`
- focused R170B tests
- append-only R170B provider/objective-connection persistence
- N70 typed frequency-response reads
- O30 typed objective input/evaluation binding
- O40 compatibility through the existing Pareto implementation
- a dedicated GitHub Actions workflow

R170A remains backward-compatible and is not generalized destructively. O50/O60/O70 hybrid validation is intentionally not implemented in this slice.

## Provider authority

Authority version:

`r170b-hybrid-prediction-provider-1`

Schema version:

`1`

The deterministic provider identity binds, at minimum:

- exact current R170A provider ref/id/hash
- exact R160 numerical artifact ref/id/hash
- exact R160 composition spec ref/id/hash
- exact R160 grid reconciliation ref
- exact R160 crossover configuration ref
- exact R160 convention-normalization ref
- exact R130 result identity
- exact R130 complex-pressure artifact identity through the base/R160 equality gate
- exact R150 response refs
- exact AcousticWaveExcitationAuthority ref
- exact source entity id
- exact receiver id
- SceneRevision/snapshot/request/environment identity inherited from the R170A provider
- exact R160 output grid, transition endpoints, and blend law
- absolute complex pressure samples and their magnitude/phase representation
- candidate-only evidence state and capability table

The builder fails closed when the R170A result/source/receiver identity and R160 identity do not agree exactly.

## Absolute pressure reconstruction

R160 publishes transfer samples:

`H_hybrid(f)` in `Pa/(m3/s)`

with common phasor:

`exp(+i*omega*t)`

The exact `AcousticWaveExcitationAuthority` publishes complex volume velocity:

`Q(f)` in `m3/s`

and the current authority uses:

`exp(-i*omega*t)`

R170B therefore performs the explicit complex conversion:

`Q_plus(f) = conj(Q_minus(f))`

and reconstructs:

`P_hybrid(f) = H_hybrid(f) * Q_plus(f)`

No phase is synthesized. Exact excitation samples are used directly. For output frequencies between excitation samples, R170B evaluates only the explicit `linear` interpolation authority, linearly in Cartesian complex real/imaginary coordinates. Unsupported interpolation methods and all extrapolation fail closed.

The product-facing representation contains:

- complex absolute pressure in Pa
- magnitude in Pa
- dB SPL relative to 20 µPa
- phase in degrees
- exact input/output phasor metadata
- `source_t0` time-origin metadata

A zero reconstructed pressure is rejected because this bounded provider promises finite dB SPL.

## Capability semantics

READY:

- `frequency_response_magnitude`
- `frequency_response_phase`
- `broadband_hybrid`

`broadband_hybrid` means only that the result is the typed product projection of an R160 numerical hybrid composition. It is not an owned-room accuracy claim.

UNSUPPORTED:

- impulse response
- RT60
- EDT
- C50
- C80
- arrival timing
- late decay
- diffraction completeness
- spatial pressure field

No numeric substitute is fabricated for unsupported observables.

## Evidence state

Every R170B provider in this slice is fixed to:

- `evidence_state=candidate`
- `evidence_scope=unvalidated`
- `production_adoption=false`

There is no validation authority or production-adoption authority field that can be populated. Model validation rejects attempts to promote this provider to validated/production/owned-room status.

A validated R170A base provider does not upgrade R170B because the R160 hybrid output itself has no production/owned-room validation authority in this slice.

## N70

`hybrid_provider_frequency_response(...)` is the product-facing typed read.

It requires exact source and receiver identity and a requested band inside the exact R160 output domain. It returns the shared `FrequencyResponse` contract and never returns R160 raw JSON. Extrapolation is prohibited.

## O30 / O40

The O30 adapter creates an immutable objective-input authority binding:

- exact R170B provider id/hash
- exact source
- exact receiver
- `frequency_response_magnitude`
- requested band

The persisted objective connection additionally binds the exact objective evaluation id/hash.

O40 consumes the resulting existing `CadObjectiveEvaluation` with the unchanged `build_pareto_set` algorithm. No Pareto algorithm semantics are modified.

## Persistence and stale rejection

`CadHybridPredictionProviderRepository` is append-only and rebuilds a provider from exact current authorities on save/reopen.

Reopen fails closed when any dependency no longer reproduces, including:

- stale/missing R170A base provider
- stale/missing R160 artifact
- changed/missing R160 composition spec
- R130 result/artifact identity changes
- R150 response identity changes
- grid reconciliation changes
- crossover configuration changes
- source excitation changes
- SceneRevision/source/receiver changes inherited through R170A
- output-grid changes
- phasor/normalization authority changes

R160's existing repository remains the authority for its own exact stale validation; R170B does not introduce an R160 semantic workaround.

## Non-claims

This slice is **not**:

- production solver adoption
- production broadband accuracy
- owned-room validation
- validated crossover frequency
- an optimal crossover claim
- late reverberation correctness
- diffraction correctness
- spatial field correctness
- R180 completion
- removal of the R100B `NO_GO`

Software integration PASS does not complete R160/R180 numerical or owned-room validation.

## Scope controls

- HTDT-Capture changes: 0
- RDC usage: 0
- R100B changes: 0
- R130D changes: 0
- R140 changes: 0
- R150 geometry/path-algorithm changes: 0
- UX changes: 0
- O50/O60/O70 hybrid-validation changes: 0
- R180 changes: 0
- `docs/IMPLEMENTATION_ROADMAP.md` changes: 0
- `docs/IMPLEMENTATION_STATUS.md` changes: 0
