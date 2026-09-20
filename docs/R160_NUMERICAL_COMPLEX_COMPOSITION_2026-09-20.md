# R160 numerical complex hybrid composition — 2026-09-20

Issue #101 vertical slice. Start-time authority: `main@f7ba4d8b30605b60da914c5e497870b104dfc7fa`.

## Scope

This task advances the existing typed/bounded R160 authority to an exact-authority-bound numerical complex transfer composition. The implementation is isolated in `cad_hybrid_numerical_composition.py` and does not alter R150 response physics.

Out of scope and unchanged:

- HTDT-Capture
- R100B MFEM experiment code
- R130D PFFDTD geometry adapter
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/IMPLEMENTATION_ROADMAP.md`

RDC usage: 0.

## R130 / R150 physical convention comparison

R130 candidate complex-pressure artifact schema
`htdt.r130a.candidate-complex-pressure-artifact-1` stores Cartesian **absolute complex pressure**:

- quantity: `complex_pressure`
- unit: `Pa`
- phasor: `exp(-i*omega*t)`
- analysis Fourier kernel: `exp(+i*omega*t)`
- reference: finite-record `P/Q` multiplied by the exact
  `AcousticWaveExcitationAuthority Q(f)`
- source quantity: `complex_volume_velocity_m3_s`
- source phasor: `exp(-i*omega*t)`
- finite record: exact `[0,T)`

The R130 execution implementation forms a unit source with
`unit_source[0] = 1`. R160 therefore treats the schema-v1 finite-record
sample-zero source impulse as the explicit R130 time-origin authority. The
normalization authority requires the persisted `time_sampling` metadata to
state `finite_record_interval = [0,T)`; it is not inferred when absent.

R150 per-path response authority stores:

- quantity: `complex_acoustic_pressure_per_volume_velocity`
- unit: `Pa/(m3/s)`
- source normalization: `unit_volume_velocity_m3_s`
- phasor: `exp(+i*omega*t)`
- corresponding analysis kernel: `exp(-i*omega*t)`
- time origin: `source_t0`
- exact frequency grid
- explicit propagation, source directivity, reflection, and Portal transfer
  authorities

The two artifacts therefore cannot be stitched directly.

## Versioned convention normalization

R160 adds
`r160-complex-convention-normalization-1`.

For an exact shared frequency sample, let R130 store `P_-(f)` and exact source
authority store `Q_-(f)`, both under `exp(-i*omega*t)`.

The supported conversion is:

`H_-(f) = P_-(f) / Q_-(f)`

followed by opposite-phasor conversion:

`H_+(f) = conj(H_-(f))`

where `H_+` is the common
`complex_acoustic_pressure_per_volume_velocity` transfer under
`exp(+i*omega*t)`.

The time-origin conversion is versioned as:

`finite_record_sample_0_source_impulse -> source_t0`

with no phase shift. This conversion is allowed only for the exact R130
schema-v1 finite-record semantics above.

Fail closed cases include:

- zero or missing exact `Q(f)`
- unsupported phasor/Fourier metadata
- missing/invalid finite-record time-origin metadata
- R130 artifact grid different from the exact candidate input grid
- non-exact requested frequency bins
- stale R130 result/input/excitation/artifact identities

## Exact compatibility binding

Before composition, every R150 response must agree with R130 on:

- source entity id
- receiver id and receiver entity id
- exact R120 compiled geometry id/hash
- exact R110 source semantic hash

The R150 response remains responsible for its own transitive exact dependency
re-resolution. R160 additionally binds the exact response artifact refs into
the composition identity.

## R150 coherent path aggregation

The high-band aggregate is:

`H_ga(f) = sum_k H_path,k(f)`

and is a coherent complex sum only when **every required path response is
`COMPLEX_SUPPORTED`** on every exact output bin.

R160 does not:

- invent phase for magnitude-only paths
- treat unsupported paths as zero
- use incoherent magnitude sum as complex pressure transfer
- assume missing reflection/Portal transfer is unity

A required `MAGNITUDE_ONLY` or `UNSUPPORTED` path makes this initial
complex-output slice `UNSUPPORTED`; no complex samples are emitted.

## Frequency and crossover authority

The first slice uses exact shared frequency bins only. There is no
nearest-neighbor, interpolation, or implicit resampling authority.

The weight law is versioned as
`linear_frequency_complementary_v1`.

For transition endpoints `f0 < f1`:

- `f <= f0`: `w_low = 1`, `w_high = 0`
- `f0 < f < f1`:
  `w_high = (f-f0)/(f1-f0)`, `w_low = 1-w_high`
- `f >= f1`: `w_low = 0`, `w_high = 1`

The numerical hybrid response is:

`H_hybrid(f) = w_low(f) H_wave(f) + w_high(f) H_ga(f)`

with `w_low + w_high = 1` at every bin.

This is complementary complex blending, not `H_wave + H_ga`; identical
signals therefore remain identical through the transition instead of doubling
in amplitude.

## Persisted numerical artifact

`r160-numerical-hybrid-response-1` persists:

- exact R130 solver result id/hash
- exact R130 complex-pressure artifact ref
- exact R130 candidate-input id/version/hash
- exact wave-excitation ref
- exact R150 frequency-response refs
- exact coherent GA aggregate identity and aggregate
- exact frequency grid
- quantity/unit
- common phasor/Fourier convention
- source normalization
- time origin
- composition spec
- transition endpoints and weight law
- per-bin low/high weights
- normalized wave complex real/imag
- aggregated GA complex real/imag
- hybrid complex real/imag
- magnitude and phase
- capability state and unsupported reasons
- semantic hash

`CadNumericalHybridResponseRepository` is append-only. Save and reopen
re-resolve the exact composition authority, convention authority, R130 result
and payload, candidate input, excitation, and each R150 response, then
recompute the artifact. Missing or changed dependencies are rejected.

## Acceptance fixtures

Focused R160 tests cover:

1. identity transfer through low-only, 50/50 transition, and high-only weights
2. explicit opposite-phasor conjugation and rejection of implicit convention mismatch
3. no-double-count transition behavior
4. coherent sum of direct + reflection complex paths against an independent complex sum
5. magnitude-only required path fail-closed behavior
6. exact-grid mismatch rejection instead of nearest-neighbor guessing
7. exact save/reopen plus stale R130, R150, and composition-authority rejection
8. repository-native integration across the existing R130 artifact-generation
   path and the actual R150 deterministic complex response builder

The repository-native integration calls
`PffdtdCandidateWaveExecutor.execute()` and therefore exercises the real R130
complex-pressure artifact/envelope/persistence construction path. Its raw
candidate numerical output is deterministic test data injected at the bounded
backend seam, so this test does **not** claim a new PFFDTD numerical validation.
The R150 side is produced by
`build_deterministic_path_frequency_response()` using the actual complex
response authority.

## Verification

Dedicated workflow:
`.github/workflows/r160-numerical-hybrid-composition.yml`.

Latest code validation before this document update:

- R160 numerical composition fixtures: **8 passed**
- existing R160 typed authority regressions: **27 passed**
- dedicated R160 workflow run #5: **PASS**
- exact branch diff: only the R160 numerical module, focused tests, this
  task-specific document, and the dedicated workflow
- HTDT-Capture changes: **0**
- RDC calls: **0**

The existing catalog warning about `EquipmentCatalogSnapshot.schema`
shadowing a Pydantic attribute remains unrelated to this slice.

## Exact supported subset

This slice supports one R130 complex-pressure result and one or more R150
per-path complex responses when all of the following hold:

- exact shared source/receiver/geometry/source-model identities
- exact shared frequency bins
- nonzero exact R130 `Q(f)`
- recognized R130 schema-v1 phasor/Fourier/time-origin semantics
- R150 p/Q unit-volume-velocity normalization under `exp(+i*omega*t)`
- every required R150 path is `COMPLEX_SUPPORTED`
- one explicit complementary crossover law

Everything else is fail-closed rather than inferred.

## Production non-claims

This slice is not production solver adoption and does not establish:

- an optimal crossover frequency
- full-broadband validation
- diffraction completeness
- late-reverberation completeness
- owned-room validation
- R180 completion

The implemented claim is limited to a numerically well-defined,
exact-authority-bound, no-double-count complex hybrid composition vertical
slice.

## Next slices

A subsequent R160/R170/R180 task can add a separately versioned resampling
authority, broaden per-frequency degraded capability, connect low-band
integration policy to R170, and add later broadband/room validation evidence.
Those are not implied by this PR.
