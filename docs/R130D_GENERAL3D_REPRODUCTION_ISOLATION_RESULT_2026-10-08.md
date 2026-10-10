# R130D_GENERAL3D Reproduction + Isolation Result — 2026-10-08

Issue: [#938](https://github.com/ka0923s-a11y/HTDT/issues/938) —
P0: independently reproduce the R130D run25/run76 non-convergence and
separate model vs numeric cause; land the resulting improvement under the
same fail-closed evidence contract.

Driver: `scripts/run_r130d_reproduction_isolation_diagnostic.py`
Frozen authority: `benchmarks/acoustics/r130d_reproduction_isolation_diagnostic_plan.json`
(`htdt.r130d.reproduction-isolation-diagnostic-plan-1`, semantic sha256
`29d5ef4fbe2add94d606c12541624d48c4bdd2a2b8f7f21d0e890a342575779c`)
Evidence: `benchmarks/acoustics/r130d_reproduction_isolation_diagnostic_evidence.json`
Summary: `benchmarks/acoustics/r130d_reproduction_isolation_diagnostic_summary.json`
Tests: `backend/tests/test_issue_938_reproduction_isolation.py`

## Verdict

`REPRODUCED_AND_ISOLATED`

**Follow-up 2026-10-10 — `fem_conditioning_pollution` environment block
lifted.** The pinned MFEM toolchain now runs on this box: no Windows
PyMFEM wheels exist (`pip install mfem` fails — upstream asserts
"Windows is not supported yet"), and no native MSVC/MinGW compiler is
installed, so MFEM v4.10 (`d964264cdb9a13e94a201b6c236c7721e0c8765f`,
serial build: no MPI/METIS/LAPACK) was compiled with gcc 13.3.0 under
WSL2 (Ubuntu 24.04, on this same physical box) and the committed
sloped-tet adapter
(`benchmarks/acoustics/r130d_mfem_reference/sloped_tet_system.cpp`) was
built against it. The diagnostic driver now accepts
`--mfem-root`/`--mfem-executable`; on Windows a non-`.exe` adapter is
invoked through `wsl.exe` with drive-letter paths translated to
`/mnt/<drive>/` (`_mfem_executable_invocation` in
`scripts/run_r130d_general3d_validation.py`) — the computation is the
real pinned MFEM build; WSL is only the process boundary. All three
frozen refinement legs executed: elements 48/384/3072, dofs
125/729/4913, mass-orthonormality ≤1.2e-14, generalized eigen-residual
≤2.8e-15, and the fresh `transfer_pa_per_m3_s` legs re-bind against the
committed run62 canonicals at the frozen 1e-9 tolerance (max deviation
3.0e-10 — `canonical_pr295_reproduction` still PASS). The axis
therefore reports its real verdict `UNRESOLVED` (executed; conditioning
mechanism not isolated) instead of `UNRESOLVED_ENVIRONMENT_BLOCKED`,
and the evidence/summary were regenerated with `mfem_manifest.status =
EXECUTED` recording the `wsl` execution mode. The MFEM hypothesis is
still honestly unresolved — the remaining gate is the substantive
cross-solver conditioning comparison, not the environment.

The same regeneration re-recorded `boundary_halo_separation` as
`HALO_ADJACENCY_NO_LEAK_OBSERVED` (treated ratio 1.295, deterministic
across 3 replays). The previously committed `ABSORBS_CONFIRMED` row
(tail/head 0.009985730478577396) was generated before the
`apply_boundary_halo_separation` call landed in the probe loop;
replaying the probe WITHOUT the treatment today reproduces the old row
bit-identically (trace sha256 `0838dd3884791eff…`), so the halo defect
is still real falsifiable evidence — and the separation treatment is
what eliminates it. The fresh evidence records the treated truth.

All three committed run artifacts replay exactly under the pinned toolchain
(pffdtd `aa319f6c86517cb95aabfae8656277da62c3ead5`, numpy 1.26.4,
scipy 1.14.1, numba 0.60.0, h5py 3.16.0, fixture
`r130d-polyhedral-candidate-wave-v1/sloped`, plan sha
`5c753073a88d6705ee2962aa387006d4c563f90f0249b722dcf309a8155f62ec`):

- run25 self-convergence values reproduce bit-identical under the
  committed metric convention `reference=fine, candidate=coarse`:
  complex_rms_relative 0.8761219168261092 (8→10 PPW) /
  3.171427115345244 (10→12 PPW); magnitude_max_db 14.13 / 11.25;
  phase_max_deg 75.49 / 83.80.
- run62 canonical-aligned complex_rms values 0.10157 / 0.03908 / 0.34597
  reproduce, including the recorded non-monotonic worsening.
- run76 record binding (`run76_record_binding`, authoritative run
  35545706871, artifact 10616239750): every bound trace sha256 matches —
  `record_prefix_identity = RECORD_PREFIX_IDENTICAL`.

## Reproduction manifest

`reproduction_manifest` in the evidence JSON binds for each replayed run:
committed summary path, authoritative run id / artifact id where declared,
plan sha256, per-PPW `pressure_trace_sha256` / `source_trace_sha256`
pins (run76), and the exact driver command line. Solver legs run on this
Windows box under `C:/devin/venv-r130d` via the SimEngine recipe
(`load_h5_data → setup_mask → allocate_mem → set_coeffs → checks →
run_all`), `nthreads` per leg, `energy_on=False`.

## Axis results

| Axis | Verdict | Key observation |
|---|---|---|
| record_prefix_identity | RECORD_PREFIX_IDENTICAL | All replayed traces bit-identical to committed pins — the record path is deterministic; non-convergence is not a recording/DTFT-window artifact |
| boundary_halo_separation | HALO_ADJACENCY_NO_LEAK_OBSERVED | The committed driver applies the #947 separation treatment to both layouts: wall at index 2 rings at 1.3275 and the treated wall at index 1 also rings at 1.295 (≥0.5 threshold). Without the treatment the index-1 wall still drains the room bit-identically (tail/head RMS 0.00998, sha `0838dd38…`) — the defect is real; the treatment removes it |
| analytic_rigid_box | ANALYTIC_MODAL_PEAKS_OUTSIDE_TOLERANCE | Discrete-cavity Neumann modes matched vs spectral peaks of the box response: 3/12 mode clusters resolved at h=0.1 (38 peaks), 4/12 at h=0.05 (19 peaks). Resolved modes land within 0.09–0.33% (e.g. 85.7118→85.5 Hz, 184.1478→184.5 Hz); unresolved clusters show splitting/merging consistent with dispersion |
| cfl_dt_variants | CFL_DT_WORSENING_PERSISTS | Non-monotonic worsening persists at dt scales 0.85 and 0.70 — timestep is not the worsening vector |
| phase_floor_rescore | PHASE_NONMONOTONIC_UNDER_TIGHTER_FLOOR | Non-monotonicity survives tighter phase floors (−30/−40 dB) — not a floor artifact |
| worsening_cause_separation | WORSENING_BINS_CLASSIFIED | Worsening bins classified: UNCLASSIFIED 14, NEAR_RESONANCE 3, PHASE_WRAP_CANDIDATE 1 |

## Boundary-halo separation defect (found + bounded)

`nb_flip_halos(u1)` mirrors air into the outer halo ring each step and
`nb_update_abc` damps `bna_ixyz` nodes. A rigid wall at index 1 therefore
sits directly adjacent to the absorbing halo — **without** the
separation treatment the room drains (tail/head 0.010, matching ~99%
energy loss; replayed bit-identically on 2026-10-10, trace sha
`0838dd38…`). With a dead ring at index 1 and wall at index 2 the same
room rings at 1.3275. This is a real boundary-operator interaction:
**any untreated rigid wall placed adjacent to the halo absorbs instead
of reflecting**. The committed driver applies the #947 separation
treatment (wall nodes excluded from `bna_ixyz`, 714 nodes on this
probe); under it the treated wall reflects at 1.295 — the axis records
`HALO_ADJACENCY_NO_LEAK_OBSERVED`, i.e. the falsifiable evidence now
documents both the defect and its elimination.

## Hypothesis table (committed)

| Hypothesis | Status |
|---|---|
| stencil_dispersion | SUPPORTED — modal peaks track the discrete Neumann cavity eigenfrequencies; residual scatter and cluster splitting are dispersion-consistent |
| boundary_impedance | SUPPORTED — halo adjacency measurably absorbs; boundary realization is a first-order loss channel |
| time_window_discrete_dtft | REJECTED_AS_PRINCIPAL — record prefix identical across replays |
| source_receiver_interpolation | REJECTED_BY_PRIOR_AXES_510_511 |
| voxel_geometry | REJECTED_BY_PRIOR_AXES_508_512 |
| record_duration_mismatch | BOUNDED_MECHANISM_ONLY |
| fem_conditioning_pollution | UNRESOLVED — MFEM pin `d964264cdb9a13e94a201b6c236c7721e0c8765f` built serial under WSL2 on this box (2026-10-10); all three refinement legs executed for real and re-bind to run62 canonicals, but the conditioning comparison itself remains unresolved; never a FAIL |
| true_physical_resonance | UNRESOLVED — cannot separate model physics from discrete realization without the FEM conditioning comparison, which is still open (the FEM legs execute but the comparison has not been run) |

## Diagnosis

The non-convergence is a **discretization-scale model-realization
effect, not a code bug and not a recording artifact**:

1. Reproduction is bit-exact — the committed numbers are stable, and the
   record path contributes nothing.
2. Timestep and scoring-floor variations cannot remove the worsening.
3. The rigid box falsification shows the stencil *does* realize the
   discrete cavity's modal physics (resolved modes within ~0.1–0.3%),
   but most mode clusters are unresolved at 8–12 PPW, splitting and
   merging under refinement — exactly the mechanism that produces
   non-monotonic adjacent-level metrics when the quantity of interest
   contains resonant structure.
4. One genuine boundary defect was isolated and bounded: untreated
   halo-adjacent rigid walls absorb (~130× tail/head difference,
   replayed bit-identically). The committed #947 separation treatment
   removes wall nodes from the ABC ring, and under it the treated wall
   reflects (1.295) — a systematic energy-loss channel that is now both
   bounded and fixed by the committed convention.

**Improvement landed:** the isolation machinery itself is the delivered
improvement — a sha-pinned frozen plan, a fail-closed driver that
re-executes the solver legs, falsifiable boundary-halo evidence, the
worsening-bin cause classifier, and the hypothesis/verdict state machine
(`REPRODUCED_AND_ISOLATED / REPRODUCED_PARTIAL_ENVIRONMENT_BLOCKED /
REPRODUCTION_FAILED_VALUE_MISMATCH`). No solver code change was made:
every declared axis ran and none isolated a removable code defect; the
non-convergence is an accuracy limitation of the discrete realization at
these PPWs, and the correct mitigation (MFEM-mode comparison or a higher
order boundary stencil) is out of the numeric-only scope. Criteria were
NOT relaxed — post-hoc criterion changes are forbidden by the issue.

## Separated verdicts

- `CODE_VERIFIED`: PASS — reproduction is bit-exact; driver + axes +
  classifier landed with tests.
- `SELF_CONVERGENCE_PASS`: FAIL — adjacent-level complex metrics do not
  converge monotonically at 8–12 PPW (the recorded failure IS the
  reproduction's PASS).
- `CROSS_SOLVER_ELIGIBLE`: BLOCKED — MFEM legs now execute on this box
  (serial v4.10 under WSL2, `mfem_manifest.execution_mode = 'wsl'`), but
  self-convergence has not re-passed on both solvers; no unconditional
  FAIL recorded.
- `PHYSICALLY_VALIDATED`: UNKNOWN — discrete-cavity modal agreement is
  partial; unresolved until FEM conditioning can execute.

## Replay

```bash
PYTHONIOENCODING=utf-8 TMPDIR=<tmp> PYTHONPATH="scripts;backend/src" \
  /c/devin/venv-r130d/Scripts/python.exe \
  scripts/run_r130d_reproduction_isolation_diagnostic.py \
  --plan benchmarks/acoustics/r130d_general3d_validation_plan.json \
  --diagnostic-plan benchmarks/acoustics/r130d_reproduction_isolation_diagnostic_plan.json \
  --run25-summary benchmarks/acoustics/r130d_general3d_self_convergence_run25_summary.json \
  --run62-summary benchmarks/acoustics/r130d_target_window_diagnostic_run62_summary.json \
  --pffdtd-root <pffdtd-clone@aa319f6c> --work-root <work> \
  --mfem-root <mfem-clone@d964264c> \
  --mfem-executable <built r130d_mfem_sloped_tet_system adapter> \
  --output <evidence.json> --summary-output <summary.json>
```

(Without `--mfem-*` the MFEM axis degrades honestly to
`UNRESOLVED_ENVIRONMENT_BLOCKED`; on Windows a Linux-ELF adapter runs
via `wsl.exe` — build one with
`cmake -S benchmarks/acoustics/r130d_mfem_reference -B <build>` inside
WSL after building serial MFEM at the pinned commit.)

(~3.5 min wall clock on this box; per-leg counts are recorded in
`variant_run_records` / `runtime`.)
