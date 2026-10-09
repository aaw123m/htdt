# R130D #938: Original physical point q0, exact-roof FV — x-grid phase sensitivity (FAIL)

2026-10-09 JST; PR #1055 Draft. Actual full-wave diagnostic, executed **entirely locally with no GitHub Actions dispatch or new CI workflow**.

## Pre-registration and unchanged physical problem

[Original prospective plan](../benchmarks/acoustics/r130d_exact_roof_original_point_q0_x_grid_phase_plan_2026-10-09.json) was committed **before the four wave calculations**, commit `14f620e391a61217b71158cf22e14e290bf008b5`. There were no adaptive parameter choices after observing results.

For two native high-resolution spatial levels PPW40 and PPW44, original pinned PFFDTD `vox_out.h5`, `comms_out.h5`, `sim_consts.h5`, original eight-node unit q0 source and receiver, original full 250ms waveform duration, sound speed 343.2 m/s, rho 1.2 kg/m³, exact room `0<=x,y,z<=4; z+0.25y<=4`, and two signed 40/80 Hz `P_T/Q_T` bins are unchanged. Original upstream PFFDTD SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`. Native grid spacing `h`, native time step `Ts`, total original `Nt`, unit discrete temporal source `q[0]=1, q[n>0]=0`, no temporal/frequency taper and no Gaussian source.

The mathematical source **stays exactly at (1.5,2,2) m** and receiver exactly at **(2.5,2,2) m** in every case. The experimental numerical x-grid is translated by exactly `-h/4` or `+h/4` while native y/z coordinates, y-dependent planar roof, native h/Nt/Ts, sound speed, source and receiver physical coordinates and time integration scheme are all fixed. Cartesian x=0 and x=4 are flat physical walls; the cut-volume solver clips each translated x cell to those same walls. Original unshifted trilinear eight-node source and receiver stencils are **first reconstructed from their physical XYZ, then required to equal the SHA-pinned original upstream PFFDTD stencils and signed weights, including original constant and first moments**. After translation, interpolation is re-evaluated on the shifted grid using the same standard eight-node trilinear point functional, preserving exact physical point position, zero-order sum and all Cartesian first moments.

This test **does not keep the original numerical eight-node weights unchanged** after shifting nodes; doing so would move the *physical* interpolated point. Source and receiver *continuous mathematical positions* are unchanged; numerical weights necessarily vary with grid phase. Therefore this study isolates combined grid-alignment, flat x-wall cut-volume, and interpolatory coupling effects — it does **not** alone separate which of those contributions dominates.

The same experimental conservative rigid Neumann exact-roof mass FV (`M=diag(V_exact)`, `K=c² Σ face_open_area/h (e_i-e_j)(e_i-e_j)^T`) and the same undamped implicit Newmark beta1/4 full-wave q0 solver are used in all four cases as in the prior PPW28–44 no-shift controls. Every complete full-wave computation is real, with up to 123k cell unknowns and the original 250ms native waveform. Strict original frozen CG relative residual and discrete homogeneous energy checks remain operative. No production numerical kernels or external HDF5 files are modified.

## Complete prospective four-wave results

Original prior unshifted baseline is PPW40→44 40/80 Hz complex relative **0.872666**, max relative magnitude **3.207787**, max phase **170.011°**: FAIL.

| Native x shift / h | PPW40→44 2-bin complex relative | Max relative magnitude | Max phase | Original numeric acceptance |
|---:|---:|---:|---:|---|
| **−0.25** | **0.489743** | **2.145139** | 14.031° | **FAIL** |
| 0 (frozen baseline) | **0.872666** | **3.207787** | 170.011° | **FAIL** |
| **+0.25** | **0.414555** | 0.230028 | **176.960°** | **FAIL** |

Original frozen limits are complex ≤0.20, max magnitude ≤0.25, max phase ≤15°; BOTH bins counted. None of the three x-grid phases passes all limits. The shifted alternatives are therefore not evidence of an approved model.

At the **same** PPW, shifting the grid x while keeping the physical source/receiver fixed causes the following full-record complex response changes relative to the unshifted control:

| Native PPW | x-grid shift −h/4, within-grid relative complex change | x-grid shift +h/4, within-grid relative complex change |
|---:|---:|---:|
| 40 | **0.008500** | **0.349311** |
| 44 | **0.380597** | **0.012300** |

40 Hz signed real pressure transfer component (Pa/(m³/s)) illustrates the sensitivity:

| x shift | PPW40 real 40Hz | PPW44 real 40Hz |
|---:|---:|---:|
| −h/4 | +102.9765 | **+32.2671** |
| 0 | +101.3017 | **−23.4387** |
| +h/4 | +31.3702 | **−25.2338** |

For PPW44, translating only x by `-h/4` flips the signed 40Hz real response from negative to positive despite the exact same physical points, exact 56m³ room, unchanged oblique roof and same original q0. That is a numerical grid-phase sensitivity, not a changed physical source or waveform.

Actual smallest cut-cell *volume fractions* (relative to full h³) are:

| PPW | original unshifted | x shift −h/4 | x shift +h/4 |
|---:|---:|---:|---:|
| 40 | 0.0178712 | 0.0216168 | 0.00720560 |
| 44 | 0.000579494 | 0.00109314 | **0.0000658516** |

Critically, the PPW44 `+h/4` variant has an even smaller cutcell than the unshifted model, but its entire signed 40/80Hz relative change is only 0.01230, while the `−h/4` variant's minimum cell fraction is *larger* and the complex transfer changes by 0.38060. **The smallest cutcell volume alone cannot be a monotonic proxy for this failure.** This is not a causal demonstration that *all* sliver cells are harmless: x translation also changes many boundary faces, source and receiver interpolation fractions and the full system's eigenmode couplings.

All four shifted full 250ms integrations completed with **Jacobi-preconditioned CG maximum iterations 13/13/13/14**, maximum verified true relative linear residual ≤9.84e−11 and homogeneous modified-midpoint energy relative drift ≤1.06e−9. Physical volume is exactly 56m³ to rounding at every translated mesh, and source and receiver zeroth/first moments are preserved. Both frequency bins, all signed original and shifted spectral transfer real/imaginary components, every original input SHA-256, interpolation RMS radius and fractional grid coordinates, true solver and energy metrics, and all original acceptance FAIL outcomes appear in [the immutable signed evidence JSON](../benchmarks/acoustics/r130d_exact_roof_original_point_q0_x_grid_phase_evidence_2026-10-09.json).

## Implications

This is the original mathematical **point impulse** (not a smooth source), evaluated by the *experimental numerical solver*, not a rerun of original PFFDTD's explicit production-like kernel. The experiment falsifies a simplistic hypothesis that exact geometry, a symmetric energy-conserving Neumann matrix and implicit integration necessarily ensure grid-phase-invariant point-to-point signed 250ms transfer. It reinforces that source/receiver alignment, x-wall spatial mass/face distribution and coherent high-Q modal interaction must be separated. The significant source/receiver numerical-support changes are *legitimate consistent discretizations of the same continuous delta location* but are not identical original native operator records.

The next useful controlled test is a **same-M/K one-sided source-only vs receiver-only interpolation phase perturbation** (or discrete modal overlap sensitivity), prospectively frozen on the existing exact roof. A full-resolution PPW44 pair cannot be used to infer PPW8–12 canonical approval; further original point q0 multi-level, cross-solver, physical reference checks are still necessary. No spectral threshold, 250ms window or physical source was changed to claim convergence.

**Canonical original PFFDTD point q0 = SELF_CONVERGENCE_FAILED; PPW40–44 exact roof shifted point q0 experimental cases all FAIL; BRAS/owned-room physical NOT_VALIDATED; product NO_GO. PR #1055 remains Draft and Issue #938 OPEN. No new Actions.**
