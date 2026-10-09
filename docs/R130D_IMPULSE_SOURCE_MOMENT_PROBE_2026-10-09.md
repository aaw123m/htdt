# R130D #938 — isolate temporal impulse placement with matched first two source moments

**2026-10-09; preregistered before execution; experiment is diagnostic only.**

## Experimental authority

Original exact one-sample impulse is `q[0]=1`, others zero,
at dt=250µs. Midpoint time=125µs, injected discrete integral
=250µs in source-rate units. Halving dt to125µs and keeping
q[0]=1 shifts the first midpoint to62.5µs and halves the discrete
source-rate integral; the linear normalized `P_T/Q_T` cancels
uniform amplitude but not its injection time or spectral shape.

For this **new diagnostic stimulus only**, the preregistered
matched fine signal uses `q[0]=q[1]=1`, all others zero, dt125µs,
2,000 steps / 250ms. Its temporal centroid is125µs and its
integral is250µs, exactly matching the original coarse sample.
All three variants use the same point source (1.5,2,2)m, receiver
(2.5,2,2)m, 56m³ sloped rigid room, c343.2, rho1.2, and 40/80Hz
finite-record P_T/Q_T. No wavelength-specific scaling or phase fit.

**The two-sample fine stimulus is not the original unit-impulse
source; it cannot clear any original impulse or production gate.**

## Independently executed 2,000-step acoustic results

| Fixed spatial solver | Original dt halving (coarse raw vs fine raw) | Coarse raw vs fine moment-aligned | Fine raw vs fine moment-aligned |
|---|---:|---:|---:|
| Exact-cut FV n20 | 0.169179 | 0.178772 | 0.048303 |
| Exact-cut FV n32 | **0.566078** | **0.539817** | **0.022828** |
| Pinned independent MFEM P2 r4 (35,937 DOF) | **0.515042** | **0.261555** | **0.473803** |

All numbers are relative complex L2 differences on both 40/80Hz
signed complex pressure/volume-velocity bins, normalized by the
second listed solution, with no bin masking. The independent MFEM P2
r4 run again integrated the full wave equation over 2,000 midpoint
steps and reached max true PCG linear relative residual below1e−11
against the same independently hashed original pinned MFEM sparse
mass/stiffness matrices; the FV waves actually stepped the candidate
conservative Neumann operator.

At FV n32 the fine-grid response changes only0.02283 when the
source temporal centroid is aligned, but coarse-to-fine numerical
discrepancy remains0.53982. Therefore **pulse first moment alone
cannot account for the FV n32 dt dependence**. At MFEM r4,
moment alignment reduces coarse-to-fine error0.515→0.262, but
remains large, and the fine-method response itself shifts0.474
when the source temporal shape changes. Different discretizations
respond differently to the extremely short high-frequency signal.

These results are not a proof of any single dominant mechanism:
changed fine two-bin waveform has less high-frequency energy
than the original fine one-bin waveform, in addition to matching
centroid. A temporal quadrature-only convergence claim requires
a smooth, dt-invariant continuous forcing function and independent
temporal reference. A broadband point source ultimately requires
spatial regularity and temporal sampling analysis together.

## Frozen evidence

- Preregistered shape and limits:
  `benchmarks/acoustics/r130d_impulse_source_moment_matching_plan_2026-10-09.json`.
- Actual 3 × 2,000-step waves and 3-way complex numerical metrics:
  `benchmarks/acoustics/r130d_impulse_source_moment_evidence_2026-10-09.json`.
- Original raw impulse experiment:
  `benchmarks/acoustics/r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json`.
- Original q[0]=1 fixed-grid dt-half study:
  `benchmarks/acoustics/r130d_impulse_time_refinement_evidence_2026-10-09.json`.
- Executable:
  `scripts/run_r130d_impulse_source_moment_probe.py`.
- Replay/checks:
  `backend/tests/test_r130d_impulse_source_moment_evidence.py`.

**Original R130D full-band impulse is SELF_CONVERGENCE_FAILED;
general nonconvex CAD, measured room/BRAS NOT_VALIDATED;
PR #1055 DRAFT, issue #938 OPEN, product NO_GO.**
