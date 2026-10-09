# R130D #938 — dt-halving original unit-discrete-impulse at fixed FV/MFEM spatial grids

**2026-10-09** — preregistered before actual execution.

## Result

**Temporal refinement does not rescue the original-impulse convergence.**
The 3D exact sloped-roof FV and independently assembled pinned P2 MFEM
both show large shifts in 40/80Hz finite-time transfer as the time step
is halved from 250µs to 125µs at **fixed spatial discretization**.

| Fixed spatial level | Relative complex T(250µs)↔T(125µs) |
|---|---:|
| Conservative Neumann FV n20 | 0.169179 |
| FV n28 | 0.117264 |
| FV n32 | **0.566078** |
| Independent pinned quadratic MFEM r3 | **0.546239** |
| Independent pinned quadratic MFEM r4 | **0.515042** |

At 125µs fixed time discretization:

| Spatial/independent check | Relative complex L2 error | Frozen Gaussian-candidate complex threshold (diagnostic reference only) |
|---|---:|---:|
| FV n20→n28 | 0.173587 | No adjacent-grid gate; diagnostic |
| FV n28→n32 | **0.183079** | 0.20 (complex alone under limit, but amplitude/trend fail) |
| P2 MFEM r3→r4 | **0.671137** | 0.05 — FAIL |
| FV n32↔MFEM r4 | **0.698377** | 0.35 — FAIL |

At 125µs FV last adjacent max magnitude relative is **1.103433**
(limit0.25), even though complex L2=0.1831. Independent P2 r3→r4
max magnitude relative is **0.803756** (limit0.08), max phase
36.3939° (limit5°). FV n32 vs MFEM r4 cross max relative magnitude
0.899051 (limit0.40), max phase34.6447° (limit25°).
**No candidate or production impulse gate is passed.** The
original 250µs impulse had also failed both self-convergence
gates. Choosing the better of the two dt values to manufacture
a PASS is not permitted.

## Experimental contract: no changed acoustic authority

The source samples were always the actual discrete `q[0]=1m³/s,
q[n>0]=0` directly injected into the midpoint RHS
`c² b q[n]`; no Gaussian drive, taper, Fourier filter,
frequency-specific correction or fitted phase. Source (1.5,2,2)m,
receiver (2.5,2,2)m, room 56m³; sound speed343.2m/s,
density1.2kg/m³, rigid wall. Both full record lengths 250ms,
at 1,000 and 2,000 midpoint samples respectively, and both
40 and 80Hz P_T/Q_T exp(+iωt) analysis bins were retained.

The exact same independently hashed reference P2 matrices from
MFEM git `d964264cdb9a13e94a201b6c236c7721e0c8765f`
were used for r3 (4,913 DOFs) and r4 (35,937 DOFs);
no FV aperture data/matrix were transferred into MFEM.
Actual midpoint evolution reached 2,000 time steps for both
P2 references; Jacobi CG true relative residual worst <
1e−11, bounded max iterations 44/37. Fixed spatial FV
n20/28/32 similarly ran 2,000 true wave steps using the exact
mass/Neumann aperture operator.

### Crucial temporal-source caveat

This is a genuine *dt-halving under the same discrete q[0]=1
sample waveform* but **not a pure temporal-integrator truncation
error study**. The source pulse at the midpoint appears at t=125µs
when dt=250µs, and at t=62.5µs when dt=125µs. Its discrete-time
total integrated volume velocity also scales with dt; this constant
gain cancels in the normalized linear transfer P_T/Q_T but its
high-frequency spectrum and time placement still matter.
Therefore call each fixed-grid error the joint *time integration
+ sampled source time placement* response, not an isolated
integrator order or proof of a specific error mechanism.
The original fullband PFFDTD configuration is not rerun here.
An explicit source-moment-matched experiment, predeclared separately,
would be required to decouple pulse placement.

### Fixed prior experiments

- Baseline exact discrete impulse at dt250µs, all FV grids
  n12/16/20/24/28/32 and MFEM r1/2/3/4:
  `benchmarks/acoustics/r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json`.
- This dt-halving experiment plan was committed FIRST:
  `benchmarks/acoustics/r130d_impulse_time_refinement_plan_2026-10-09.json`.
- Newly actually executed 2,000-step data and all 40/80Hz
  true complex observations:
  `benchmarks/acoustics/r130d_impulse_time_refinement_evidence_2026-10-09.json`.
- Exact replay:
  `scripts/run_r130d_impulse_time_refinement.py`.
- Fail-closed evidence tests:
  `backend/tests/test_r130d_impulse_time_refinement.py`.
- Current 148 focused regression tests: all passed on local
  Windows Python3.12/NumPy1.26.4/SciPy1.14.1. A fresh
  CI replay should be reported separately only after it finishes.

## Actionable numerical development direction

1. **Source temporal moment matching** as an explicitly diagnostic
   alternative: match the delta time centroid at both dt grids
   without changing the integrated source strength. Must be
   preregistered separately; it is not the original q[0]=1 source.
2. **Independent time-integration order check** against direct
   resolvent or method-of-lines for a fixed spatial operator and
   a smooth source whose continuous timing does not move.
3. **Broadband point-source regularity** and nonorthogonal cut-cell
   near-wall dispersion must be assessed separately; pinned
   P2 FEM r4 itself is not converged for the current impulse.
4. Real nonconvex multi-air-domain CAD and actual measured-room
   calibration/BRAS holdouts remain absent.

**Strict frozen authority:** original impulse
`SELF_CONVERGENCE_FAILED`, fullband independent cross-method
`CROSS_SOLVER_BLOCKED`, measured room `NOT_VALIDATED`,
product `NO_GO`, PR #1055 draft, issue #938 open.
The separate smooth Gaussian source numerical candidate PASS
for 3 convex shapes is still narrower, unchanged and **cannot
override this original impulse failure**.
