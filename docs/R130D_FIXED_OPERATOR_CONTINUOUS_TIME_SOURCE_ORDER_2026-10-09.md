# R130D fixed spatial operator, independently evaluated continuous-time source — actual midpoint convergence

Date: 2026-10-09. Issue #938; investigation only. **This is NOT the original q[0]=1 unit sample impulse.** It explicitly changes the temporal forcing to \(q(t)=\exp[-\frac{1}{2}((t-0.040)/0.004)^2]\) m³/s, a smooth analytic waveform, evaluated at each midpoint time without shifting its center, renormalizing its samples, temporal interpolation, or taper. Source/receiver spatial point functionals stay fixed **within** each spatial discretization.

Precommitted [experiment design](../benchmarks/acoustics/r130d_continuous_time_source_refinement_plan_2026-10-09.json) at local commit `de22e873f5d6fbfd864b205667a5b7c42621cd2e` **before** the numerical result. Fixed room, frequency bins 40/80 Hz, record duration 250 ms, \(P_T/Q_T\) with \(\exp(+i2\pi f t_{mid})\), and exactly fixed FV n20 and independent hashed P2 MFEM r2 operators. The original independent archived MFEM r2 matrix SHA-256 is `e61410d4a69000c39975762b9986008974353953f7d8d9f100f9953a33c5ecb4`.

The actual finite-time wave solver ran independently at dt = 250, 125 and 62.5 microseconds (1000, 2000 and 4000 samples per operator), with true linear residual verification and no removed 40/80 Hz data. Raw signed complex observations, magnitudes, phases, sampled-source SHA-256 and all residuals are retained in [numerical evidence](../benchmarks/acoustics/r130d_continuous_time_source_refinement_evidence_2026-10-09.json).

| fixed operator | complex relative L2 250→125us | complex relative L2 125→62.5us | last/first ratio | preregistered 0.40 ratio criterion | preregistered finest ≤0.01 |
|---|---:|---:|---:|---|---|
| FV n20 | 0.07791458 | 0.01893184 | 0.24298193 | PASS | FAIL |
| Independent pinned MFEM P2 r2 | 0.05884659 | 0.01478503 | 0.25124701 | PASS | FAIL |

The observed ratio is consistent with **second-order temporal convergence for this smooth forcing on both fixed semidiscrete operators**, while neither meets the preregistered 0.01 final-difference bound. No post hoc threshold or waveform alteration. The remaining magnitude at the finest level is not proven to be dominated by the integrator (finite-time complex observables, spatial operators and source approximation must be considered independently).

This isolates that the midpoint implementation is capable of smooth-source temporal refinement. It does **not** imply that the original sampled single-bin unit impulse converges, that the point-source spatial operator is convergent, or that PFFDTD fullband run25/run76 passes. Original point-impulse remains **SELF_CONVERGENCE_FAILED**, experiment point-candidate **NOT_QUALIFIED**, physical BRAS/owned-room **NOT_VALIDATED**, product **NO_GO**, Issue #938 OPEN / PR #1055 Draft.

Tests: continuous-source waveform integrity, preregistered source center/width/dt/threshold fail-closed, finite signed transfer and 1-DOF test. Actual Windows CI for this newly introduced time-order diagnostic must be reported separately; local test success is not a CI success claim.
