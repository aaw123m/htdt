# R130D general-3D stencil-discretization sensitivity diagnostic result — 2026-10-03

Issue: #101
Task-start `main`: `6ee4f4a71954df10aa6ab02096662c9db695025b`
Frozen stencil diagnostic semantic SHA-256: `25229cb1a1c4778bba64ffb0b1a57ed6cc92d87c056b312aa7c061c04406a914`
Parent dense diagnostic semantic SHA-256: `5922d03be22b86634c9397d94d15b1164b00c7a9c3e012d9628fd3a63dd3c80a`
Runner: `scripts/run_r130d_stencil_sensitivity_diagnostic.py` (local Devin VM; pinned runtime numpy 1.26.4 / scipy 1.14.1 / numba 0.60.0)
Evidence file SHA-256: `600070d88424141579eab17133f32193d2394894b8bf05ca1bfc06f1882fb78f`
Evidence semantic SHA-256: `6df726d549e387f4924bcebc96c31b707b5b3d2bdc252d7778f530fbd72010df`
Summary semantic SHA-256: `8c66365c759a1297208e1d6510f2bd22a03abd647238a3ed7e65b75fd0660138`

## Result

This slice executed the source/receiver discretization-sensitivity experiment
that `R130D_GENERAL3D_DENSE_FREQUENCY_DIAGNOSTIC_RESULT_2026-10-03.md`
predeclared verbatim: vary only the PFFDTD interpolation-stencil weight
representation on the executed eight-node source/receiver stencils under the
same frozen solver contract, and record whether the 34-frequency sub-band
worsening pattern moves. The frozen variants were the executed trilinear
control, a nearest-node delta stencil (nearest stencil node, lowest-row
tie-break), and a uniform 1/8 stencil, evaluated over the full 3x3
(source x receiver) cell matrix on the bound dense grid with the same metric,
pairs and worsening-count thresholds.

The PFFDTD 8/10/12 PPW levels were re-executed under the unchanged frozen
solver contract and the canonical cell bound to run 76 before evaluation:
the recombined pressure trace and unit-impulse source trace matched the
frozen run-76 pins at every level (`RUN76_TRACE_IDENTICAL`), and the
canonical cell reproduced the committed run-76 worsening vector and dense
transfer digests exactly. Each non-canonical source variant re-executed the
pinned SimEngine on a byte-copied sim asset set with only the `in_sigs`
weight rows renormalized; the recorded semantic sha256 of the total injected
signal `sum_j in_sigs[j,n]` verified equal to the canonical leg at every
level and variant. Receiver variants recombined the executed raw `u_out`
node traces post-hoc.

All three canonical PFFDTD legs reproduced the PR #295 canonical transfers
exactly (`PASS` at the frozen `1e-9` max absolute complex-component
tolerance). The canonical contract — solver execution, PPW series, duration,
40/80 Hz scored frequencies, window, thresholds, magnitude mask — is
unchanged.

| item | result |
| --- | --- |
| run-76 record binding | `RUN76_TRACE_IDENTICAL` (all 3 levels, canonical cell) |
| total injected signal invariant | held at all 6 source-variant runs |
| evaluation state | `EVALUATED` |
| PR #295 canonical reproduction (PFFDTD leg) | `PASS` |
| stencil worsening pattern | `STENCIL_WORSENING_PATTERN_RECLASSIFIED` |
| canonical MFEM self-convergence | `SELF_CONVERGENCE_FAILED` (unchanged) |
| canonical PFFDTD self-convergence | `SELF_CONVERGENCE_FAILED` (unchanged) |
| cross-solver eligibility | `CROSS_SOLVER_BLOCKED` (unchanged) |
| general-3D validation | `NOT_VALIDATED` (unchanged) |

The diagnostic classification does not alter canonical acceptance.

## Per-cell worsening structure

Each cell reports the frozen dense worsening rule `d_10_12(f) > d_8_10(f)`
over the bound 34-frequency grid; `hamming` is the count of frequencies whose
worsening bit differs from the canonical cell's vector.

| cell (source\|receiver) | worsening /34 | dense classification | hamming |
| --- | --- | --- | --- |
| canonical_trilinear\|canonical_trilinear | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 0 |
| canonical_trilinear\|nearest_node | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 8 |
| canonical_trilinear\|uniform_eight_node | 16 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 8 |
| nearest_node\|canonical_trilinear | 19 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 9 |
| nearest_node\|nearest_node | 5 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 23 |
| nearest_node\|uniform_eight_node | 12 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 14 |
| uniform_eight_node\|canonical_trilinear | 16 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 4 |
| uniform_eight_node\|nearest_node | 15 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 11 |
| uniform_eight_node\|uniform_eight_node | 14 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 6 |

- Every non-canonical cell moves the worsening vector (hamming 4–23 of 34):
  the detailed sub-band pattern is not a frozen artifact of the executed
  trilinear stencil, it shifts under every tested weight representation.
- Seven of eight non-canonical cells keep the canonical
  `DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY` label; the overall mixed character
  is robust to single-axis stencil perturbation.
- `nearest_node|nearest_node` collapses to 5/34 and reclassifies to
  `DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS` — the coarsest
  tested discretization (delta on both ends) largely suppresses the
  adjacent-level worsening.

## Interpretation

The frozen classifier returns `STENCIL_WORSENING_PATTERN_RECLASSIFIED`: at
least one evaluated non-canonical cell carries a different dense
classification than the canonical cell. The detailed sub-band worsening
pattern is sensitive to the source/receiver stencil representation —
worsening positions move under every perturbation — while the aggregate
`DENSE_MIXED` label survives single-axis variants. The simultaneous
nearest-node snap on both ends produces a qualitatively different, largely
localized pattern, which keeps the interpolation/discretization mechanism a
live candidate for part of the run-76 non-monotonicity rather than ruling it
out. This label is a predeclared classification name, not a claim about the
mechanism.

All variant cells remain diagnostic-only and do not enter canonical
acceptance.

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
`docs/reviews/rev34-featureaudit.md` is **boundary/voxel-staircase
discretization sensitivity**: vary only the PFFDTD cartesian
boundary/staircase representation under the same frozen records contract,
and record whether the sub-band worsening pattern moves now that
source/receiver stencil sensitivity is characterized. If a future diagnostic
is predeclared, freeze the boundary variants, evaluation rule, and
classification before reading results.

No execution of this proposed experiment is part of the present slice.

## Verification and provenance

Focused tests: `backend/tests/test_r130d_general3d_validation.py` — 56 passed.
Runner: `scripts/run_r130d_stencil_sensitivity_diagnostic.py` executed
end-to-end on this VM. Canonical-cell records bound `RUN76_TRACE_IDENTICAL`
to the frozen run-76 pins at all three levels; the canonical cell reproduced
the committed run-76 dense worsening vector and per-level transfer digests
exactly; all six source-variant re-executions verified the frozen
total-injected-signal invariant (`sum_j in_sigs[j,n]` semantic sha256 equal
to the canonical leg).
