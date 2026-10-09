# R130D Issue #938 — true Galerkin consistent-mass roof-conforming P1 point q0: isolated PASS but complete five-grid nonconvergence

2026-10-09 JST. Draft PR #1055 / original Issue #938 OPEN. No original upstream PFFDTD wave runs or manually dispatched GitHub Actions.

## Prospective study and fully fixed original observations

This is a real NEW **physical Galerkin time-dependent mass operator**, not a reweighting/filter of a frequency response. A complete five-grid physically conforming mass-lumped P1 FEM experiment had been previously observed (HEAD `585159269e8b085319222a339f4689169906dfea`), including its unfavorable full q0 signed 40/80Hz transfer and the isolated complex-only passes. The new *non-lumped consistent mass* design was **prospectively committed and pushed BEFORE any consistent-mass wave scores were computed** as `2b9436a93db27ebdc54049fc76877745a2496b8c` with `[skip ci]`.

The original upstream PFFDTD SHA remains `aa319f6c86517cb95aabfae8656277da62c3ead5`. Actual original SHA-256-pinned PPW28/32/36/40/44 native HDF5 source/receiver/voxel files, exactly the **original 8 individual node indices and interpolation weights** for physical source (1.5,2,2)m and receiver (2.5,2,2)m, original `q[0]=1, q[n>0]=0`, native original h/Ts/Nt, original full **250 ms** pressure records, original **both signed 40 and 80 Hz** P_T/Q_T, original sound speed 343.2 m/s, rho=1.2 kg/m³, and unchanged simultaneous acceptance complex ≤0.20 / relative magnitude ≤0.25 / phase ≤15° remain frozen.

The exact physical Neumann spatial mesh, P1 triangles, x-axis physical vertices and stiffness **are identical to the previously completed true 56m³ roof-conforming P1 FEM**. No original Cartesian source nodes moved or dropped; all additional boundary nodes have original source coefficient **zero**. No mesh repair/Steiner simplification/truncation, artificial damping, high-mode cut, point-smoothing, Gaussian, fitted modal weights, frequency mask or new waveform.

## What the new implementable variational scheme changes

Only the FE **mass operator** changes from physical **row-sum lumping** into the exact standard **non-diagonal, consistent P1 Galerkin mass**:

- x line segment of length dx: `M_e=dx/6 * [[2,1],[1,2]]`, exactly integrated, all original physical x nodes and x=0/4 endpoints preserved.
- Every original true sloping roof yz triangle of area A: `M_e=A/12 * [[2,1,1],[1,2,1],[1,1,2]]`, exactly integrated. Standard P1 Neumann stiffness `K_e=c² * A * gradNa·gradNb` **unchanged**.
- True physical sparse 3D off-diagonal mass `M=Mx_consistent⊗Myz_consistent`; physical 3D Neumann stiffness `K=Kx⊗Myz_consistent + Mx_consistent⊗Kyz` as required for consistent tensor Galerkin.
- **Every** positive true generalized FEM eigenmode `K v=lambda M v`, `v^T M v=I` independently verified for strict eigenpair residual and mass orthonormality; exact physical Neumann constant mode retained.
- Original 8node weak point impulse `q0`, original receiver nodal values, true physical element mass and full beta=1/4 Newmark 250 ms frequency-domain *signed* wave. Unchanged original modal kick `A=dt² c²*(vsource)*(vreceiver)/(1+dt²lambda/4)`. Every real original mode, including above-Nyquist FEM modes, is included.

The new P1 *consistent-mass* FEM physically integrates squared affine fields exactly. An independent manufactured field `u=x+z` over the true x-extruded wedge has the closed analytical true physical volume integral `∫u² dV = 2780/3`; full sparse new off-diagonal 3D mass satisfies `u^T M u = 2780/3` to better than 2e−10. Likewise exact stiffness gives `u^T K u = 112*c²` for the true 56m³ domain. Constant mass integral `1^T M 1=56m³`. Row sums exactly reproduce the prior physical lumped mass, providing an independent numerical check that **only mass consistency** was changed, not roof geometry, physical total volume or original source.

Actual true 3D generalized mode counts are identical to the previously frozen lumped model, **37,835 / 53,001 / 75,504 / 102,949 / 132,341 = 401,630 all modes**. Mass is genuinely non-diagonal: physical true 3D mass nonzeros **750,767 / 1,058,345 / 1,516,060 / 2,075,965 / 2,677,949**. All semidiscrete eigenmodes above original native Nyquist are kept: **8,186 / 8,668 / 14,058 / 18,239 / 19,220**, respectively; mass consistency shifts the spectral distribution physically, never truncates high modes.

## Actual original-source unfiltered full 250ms results: frozen THREE gates

| PPW adjacent | Previously observed true roof P1 **lumped mass** complex RMS | NEW true consistent-mass P1 complex RMS | New max relative magnitude | New max phase | ORIGINAL simultaneous all three |
|---|---:|---:|---:|---:|---|
| 28→32 | 0.152290 | **0.087346** | **0.564412** | 11.366° | **FAIL** |
| 32→36 | 0.178714 | **0.015948** | **0.143612** | **0.497°** | **PASS (only this grid pair)** |
| 36→40 | 0.407179 | **0.377469** | **0.787819** | **16.551°** | **FAIL** |
| 40→44 | 0.578551 | **0.498232** | **10.710197** | **18.968°** | **FAIL** |

The **32→36 isolated triple-gate PASS is REAL and preserved**: two-bin signed relative complex 0.015948 <0.20, max relative magnitude .143612 <.25, max phase .497° <15. It is **NOT sufficient**: three other neighboring refinements FAIL and the entire PPW28→44 series does **not** decrease monotonically for complex error, maximum amplitude or maximum phase. The 40→44 high-PPW comparison has **10.710×** relative magnitude error — unequivocally FAIL despite moderately improved complex global norm. Neither the one-grid apparent success nor favorable 28→32 complex/phase is cherry-picked for product promotion.

Important: all complete per-grid raw **signed real/imag 40 and 80Hz** P_T/Q_T transfers, signed coarse/fine differences, adverse bin-resolved magnitude/phase/complex scores, original source and geometry SHA-256 hashes, exact full physical mass/volume/Neumann checks and all numerical eigenresiduals are retained in [evidence JSON](../benchmarks/acoustics/r130d_original_q0_conforming_p1_consistent_mass_evidence_2026-10-09.json). Old lumped-mass P1 reference comparisons were recomputed from their already saved evidence, not fitted.

### Reproduction

- Pre-observation plan `benchmarks/acoustics/r130d_original_q0_conforming_p1_consistent_mass_plan_2026-10-09.json`.
- Actual numerical solver `backend/src/htdt/r130d_conforming_roof_p1_consistent_mass.py` built on fixed P1 mesh `backend/src/htdt/r130d_conforming_roof_p1_fem.py`.
- Full original q0 input/shape/strength verified runner `scripts/run_r130d_original_q0_conforming_p1_consistent_mass.py`.
- Independent manufactured analytic mass and stiffness integral, complete generalized eigenbasis, preserved negative results and fail-closed original gates `backend/tests/test_r130d_original_q0_conforming_p1_consistent_mass.py`.
- No manually dispatched GitHub Actions, no new native original PFFDTD waves, `scratch/` left intact.

## Release decision

Mass consistency is a physical improvement over lumped-mass P1 in some refinements and a strong candidate for **further** genuine root-cause investigation, but mathematically **does NOT solve full broadband point-source q0 convergence**. The huge 40→44 amplitude discrepancy and nonmonotonic full-series errors remain; the original physical continuum reference and source/observer singularity still need investigation. This numerical P1 experiment cannot retroactively requalify the upstream original staircase PFFDTD run25/run76 PPW8/10/12 or independent BRAS/MFEM/owned-room physical validity.

**Original native PFFDTD q0 remains SELF_CONVERGENCE_FAILED. Independent physical validation NOT_VALIDATED. Product NO_GO. PR #1055 Draft, Issue #938 OPEN.**
