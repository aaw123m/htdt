# R130D general-3D time-gate localization diagnostic result — 2026-10-03

Issue: #101
Task-start `main`: `ede5eff8be6b4bef552e52c78582df19287d0d3c`
Frozen time-gate diagnostic semantic SHA-256: `32200abe7c7b35e3af82b871793fe417a59b2b911071bfdea4dcd2dede63f89e`
Parent voxel-staircase diagnostic semantic SHA-256: `0e39ec4222d352b4f66560241c05825ce7c9b3792a410a60ac7e16cd6c45af9b`
Runner: `scripts/run_r130d_time_gate_localization_diagnostic.py` (local Devin VM; pinned runtime numpy 1.26.4 / scipy 1.14.1 / numba 0.60.0)
Evidence file SHA-256: `30f12cf845cc3d7b026ca0f83ad2a4d0511c2c57af84405d7d15e3305d172d20`
Evidence semantic SHA-256: `757f536f5bac13b6f86af366d08470284a664451710302fc140da623314ba75b`
Summary semantic SHA-256: `22b9b1564cbd1def933b90325cea45b9fbe7cd37919d31a1b1674d6efab98fd7`

## Result

This slice executed the time-localization experiment that
`R130D_GENERAL3D_VOXEL_STAIRCASE_SENSITIVITY_RESULT_2026-10-03.md`
predeclared verbatim: apply a small frozen set of predeclared causal time
gates to the bound raw pressure traces of the three canonical PFFDTD
levels, re-derive the finite-record transfers on the unchanged
34-frequency dense grid under the same metric, pairs and thresholds, and
record whether the 10→12 sub-band worsening is carried by early
staircase-mediated record energy or is broadband across the record. The
frozen gate set is eleven half-open binary interval cells on the [0,T)
record: the `full_record` control, four causal prefixes ending at
10/25/50/100 ms, four complementary tails starting at 10/25/50/100 ms, and
two interior bands covering 50-150 ms and 150-250 ms. The gating
convention is frozen: gates mask the pressure record with zeros on the
unchanged solver time grid and divide by the unchanged full-record
unit-impulse source spectrum, so disjoint gates decompose the canonical
transfer additively. Degenerate axes were rejected in the frozen plan:
shaped/tapered windows, per-gate source gating, sub-record re-origin, and
any solver re-execution beyond the canonical binding legs.

The PFFDTD 8/10/12 PPW levels were re-executed under the unchanged frozen
solver contract and the canonical cell bound to run 76 before evaluation:
the recombined pressure trace and unit-impulse source trace matched the
frozen run-76 pins at every level (`RUN76_TRACE_IDENTICAL`), and the
canonical cell reproduced the committed run-76 worsening vector and dense
transfer digests exactly. Every gate cell is a pure re-evaluation of those
bound records — no solver state was re-executed for any non-canonical
cell. The frozen additive partition identity held at all three levels:
`prefix_50ms + band_mid_50_150ms + band_late_150_250ms` equals the control
transfer within max absolute complex-component deviations of
4.40e-13 / 7.03e-13 / 5.39e-13 at PPW 8/10/12 (frozen tolerance 1e-12).

All three canonical PFFDTD legs reproduced the PR #295 canonical transfers
exactly (`PASS` at the frozen `1e-9` max absolute complex-component
tolerance). The canonical contract — solver execution, PPW series,
duration, 40/80 Hz scored frequencies, window, thresholds, magnitude
mask — is unchanged.

| item | result |
| --- | --- |
| run-76 record binding | `RUN76_TRACE_IDENTICAL` (all 3 levels, canonical cell) |
| partition identity | held at all 3 levels (max deviation 7.03e-13 < 1e-12) |
| evaluation state | `EVALUATED` |
| PR #295 canonical reproduction (PFFDTD leg) | `PASS` |
| time-gate worsening localization | `TIME_GATE_WORSENING_BROADBAND` |
| canonical MFEM self-convergence | `SELF_CONVERGENCE_FAILED` (unchanged) |
| canonical PFFDTD self-convergence | `SELF_CONVERGENCE_FAILED` (unchanged) |
| cross-solver eligibility | `CROSS_SOLVER_BLOCKED` (unchanged) |
| general-3D validation | `NOT_VALIDATED` (unchanged) |

The diagnostic classification does not alter canonical acceptance.

## Per-cell worsening structure

Each cell reports the frozen dense worsening rule `d_10_12(f) > d_8_10(f)`
over the bound 34-frequency grid; `hamming` is the count of frequencies
whose worsening bit differs from the canonical cell's vector. A gate
"carries" the worsening iff its per-cell dense classification is not
localized (worsening count above the frozen localized maximum 11).

| cell (causal time gate) | gate kind | worsening /34 | dense classification | hamming |
| --- | --- | --- | --- | --- |
| full_record | control | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 0 |
| prefix_10ms | prefix | 0 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 18 |
| prefix_25ms | prefix | 25 | DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD | 17 |
| prefix_50ms | prefix | 6 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 12 |
| prefix_100ms | prefix | 6 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 12 |
| tail_10ms | tail | 19 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 1 |
| tail_25ms | tail | 15 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 7 |
| tail_50ms | tail | 13 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 9 |
| tail_100ms | tail | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 14 |
| band_mid_50_150ms | band | 7 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 15 |
| band_late_150_250ms | band | 22 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 10 |

- `prefix_10ms` — the direct arrival (~2.9 ms) plus the first wall
  reflections carries zero worsening (0/34, localized): the worsening is
  absent from the earliest 10 ms of the record.
- `prefix_25ms` — extending the causal record to 25 ms already produces
  the worsening at 25/34 and reclassifies to
  `DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD`: the worsening is born in
  the 10-25 ms early-reflection window, concentrated in the 36-44 Hz
  sub-band (17/17 low-band bins vs 8/17 high-band).
- `prefix_50ms` / `prefix_100ms` — the worsening count collapses back to
  6/34 localized as the early decay buildup enters the record: the
  mid-record energy partially compensates the early-window excess.
- All four tails carry (13-19/34): removing the first 10-100 ms of the
  record never removes the worsening; the late record alone
  (`tail_100ms`) reproduces the canonical 18/34 count with hamming 14 —
  same count, substantially shifted bins.
- `band_mid_50_150ms` is localized (7/34) while `band_late_150_250ms`
  carries at 22/34, skewed toward the 76-84 Hz sub-band — consistent with
  the tail cells carrying.

## Interpretation

The frozen classifier returns `TIME_GATE_WORSENING_BROADBAND`: at least
one prefix cell carries the worsening (`prefix_25ms`, persisting at 25/34)
and at least one tail cell carries it (all four tails, 13-19/34). The
run-76 dense sub-band worsening is therefore not a property of early
staircase-mediated energy alone, and not a property of the late diffuse
tail alone — it is a broadband property of the record. The onset is
localized in time to the 10-25 ms early-reflection window (prefix_10ms is
worsening-free while prefix_25ms already persists), but no causal gate
removes the worsening: it is regenerated in every tail and amplified again
in the late band. Read together with the voxel-staircase result — where
removing the staircase collapses the worsening and displacing it makes it
universal — the worsening is a boundary-representation property that is
sustained across the whole record rather than a transient staircase
artifact localized at first reflections. This label is a predeclared
classification name, not a claim about the mechanism, and the result does
not license changing the canonical record window.

All gate cells remain diagnostic-only and do not enter canonical
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
amplitude/phase fit, geometry, source/receiver position, or solver
execution change was made.

## Next pre-fixed numerical experiment

The remaining honest mechanism axis inside the frozen contract is
**receiver-position sensitivity of the dense worsening**: the
time-localization result shows the worsening is regenerated across the
record, so the open question is whether it is a property of the canonical
source-receiver measurement pair or a room-global feature of the discrete
boundary. A frozen lattice of receiver offsets (moved `out_ixyz`/`out_alpha`
comms stencils on the unchanged cartesian grid, fail-closed to
air-connected nodes, re-executed variant legs under the same frozen solver
contract) would evaluate the same bound 34-frequency grid and record
whether the worsening vector moves with measurement position. The
experiment must freeze the offset lattice, the stencil-construction rule,
and the classification rule before reading results.

No execution of this proposed experiment is part of the present slice.

## Verification and provenance

Focused tests: `backend/tests/test_r130d_general3d_validation.py` — 76 passed.
Runner: `scripts/run_r130d_time_gate_localization_diagnostic.py`
executed end-to-end on this VM. Canonical-cell records bound
`RUN76_TRACE_IDENTICAL` to the frozen run-76 pins at all three levels; the
canonical cell reproduced the committed run-76 dense worsening vector and
per-level transfer digests exactly; the disjoint partition gates
decomposed the control transfer within the frozen 1e-12 tolerance at every
level; no non-canonical cell executed solver code.
