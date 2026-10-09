# R130D #938 — Original native Cartesian eight-point q0 with true sloping-roof cut-domain consistent Q1 Galerkin wave solver: FULL five-grid FAIL

2026-10-09 JST. Experiment branch `feat/r130d-embedded-neumann-fv-20261009`, Draft PR #1055; Issue #938 OPEN.

## Strict advance experimental registration

A detailed prospective frozen real numeric 5-grid experiment plan was committed and **pushed before computing ANY original-grid Q1 q0 candidate transfer** as `a5d8598efb3af8690d0525c073be85c9e2a67b3a`, with `[skip ci]`. Previous same-input true physical Neumann roof conforming P1 consistent-mass FEM scores (including one isolated all-gate PASS at PPW32→36 but overall FAIL) were frozen in an earlier independent commit `a6cb3146774d911b62438bb27aef6201640bbcd2`. True P1 Dirac experiments `e2e089d` / `163ebdd` were also separately preregistered and observed prior to the current Q1 plan; the former source improvement hypothesis had failed. No result-driven adjustment of acceptance gates, source or record.

This trial tests a **different real spatial differential operator**: true weak Neumann Galerkin on the exact physical 56m³ wedge, with **Cartesian-native Q1 bilinear y-z elements** so the original eight-node Cartesian trilinear source and receiver are consistent with the same FE shape family. Unlike triangular conforming P1 elements, there is no selection of an arbitrary diagonal triangle for fixed physical source (1.5,2,2)m or receiver (2.5,2,2)m. Unlike FV on aperture-only mass stencils, mass and stiffness are integrated over the true roof-clipped physical domain with non-diagonal consistent basis mass. Unlike an artificial mode filter, ALL physically supported exterior/ghost Q1 nodes, ALL sliver modes and original pulse high-frequency modes are kept.

## Physical mathematical scheme implemented

Actual source input *unchanged*:
- Upstream original PFFDTD SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`, verified original real native HDF5 voxel, original **8 source and 8 receiver node indices AND original per-node weights**, original `q[0]=1; q[n>0]=0`, original sum of native input source kick `l2/h`, original PPW28/32/36/40/44 h/Ts/Nt and original 250 ms full time/pressure samples, signed original both **40Hz and 80Hz** P_T/Q_T, rho=1.2kg/m³ and c=343.2m/s.
- Original physically separated source (1.5,2,2)m / receiver (2.5,2,2)m **unchanged**, original Cartesian bilinear/trilinear weights **unchanged** and mapped to exactly the same native Cartesian nodes in the Q1 basis. **No source fitting, new amplitude scale, point movement, smoothing, Gaussian, taper, high-mode deletion, window shortening, or pressure/sign mask.**
- Original complete acceptance gates **0.20 complex relative RMS**, **0.25 maximum relative magnitude** and **15° maximum phase**, ALL simultaneously and on **every adjacent refinement pair**, remain frozen.

Exact mathematical Q1 spatial discretization:
1. In x, preserve native physical Cartesian x nodes plus true physical x=0 and x=4 endpoints; exact P1 line elements with **consistent** (not lumped) mass `M_e=dx/6[[2,1],[1,2]]` and standard natural Neumann stiffness.
2. In y,z, retain original native Cartesian rectangle-node **tensor Q1 bilinear** shape functions. Clip EACH true native rectangle against physical `0≤y≤4`, `0≤z≤4-.25y`. The physical intersection is a polygon; its subdivision into triangles is ONLY for **exact integration**, not a change to the original native bilinear shape basis.
3. On each physically clipped polygon, integrate the full **consistent off-diagonal** `M_ab=∫N_a N_b` (polynomial total degree ≤4) and `K_ab=c²∫gradN_a·gradN_b` (degree ≤2), using the strictly positive symmetric **six-point degree-4** triangle quadrature. These integrals are mathematically exact for Q1 basis products. External native y,z nodes whose Q1 support intersects ANY positive physical polygon are **kept** as actual DOFs; only identically zero physical support functions have no physical unknown. No physical sliver is merged/dropped or given an artificial mass floor.
4. True full physical tensor 3D `M=Mx⊗Myz_Q1`, `K=Kx⊗Myz_Q1+Mx⊗Kyz_Q1`. Natural Neumann zero-flux on true roof and remaining 5 room walls, symmetric mass and stiffness and true constant null. All TRUE generalized eigenpairs `K v = lambda M v` with strict <2e-7 *actual physical residual* and <5e-8 M-orthogonal Gram check, including all high-frequency/roof-support eigenmodes. Native original q0 signal uses prior exact original-clock beta=1/4 Newmark with precisely the original eight-source physical coupling and complete original 250ms signed 40/80Hz pressure observation; this is NOT original PFFDTD's native leapfrog.
5. All simulated native meshes independently pass the physical volume checks `true ∫dy dz = 14m²`, `true ∫dV=56m³` and manufactured affine field `u=x+z`: **`u^T M u=2780/3`** over exact true physical room and **`u^T K u=112*c²`**. The analytic physical rigid Neumann constant is reproduced exactly; all other modes untouched.

**Numerical precision issue transparently retained:** a generic SPD-generalized dense eigensolver on PPW28 true Q1 thin roof shapes first returned the rigid Neumann eigenvalue `-1.0980887e-6 s^-2` due to floating cancellation against largest true physical generalized eigenvalue ~`1.5328745e12 s^-2`: relative `7.16e-19`. This is NOT a negative physical Neumann mode. The verified **analytic Neumann constant mass-normalized vector** is substituted ONLY for this rigid zero mode, WITHOUT changing any physical K, M, source, high-mode eigenvectors, original input or acceptance gate. ALL nonconstant eigenvalues must be positive and EVERY generalized eigenpair must pass the original strict <2e-7 physical residual and Gram bounds; e.g. PPW28 true allmode maximum generalized residual **3.89e−10**. The original first Q1 failure diagnostic was not presented as a convergence result; this numerical corner case is explicitly documented.

## Actual original SHA point q0 all-mode results

| True native-grid PPW | Physical full Q1 3D modes | Physically wet exterior / ghost yz nodes KEPT | Full true high modes above original native Nyquist | Original native smallest positive cut rectangle area (m²) |
|---|---:|---:|---:|---:|
| 28 | 37,835 | 136 | 5,797 | 2.452e−6 |
| 32 | 53,001 | 152 | 5,016 | 6.730e−4 |
| 36 | 75,504 | 172 | 9,108 | 1.253e−4 |
| 40 | 102,949 | 192 | 11,438 | 2.122e−4 |
| 44 | 132,341 | 208 | 9,939 | 3.001e−4 |

**401,630 original-grid physically supported generalized 3D Cartesian Q1 modes**, none dropped. Actual true off-diagonal 3D M, K, physical manufactured mass/stiffness, exact Neumann zero and all modes numerically verified in independent tests.

Unmodified actual one-sample eightnode q0 250ms **SIGNED** 40/80Hz full two-bin self-refinement:

| Adjacent real native PPW | PREOBSERVED conforming P1 physical consistent-mass original 8node complex RMS | NEW exact physical Cartesian Q1 consistent Galerkin original 8node complex RMS | NEW max magnitude relative | NEW max phase | NEW all three original gates |
|---|---:|---:|---:|---:|---|
| 28→32 | 0.087346 | **2.449105** | **2.804982** | **178.591°** | **FAIL** |
| 32→36 | 0.015948 | **0.974794** | **0.315859** | **175.716°** | **FAIL** |
| 36→40 | 0.377469 | **0.523912** | **0.665377** | **23.589°** | **FAIL** |
| 40→44 | 0.498232 | **1.314187** | **11.659067** | **37.079°** | **FAIL** |

**All four Q1 adjacent grid refinements FAIL all-three-gate original acceptance, and complex, amplitude and phase errors do not monotonically decrease.** This is materially worse than even the full-domain triangular P1/consistent mass solver: physically exact Cartesian Q1 trial/point interpolation alone cannot cure the original broadband full-record singular q0 point transfer. In particular the PPW40→44 relative magnitude error is **11.659×** original baseline, not hidden or masked. The previously observed physically conforming P1 original 8node isolated 32→36 pass is lost, not used to claim overall convergence.

The signed 40Hz/80Hz real/imag P_T/Q_T on EACH grid, true Q1 exterior supported node counts, true above-Nyquist eigenmode counts, complete per-bin unfavorable magnitude/phase score, independent physical manufactured integration and original source/voxel SHA, complete real negative code/evidence are recorded in [the full evidence JSON](../benchmarks/acoustics/r130d_original_q0_exact_roof_cut_Q1_variational_evidence_2026-10-09.json). Implementation `backend/src/htdt/r130d_native_cut_roof_Q1_galerkin.py`, actual original SHA-hashed five-grid HDF5 runner `scripts/run_r130d_original_q0_true_roof_cut_Q1_galerkin.py`, strict manufactured invariant and full original negative score tests `backend/tests/test_r130d_original_q0_true_roof_cut_Q1_galerkin.py`, [prospective plan](../benchmarks/acoustics/r130d_original_q0_exact_roof_cut_Q1_variational_plan_2026-10-09.json).

## What remains and canonical release status

A source/space basis consistent true roof Q1 Galerkin method *also* fails original high-PPW full-q0 point observation self-convergence. The core unresolved physical/numerical target remains the **singular broadband point Green function observed at a distinct point in a fixed finite 250ms rectangle** and its nonmonotonic convergence under original native finite-width 8node source/receiver, with the true roof Neumann wall and original 40Hz/80Hz finite DFT both signed. Further mathematical investigation should separate full-spectrum point forcing/receiving errors and finite-window phase cancellation against an independently computed physical continuum reference; should not fit, attenuate or mask unfavorable true high modes or change original source and gate after results.

**Original upstream native PFFDTD remains SELF_CONVERGENCE_FAILED. Independent physical BRAS/MFEM/owned-room NOT_VALIDATED. Product NO_GO. PR #1055 remains Draft OPEN; Issue #938 OPEN.** No new upstream original PFFDTD waves, no manually triggered GitHub Actions, `scratch/` retained.
