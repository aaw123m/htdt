# R100B MFEM current-authority concave bounded experiment — 2026-09-20

Issue: #101  
Base main: `a8491db973c6fcefc66e259903ca668936f63a2e`  
RDC usage: **0**

## Purpose

This task is the smallest bounded MFEM experiment intended to test whether the current exact
`wave-concave-l-room-v1` **FAIL / non-converged** evidence can change without changing R100A.

The previous current-authority finite-record evidence is preserved unchanged:

- R100A semantic hash: `a9d45a3d650f20747368dd5610a6a91f93cdad881dcb10fcac88cd9d17e211e7`
- MFEM source: `mfem/mfem@d964264cdb9a13e94a201b6c236c7721e0c8765f`
- previous evidence: run `35439744627`, artifact `10583118482`
- previous outcome: **FAIL / non-converged**
- previous coupled p/dt RMS sequence: `1.4162721 -> 1.0545476 -> 1.2327872`
- previous final p4→p5 delta: `35.0882 dB / 16.80364 relative / 179.7289 deg`
- previous linear residuals: approximately `1e-12`, already far inside the fixed `1e-8` qualification ceiling

No historical negative evidence is deleted or relabeled.

## Why this experiment

The previous sequence changed H1 polynomial order and solver-native time step at the same time
while retaining the original five-element mesh. That result proves non-convergence for that exact
coupled sequence, but it does not separate temporal discretization from spatial mesh resolution.

The existing MFEM C++ finite-record probe already supports exact geometry-preserving
`UniformRefinement()`. This task therefore changes only the benchmark orchestration and evidence
contract, not R130 production code and not R100A.

## Predeclared bounded plan

The plan is frozen in:

`benchmarks/acoustics/r100b_mfem_concave_experiment_plan.json`

It contains exactly five unique attempts. One pivot is shared by two three-level tracks.

| Attempt | H1 order | Uniform h refinements | Sample rate |
|---|---:|---:|---:|
| `time-p2-h1-sr6000` | 2 | 1 | 6000 Hz |
| `time-p2-h1-sr9000` | 2 | 1 | 9000 Hz |
| `pivot-p2-h1-sr12000` | 2 | 1 | 12000 Hz |
| `space-p2-h0-sr12000` | 2 | 0 | 12000 Hz |
| `space-p2-h2-sr12000` | 2 | 2 | 12000 Hz |

Time track:

`time-p2-h1-sr6000 -> time-p2-h1-sr9000 -> pivot-p2-h1-sr12000`

Space track:

`space-p2-h0-sr12000 -> pivot-p2-h1-sr12000 -> space-p2-h2-sr12000`

Thus:

- the time track holds order and spatial mesh fixed;
- the space track holds order and time step fixed;
- the shared pivot avoids an unnecessary sixth solve;
- exact L-prism geometry is retained at every h level;
- no adaptive search or after-the-fact parameter sweep is permitted.

## Exact numerical contract

The runner fails closed unless the plan matches the current R100A authority for:

- quantity: finite-record complex pressure transfer `P_T/Q_T`;
- unit: `Pa/(m3/s)`;
- physical source normalization: unit volume velocity;
- coordinate convention: `x_right_y_rear_z_up`;
- interpolation rule: `linear_complex`;
- Fourier convention: `exp(-i*omega*t)`;
- finite-record DTFT kernel: `exp(+i*2*pi*f*n*dt)`;
- half-open `[0, 2 s)` record;
- 20–300 Hz / 1 Hz frequency grid;
- existing magnitude and phase null masks;
- current R100A tolerances: **0.75 dB / 0.05 relative / 8 deg**.

The runner derives and persists the exact canonical fixture SHA-256 from the exact bound R100A
fixture before executing numerical attempts. It also persists deterministic hashes for the full
solver configuration and predeclared mesh/refinement configuration.

No R100A fixture, observable, mask, tolerance, source normalization, phase convention, or
frequency grid is modified.

## Qualification and promotion

Each attempt must remain inside the existing fixture resource ceiling and fixed linear residual
qualification. Every attempt is retained whether it completes, fails, or blocks.

For each track:

1. both adjacent complex-RMS refinement errors must be available;
2. the complex-RMS error must strictly decrease;
3. the final adjacent pair must satisfy the unchanged current R100A magnitude and phase tolerances
   after the existing null masks.

The overall result is:

- **PASS** only if all five attempts complete and both tracks PASS;
- **FAIL** if all attempts complete but either track remains non-converged;
- **BLOCKED** if a predeclared attempt cannot complete or exceeds a solver/resource ceiling.

Only `space-p2-h2-sr12000` is eligible for promotion, and only after an overall PASS. The
runner cannot choose the numerically best trace after observing results. A non-converged trace can
never be promoted.

## Evidence retention

The dedicated workflow uploads:

- the complete report;
- all raw time-domain attempt records;
- all attempt identities and resources;
- all adjacent-pair convergence metrics;
- both track evaluations;
- the deterministic report identity.

The checked-in candidate-local evidence summary and this record will be updated only after the
dedicated workflow has produced the actual result. Workflow success is evidence-generation
success; it is not equivalent to a physics PASS.

## Scope exclusions

This task does not modify:

- `docs/IMPLEMENTATION_STATUS.md`;
- `docs/IMPLEMENTATION_ROADMAP.md`;
- R120/R130/R140/R150/R160/R170 production implementation;
- UX;
- HTDT-Capture.

It does not select a production solver and does not change an unrelated successful R100B fixture.


## Executed result

Dedicated workflow run `35500693297` completed successfully as an evidence-generation workflow.
Artifact `10602433114` (`r100b-mfem-concave-bounded-experiment`) has digest
`sha256:9682d648017bba28146a49a6b223c03ebee564067cfa602f947a84b9f29d056a`.

The physics/numerical experiment outcome is **BLOCKED**, not PASS.

Deterministic report identity:

`8c8245f999684db0150825addf2b094d0e658a1e1006e119d173e97d6b878e65`

Exact fixture semantic hash:

`3297647551cb0fbcdf25d5bc9ddf3d1e591432f4eb82e6141309a83496cc01ce`

Solver configuration hash:

`540b7cbd8b7e055965d75769312f18ef9d98e95ab283495f5b9982ad957502a7`

Mesh/refinement configuration hash:

`6d6720d9a3ec37ddb7533f5340f48fb1c65afcf9fe185cf06ed1eefa06354e6b`

### All attempts

| Attempt | Result | Elements | DOFs | Solve | Peak RAM | Numerical identity |
|---|---|---:|---:|---:|---:|---|
| `time-p2-h1-sr6000` | COMPLETED | 40 | 525 | 18.564 s | 6.43 MiB | `bc9c38dee62c8059db295dd893a5c6f2b3b64348e1dd2ae195922eca20d71565` |
| `time-p2-h1-sr9000` | COMPLETED | 40 | 525 | 28.529 s | 6.03 MiB | `16c117879f448a5e74bcb1a9aa151d82eec71369b96ad0b12610960d40c534b2` |
| `pivot-p2-h1-sr12000` | COMPLETED | 40 | 525 | 39.006 s | 6.07 MiB | `25c78bfa450b5c5152687ae937b46447bc2ad2e76fb290b7e345e1d77d1ea911` |
| `space-p2-h0-sr12000` | COMPLETED | 5 | 99 | 4.153 s | 5.28 MiB | `cd131e0fa99d0356c1551494505e7cae661b63947555589d2a1ae173e8d7fb48` |
| `space-p2-h2-sr12000` | **BLOCKED** | — | — | — | 11.66 MiB observed | no completed numerical identity |

The final h2 attempt exceeded the predeclared `330 s` subprocess wall ceiling. It was killed and
retained as `resource_wall_timeout`; it was not silently retried at another resolution.

All completed attempts retained linear residuals around `1e-12`, well below the fixed
`1e-8` qualification ceiling. This rules out insufficient iterative linear solve accuracy as the
cause of the observed time-track failure.

### Time-track result: FAIL

Fixed spatial configuration: H1 p=2, one uniform h-refinement, 40 elements, 525 DOFs.

Adjacent complex-RMS relative error:

- 6000 -> 9000 Hz sample rate: `1.3397652498`
- 9000 -> 12000 Hz sample rate: `1.3596844444`

The error is **not strictly decreasing**.

The final 9000 -> 12000 pair has:

- max magnitude absolute delta: `21.4094258604 dB`
- max magnitude relative delta: `9.0797431125`
- max phase delta: `176.710382952 deg`

These are far outside the unchanged current R100A limits
`0.75 dB / 0.05 relative / 8 deg`.

Therefore the previous coupled p/dt failure can now be narrowed: the current
Newmark + point-source/receiver finite-record configuration is already non-convergent when
**spatial discretization and H1 order are held fixed**. The previous failure cannot be explained
only by changing p-order together with dt.

### Space-track result: BLOCKED

At fixed H1 p=2 and 12000 Hz recorded sample rate, the available h0 -> h1 pair has:

- complex-RMS relative error: `1.0200254593`
- max magnitude absolute delta: `51.1621804224 dB`
- max magnitude relative delta: `20.3211788496`
- max phase delta: `179.606323563 deg`

The required h1 -> h2 pair is unavailable because the predeclared h2 attempt hit the wall-time
ceiling. The space track is therefore **BLOCKED**, not extrapolated and not scored from the first
pair alone.

### Resource evidence

- MFEM native configure/build: `956.803 s`
- completed-attempt solve total: `90.251 s`
- peak observed attempt RAM: `11.66 MiB`
- experiment work disk: `8.446 MiB`
- h2 process wall ceiling: `330 s`
- existing R100A per-attempt solve ceiling: `300 s`
- R100A RAM/disk/output ceilings remained unchanged

The limiting resource is runtime of the h2 transient attempt, not RAM or disk.

## Production-readiness effect

No bounded configuration qualified as a PASS, so no new fixture trace is eligible for promotion
into production-selection truth. The existing exact-current MFEM concave FAIL evidence remains
selection-relevant and the production-adoption decision remains **NO_GO**.

The checked-in candidate-local evidence is:

`benchmarks/acoustics/evidence/r100b_mfem_concave_bounded_2026-09-20.json`

This new artifact is diagnostic exact evidence, but it is deliberately **not** converted into a
passing readiness input. No unrelated previously successful fixture was replayed for selection.

## Smallest next selection-changing experiment

Do not spend the next experiment on a larger h-mesh first. The fixed-space time track already
fails badly, so resolving temporal/source representation is the smaller prerequisite.

The next bounded experiment should keep the exact p2/h1 MFEM mass/stiffness/source/receiver system
fixed and replace Newmark stepping with a **non-dissipative exact or semi-exact modal evolution**
of that same semidiscrete system, evaluated on the same 6000 / 9000 / 12000 Hz recorded grids.

This experiment keeps the current R100A finite-record `P_T/Q_T`, source record, phase convention,
frequency grid, masks and tolerances unchanged.

Decision logic:

- if the modal time track converges, Newmark temporal dispersion is isolated as the primary
  numerical cause and a production-suitable nondissipative transient integration path becomes the
  next candidate experiment;
- if the modal time track still does not converge, source/receiver delta representation or the
  finite-record semidiscrete spatial response is the next isolated cause;
- do not retry h2 until the time track is numerically qualified.

## Validation

- focused experiment contract: **5 passed** in workflow `35500693297`
- dedicated MFEM bounded workflow: **PASS** as evidence generation; numerical outcome **BLOCKED**
- ordinary CI run `35500693343`: **PASS — 967 passed, 2 skipped**
- Windows Release Artifact run `35500693245`: **PASS**
- RDC usage: **0**

Workflow success is not a physics PASS. No hidden score, tolerance relaxation, best-trace
selection, non-converged promotion, or production solver selection occurred.
