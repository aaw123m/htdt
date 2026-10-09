# R130D #938 — same-source experimental candidate numerical PASS (2026-10-09)

## Decision and scope

**EXPERIMENTAL_CANDIDATE_NUMERICAL_PASS_ONLY**, for exactly the sloped
rigid room with a *new* planar cut-cell finite-volume (FV) operator
and *actually injected* 3 ms Gaussian volume-velocity source. Two
independent spatial solvers pass their bounded self-refinement tests,
and their same-source cross-method comparison is within **unchanged
original numerical tolerances**.

This **does not** change the canonical pinned PFFDTD/full-impulse
`SELF_CONVERGENCE_FAILED` nor the original MFEM/full-impulse gate.
Production eligibility remains false and physical validation
`NOT_VALIDATED`; BRAS and owned-room validation remain outstanding.

The numerical acceptance plan was preregistered and committed before
the independent MFEM r4 outcome:
`benchmarks/acoustics/r130d_candidate_same_source_numerical_acceptance_plan_2026-10-09.json`.

## Identical physical/time contract

- Exact sloped 56 m³ polyhedron: `0≤x,y≤4`, `0≤z≤4-y/4` m;
  rigid Neumann walls, sound speed 343.2 m/s, density 1.2 kg/m³.
- Volume-velocity input `q(t)=exp(-.5*((t-.012)/.003)^2)`
  **actually injected at each midpoint step** into both
  `M phi_tt + K phi = c² b q(t)`; source (1.5,2,2) m,
  receiver (2.5,2,2) m, output `p=rho*r^T phi_t`.
- `dt=0.00025 s`, 1000 midpoint steps, `T=0.25 s`,
  original 40 and 80 Hz bins without selectively omitting bins.
  `P_T/Q_T`, exp(+iωt) identical midpoint Fourier quadrature.
- FV: exact cut-cell fluid volume/open-face apertures, symmetric Neumann
  stiffness and energy-conserving implicit midpoint.
- MFEM: **independently compiled** P2 quadratic tetrahedral Neumann
  matrices and point functionals from pinned
  `mfem/mfem@d964264cdb9a13e94a201b6c236c7721e0c8765f`.
  Reference levels r2/r3/r4 are 729/4913/**35937 DOF**.

## Actual measured numeric acceptance

| Bounded test | Complex L2 relative | Max magnitude relative | Max phase | Limit / result |
| --- | ---: | ---: | ---: | --- |
| FV n20→24 | 0.068819 | 0.0475239 | 3.0812° | baseline |
| FV n24→28 | 0.037275 | 0.0270485 | 1.5815° | baseline |
| FV n28→32 | **0.025719** | **0.0161120** | **1.2046°** | ≤0.20 / ≤0.25 / ≤15°: PASS |
| MFEM r2→3 | 0.181413 | 0.0806631 | 9.2559° | baseline |
| MFEM r3→4 | **0.006040** | **0.0071543** | **0.4858°** | ≤0.05 / ≤0.08 / ≤5°: PASS |
| FV n32→MFEM r4 | **0.077216** | **0.0471885** | **3.7356°** | ≤0.35 / ≤0.40 / ≤25°: PASS |

Cross-solver max magnitude dB: **0.419860 dB**, below
the retained original 3 dB limit. All FV and MFEM adjacency
error metrics decrease strictly over their preregistered pairs.

The experimental candidate-only numeric gate returns:

```
EXPERIMENTAL_CANDIDATE_NUMERICAL_PASS_ONLY
FV: SELF_CONVERGENCE_PASS_CANDIDATE_ONLY
MFEM: SELF_CONVERGENCE_PASS_CANDIDATE_ONLY
CROSS: CROSS_SOLVER_PASS_CANDIDATE_ONLY
Canonical impulse: SELF_CONVERGENCE_FAILED
Physical validation: NOT_VALIDATED
Production enabled: false
```

## Exact independent MFEM r4 provenance and correction history

Independent MFEM Windows C++ P2 r4 compile and 24,576-tetra /
35,937-DOF CSR export **PASS** in
https://github.com/ka0923s-a11y/HTDT/actions/runs/37860268237.
The exported r4 JSON is 57,335,240 bytes with raw SHA-256:
`e1c67d02db77a6e8775a0a59d8e75fa996434d58f8efae077f4cec2cf2c831ed`.
Artifact ID `11585962934` and archived raw system preserve the
original independently compiled bytes.

That original job's Gaussian sparse-CG solve **FAILED** a controlled
true relative residual test: `4.4667902057047184e-7`
exceeded the frozen `1e-8` maximum, because `cg(atol=1e-12)`
could stop on an unphysically weak RHS. **The original failure is
retained**, not reclassified.

A **separate preregistered correction plan**
`benchmarks/acoustics/r130d_mfem_ref4_same_drive_solver_v2_plan_2026-10-09.json`
pinned the EXACT SAME independent r4 JSON SHA-256, mesh,
source, medium, bins and numerical tolerances; **only** CG
absolute tolerance was tightened from `1e-12` to `0`.
Relative solver tolerance `1e-11`, max iterations `350`
and post-checked true linear relative residual `1e-8`
remain unchanged. The re-execution on pinned Windows
NumPy 1.26.4/SciPy 1.14.1 completed 1000 steps,
using at most **32 CG iterations** per step and ~33.48 seconds.

Outputs (actual measured, not generated synthetic pass):
- `benchmarks/acoustics/r130d_mfem_ref4_same_drive_v2_evidence_2026-10-09.json`;
- `benchmarks/acoustics/r130d_candidate_same_source_numerical_pass_2026-10-09.json`;
- exact original r4 sparse system compressed losslessly:
  `benchmarks/acoustics/r130d_mfem_independent_sparse_systems/mfem-r4.json.gz`.
- `scripts/run_r130d_mfem_ref4_same_drive.py` and
  `scripts/run_r130d_candidate_same_source_qualification.py`,
  with tests verifying original retained thresholds and immutable
  source hashes. No r4 matrix fitting or source response rephasing.

## What remains before a production or general solver claim

1. **Generic nonplanar CAD and boundary conditions:** the experimental
   cut-cell implementation currently supports this one planar roof
   and rigid single-region domain only; no general triangular CAD,
   impedance/porous surfaces, curved walls, interfaces or doors.
2. **Numerical robustness:** demonstrate nonorthogonal cut-face flux
   consistency, tiny sliver conditioning/solver memory, geometry
   convergence and modal bandwidth across many rooms and source/receiver
   positions; prove solver errors do not merely cancel on two bins.
3. **Opt-in integration/identity:** add explicit versioned solver model,
   CAD compiler/provenance, resource guards, and source waveform contract
   to production dispatch before any adoption (no silent PFFDTD swap).
4. **Experimental and measured acoustic validation:** independent
   BRAS / owned-room tests (#809/#801) remain hard constraints.
5. Original frozen PFFDTD and MFEM full-impulse contract remains
   `SELF_CONVERGENCE_FAILED`; resolving it requires a new validated
   baseline or a separately approved physical-contract migration,
   not reclassifying old failures.

**#938 stays open. PR #1055 stays DRAFT.** This demonstrates a genuine
numerical candidate converging with two methods on one bounded new-source
experiment, not a general/proven physical solver or a production pass.
