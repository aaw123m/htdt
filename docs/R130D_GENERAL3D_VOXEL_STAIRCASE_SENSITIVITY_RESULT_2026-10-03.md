# R130D general-3D boundary/voxel-staircase sensitivity diagnostic result — 2026-10-03

Issue: #101
Task-start `main`: `1131897041951af001ce16cf33220db923afc3b3`
Frozen voxel-staircase diagnostic semantic SHA-256: `0e39ec4222d352b4f66560241c05825ce7c9b3792a410a60ac7e16cd6c45af9b`
Parent stencil diagnostic semantic SHA-256: `25229cb1a1c4778bba64ffb0b1a57ed6cc92d87c056b312aa7c061c04406a914`
Runner: `scripts/run_r130d_voxel_staircase_sensitivity_diagnostic.py` (local Devin VM; pinned runtime numpy 1.26.4 / scipy 1.14.1 / numba 0.60.0)
Evidence file SHA-256: `568de256a8b6e053a10c0dfac9a260e9fc26963ba2d29f09630939f095a67e84`
Evidence semantic SHA-256: `f2a2a2164fd24a91048828654fdea763f2dbbf06fb75d3d7b2fd33640066a0a6`
Summary semantic SHA-256: `775e42741a5d1771488820f3645fe187e71f40bf4b4dda473af85db72961ff9f`

## Result

This slice executed the boundary/voxel-staircase discretization-sensitivity
experiment that `R130D_GENERAL3D_STENCIL_SENSITIVITY_RESULT_2026-10-03.md`
predeclared verbatim: vary only the PFFDTD cartesian boundary/staircase
representation inside `vox_out.h5` — the `bn_ixyz` boundary-node set and the
`adj_bn` six-direction blocked-neighbor mask — under the same frozen solver
contract, and record whether the 34-frequency sub-band worsening pattern
moves. The frozen variants were the executed canonical control, a one-cell
inward dilation of the staircase layer (each appended node inherits the
adjacency row of its lowest-linear-index canonical boundary neighbor), a
drop of fully-blocked near-hit nodes, and the two adjacency brackets
(all-open and fully-blocked), evaluated over the bound dense grid with the
same metric, pairs and worsening-count thresholds. Degenerate axes were
rejected in the frozen plan: adjacency symmetrization (the spatial
diagnostic already proved blocked-edge symmetry in the executed data),
`saf_bn`/`mat_bn` representation (rigid-only fixture makes them exact
no-ops), and grid-phase re-registration (moves the pinned source/receiver
positions).

The PFFDTD 8/10/12 PPW levels were re-executed under the unchanged frozen
solver contract and the canonical cell bound to run 76 before evaluation:
the recombined pressure trace and unit-impulse source trace matched the
frozen run-76 pins at every level (`RUN76_TRACE_IDENTICAL`), and the
canonical cell reproduced the committed run-76 worsening vector and dense
transfer digests exactly. Each non-canonical variant re-executed the pinned
SimEngine on a byte-copied sim asset set with only `vox_out.h5` rewritten;
the comms source/receiver authority, solver constants, and grid axes were
sha256-verified byte-identical to the canonical leg at every variant run,
and every mutated boundary kept the source stencil connected in the air
domain (fail-closed connectivity gate held).

All three canonical PFFDTD legs reproduced the PR #295 canonical transfers
exactly (`PASS` at the frozen `1e-9` max absolute complex-component
tolerance). The canonical contract — solver execution, PPW series, duration,
40/80 Hz scored frequencies, window, thresholds, magnitude mask — is
unchanged.

| item | result |
| --- | --- |
| run-76 record binding | `RUN76_TRACE_IDENTICAL` (all 3 levels, canonical cell) |
| frozen comms/consts/grid authority | byte-identical at all 12 variant runs |
| air-domain connectivity gate | held at all 12 variant runs |
| evaluation state | `EVALUATED` |
| PR #295 canonical reproduction (PFFDTD leg) | `PASS` |
| voxel-staircase worsening pattern | `VOXEL_STAIRCASE_WORSENING_PATTERN_RECLASSIFIED` |
| canonical MFEM self-convergence | `SELF_CONVERGENCE_FAILED` (unchanged) |
| canonical PFFDTD self-convergence | `SELF_CONVERGENCE_FAILED` (unchanged) |
| cross-solver eligibility | `CROSS_SOLVER_BLOCKED` (unchanged) |
| general-3D validation | `NOT_VALIDATED` (unchanged) |

The diagnostic classification does not alter canonical acceptance.

## Per-cell worsening structure

Each cell reports the frozen dense worsening rule `d_10_12(f) > d_8_10(f)`
over the bound 34-frequency grid; `hamming` is the count of frequencies
whose worsening bit differs from the canonical cell's vector.

| cell (boundary/staircase variant) | worsening /34 | dense classification | hamming |
| --- | --- | --- | --- |
| canonical_voxelization | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 0 |
| dilated_boundary_layer | 34 | DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD | 16 |
| near_boundary_nodes_as_air | 18 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 0 |
| open_boundary_as_air | 3 | DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS | 19 |
| fully_blocked_boundary | 20 | DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY | 16 |

- `dilated_boundary_layer` — displacing the staircase up to one cell into
  the air domain (appended 752/1344/1904 boundary nodes at 8/10/12 PPW)
  reclassifies to `DENSE_NON_MONOTONICITY_PERSISTS_NEIGHBORHOOD`: the
  worsening becomes universal across the grid (34/34). The observed
  non-monotonicity is amplified, not removed, by a shifted staircase.
- `near_boundary_nodes_as_air` is an exact no-op by data: the canonical
  boundary contains zero fully-blocked near-hit nodes at every level, so
  the variant reproduces the canonical vector bit-for-bit (hamming 0) — a
  recorded degenerate-variant consistency check, not evidence.
- `open_boundary_as_air` — releasing the staircase entirely (all six
  adjacency directions open) reclassifies to
  `DENSE_NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS` (3/34): with no
  boundary applied the adjacent-level worsening essentially vanishes, so
  the worsening pattern requires the staircase boundary to exist at all.
- `fully_blocked_boundary` — freezing every boundary node (zero open
  directions) keeps the `DENSE_MIXED` label but shifts the detailed vector
  (20/34, hamming 16), so the pattern is also sensitive to the
  adjacency-magnitude convention.

## Interpretation

The frozen classifier returns
`VOXEL_STAIRCASE_WORSENING_PATTERN_RECLASSIFIED`: at least one evaluated
non-canonical cell carries a different dense classification than the
canonical cell — here two of four do. Read together with the stencil
result, the run-76 dense sub-band worsening is sensitive to the
boundary/staircase representation as well: removing the wall collapses the
worsening to the canonical bins (3/34 localized), while displacing the
staircase one cell inward makes it persist at every evaluated frequency
(34/34). The worsening pattern is therefore a property of the discrete
boundary/staircase layer rather than a stencil-only artifact or a
representation-independent feature of the geometry. This label is a
predeclared classification name, not a claim about the mechanism, and the
result does not license changing the canonical voxelization.

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
amplitude/phase fit, geometry, source/receiver position, or solver
execution change was made.

## Next pre-fixed numerical experiment

The remaining honest mechanism axis inside the frozen contract is
**time-localization of the dense sub-band worsening**: apply a small frozen
set of predeclared causal time gates to the bound raw pressure traces
(e.g. direct/early-reflection interval vs late tail), re-derive the
finite-record transfers on the unchanged 34-frequency grid, and record
whether the 10→12 worsening is carried by early staircase-mediated energy
or is broadband across the record. The experiment needs no new solver
executions — it re-evaluates the same bound raw records — and must freeze
the gate set, the gating convention, and the classification rule before
reading results.

No execution of this proposed experiment is part of the present slice.

## Verification and provenance

Focused tests: `backend/tests/test_r130d_general3d_validation.py` — 67 passed.
Runner: `scripts/run_r130d_voxel_staircase_sensitivity_diagnostic.py`
executed end-to-end on this VM. Canonical-cell records bound
`RUN76_TRACE_IDENTICAL` to the frozen run-76 pins at all three levels; the
canonical cell reproduced the committed run-76 dense worsening vector and
per-level transfer digests exactly; all twelve boundary-variant
re-executions verified byte-identical comms/consts/grid authority and kept
the source stencil connected in the air domain.
