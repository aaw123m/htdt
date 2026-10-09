# R130D #938 — three held-out native q0 exact-roof FV grids refute all five directional dispersion fixes

2026-10-09 JST; Draft PR #1055, Issue #938 OPEN. No GitHub Actions manually dispatched.

## Prospective boundary; no favorable-pair cherry-picking

Full PPW40/44 results had already been observed and pushed in `6cf0b1b48537b7e7af8ba1c8d4e905b9f9401004`. Only the **new PPW28, PPW32 and PPW36** grid outcomes were **unobserved** when the additional explicit multigrid plan was committed and pushed as `20a19c1bab6a02363dca0ae452f041fb8b73d21a`. The new plan prospectively freezes **all five preexisting directional schemes**, every native point source/receiver/record/frequency, and comparison of **all four adjacent PPW pairs**; no arm, metric or grid was selected from the new results. Original upstream PFFDTD SHA `aa319f6c86517cb95aabfae8656277da62c3ead5` and native source/voxel HDF5 SHA verified.

All exact physical 56m³ roof cutcell volumes, Neumann walls, eight original q[0]=1 point source weights, fixed physical source (1.5,2,2)m / receiver (2.5,2,2)m, native PPW-dependent Ts and Nt, full 250ms no taper, both signed 40Hz and 80Hz pressure bins, exact 1/12 dispersion coefficient, beta=1/4 Newmark finite-q0 discrete propagation and frozen original three-gate thresholds are unchanged. New source Gaussian, damping, high-mode cuts, reweighting, fitting and frequency masking are expressly prohibited.

New genuine full spectra evaluated without truncation: **PPW28 31,713 modes**, **PPW32 48,032**, **PPW36 65,688** (plus the preserved previously computed PPW40 91,415 and PPW44 123,032): **359,880 total experimental exact-roof coupled modes** across 5 grids. Every 3D sparse conservative corrected operator is checked independently against the exact tensor generalized Neumann eigenmodes and constant mode, not merely speculated analytically. On each new grid the unmodified exact-roof full-mode result reproduces previously archived genuine Newmark-CG 250ms q0 raw-wave signed transfers at relative **2.95e−8, 1.87e−8, 1.55e−8** respectively.

## Real full 250ms signed original-q0 complex error across all adjacent PPW grids

All entries are two original 40/80Hz bins, relative signed complex transfer errors, *lower better*; original gate **≤0.20** also requires maximum relative magnitude **≤0.25** and maximum phase **≤15°** (all three must pass simultaneously).

| Fixed conservative exact-roof scheme | PPW28→32 | 32→36 | 36→40 | 40→44 | All four pairs/monotonic all metrics? |
|---|---:|---:|---:|---:|---|
| No dispersion correction, baseline FV | 0.052369 | 0.218508 | 0.106168 | **0.872666** | **FAIL / NO** |
| x-only correction | 0.713827 | 0.276839 | 0.556031 | **0.491963** | **FAIL / NO** |
| sloped-roof y-z-only correction | 0.404351 | 0.718977 | 0.109931 | **0.711329** | **FAIL / NO** |
| x plus y-z correction, without mixed term | 0.557780 | 0.076145 | 0.573667 | **1.383694** | **FAIL / NO** |
| full K M⁻¹ K including x/y-z mixed term | 0.061558 | 0.387107 | 0.096163 | **0.334325** | **FAIL / NO** |

Every fixed scheme violates the canonical three-component numerical limits on multiple refinements and fails strict all-metrics monotonicity. Even deceptively good complex error at PPW28→32 for the baseline (**0.052**) and full correction (**0.062**), or PPW32→36 for x+yz additive (**0.076**), is *not* convergence: each candidate subsequently becomes worse by large factors and cannot satisfy full amplitude/phase criteria. The original high-PPW 40→44 near-null / phase problem persists on all schemes.

Original true native staircase PFFDTD self-convergence is not requalified by any experimental FV scheme. The new five-grid test is a **held-out expansion**, not an independent physical BRAS or cross-solver reference and not original run25/run76 PPW8/10/12 replay. No post hoc frequency restriction or acceptance revision is allowed. Product remains NO_GO.

Evidence: [prospective heldout plan](../benchmarks/acoustics/r130d_exact_roof_q0_tensor_directional_heldout_plan_2026-10-09.json), [complete actual five-grid signed transfers and every adverse individual bin metric](../benchmarks/acoustics/r130d_exact_roof_q0_tensor_directional_heldout_evidence_2026-10-09.json). Runner: `scripts/run_r130d_exact_roof_q0_tensor_directional_heldout.py`, shared real physics/spectral operator: `backend/src/htdt/r130d_tensor_directional_dispersion.py`, independent fail-closed tests: `backend/tests/test_r130d_exact_roof_q0_tensor_directional_heldout.py`.

**FINAL: original PFFDTD q0 SELF_CONVERGENCE_FAILED, external BRAS/owned-room NOT_VALIDATED, product NO_GO, PR #1055 Draft, Issue #938 OPEN.** Next credible repair must jointly enforce boundary consistency, point delta coupling and space/time discretization across multiple grids; adding a formal uniform-grid high-order stiffness correction is insufficient.
