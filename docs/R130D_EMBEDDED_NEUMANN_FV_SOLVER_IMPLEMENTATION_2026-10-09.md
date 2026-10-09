# R130D #938: conservative cut-cell Neumann FV solver prototype — 2026-10-09

## Scope and gate disposition

**New numerical method implemented and executed; canonical HTDT self-convergence remains FAILED.**

This proposal implements a new experimental Python/SciPy finite-volume wave
operator for the exact R130D sloped 4 × 4 m rigid polyhedron
(`0≤x,y≤4`, `0≤z≤4-y/4`, 56 m³).

- New source: `backend/src/htdt/r130d_embedded_neumann_fv.py`.
- Same sound speed 343.2 m/s and density 1.2 kg/m³ as R130D.
- The *pinned PFFDTD production adapter, prior MFEM operator, run25 thresholds
  and frozen full-basis impulse source are **not** modified.*
- The smooth 3-ms Gaussian drive is actually injected as a force in the
  new solver's time-stepping loop; no postprocessing or favorable-bin fit.
- This implementation is a **candidate solver authority**, not production,
  and results here are not interchangeable with the original impulse
  acceptance contract.

The separately preregistered continuation plan
`benchmarks/acoustics/r130d_embedded_neumann_fv_refinement_plan.json`
was committed **before** the 28/32-level solver runs. The baseline
12/16/20/24-level evidence SHA-256 is
`6ab61d30545ace0b1475e023942068c83b857d9891f69531101a323fadd61c1f`.

## Numerical method: exact cut geometry + conservative flux

For each grid cell, the area of the **exact intersection** of its
`yz` rectangle with the inclined roof half-plane is found by clipping.
The fluid volume equals this polygon area times the `x` cell width.
Open interface faces in `x`, `y` and `z` use their **geometrically
clipped** fluid areas, not a boolean voxel occupancy mask.

The positive-semidefinite wave stiffness is assembled from undirected
pairs of neighboring cells:

`K = c² ∑ (A_face / h) (e_i - e_j)(e_i - e_j)^T`

and the mass is `M = diag(V_fluid)`. At the true sloped rigid roof
there is no cross-boundary edge, implementing zero flux (Neumann).
The operator is symmetric, conservative and has an exact constant
null mode. A cut-cell volume can be much smaller than `h³`; thus an
**implicit-midpoint** integrator was implemented to avoid the
small-cell explicit CFL stability issue while conserving undriven
discrete energy:

`(M + dt² K/4) phi[n+1] = (M - dt² K/4) phi[n] + dt M v[n] + dt² F_mid/2`.

This remains a **low-order two-point face-normal approximation**: an
oblique cut-face can introduce nonorthogonal flux inaccuracies not
removed by exact cut volumes alone. A standalone convergence check
with independent MFEM remains necessary.

## Executed operator checks

| cells per axis | active degrees of freedom | first nonzero frequency (Hz) | 50-step relative energy drift |
| ---: | ---: | ---: | ---: |
| 6 | 204 | 41.4941 | 2.23e-15 |
| 8 | 480 | 41.7078 | 3.70e-14 |
| 12 | 1584 | 41.8475 | 3.59e-14 |
| 16 | 3712 | 41.8966 | 2.46e-13 |

Pinned independent MFEM r3 reports approximately **41.9606 Hz** for
the corresponding first positive acoustic mode. This is a consistency
indicator, not proof of convergence to exact continuum eigenvalues.

Full operator benchmark evidence:
`benchmarks/acoustics/r130d_embedded_neumann_fv_eigen_evidence_2026-10-09.json`.

## Actually driven band-limited response

Identical source at (1.5,2,2) m and receiver (2.5,2,2) m;
source `q(t)=exp(-0.5*((t-0.012)/0.003)²)` is **injected into the new
wave equation** as `c² b q` at each midpoint. Pressure is
`rho*r^T phi_t`, with source/receiver trilinear interpolation
and a common finite-record `P_T/Q_T` complex Fourier kernel.
Record `T=0.25 s`, `dt=0.00025 s`, evaluation bins 40/80 Hz.

| adjacent spatial grid | normalized complex L2 difference | max phase difference |
| --- | ---: | ---: |
| 12→16 | 0.379470 | 12.244° |
| 16→20 | 0.134207 | 5.467° |
| 20→24 | 0.068819 | 3.081° |
| **24→28** | **0.037275** | **1.581°** |
| **28→32** | **0.025719** | **1.205°** |

The final two rows were preregistered as an extension *before*
executing them. All adjacent normalized complex differences from
12→16 down to 28→32 decrease monotonically on this fixed-source
experimental solver. This is **evidence of bounded candidate
self-convergence**, not a completed independent reference/production
qualification. No solver was calibrated to the observed values.

Initial actual driven evidence:
`benchmarks/acoustics/r130d_embedded_neumann_fv_driven_baseline_2026-10-09.json`.
Follow-up:
`benchmarks/acoustics/r130d_embedded_neumann_fv_driven_followup_2026-10-09.json`.

## Tests and strict unresolved work

21 targeted Python tests passed after adding the measured 28/32
evidence guards, covering exact 56 m³ volume, flat-box Neumann
eigenvalues, sloped eigen refinement, symmetric positive flux,
constant null mode, undriven Hamiltonian, actual forced solve,
resource/geometry failures and the predeclared follow-up identity.

**Not yet established:**

1. An independent FEM/BEM **under the identical Gaussian source** and
   exact 250-ms finite record. Existing pinned MFEM run25 is impulse
   full-basis and is not a like-for-like comparison.
2. Conformal cut-cell consistency on arbitrary CAD solids and
   nonorthogonal boundary faces, robust small sliver handling and
   large-room sparse-factorization resource bounds.
3. At least three additional high levels or a robust Richardson-order
   estimate around *continuum transfer* (monotone differences alone
   are not a mathematical convergence theorem).
4. A physically predeclared tolerance for the **new** operator and
   input waveform. Applying the old R130D thresholds or promoting
   this test to current R130D qualification is prohibited.
5. Production integration: versioned solver capability, geometry
   provenance, source contract, CPU/memory budgets and explicit
   risk-gated switching; independent measurement/BRAS validation
   #809/#801.

**Current status:** new numerical kernel implemented, measured
and tested; candidate monotone 12→32 spatial refinement observed.
Frozen run25 PFFDTD `SELF_CONVERGENCE_FAILED` and MFEM
`SELF_CONVERGENCE_FAILED` remain. Cross-solver `BLOCKED`,
production `NOT_VALIDATED`. Keep #938 open.
