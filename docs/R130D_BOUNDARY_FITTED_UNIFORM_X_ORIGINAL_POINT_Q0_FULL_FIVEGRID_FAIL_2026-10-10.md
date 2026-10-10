# R130D — deterministic physical boundary-fitted uniform x-P1 + true cut-roof positive Q1 (original q0, all 5 grids)

**Date 2026-10-10 JST. Conclusion: EXPERIMENTAL ONLY, NOT accepted.** Repository `aaw123m/htdt`; Issue #53, Draft PR #118.

## Frozen preregistration / honest change of discrete representation

The scientific plan was committed remotely [`61c0de9d664280b42cf16f97eb579d0d976dc741`](https://github.com/aaw123m/htdt/commit/61c0de9d664280b42cf16f97eb579d0d976dc741) **before** the new full five-grid run. The runner verifies the live GitHub branch, plan bytes, ancestral commit and original SHA of native HDF5. No original wave calculation, Github Actions or post hoc arm selection was performed.

Physical x [0,4] m is discretized into the **pre-registered** `N=ceil(4/h_original)` equal segments. Standard row-lumped physical P1 has positive `M_x[0,0]=M_x[N,N]=dx/2`, all interior `dx`, conservative Neumann `Kx`, total 4 m and exact affine x weak energy `∫c²(∂_x x)²dx = 4c²`. No x-end short-supported sliver survives. The y-z part remains the previous native full-wet Cartesian 5point true inclined physical Neumann cut-Q1 stiffness, with **all** original native positive cut-roof sliver basis DOFs and exact physical positive row-sum `Myz`; area14m² and room56m³. Full tensor `K=Kx ⊗ Myz + Mx ⊗ Kyz`, `M=Mx ⊗ Myz`, all new 3D eigenmodes kept, original Neumann constant. Original roof tangent `u=y-0.25z` satisfies independent weak residual.

**Important model honesty:** The numerical x DOF positions change relative to original PFFDTD. The original SHA-pinned *eight* point-source HDF5 nodes and exact *eight* original receiver weights are mapped from their actual physical coordinates onto the deterministic fitted x P1 hat functions; native y-z Q1 indices and original eight HDF5 values remain unchanged. Both zeroth and first physical XYZ moments and rank-one tensor factorization reproduce the SHA-original physical source [1.5,2,2]m and receiver [2.5,2,2]m to strict tolerance. This is a physically consistent **alternative discretization**, NOT identical original 8-node discrete point q0 basis or PFFDTD graph. The original signal q0, native dt/h/Nt, 250ms signed 40/80Hz, sound speed343.2, rho1.2 and exact frozen acceptance are never changed.

The candidate uses all-mode implicit Newmark β=1/4 for the complete original point-q0 record (even when explicit timestep CFL fails). The original native explicit leapfrog spectral CFL is diagnosed on precisely the same new complete eigenbasis and is **never** made stable by dropping modes or retuning time.

## Original native 5-grid full-modes real-HDF5 diagnostics

| PPW | physical uniform x P1 segments | all new positive-supported 3D modes | prior short-ended x `dt² λmax` | fitted uniform x `dt² λmax` | native explicit leapfrog CFL |
|---|---:|---:|---:|---:|---|
| 28 | 33 | 36,754 | 49.0383 | **8.9228** | FAIL |
| 32 | 38 | 53,001 | 7.1199 | **4.8158** | FAIL |
| 36 | 42 | 73,788 | 7.9477 | **4.9828** | FAIL |
| 40 | 47 | 100,848 | 59.5769 | **9.7547** | FAIL |
| 44 | 52 | 132,341 | 7.1643 | **4.8470** | FAIL |

The enormous PPW28/40 CFL outliers improve substantially by boundary-fit x, identifying a **genuine x physical-end support instability** of the previous experimental solver. Yet every grid still fails explicit `dt²λmax < 4`, consistent with remaining sloped-roof tiny positive-supported basis modes. Changing only x is not sufficient.

## Full unchanged 250ms signed original q0 40/80Hz acceptance

The old three strict acceptance gates are applied identically (complex RMS ≤0.20, relative magnitude ≤0.25, max phase ≤15°), requiring **every adjacent refinement pair PASS and all three metrics strictly decrease**. No changing the final time sample, window, high-mode content, or selection of good grids.

| PPW pair | Complex RMS | Relative magnitude | Phase deg | all three |
|---|---:|---:|---:|---|
| 28→32 | 0.36628818 | 5.20537377 | 22.38915° | **FAIL** |
| 32→36 | 0.19048882 | 0.75650482 | 25.35306° | **FAIL** |
| 36→40 | 0.08033224 | 0.05836585 | 10.24176° | PASS |
| 40→44 | 0.81247803 | 0.59434067 | 173.97622° | **FAIL** |

**Only 36→40 passes; strict monotonicity fails. The experimental spatial solver remains NOT CONVERGED and original PFFDTD remains SELF_CONVERGENCE_FAILED.** At PPW40→44 there is again a nearly 174° phase inversion. x-end boundary regridding does not eliminate the original finite-record broadband q0 sensitivity.

## Independent analytic first true inclined-roof reflection

Pre-existing fixed physical 3 widths (0.35/0.60/0.85ms) and analytic original 64 source–receiver pair roof weak witness were used, with all new modes included. Relative weak witness errors:

| PPW | fitted uniform x | previous positive-mass native x |
|---|---|---|
| 28 | 1.257 / 1.203 / 1.163 | 1.393 / 1.399 / 1.381 |
| 32 | 0.995 / 0.949 / 0.918 | 0.829 / 0.841 / 0.845 |
| 36 | 0.961 / 0.878 / 0.836 | 1.157 / 0.996 / 0.911 |
| 40 | 0.847 / 0.864 / 0.849 | 0.715 / 0.863 / 0.907 |
| 44 | 0.850 / 0.889 / 0.847 | 0.866 / 0.908 / 0.859 |

10 of 15 **improve relative to the previously rejected row-lumped model**, but numerical errors remain extremely large compared with the earlier physically more accurate *consistent-mass* roof hybrid. The weak witness contains direct-pulse dispersion tails and is not an independent roof-only time-domain qualification. No promotion.

## Reproducibility / status

- Frozen preregistered plan: `benchmarks/acoustics/r130d_original_q0_boundary_fitted_uniform_x_true_roof_plan_2026-10-10.json`
- Source and physically moment-preserving original HDF5 reprojection: `backend/src/htdt/r130d_uniform_boundary_fitted_x_p1.py`
- Complete original SHA-native HDF5 all-mode experiment: `scripts/run_r130d_original_q0_boundary_fitted_uniform_x_true_roof.py`
- All5 signed source, roof and 3 gate evidence: `benchmarks/acoustics/r130d_original_q0_boundary_fitted_uniform_x_true_roof_evidence_2026-10-10.json`
- Independent physical x support/energy and original 8node trilinear moment regression; full frozen five-grid evidence regression: `backend/tests/test_r130d_uniform_boundary_fitted_x_p1.py` and `backend/tests/test_r130d_original_q0_uniform_x_fivegrid_evidence.py`

**Original PFFDTD = SELF_CONVERGENCE_FAILED; independent physics = NOT_VALIDATED; product = NO_GO.** Issue #53 remains OPEN, PR #118 Draft. No original upstream wave reruns, GitHub Actions, changed canonical q0 data or acceptance gates, source smoothing, mass floors or modal truncation. Keep `scratch/`.
