# R130D general-3D joint source-receiver rigid-translation sensitivity diagnostic result — 2026-10-03

Issue: #101
Task-start `main`: `9c6d1582d5919f26d24e78757660b230aa6eeab1`
Frozen joint-translation diagnostic semantic SHA-256: `c979f661ab38148148b13cdcc87f57dd8c04126c62c819eb44817bd66df77c4e`
Parent source-position diagnostic semantic SHA-256: `f4ad35dd500b80e000cd1efffbde8a301ad74b69bc8df0656dedf91044fef52c`
Runner: `scripts/run_r130d_joint_translation_sensitivity_diagnostic.py` (local Devin VM; pinned runtime numpy 1.26.4 / scipy 1.14.1 / numba 0.60.0)
Evidence file SHA-256: `10ae068e583e679edf774ef6a461c80fc128b278306162e985f322110ffe8f4d`
Evidence semantic SHA-256: `25cfdb7c9ea154d9e820d15b04ec85da83d396bd2073045889588de1a8174458`
Summary semantic SHA-256: `dac247bac3ef67264aaac59c19dc6ee4bd745aaf0e0420e5b12aa6c00909f8bd`

## Result

This slice executed the joint source-receiver rigid-translation
sensitivity experiment that
`R130D_GENERAL3D_SOURCE_POSITION_SENSITIVITY_RESULT_2026-10-03.md`
predeclared verbatim: apply an identical whole-cell offset to BOTH comms
stencils — the source `in_ixyz` trilinear set and the receiver `out_ixyz`
trilinear set — on each level's own cartesian grid with the pair
separation vector held fixed, re-execute the pinned SimEngine per moved
cell, re-derive the finite-record transfers on the unchanged 34-frequency
dense grid under the same metric, pairs and thresholds, and record
whether the 10→12 sub-band worsening vector survives a translation that
preserves the pair's relative geometry.

The frozen lattice is thirteen cells: the `canonical_position` control
and twelve whole-cell offsets ±1 and ±2 along each cartesian axis
(x_minus/x_plus/y_minus/y_plus/z_minus/z_plus, 1 and 2 cells —
~0.43/0.34/0.29 m per cell at PPW 8/10/12). The stencil construction rule
is frozen: a joint whole-cell offset is exactly the canonical eight-node
`in_ixyz` set AND the canonical eight-node `out_ixyz` set rigidly
translated by the same offset vector, with the canonical
injected-signal rows `in_sigs` and readout weight row `out_alpha`
unchanged — a pure pair translation with no stencil-weight confound and
no separation change. Every moved cell is fail-closed: all sixteen moved
nodes (eight per stencil) must stay in-grid, off the outer
absorbing-layer planes, and air-connected under the frozen six-neighbor
cartesian BFS of the canonical boundary; the moved node coordinates must
be the canonical coordinates translated by the exact offset vector; the
unchanged canonical weights must still interpolate the moved positions;
and the moved pair separation must equal the canonical separation. All
twelve offset cells passed at all three levels. Each variant leg
re-executes the pinned SimEngine on a byte-copied sim asset set
(`vox_out.h5`, `comms_out.h5`, `sim_consts.h5`, `sim_mats.h5`) with only
the `in_ixyz` and `out_ixyz` node sets replaced by the frozen moved
stencils; `in_sigs`, `out_alpha`, `Ns`/`Nr`/`Nt`, the boundary/staircase
representation, the cartesian grid, and the solver constants are
sha256-verified byte-identical to the canonical leg. Degenerate axes were
rejected in the frozen plan: fractional or sub-cell offsets, meter-fixed
offsets, unequal source-vs-receiver offsets (a separation change — already
covered by the single-endpoint axes), `in_sigs`/`out_alpha` reweighting,
multi-node readout, and any boundary/material/solver-constant change.

The PFFDTD 8/10/12 PPW levels were re-executed under the unchanged frozen
solver contract and the canonical cell bound to run 76 before evaluation:
the recombined pressure trace and unit-impulse source trace matched the
frozen run-76 pins at every level (`RUN76_TRACE_IDENTICAL`), and the
canonical cell reproduced the committed run-76 dense worsening vector and
per-level transfer digests exactly. All 36 non-control variant legs were
executed fresh and their moved source/receiver stencil digests are
recorded per level; pair-separation preservation error was ≤ 1e-12 m on
every moved cell.

All three canonical PFFDTD legs reproduced the PR #295 canonical transfers
exactly (`PASS` at the frozen `1e-9` max absolute complex-component
tolerance; worst error 4.26e-13). The canonical contract — solver
execution, PPW series, duration, 40/80 Hz scored frequencies, window,
thresholds, magnitude mask — is unchanged.

| item | result |
| --- | --- |
| run-76 record binding | `RUN76_TRACE_IDENTICAL` (all 3 levels, canonical cell) |
| moved-pair feasibility | 12/12 cells executable at all 3 levels (in-grid, off-halo, air-connected, separation preserved) |
| variant re-executions | 36 (12 moved cells × 3 PPW levels), contract invariants sha256-identical |
| evaluation state | `EVALUATED` |
| PR #295 canonical reproduction (PFFDTD leg) | `PASS` |
| joint-translation worsening pattern | `JOINT_TRANSLATION_WORSENING_ABSOLUTE_POSITION_BOUND` |
| canonical MFEM self-convergence | `SELF_CONVERGENCE_FAILED` (unchanged) |
| canonical PFFDTD self-convergence | `SELF_CONVERGENCE_FAILED` (unchanged) |
| cross-solver eligibility | `CROSS_SOLVER_BLOCKED` (unchanged) |
| general-3D validation | `NOT_VALIDATED` (unchanged) |

The diagnostic classification does not alter canonical acceptance.

## Per-cell worsening structure

Each cell reports the frozen dense worsening rule `d_10_12(f) > d_8_10(f)`
over the bound 34-frequency grid; `hamming` is the count of frequencies
whose worsening bit differs from the canonical cell's vector. A moved
cell "carries" the worsening iff its per-cell dense classification is not
localized (worsening count above the frozen localized maximum 11).

| cell (whole-cell joint offset) | offset | worsening /34 | dense classification | hamming |
| --- | --- | --- | --- | --- |
| canonical_position | (0,0,0) control | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 0 |
| x_minus_1 | (−1,0,0) | 24 | DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD | 8 |
| x_plus_1 | (+1,0,0) | 22 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 8 |
| x_minus_2 | (−2,0,0) | 19 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 11 |
| x_plus_2 | (+2,0,0) | 24 | DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD | 8 |
| y_minus_1 | (0,−1,0) lateral | 11 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 15 |
| y_plus_1 | (0,+1,0) toward slope | 16 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 10 |
| y_minus_2 | (0,−2,0) lateral | 2 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 20 |
| y_plus_2 | (0,+2,0) toward slope | 4 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 18 |
| z_minus_1 | (0,0,−1) toward floor | 14 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 6 |
| z_plus_1 | (0,0,+1) toward ceiling | 9 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 9 |
| z_minus_2 | (0,0,−2) toward floor | 20 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 6 |
| z_plus_2 | (0,0,+2) toward ceiling | 14 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 10 |

- No moved cell reproduces the canonical worsening vector: every
  non-control cell's 34-bit vector differs (hamming 6–20) even though the
  pair separation vector is held exactly fixed — the worsening *pattern*
  is bound to where the pair sits in the room, not to the pair's
  relative geometry.
- `y_minus_1` (11/34), `y_minus_2` (2/34), `y_plus_2` (4/34) and
  `z_plus_1` (9/34) are fully localized: translating the pair rigidly in
  those directions removes the 10→12 worsening outright under the frozen
  count rule. `y_minus_2` and `z_plus_1` localized on all three position
  axes (receiver, source, joint); the lateral directions dominate the
  dissolution on every axis.
- The separation-axis (x) slides do not dissolve the worsening — they
  keep or amplify it (x_minus_1 → 24/34, x_plus_1 → 22/34,
  x_plus_2 → 24/34 persisting): sliding the pair along its own axis
  samples different absolute positions inside the same interference
  structure rather than removing it.
- The remaining cells keep a mixed dense classification at 14–20/34 with
  shifted bins (hamming 6–11): the worsening persists but at different
  sub-band frequencies.

## Interpretation

The frozen classifier returns
`JOINT_TRANSLATION_WORSENING_ABSOLUTE_POSITION_BOUND`: at least one moved
joint cell does not carry the worsening (four of twelve, at `y_minus_1`,
`y_minus_2`, `y_plus_2`, `z_plus_1`) while no moved cell preserves the
canonical vector exactly. This is the decisive disambiguation the axis
was predeclared to deliver: under a translation that preserves the pair
separation vector to ≤ 1e-12 m — no change to relative geometry, no
change to `in_sigs`/`out_alpha`, no change to the boundary — the
worsening vector never survives intact, and in a third of the moved cells
it dissolves outright. The run-76 dense sub-band worsening is therefore
bound to the pair's *absolute* position inside the room's modal field,
not to the pair's relative separation or orientation.

Read across the three position axes, the picture is consistent and
closed: moving only the receiver
(`RECEIVER_POSITION_WORSENING_POSITION_LOCAL`), only the source
(`SOURCE_POSITION_WORSENING_POSITION_LOCAL`), or both rigidly
(`JOINT_TRANSLATION_WORSENING_ABSOLUTE_POSITION_BOUND`) all reshuffle or
remove the same 34-bin pattern — the worsening tracks the absolute
placement of the measurement configuration in the room, with
perpendicular (lateral/vertical) translations dissolving it most strongly
on every axis. The mirrored pair-shortening amplification seen on the
single-endpoint axes was therefore a separation-geometry effect *within*
a position-bound pattern, not the pattern's cause. This label is a
predeclared classification name, not a claim about the mechanism, and the
result does not license moving the canonical source or receiver
positions.

All moved cells remain diagnostic-only and do not enter canonical
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

## R130D diagnostic campaign status: EXHAUSTED

This axis was predeclared as the remaining honest mechanism axis inside
the frozen solver contract, and it completes the comms-stencil maneuver
space: receiver-only movement (REV39-R130D5), source-only movement
(REV39-R130D6), and joint rigid translation (this slice) are the three
combinations the frozen contract admits — every other pair
reconfiguration is either a forbidden change (boundary, materials,
solver constants, `in_sigs`/`out_alpha` weights, PPW series, duration,
window, scored-frequency set, grid) or a degenerate recombination of axes
already executed (separation changes were covered by the x-axis cells of
the single-endpoint axes; rotations confound relative and absolute
geometry and add no separable information). No further software-only
sensitivity axis remains inside the frozen contract.

The consolidated verdict table and campaign-level conclusion are recorded
in `docs/R130D_GENERAL3D_DIAGNOSTIC_CAMPAIGN_SUMMARY_2026-10-03.md`. The
characterization the campaign reached — a broadband,
boundary-representation-dependent worsening bound to the absolute
position of the canonical measurement pair, with onset in the early
part of the record — means the remaining question (whether PFFDTD's
denser sub-band structure is the physically truer answer or a
position-bound numerical feature) is not decidable by further
frozen-contract PFFDTD re-execution; it belongs to a different program
(MFEM-side diagnostics or physics-contract changes), not to the R130D
diagnostic chain.

No further pre-fixed numerical experiment is declared.

## Verification and provenance

Focused tests: `backend/tests/test_r130d_general3d_validation.py`.
Runner: `scripts/run_r130d_joint_translation_sensitivity_diagnostic.py`
executed end-to-end on this VM. Canonical-cell records bound
`RUN76_TRACE_IDENTICAL` to the frozen run-76 pins at all three levels; the
canonical cell reproduced the committed run-76 dense worsening vector and
per-level transfer digests exactly; every moved pair was verified as a
rigid whole-cell translation of both canonical trilinear sets
(reconstruction error ≤ 1e-9 m, pair-separation preservation error
≤ 1e-12 m), stayed in-grid, off the outer absorbing planes and
air-connected under the frozen six-neighbor BFS; and every variant leg
preserved the frozen vox/consts/in_sigs/out_alpha/Ns/Nr/Nt authority
sha256-identical to the canonical leg.
