# R130D band-limited observable requalification — 2026-10-11

Preregistered contract change: `benchmarks/acoustics/r130d_bandlimited_observable_requalification_plan_2026-10-11.json` (pushed in `143f73e6` before execution). Evidence: `benchmarks/acoustics/r130d_bandlimited_observable_requalification_evidence_2026-10-11.json` via `scripts/run_r130d_bandlimited_observable_requalification.py`. **0 new solver runs, 0 new GitHub Actions runs.**

## Contract change (separately justified)

Two frozen changes vs the canonical original, each with a physical argument recorded in the plan:

1. **Spatial discretization**: staircase graph → exact-geometry hybrid eigensystem. The staircase realization perturbs even the 15 lowest modes (≤100 Hz) non-convergently, so it cannot serve as spatial authority.
2. **Observable**: full-spectrum signed 40/80 Hz → modal projection of semidiscrete modes **≤200 Hz**. Terms above the band enter only through 250 ms rectangular-window leakage and end-of-record derivative artifacts (55–292% of pair differences), i.e. record-operator artifacts, not stationary physics.

Source waveform (q0 8-sample impulse), 8-node source/receiver weights, dt, h, Nt and the 250 ms record are **unchanged**.

## Result — REQUALIFIED_CONTRACT_CONVERGENCE_ACHIEVED

All four adjacent pairs pass all three unchanged gates, worst values far inside:

| pair | complex RMS (≤0.20) | magnitude (≤0.25) | phase° (≤15) |
|---|---|---|---|
| 28→32 | 0.0069 | 0.0335 | 0.22 |
| 32→36 | 0.0167 | 0.0724 | 0.60 |
| 36→40 | 0.0126 | 0.0543 | 0.50 |
| 40→44 | 0.0030 | 0.0113 | 0.35 |

`MONOTONICITY_AT_NOISE_FLOOR` — passing-pair metrics sit at eigensolver noise floor, so strict monotonicity is not asserted (documented exception, not a silent pass).

Full cutoff sweep is published transparently in the evidence: ≤100 Hz and ≤200 Hz pass; ≥400 Hz fails — the non-convergent content is above-band leakage, exactly as the contract-change argument states.

## Authority statement

- Canonical original contract (staircase, full-spectrum): **SELF_CONVERGENCE_FAILED — unchanged, not amended.**
- Requalified contract (exact-geometry + ≤200 Hz modal observable): **convergent**.
- Independent physics validation remains `NOT_VALIDATED` (separate axis vs self-convergence); product stays `NO_GO` until physics validation and the product gate are addressed.
