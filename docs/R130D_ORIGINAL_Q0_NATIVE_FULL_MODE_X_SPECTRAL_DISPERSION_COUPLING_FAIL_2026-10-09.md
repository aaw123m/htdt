# R130D #938 — original native complete-q0 temporal / x-Neumann spectral A/B: ALL FAIL

2026-10-09 JST; Draft PR #1055; original Issue #938 OPEN. **No GitHub Actions initiated.**

## Authority, registration, exact input

Prospectively frozen experimental plan:
[`r130d_original_q0_spectral_x_dispersion_coupling_plan_2026-10-09.json`](../benchmarks/acoustics/r130d_original_q0_spectral_x_dispersion_coupling_plan_2026-10-09.json),
committed and pushed in `f6d05665587f622634cf44b79e7d992400eec716` **before numerical results**. Upstream PFFDTD pin `aa319f6c86517cb95aabfae8656277da62c3ead5`. Original native HDF5 voxels, original physical source (1.5,2,2) m / receiver (2.5,2,2) m, eight-node trilinear interpolation in native arms, zero-flux original voxel staircase y-z geometry, q[0]=1 with q[n>0]=0, individual native Ts and Nt, complete 250ms rectangular pressure response, and both original 40/80 Hz bins are preserved. **This does not change the original PFFDTD solver, its canonical run25/run76 thresholds, or its source waveform.**

The original **ALL five SHA-validated original 3D PFFDTD source-connected graph eigenbasis**, independently proved `A = Ax⊗I + I⊗Ayz` and previously frozen in `3cf849c`, is reconstructed for each case. All **347,154 original eigencoupled modes** are propagated in five numerical operator arms (no modal filters, damping, averaging of amplitudes, frequency deletion, taper, smoothing, or spectral cutoff). Every original full-wave Leapfrog control reproduces its saved raw native full response at signed relative **1.49e-13 to 2.53e-11**.

### Explicitly compared numerical propagation arms

1. **Native Leapfrog control:** exact original eigenvalues and eight-node source/receiver couplings, `theta=2asin(omega_native*Ts/2)`; analytically exact native q0 finite-record pressure.
2. **Native-graph Newmark:** original spatial eigenvalues and couplings; replace homogeneous propagator by `theta=2atan(omega_native*Ts/2)`.
3. **x-frequency Newmark:** as 2, replace `lambda_x,m` by `(h*pi*m/Lx)^2`, `Lx=4m`; original y-z graph/couplings untouched. First nonzero corrected x frequency is **42.900000 Hz** for every grid.
4. **x-coupling Newmark:** as 2, original graph frequencies, replace only x source–receiver product by continuous Neumann `(2-delta_m0)/Nx*cos(m*pi*x_s/4)*cos(m*pi*x_r/4)`. Original y-z coupling preserved.
5. **Both x Newmark:** apply 3 and 4 simultaneously, with otherwise same propagator and y-z graph.

**Interpretation boundary:** Newmark arms change the time propagator while freezing the *native discrete initial one-sample q0 modal kick* (`phi[0]=0, phi[1]=original strength × source projection`). That is an intentionally isolated finite discrete-update diagnostic, not a proof that the time-discretized forcing equals a physical continuum delta under different integrators. The continuum x-mode point coupling is a candidate quadrature, not a confirmed consistent replacement for original HDF5 8-node weights or a general 3D production solver. No BRAS reference is used.

## Complete observed signed-refinement metrics

The entries are **actual full 250ms signed complex** PPW coarse→fine errors; the frozen acceptance requires all three **complex RMS ≤0.20, maximum relative magnitude ≤0.25 and maximum phase ≤15°**. Full signed complex transfers, two-bin deltas, per-frequency metrics and every failed metric are saved in
[`r130d_original_q0_spectral_x_dispersion_coupling_evidence_2026-10-09.json`](../benchmarks/acoustics/r130d_original_q0_spectral_x_dispersion_coupling_evidence_2026-10-09.json).

| Numerical arm | PPW28→32 complex | PPW32→36 complex | PPW36→40 complex | PPW40→44 complex | Gate |
|---|---:|---:|---:|---:|---|
| **Original native Leapfrog** | 1.246927 | 0.761305 | 0.367367 | 0.958742 | **4/4 FAIL** |
| Native spatial graph, Newmark | 1.124513 | 0.610438 | 0.693884 | 1.032989 | **4/4 FAIL** |
| x Neumann frequency corrected, Newmark | 0.734939 | 1.031479 | 0.562635 | 0.747935 | **4/4 FAIL** |
| x point coupling corrected, Newmark | 0.930510 | 0.617531 | 2.171906 | 1.081905 | **4/4 FAIL** |
| Both x corrections, Newmark | 2.136844 | 0.966175 | 2.942214 | 1.178809 | **4/4 FAIL** |

The **x-frequency-only correction** lowers the signed complex error at PPW28→32 (1.125→0.735 against same Newmark time integrator) and 40→44 (1.033→0.748), but **worsens 32→36** (0.610→1.031), and magnitude/phase remain far beyond tolerance; PPW40→44 relative magnitude **3.452893** and phase **120.924°**. This is not a convergence cure.

The **x-coupling-only correction** significantly worsens 36→40 (0.694→2.172), and the simultaneous corrections worsen it again (2.942). At PPW28→32 both corrected arms have magnitude error **102.022**, exposing strong signed-cancellation sensitivity that must not be hidden. Even switching original Leapfrog to Newmark on original graph alone improves some pairs while worsening others. The result proves that local x-frequency accuracy, x point evaluation and temporal update cannot simply be mixed independently and assumed to cure the fixed broadband finite-window transfer.

## Negative conclusion and next numerical task

1. **Rejected as a remedy:** these particular five full-mode spatial/temporal combinations all fail all four frozen adjacent high-PPW pair gates.
2. They do **not** isolate a unique causal contribution of y-z roof, x-frequency and x coupling in a nonlinear norm sense. Because differences are signed complex and combine/cancel, keep every raw two-bin transfer and phase. Higher accuracy in a single x eigenfrequency does not imply convergence of the complete Green transfer.
3. For a credible solver repair, evaluate a **jointly consistent high-order space–time–source discretization** that conserves the correct discrete/continuous impulse convention and consistent geometry, including y-z roof and high-mode phase. Cross-validate with independent solvers and held-out grid refinements before any claim of convergence.
4. The original **PPW8/10/12 canonical run25/run76** point q0 and physical BRAS/room validation have **NOT** been passed or requalified. This spectral exercise must not substitute for native production tests.

**Verdict: original PFFDTD original point q0 SELF_CONVERGENCE_FAILED; external physical NOT_VALIDATED; product NO_GO; PR #1055 Draft; Issue #938 OPEN.** Test runner `scripts/run_r130d_original_q0_spectral_x_ab.py` and independent fail-closed regression `backend/tests/test_r130d_original_q0_spectral_x_ab.py` accompany saved unfavorable evidence. No GitHub Actions dispatched.
