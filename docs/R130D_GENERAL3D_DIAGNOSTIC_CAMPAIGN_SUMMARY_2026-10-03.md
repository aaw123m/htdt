# R130D general-3D diagnostic campaign — consolidated summary — 2026-10-03

Issue: #101
Status: **CAMPAIGN EXHAUSTED** — declared by
`R130D_GENERAL3D_JOINT_TRANSLATION_SENSITIVITY_RESULT_2026-10-03.md`
(REV39-R130D7), which executed the last remaining predeclared software-only
sensitivity axis inside the frozen solver contract.

## What the campaign set out to characterize

The R130D sloped-room validation fixture leaves a residual: on the bound
34-frequency dense grid, the PFFDTD refinement series shows a dense
sub-band worsening — `d_10_12(f) > d_8_10(f)` at 18/34 frequencies in the
canonical configuration — while the canonical states stay
`SELF_CONVERGENCE_FAILED` / `CROSS_SOLVER_BLOCKED` / `NOT_VALIDATED`. The
campaign ran a chain of predeclared, hash-bound, diagnostic-only
experiments to localize what that worsening is a property *of*, without
ever touching the canonical acceptance contract. Every axis froze its
plan (semantic-sha256-pinned in the validation module), re-executed or
re-derived under unchanged contract invariants, bound the control cell to
run 76 (`RUN76_TRACE_IDENTICAL`) and reproduced the PR #295 canonical
transfers exactly before reading results.

## Axis verdicts

| axis | experiment | verdict | what it established |
| --- | --- | --- | --- |
| target window (PR #295, run 62) | exact `[0,T)` aligned observation window | `ALIGNED_NON_MONOTONICITY_REMAINS` | finite-window alignment is not the principal cause; worsening excess ~1.51% remains under the exact operator |
| spatial representation (run 76) | coarse-grid neighborhood around the scored bins | `NON_MONOTONICITY_LOCALIZED_TO_CANONICAL_BINS` | worsening is confined to the canonical bins' neighborhood on the coarse 2-frequency grid |
| dense frequency | 34-frequency dense grid + localized/persists thresholds | `DENSE_MIXED_NEIGHBORHOOD_SENSITIVITY` | the worsening is a real dense-grid structure (18/34), not a coarse-binning artifact; defines the canonical vector all later axes compare against |
| stencil sensitivity | predeclared stencil/nearest-node variants | `STENCIL_WORSENING_PATTERN_RECLASSIFIED` | worsening positions move under every stencil perturbation; interpolation/discretization stays a live candidate for part of the pattern |
| voxel staircase | frozen boundary staircase remove/displace variants | `VOXEL_STAIRCASE_WORSENING_PATTERN_RECLASSIFIED` | removing the staircase collapses the worsening (3/34 localized); displacing it one cell makes it universal (34/34) — boundary-representation property |
| time-gate localization | prefix/tail gates over the record | `TIME_GATE_WORSENING_BROADBAND` | onset in the 10–25 ms early-reflection window but regenerated in every tail — sustained across the record, not a transient staircase artifact |
| receiver position (REV39-R130D5) | 12-cell frozen whole-cell receiver lattice | `RECEIVER_POSITION_WORSENING_POSITION_LOCAL` | every moved readout reshuffles the vector (hamming 8–20); worsening is readout-position-local, not room-global |
| source position (REV39-R130D6) | 12-cell frozen whole-cell source lattice | `SOURCE_POSITION_WORSENING_POSITION_LOCAL` | same localized direction set; mirrored pair-shortening amplification (receiver x_minus_2 → 27/34, source x_plus_2 → 26/34) — pair-geometry bound |
| joint rigid translation (REV39-R130D7) | identical whole-cell offset on both stencils, separation held ≤ 1e-12 m | `JOINT_TRANSLATION_WORSENING_ABSOLUTE_POSITION_BOUND` | no moved pair preserves the vector (hamming 6–20); 4/12 cells dissolve it outright — bound to the pair's absolute position in the modal field, not relative geometry |

## Campaign-level conclusion

The run-76 dense sub-band worsening is characterized as a **broadband,
boundary-representation-dependent interference structure bound to the
absolute position of the canonical measurement pair** inside the sloped
fixture's modal field:

- It is not a window operator artifact (target-window axis), not a
  coarse-grid binning artifact (spatial/dense axes), and not localized to
  first reflections alone (time-gate axis).
- It is sensitive to the discrete boundary/staircase layer (voxel axis)
  and to the comms stencil representation (stencil axis).
- It is bound to where the measurement sits — moving either endpoint, or
  both rigidly with the separation vector held fixed, always reshuffles
  the 34-bin vector and frequently removes it (position axes).

What remains open — whether PFFDTD's denser structure is the physically
truer answer or a position-bound numerical feature of the frozen
discretization — is **not decidable by further frozen-contract PFFDTD
re-execution**. The comms-stencil maneuver space is exhausted (receiver,
source, and joint are the only three stencil-movement combinations the
contract admits; all were executed). Every further perturbation is either
a forbidden change per the frozen contract — boundary, materials, solver
constants, `in_sigs`/`out_alpha` weights, PPW series, duration, window,
scored frequencies, grid — or a degenerate recombination of axes already
run. Continuation belongs to a different program (e.g. MFEM-side
diagnostics or a physics-contract revision), not the R130D diagnostic
chain.

## Canonical acceptance — unchanged throughout

No axis altered the canonical contract. Final canonical state after the
complete campaign:

- MFEM self-convergence: `SELF_CONVERGENCE_FAILED`
- PFFDTD self-convergence: `SELF_CONVERGENCE_FAILED`
- cross-solver eligibility: `CROSS_SOLVER_BLOCKED`
- general-3D validation: `NOT_VALIDATED`

All diagnostic cells across all axes remain diagnostic-only; none entered
canonical acceptance, and no threshold, mask, PPW, scored-frequency set,
observation contract, geometry, source/receiver position, or solver
execution change was made anywhere in the campaign.

## Evidence index

- `benchmarks/acoustics/r130d_target_window_diagnostic_run62_summary.json` — run-62 target-window diagnostic (PR #295 artifact)
- `benchmarks/acoustics/r130d_spatial_representation_diagnostic_run76_summary.json` — spatial representation diagnostic
- `benchmarks/acoustics/r130d_dense_frequency_diagnostic_summary.json` — dense frequency diagnostic
- `benchmarks/acoustics/r130d_stencil_sensitivity_diagnostic_summary.json` — stencil sensitivity
- `benchmarks/acoustics/r130d_voxel_staircase_sensitivity_diagnostic_summary.json` — voxel staircase sensitivity
- `benchmarks/acoustics/r130d_time_gate_localization_diagnostic_summary.json` — time-gate localization
- `benchmarks/acoustics/r130d_receiver_position_sensitivity_diagnostic_summary.json` — receiver position (R130D5)
- `benchmarks/acoustics/r130d_source_position_sensitivity_diagnostic_summary.json` — source position (R130D6)
- `benchmarks/acoustics/r130d_joint_translation_sensitivity_diagnostic_summary.json` — joint rigid translation (R130D7)
