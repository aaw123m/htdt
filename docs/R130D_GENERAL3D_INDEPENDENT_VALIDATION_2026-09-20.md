# R130D independent general-3D numerical validation

Date: 2026-09-20  
Issue: #101  
Draft PR: #282  
Starting main authority: `f7ba4d8b30605b60da914c5e497870b104dfc7fa`

## Purpose and authority boundary

PR #278 established actual pinned PFFDTD CPU execution of exact R120B single-region
closed polyhedra and proved that the sloped and concave fixtures do not silently
collapse to their bounding box. It deliberately left
`general_3d_physics_validated=false / NOT_VALIDATED` because those results had
no independent numerical reference.

This task adds a separate validation authority. It does not rewrite PR #278
historical execution evidence and does not change the production-facing R130D
geometry adapter.

The predeclared numerical plan is:

`benchmarks/acoustics/r130d_general3d_validation_plan.json`

The refinement schedules and acceptance thresholds in that file were committed
before the independent numerical workflow was executed. They must not be relaxed
after observing results.

## Independent fixture

The first bounded validation fixture is the exact PR #278 sloped closed
polyhedron:

- vertices: `(0,0,0), (4,0,0), (4,4,0), (0,4,0), (0,0,4), (4,0,4), (4,4,3), (0,4,3)` m;
- six explicit planar faces;
- the ceiling is non-axis-aligned;
- this is not a world rotation of a rectangular room;
- exact source position: `(1.5, 2.0, 2.0) m`;
- exact receiver position: `(2.5, 2.0, 2.0) m`;
- density: `1.2 kg/m^3`;
- sound speed: `343.2 m/s`;
- rigid zero-normal-velocity / homogeneous natural Neumann boundary.

The MFEM reference uses an independently authored six-tetrahedron conforming
partition around body diagonal `0 -> 6`:

`[0,1,2,6], [0,2,3,6], [0,3,7,6], [0,7,4,6], [0,4,5,6], [0,5,1,6]`.

Its audited total volume is `56 m^3`. MFEM uniform tetrahedral refinement is
then applied. PFFDTD voxelization, Cartesian grid code, containment code, and
triangle/box intersection code are not used to construct the reference mesh.

The receiver lies on a conforming *internal* tetrahedral facet of this fixed
partition. It remains strictly inside and away from the physical room boundary.
The reference uses continuous H1 finite elements, for which point evaluation is
single-valued across that conforming internal facet. An empty, invalid, or
otherwise non-corresponding MFEM receiver functional is fail-closed as BLOCKED.

## Compared physical quantity

The common comparison quantity is the finite-record transfer

`P_T(f) / Q_T(f)`

with units `Pa/(m^3/s)`.

Both transforms use:

- half-open record `[0,T)`;
- `T = 0.06 s`;
- frequencies `40 Hz` and `80 Hz`;
- phasor convention `exp(-i*omega*t)`;
- analysis kernel `exp(+i*omega*t)`.

PFFDTD's R130D artifact stores absolute complex pressure after multiplying its
finite-record transfer by the exact complex
`AcousticWaveExcitationAuthority Q(f)`. The validation runner divides by those
exact complex Q samples before comparison. No fitted amplitude scale, fitted
phase rotation, frequency shift, or post-hoc normalization is permitted.

The MFEM reference uses the same unit discrete volume-velocity impulse contract:
`q[0] = 1 m^3/s`, `q[n>0] = 0`. For velocity potential `phi`,

`M phi_tt + K_c2 phi = c^2 b q`

with the impulse represented as

`phi_t(0+) = c^2 dt M^-1 b q[0]`.

Pressure is reconstructed as `p = rho d(phi)/dt`. The same dt-weighted direct
finite-record transform is then applied to the MFEM pressure and source records.

## Independent MFEM reference

Pinned implementation:

`mfem/mfem@d964264cdb9a13e94a201b6c236c7721e0c8765f`

Reference discretization:

- serial MFEM;
- tetrahedral continuous Galerkin H1;
- polynomial order 2;
- natural homogeneous Neumann rigid boundary;
- `MassIntegrator`;
- `DiffusionIntegrator(c^2)`;
- point source and receiver functionals from
  `DomainLFIntegrator(DeltaCoefficient)`;
- full generalized symmetric eigenproblem `K_c2 v = lambda M v`;
- full basis retained, with no modal truncation;
- NumPy `1.26.4`;
- SciPy `1.14.1`;
- `scipy.linalg.eigh(..., driver="gvd")`;
- output time grid `12000 samples/s`.

Reference refinement schedule, fixed before results:

| level | MFEM uniform refinement | expected tetrahedra |
| --- | ---: | ---: |
| coarse | 0 | 6 |
| medium | 1 | 48 |
| fine | 2 | 384 |

The workflow records element/DOF counts, mesh identity, full exported
semidiscrete-system hash, source/receiver functional identity, generalized
eigen residual, mass orthonormality, transfer output, hashes, timings, and
resource checkpoints for each level.

## PFFDTD refinement schedule

Pinned implementation:

`bsxfun/pffdtd@aa319f6c86517cb95aabfae8656277da62c3ead5`

The exact R120B sloped semantic/compiled geometry is re-bound through R130D for
each configuration. The schedule is fixed to:

- coarse: 6 points/wavelength;
- medium: 8 points/wavelength;
- fine: 10 points/wavelength.

Each run records the exact solver configuration, candidate input, solver
geometry, realized grid identity, boundary-mask identity, resource estimate,
raw output hash, result hash, and normalized transfer.

## Predeclared convergence and cross-solver gates

Magnitude/phase scoring uses a `-50 dB` relative magnitude mask.

Reference medium -> fine must satisfy all of:

- complex RMS relative <= `0.05`;
- maximum magnitude relative <= `0.08`;
- maximum phase error <= `5 deg`.

PFFDTD medium -> fine must satisfy all of:

- complex RMS relative <= `0.20`;
- maximum magnitude relative <= `0.25`;
- maximum phase error <= `15 deg`.

Only if both self-convergence gates pass is MFEM-fine vs PFFDTD-fine eligible
for cross-solver PASS. Fine/fine acceptance requires all of:

- complex RMS relative <= `0.35`;
- maximum magnitude relative <= `0.40`;
- maximum magnitude difference <= `3.0 dB`;
- maximum phase error <= `25 deg`.

These tolerances are task-specific heterogeneous-discretization validation
limits, not R100A benchmark tolerances and not production accuracy guarantees.
They were chosen before results to require the independent FEM medium/fine pair
to be substantially tighter than the cross-method comparison, while allowing
the voxel FDTD pair a wider discretization envelope. The fine/fine limits still
require order-one agreement in the complex transfer: less than 3 dB magnitude
difference and less than 25 degrees phase difference at the scored low-band
samples, with an aggregate complex RMS relative error below 0.35.

Per-frequency ACCEPTED/REJECTED status uses the magnitude mask, magnitude
relative, magnitude-dB, and phase limits above. The complex RMS relative limit
is intentionally an aggregate all-frequency gate and is not reused as a
per-frequency threshold.

## Fail-closed states

The evidence remains BLOCKED/NOT_VALIDATED when any required correspondence or
execution authority cannot be demonstrated, including:

- invalid or mismatched reference tetrahedralization;
- stale exact R120B geometry;
- source or receiver mismatch;
- quantity/unit/phasor/Fourier-kernel mismatch;
- exact solver implementation mismatch;
- missing predeclared refinement level;
- invalid/empty source or receiver functional;
- reference eigen-system quality failure;
- reference self-convergence failure before cross-solver scoring;
- PFFDTD self-convergence failure before cross-solver scoring;
- resource ceiling violation.

Workflow success and physics-validation PASS are intentionally different states.
The dedicated workflow may complete successfully while preserving a numerical
FAIL or BLOCKED result in the immutable evidence artifact.

## Validation-state scope

A successful first fixture can set only:

`VALIDATED_BOUNDED_SLOPED_FIXTURE`

It does not establish:

- arbitrary polyhedron validation;
- concave validation (`CONCAVE_NOT_VALIDATED`);
- multi-region validation (`MULTI_REGION_NOT_VALIDATED`);
- Portal propagation validation (`PORTAL_NOT_VALIDATED`);
- GPU equivalence;
- owned-room validation;
- production solver selection/adoption.

The actual PASS/FAIL/BLOCKED result and all numerical metrics are authoritative
only in the uploaded `r130d-independent-general3d-validation` workflow
artifact for the exact PR head.

## Operational constraints

- RDC calls: **0**
- HTDT-Capture changes: **0**
- `docs/IMPLEMENTATION_STATUS.md` changes: **0**
- `docs/IMPLEMENTATION_ROADMAP.md` changes: **0**
- R100B bakeoff implementation changes: **0**
- R150/R160 implementation changes: **0**
