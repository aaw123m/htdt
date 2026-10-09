# R130D #938: proposed numerical 3-phase lattice averaging of original q0 — FAIL

Date 2026-10-09 JST. Draft PR #1055. No new PFFDTD code or wave runs; no GitHub Actions.

This is a **prospectively frozen numerical discretization remedy candidate**, not another physical source model. Plan [r130d_exact_roof_q0_three_phase_numerical_ensemble_plan_2026-10-09.json](../benchmarks/acoustics/r130d_exact_roof_q0_three_phase_numerical_ensemble_plan_2026-10-09.json) was committed as `c5a33aba18d79666488494ea8817ad78b765673c` **before observing the averaged scores**.

## Motivation and unchanged physics

The directly measured original native PFFDTD full signed q0 eigendecomposition shows BOTH low Neumann geometry drift and non-negligible high-mode contamination of the unaltered 250ms 40/80Hz finite pressure transfer. The exact-volume 56m³ conservative roof FV corrects the low frequencies but point q0 remains nonconvergent. Native grid phase produces actual signed point q0 sensitivity. Here we test simple **deterministic quadrature of the same mathematical continuous physical point problem across three x-grid phase realizations** without moving the true source at (1.5,2,2)m, receiver (2.5,2,2)m, changing source samples, roof geometry, receiver physical point, original native per-grid dt or Nt, 250ms record, or 40/80Hz bins. Numerical source/receiver trilinear weights follow the shifted computational grid, preserving constant and first physical Cartesian moments. Every constituent was an **already executed actual 250ms wave**, energy-conserving original q0 drive from previous frozen evidence.

The sole preregistered estimator is arithmetic `(H(-h/4)+H(0)+H(+h/4))/3` of complex signed **physical** P_T/Q_T at each frequency, never phase/amplitude magnitudes separately, with no fitted weights, no frequency mask, amplitude correction, taper, damping or shortening. This is a numerical spatial phase ensemble, *not* a native PFFDTD production-compatible modification or independent physical FEM/BRAS reference.

## Actual PPW40 and PPW44 results

| Original physical point q0 experimental spatial scheme | PPW40→44 complex L2 relative | max relative magnitude | max phase | Meets frozen original 0.20 / 0.25 / 15° thresholds |
|---|---:|---:|---:|---|
| Exact-roof FV + one original native x phase | **0.872666** | **3.207787** | **170.011°** | FAIL |
| Exact-roof FV + all three x phases signed **equal averaging** | **0.599302** | **8.887243** | **137.805°** | **FAIL** |

The proposed averaging lowers the complex relative L2 error (from 0.873 to 0.599), but the physical **40Hz** complex signed transfer of PPW44 is near zero after averaging different signed responses, dramatically inflating relative amplitude error (8.89) and still leaving phase difference >137°. This is an adverse numerical cancellation across physical same-source spatial discretizations; averaging cannot be trusted as a convergence cure just because one metric improves.

True signed 40Hz/80Hz ensemble P_T/Q_T, Pa/(m³/s):
- PPW40: 40Hz `+78.5494608320−6.14263807368i`, 80Hz `+107.807695508−229.126882613i`.
- PPW44: 40Hz `−5.46848573761+5.79630331820i`, 80Hz `+20.7378266017−205.480203283i`.

Both originally evaluated 40/80Hz bins are kept. All three exact original native timestep and sample counts and six actual full-wave signed source/receiver point records are bound to their previous frozen raw evidence in [the complete three-phase negative evidence](../benchmarks/acoustics/r130d_exact_roof_q0_three_phase_ensemble_evidence_2026-10-09.json). The [deterministic original point q0 numerical phase quadrature script](../scripts/run_r130d_exact_roof_q0_three_phase_ensemble.py) and its fail-closed regressions check signed averages, original four shifted full waves, original two unshifted full waves, source/time invariance, fixed weights, no new wave or Actions and every negative gate.

**Decision: 3-phase equal-weight grid averaging is REJECTED as a convergence remedy.** One high-PPW pair cannot establish full grid convergence even if numerically passing; this pair fails decisively. Do not smooth the actual point source, tune quadrature weights to particular bins, change the full-record authority or promote alternative numerical schemes.

**Original PFFDTD q0 point SELF_CONVERGENCE_FAILED; BRAS/owned-room physical NOT_VALIDATED; product NO_GO; Issue #938 OPEN; PR #1055 Draft.** No new Actions.
