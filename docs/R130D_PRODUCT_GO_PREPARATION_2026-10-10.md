# R130D packaged numerical execution: GO preparation

**Numerical analysis is qualified within the stated fixture. Product GO remains disabled.**

The installable backend now exposes `htdt-r130d status` and `htdt-r130d run`.
This command executes the pressure waveform, verifies its signed transfer,
and records the exact request, implementation, environment, evidence and
modal-asset hashes. It accepts a sealed fixed-fixture request, not arbitrary
CAD scenes. The GUI, generic CAD prediction dispatch and production
recommendation authority have not been connected to this new executor.

## Discrete finite-band qualification

The existing physical source is unchanged: Gaussian volume velocity centered
at 40 ms with sigma 4 ms, fixed source `(1.5,2,2)` and receiver `(2.5,2,2)`.
The rigid room is the exact 56 m³ roof `z=4-y/4` with x/y in `[0,4]`.
Every spatial mode, including the rigid constant mode, is evolved for the
complete 250 ms record. No source fitting, frequency mask, modal cutoff or
record taper is applied.

The accepted profile includes **41 discrete samples at integer Hz from 40
through 80**. This does not certify every intervening frequency. Requests for
40.5 Hz, other shapes, other source positions, material loss or product-GO
flags are rejected before execution.

The fixed-time study failed strict spatial trend and the P8 phase comparison
at the small response near 51 Hz. An h/dt study with independent P8/P9/P10
also failed strict trend and independent phase. Both records remain FAIL.
The final design was informed by these results and existing P3/P4/P5 r2
diagnostics; it is explicitly marked `informed_design=true`, not a blind
validation. Its protocol was committed and pushed before the joint runtime
evaluation. All acceptance limits remained unchanged.

The accepted schedule is `5*ceil(0.25*sqrt(3)*100*PPW)` midpoint steps:
6065 / 6930 / 7795 / 8665 / 9530 on PPW 28 / 32 / 36 / 40 / 44.
All four adjacent pairs pass 20% complex RMS / 25% maximum magnitude / 15°
maximum phase; all three metrics strictly decrease across all pairs.
Every one of the 41 samples is included.

At PPW44 the temporal control against the exact all-mode causal solution is
0.0752344% complex RMS / 0.219604% maximum magnitude / 0.295210° maximum phase
(limits 1% / 2% / 1°). Independently assembled MFEM P4/P5 r2 differs by
0.118890% / 1.308044% / 1.225169°. SEM44 versus MFEM P5 differs by
0.00654475% / 0.115007% / 0.103862°; both independent checks pass the
unchanged 3% / 5% / 3° limits. These comparisons use complete eigenbases.
Per-mode residual and mass norms are verified for all modes; the recorded
128-mode orthogonality check is a sample, not a full Gram-matrix proof.

Evidence: `benchmarks/acoustics/r130d_finite_band_grid_refined_evidence_2026-10-10.json`.
The two preceding failed studies have distinct evidence files. Reproduction:
`scripts/run_r130d_finite_band_grid_refined.py --exports <pinned MFEM exports>`.

## Instantaneous profile

The packaged `stabilized_instantaneous` profile retains the original q0,
eight-node physical functionals and weights, native clocks, pressure gradient
and rectangular observation. Its qualified samples remain 40 and 80 Hz.
It uses mesh-vanishing numerical viscosity with kappa=1 and retains all
modes. This changes the finite-mesh numerical method. The old PFFDTD record
remains `SELF_CONVERGENCE_FAILED`; the undamped point-source full-record
continuum limit remains `NOT_ESTABLISHED`.

## Execution contract and validation

JSON input forbids extra fields, duplicate keys, nonfinite numbers, fractional
PPW, unordered/duplicate frequencies and unsupported samples. Before any
output is published, the complete modal asset SHA must match bundled
evidence. Runtime transfers must match the published signed values; finite
band temporal fidelity and instantaneous waveform/DTFT consistency are also
checked. A new output directory is required; final results are published by
renaming a completed staging directory. Each run writes `summary.json`,
`waveform.csv`, `transfer.csv`, and hashed `verification.json`.

`htdt-r130d status --require-product-go` returns exit code 3 with
`product_go=false`. A successful numerical run returns 0 for numerical
execution only; its output retains `recommendation_gate=disabled`.
Input/evidence/cache/accuracy failures return nonzero and publish no success.

26 focused tests pass: independent ODE pressure integrals, Gaussian/legacy
qualification evidence, bound requests, malformed JSON, tampered cache,
actual metric rejection and the disabled physical GO gate. Installed-wheel
execution is additionally checked outside the research checkout for both
profiles. The full application/GUI/installer release gates have not been run.

## Remaining product acceptance

The user confirmed that owned-room measurements are not available.
Synthetic results cannot substitute for measured evidence. Existing O60
production rules require a preregistered owned-room campaign, independent
calibration and disjoint holdout candidates, repeatability, placement
sensitivity and objective-trend evidence, with exact prediction/measurement
authority bindings. External measured-benchmark validation is also pending.

The present fixture has rigid boundaries and fixed points. Actual rooms need
an applicable geometry/material/source model before their recordings can
validate this executor. Generic CAD input, nonrigid boundary impedance,
source/microphone calibration and the production dispatch/GUI connection
remain work; passing the fixed fixture does not qualify those features.
Whole-product release acceptance additionally requires the existing full
release profile, owned Windows/UX and physical-device evidence and any
applicable GPU solver gate. This PR remains Draft and Issue #53 remains open.
