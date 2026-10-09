# R130D fixed-physical-width spatial Gaussian / original temporal impulse — independent numerical diagnostic

Date: 2026-10-09 (JST). Issue #938, draft PR #1055. This document records an **altered spatial source and receiver model**; it does **not** replace original point-source R130D, the canonical PFFDTD run25/run76 input, or a production gate.

## Scientific question and preregistration

Low-order Neumann modes converge on FV and independently pinned P2 MFEM, but the original finite-record, full-state sampled point-impulse transfer at 40/80 Hz remains nonconvergent. Source and receiver point functionals may excite/measure unresolved high spatial modes. Before numerical observation, the source/receiver operator, physical scales, quadrature, grid sequence, 1000 midpoint steps, original temporal impulse, observation complex-sign convention, frozen thresholds and resource cap were committed in [r130d_fixed_spatial_kernel_plan_2026-10-09.json](../benchmarks/acoustics/r130d_fixed_spatial_kernel_plan_2026-10-09.json), plan-only commit `cec201c54c7f399c0fae7e811bfecd85440df85e`.

The kernel is \(g_\sigma(x;x_0)=\exp(-|x-x_0|^2/(2\sigma^2)) / \int_\Omega\exp(-|y-x_0|^2/(2\sigma^2))\,dy\) for source center (1.5,2,2)m and receiver center (2.5,2,2)m. Frozen \(\sigma = 0.35\) m and 0.70 m. Source and receiver are individually unit-integral on the room, **not point functionals**. No temporal smoothing whatsoever: \(q[0]=1, q[n>0]=0\), \(dt=250\mu s\), \(T=250 ms\), no taper, \(P_T/Q_T\) at **both** 40 and 80 Hz.

FV: exact clipped planar cell geometry, 4-point Gauss–Legendre quadrature in each of x/y/z and y-interval subdivision where the roof crosses a z-bound; integrate source/receiver **over each actual air volume**. P2 FEM: independent pinned MFEM C++ tetrahedron mesh, assemble source and receiver via `DomainLFIntegrator` with tetrahedral integration order 10, and independently normalize each integrated H1 P2 functional to sum 1. The FEM original mass and stiffness operators and hashes are not replaced by FV approximations.

## Completed FV observations (actual midpoint wave solves)

Six actual FV time-domain wave solves: n12, n20, n32 at each width; 1000 steps each. Plan hash `c7a2bc636b26aeeffed48d362586cff3cb45653d20431dca4de261221e8f9b2f`. Complete complex 40/80 Hz values, per-frequency magnitude and phase, exact quadrature volumes, and true midpoint solver residuals are in [FV-only evidence](../benchmarks/acoustics/r130d_fixed_spatial_kernel_fv_evidence_2026-10-09.json).

| sigma (m) | FV n12→n20 complex relative L2 | FV n20→n32 complex relative L2 | n20→n32 max relative magnitude | n20→n32 max phase |
|---:|---:|---:|---:|---:|
| 0.35 | 0.546495 | 0.157134 | 0.094833 | 7.606° |
| 0.70 | 0.482576 | 0.166888 | 0.067279 | 9.551° |

The last FV pairs meet the **unchanged numerical comparison thresholds** (L2 ≤ 0.20, maximum magnitude relative ≤ 0.25, phase ≤ 15°). Earlier FV pairs have much larger differences. This is at most a *candidate spatial-operator-only diagnostic improvement*, not an original-point-impulse pass; it is not a general 3D CAD or PFFDTD validation.

## Independent method / remaining checks

The same physical Gaussian integrals must be assembled by pinned MFEM C++ P2 r2/r3/r4; the C++ source and actual 1000-step independent FEM drives are tracked by `.github/workflows/r130d-fixed-spatial-kernel-impulse.yml`. At initial commit, the full independent MFEM run is **NOT_YET_CI_VERIFIED**, and neither FEM nor cross-method Gaussian convergence may be claimed. The workflow retains all values, regardless of sign, magnitude, phase, and comparison result.

Even if the altered spatial model meets all numerical thresholds, it cannot establish convergence for the **original** full-band point-source impulse. More high-mode and purely temporal integrator tests remain, along with measured BRAS/owned-room holdouts and general CAD. Current original impulse = **SELF_CONVERGENCE_FAILED**; original candidate = **NOT_QUALIFIED**; physical measurement = **NOT_VALIDATED**; product = **NO_GO**; issue OPEN, PR Draft.
