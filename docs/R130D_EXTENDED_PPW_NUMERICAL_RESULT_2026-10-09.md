# R130D #938: pinned higher-PPW self-convergence experiment — 2026-10-09

## Executive decision

**R130D SELF_CONVERGENCE_FAILED remains open.** Executing the *same* pinned
PFFDTD solver and frozen sloped-room observable at 8/10/12/16/20/24 points per
wavelength did **not** establish monotonic convergence or the original numerical
thresholds. This does **not** establish that convergence is impossible at higher
resolution; it rules out an uncomplicated resolution-only fix *through 24 PPW*
for the exact test.

The numeric experiment is separate from:
- PR #1051 circular-phase *diagnostic classifier* fix (not solver physics);
- the already-landed #947 absorbing-halo/boundary interaction fix, which is
  inert on the canonical separated-mask fixture;
- independent pinned MFEM reference replay, covered by the new Windows Actions
  lane `r130d-mfem-reference-replay.yml` and runner
  `scripts/run_r130d_mfem_reference_replay.py`;
- BRAS external / owned-room physical acceptance (#809/#801).

## Frozen, predeclared conditions

- Plan: `benchmarks/acoustics/r130d_extended_ppw_diagnostic_plan.json`,
  SHA-256 (semantic) `7886ca63e83be5ef63aa08c186aab452b3fd439575073724779435824b25af8b`;
  the plan was committed **before** the new numerical runs.
- Parent authority: `r130d_general3d_validation_plan.json`, semantic SHA-256
  `5c753073a88d6705ee2962aa387006d4c563f90f0249b722dcf309a8155f62ec`.
- Executing repository HEAD: `2ff191576b21bcfc8dcb80feeaeef7607748e66a`.
- Pinned PFFDTD: `aa319f6c86517cb95aabfae8656277da62c3ead5`,
  Windows CPU / Numba; NumPy 1.26.4 and SciPy 1.14.1.
- Same R120B sloped closed polyhedron, source (1.5,2,2) m and receiver
  (2.5,2,2) m, rigid Neumann boundary, 40 and 80 Hz, 250 ms finite record,
  exact P/Q complex transfer. No retuned geometry, mask or response.
- All 8/10/12 PPW complex transfer samples reproduce run25 exactly to the
  serialized values (**maximum difference 0 for all three levels**).
- PPW series [8, 10, 12, 16, 20, 24] was fixed prospectively. The original
  acceptance limits remain `complex_rms_relative <= 0.2`,
  `magnitude_max_relative <= 0.25`, `phase_max_deg <= 15`, original -50 dB
  magnitude phase mask.

## Executed results

| PPW adjacent levels | complex RMS relative | magnitude max relative | phase max degrees |
| --- | ---: | ---: | ---: |
| 8 → 10 | 0.876122 | 0.803499 | 75.4866 |
| 10 → 12 | 3.171427 | 2.652561 | 83.8040 |
| 12 → 16 | 0.439770 | 0.612676 | 16.5848 |
| 16 → 20 | 0.857478 | 0.485784 | 78.5478 |
| 20 → 24 | 0.552346 | 0.579565 | 29.7424 |

Grid steps [m] by PPW: 8=0.429; 10=0.3432; 12=0.286;
16=0.2145; 20=0.1716; 24=0.143. This was real PFFDTD execution rather
than a synthetic model or a re-evaluation of old artifacts.

**Final 20→24 adjacent pair** fails all three numerical limits. Error
trajectory is nonmonotonic (including a worsening at 16→20 in complex RMS).
The 8/10/12 old failure is independently reproduced. The correct decision
is `NONCONVERGENT_OR_PREASYMPTOTIC_DIAGNOSTIC`; canonical
`SELF_CONVERGENCE_FAILED`, `CROSS_SOLVER_BLOCKED`, and `NOT_VALIDATED`
remain unchanged.

## Reproducibility and next actions

Machine-readable execution evidence:
`benchmarks/acoustics/r130d_extended_ppw_diagnostic_evidence_2026-10-09.json`
(records all six complex transfer values, time steps, grid/mask hashes,
runtime, adjacent errors, and fail-closed verdict).

A clean Windows Python 3.12 numerical environment with exact pins is required.
Set `PYTHONUTF8=1`, then run:

```powershell
python scripts/run_r130d_extended_ppw_diagnostic.py `
  --plan benchmarks/acoustics/r130d_extended_ppw_diagnostic_plan.json `
  --parent-plan benchmarks/acoustics/r130d_general3d_validation_plan.json `
  --canonical-summary benchmarks/acoustics/r130d_general3d_self_convergence_run25_summary.json `
  --pffdtd-root <PINNED_PFFDTD_CHECKOUT> `
  --work-root <FRESH_WORK_DIRECTORY> `
  --output <OUTPUT_JSON>
```

The runner stops if it cannot reproduce pinned historical 8/10/12 transfer
values, if the plan/thresholds/toolchain differ, or if the solver returns
invalid values; a fresh `work-root` is mandatory. The numerical result is not
allowed to change production eligibility.

1. Execute `R130D pinned MFEM reference replay` (pinned MFEM
   `d964264cdb9a13e94a201b6c236c7721e0c8765f`, exact 1/2/3 refinement,
   4913-DOF finest level), retain its independently re-created complex
   transfers and the *failure/pass* decision. The local Windows computer
   used for the PFFDTD study lacked the required MFEM build toolchain.
2. Identify FEM conditioning/40 Hz phase behavior from independent modal
   resolution and p-/h-refinement; compare exact observable/geometry provenance.
3. If the MFEM evidence still fails, preregister a *new* physical/numerical
   model experiment (e.g., higher-order boundary representation, optimized
   voxelization/dispersion or reference BEM) rather than increasing PPW until a
   favorable adjacent pair appears.
4. Only after **both** independent solvers pass appropriate self-convergence
   may cross-solver comparisons become eligible. This still cannot substitute
   for BRAS or real-room validation.

## Test evidence

Local isolated Windows numeric environment: 25 focused tests PASS, covering
the preregistered plan invariants, preserved canonical historical levels,
fail-closed mutations, and the #938/#947 prior guards. This is not a report
of repo-wide Windows GUI acceptance.
