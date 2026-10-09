# R130D #938 — convex multi-plane 3D cut-cell operator follow-up (2026-10-09)

## Status

**Implemented and actually executed**, with reproducible tests and pinned
geometry/evidence. The numerical kernel now handles a **bounded convex
polyhedral room described as the intersection of an axis-aligned box and
1–8 additional arbitrarily oriented planes**. It is not a general CAD
triangular mesh importer, not nonconvex/curved/multi-region geometry,
and **not production validated**. The original R130D independent impulse
numerical gate remains `SELF_CONVERGENCE_FAILED`, and the previously
qualified **single sloped room / Gaussian source candidate** remains
`EXPERIMENTAL_CANDIDATE_NUMERICAL_PASS_ONLY`.

No original PFFDTD production method, source contract or frozen acceptance
limit was modified. The generalized convex geometry is a new independent
module `backend/src/htdt/r130d_embedded_neumann_convex.py` that reuses
the already-tested finite-volume symmetric Neumann wave operator,
implicit midpoint time integration and real source injection.

## Mathematical method

Let air domain be `Omega = [0,L]^3 ∩ ⋂(a_j · xyz ≤ b_j)`, a convex,
closed, planar half-space intersection.

1. Each Cartesian voxel is classified fully inside, fully outside or
   intersecting the boundary by evaluating exact plane extrema on its
   axis-aligned bounding box.
2. Boundary voxels are clipped via 3D convex polygon Sutherland–Hodgman
   operations over all room planes, and closed using consistently
   oriented cap polygons. The **true geometric cell volume** is integrated
   via the signed oriented-surface divergence-theorem integral.
3. Every inter-cell Cartesian aperture is a **clipped 2D plane polygon**
   area. Physical oblique wall faces have no inter-cell edge; this is a
   rigid no-throughflow/Neumann boundary, not staircase wall reflection.
4. From these independent finite-volume geometric integrals the kernel
   assembles `M=diag(V_fluid)` and
   `K=c² sum(A_face/h)(e_i-e_j)(e_i-e_j)^T`. Energy
   `(phi_t^T M phi_t + phi^T K phi)/2` is conserved by the
   implicit-midpoint scheme in the absence of a source.
5. An independent **triple-plane vertex enumeration + SciPy Qhull**
   constructs and checks the volume of the *entire room*. A cell-mass
   sum that disagrees fails closed.
6. Invalid planes, degenerate/empty solids, resource bounds,
   disconnected cut-cell graph, source/receiver outside the physical
   convex room, and sub-threshold slivers are rejected. No false
   solidification of a thin cell is permitted.

**Limitations of this method:** two-point face-gradient flux uses cell
centre spacing `h`, not volume-centroid-to-centroid geometry; a strongly
nonorthogonal cut face can therefore still cause truncation error.
Concave walls, holes, internal baffles, curved walls and multiple
acoustic regions **must not** use this geometry authority. No hidden
solver dispatch or source/model equivalence is claimed.

## Preregistered geometry tests

Study plan was pushed to GitHub before the new numerical cases were
executed:
`benchmarks/acoustics/r130d_convex_multiplane_cutcell_plan_2026-10-09.json`.
The actual run used all predeclared grids 6, 8, 12, 16, 20,
source at (1.5,2,2)m and receiver at (2.5,2,2)m,
`c=343.2m/s`, `rho=1.2kg/m³`; an actual 3-ms Gaussian
volume-velocity drive is injected for 250ms at dt=0.25ms,
frequency bins 40/80Hz, no fitting/excluded data.

| Air room | Plane constraints beyond the 4m cube | Analytic volume | Measured cut-cell volume n20 |
|---|---|---:|---:|
| Baseline sloped | z + 0.25 y ≤ 4 | 56.000000 m³ | 56.000000 m³ |
| New 2-plane wedge | z + 0.25 y ≤ 4 and x+y ≤ 6 | 49.666667 m³ (149/3) | 49.666667 m³ |
| New 3-axis diagonal | x+y+z ≤ 8 | 53.333333 m³ (160/3) | 53.333333 m³ |

The same results hold for **all** specified 6/8/12/16/20 grid
resolutions. Baseline sloped volume and stiffness match the prior
analytical R130D finite-volume implementation at shared grid levels
to <1e-12 **relative**. That is an independent regression of the
generalized clipping algorithm against the original accepted
candidate spatial discretization.

## Actually driven numerical refinement

All transfer measurements are complex source-normalized P_T/Q_T
from the actual wave stepping loop (not impulse convolution):

| Air room | Complex relative error n12→16 | n16→20 | n16→20 max phase |
|---|---:|---:|---:|
| Baseline sloped | 0.379470 | 0.134207 | 5.467° |
| 2-plane wedge | 0.166301 | **0.065670** | 4.659° |
| 3-axis diagonal | 0.214474 | **0.095533** | 5.485° |

All three rooms show decreasing **complex relative L2** differences
through the preregistered sequence n6→8→12→16→20; the
2-plane wedge **phase** metric is nonmonotonic at n6→8→12
(16.071°→23.135°) and this is preserved, not hidden. The resulting
observation supports bounded candidate FV spatial refinement but
does **not** satisfy independent same-source MFEM reference requirements
for the two new geometries. Their per-geometry `CROSS_SOLVER_BLOCKED`
statuses therefore remain.

Across all 15 actual grid computations, the 60-step undriven implicit
midpoint discrete-energy relative error was below 6e-13, and the
Neumann constant-mode and matrix symmetry invariants were enforced.
A source-position-inside-convex-room check (including the oblique
side wall) is exercised in regression tests.

Raw, unselected complex values / all grids / per-bin phases are
committed in
`benchmarks/acoustics/r130d_convex_multiplane_cutcell_evidence_2026-10-09.json`;
re-execution command:

```pwsh
$env:PYTHONPATH = 'backend/src'
python scripts/run_r130d_convex_multiplane_cutcell_diagnostic.py `
  --plan benchmarks/acoustics/r130d_convex_multiplane_cutcell_plan_2026-10-09.json `
  --output scratch/convex_multiplane_replay.json
```

## Remaining criteria for real CAD and production

- Import real CAD boundary topology and **prove** watertight, orientable,
  non-self-intersecting manifold constraints. This operator **only**
  accepts intersected half-spaces, and can represent convex planar
  rooms only.
- Independently build the corresponding new wedge/diagonal MFEM
  reference *from those exact geometries and actual identical source*
  and refine both methods until frozen candidates can be assessed
  without reference contamination.
- Verify thin sliver conditioning, disconnected region behavior,
  floating precision under extreme nearly coplanar walls and
  nonorthogonal face-gradient consistency on finer grids.
- Add versioned, explicit opt-in solver dispatch and geometry/source
  provenance, with production routing **off** until tests and external
  physical evidence satisfy their separate approval contracts.
- BRAS/owned-room tests #809/#801 remain independent. The canonical
  original PFFDTD/MFEM full-band impulse validation remains failed.

**Gate:** `CONVEX_MULTIPLANE_CANDIDATE_ONLY`;
`NEW_GEOMETRIES_CROSS_SOLVER_BLOCKED`,
`GENERAL_CAD_NOT_VALIDATED`, `PRODUCTION_NO_GO`.
