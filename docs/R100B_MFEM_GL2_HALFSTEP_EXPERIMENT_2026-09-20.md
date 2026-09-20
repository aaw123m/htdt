# R100B MFEM GL2 exact-half-step experiment — 2026-09-20

## Scope

Issue #101 / R100B next numerical experiment after PR #281.

Task-start main: `1fb07fa210aae55d91c7d86b3f4c5ee993be7c93`.

The only intended numerical variable changed from PR #281 is the internal GL2 step construction:

```text
output interval Δt
  -> GL2 step Δt/2
  -> GL2 step Δt/2
  -> output sample
```

`substeps_per_output_interval = 2` is fixed before execution. Adaptive stepping and rate-specific hidden heuristics are forbidden.

## Frozen authority

The experiment preserves:

- the same exact p2 / h1 MFEM semidiscrete spatial system;
- the same source and receiver;
- the same finite-record `P_T / Q_T` observable;
- output rates 6000 / 9000 / 12000 Hz;
- the same full-basis modal reference;
- the same normalization and phasor/Fourier convention;
- scored bins 20..300 Hz at 1 Hz spacing;
- magnitude / phase null masks -60 / -40 dB;
- tolerances 0.75 dB / 0.05 relative / 8 deg;
- the same adjacent-rate, modal-reference, and resource decision structure.

No tolerance fitting, record shortening, comparison-bin exclusion, bad-mode removal, or normalization substitution is permitted after seeing results.

## Required evidence

The final artifact must explicitly record integrator family/order, output interval, exactly two internal substeps, exact internal step size, system/source/receiver identity, record duration, observable/tolerance contract, implementation identity, adjacent-rate metrics, modal-reference metrics, wall time, memory when available, factorization/reuse, and substepping cost.

Numerical convergence, modal-reference agreement, execution suitability, overall outcome, and production-adoption implication are independent decisions. Production adoption remains `NO_GO` unless separate candidate-wide authority later changes it.

## Execution

RDC usage: **0**. Validation uses repository-native focused tests and GitHub Actions. The full reproducible benchmark command and immutable run/artifact identity will be added after execution.
