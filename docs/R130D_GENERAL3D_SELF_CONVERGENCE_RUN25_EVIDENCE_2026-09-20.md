# R130D general-3D self-convergence rerun evidence — 2026-09-20

Issue: #101  
Draft PR: #286  
Evidence-producing commit: `f9802da57f47d6fa64961014de509350092e49bf`  
GitHub Actions run: `35513920639` / run #25  
Immutable workflow artifact digest: `sha256:a6e1117b52d6827287dcc6d6339b251913822642158329da18c75b0455db4006`  
Evidence semantic SHA-256: `aab68a65a7d5c40e5477c71e9efd6898b0672febb9a8d74c7cd69b8973798e20`

## Result

The predeclared refinement plan completed. Execution succeeded, the physical observable contract audit matched, both solver self-convergence gates failed, cross-solver evaluation remained blocked, and the general-3D state remains `NOT_VALIDATED`.

| gate | state |
| --- | --- |
| execution | `PASS` |
| physical observable contract | `MATCH` |
| MFEM self-convergence | `SELF_CONVERGENCE_FAILED` |
| PFFDTD self-convergence | `SELF_CONVERGENCE_FAILED` |
| cross solver | `CROSS_SOLVER_BLOCKED` |
| general-3D validation | `NOT_VALIDATED` |

No cross-solver fine/fine metric was computed because both self-convergence prerequisites failed.

## Contract audit

Both paths use the exact sloped R120B fixture hash `b846084627eb266d174e8412b5737b8691bbc279ff21e066c48dc61ded1d72c3`, source `(1.5,2,2) m`, receiver `(2.5,2,2) m`, `rho=1.2 kg/m3`, `c=343.2 m/s`, rigid / natural homogeneous Neumann boundary semantics, pressure per volume velocity `Pa/(m3/s)`, phasor `exp(-i*omega*t)`, and analysis kernel `exp(+i*omega*t)`. No amplitude fit, phase rotation, or frequency shift is applied.

MFEM represents source and receiver by continuous-H1 point functionals assembled from `DeltaCoefficient`; upstream MFEM locates a single containing element with `Mesh::FindPoints`. PFFDTD represents the exact physical points with its native eight-node trilinear interpolation. The numerical representations differ, but the physical coordinates and observable authority are identical.

The requested record was changed from PR #282's 60 ms to the predeclared 250 ms rectangle. This makes 40/80 Hz integer-cycle frequencies with respect to requested continuous `T=0.25 s`. The actual PFFDTD sample counts, however, have `Nt*dt = 0.2501752536 / 0.2503194468 / 0.2504155756 s` for 8/10/12 PPW. The native DTFT consumes every sample whose timestamp lies in `[0,T)`; therefore discrete-bin coherence is not exact and varies slightly by level. This is now an explicit sampling/observable limitation, not hidden normalization.

## MFEM fixed refinement series

| ref | elements | DOF | characteristic h (m) | eigensolve (s) | checkpoint RSS (MiB) |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 48 | 125 | 3.201562 | 0.338765 | 264.15 |
| 2 | 384 | 729 | 1.600781 | 8.160381 | 292.27 |
| 3 | 3072 | 4913 | 0.800391 | 26.524319 | 1401.81 |

Adjacent metrics:

| pair | complex RMS | max magnitude relative | max magnitude dB | max phase |
| --- | ---: | ---: | ---: | ---: |
| 1 -> 2 | 0.994997 | 0.889844 | 19.1598 dB | 85.8247 deg |
| 2 -> 3 | 0.207043 | 0.172103 | 1.64047 dB | 177.656 deg |

Complex-RMS and magnitude errors decrease strongly, but the final pair still exceeds the fixed thresholds and the maximum phase error becomes nearly 180 degrees. At 80 Hz the 2->3 pair is already close (`complex_relative=0.01749`, phase `0.987 deg`), while 40 Hz changes sign/phase between refinements. That behavior is consistent with a comparison frequency lying near a mesh-dependent lossless-cavity modal pole, but the current evidence does not contain enough local eigenspectrum detail to claim that as proven.

## PFFDTD fixed refinement series

| PPW | h (m) | grid | cells | boundary nodes | dt (s) | Nt | solve (s) |
| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 8 | 0.4290 | 18x18x18 | 5832 | 808 | 0.0007209661 | 347 | 13.293590 |
| 10 | 0.3432 | 20x20x20 | 8000 | 1442 | 0.0005767729 | 434 | 0.073916 |
| 12 | 0.2860 | 22x22x22 | 10648 | 2018 | 0.0004806441 | 521 | 0.088416 |

The unusually high 8-PPW solve time is first-run Numba compilation/JIT overhead; compile and solve timing are persisted separately and are not used as convergence evidence. Every level used `c*dt/h = 0.5767729189`, i.e. 0.999 of the Cartesian 3-D CFL limit.

Adjacent metrics:

| pair | complex RMS | max magnitude relative | max magnitude dB | max phase |
| --- | ---: | ---: | ---: | ---: |
| 8 -> 10 PPW | 0.876122 | 0.803499 | 14.1327 dB | 75.4866 deg |
| 10 -> 12 PPW | 3.171427 | 2.652561 | 11.2520 dB | 83.8040 deg |

The PFFDTD sequence is not converging under this observable: complex RMS and magnitude-relative errors worsen at the fine pair and phase does not improve monotonically.

## Diagnosis

- **Normalization / convention:** no mismatch was found. The exact Q authority, pressure conversion, units, phasor sign, Fourier sign, density, sound speed and boundary semantics are explicitly checked. Arbitrary scale fitting is absent.
- **Geometry / units:** exact R120B geometry identity and SI coordinates match. MFEM refines an exact conforming tetrahedral body; PFFDTD necessarily re-voxelizes/staircases the exact polyhedron at each PPW. The latter changes boundary-node geometry with resolution and remains a credible contributor to the non-monotone PFFDTD response.
- **Source / receiver sampling:** physical positions match. MFEM H1 point evaluation and PFFDTD trilinear interpolation are distinct numerical sampling operators. There is no evidence of a gross location mismatch, but point-observable sensitivity near cavity modes can amplify their discretization differences.
- **Record / window:** extending to requested 250 ms did not establish self-convergence, so PR #282's short/non-integer-cycle 60 ms record was not the sole cause. Native PFFDTD sample counts also make exact discrete coherence level-dependent, which remains a measurable sampling limitation.
- **Spatial resolution:** MFEM shows substantial error reduction in complex RMS and magnitude but still fails because of the 40-Hz phase/sign behavior. PFFDTD does not show monotone improvement from 8/10/12 PPW.
- **Observable comparison:** a lossless rigid enclosure has sharp modal poles. Comparing unsmoothed point `P/Q` at only 40 and 80 Hz is highly sensitive when a numerical eigenfrequency moves across a scored bin. The actual results strongly expose that sensitivity; the evidence does not justify relaxing thresholds or moving bins after seeing the result.

## Resource outcome

The planned MFEM level 3 completed on the GitHub-hosted Windows runner: 4913 DOF, conservative dense working-set estimate 1,158,603,312 bytes, checkpoint RSS 1401.81 MiB. No OOM/resource block occurred. PFFDTD 12 PPW completed with 10,648 Cartesian cells and 521 time steps. GPU was not used.

## Final authority

This slice is a reproducible `FAIL`, not a validation PASS and not a resource-blocked result. The fixed plan has been executed without post-hoc parameter tuning. `CROSS_SOLVER_BLOCKED` and `NOT_VALIDATED` remain authoritative.

A subsequent slice should diagnose modal-bin sensitivity explicitly (for example by persisting local MFEM eigenfrequencies and a predeclared narrow frequency neighborhood, plus a discrete-record/window authority) before selecting any different comparison observable. That would be a new predeclared experiment, not a reinterpretation of this failed run.

RDC calls: 0. HTDT-Capture changes: none.
