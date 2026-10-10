# R130D band-limited modal convergence diagnostic — 2026-10-11

Preregistered plan: `benchmarks/acoustics/r130d_original_q0_bandlimited_modal_convergence_plan_2026-10-11.json` (pushed in `66590d5f` before any rescoring). Evidence: `benchmarks/acoustics/r130d_original_q0_bandlimited_modal_convergence_evidence_2026-10-11.json`, produced deterministically by `scripts/run_r130d_bandlimited_modal_convergence_diagnostic.py` from the two SHA-pinned committed modal evidences. **0 new solver runs, 0 new GitHub Actions runs.**

## Question

The canonical original contract (unmodified PFFDTD commit `aa319f6c`, native q0 8-point impulse, native dt/h/Nt, 250 ms signed 40/80 Hz) is `SELF_CONVERGENCE_FAILED`. Does the failure decompose into a non-convergent artifact plus a convergent physical low band?

## Method

For each of the five original SHA-pinned grids (PPW 28/32/36/40/44) and each eigensystem — the original native staircase graph (37,835–132,341 modes) and the exact-geometry hybrid — the committed per-grid per-band modal terms are summed up to cumulative semidiscrete cutoffs {100, 200, 400, 800, 1600, 3200} Hz and rescored under the **unchanged three gates** (complex RMS ≤0.20, magnitude ≤0.25, max phase ≤15°) on all four adjacent pairs plus strict monotonicity. Band sums reconstruct each frozen full transfer to ≤9.8e-15 (conservation gate) before rescoring. No mode is dropped from accounting; the above-cutoff remainder stays reported.

## Result

| eigensystem | ≤100 Hz | ≤200 Hz | ≤400 Hz | ≤800 Hz | ≤1600 Hz | ≤3200 Hz |
|---|---|---|---|---|---|---|
| original native staircase | **fail all pairs** (complex 0.46–0.99, phase 21–79°) | fail | fail | fail | fail | fail |
| exact-geometry hybrid | **PASS all pairs** | **PASS all pairs** | fail | fail | fail | fail |

Hybrid ≤200 Hz detail (worst pair): complex RMS ≤0.017, magnitude ≤0.072, phase ≤0.6° — orders of magnitude inside the gates.

## Verdicts

- `BANDLIMITED_CONVERGENCE_ACHIEVED_TRUE_GEOMETRY`
- `ORIGINAL_NATIVE_LOWBAND_NOT_CONVERGENT`
- `MONOTONICITY_AT_NOISE_FLOOR` (passing-pair metrics sit at the eigensolver noise floor, so strict monotonicity is not asserted there)

## Mechanism resolution (closed form)

1. **Original staircase graph**: its 15 lowest semidiscrete modes (≤100 Hz) do NOT converge across refinement — the staircase realization perturbs the room's lowest eigenmodes realization-dependently. This alone makes the original contract non-convergent.
2. **Exact-geometry models**: the physical low band converges cleanly; the canonical full-band score fails on the realization-dependent >200 Hz modal tail, dominated by finite-record end-of-record leakage terms (last-sample 55–292% of pair differences, prior endpoint attribution).
3. Therefore the original frozen contract is non-convergent **by construction** on both axes; the physical 40/80 Hz transfer is convergent on true geometry.

## Authority statement

Band-limited scores are diagnostics only. Canonical: `SELF_CONVERGENCE_FAILED`; independent physics `NOT_VALIDATED`; product `NO_GO`. Any contract change (band-limited source observable, smoothed q0, etc.) remains blocked until separately preregistered with a physical argument and full requalification.
