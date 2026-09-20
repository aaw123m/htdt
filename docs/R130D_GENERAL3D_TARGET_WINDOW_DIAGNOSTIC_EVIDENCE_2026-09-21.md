# R130D general-3D target-window sampling diagnostic evidence — 2026-09-21

Issue: #101  
Draft PR: #295  
Predecessor: PR #286  
Evidence-producing commit: `321edaf6808217b8864f1d865b5450bb1655d08c`  
GitHub Actions run: `35540693380` / run #62  
Artifact ID: `10614827536`  
Artifact digest: `sha256:62a57fc08bb983ca4d5f5ead6ae86a8ab87819a255d75fe082c4a01570195b9f`  
Evidence semantic SHA-256: `d03582d4a56251d704d8a3b8db92b3cfa968bf22248e10cc4189d29dd6ece473`  
Evidence file SHA-256: `07a45df2c7678e1b55252c2e356fab7b75e21f1bbc49200f6a9407ceb0d788db`

## Result

The fixed PR #286 series was rerun without changing resolution, frequencies, duration, taper, normalization, or acceptance thresholds. The canonical PR #286 observable was reproduced exactly at all six levels. The independent target-window observation operator passed its analytic harmonic fixture, but the PFFDTD 8/10/12 PPW non-monotonicity remained after exact target-window clipping.

| gate | state |
| --- | --- |
| solver execution | `PASS` |
| canonical observable contract | `MATCH` |
| diagnostic observation operator | `PASS` |
| canonical MFEM self-convergence | `SELF_CONVERGENCE_FAILED` |
| aligned-diagnostic MFEM self-convergence | `SELF_CONVERGENCE_FAILED` |
| canonical PFFDTD self-convergence | `SELF_CONVERGENCE_FAILED` |
| aligned-diagnostic PFFDTD self-convergence | `SELF_CONVERGENCE_FAILED` |
| cross-solver eligibility | `CROSS_SOLVER_BLOCKED` |
| general-3D validation | `NOT_VALIDATED` |

The diagnostic therefore does not promote or reinterpret the canonical validation result.

## Frozen operator authority

The diagnostic plan was frozen before numerical execution:

- diagnostic id: `r130d-general3d-target-window-clipped-left-rectangle-2026-09-20`
- semantic SHA-256: `ff42a7e0c44ed4726ea34edfa2549d018d66a37181786df18bbd4cf59c61d0db`
- parent general-3D plan semantic SHA-256: `5c753073a88d6705ee2962aa387006d4c563f90f0249b722dcf309a8155f62ec`
- exact target interval: `[0,0.25 s)`
- exact frequency grid: 40 / 80 Hz
- phasor convention: `exp(-i*omega*t)`
- analysis kernel: `exp(+i*omega*t)`
- rectangular/no taper
- canonical left-rectangle DTFT retained; only the final native cell width is clipped to end exactly at target `T`.

Linear/trapezoidal endpoint reconstruction was not adopted because the established source is the discrete impulse `q[0]=1, q[n>0]=0`. Trapezoidal endpoint weighting would alter that discrete source area and mix a source-contract change into the window diagnosis. The clipped-left-rectangle operator instead preserves all native sample/kernel/source semantics except the final integration-cell width.

## Independent analytic fixture

The fixed complex-harmonic fixture used pressure `2.1+0.4i @ 53 Hz`, source `0.7-0.2i @ 17 Hz`, and non-integer `T/dt` values `0.007 / 0.0035 / 0.00175 / 0.000875 s`.

The existing native-window extractor agreed with the exact sampled-harmonic authority to about `2.4e-14 ... 4.4e-14` relative error. The target-window diagnostic error decreased strictly:

`0.9874835 -> 0.4710929 -> 0.2039917 -> 0.1021818`.

The predeclared finest-error bound was `< 0.11`, so the diagnostic observation operator fixture is `PASS`.

## Sampling authority

MFEM uses `dt=1/12000 s`, `N=3000`, so `N*dt=0.25 s` exactly at all three refinements. Canonical and aligned MFEM transfers therefore differ only at floating-point noise level.

| solver level | dt (s) | N | last sample (s) | N*dt (s) | N*dt - requested |
| --- | ---: | ---: | ---: | ---: | ---: |
| MFEM r1 | 0.0000833333333333 | 3000 | 0.249916666667 | 0.250000000000 | 0 |
| MFEM r2 | 0.0000833333333333 | 3000 | 0.249916666667 | 0.250000000000 | 0 |
| MFEM r3 | 0.0000833333333333 | 3000 | 0.249916666667 | 0.250000000000 | 0 |
| PFFDTD 8 PPW | 0.000720966149 | 347 | 0.249454287433 | 0.250175253582 | +0.000175253582 |
| PFFDTD 10 PPW | 0.000576772919 | 434 | 0.249742673893 | 0.250319446811 | +0.000319446811 |
| PFFDTD 12 PPW | 0.000480644099 | 521 | 0.249934931532 | 0.250415575631 | +0.000415575631 |

For every level, the artifact records requested duration, native `dt`, generated sample count, first/last sample time, canonical and target effective intervals, endpoint convention, `N*dt`, duration difference, Fourier/phasor signs, rectangular weighting, source `Q_T` sampling, pressure `P_T` sampling, and exact frequency evaluation rule.

## Canonical reproduction and per-level window effect

The PR #286 canonical transfer was reproduced with maximum absolute complex-component error `0.0` for MFEM refinements 1/2/3 and PFFDTD 8/10/12 PPW.

The aligned-window change is effectively zero for MFEM. For PFFDTD, the canonical-to-aligned complex-RMS change per level is:

| PPW | canonical vs aligned complex-RMS | max magnitude-relative | max phase |
| ---: | ---: | ---: | ---: |
| 8 | 0.101572 | 0.176464 | 6.84272 deg |
| 10 | 0.0390809 | 0.169556 | 18.7778 deg |
| 12 | 0.345971 | 1.52003 | 26.6536 deg |

Thus the finite-window overrun is measurable and can materially move the finite-record transfer, especially at 12 PPW. That fact alone does not establish it as the convergence failure cause.

## Convergence diagnosis

MFEM is unchanged by alignment to numerical precision:

| pair | canonical RMS | aligned RMS | canonical max mag-rel | aligned max mag-rel | canonical max phase | aligned max phase |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 -> 2 | 0.994997 | 0.994997 | 0.889844 | 0.889844 | 85.8247 deg | 85.8247 deg |
| 2 -> 3 | 0.207043 | 0.207043 | 0.172103 | 0.172103 | 177.656 deg | 177.656 deg |

PFFDTD remains non-monotone:

| pair | canonical RMS | aligned RMS | canonical max mag-rel | aligned max mag-rel | canonical max phase | aligned max phase |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 -> 10 PPW | 0.876122 | 0.871173 | 0.803499 | 0.807052 | 75.4866 deg | 101.107 deg |
| 10 -> 12 PPW | 3.17143 | 3.18788 | 2.65256 | 3.07171 | 83.8040 deg | 91.6799 deg |

At 40 Hz, aligned complex-relative errors are `1.46096` for 8->10 and `1.21719` for 10->12; at 80 Hz they are `0.867701` and `3.40442`. The fine pair still worsens strongly at 80 Hz.

The predeclared complex-RMS worsening ratio changes from `3.6198468` canonical to `3.6592951` aligned. The worsening-excess reduction fraction is `-0.01505749`, i.e. the predeclared non-monotonicity measure becomes about **1.51% worse**, not better. The frozen classification is therefore:

`ALIGNED_NON_MONOTONICITY_REMAINS`.

## Interpretation and next slice

This evidence does **not** support the claim that level-dependent finite observation-window length is the principal cause of the PFFDTD non-monotone self-convergence. Exact target-window clipping changes individual PFFDTD transfer values, but it does not make the series monotone and does not substantially reduce the worsening excess.

Accordingly, this slice does not justify a formal change to the canonical observation contract. The canonical `CROSS_SOLVER_BLOCKED` and `NOT_VALIDATED` states remain authoritative. A subsequent separately predeclared slice should isolate other credible contributors, especially resolution-dependent PFFDTD voxel/staircase boundary geometry and modal/bin sensitivity. That must be a new experiment, not a post-hoc reinterpretation of this series.

## Reproducibility / artifact

Run #62 passed the focused regression suite (`32 passed, 1 warning`) and validation-runner compile check. Its artifact contains:

- `evidence.json`
- `frozen_general3d_validation_plan.json`
- `frozen_target_window_diagnostic_plan.json`
- `pr286_baseline_summary.json`
- `artifact_manifest.json`

The artifact byte hashes are independently recorded by its manifest; the semantic plan hashes are recorded by the evidence payload and frozen plan authority. A later non-numerical workflow edit only clarifies the manifest field names as file hashes versus semantic hashes and does not alter run #62 numerical evidence.

RDC calls: 0. HTDT-Capture diff: 0. Acceptance thresholds changed: no.
