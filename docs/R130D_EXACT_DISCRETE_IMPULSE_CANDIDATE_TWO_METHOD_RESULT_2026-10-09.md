# R130D #938 — Original unit-discrete-impulse source on new FV and independent MFEM P2

**Date:** 2026-10-09. **Result: NUMERICAL FAIL under preregistered exact impulse.**
This report records **actual acoustic time integration**, not a retuned
Gaussian source, smoothing, or renamed diagnostics.

## What was actually changed

- The experimental embedded Neumann FV solver now accepts and actually
  integrates **arbitrary explicit source samples** in
  `discrete_source_complex_transfer`. In particular, the original
  source sample sequence `q[0]=1 m³/s; q[1..999]=0` enters the
  wave equation as `c²*b*q[n]` each implicit-midpoint step, not as an
  inferred Gaussian convolution or a changed initial velocity.
- Source and pressure finite-record transforms both use
  `dt * Σ exp(+i 2π f (n+1/2)dt)` and `P_T/Q_T`; frequency bins are
  the frozen 40 and 80 Hz. Record duration = 0.25s, dt=0.00025s.
- The independent reference uses the **permanent original pinned
  MFEM P2 tetrahedral CSR M,K and source/receiver functionals** at r1,
  r2, r3 and r4. It injects the **same sampled original impulse
  time series** using its own Jacobi-PCG midpoint integration and
  strict true linear residual tolerance 1e−8. No FV matrix or face
  aperture is shared.
- The physical room is the original 4×4m sloped polyhedron
  z+y/4≤4 (56m³), source (1.5,2,2)m, receiver (2.5,2,2)m,
  speed343.2m/s, density1.2kg/m³, rigid Neumann boundary.
- The complete plan
  `benchmarks/acoustics/r130d_exact_discrete_impulse_new_solver_plan_2026-10-09.json`
  was committed in advance of the actual signal experiments, with
  all levels, bins, metrics, thresholds and product authority frozen.
  Actual evidence:
  `benchmarks/acoustics/r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json`.

### True impulse and independent response by refinement

| New embedded FV grid | Active DOFs | 40Hz complex transfer (Pa per m³/s) | 80Hz complex transfer |
|---:|---:|---|---|
| n12 | 1,584 | 32.9881 + 29.4003i | 90.2009 − 42.6011i |
| n16 | 3,712 | 63.4432 + 9.0222i | 143.8331 − 138.3386i |
| n20 | 7,200 | 52.0306 + 16.3372i | 124.2371 − 148.1518i |
| n24 | 12,384 | 44.7177 + 6.9791i | 109.0407 − 178.0864i |
| n28 | 19,600 | 55.2327 + 4.1073i | 115.0356 − 191.7267i |
| n32 | 29,184 | 101.1132 + 3.0064i | 158.1210 − 197.6113i |

| Independent pinned MFEM P2 refinement | DOFs | 40Hz | 80Hz |
|---:|---:|---|---|
| r1 | 125 | 8.0370 + 14.3838i | −49.9941 + 4.8780i |
| r2 | 729 | 57.2210 + 3.4208i | 44.9317 − 211.1097i |
| r3 | 4,913 | 118.2806 + 1.9756i | 156.7877 − 213.0454i |
| r4 | 35,937 | 73.3650 − 1.6945i | 107.1927 − 218.9262i |

Each FEM solve actually completed 1,000 implicit-midpoint steps. All
true CG residuals were <1e−11 at the worst step; CG maximum
iterations by r1/r2/r3/r4 were 39/43/37/34. Neither FEM's spatial
nor its linear failure was silently ignored.

### Frozen self- and cross-method criteria

| Gate | Measured normalized complex L2 | Frozen limit | Status |
|---|---:|---:|---|
| FV n24→n28 | 0.0801442 | trend only | — |
| **FV n28→n32** | **0.2319674** | ≤0.20 **and strict decrease** | **FAIL** |
| MFEM r2→r3 | 0.4398683 | trend only | — |
| **MFEM r3→r4** | **0.2642494** | ≤0.05 **and strict decrease** | **FAIL** |
| FV n32 vs MFEM r4 | 0.2434267 | ≤0.35 | PASS, isolated metric |

FV finest magnitude relative max = **0.452488** (limit ≤0.25).
Independent MFEM finest magnitude max = **0.612016**
(limit ≤0.08), max phase = **10.26295°** (limit ≤5°).
Cross method's maximum magnitude ratio = **0.378462**
(limit≤0.40), 2.78790 dB (≤3 dB), and maximum phase
**12.57773°** (≤25°).

The **isolated cross-method pass is not an overall pass**:
FV self fails, independent P2 FEM self fails, the complex FV
n24→28 → n28→32 trend worsens 0.08014→0.23197, and no
single failing metric is masked or excluded.
Accordingly
`exact_unit_impulse_candidate_numerical_pass=false`.

## Interpretation and limits of inference

This exposes the **source-spectrum / spatial convergence problem** that
the previous 3-ms Gaussian drive does not exercise in the same way.
The sampled unit impulse excites much higher spatial/temporal
frequencies. Their finite-record contribution and point-source
singularity can affect the observed 40/80-Hz finite-window transfer
even though those bins themselves are below the nominal grid limit.
The numerical results do not by themselves prove which contribution
dominates.

The experiment shares the original discrete q[0]=1 input shape
but uses the new candidate FV method and implicit-midpoint 0.25ms
time grid, not the original PFFDTD's complete historical PPW
configuration and full frequency-band evidence; it therefore cannot
clear the original canonical R130D validator, which remains
`SELF_CONVERGENCE_FAILED`.

Reproducible execution:

```pwsh
$env:PYTHONPATH = 'backend/src'
python scripts/run_r130d_exact_discrete_impulse_candidate.py `
  --plan benchmarks/acoustics/r130d_exact_discrete_impulse_new_solver_plan_2026-10-09.json `
  --systems benchmarks/acoustics/r130d_mfem_independent_sparse_systems `
  --output scratch/exact_unit_impulse_replay.json
```

Additional unit tests confirm generic actual q[n] injection gives
identical Gaussian results to the previous frozen candidate Gaussian
routine, with no change to its previously qualified evidence. Invalid
waveforms and absent frequency-domain input amplitude fail closed.

**Next genuine physics/numerics research path:** isolate the
unregularized spatial point impulse response using an explicitly
preplanned higher-order conservative finite-volume/finite-element
accuracy study and multiple source–receiver distances; compare
time step refinement separately from spatial refinement, without
changing the canonical q(t), output bins or acceptance limits.
Independent MFEM should be refined past r4 only with explicit
runtime/DOF bounds; current r4 self-convergence demonstrably fails.
Any new acceptance protocol must be prospectively published.

**No production authority change.** Previous Gaussian-source
restricted numerical PASS for three convex rooms still exists
but **does not apply to the original impulse**. Original
PFFDTD/independent full-band numerical acceptance:
`SELF_CONVERGENCE_FAILED`. Physical BRAS/owned-room:
`NOT_VALIDATED`. Product: `NO_GO`. PR remains DRAFT;
issue #938 remains OPEN.
