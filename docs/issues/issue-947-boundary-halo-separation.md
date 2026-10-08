# Issue #947 — R130D 剛体壁/吸収ハロ干渉の修正と数値回帰ゲート

## Defect mechanism (verified against pinned upstream)

Pinned PFFDTD (`aa319f6c86517cb95aabfae8656277da62c3ead5`, which is also
upstream `main` HEAD — no upstream fix exists) derives its absorbing
boundary (ABC) node set in `sim_fdtd._load_abc` purely from grid
dimensions: `nb_get_abc_ib` selects every strictly interior node at grid
index `1` or `N-2` on any axis
(`Nba = 2(NxNy + NxNz + NyNz) − 12(Nx+Ny+Nz) + 56`; `//2` on FCC with an
even-parity filter). The set is computed *without* knowledge of
`bn_ixyz` (the room boundary mask). Each step, `nb_flip_halos` mirrors
air into the halo and `nb_update_abc` applies
`u0.flat[ib] = (u0.flat[ib] + lQ·u2ba[i]) / (1 + lQ)` — **after** the
boundary/leapfrog updates. A boundary node sitting in the ring therefore
receives the absorbing update last and **absorbs instead of reflecting**.

Measured on the frozen analytic-box probe
(`benchmarks/acoustics/r130d_reproduction_isolation_diagnostic_plan.json`,
threshold declared *before* results): rigid wall at index 1 →
tail/head RMS ratio **0.0100** (room drains); same wall at index 2 →
**1.3275** (rings). ~130× separation.

## Trigger conditions

A computation hits the defect when its voxelized boundary mask places any
`bn_ixyz` node at grid index `1` or `N-2` on any axis. HTDT's planner
(`_planned_pffdtd_grid`: origin `mins − 3.5·h`, dims
`ceil((extent + 7h)/h) + 1`) makes this reachable for sloped/polyhedral
voxelized walls, not just contrived masks.

## Chosen approach

**HTDT driver-side exclusion** — not an upstream edit, not a pin bump,
not dead-ring re-voxelization:

- `apply_boundary_halo_separation(engine)` removes `bn_ixyz ∩ bna_ixyz`
  nodes from the engine's ABC set (`bna_ixyz`/`Q_bna`/`Nba`, plus `V_bna`
  when energy tracking is on). Called after `load_h5_data`/`setup_mask`,
  before `allocate_mem`, so `bna`-sized buffers stay consistent.
- Each ring node is then governed by exactly one update — the boundary
  physics update — restoring reflective behaviour. Boundary semantics
  unchanged: no padding, no re-voxelization, no upstream source change.
- **Inert on separated masks.** Removing a disjoint node set cannot
  change the trace, so replayed evidence keeps pinned trace hashes.
- `assess_boundary_halo_adjacency` counts ring intersections (per-axis,
  rigid vs lossy, ring linear-index sha256) for provenance.
- `enforce_boundary_halo_separation` is the fail-closed gate: an
  untreated halo-adjacent mask raises `UNSUPPORTED/NOT_VALIDATED`.

## Provenance / evidence

- Treatment record (`HaloSeparationTreatment`: convention version,
  excluded count, pre/post ABC-set sha256) is stored under
  `compatibility_patch['boundary_halo_separation']` in every
  `CandidateNumericalOutput`.
- `benchmarks/acoustics/issue947_boundary_halo_regression_evidence.json`
  — four legs (wall@{1,2} × {untreated,treated}) on the pinned solver:
  `0.0100 → 1.2950` after treatment; `wall@2` bit-identical
  (inert). Frozen metrics/threshold from the diagnostic plan.
- `benchmarks/acoustics/issue947_postfix_rerun_evidence.json` /
  `_summary.json` — full reproduction-isolation rerun under the fix:
  **run25/run62 values match, run76 bit-identical** (production masks
  are already separated → treatment inert); the driver's own
  halo-adjacent probe now rings (`1.295`) → verdict honestly changed to
  `HALO_ADJACENCY_NO_LEAK_OBSERVED`. The PR #295 canonical-baseline CI
  artifact expired (404); the baseline was synthesized from the
  committed run25 summary's identical 6-level schedule and recorded as
  such — post-fix legs still reproduce it (PASS).
- PPW self-convergence re-evaluated under the fix: stays FAIL — the #938
  root cause is pre-asymptotic discretization, not this defect (no
  unjust promotion of #938/#801 gates).
- MFEM-dependent legs remain `UNRESOLVED_ENVIRONMENT_BLOCKED`.

## Regression gate

- `backend/tests/test_issue947_boundary_halo.py` (10 tests): assessment
  verdicts on synthetic masks (index-1 / N-2 / corner / out-of-grid),
  fail-closed enforce, treatment removes exactly the ring intersection,
  inert-on-separated hash identity, inconsistent-state guards.
- `scripts/run_issue947_boundary_halo_regression.py` reruns the real
  four-leg probe and exits non-zero unless all four verdicts hold.

## Scope

`backend/src/htdt/pffdtd_boundary_halo.py` (authority), wired into the
candidate-wave executor
(`cad_candidate_wave_execution.py`, after `engine.setup_mask()`) and the
reproduction-isolation driver legs. No metrics, thresholds, or bands
were changed after observing results.
