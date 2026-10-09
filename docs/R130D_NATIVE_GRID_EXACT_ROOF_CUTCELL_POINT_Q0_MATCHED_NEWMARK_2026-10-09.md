# R130D Issue #938: Actual exact-roof mass FV numerical solver and time-matched original staircase A/B — still FAIL

2026-10-09 JST, PR #1055 **Draft**. Actual five-grid native time-dependent numerical recalculation, **no GitHub Actions started or workflow added**. All source/receiver points and exact original discrete temporal q0 remain unchanged. This is an experimental **spatial + temporal numerical solver** variant and must never be misrepresented as passing the original PFFDTD eight-node canonical approval or physical BRAS reference.

## Preregistered, BEFORE numerical observation

* Exact native-grid room geometry / conservative mass-FV / implicit Newmark point q0 plan [r130d_native_grid_exact_roof_mass_fv_q0_plan_2026-10-09.json](../benchmarks/acoustics/r130d_native_grid_exact_roof_mass_fv_q0_plan_2026-10-09.json) committed `4e778179057b0330321b02a81bf6a3d6def9e2aa`.
* **Crucial causal time-matched control**, the original PFFDTD staircase rigid Neumann graph under exactly the *same* implicit Newmark beta=1/4 and original point q0 sample timeline, plan [r130d_original_point_q0_matched_newmark_spatial_ab_plan_2026-10-09.json](../benchmarks/acoustics/r130d_original_point_q0_matched_newmark_spatial_ab_plan_2026-10-09.json) committed `ad1d13438ca4516ec6589200b29e7d4a365b9eb7` before computing its results.
* All five independent original PFFDTD PPW28/32/36/40/44 native-grid HDF5 **comms/vox/sim_consts and untouched original raw 8-channel wave** have per-case SHA-256 pinned to previously saved actual original PFFDTD wave evidence. Original pinned upstream `aa319f6c86517cb95aabfae8656277da62c3ead5`. Original frozen 40 Hz and 80 Hz, entire 0.25 s no-taper record, room `[0,4]^3 ∩ {z+0.25y ≤ 4}`, sound speed 343.2 m/s, density 1.2 kg/m³, point source (1.5,2,2), receiver (2.5,2,2), and eight original PFFDTD native source/receiver trilinear weights.

## New numerical spatial and temporal algorithm

[The new isolated solver](../backend/src/htdt/r130d_native_grid_exact_roof_fv.py) consumes the exact **original PFFDTD grid coordinates**, not a retuned grid. For each original Cartesian node, its dual Voronoi cube is clipped against all six exact physical cube faces and the oblique roof. In each cell, the fluid volume is the **exact** two-dimensional clipped yz polygon area times the exact intersected x length; neighbor open-face areas are separately determined by the exact roof intersection on each x, y or z common face.

A conservative rigid Neumann semidiscretization is assembled using `M=diag(V_cell)`, `K=c² Σ(A_face/h)(e_i−e_j)(e_i−e_j)^T`. No arbitrary staircasing, fictitious normal-flux edges, sliver-cell mass flooring, source Gaussian or fitted frequency correction. **All five grid sums of exact cut-cell masses equal physical 56 m³ to ~1e−13 m³.** The original native eight-node source/receiver indices and weights are reused unchanged. The original *discrete temporal q[0]=1, q[n>0]=0* is injected at original sample zero through `c² M⁻¹ S` with source node weights summing to 1. No taper. Pressure recovered by the exact original second-order `p=rho*d(phi)/dt`, signed complex `P_T/Q_T` computed on the same full original native time record.

Because exact roof creates arbitrarily small positive mass slivers, an **implicit, energy-conserving Newmark beta=1/4** second-order wave update solves `(M+dt²K/4) u_{n+1} = (2M−dt²K/2)u_n −(M+dt²K/4)u_{n−1}+dt²c²S q[n]` with preconditioned sparse conjugate gradients. This differs from upstream original explicit Leapfrog; do not claim a purely geometry-only change when comparing with original native explicit results. Every CG true relative residual ≤8.88e−11, every maximum iteration count =13, well within frozen max 500 and true residual ceiling 5e−10. Undriven modified midpoint discrete energy is invariant to maximum ~1.18e−9 relative drift, well within preregistered 1e−6. No material damping or hidden numerical dissipation. All five entire original 0.25-second experimental wave computations were executed locally: about 31,713 to 123,032 active dual cells, with the original time step and counts.

## First numerical solver attempt vs frozen original PFFDTD

| Adjacent native PPW | Frozen original PFFDTD explicit native 8-node q0 complex error | New exactly cut-volume FV + Newmark q0 complex error | Exact-roof max rel magnitude | Exact-roof max phase |
|---|---:|---:|---:|---:|
| 28→32 | 1.246927 | **0.052369** | 0.086718 | 1.598° |
| 32→36 | 0.761305 | **0.218508** | 0.442811 | 7.561° |
| 36→40 | 0.367367 | **0.106168** | 0.178962 | 5.173° |
| 40→44 | 0.958743 | **0.872666** | **3.207787** | **170.011°** |

**FAIL**: original frozen numerical thresholds require complex RMS ≤0.20, maximum relative magnitude ≤0.25, maximum phase ≤15°, plus strictly decreasing errors across refinement. In this experimental variant, pair 32→36 fails and **last PPW40→44 badly fails**. The point q0 problem is **not solved** by exact sloped-room geometry and Newmark update.

The smallest exact cut-cell mass fractions vary strongly by grid: PPW28 = 0.0227805, 32 = 0.0012549, 36 = 0.0829309, 40 = 0.0178712, 44 = 0.0005795 of full original node volume. All are retained without capping and integrated stably with the implicit method. This is evidence that the new algorithm prevents numerical cut-cell CFL instability, **not** continuum convergence.

[Full first actual five-grid wave signed complex and CG/energy evidence](../benchmarks/acoustics/r130d_native_grid_exact_roof_mass_fv_q0_evidence_2026-10-09.json) retains all original source and geometry SHA-256, original full-spectrum controls, new 40/80 Hz real+imag finite-record scores, complete four error pairs including unfavorable magnitude/phase, physical volumes/minimum sliver volumes, true CG linear solver stats, signed discrete-energy probes, and explicit non-promotion state.

## Matched-time spatial ablation — isolate geometry vs time method

To avoid posthoc claiming the spatial correction alone caused the initial large improvements, a second **prospectively frozen** five-grid independent full-time numerical control was executed. For each exact original physical point source and native q0 record, only the **spatial** matrices differ:

* **Control:** original SHA-pinned PFFDTD staircase rigid graph `A=degree-adjacency`, full original flat nodal mass `M=h³ I` and stiffness `K=c² h A`; source/receiver and native time steps as original; *same* Newmark beta1/4, exactly the same verified CG implementation, same pressure derivative and original 250ms analysis. This control is **not** the native PFFDTD explicit Leapfrog waveform.
* **Treatment:** exact sloped-room cutcell masses and face apertures as above, same Newmark solver and exact original source/receiver/q0/native clocks.

| PPW pair | Original staircase **+same Newmark** complex error | True exact-roof cutcell **+same Newmark** complex error |
|---|---:|---:|
| 28→32 | 1.015149 | **0.052369** |
| 32→36 | 0.586163 | **0.218508** |
| 36→40 | 0.654502 | **0.106168** |
| 40→44 | 0.946197 | **0.872666** |

This **demonstrates a positive spatial correction effect at all four adjacent pairs with time integration fixed**, but not sufficient for actual convergence. The original PFFDTD staircase discrete room volume is nonmonotonic, unlike the exact cutcell 56 m³:

| Original PPW | Original staircase effective room volume, m³ | Exact-cutcell volume, m³ | Relative same-Newmark complex difference between the two SPATIAL schemes at that grid |
|---:|---:|---:|---:|
| 28 | 57.42665 | 56.00000 | 1.11469 |
| 32 | 55.09357 | 56.00000 | 0.63897 |
| 36 | 56.18636 | 56.00000 | 0.30939 |
| 40 | 56.67162 | 56.00000 | 0.63441 |
| 44 | 55.39873 | 56.00000 | 0.57444 |

Even when original source and temporal discretization are identical in both A/B arms, the **spatial mask/geometry causes large differences**. However, nonmonotonic new PPW40→44 proves that correcting the *global volume* and roof face apertures is insufficient to obtain convergence of the signed original full-record point impulse. The remaining causes may include singular point-source high modes, mass-discretization effects in tiny cells and residual finite-time modal interference. These hypotheses are not yet separated or proven unique.

The [full matched-time physical point A/B actual wave evidence](../benchmarks/acoustics/r130d_original_point_q0_matched_newmark_spatial_ab_evidence_2026-10-09.json) retains original HDF5 SHA, native physical volume for both arms, all ten complex 40/80 values, exact four original frozen comparisons, cross-arm complex differences, all CG true linear residual and homogeneous discrete-energy probes and unchanged failed qualification.

## Reproduction, tests, deployment boundary

- Actual experiments: `scripts/run_r130d_native_exact_roof_fv_q0.py` and `scripts/run_r130d_matched_newmark_spatial_ab_q0.py`. Require external prior exact SHA-256-pinned original native PPW28–44 HDF5 setups; no fake CI recreation or unobserved HDF5.
- Regressions: `backend/tests/test_r130d_native_grid_exact_roof_fv_q0.py`, `backend/tests/test_r130d_native_grid_exact_roof_fv_q0_evidence.py`, `backend/tests/test_r130d_matched_newmark_spatial_ab_q0.py`. They verify exact physical volume for flat and sloped control rooms, symmetric conservative K, all positive cell masses and face areas, source/receiver fail-closed, true 8-node original wave provenance, no numerical acceptance mutation, all 5 actual driven wave fingerprints and 4 unfavorable adjacent scores, source-free energy and CG behavior. No GitHub Actions workflow is added or triggered.
- Further genuine numerical changes must preserve original original q0 point source, prove convergence on **held-out PPW levels and an independent physical/reference discretization**, control the high-frequency point singularity without silently changing the source, and then rerun canonical PFFDTD PPW8–12 with unchanged frozen approval rules.

**FINAL: original eight-node R130D broadband point q0 PFFDTD = SELF_CONVERGENCE_FAILED. Original experimental 27-node Q2 point = FAIL. New exact roof + Newmark experiment = FAIL even though spatial A/B yields improvements. Owned-room/BRAS physical = NOT_VALIDATED, product = NO_GO. PR #1055 Draft and Issue #938 OPEN.** No production upgrade or issue closure.
