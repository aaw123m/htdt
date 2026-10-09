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

## Independent method and remaining acceptance gates

Pinned independent MFEM C++ P2 r2/r3/r4 actually assembled the same physical Gaussian integrals, and the original q0 midpoint waveform was solved on all spatial levels. The real C++ build, quadrature and drive completed successfully in `.github/workflows/r130d-fixed-spatial-kernel-impulse.yml` run #37880770435. The workflow retains all values, regardless of sign, magnitude, phase, and comparison result. The independent numeric verdict, including the sigma 0.35 failure, is detailed below.

Even if the altered spatial model meets all numerical thresholds, it cannot establish convergence for the **original** full-band point-source impulse. More high-mode and purely temporal integrator tests remain, along with measured BRAS/owned-room holdouts and general CAD. Current original impulse = **SELF_CONVERGENCE_FAILED**; original candidate = **NOT_QUALIFIED**; physical measurement = **NOT_VALIDATED**; product = **NO_GO**; issue OPEN, PR Draft.


## Independent MFEM P2 C++ actual full CI result — complete

[GitHub Actions run #37880770435](https://github.com/ka0923s-a11y/HTDT/actions/runs/37880770435) **SUCCESS** on commit `f680e618f3bc2741c87b9c6d30f8e257f376d8f2`: fetched the pinned MFEM C++ commit, compiled the new independent P2 Gaussian integrator, truly assembled r2/r3/r4 source and receiver spatial integrals with order-10 quadrature, actually integrated the unchanged temporal `q[0]=1` for **all 12 spatially regularized wave cases** (both widths, 3 FV and 3 MFEM), checked full signed complex bins and true linear residuals. Full raw results and all P2 sparse and spatial-integral SHA-256 hashes are in [independent FV/MFEM numerical evidence](../benchmarks/acoustics/r130d_fixed_spatial_kernel_independent_fv_mfem_evidence_2026-10-09.json). The published actual FV values agree with local FV evidence within floating-point precision.

| Spatial width (m) | FV n20→32 complex L2 | P2 MFEM r3→4 complex L2 | FV n32 vs P2 r4 complex L2 | P2 last maximum magnitude relative | P2 last maximum phase |
|---:|---:|---:|---:|---:|---:|
| 0.35 | 0.157134 | 0.047765 | 0.162211 | **0.189280** | 1.391° |
| 0.70 | 0.166888 | 0.017375 | 0.111609 | 0.007522 | 0.998° |

**Using exactly the previously registered numeric bounds** (FV final complex 0.20, max relative magnitude 0.25, max phase 15°; P2 final complex 0.05, max magnitude 0.08, max phase 5°; finest cross-method complex 0.35, max relative magnitude 0.40, max phase 25°, cross magnitude dB 3; and last-adjacent improvement on all three), the spatial-Gaussian **σ=0.70m** diagnostic meets all specified numerical criteria. Both methods' latest FV/P2 errors improve in complex, maximum magnitude, and maximum phase compared with their prior adjacent comparisons. Finest cross-case sigma0.70 maximum amplitude difference is 0.067630, maximum phase 6.533°, maximum magnitude difference 0.5684 dB; all below prospective limits.

The **σ=0.35m** diagnostic **FAILS** its independent P2 max relative magnitude criterion (0.189280 against 0.080000), despite independent P2 relative complex error 0.047765 ≤0.05. This is retained, **not reclassified as PASS**. Cross-case sigma0.35 relative complex 0.162211; separate pass for cross-method alone cannot override P2 amplitude failure.

Maximum true FEM midpoint PCG relative residual over all stored cases is below 1.0e-11; FV LU is at machine precision. These observations *support* sensitivity to the spatial idealized point-source/receiver operators and a source-width-dependent numerical convergence improvement; they do **not** identify a single uniquely causal defect nor imply true continuum point-source convergence.

### Canonical authority unchanged

This separately regularized Gaussian **spatial source and receiver** materially modifies the physical input/observation contract even though the original *temporal* q0 remains unchanged. The sigma0.70 candidate-only numeric PASS is **not** a canonical point-source self-convergence pass. Canonical R130D fullband PFFDTD and original point-impulse **SELF_CONVERGENCE_FAILED** / original independent candidate **NOT_QUALIFIED**; owned-room/BRAS physical reference **NOT_VALIDATED**; product **NO_GO**. Keep PR #1055 Draft and Issue #938 OPEN.

**Provenance nuance:** the original local untracked-tree Windows FV-only calculation serialized the same preregistered JSON with CRLF and has raw plan-byte SHA `c7a2bc63...`; Git checkout normalizes to LF and verified CI's canonical raw-plan SHA is `26b1c8d7...`. JSON plan contents are equal, and raw signed finite-record values are cross-checked with numeric float tolerance rather than exact ASCII serialization. No threshold, geometric model, or waveform changed.
