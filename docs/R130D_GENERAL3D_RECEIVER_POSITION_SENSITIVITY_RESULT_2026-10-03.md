# R130D general-3D receiver-position sensitivity diagnostic result — 2026-10-03

Issue: #101
Task-start `main`: `98da71aea3958f0ed0e6729c45ad5eff0c44c92f`
Frozen receiver-position diagnostic semantic SHA-256: `aa984be8893ff1a9b7f7f02b08acbf63e171ed41c27555212566579a9c95d6a7`
Parent time-gate diagnostic semantic SHA-256: `32200abe7c7b35e3af82b871793fe417a59b2b911071bfdea4dcd2dede63f89e`
Runner: `scripts/run_r130d_receiver_position_sensitivity_diagnostic.py` (local Devin VM; pinned runtime numpy 1.26.4 / scipy 1.14.1 / numba 0.60.0)
Evidence file SHA-256: `4290f58727534a13a980b35b664ec6de47f9dfdfc2c73bb32d97543754f17ff4`
Evidence semantic SHA-256: `d115bc645ad8e1120be396730706ea72db61128174d51cb458bf3ad3e729bf8e`
Summary semantic SHA-256: `40adad47c1d1632f0ae39ac9550be7cdc58632a9bf26af2d0aabe44cefc043a5`

## Result

This slice executed the receiver-position sensitivity experiment that
`R130D_GENERAL3D_TIME_GATE_LOCALIZATION_RESULT_2026-10-03.md` predeclared
verbatim: move only the receiver comms stencil over a frozen whole-cell
offset lattice on each level's own cartesian grid, re-execute the pinned
SimEngine per moved cell, re-derive the finite-record transfers on the
unchanged 34-frequency dense grid under the same metric, pairs and
thresholds, and record whether the 10→12 sub-band worsening moves with
measurement position.

The frozen lattice is thirteen cells: the `canonical_position` control
and twelve whole-cell offsets ±1 and ±2 along each cartesian axis
(x_minus/x_plus/y_minus/y_plus/z_minus/z_plus, 1 and 2 cells —
~0.43/0.34/0.29 m per cell at PPW 8/10/12). The stencil construction rule
is frozen: a whole-cell offset is exactly the canonical eight-node
`out_ixyz` trilinear set rigidly translated by the offset vector with the
canonical `out_alpha` weight row unchanged — a pure readout-position move
with no stencil-weight confound. Every moved cell is fail-closed: all
eight moved nodes must stay in-grid, off the outer absorbing-layer
planes, and air-connected under the frozen six-neighbor cartesian BFS of
the canonical boundary; all twelve cells passed at all three levels.
Each variant leg re-executes the pinned SimEngine on a byte-copied sim
asset set (`vox_out.h5`, `comms_out.h5`, `sim_consts.h5`, `sim_mats.h5`)
with only the `out_ixyz` node set replaced by the frozen moved stencil;
the source stencil (`in_ixyz`/`in_sigs`), `out_alpha`, `Ns`/`Nr`/`Nt`, the
boundary/staircase representation, the cartesian grid, and the solver
constants are sha256-verified byte-identical to the canonical leg.
Degenerate axes were rejected in the frozen plan: fractional or sub-cell
offsets (which would change `out_alpha` and confound the already-run
stencil-weight axis), meter-fixed offsets, source-stencil or joint
source-receiver moves, `out_alpha` reweighting, multi-receiver readouts,
and any boundary/material/solver-constant change.

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
| receiver-position worsening pattern | `RECEIVER_POSITION_WORSENING_POSITION_LOCAL` |
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

| cell (whole-cell receiver offset) | offset | worsening /34 | dense classification | hamming |
| --- | --- | --- | --- | --- |
| canonical_position | (0,0,0) control | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 0 |
| x_minus_1 | (−1,0,0) toward source | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 16 |
| x_plus_1 | (+1,0,0) from source | 15 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 11 |
| x_minus_2 | (−2,0,0) toward source | 27 | DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD | 15 |
| x_plus_2 | (+2,0,0) from source | 15 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 11 |
| y_minus_1 | (0,−1,0) lateral | 14 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 12 |
| y_plus_1 | (0,+1,0) toward slope | 13 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 9 |
| y_minus_2 | (0,−2,0) lateral | 4 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 20 |
| y_plus_2 | (0,+2,0) toward slope | 12 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 12 |
| z_minus_1 | (0,0,−1) from ceiling | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 8 |
| z_plus_1 | (0,0,+1) toward ceiling | 11 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 9 |
| z_minus_2 | (0,0,−2) toward floor | 14 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 12 |
| z_plus_2 | (0,0,+2) toward ceiling | 9 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 11 |

- No moved cell reproduces the canonical worsening vector: every
  non-control cell's 34-bit vector differs (hamming 8–20) — the worsening
  *pattern* is readout-position-dependent everywhere on the lattice.
- `y_minus_2` (4/34), `z_plus_1` (11/34), `z_plus_2` (9/34) are fully
  localized: moving the receiver one or two cells in those directions
  removes the 10→12 worsening outright under the frozen count rule.
- `x_minus_2` — two cells toward the source — amplifies the worsening to
  27/34 and reclassifies to `DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD`.
- The remaining eight cells keep a mixed dense classification at 12–18/34
  with shifted bins (hamming 8–16): the worsening persists but at
  different sub-band frequencies.

## Interpretation

The frozen classifier returns `RECEIVER_POSITION_WORSENING_POSITION_LOCAL`:
at least one moved receiver cell does not carry the worsening (three of
twelve, at `y_minus_2`, `z_plus_1`, `z_plus_2`). The run-76 dense
sub-band worsening is therefore not a room-global feature of the discrete
boundary solution: it is a property of the canonical measurement position.
Every probed offset changes the worsening vector, and three offsets —
one lateral step toward the wall side and two steps toward the sloped
ceiling — eliminate the worsening under the frozen rule, while the step
closest to the source amplifies it to neighborhood-persistent strength.
Read together with the time-gate result — where the worsening is born in
the 10–25 ms early-reflection window and regenerated broadband across the
record — the worsening is consistent with an early-reflection
interference structure at the canonical source-receiver pair that moves
with receiver position rather than a boundary-solver defect present
throughout the room. This label is a predeclared classification name, not
a claim about the mechanism, and the result does not license moving the
canonical receiver position.

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
**source-position sensitivity of the dense worsening**: the
receiver-position result shows the worsening is readout-position-local,
so the open question is whether it is a property of the canonical
measurement *pair* — i.e. whether it also moves with the source — or
specifically of the receiver readout location. A frozen lattice of source
offsets (moved `in_ixyz` comms stencils on the unchanged cartesian grid,
the injected signal `in_sigs` unchanged, fail-closed to air-connected
nodes, re-executed variant legs under the same frozen solver contract)
would evaluate the same bound 34-frequency grid and record whether the
worsening vector moves with source position. The experiment must freeze
the offset lattice, the stencil-construction rule, and the classification
rule before reading results. If the worsening is also source-position
local, the worsening is a property of the source-receiver pair geometry;
if it persists under all probed source moves, it is specifically
receiver-local.

No execution of this proposed experiment is part of the present slice.

## Verification and provenance

Focused tests: `backend/tests/test_r130d_general3d_validation.py`.
Runner: `scripts/run_r130d_receiver_position_sensitivity_diagnostic.py`
executed end-to-end on this VM. Canonical-cell records bound
`RUN76_TRACE_IDENTICAL` to the frozen run-76 pins at all three levels; the
canonical cell reproduced the committed run-76 dense worsening vector and
per-level transfer digests exactly; every moved stencil was verified as a
rigid whole-cell translation of the canonical trilinear set
(reconstruction error ≤ 4.5e-16 m), stayed in-grid, off the outer
absorbing planes and air-connected under the frozen six-neighbor BFS; and
every variant leg preserved the frozen vox/consts/source-comms authority
sha256-identical to the canonical leg.
