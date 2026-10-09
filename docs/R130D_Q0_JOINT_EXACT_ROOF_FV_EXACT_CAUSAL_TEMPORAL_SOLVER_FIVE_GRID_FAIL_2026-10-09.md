# R130D Issue #938 — Joint exact sloped-roof FV spatial operator + exact causal time propagators: full q0 five-grid FAIL

2026-10-09 JST. Draft PR #1055 remains Draft, Issue #938 OPEN. No new native PFFDTD waves or manually dispatched GitHub Actions.

## Precisely predeclared physical numerical-solver test

Before computing the new five-grid wave-transfer outcomes, the detailed fixed experiment plan was committed **AND PUSHED** in `04fa4058be4cf5df8a6b9aaf62e86309b3a027c5` with `[skip ci]`. This comparison is a **true space/time numerical method modification**, not just an original PFFDTD postprocessing change. The spatial scheme is the existing **exact 56m³ sloped Neumann cutcell FV**: genuine physical diagonal mass M, genuine conservative exact fluid-face apertures and symmetric positive semidefinite K, independent full 3D CSR proof of `M=Mx⊗Myz` and `K=Kx⊗Myz + Mx⊗Kyz`. It is NOT the canonical original upstream PFFDTD 6-neighbor staircase graph.

We use the actual pinned upstream native PFFDTD SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`, **original source and receiver physical positions (1.5,2,2)m and (2.5,2,2)m**, SHA-checked real native PPW28/32/36/40/44 voxel and comms HDF5, original 8node source/receiver weights, original q[0]=1 with all later q0 zero, individual native original h/dt/Nt and source scaling `sum(in_sigs[:,0])=l2/h`, original 250ms pressure sample count, original 40/80Hz signed P_T/Q_T and the frozen three simultaneous numeric acceptance tolerances **complex 0.20 / magnitude 0.25 / phase 15°**.

All *actual original-grid exact roof* **359,880 generalized M/K mass-orthonormal eigenmodes** are included; no cutoff, damping, fit, smoothing, altered point, frequency selection, taper or changed scoring. Modal source/receiver couples the original 8node weights to the exact physical FV masses, with fixed unfitted q0 kick `A_m=dt²*c²*coupling_m`, agreeing with original forcing on equal-volume cells.

## Three actual numerical space/time operators

1. `existing_exact_roof_newmark`: the formerly implemented **true 3D full-state Newmark-CG** exact 56m³ FV q0 solver (mass/stiffness the same); exact full modal closed-form theta `2atan(dt sqrt(lambda)/2)` and original beta=1/4 midpoint kick. The new analytical replay individually reproduces **earlier independently integrated actual 250ms FV/Newmark-CG waves** on PPW28,32,36,40,44 with signed two-bin complex relative **2.9504e−8 / 1.8718e−8 / 1.5488e−8 / 1.7467e−8 / 1.7294e−8**.
2. `exact_roof_velocity_impulse`: **exact causal semidiscrete FV oscillator** time propagator, instantaneous point-source velocity jump `v(0+)=A/dt`, `phi[n]=A*sin(n*dt*sqrt(lambda))/(dt*sqrt(lambda))`. Original 8node q0 coefficient and geometry are fixed. The source is interpreted as an instantaneous impulse; this is not the original PFFDTD source stencil.
3. `exact_roof_one_native_sample_hold`: exact semidiscrete *forced* FV oscillator, q0 acceleration source `a=A/dt²` constant for the first original timestep only and zero thereafter, `phi[0]=0`, `phi[n>=1]=A*(cos((n-1)*theta)-cos(n*theta))/theta²`. This causal held-force temporal interpretation differs from upstream discrete leapfrog and Newmark, but uses the **same frozen original one-sample q0 waveform data, clock, grid, physical mass and complete 250ms score**. Zero-mode n−1/2 continuation is handled analytically.

All arms use the original second-order finite pressure difference (one-sided endpoints, centered interior) and exact original rectangular full-record signed two-bin transform.

**Critical high-frequency case:** exact sloped roof PPW32 has **380 full physical generalized eigenmodes with `omega*dt > pi`**, and PPW44 has **676**; PPW28/36/40 have zero exceeding Nyquist. These are **real mass/stiffness sliver-cutcell high modes**; none were deleted, masked or clamped. For the true exact semidiscrete solver, their physical `omega*dt` is retained in the forcing `sinc` amplitudes; only the exponential finite-DTFT geometric phase is reduced modulo `2pi` by a mathematically exactly equivalent identity for numerical stability. Thus no physical high-frequency filtering is used. The stable O(all true modes) formula, including high theta and zero mode, was independently unit-tested against explicitly sampled direct oscillators, direct finite difference pressures and direct DFT on multiple native Nt/dt.

## Actual complete original-q0 signed PPW adjacent comparisons

All numbers are unrounded original two-bin 40/80Hz complex relative errors (shown to 6 decimals), lower is better; the complex gate is **≤0.20** and the two additional magnitude/phase gates are both mandatory.

| Exact roof physical solver with original 8node q0 | PPW28→32 | PPW32→36 | PPW36→40 | PPW40→44 | All four three-gates and strict monotonicity |
|---|---:|---:|---:|---:|---|
| Existing exact-roof native Newmark FV (actual CG wave controlled) | **0.052369** | 0.218508 | 0.106168 | **0.872666** | **FAIL / NO** |
| **New exact causal velocity-impulse FV integrator** | **0.595250** | **0.612004** | **0.137052** | **0.939071** | **FAIL / NO** |
| **New exact causal one-sample forced FV integrator** | **0.775186** | **0.642206** | **0.041357** | **0.551738** | **FAIL / NO** |

Candidate detailed adverse [complex / max relative magnitude / max phase]:
- **Velocity impulse**: 28→32 `0.595250 / 0.422317 / 175.841°`; 32→36 `0.612004 / 1.438360 / 174.806°`; 36→40 `0.137052 / 0.436589 / 5.817°`; 40→44 `0.939071 / 0.721976 / 174.291°`.
- **One-sample held force**: 28→32 `0.775186 / 0.368006 / 176.159°`; 32→36 `0.642206 / 10.915523 / 169.230°`; 36→40 `0.041357 / 0.414771 / 2.021°`; 40→44 `0.551738 / 0.848923 / 163.988°`.

**ALL three numerical methods fail the frozen full original acceptance on the complete five-grid refinement series.** Notably, 36→40 one-sample hold has an apparently spectacular complex-only **0.041357** and phase **2.021°**, but **max magnitude 0.414771 > original 0.25**, therefore FAIL. PPW40→44 remains grossly phase-inconsistent across both new schemes. At PPW32→36 the one-sample held-force arm has enormous max relative magnitude **10.915523**, not a hidden outlier. Previous baseline Newmark occasionally passes an isolated pair, but fails the full multigrid refinement trend; no favorable pair is accepted alone. Strict all-metrics monotonicity fails for all three arms.

The exact-roof sliver high-frequency population changes **non-monotonically** with original PPW due to how original nodes intersect the roof. Combined with mathematically exact q0 pulse quadrature, the complex signed 250ms responses still exhibit large inter-grid drift. This is diagnostic evidence of a space/time/observable **joint consistency and point singularity challenge**, not a rigorous proof that *only* sliver cells cause the problem. The old exact-roof mass/Newmark-CG archive cross-check and new direct oscillator pressure-time verification demonstrate the failure is not numerical instability or a lack of mode completeness.

Evidence: [all actual five-grid physical full-mode signed transfers, all frequency-bin magnitude/phase adverse metrics and every SHA](../benchmarks/acoustics/r130d_exact_roof_q0_exact_causal_time_multigrid_evidence_2026-10-09.json). Actual numerical solver implementation: `backend/src/htdt/r130d_exact_semidiscrete_q0.py`; true native original-HDF5 exact roof runner: `scripts/run_r130d_exact_roof_q0_exact_causal_time_multigrid.py`; high-frequency aliases, exact waveform, regression/frozen three-gate controls: `backend/tests/test_r130d_exact_roof_q0_exact_causal_time_multigrid.py`. Prospectively frozen [plan](../benchmarks/acoustics/r130d_exact_roof_q0_exact_causal_time_multigrid_plan_2026-10-09.json).

## Authority and remaining genuine engineering

This is the first frozen 5-grid **joint exact roof FV physical geometry + exact causal q0 temporal integration** test in this series, and it fails; it is not retroactive self-convergence of original PFFDTD and cannot requalify canonical run25/run76 PPW8/10/12. Future work should focus on physical point Green-function well-posedness, consistent mass/source/receiver and Neumann sloped boundary variational discretization, especially tiny cutcell high-mode behavior and time-integrated observation without source/target alteration. Any new numerical scheme must be prospectively frozen and then tested across all grids with exact original signed outputs and independent physical reference.

**Original upstream PFFDTD q0 SELF_CONVERGENCE_FAILED; external BRAS/MFEM/owned-room NOT_VALIDATED; product NO_GO; PR #1055 Draft OPEN; Issue #938 OPEN.**
