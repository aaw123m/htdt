# R130D Issue #938 — All original PFFDTD point-q0 3D modes, exact roof cross-check and nonconvergence cause isolation

2026-10-09 JST, Draft PR #1055. **No GitHub Actions started or created.** The original native eight-point PFFDTD q[0]=1 temporal unit impulse, sloped rigid room, physical source (1.5,2,2), receiver (2.5,2,2), signed 40/80Hz whole original 0.25s finite P_T/Q_T are unmodified. This report does **not** claim convergence or physical validation.

## Prospective reproducible experiments

* Exact-roof conservative mass finite-volume (`backend/src/htdt/r130d_native_grid_exact_roof_fv.py`) x/yz Kronecker identity and first 384 true original-clock native q0 product modes precommitted as `cd21716edf15ca9d2e498f4e0ad7ca241484b532`, before any spectrum; [plan](../benchmarks/acoustics/r130d_native_exact_roof_xy_z_separable_modal_q0_plan_2026-10-09.json). First-384 modal signed pressure reconstruction and all signed high-mode omissions are in [actual partial evidence](../benchmarks/acoustics/r130d_native_exact_roof_xy_z_separable_modal_q0_evidence_2026-10-09.json).
* Full **untruncated** exact roof 3D eigenspectrum q0 finite-record source was preregistered as `68e7f34c3be19fd1e7fec290ec3da1ba2662007e`, [plan](../benchmarks/acoustics/r130d_native_exact_roof_full_xy_z_modal_finite_window_q0_plan_2026-10-09.json). The pre-observation analytic Fourier cosine-progression average was initially missing a factor one-half (corrected before execution as `95c128d...`). The first partial-384 **cross-check correctly rejected** an additional erroneous interior `cos(theta)` prefactor: observed diagnostic relative mismatch `0.0027646352`; analytical correction committed `92a505c...` **before re-executing or interpreting the full eigenbasis**. This arithmetic failure was **not hidden or accepted** by relaxing any threshold. The final closed form was independently validated against genuine direct 250ms time sampling; all eigenmodes sum to the untouched saved full wave at relative `1.7e-8`. The complete [actual signed full eigenmode evidence](../benchmarks/acoustics/r130d_native_exact_roof_full_xy_z_modal_q0_evidence_2026-10-09.json) preserves high-mode signed band contributions and the original failed PPW40→44 result.
* **DIRECT ORIGINAL PFFDTD all-mode experiment**, distinct from the above experimental new FV: original native graph mode plan committed as `ed059f92e17ac31c0f00dccea0bb8f92c19fc386` **BEFORE processing original grid spectra**, [prospective plan](../benchmarks/acoustics/r130d_original_pffdtd_native_full_kronecker_modal_q0_plan_2026-10-09.json). No Numba solver or native `vox_out.h5`/`comms_out.h5`/`sim_outs.h5` changes. The original SHA-pinned source-connected symmetric graph from the prior real native Neumann operator audit is used, not recreated from analytic roof cells. Independent complete ORIGINAL graph x/y-z separation and full native Leapfrog modal recurrence with original source q0 impulse and original time pressure derivative are evaluated at the identical complete original 250ms and both 40/80 bins. Complete [real ORIGINAL five-grid PFFDTD all-mode signed evidence](../benchmarks/acoustics/r130d_original_pffdtd_native_full_modal_q0_evidence_2026-10-09.json) is available, together with all raw voxel/source/output SHA-256.

## Exact 3D spatial matrix identity (experimental geometric correction)

For the exact planar roof cutcell FV, the physical room extrudes uniformly in x, so `M3 = Mx ⊗ Myz`, `K3 = Kx ⊗ Myz + Mx ⊗ Kyz`. Both **entire unmodified first experiment 3D M and K** at native PPW40/44 were assembled independently as Kronecker factors and compared **entrywise**, preserving original native ordered active nodes. Mass matrix absolute difference **0**, stiffness matrix absolute max `1.45519e-11` for both grids. Every true cutcell mass and roof aperture remains intact, total 56 m³. First 384 modes with full original physical source and output (x first12 × y-z first32) reproduced the original partial signed transfer but had relative truncation gaps `0.389252` (PPW40) and `0.252909` (PPW44) versus the saved complete real q0 wave.

Diagonalizing ALL original exact roof x and y-z generalized mass/stiffness modes, with **NO mode truncation**, and propagating the original discrete source analytically through exactly the native undamped Newmark `theta = 2atan(dt sqrt(lambda)/2)` and original p=rho*2nd-order dphi/dt, yields:
- PPW40: **91,415** 3D full actual modes; exact eigenspectrum vs full saved original 8/8 Newmark-CG signed two-bin P_T/Q_T relative `1.74669e-8`.
- PPW44: **123,032** full modes; relative `1.72944e-8`.
- Partial 384-mode direct-time transfer was also reconstructed by the new finite analytic transform to required precommitted `2e-9` relative. True full spectral bands [0,100), [100,200), [200,400), [400,800), [800,+∞) Hz show relative contribution to signed **PPW40→44** 2-bin spatial difference norm of `0.02407 / 0.02615 / 0.18768 / 0.34396 / 1.10091`. This **experiment** clearly has dominant original-q0 finite-record high-mode leakage from >800 Hz despite evaluating 40 and 80 Hz, but is not the original explicit native PFFDTD solver.

## THE ORIGINAL FROZEN NATIVE PFFDTD: exact all-mode full wave reproduction

The **real original** connected, voxel-staircase, seven-point rigid PFFDTD negative Laplacian was independently shown to be **exactly** `A3 = Ax ⊗ Iyz + Ix ⊗ Ayz` on **ALL FIVE original native PPW28/32/36/40/44 source-connected rooms**, with identical source-connected node ordering and **zero entrywise matrix difference**, no masked-off source nodes. This does not alter or smooth the original staircase boundary. The original source and receiver eight-point HDF5 spatial functionals also factor **exactly** into x and y-z, allowing the **complete Euclidean-orthonormal source/receiver coupling** and native source q0 total input amplitude for every mode.

All original x modes and ALL original y-z modes were diagonalized (no truncation). The original **explicit** Leapfrog modal angle `theta=2 asin(sqrt(l² lambda_3D)/2)`, original unmodified `in_sigs[:,0]` discrete q0 kick, original 8-node output, original native Ts and Nt, and exact 2nd-order endpoint-inclusive p derivative and finite rectangular 40/80 Hz P_T/Q_T were then fully reconstructed **analytically**, keeping high-frequency and Nyquist-adjacent modes.

| Original PPW | Original full native PFFDTD 3D eigenmodes included | Entire modal q0 vs previously saved original raw 8-node 250ms complex P_T/Q_T relative error |
|---:|---:|---:|
| 28 | 31,185 | **1.2322e−11** |
| 32 | 44,659 | **1.4850e−13** |
| 36 | 64,848 | **2.4816e−11** |
| 40 | 89,723 | **2.5254e−11** |
| 44 | 116,739 | **2.0508e−11** |

**Every full original native q0 wave control is reproduced to machine precision.** The original native PFFDTD code, physical input, source and receiver q0, wall physics and full window are unchanged. Therefore the following **band decompositions are now causally tied directly to the REAL original PFFDTD nonconvergence**, not merely to an analogous FV experiment. Ratio = `norm(signed(original coarse band H − original fine band H)) / norm(signed(original complete coarse H − original complete fine H))` across BOTH original 40/80Hz pressure bins.

| ORIGINAL PPW pair | 0–100 Hz | 100–200 Hz | 200–400 Hz | 400–800 Hz | ≥800 Hz |
|---|---:|---:|---:|---:|---:|
| 28→32 | **0.69091** | 0.10715 | 0.16175 | 0.16151 | 0.24257 |
| 32→36 | **0.78581** | 0.22604 | **0.79340** | 0.15702 | **0.54058** |
| 36→40 | **1.26272** | 0.13838 | **0.70652** | **0.50057** | **1.77104** |
| 40→44 | **0.69708** | 0.08500 | 0.26516 | 0.17887 | **0.73584** |

These are **norms of signed complex component differences**. They do NOT add to one; values >1 mean cancellation among different modal bands. All five original physical semidiscrete eigenfrequency bands are exhaustive; the sum of their signed complex pressure transfers reproduces **each and every original native PPW full-record transfer and all four adjacent original spatial differences** to the preregistered tolerances. No frequency masking, fitted gains, pulse taper, shortened record, new source, physically unjustified damping or retroactive acceptance gate.

## Crucial low-mode observation: why correcting roof geometry helps but is not sufficient

The FIRST nonzero original PFFDTD y-z staircase cross-section semidiscrete mode frequencies in Hz:
- PPW28: **41.31690**; PPW32 **42.07803**; PPW36 **41.74790**; PPW40 **41.54897**; PPW44 **42.03263** (nonmonotone).
- The original staircase x-direction first Neumann frequency varies (PPW40 **42.54527**; PPW44 **43.13043**) despite the constant physical x-length 4 m.
- In contrast the exact 56m³ roof volume/aperture FV y-z first mode is PPW40 **41.952477** vs PPW44 **41.953844** (drift only **0.001366 Hz**), and x first mode PPW40 **42.891844** vs PPW44 **42.893338** (drift only **0.001494 Hz**). Therefore **geometry correction sharply improves low eigenfrequency consistency**, consistent with earlier same-Newmark x/y/z spatial-only A/B improvements.

But the exact-roof FV original point q0 full signed PPW40→44 error still **0.872666** (FAIL), because the high-mode tail and original grid-phase/point observation remain. The real upstream native original PPW40→44 signed complex relative error remains **0.958742**, max magnitude `0.543991`, max phase `150.63°`; ALL 4 original high-PPW pairs also fail. The verified original numerical update does conserve energy, but the underlying original fullband singular point excitation and staircase spatial geometry lead to nonmonotonic signed original finite-window acoustic response.

## Engineering conclusions and remaining solution path

Evidence now isolates **TWO real unresolved numerical mechanisms in the ORIGINAL exact q0 solver**:
1. **Low-mode drift from staircase Neumann geometry**. The underlying original voxelized domain yields wrong/nonmonotone low Neumann frequencies; exact analytic roof mass/open-face FV nearly eliminates this drift, including nonmonotone cross-grid changes.
2. **Unresolved high-mode and point operator finite-window contamination**. The original rectangular 250ms pressure transfer at 40/80 Hz is materially affected by modes from 200–400 Hz and ≥800 Hz, including an ≥800 Hz contribution of **1.771×** the complete original PPW36→40 signed error. The signed mode bands cancel. At fixed exact-roof M/K, substituting point 8→27 Q2 source/receiver made the original point q0 high-grid complex error worse, not better. Nonlinear artificial damping, frequency masking, truncating 250ms, changing q0 input or retroactively altering the frozen pass thresholds are **not valid fixes**.

Next true candidate must enforce geometrically correct modal frequencies AND physically correct discrete mathematical point source/receiver high-frequency response with stable continuum refinement, then validate with an **independent reference** at held-out native PPW and run the frozen original canonical PPW8/10/12 fullband source without replacing it. The present results are an exact **root-cause decomposition** of the original finite native numerical wave, **not** a repaired convergent fullband model or a physical BRAS measurement.

**FINAL: Original eight-node PFFDTD q0 `SELF_CONVERGENCE_FAILED`; BRAS/owned-room physical `NOT_VALIDATED`; product `NO_GO`; Issue #938 OPEN; PR #1055 Draft; no new GitHub Actions.**
