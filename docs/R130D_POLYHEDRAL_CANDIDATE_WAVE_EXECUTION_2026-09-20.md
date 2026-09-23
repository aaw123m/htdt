# R130D — R120B explicit polyhedral geometry → actual PFFDTD CPU execution

Date: 2026-09-20

Issue: #101

Draft PR: #278

Starting main authority: `49901a6b46ad889bd06b34e4482a5b6121545d1d`

## Decision boundary

R130D establishes a bounded software-execution vertical slice from exact R120B
polyhedral geometry into the existing PFFDTD Python/Numba CPU candidate:

`R120B semantic polyhedron → exact compiled triangles → R130D solver representation → PFFDTD grid/voxelization → bounded CPU solve → immutable complex-pressure result`.

It does **not** change production solver adoption. General-3D sloped/concave
results remain `NOT_VALIDATED` as physics evidence because this slice has no
independent general-3D numerical reference.

## Supported geometry/execution scope

The R130D adapter supports the following bounded subset:

- one `AcousticRegion`;
- exact `source_geometry_kind=explicit_polyhedral`;
- closed, watertight R120B topology with the exact topology report re-derived
  at the explicitly bound topology tolerance;
- planar polygon faces at arbitrary orientation;
- concave closed volume topology;
- deterministic R120B indexed triangles;
- exact per-surface material authority references;
- exactly one existing R130 source acoustic reference and one or more receivers;
- source/receiver positions strictly inside the exact polyhedral volume;
- rigid zero-normal-velocity boundary only;
- existing pinned PFFDTD Python/Numba CPU candidate;
- existing R130 complex-pressure output/persistence path.

The implementation accepts frequency requests only through the existing R130
request/configuration gates. The dedicated numerical fixture exercised 40 Hz and
80 Hz inside the intended bounded 20–300 Hz low-band slice; this run does not
claim endpoint or broadband 20–300 Hz numerical coverage.

## Explicitly unsupported / fail-closed

The adapter rejects rather than approximates:

- multiple `AcousticRegion` volumes;
- Portal-bearing wave geometry;
- polygon holes;
- bounded-planar/curved approximation authority in the exact-polyhedron lane;
- invalid, open, non-manifold, or topology-report-mismatched geometry;
- semantic/compiled R120B identity mismatch;
- stale/unbound R120B geometry for the exact `AcousticSceneSnapshot`;
- source/receiver outside the exact polyhedron;
- source/receiver on a boundary or in an ambiguous tolerance region;
- non-rigid impedance/causal material execution in this initial R130D lane;
- grid workloads above the configured resource ceiling.

No automatic resolution downgrade is performed.

## Deterministic solver representation and provenance

`backend/src/htdt/acoustic_pffdtd_polyhedral_geometry.py` is a solver-specific
adapter. R120B semantic/compiler authority is not rewritten.

The solver geometry authority binds at minimum:

- exact R120B semantic geometry ref;
- exact R120B compiled geometry ref;
- exact topology report id/hash and topology identity;
- topology and containment tolerances;
- single region id;
- deterministic surface → material bindings;
- canonical oriented triangle hash;
- generated geometry hash;
- PFFDTD solver-model hash;
- bounding box;
- grid origin, spacing, dimensions, and cell count;
- PFFDTD grid algorithm/version;
- exact interior classification rule and version;
- exact PFFDTD triangle-intersection rule/version;
- source/receiver exact containment evidence.

The actual executed PFFDTD grid/voxel authority additionally records logical
content hashes for `cart_grid.h5` and `vox_out.h5`, including the realized
boundary mask/node data. This distinguishes exact geometry execution from a
bounding-box-only plan.

The R130D candidate input also binds:

- `SceneRevision` id/content hash;
- `AcousticSceneSnapshot` id/hash;
- semantic/compiled/solver R120B geometry refs;
- source/receiver;
- environment and solver configuration inherited from the existing exact R130
  request/dispatch chain;
- rigid boundary/material refs;
- resource configuration/estimate;
- PFFDTD implementation identity.

Existing R130A/B/C execution-input identities are preserved: the new R130D
binding is optional and is omitted from the semantic payload for earlier lanes.

## Exact source/receiver containment

Containment is not a bounding-box test.

The R130D lane first performs an exact triangle-boundary proximity check using
the configured tolerance. Points on/near a triangle are classified
`boundary_or_ambiguous` and rejected. Remaining points are classified using
the oriented-triangle solid-angle sum for the closed polyhedron. This works for
the bounded concave fixture and rejects a point located inside its bounding box
but inside the deliberately removed concave notch.

## Silent box-approximation mechanical gate

Two independent gates are used.

### Sloped-ceiling fixture

The rectangular and sloped fixtures deliberately have the same bounding box,
same planned PFFDTD grid origin, same spacing, and same grid dimensions.

The run requires all of the following to differ:

- generated solver geometry identity;
- R130D candidate input identity;
- actual PFFDTD voxel/boundary-mask logical hash;
- immutable complex-pressure artifact identity;
- numerical response.

Therefore a solver implementation that silently replaces the sloped geometry by
the common bounding box fails mechanically.

### Concave fixture

The L-shaped/concave fixture uses exact closed polyhedral containment and exact
triangles. A point in the bounding-box-only notch is classified outside.
Execution additionally requires geometry/input/boundary-mask/artifact identities
and the numerical response to differ from the rectangular fixture.

## Numerical verification

Authoritative successful workflow:

- workflow: `R130D Polyhedral Candidate Wave Execution`;
- run: #2, GitHub Actions run id `35503987392`;
- head commit: `8d63603af9f9d8feaf1eb8cf7eefac3ecca782e4`;
- URL: https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/actions/runs/35503987392
- evidence artifact id: `10602773726`;
- artifact ZIP SHA-256:
  `05d04919181c98771539a7dac4a8a09814e7c10d5c9e79c0d6432303bc2233fd`.

Focused repository tests in that run:

- R120B polyhedral compiler tests;
- R130D focused polyhedral adapter invariants;
- existing R130 candidate execution regression;
- existing PFFDTD/R140 resource estimator regression.

Result: **40 passed, 1 warning**.

### 1. Rectangular equivalence

The existing R130A rigid R120/PFFDTD lane and an equivalent R120B exact
polyhedron were executed with the same source, receiver, boundary, environment,
grid/configuration and frequency settings.

Observed complex-pressure difference:

- maximum absolute error: `0.0 Pa`;
- maximum relative error: `0.0`;
- acceptance: `rtol=1e-9`, `atol=1e-10 Pa`.

Legacy result:

`acoustic-solver-result:f4ab0bf2773dd0a5e51bba1690e9520a94317f25dd76ee889c6783d5d2ef5ca7`

R130D rectangular result:

`acoustic-solver-result:e75138c8d377ba105a9273cb8e4bfdbca057aa9c718b6f196dfdf005a7cc110c`

This is a software/regression equivalence result, not an independent validation
of PFFDTD physics.

### 2. General-3D sensitivity

Mechanical gate identities recorded by the successful run include:

| Fixture | Candidate input SHA-256 | Executed-grid SHA-256 | Result SHA-256 |
| --- | --- | --- | --- |
| rectangular | `89aaa38d210896304c245e36566cf06b14f273c3ab98e1b915d5645b0a3b659b` | `f1632593dfcdb66ead49435418883f756643005ad4cf4e6f7e595c70637d4446` | `e75138c8d377ba105a9273cb8e4bfdbca057aa9c718b6f196dfdf005a7cc110c` |
| sloped | `fc97d99a1981a333a579ce488249f2778ae1f88cb6dfa568d7a6ae80f1eafdf5` | `757e3231c09dbc4715c5188b8395da1cebe8742c6d341b1a744ef8b8ad13cac5` | `7cd2aa047b0626201c8f87efc17592508cdf51ee232040a523e66ee6a6901e21` |
| concave | `be8c91188b8dd4cb367a359ef2c21f3667ad0c5018c3feb439e8f064a4582bfa` | `51e290bd1b14bcb40f844a6f93705de355ffa2a3e91a65f9538aa3d910dc669a` | `4ce7edebb523b9fd7a805d73db2722e2391e23881b461d0d75d369f2f1e2b228` |

For the sloped fixture, the rectangular and sloped grid envelope is deliberately
identical: origin `(-1.5015, -1.5015, -1.5015) m`, spacing `0.429 m`,
dimensions `18 × 18 × 18`. The sloped boundary-mask logical SHA-256 is
`f64b971faede55905860b6bec414b2c46eebc12b3fabbbf99ee41bf22bcf1aee`
with 808 boundary nodes, and it differs from the rectangular mask.

Both sloped and concave responses differ from the rectangular response. These
checks prove execution sensitivity to general-3D geometry; they do **not** make
the sloped/concave response a physics-validation PASS.

## Resource evidence

R130D calls the existing PFFDTD/R140 resource estimator before invoking solver
setup. The dedicated fixture records the exact workload authority in each
result/provenance chain.

Representative bounded workload from the successful run:

- Cartesian grid: `18 × 18 × 18 = 5,832` points;
- `Nt = 42`;
- time step: `0.0007209661486505452 s`;
- logical CPU demand: `4`;
- setup processes: `1`;
- estimated peak task-incremental memory: `1,763,808 bytes`;
- estimated scratch: `36,221,740 bytes`;
- GPU slots: `0`;
- VRAM: `UNAVAILABLE` for this CPU-only lane.

A deliberately impossible `max_grid_cells=1` configuration is rejected before
PFFDTD setup. The adapter never waits for an OOM and never silently lowers
resolution.

PFFDTD setup/solve timings contain one-time Python/Numba JIT effects; the first
process solve is materially slower than later warm solves. Timing numbers are
therefore recorded as execution evidence only, not as a production performance
claim.

## Persistence / reopen

The complex-pressure artifact is stored through the existing immutable
content-addressed authority store and persisted in the existing
`AcousticSolverResultEnvelope`.

The successful run verifies:

- exact result save/reopen re-resolution succeeds;
- a modified complex-pressure artifact is rejected on reopen;
- stale R120B semantic/compiled authorities are rejected;
- stale R120B snapshot association is rejected;
- grid/tolerance changes alter solver geometry identity;
- source/receiver/material/geometry/configuration changes flow into the exact
  candidate input/result authority chain.

## Regression results on the R130D implementation commit

For commit `8d63603af9f9d8feaf1eb8cf7eefac3ecca782e4`:

- R130A Candidate Wave Execution: PASS;
- R130B Candidate Explicit Impedance Execution: PASS;
- R130C Candidate Causal Boundary Execution: PASS;
- R140 PFFDTD Resource Executor: PASS;
- R130D Polyhedral Candidate Wave Execution: PASS.

The general repository CI was still running when this record was initially
written; the PR check status is the authority for its final result.

## Known limitations / authority boundary

### R120B association with the current snapshot schema

The current `AcousticSceneSnapshot` authority directly carries the legacy R120
compiled-geometry binding, not an R120B-specific semantic/compiled polyhedral
slot. Altering snapshot/R120B core authority was outside this parallel task's
ownership.

R130D therefore binds the exact R120B semantic geometry, compiled geometry,
solver representation, `SceneRevision`, and exact snapshot id/hash in the
solver-specific execution authority. It fail-closes if that association is
missing or stale. This is sufficient for this candidate software vertical slice
but remains a production-authority integration limitation.

### PFFDTD compatibility patch lifecycle

The existing PFFDTD runtime compatibility patcher is intentionally exact but is
not idempotent when multiple solves reuse the same already-patched source
checkout. The dedicated R130D runner restores the temporary external pinned
PFFDTD checkout to the exact commit before each independent bounded solve. No
HTDT shared compatibility adapter or upstream source authority was changed in
this task.

## What this changes for production readiness

This slice establishes:

- actual CPU execution of exact R120B closed polyhedral geometry;
- deterministic solver-specific geometry/provenance;
- strict exact-polyhedron source/receiver containment;
- pre-execution resource gating;
- rectangular regression equivalence;
- mechanical proof that sloped/concave fixtures are not silently boxified;
- immutable complex-pressure result persistence/reopen.

It does **not** establish:

- production solver selection/adoption;
- independently validated general-3D wave physics;
- arbitrary multi-region wave solve;
- Portal transmission;
- owned-room validation;
- general material calibration;
- GPU execution/equivalence;
- R160 hybrid numerical composition;
- broadband 20 Hz–20 kHz full-wave execution;
- curved analytic boundaries.

Production adoption remains unchanged.

## Operational constraints

- RDC calls: **0**
- HTDT-Capture changes: **0**
- prohibited R120B compiler/model files changed: **0**
