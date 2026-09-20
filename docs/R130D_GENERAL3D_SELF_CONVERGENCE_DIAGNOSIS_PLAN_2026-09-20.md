# R130D general-3D self-convergence diagnosis / frozen rerun plan

Issue: #101  
Predecessor: PR #282  
Task-start main: `1fb07fa210aae55d91c7d86b3f4c5ee993be7c93`

This document is committed before the new numerical run. The refinement schedule and acceptance thresholds below are frozen before evidence is observed. PASS is not a target condition; PASS, FAIL, or resource-blocked are all acceptable outcomes if the evidence is reproducible.

## PR #282 diagnosis

PR #282 executed the same bounded sloped R120B polyhedron in independent MFEM H1 tetrahedral and PFFDTD CPU lanes, but the final adjacent pairs failed self-convergence:

- MFEM refinement 1 -> 2 complex-RMS relative: `0.7958360401033702`.
- PFFDTD 8 -> 10 PPW complex-RMS relative: `1.0100111028102499`.
- Cross-solver acceptance therefore remained BLOCKED and general-3D remained NOT_VALIDATED.

The physical observable contract is retained: finite-record complex acoustic pressure per volume velocity, `P_T(f)/Q_T(f)`, unit `Pa/(m3/s)`, phasor convention `exp(-i*omega*t)`, analysis kernel `exp(+i*omega*t)`, rigid boundary, density `1.2 kg/m3`, sound speed `343.2 m/s`, exact sloped polyhedron, source `(1.5,2,2) m`, receiver `(2.5,2,2) m`.

The source normalization also matches physically. PFFDTD internally computes transfer from a unit discrete volume-velocity impulse and later applies the exact AcousticWaveExcitationAuthority Q(f); the R130D validator divides by that same Q(f). MFEM uses the corresponding unit discrete volume-velocity impulse through `phi_t(0+)=c^2 dt M^-1 b` and reports `rho r^T phi_t`.

Source/receiver numerical representation is different by design but not a physical-contract mismatch. PFFDTD upstream `sim_comms.get_linear_interp_weights` uses eight-node trilinear interpolation for both source and receiver. MFEM uses a continuous H1 point functional located by `Mesh::FindPoints`. The receiver lies on an internal tetrahedral face in the base partition; MFEM's delta LinearForm selects one containing element, and H1 continuity makes the physical point functional single-valued. The rerun must persist both representation descriptors rather than silently treating them as identical algorithms.

The most actionable observable issue in PR #282 is the 60 ms rectangular record: 40 Hz and 80 Hz occupy 2.4 and 4.8 cycles, so neither scored frequency is coherent with the finite-record window. In a lossless rigid enclosure, refinement moves modal frequencies; rectangular-window leakage from those shifted modes can dominate a two-bin transfer comparison even when the underlying spatial discretization improves. The rerun therefore changes the predeclared observable window to 250 ms, exactly 10 cycles at 40 Hz and 20 cycles at 80 Hz. No taper, fitted scale, phase rotation, frequency shift, or post-hoc normalization is introduced.

## Frozen bounded refinement plan

### MFEM

CI/full evidence series for this slice:

| level | uniform refinement | expected tetrahedra | expected order | expected DOF |
| --- | ---: | ---: | ---: | ---: |
| coarse | 1 | 48 | 2 | 125 |
| medium | 2 | 384 | 2 | 729 |
| fine | 3 | 3072 | 2 | 4913 |

The level-3 DOF expectation follows the observed structured H1-order-2 sequence for this fixed six-tetra body-diagonal mesh. The validator must fail closed if actual element count or DOF differs.

Pre-run dense-modal working-set estimate for 4913 DOF using the validator's conservative `6*n^2*8` model is about 1.08 GiB before LAPACK/runtime overhead. The workflow ceiling is therefore 6 GiB checkpoint RSS and 900 s per reference level. If this GitHub-hosted Windows runner cannot execute the level within those declared bounds, the outcome is resource-blocked; no smaller substitute level is allowed after results are seen.

### PFFDTD

Frozen series: 8 / 10 / 12 PPW at `fmax=100 Hz`.

Expected deterministic planning values before execution:

| PPW | nominal spacing |
| ---: | ---: |
| 8 | 0.429 m |
| 10 | 0.3432 m |
| 12 | 0.286 m |

The exact grid dimensions, active/boundary cell counts, time step, CFL information, interpolation weights/indices where available, time-step count, runtime, and resource estimate must be persisted from actual execution. No GPU is assumed.

### Observable / window

- record interval: `[0,T)`
- `T = 0.25 s`
- rectangular window, no taper
- scored bins: 40 Hz and 80 Hz
- both bins are coherent with T
- same exact source/receiver physical coordinates and exact geometry authority at every level
- same density, sound speed, rigid boundary, phasor convention, Fourier sign, and P/Q normalization at every level

### Acceptance thresholds

Thresholds are unchanged from PR #282:

- MFEM self-convergence: complex RMS <= 0.05, max magnitude-relative <= 0.08, max phase <= 5 deg.
- PFFDTD self-convergence: complex RMS <= 0.20, max magnitude-relative <= 0.25, max phase <= 15 deg.
- Cross-solver, only if both self-convergence gates pass: complex RMS <= 0.35, max magnitude-relative <= 0.40, max magnitude <= 3 dB, max phase <= 25 deg.
- magnitude mask: -50 dB relative to the reference maximum.

Self-convergence requires the final adjacent pair to meet thresholds and the adjacent-pair error trend to decrease toward the fine level. Cross-solver evaluation is ineligible if either solver fails, is missing a level, is resource-blocked, or has a physical-observable contract mismatch.

## Required failure semantics

Evidence distinguishes:

- `EXECUTION_FAILED`
- `CONTRACT_MISMATCH`
- `SELF_CONVERGENCE_FAILED`
- `SELF_CONVERGENCE_PASS`
- `CROSS_SOLVER_BLOCKED`
- `CROSS_SOLVER_FAILED`
- `CROSS_SOLVER_PASS`
- `NOT_VALIDATED`
- `VALIDATED`

A successful workflow or a produced numerical vector is not a physics validation PASS.

## Scope

No production solver adoption claim is made. Concave, multi-region, Portal, GPU, and owned-room validation remain out of scope. HTDT-Capture is unchanged. RDC calls are zero.
