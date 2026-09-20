# R100B MFEM GL2 exact-four-substep experiment — 2026-09-20

## Scope

Issue #101 / R100B predeclared numerical experiment after PR #285.

Task-start main: `c77b8b50d7e4f9706c08e64a1daa7a7a51e8ed96`.

The only numerical variable changed from PR #285 is:

`substeps_per_output_interval = 4`

For every output interval, the candidate performs exactly four equal GL2 / [2/2] Padé substeps before the next output sample. The frozen plan was committed before any result was obtained in commit `ba596ec0144b61874fbaaf1df9e1a5094778b44d`.

## Frozen authority

Unchanged from PR #285:

- MFEM source commit `d964264cdb9a13e94a201b6c236c7721e0c8765f`;
- exact H1 p2 / h1 spatial authority, one uniform refinement, 40 elements / 525 DOFs;
- exact semidiscrete M/K/source/receiver authority and source kick;
- half-open 2 s finite-record `P_T/Q_T` observable;
- output rates 6000 / 9000 / 12000 Hz;
- scored grid 20..300 Hz at 1 Hz spacing;
- null masks -60 / -40 dB;
- tolerances 0.75 dB / 0.05 relative magnitude / 8 deg phase;
- diagnostic full-basis modal reference and its normalization / phasor convention;
- candidate-wide production-adoption semantics;
- resource ceilings.

No adaptive stepping, result-driven frequency/mode exclusion, tolerance fitting, normalization change, record-duration change, source/receiver change, spatial refinement change, h2 retry, or rate-specific hidden tuning is permitted.

## Exact step construction

For an output rate `f_out`:

- output interval: `dt_out = 1 / f_out`;
- internal GL2 step: `dt_internal = dt_out / 4`;
- output sample count: `N = T * f_out` for the frozen half-open 2 s record;
- internal step count: `4 * (N - 1)`.

Therefore the frozen internal rates are:

| output rate | internal rate | output samples | internal steps |
|---:|---:|---:|---:|
| 6000 Hz | 24000 Hz | 12000 | 47996 |
| 9000 Hz | 36000 Hz | 18000 | 71996 |
| 12000 Hz | 48000 Hz | 24000 | 95996 |

The step counts are derived by the implementation from the half-open sampling contract; they are not hard-coded.

## Implementation scope

The existing R100B transient runner and sparse-factorization architecture are reused. The plan model is extended only enough to admit the predeclared v3 four-substep authority while preserving parseability of the PR #281 one-step and PR #285 two-substep schemas. The runner remains responsible for recording plan/system/integrator identities, output/internal dt, sample/step counts, factorization provenance, residuals, runtime, RSS, disk use, source provenance, and the separated decision gates.

## Authoritative execution

The authoritative GitHub Actions run, raw outputs, numerical comparisons, resource comparison against PR #281 / PR #285, artifact identity, and final decision are recorded below only after the frozen plan has executed.

Pending authoritative execution.
