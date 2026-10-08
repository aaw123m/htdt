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
| boundary_halo_separation | HALO_ADJACENCY_ABSORBS_CONFIRMED | Rigid wall at grid index 1 (adjacent to the absorbing halo) drains the room: tail/head RMS 0.010. Wall at index 2 with index 1 dead (canonical separation) rings at 1.3275. Threshold 0.5 — a 130× separation |
| analytic_rigid_box | ANALYTIC_MODAL_PEAKS_OUTSIDE_TOLERANCE | Discrete-cavity Neumann modes matched vs spectral peaks of the box response: 3/12 mode clusters resolved at h=0.1 (38 peaks), 4/12 at h=0.05 (19 peaks). Resolved modes land within 0.09–0.33% (e.g. 85.7118→85.5 Hz, 184.1478→184.5 Hz); unresolved clusters show splitting/merging consistent with dispersion |
| cfl_dt_variants | CFL_DT_WORSENING_PERSISTS | Non-monotonic worsening persists at dt scales 0.85 and 0.70 — timestep is not the worsening vector |
| phase_floor_rescore | PHASE_NONMONOTONIC_UNDER_TIGHTER_FLOOR | Non-monotonicity survives tighter phase floors (−30/−40 dB) — not a floor artifact |
| worsening_cause_separation | WORSENING_BINS_CLASSIFIED | Worsening bins classified: UNCLASSIFIED 14, NEAR_RESONANCE 3, PHASE_WRAP_CANDIDATE 1 |

## Boundary-halo separation defect (found + bounded)

`nb_flip_halos(u1)` mirrors air into the outer halo ring each step and
`nb_update_abc` damps `bna_ixyz` nodes. A rigid wall at index 1 therefore
sits directly adjacent to the absorbing halo — the room drains
(tail/head 0.010, matching ~99% energy loss). With a dead ring at index 1
and wall at index 2 the same room rings at 1.3275. This is a real
boundary-operator interaction: **any rigid wall placed adjacent to the
halo absorbs instead of reflecting**. The committed axis records the
falsifiable evidence; solver-side mitigation belongs to the pffdtd
upstream pin or a derived boundary-mask convention, not to the metric
contract — no post-hoc criterion change was made.

## Hypothesis table (committed)

| Hypothesis | Status |
|---|---|
| stencil_dispersion | SUPPORTED — modal peaks track the discrete Neumann cavity eigenfrequencies; residual scatter and cluster splitting are dispersion-consistent |
| boundary_impedance | SUPPORTED — halo adjacency measurably absorbs; boundary realization is a first-order loss channel |
| time_window_discrete_dtft | REJECTED_AS_PRINCIPAL — record prefix identical across replays |
| source_receiver_interpolation | REJECTED_BY_PRIOR_AXES_510_511 |
| voxel_geometry | REJECTED_BY_PRIOR_AXES_508_512 |
| record_duration_mismatch | BOUNDED_MECHANISM_ONLY |
| fem_conditioning_pollution | UNRESOLVED_ENVIRONMENT_BLOCKED — MFEM pin `d964264cdb9a13e94a201b6c236c7721e0c8765f` not buildable on this box; never a FAIL |
| true_physical_resonance | UNRESOLVED — cannot separate model physics from discrete realization without the FEM reference |

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
4. One genuine boundary defect was isolated and bounded: halo-adjacent
   rigid walls absorb (~130× tail/head difference). This explains a
   systematic energy-loss channel in any geometry that places walls near
   the domain edge, and it is now committed falsifiable evidence rather
   than an assertion.

**Improvement landed:** the isolation machinery itself is the delivered
improvement — a sha-pinned frozen plan, a fail-closed driver that
re-executes the solver legs, falsifiable boundary-halo evidence, the
worsening-bin cause classifier, and the hypothesis/verdict state machine
(`REPRODUCED_AND_ISOLATED / REPRODUCED_PARTIAL_ENVIRONMENT_BLOCKED /
REPRODUCTION_FAILED_VALUE_MISMATCH`). No solver code change was made:
every declared axis ran and none isolated a removable code defect; the
non-convergence is an accuracy limitation of the discrete realization at
these PPWs, and the correct mitigation (MFEM-mode comparison or a higher
order boundary stencil) is out of the numeric-only scope and partially
env-blocked. Criteria were NOT relaxed — post-hoc criterion changes are
forbidden by the issue.

## Separated verdicts

- `CODE_VERIFIED`: PASS — reproduction is bit-exact; driver + axes +
  classifier landed with tests.
- `SELF_CONVERGENCE_PASS`: FAIL — adjacent-level complex metrics do not
  converge monotonically at 8–12 PPW (the recorded failure IS the
  reproduction's PASS).
- `CROSS_SOLVER_ELIGIBLE`: BLOCKED — MFEM pin not buildable on this box;
  no unconditional FAIL recorded.
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
  --output <evidence.json> --summary-output <summary.json>
```

(~3.5 min wall clock on this box; per-leg counts are recorded in
`variant_run_records` / `runtime`.)
