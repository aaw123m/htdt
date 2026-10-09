# R130D #938 — independent P2 MFEM for two extra convex rooms (2026-10-09)

## Executive numerical result and limitations

The new conservative exact-cut-cell FV solver and an independently
compiled **pinned MFEM P2 tetrahedral solver** both actually integrated
the same 250-ms Gaussian volume-velocity wave input at 40/80 Hz for two
additional rooms: a two-plane wedge and a fully oblique XYZ plane.
Their **preregistered n20 FV / MFEM refinement 4 numerical comparison
passes the existing, unchanged thresholds** for both geometries.

This supports candidate numerical correctness across **three
predeclared rigid convex polyhedra including the original sloped R130D**,
but **not original full-impulse PFFDTD, nonconvex CAD or acoustically
measured room validity**. Product `NO_GO` and original run25
`SELF_CONVERGENCE_FAILED` remain unchanged.

## Independent 3D FEM input provenance

The independent comparator was not generated from the cut-cell FV
matrix or face apertures. It independently constructs 3D tetra meshes
from scipy `HalfspaceIntersection`, `ConvexHull` and `Delaunay`
on the analytical halfspaces, verifying positive oriented tetrahedral
Jacobians and volume sums. The tetra source files and exact SHA-256
are versioned under
`benchmarks/acoustics/r130d_convex_mfem_tetra_sources/`.

- Wedge: 10 distinct vertices, 9 original tetrahedra, volume
  exactly `149/3 m³`. Tetra source SHA-256
  `5ce39370b424c6a2bc66b444ce64c816e59476348cfa62dfc00f4e74cdacf51f`.
- XYZ-diagonal: 7 vertices, 5 original tetrahedra, volume
  exactly `160/3 m³`. Tetra source SHA-256
  `24e6e3fca8120c55a15411e7eedd78b906fead2af94d60b15978c8be5f206376`.

The **actual original MFEM C++ implementation** is pinned to source
`mfem/mfem@d964264cdb9a13e94a201b6c236c7721e0c8765f`.
A separate exact-geometry C++ export binary
`r130d_mfem_convex_tet_system` was successfully built independently
on GitHub Actions run
https://github.com/ka0923s-a11y/HTDT/actions/runs/37865427351
and exported FEM P2 sparse mass matrices, c²-stiffness, and
source/receiver delta functionals for **both rooms and four levels**
r1/r2/r3/r4, i.e., **8 actual distinct FEM wave integrations**.

The same exact physical condition as FV was used: natural rigid
Neumann wall, source xyz (1.5,2,2)m, receiver xyz (2.5,2,2)m,
c=343.2m/s, rho=1.2kg/m³, q(t) Gaussian center=12ms sigma=3ms,
actual midpoint source injection into `M*phi_tt+K*phi=c²*b*q(t)`,
implicit-midpoint dt=250µs for 1000 steps, identical finite-window
`P_T/Q_T` at BOTH fixed 40/80-Hz bins, exp(+iωt) kernel.

The FEM temporal system was solved using Jacobi-preconditioned sparse
CG with rtol1e−11, atol0, cap350, and a measured true relative
linear residual acceptance of ≤1e−8 on **every step**. The independent
C++ export SHA-256 and source mesh SHA are preserved in every row.

### Pipeline provenance and first failure retained

The first complete 8-level GitHub Actions execution
`37865427351` completed all eight FEM numerical integrations and
uploaded the actual 8-level output artifact
`r130d-convex-two-geometry-independent-mfem-actual-drive`
(ID `11588461795`). Its final job status was
**FAILURE solely in the Python cross-comparison collector**:
the existing FV evidence used
`actual_driven_transfer_40_80_hz`, while the independent FEM evidence
used `transfer_complex_40_80_hz`. The collector incorrectly required
the FEM field on the FV record, raising `KeyError`.
This is a **test orchestration/schema bug**, not a fabricated PASS.
The exact collected eight immutable numerical results were re-used with
a fixed, strict dual-schema reader, unmodified source or tolerances,
and the computed outcome was subjected to independent evidence tests.

A separately pinned full continuous-integration rerun of the corrected
collector remains necessary to claim the entire C++ workflow SUCCESS;
the original failure log is retained and should not be rewritten.

## Measured preregistered numerical limits

MFEM quadratic tetrahedral `r2→r3→r4` checks require ALL complex,
magnitude and phase errors strictly decrease and the **r3→r4**
finest errors be at most respectively:
complex RMS0.05, max relative magnitude0.08, max phase5°.
FV n12→16→20 self-convergence must pass original FV limits.
The fine–fine `FV n20 vs MFEM r4` cross thresholds are
complex0.35, relative magnitude0.40, max dB3, max phase25°.

| Physical room | MFEM r3→r4 complex error | MFEM r3→r4 max phase | FV n20 vs independent MFEM r4 complex error | FV/MFEM max phase |
|---|---:|---:|---:|---:|
| Two-plane wedge | **0.0092396** | 0.5192° | **0.1344899** | 9.5686° |
| Three-axis diagonal | **0.0167092** | 0.9539° | **0.1772814** | 10.1232° |

These **pass the prospectively frozen candidate-only self and
cross numerical gates**, without excluding any bin, fitting source
normalization or retuning numeric tolerances. The original impulse
data are unchanged, so product status remains `NO_GO`.

## Supplemental finer FV n32 vs independent FEM r4

A distinct **prospective FV n24/n28/n32** plan (registered before
execution) was compared, after the fact, to the independently driven
MFEM r4 reference. These are useful numerical discrepancy diagnostics,
but **do not promote a blanket monotone magnitude self-convergence
claim**.

| Geometry | FV n20 vs MFEM r4 complex | FV n32 vs MFEM r4 complex | FV n32 vs MFEM r4 max phase |
|---|---:|---:|---:|
| Wedge | 0.134490 | **0.055567** | 3.9801° |
| XYZ-diagonal | 0.177281 | **0.069499** | 3.9771° |

The new fine-grid FV amplitude self-difference sequence is NOT
monotonic: wedge n24→28 0.008034 to n28→32 0.012331, and diagonal
n20→24 0.005151 to n24→28 0.014794. This failure to demonstrate
**strict monotonic convergence of all three FV metrics over the
entire n20→32 series** is explicitly captured as
`new_geometries_high_resolution_full_monotonicity_gate=NOT_QUALIFIED`.
The original *predeclared n20 candidate gate* is a separate limited
PASS and its tolerances are never weakened.

## Actual evidence and replay entrypoints

- `benchmarks/acoustics/r130d_convex_independent_mfem_plan_2026-10-09.json`
- `benchmarks/acoustics/r130d_convex_mfem_tetra_mesh_provenance_2026-10-09.json`
- `benchmarks/acoustics/r130d_convex_independent_mfem_all_level_driven_evidence_2026-10-09.json`
- `benchmarks/acoustics/r130d_convex_independent_mfem_same_source_evidence_2026-10-09.json`
- `benchmarks/acoustics/r130d_convex_high_fv_vs_mfem_r4_supplemental_2026-10-09.json`
- `scripts/run_r130d_convex_independent_mfem_drive.py`
- `scripts/assess_r130d_convex_high_fv_mfem.py`
- `backend/tests/test_r130d_convex_independent_mfem_contract.py`
- `backend/tests/test_r130d_convex_mfem_actual_evidence.py`

The reported numerical comparators can be re-evaluated directly
from the eight pinned, SHA-identified actual FEM results and frozen
FV evidence. A fresh underlying MFEM spatial matrix assembly requires
the pinned independent MFEM C++ source and original committed tetra
meshes; those are reproducible via the separate Windows workflow.

## Strictly outstanding

- The existing R130D original **full-band discrete impulse / PFFDTD**
  remains nonconvergent, in particular run25/run76 and later PPW44
  evidence. The Gaussian is a different declared source/physical
  observation, not a repair of the original operator.
- A genuine arbitrary CAD importer must handle concave domains,
  openings, internal baffles, invalid topology and material/impedance
  interfaces. The new `.obj` importer **only** supports watertight,
  outward oriented **convex triangular room shells**.
- Finer FV amplitude monotonicity and near-cut nonorthogonal
  truncation analysis need additional refinements, not relaxed limits.
- No physical measured-room data were provided and no actual
  BRAS/owned-room calibration/holdout performed. #801/#809 are
  separate hard requirements.
- Production CAD wave dispatch remains unchanged. Explicit
  candidate opt-in and source-file provenance have been implemented
  but do not grant recommendation eligibility.

**Verdict:** 2 additional rigid convex polyhedra, n20 changed-source
candidate independent numerical PASS. Finer-grid *full monotonicity*
NOT_QUALIFIED. Generic CAD and production NOT_VALIDATED/NO_GO.
