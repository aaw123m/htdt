# R160 explicit frequency-grid reconciliation and bounded stitch validation

Date: 2026-09-20  
Issue: #101  
Starting main: `1fb07fa210aae55d91c7d86b3f4c5ee993be7c93`  
RDC usage: 0

## Scope and claim

This slice extends the PR #283 exact-common-bin complex composition path with an
explicit, hash-bound frequency-grid reconciliation contract and a fixed bounded
crossover contract.

The maximum claim for this slice is:

> R160 can deterministically reconcile supported frequency grids and perform
> bounded complex hybrid composition under an explicit overlap contract.

The synthetic stitch fixture is **not production broadband validation**. This
slice does not claim optimal crossover selection, late-field completeness,
diffraction correctness, owned-room validity, or general-3D wave validation.

## Frequency-grid authority

`FrequencyGridReconciliationAuthority` binds all of the following into the
composition semantic identity:

- original R130 wave frequency grid;
- original R150 GA frequency grid;
- requested/output composition grid;
- reconciliation method;
- interpolation domain and complex interpolation representation;
- extrapolation policy;
- wave and GA valid input bands;
- valid output band;
- frequency tolerance;
- algorithm identity and version.

The output grid remains exposed through the existing
`exact_frequency_grid_hz` field for backwards API compatibility, but its
meaning is now the exact requested/output grid. Its relationship to each input
grid is no longer implicit: it is fully described by the reconciliation
authority embedded in the composition spec and persisted output.

Changing the reconciliation rule or tolerance changes the authority hash, the
composition-spec identity, and the composed artifact identity even when the
underlying R130/R150 artifact identities are unchanged.

## Authorized complex interpolation

Two reconciliation modes are recognized:

1. `exact_bin_identity_v1`
   - no interpolation;
   - output bins must have exact input samples (subject only to the explicitly
     bound tolerance);
   - this is the default and preserves the PR #283 exact-bin behavior.

2. `cartesian_linear_v1`
   - piecewise-linear interpolation of real and imaginary components;
   - interpolation coordinate is linear frequency in Hz;
   - no extrapolation.

The chosen resampling authority for unequal grids is
`cartesian_linear_v1`.

### Why Cartesian real/imaginary

R160 is composing a phase-bearing complex transfer. Interpolating magnitude in
dB and phase independently is not an unconditional default because phase
unwrapping becomes ill-conditioned near complex zeros and can introduce an
authority-dependent branch choice. A requested magnitude/unwrapped-phase
method is therefore rejected as
`PHASE_INTERPOLATION_UNSUPPORTED` in this slice rather than silently guessed.

Cartesian real/imaginary interpolation avoids an independent phase-unwrapping
state and is deterministic for the bounded smooth-response subset validated
here. It does not imply that Cartesian interpolation is physically optimal for
every future broadband model.

### Linear frequency versus log frequency

Linear-frequency interpolation is used when the solver samples are interpreted
as samples of the complex transfer at explicit Hz coordinates and the bounded
interval is sufficiently resolved for piecewise-linear reconstruction.

Log-frequency interpolation can be appropriate for quantities whose intended
sampling/interpolation semantics are explicitly logarithmic (for example,
certain perceptual or octave-spaced parameterizations), but that is a separate
authority and is not silently substituted here.

No high-order spline is used. The implementation intentionally avoids
unconstrained overshoot.

## Extrapolation and validity

Default and only current extrapolation policy:

`forbidden`

The supported full-composition subset requires every requested output bin to
lie inside the intersection of the wave and GA valid input bands. A requested
output outside that common band fails closed as `OUT_OF_VALID_BAND`. Internal
reconciliation that would require stepping beyond an original grid is
`EXTRAPOLATION_REQUIRED`.

Unsupported bins are not filled with 0 Pa, zero energy, nearest neighbors, or
other fabricated values. Partial/clipped output is intentionally left for a
separate slice.

## Fixed bounded crossover authority

`HybridCrossoverConfigurationAuthority` binds:

- fixed overlap lower bound;
- fixed overlap upper bound;
- `linear_frequency_complementary_v1` blend law;
- wave validity band;
- GA validity band.

Automatic crossover optimization is not part of this slice.

For every supported output bin, both solvers are valid and the PR #283
complementary blend is retained:

`H = w_wave H_wave + w_ga H_ga`

with:

`w_wave + w_ga = 1`

The lower overlap boundary is wave-only, the upper overlap boundary is GA-only,
and the interior is a linear-frequency complementary crossfade. The same
physical response therefore does not double in the overlap.

## Failure semantics

The reconciliation layer defines typed failure codes:

- `INVALID_GRID`
- `NON_MONOTONIC_GRID`
- `DUPLICATE_FREQUENCY`
- `OUT_OF_VALID_BAND`
- `EXTRAPOLATION_REQUIRED`
- `PHASE_INTERPOLATION_UNSUPPORTED`
- `OVERLAP_INVALID`
- `INPUT_CAPABILITY_MISMATCH`
- `ARTIFACT_HASH_MISMATCH`

Capability failure remains fail-closed: a required magnitude-only/unsupported
R150 path produces an unsupported artifact with
`INPUT_CAPABILITY_MISMATCH`, not a fabricated complex response.

## Deterministic stitch fixture

Fixture:
`backend/tests/fixtures/r160_frequency_grid_stitch_fixture.json`

It contains two deterministic cases:

- unequal regular wave/GA grids;
- unequal irregular wave/GA grids.

Both sample the same affine, smooth, non-zero complex transfer. Because that
transfer is affine in Hz, piecewise-linear Cartesian interpolation has a known
exact expected value on every requested output bin. The focused test verifies:

- PR #283 exact-bin numerical behavior remains unchanged;
- deterministic unequal-grid reconstruction;
- regular and irregular input-grid cases;
- overlap-boundary behavior;
- no level discontinuity relative to the known transfer;
- no artificial wrapped phase jump in the fixture;
- complementary no-double-count weights;
- preserved common phasor/time-origin convention;
- out-of-band rejection;
- unsorted and duplicate grid rejection;
- unsupported phase-interpolation authority rejection;
- overlap rejection;
- reconciliation-config hash separation;
- save/reopen reproduction of reconciliation and crossover provenance;
- existing duplicate-path protection;
- fail-closed R130/R150 capability/hash handling.

This is synthetic numerical stitch evidence only.

## Persistence

The composed artifact embeds the exact composition spec, reconciliation
authority, crossover authority, output grid, original R130/R150 authority
bindings, aggregate identity, and composed payload hash. Repository reopen
re-resolves the exact dependencies and recomposes the artifact; a changed spec
or changed reconciliation configuration cannot reuse the old composed payload.

## Current limitations

This slice intentionally leaves the following unresolved:

- production broadband accuracy;
- automatic/optimal crossover selection;
- log-frequency interpolation authority;
- magnitude/unwrapped-phase interpolation authority;
- partial/clipped output mode;
- late reverberation completeness;
- diffraction completeness;
- owned-room validation;
- general-3D wave validation.

## Actual synthetic stitch evidence

GitHub Actions `R160 Numerical Hybrid Composition` run #14 validated
implementation head `4987b4fe3d0f2b9186d6c9b804847b50f5469f00`:

- focused `backend/tests/test_cad_hybrid_numerical_composition.py`:
  14/14 test nodes passed, including both unequal regular and unequal irregular
  stitch fixture cases;
- existing `backend/tests/test_cad_hybrid_acoustic_result.py`:
  27/27 test nodes passed;
- scope/non-claim gate: passed;
- RDC usage: 0.

Run:
`https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/actions/runs/35511230763`

This evidence demonstrates deterministic reconstruction for the synthetic
affine complex fixture and the bounded authority contract only. It remains
**not production broadband validation**.

## Verification

Repository-native focused tests and the existing R160 GitHub Actions workflow
are the validation authority. No local Windows or RDC validation is used.
