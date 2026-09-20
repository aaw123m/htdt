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
