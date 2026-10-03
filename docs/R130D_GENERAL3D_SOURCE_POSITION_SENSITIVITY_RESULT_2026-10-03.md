# R130D general-3D source-position sensitivity diagnostic result — 2026-10-03

Issue: #101
Task-start `main`: `aecf6842cabe91535781534156390b2ee247e844`
Frozen source-position diagnostic semantic SHA-256: `f4ad35dd500b80e000cd1efffbde8a301ad74b69bc8df0656dedf91044fef52c`
Parent receiver-position diagnostic semantic SHA-256: `aa984be8893ff1a9b7f7f02b08acbf63e171ed41c27555212566579a9c95d6a7`
Runner: `scripts/run_r130d_source_position_sensitivity_diagnostic.py` (local Devin VM; pinned runtime numpy 1.26.4 / scipy 1.14.1 / numba 0.60.0)
Evidence file SHA-256: `43f2e795361a2ef8694760fcb7c47714e2e071a2920824c2af34bbc4f69831b4`
Evidence semantic SHA-256: `4b39aa2c80868b98c318bac286de770673d5963d2793cbb1625763c3c8f29cad`
Summary semantic SHA-256: `f1ecf4423e9027da54f109a93507e928f997af9a360c343fae85d8e04f518184`

## Result

This slice executed the source-position sensitivity experiment that
`R130D_GENERAL3D_RECEIVER_POSITION_SENSITIVITY_RESULT_2026-10-03.md`
predeclared verbatim: move only the source comms stencil over a frozen
whole-cell offset lattice on each level's own cartesian grid, re-execute
the pinned SimEngine per moved cell, re-derive the finite-record
transfers on the unchanged 34-frequency dense grid under the same metric,
pairs and thresholds, and record whether the 10→12 sub-band worsening
moves with injection position.

The frozen lattice is thirteen cells: the `canonical_position` control
and twelve whole-cell offsets ±1 and ±2 along each cartesian axis
(x_minus/x_plus/y_minus/y_plus/z_minus/z_plus, 1 and 2 cells —
~0.43/0.34/0.29 m per cell at PPW 8/10/12). The stencil construction rule
is frozen: a whole-cell offset is exactly the canonical eight-node
`in_ixyz` trilinear set rigidly translated by the offset vector with the
canonical injected-signal rows `in_sigs` unchanged — a pure
injection-position move with no stencil-weight confound. Every moved cell
is fail-closed: all eight moved nodes must stay in-grid, off the outer
absorbing-layer planes, and air-connected under the frozen six-neighbor
cartesian BFS of the canonical boundary; all twelve cells passed at all
three levels. Each variant leg re-executes the pinned SimEngine on a
byte-copied sim asset set (`vox_out.h5`, `comms_out.h5`, `sim_consts.h5`,
`sim_mats.h5`) with only the `in_ixyz` node set replaced by the frozen
moved stencil; the receiver stencil (`out_ixyz`/`out_alpha`), `in_sigs`,
`Ns`/`Nr`/`Nt`, the boundary/staircase representation, the cartesian
grid, and the solver constants are sha256-verified byte-identical to the
canonical leg. Degenerate axes were rejected in the frozen plan:
fractional or sub-cell offsets (which would change `in_sigs` and confound
the already-run stencil-weight axis), meter-fixed offsets,
receiver-stencil or joint source-receiver moves, `in_sigs` reweighting,
multi-source injection, and any boundary/material/solver-constant change.

The PFFDTD 8/10/12 PPW levels were re-executed under the unchanged frozen
solver contract and the canonical cell bound to run 76 before evaluation:
the recombined pressure trace and unit-impulse source trace matched the
frozen run-76 pins at every level (`RUN76_TRACE_IDENTICAL`), and the
canonical cell reproduced the committed run-76 dense worsening vector and
per-level transfer digests exactly. All 36 non-control variant legs were
executed fresh and their moved-stencil/node-set digests are recorded per
level.

All three canonical PFFDTD legs reproduced the PR #295 canonical transfers
exactly (`PASS` at the frozen `1e-9` max absolute complex-component
tolerance; worst error 4.26e-13). The canonical contract — solver
execution, PPW series, duration, 40/80 Hz scored frequencies, window,
thresholds, magnitude mask — is unchanged.

| item | result |
| --- | --- |
| run-76 record binding | `RUN76_TRACE_IDENTICAL` (all 3 levels, canonical cell) |
| moved-stencil feasibility | 12/12 cells executable at all 3 levels (in-grid, off-halo, air-connected) |
| variant re-executions | 36 (12 moved cells × 3 PPW levels), contract invariants sha256-identical |
| evaluation state | `EVALUATED` |
| PR #295 canonical reproduction (PFFDTD leg) | `PASS` |
| source-position worsening pattern | `SOURCE_POSITION_WORSENING_POSITION_LOCAL` |
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

| cell (whole-cell source offset) | offset | worsening /34 | dense classification | hamming |
| --- | --- | --- | --- | --- |
| canonical_position | (0,0,0) control | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 0 |
| x_minus_1 | (−1,0,0) away from receiver | 15 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 11 |
| x_plus_1 | (+1,0,0) toward receiver | 15 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 15 |
| x_minus_2 | (−2,0,0) away from receiver | 16 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 12 |
| x_plus_2 | (+2,0,0) toward receiver | 26 | DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD | 14 |
| y_minus_1 | (0,−1,0) lateral | 14 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 12 |
| y_plus_1 | (0,+1,0) toward slope | 13 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 9 |
| y_minus_2 | (0,−2,0) lateral | 4 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 20 |
| y_plus_2 | (0,+2,0) toward slope | 12 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 12 |
| z_minus_1 | (0,0,−1) toward floor | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 8 |
| z_plus_1 | (0,0,+1) toward ceiling | 11 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 9 |
| z_minus_2 | (0,0,−2) toward floor | 14 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 12 |
| z_plus_2 | (0,0,+2) toward ceiling | 9 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 11 |

- No moved cell reproduces the canonical worsening vector: every
  non-control cell's 34-bit vector differs (hamming 8–20) — the worsening
  *pattern* is injection-position-dependent everywhere on the lattice.
- `y_minus_2` (4/34), `z_plus_1` (11/34), `z_plus_2` (9/34) are fully
  localized: moving the source one or two cells in those directions
  removes the 10→12 worsening outright under the frozen count rule — the
  same three direction cells that localized under the receiver axis.
- `x_plus_2` — two cells toward the receiver — amplifies the worsening to
  26/34 and reclassifies to `DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD`,
  mirroring the receiver axis where `x_minus_2` (two cells toward the
  source, i.e. the same shorter pair separation) amplified it to 27/34.
- The remaining eight cells keep a mixed dense classification at 12–18/34
  with shifted bins (hamming 8–15): the worsening persists but at
  different sub-band frequencies.

## Interpretation

The frozen classifier returns `SOURCE_POSITION_WORSENING_POSITION_LOCAL`:
at least one moved source cell does not carry the worsening (three of
twelve, at `y_minus_2`, `z_plus_1`, `z_plus_2`). Combined with the
receiver-position result (`RECEIVER_POSITION_WORSENING_POSITION_LOCAL`),
the run-76 dense sub-band worsening is therefore a property of the
canonical source-receiver *pair* — it moves when either endpoint of the
measurement moves — and not a defect localized to the receiver readout or
a room-global feature of the discrete boundary solution. The mirrored
amplification under pair-shortening offsets (receiver `x_minus_2` → 27/34,
source `x_plus_2` → 26/34) and the identical localized direction set both
point the same way: the worsening is consistent with an early-reflection
interference structure tied to the canonical measurement pair, born in
the 10–25 ms window per the time-gate axis, that redistributes over the
34-bin grid when the pair geometry changes. This label is a predeclared
classification name, not a claim about the mechanism, and the result does
not license moving the canonical source position.

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

## Next pre-fixed numerical experiment

The remaining honest mechanism axis inside the frozen contract is
**joint source-receiver rigid-translation sensitivity of the dense
worsening**: each single-endpoint axis showed the worsening is
position-local, but those moves both changed the pair separation *and*
the pair's absolute coordinates inside the room's modal field — the open
question is which of those the worsening is bound to. A frozen lattice of
joint offsets (both `in_ixyz` and `out_ixyz` comms stencils rigidly
translated by the same whole-cell vector on the unchanged cartesian grid,
`in_sigs` and `out_alpha` unchanged, pair separation vector held fixed,
fail-closed to in-grid/off-halo/air-connected nodes, re-executed variant
legs under the same frozen solver contract) would evaluate the same bound
34-frequency grid and record whether the worsening vector survives a
translation that preserves relative geometry. If the worsening persists
under joint translation, it is a property of the pair's relative
separation/orientation alone; if it dissolves or shifts, it is bound to
the pair's absolute position in the room's modal structure rather than to
the pair geometry. The experiment must freeze the offset lattice, the
stencil-construction rule, and the classification rule before reading
results.

No execution of this proposed experiment is part of the present slice.

## Verification and provenance

Focused tests: `backend/tests/test_r130d_general3d_validation.py`.
Runner: `scripts/run_r130d_source_position_sensitivity_diagnostic.py`
executed end-to-end on this VM. Canonical-cell records bound
`RUN76_TRACE_IDENTICAL` to the frozen run-76 pins at all three levels; the
canonical cell reproduced the committed run-76 dense worsening vector and
per-level transfer digests exactly; every moved stencil was verified as a
rigid whole-cell translation of the canonical trilinear set
(reconstruction error ≤ 1.1e-15 m), stayed in-grid, off the outer
absorbing planes and air-connected under the frozen six-neighbor BFS; and
every variant leg preserved the frozen vox/consts/receiver-comms
authority sha256-identical to the canonical leg.
