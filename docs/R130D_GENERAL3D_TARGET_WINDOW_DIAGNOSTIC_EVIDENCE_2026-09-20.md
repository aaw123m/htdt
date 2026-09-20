# R130D general-3D target-window sampling diagnostic — evidence

Issue: #101  
Predecessor: PR #286  
Draft PR: #295  
Task-start main: `c77b8b50d7e4f9706c08e64a1daa7a7a51e8ed96`

## Authoritative numerical run

The authoritative numerical evidence for this diagnostic slice is GitHub Actions **R130D Independent General-3D Validation** run **#62**, run id `35540693380`, produced from commit `321edaf6808217b8864f1d865b5450bb1655d08c`.

Immutable artifact:

- artifact id: `10614827536`
- artifact name: `r130d-independent-general3d-validation`
- artifact ZIP digest: `sha256:62a57fc08bb983ca4d5f5ead6ae86a8ab87819a255d75fe082c4a01570195b9f`
- evidence semantic SHA-256: `d03582d4a56251d704d8a3b8db92b3cfa968bf22248e10cc4189d29dd6ece473`
- evidence file SHA-256: `07a45df2c7678e1b55252c2e356fab7b75e21f1bbc49200f6a9407ceb0d788db`
- artifact manifest file SHA-256: `54caed4c7c2a4a524e0fb9ec3072214dce1c02a5bb4b7900a3092eeecdd747a4`

The artifact contains `evidence.json`, the frozen general-3D validation plan, the frozen target-window diagnostic plan, the PR #286 baseline summary, and an artifact manifest. Post-run commits only clarified manifest field naming and persisted this run summary/evidence; they do not alter the numerical implementation, frozen plan, thresholds, refinement series, or run #62 result.

Persistent machine-readable summary:

- `benchmarks/acoustics/r130d_target_window_diagnostic_run62_summary.json`

## Frozen diagnostic authority

The diagnostic plan was fixed before the numerical run:

- diagnostic id: `r130d-general3d-target-window-clipped-left-rectangle-2026-09-20`
- diagnostic plan semantic SHA-256: `ff42a7e0c44ed4726ea34edfa2549d018d66a37181786df18bbd4cf59c61d0db`
- parent PR #286 plan semantic SHA-256: `5c753073a88d6705ee2962aa387006d4c563f90f0249b722dcf309a8155f62ec`
- MFEM refinements: 1 / 2 / 3
- PFFDTD: 8 / 10 / 12 PPW
- requested duration: 0.25 s
- frequencies: 40 / 80 Hz
- rectangular/no taper
- phasor convention: `exp(-i*omega*t)`
- analysis kernel: `exp(+i*omega*t)`
- PR #282/#286 acceptance thresholds and -50 dB mask: unchanged

The diagnostic operator is `htdt.r130d.target_window_clipped_left_rectangle@1`. It keeps the canonical direct-DTFT left-rectangle sample/kernel evaluation and clips only the final quadrature cell width so that integration ends exactly at `T=0.25 s`.

The recommended linear/trapezoidal reconstruction was not adopted because the established source authority is the discrete impulse `q[0]=1, q[n>0]=0`. Trapezoidal endpoint weighting would change that impulse area and confound the window diagnosis with a source-contract change. The frozen clipped-left-rectangle operator preserves the source impulse's full first-cell `dt` weight and changes only the final record cell.

## Independent observation-operator fixture

The frozen complex-harmonic fixture used pressure `2.1+0.4i` at 53 Hz and source `0.7-0.2i` at 17 Hz, evaluated at 40/80 Hz with non-integer `T/dt`.

| dt (s) | aligned target relative error | native-window analytic error |
|---:|---:|---:|
| 0.007 | 0.9874835055 | 4.4239e-14 |
| 0.0035 | 0.4710928879 | 3.2481e-14 |
| 0.00175 | 0.2039916538 | 2.4919e-14 |
| 0.000875 | 0.1021818032 | 2.4272e-14 |

The aligned error is strictly decreasing and the finest error is below the frozen `0.11` gate. The existing native-window extractor also matches its analytic sampled-harmonic authority at machine precision. Diagnostic observation-operator validation: **PASS**.

## Sampling authority

MFEM uses the same exact sample grid at every refinement:

| refinement | dt (s) | samples | first (s) | last (s) | N*dt (s) | N*dt - T |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 8.333333333e-5 | 3000 | 0 | 0.2499166667 | 0.25 | 0 |
| 2 | 8.333333333e-5 | 3000 | 0 | 0.2499166667 | 0.25 | 0 |
| 3 | 8.333333333e-5 | 3000 | 0 | 0.2499166667 | 0.25 | 0 |

PFFDTD native record coverage varies by PPW:

| PPW | dt (s) | samples | first (s) | last (s) | N*dt (s) | N*dt - T |
|---:|---:|---:|---:|---:|---:|---:|
| 8 | 0.0007209661487 | 347 | 0 | 0.2494542874 | 0.2501752536 | +0.0001752536 |
| 10 | 0.0005767729189 | 434 | 0 | 0.2497426739 | 0.2503194468 | +0.0003194468 |
| 12 | 0.0004806440991 | 521 | 0 | 0.2499349315 | 0.2504155756 | +0.0004155756 |

For every level the machine-readable evidence records requested duration, native `dt`, generated sample count, first/last sample time, effective canonical and target intervals, endpoint convention, exact `N*dt`, `N*dt-T`, Fourier/phasor sign, rectangular weighting rule, source `Q_T` sampling, pressure `P_T` sampling, and direct exact-frequency evaluation rule.

PFFDTD source sampling remains `q[0]=1, q[n>0]=0`. Pressure remains the existing recombined native receiver potential followed by the existing second-order `p=rho*d(phi)/dt` mapping. No interpolation, fitted amplitude, phase rotation, frequency shift, taper, or frequency masking was introduced.

## Canonical reproduction

All six canonical transfers reproduced the committed PR #286 baseline exactly: maximum absolute complex-component difference **0.0** for MFEM 1/2/3 and PFFDTD 8/10/12 PPW.

Thus the diagnostic path does not silently replace or perturb the canonical observable.

## Per-level canonical versus aligned output

At MFEM levels the target interval already equals `N*dt=0.25 s`; aligned outputs differ from canonical only at floating-point roundoff, with per-level canonical/aligned complex-RMS of approximately `4.0e-15`, `4.3e-16`, and `1.6e-15`.

PFFDTD changes materially when the final overrun cell is clipped:

| PPW | canonical 40 Hz | aligned 40 Hz | canonical 80 Hz | aligned 80 Hz | canonical/aligned complex-RMS |
|---:|---|---|---|---|---:|
| 8 | -34.9251 - 13.0082i | -27.2809 - 14.0632i | -34.7946 - 94.5736i | -27.3663 - 96.6639i | 0.1015720484 |
| 10 | -23.7918 + 34.0520i | -9.60339 + 33.1331i | 220.126 - 463.182i | 234.225 - 465.016i | 0.0390808588 |
| 12 | 15.4975 + 13.5372i | 50.2076 + 12.9696i | -69.7814 - 121.833i | -35.0852 - 122.969i | 0.3459706790 |

This shows that the finite-window difference is numerically material to individual PFFDTD transfer values, especially at 12 PPW, but contribution to *self-convergence monotonicity* must be judged from adjacent metrics rather than from per-level movement alone.

## MFEM convergence diagnosis

Canonical adjacent complex-RMS:

- refinement 1→2: `0.9949966914`
- refinement 2→3: `0.2070426700`

Aligned adjacent complex-RMS:

- refinement 1→2: `0.9949966914`
- refinement 2→3: `0.2070426700`

Frequency diagnostics are likewise unchanged to roundoff.

Canonical 1→2:
- 40 Hz: magnitude difference `3.7344699903`, relative magnitude `0.1823088924`, phase `27.44830564°`
- 80 Hz: magnitude difference `194.3352366320`, relative magnitude `0.8898438336`, phase `85.82474175°`

Canonical 2→3:
- 40 Hz: magnitude difference `4.2582639488`, relative magnitude `0.1721027784`, phase `177.65555729°`
- 80 Hz: magnitude difference `0.6231616435`, relative magnitude `0.0028615672`, phase `0.98728045°`

Aligned values differ only in floating-point noise. Canonical and aligned MFEM self-convergence both remain **SELF_CONVERGENCE_FAILED**.

## PFFDTD convergence diagnosis

Canonical adjacent metrics:

| pair | complex-RMS | max relative magnitude | max phase |
|---|---:|---:|---:|
| 8→10 | 0.8761219168 | 0.8034993027 | 75.48664328° |
| 10→12 | 3.1714271153 | 2.6525614377 | 83.80402351° |

Aligned diagnostic adjacent metrics:

| pair | complex-RMS | max relative magnitude | max phase |
|---|---:|---:|---:|
| 8→10 | 0.8711728980 | 0.8070519147 | 101.10711286° |
| 10→12 | 3.1878787270 | 3.0717093583 | 91.67990931° |

Per-frequency canonical diagnostics:

| pair | Hz | magnitude difference | relative magnitude | phase |
|---|---:|---:|---:|---:|
| 8→10 | 40 | 4.2711546295 | 0.1028199425 | 75.48664328° |
| 8→10 | 80 | 412.0573480869 | 0.8034993027 | 45.61834000° |
| 10→12 | 40 | 20.9627360035 | 1.0187260630 | 83.80402351° |
| 10→12 | 80 | 372.4260758018 | 2.6525614377 | 55.22163518° |

Per-frequency aligned diagnostics:

| pair | Hz | magnitude difference | relative magnitude | phase |
|---|---:|---:|---:|---:|
| 8→10 | 40 | 3.8044046026 | 0.1102830227 | 101.10711286° |
| 8→10 | 80 | 420.2109170481 | 0.8070519147 | 42.54131904° |
| 10→12 | 40 | 17.3589430441 | 0.3347548909 | 91.67990931° |
| 10→12 | 80 | 392.7979473439 | 3.0717093583 | 42.65848713° |

The predeclared non-monotonicity classifier gives:

- canonical worsening ratio: `3.6198467981`
- aligned worsening ratio: `3.6592951114`
- canonical worsening excess: `2.6198467981`
- aligned worsening excess: `2.6592951114`
- worsening-excess reduction fraction: `-0.0150574886`

Classification: **ALIGNED_NON_MONOTONICITY_REMAINS**.

The aligned operator does **not** make the series monotone and does not substantially reduce the non-monotonicity. Under the frozen diagnostic, the worsening-excess measure is approximately **1.51% worse**, not better. Therefore the PPW-dependent `N*dt-T` mismatch is not supported as the principal cause of the observed PFFDTD non-monotone self-convergence.

## Decision semantics

Separated result:

- solver execution: **PASS**
- canonical observable contract: **MATCH**
- diagnostic observation operator validation: **PASS**
- canonical MFEM self-convergence: **SELF_CONVERGENCE_FAILED**
- aligned-diagnostic MFEM self-convergence: **SELF_CONVERGENCE_FAILED**
- canonical PFFDTD self-convergence: **SELF_CONVERGENCE_FAILED**
- aligned-diagnostic PFFDTD self-convergence: **SELF_CONVERGENCE_FAILED**
- cross-solver eligibility: **CROSS_SOLVER_BLOCKED**
- general-3D validation: **NOT_VALIDATED**

The diagnostic result does not establish a defect requiring formal replacement of the canonical observation contract, so this slice recommends **no canonical observation-contract change**.

A later separately frozen diagnostic can investigate PFFDTD spatial discretization effects—particularly voxel/staircase boundary representation and mode/bin sensitivity—without reinterpreting the failed canonical series.

## Verification and scope

Run #62 focused validation: **32 passed, 1 warning**; validation runner compile-check: **PASS**.

No acceptance threshold, frequency, mask, amplitude scale, phase rotation, frequency shift, source normalization, PPW level, MFEM refinement, solver physics, or unrelated R-series implementation was changed.

- HTDT-Capture diff: **0**
- R100B/R140/R150/R160/R170 changes: **0**
- shared roadmap/status docs: unchanged
- RDC calls: **0**
