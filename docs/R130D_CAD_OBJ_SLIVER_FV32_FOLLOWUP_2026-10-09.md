# R130D #938 — bounded OBJ air geometry, sliver stability and convex n32 evidence

## Engineering result / authority status

**Three additional pieces of actual solver functionality were implemented.**

1. Import a watertight, orientable, triangulated **convex OBJ air-surface**
   and derive the explicit oriented half-space planes used by the
   exact cut-cell finite-volume solver, preserving the original OBJ
   SHA-256. **Arbitrary nonconvex CAD, curved walls, openings and
   multiple air compartments are NOT supported**; fail closed.
2. Execute an opt-in, versioned, provenance-bearing **experimental-only
   candidate wave solver entrypoint**. The existing CAD candidate-wave
   production dispatcher and its run25 PFFDTD source/provenance
   contract have not been changed or switched.
3. Detect resolvable near-zero **positive** air cut-cell volumes and
   reject them instead of silently deleting a valid sliver. Retain
   the benign roundoff zero-volume case when an entire voxel only
   touches a physical plane at a vertex. Stress test the retained
   small masses using the existing implicit-midpoint wave integrator.

All supporting input fixture files, actual Gaussian-source numerical
output, prospective numerical plan, tests and standalone scripts are
committed as part of draft PR #1055.

### OBJ import mathematical authority and integrity

`backend/src/htdt/r130d_convex_obj_import.py` accepts only a bounded
ASCII `.obj` file containing `v x y z` and `f i j k` lines. It
enforces finite coordinates in the configured 4-metre cube, positive
triangle indices, one manifold closed shell (every undirected edge
incident on exactly two opposing directed faces), nonzero facet area,
consistent outward normals and supporting *convexity* of all triangles
against all vertices. It verifies the oriented signed closed-mesh
volume against an independent SciPy convex hull, deduplicates
coplanar faces and rejects unsupported materials, holes, nonconvexity,
curved tessellations, duplicate vertices and excessive resources.

The only imported `.obj` files in this experiment are deterministic
**synthetic fixtures** generated from independently produced convex
tetrahedral meshes; these are *not claimed to be actual user room
measurements or production CAD exports*.

| Imported fixture | SHA-256 original OBJ bytes | Triangles | Independent analytic volume |
|---|---|---:|---:|
| planar wedge | `396f7f6e6119a7e7062f01c3af32827e0ecefb985f434c488363e6609cb5babb` | 16 | 149/3 m³ |
| 3-axis diagonal | `3cd0e5eead16208461e88a03ee6c41d39545e1b0fcb71b5d7027bb95452d58cb` | 10 | 160/3 m³ |

The OBJ-derived exact-cell FV mass/stiffness agrees with the
independently supplied half-space room geometry at n=8 to
<1e-11 relative and the actual 250-ms two-bin transfer agrees with
the prerecorded independent plane input test.

### Opt-in code path

`backend/src/htdt/r130d_experimental_convex_obj_wave.py` and
`scripts/run_r130d_experimental_convex_obj_wave.py` provide a
**deliberately isolated diagnostic execution route**, version
`htdt.r130d.convex-obj-cutcell-neumann-experimental/1`.
No production dispatch capability registration is made.

Example:

```pwsh
python scripts/run_r130d_experimental_convex_obj_wave.py `
  --experimental-only `
  --obj benchmarks/acoustics/r130d_convex_obj_fixtures/planar_wedge.obj `
  --cells-per-axis 12 `
  --output scratch/wedge_experimental_output.json
```

The actual execution verified the source OBJ SHA-256 and produced
complex P_T/Q_T for 40/80 Hz at n=12:

- 40 Hz: `−34.6961589476 + 36.5642057304i`
- 80 Hz: `23.1170269731 − 47.9578018895i`.

Every result binds the explicit source identity, source/receiver
positions, exact geometry planes, original imported OBJ bytes digest,
time window and source waveform, and carries
`production_dispatch_registered=false`, `production_ready=false`,
`NOT_VALIDATED`, `SELF_CONVERGENCE_FAILED` (canonical impulse).

### Small cut-cell mass/CFL stress: actual implicit wave dynamics

Room clipping plane `x+y+z ≤ 4+delta*h` on an 8-cell axis grid,
h=0.5m, with a varied small positive `delta`. The Gaussian
source is *not* reinterpreted as a small-cell physics validation:
this is an undriven discrete Hamiltonian preservation stress.

| delta | minimum retained air fraction V/h³ | max undriven relative energy drift /100 steps |
|---:|---:|---:|
| 0.0100 | 1.66667e−7 | 2.665e−14 |
| 0.0010 | 1.66667e−10 | 5.940e−14 |
| 0.0005 | 2.08333e−11 | 1.688e−14 |
| 0.0002 | 1.33333e−12 | 2.565e−14 |

Time integration `dt=20ms`, 100 steps, deliberately much larger
than explicit CFL for the tiny masses. Midpoint remains stable and
discrete energy conserving **for this undriven semi-discrete model**.
Resolvably positive slivers at relative volume below the declared
`1e−12` threshold (delta=1e−5,1e−6) now **raise** rather than be
deleted; mere zero-volume vertex contacts remain inactive. This
does not imply accuracy of arbitrary slivers.

### Preregistered FV n20→24→28→32 same-source study

New physical plan
`benchmarks/acoustics/r130d_convex_high_resolution_followup_plan_2026-10-09.json`
was committed **before** executing the new n24/n28/n32 input-driven
wave steps. Source/receiver, 3-ms Gaussian volume velocity,
dt=0.25ms, duration250ms, c=343.2, rho=1.2 and the exact
40/80-Hz P_T/Q_T analysis are unchanged.

| Room | complex relative n20→24 | n24→28 | n28→32 | final max phase |
|---|---:|---:|---:|---:|
| 2-plane wedge | 0.044221 | 0.024618 | **0.018373** | 1.117° |
| 3-axis diagonal | 0.054851 | 0.031084 | **0.021849** | 1.239° |

**Important nonpass limitation:** per-bin max magnitude-relative
errors are *not* strictly decreasing across every pair.
Wedge n24→28 **0.008034** then n28→32 **0.012331**;
diagonal n20→24 **0.005151** then n24→28 **0.014794**.
No magnitude metric is dropped or substituted. This is additional
support for shrinking complex/phase self discrepancy, **not** a
blanket "all-three-metrics monotonic and cross-solver PASS."

FEM reference for the two new shapes is independently planned,
with pinned `MFEM@d964264c...` assembly and an alternate tetra
mesh generated from a separate HalfspaceIntersection/Qhull/Delaunay
algorithm. That comparison must actually execute and satisfy the
precommitted thresholds before reporting a new-shape numeric gate.

### Remaining and blocked

- Original PFFDTD full-band impulse acceptance remains failed.
- A validated **nonconvex/curved actual room CAD** adapter, boundary
  topology and parallel decompositions require separate engineering;
  this implementation rejects them intentionally.
- The new shape-independent same-input MFEM reference and critical
  higher-grid amplitude convergence must be reported without
  threshold retuning. A successful build alone is not numerical PASS.
- Real owned-room calibration/holdout or external BRAS corpus
  **has not been supplied as input or executed** for this candidate;
  #801 and #809 remain external physical credibility gates.
- Existing production solver dispatch stays unchanged and default-off.
  Neither optional diagnostics nor unit tests may bypass physical
  measurement and numerical authority.

**Recorded decision:** new geometry ingestion / sliver stability
checks and explicit opt-in diagnostic route IMPLEMENTED. New-shape
independent full numerical qualification PENDING;
general CAD `NOT_VALIDATED`; production `NO_GO`.
