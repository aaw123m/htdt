# R130D general-3D dense frequency-neighborhood diagnostic result — 2026-10-03

Issue: #101
Task-start `main`: `b6577df1109ad786040d8ae1d3719d2b5cd3456d`
Frozen dense diagnostic semantic SHA-256: `5922d03be22b86634c9397d94d15b1164b00c7a9c3e012d9628fd3a63dd3c80a`
Parent spatial diagnostic semantic SHA-256: `7703ca0d2b083e6b732c04d3b1ef206dc67fe5448bbd9d05dfa25d3b7f637ad4`
Runner: `scripts/run_r130d_dense_frequency_diagnostic.py` (local Devin VM; pinned runtime numpy 1.26.4 / scipy 1.14.1 / numba 0.60.0)
Evidence file SHA-256: `6ec0b08fbe619ae06c42fc5af77e9d26800d29a033372188e64b3bfbc618ec9c`
Evidence semantic SHA-256: `916a993e8daf5bed2ea997d46af507cf387d2f627e1ac43a1345fe3be2d68027`
Summary semantic SHA-256: `143e158dc4f30d451e1dc4d43a41ccd50fdf8bcdc7f35a3595e8b4c584712ec5`

## Result

This slice executed the dense frequency-neighborhood direct-DTFT experiment that
`R130D_GENERAL3D_SPATIAL_REPRESENTATION_DIAGNOSTIC_RESULT_2026-09-21.md`
predeclared verbatim: 36.0–44.0 Hz and 76.0–84.0 Hz inclusive at 0.5 Hz spacing
(34 frequencies total), the same persisted run-76 raw records, the same native
finite-record `P_T/Q_T` left-rectangle operator, the same metric
`|H_b-H_a| / max(|H_b|, |H_a|, 1e-12)`, the same 8→10 and 10→12 pairings, and the
complete predeclared grid evaluated with no post-result frequency selection.

The PFFDTD 8/10/12 PPW levels were re-executed under the unchanged frozen solver
contract and bound to run 76 before evaluation: at every level the semantic
sha256 of the recombined pressure trace and the unit-impulse source trace matched
the frozen run-76 pins exactly (`RUN76_TRACE_IDENTICAL`). A second independent
execution produced bit-identical traces and an identical 34-frequency sweep.

All three PFFDTD levels reproduced the PR #295 canonical transfers exactly
(`PASS` at the frozen `1e-9` max absolute complex-component tolerance). The
canonical contract — solver execution, PPW series, duration, 40/80 Hz scored
frequencies, window, thresholds, magnitude mask — is unchanged.

| item | result |
| --- | --- |
| run-76 record binding | `RUN76_TRACE_IDENTICAL` (all 3 levels) |
| evaluation state | `EVALUATED` |
| PR #295 canonical reproduction (PFFDTD leg) | `PASS` |
| dense frequency-neighborhood sensitivity | `DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY` |
| worsening count | 18 / 34 predeclared frequencies |
| canonical MFEM self-convergence | `SELF_CONVERGENCE_FAILED` (unchanged) |
| canonical PFFDTD self-convergence | `SELF_CONVERGENCE_FAILED` (unchanged) |
| cross-solver eligibility | `CROSS_SOLVER_BLOCKED` (unchanged) |
| general-3D validation | `NOT_VALIDATED` (unchanged) |

The diagnostic classification does not alter canonical acceptance.

## Per-band worsening structure

Under the frozen worsening rule `d_10_12(f) > d_8_10(f)`:

| band | grid | worsening |
| --- | --- | ---: |
| `canonical_40hz_neighborhood` | 36.0–44.0 Hz @ 0.5 Hz | 8 / 17 |
| `canonical_80hz_neighborhood` | 76.0–84.0 Hz @ 0.5 Hz | 10 / 17 |

The worsening is band-structured rather than bin-localized:

- In the 40 Hz band every frequency below 40.0 Hz worsens (36.0–39.5 Hz), while
  every frequency at or above the canonical bin improves. The largest worsening
  gap in the whole grid sits at `argmax(d_10_12 - d_8_10)` = 36.5 Hz
  (`d_10_12 - d_8_10 = +0.6824`).
- In the 80 Hz band worsening is split into two contiguous runs, 76.0–78.0 Hz and
  80.5–82.5 Hz; the canonical 80.0 Hz bin itself does not worsen.
- Neither canonical scored frequency (40.0, 80.0 Hz) worsens; the six-frequency
  run-76 worsening at 39/81 Hz is confirmed as part of wider sub-band structure,
  not an isolated canonical-bin artifact.

## Interpretation

The frozen 34-point classifier returns `DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY`
(18 of 34 — between the `≤11` localized and `≥23` persists boundaries). This
resolves the open question left by the six-frequency run-76 label
`NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS`: the adjacent-level
non-monotonicity is not confined to the canonical bins — it extends over
roughly half the ±4 Hz record-resolution neighborhood — but it is not uniform
either. Worsening concentrates in the low side of the 40 Hz band and in two
mid-band runs of the 80 Hz band, consistent with a sub-band transfer-shape /
adjacent-mode sensitivity rather than a grid-wide degradation or a canonical-bin
artifact. This label is a predeclared classification name, not a claim about the
mechanism.

The 32 non-canonical grid frequencies remain diagnostic-only and do not enter
canonical acceptance.

## Canonical state

PR #295 canonical transfers reproduced exactly at all three executed PFFDTD
levels (MFEM legs were not re-executed; this diagnostic is PFFDTD-only by
predeclaration). The canonical states therefore remain unchanged:

- MFEM self-convergence: `SELF_CONVERGENCE_FAILED`;
- PFFDTD self-convergence: `SELF_CONVERGENCE_FAILED`;
- cross solver: `CROSS_SOLVER_BLOCKED`;
- general-3D: `NOT_VALIDATED`.

No threshold, mask, PPW, scored-frequency set, observation contract,
amplitude/phase fit, geometry, source/receiver position, or solver execution
change was made.

## Next pre-fixed numerical experiment

The remaining predeclared diagnostic axis from
`docs/reviews/rev34-featureaudit.md` is **source/receiver discretization
sensitivity**: vary only the PFFDTD source/receiver interpolation stencil
representation (e.g. trilinear node set vs. neighboring supported stencils)
under the same frozen records contract, and record whether the sub-band
worsening pattern moves. If a future diagnostic is predeclared, freeze the
stencil variants, evaluation rule, and classification before reading results.

No execution of this proposed experiment is part of the present slice.

## Verification and provenance

Focused tests: `backend/tests/test_r130d_general3d_validation.py` — 46 passed.
Runner compile-check: `PASS` (parser/binding import path exercised). Two
independent boxed executions produced bit-identical pressure/source trace
digests and identical sweep outputs; both bound `RUN76_TRACE_IDENTICAL` to the
frozen run-76 pins.
