# R130D #938 — candidate numerical convergence result (2026-10-09)

## Exact scope and decision

The **new** conservative planar embedded-boundary finite-volume Neumann solver,
combined with an **actually injected** Gaussian volume-velocity source, has
**passed the separately preregistered, bounded candidate numerical gate** on
the 56 m³ sloped rigid room at 40 and 80 Hz. Both the new FV method and the
independently built MFEM quadratic FEM reference exhibit convergent numerical
refinement under the same Gaussian-source experiment; their finest-grid
transfer discrepancy is within the frozen cross-method tolerances.

**This does not repair or pass the original R130D full-band discrete impulse
reference, does not generalize to arbitrary 3D CAD solids and does not grant
production or physical/BRAS validation.** Keep #938 open and both previous
canonical `SELF_CONVERGENCE_FAILED` verdicts immutable.

### Physical and numerical experimental contract

- Exact sloped rigid polyhedron (56 m³), Neumann boundary.
- Source at (1.5, 2, 2) m, receiver at (2.5, 2, 2) m.
- Air c=343.2 m/s, rho=1.2 kg/m³; actual injected Gaussian
  `q(t)=exp[-0.5((t−0.012)/0.003)²]` m³/s in **both** solvers.
- Wave equation `M phi_tt + K phi = c² b q(t)`, pressure
  `p=rho*rᵀ phi_t`; implicit midpoint with dt=0.25 ms for 1,000 steps,
  250-ms finite rectangular record, Fourier kernel `exp(+i2π f t)` and
  complex source normalization P_T/Q_T at 40 and 80 Hz.
- FV exact clipped cut cell fluid volume / open face areas; mesh
  n=20,24,28,32. MFEM independent pinned P2 conforming tetrahedral
  Galerkin with 729→4,913→35,937 DOF.
- No fitted amplitude, phase, adjusted bins, time window, or threshold.
  The candidate numerical thresholds are copied exactly from the original
  R130D plan, with an explicitly separate physical contract.

### Measured immutable result

| Candidate self-convergence | complex L2 relative | max magnitude relative | max phase difference |
|---|---:|---:|---:|
| FV n20→n24 | 0.068819 | 0.047524 | 3.081° |
| FV n24→n28 | 0.037275 | 0.027048 | 1.581° |
| **FV n28→n32** | **0.025719** | **0.016112** | **1.205°** |
| MFEM r2→r3 | 0.181413 | 0.080663 | 9.256° |
| **MFEM r3→r4** | **0.006040** | **0.007154** | **0.486°** |

Frozen candidate limits: FV 0.20/0.25/15°, MFEM 0.05/0.08/5°,
respectively; all three adjacent error metrics decrease.

| Fine–fine cross solver | complex L2 relative | max magnitude relative | max magnitude dB | max phase |
|---|---:|---:|---:|---:|
| **FV n32 vs MFEM r4** | **0.077216** | **0.047189** | **0.419860 dB** | **3.736°** |

Frozen cross-method limits: 0.35, 0.40, 3.0 dB, 25°.

**Pre-registered numerical decision**:
- `fv_self_state: SELF_CONVERGENCE_PASS_CANDIDATE_ONLY`
- `mfem_self_state: SELF_CONVERGENCE_PASS_CANDIDATE_ONLY`
- `cross_solver_state: CROSS_SOLVER_PASS_CANDIDATE_ONLY`
- `candidate_numerical_evidence_state: EXPERIMENTAL_CANDIDATE_NUMERICAL_PASS_ONLY`
- `production_enabled: false`
- `canonical_r130d_impulse: SELF_CONVERGENCE_FAILED`
- `physical_validation: NOT_VALIDATED`.

Actual evidence:
`benchmarks/acoustics/r130d_mfem_ref4_same_drive_v2_evidence_2026-10-09.json`,
`benchmarks/acoustics/r130d_candidate_same_source_numerical_pass_2026-10-09.json`.
FEM P2 ref4 sparse matrices are permanently stored in
`benchmarks/acoustics/r130d_mfem_independent_sparse_systems/mfem-r4.json.gz`;
the **decompressed** source file's SHA-256 is
`e1c67d02db77a6e8775a0a59d8e75fa996434d58f8efae077f4cec2cf2c831ed`.

### Independent MFEM provenance and numerical solver correction

The separate pinned C++ r4 exporter was compiled from
`mfem/mfem@d964264cdb9a13e94a201b6c236c7721e0c8765f`
on GitHub Actions run
https://github.com/ka0923s-a11y/HTDT/actions/runs/37860268237 .
It produced a 57,335,240-byte sparse JSON file with 35,937 DOFs and
24,576 tetrahedra. Importantly, the **first** r4 execution failed its
own true linear residual bound:
`4.4667902057047184e-7 > 1e-8`. This failure is preserved.

Under a separately versioned, prospectively committed v2 *solver-only*
plan, conjugate-gradient absolute tolerance was tightened from `1e-12`
to `0` (relative `rtol=1e-11`, Jacobi preconditioner, `maxiter=350`,
and `true_residual_relative_max=1e-8` **unchanged**). All source physics,
FEM matrices, observation coordinates, sampling, refinement plan, numerical
acceptance thresholds remained unchanged.

The actual v2 numerical run completed all 1,000 steps:
- Max preconditioned-CG iterations per step: **32** / 350 allowed.
- Mean iterations: **20.338**.
- Worst true relative linear residual: **9.9833e-12**, well below
  the fixed `1e-8` ceiling.
- MFEM r4 complex P_T/Q_T at 40 Hz:
  `14.4129225100 + 39.9211693271i`; at 80 Hz:
  `84.3941629439 − 164.6323109125i`.
- A byte-hash check binds every numerical result to the original
  independently compiled r4 FEM matrices.

No relaxation of the physical/numerical acceptance was performed.

### Outstanding after this bounded candidate pass

1. Generalize exact cut-cell clipped planes to **arbitrary CAD room solid
   walls, joints and cut-cell slivers**. Verify discrete conservation,
   stability, consistency, geometry provenance and resource ceilings under
   several independent geometries. Current method only implements a single
   analytic planar roof.
2. Define and register the new band-limited source and solver authority
   behind an explicit opt-in candidate route, with **production off** until
   geometry stress validation, automated qualification, acceptance of
   the new physical contract and external measurements are complete.
3. Additional source/receiver placements and frequency bins chosen
   **before** testing, to catch degeneracy or resonance sensitivity.
4. Physical evidence, BRAS and owned-room verification #801/#809 remains
   independent.
5. The old full-band impulse protocol #938/#1051 is still nonconvergent,
   so it cannot be marked pass merely because the new bounded source/
   numerical method passes.

This report should be read as **numerical method qualification for one
preregistered bounded experiment**, not acoustic production acceptance.
