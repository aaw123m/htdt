# R130D native Cartesian axis flux + true cut roof Neumann hybrid — original q0 5-grid outcome

Date: 2026-10-10 JST. Issue #938; Draft PR #1055.

**Outcome: experimentally useful local first-roof improvement; full original 5-grid signed q0 self-convergence still FAIL. Original upstream PFFDTD remains SELF_CONVERGENCE_FAILED; independent physical validation NOT_VALIDATED; product NO_GO.**

## Prospective registered experiment

Precommitted and pushed BEFORE the new solver numerical run: commit `a0ea3dbfad013b161d8e758bedae00936a5c021b`, plan `benchmarks/acoustics/r130d_original_q0_cartesian_flux_true_cut_roof_hybrid_plan_2026-10-10.json`. No solver coefficient fitted to 40/80 Hz or roof-window outcomes. All five actual original native PFFDTD PPW 28/32/36/40/44 HDF5 SHA-pinned comms/vox source/receiver, original q0 source weights, original native h/Ts/Nt, the original 250ms rectangle, full 3D spectrum, and signed 40/80 Hz comparisons are unchanged. No native PFFDTD reruns or GitHub Actions.

## Operator actually implemented

`backend/src/htdt/r130d_cartesian_true_roof_hybrid_flux.py`. On original Cartesian y/z Q1 nodes, with ALL positive physical cut-polyon basis supports, full exact Q1 consistent mass and true oblique roof natural Neumann stiffness `K_cutQ1`, add

`K_hybrid = K_cutQ1 + (c²/3) Σ_{E: fully wet Cartesian rectangles} s_E s_Eᵀ,` with `s_E=(+1,-1,-1,+1)` on `(00,10,01,11)`.

This coefficient is derived, not fitted. On a full 2D Cartesian rectangle, Q1 local `K/c²` has diagonal `2/3`, side neighbor `-1/6`, opposite corner `-1/3`. The term `ssᵀ/3` yields diagonal `1`, Cartesian side edge `-1/2`, opposite `0`, i.e. the native interior conservative 5-point flux assembled from adjacent cells. FULL interior rectangles ONLY get this exact correction. Every actually clipped roof/other-wall rectangle retains exact true physical-domain Q1 bilinear gradient quadrature, including slivers; no face stair normal is substituted for `(.25,1)/sqrt(1+.25²)`.

As `(sᵀ 1)=(sᵀ y)=(sᵀ z)=0`, the added correction annihilates all affine fields; in particular, `u=y-z/4` retains zero weak stiffness on roof-local basis rows (not at other physical walls, where it is not Neumann). Symmetry and positive semidefiniteness follow from Q1 weak-gradient energy and sum of positive rank-one updates; `K 1=0`. Exact full positive physical consistent Q1 mass is SPD (no mass floors/sliver drop). The x P1 and y/z hybrid tensor product preserves all original 8node point source/receiver coefficients; true 3D `M=Mx ⊗ Myz`, `K=Kx ⊗ Myz + Mx ⊗ Kyz`. Generalized eigenmodes are all retained. Native `beta=1/4` Newmark all-mode signed finite-record pressure transfer is used unchanged, with `rho=1.2,c=343.2`.

Independent small-grid SPD-mass, positive-semidefinite stiffness, zero Neumann constant, exact native 5-point local element symbol and midpoint-discrete undriven energy tests are in `backend/tests/test_r130d_cartesian_true_roof_hybrid_flux.py`. Real five-grid evidence all-mode spectrum, native source SHA, affine near-roof weak rows, true mass and rigid mode checks in `backend/tests/test_r130d_original_q0_cartesian_flux_true_roof_hybrid_evidence.py`. The two new test files and the old original operator-manufactured evidence tests passed. A wider local run of ALL 64 `backend/tests/test_r130d*.py` modules completed with **662 tests PASS, exit 0**, using `pytest --noconftest` (the unrelated GUI conftest requires PySide6, unavailable in this numerical environment). No GitHub Actions were started.

## All 5 original grid original full 250ms signed q0 evidence

| PPW | Native true y/z Q1 positive-support modes | Full 3D modes (uncut) | Full Cartesian rectangles | Physical cut rectangles |
|---:|---:|---:|---:|---:|
| 28 | 1,081 | 37,835 | 880 | 132 |
| 32 | 1,359 | 53,001 | 1,134 | 148 |
| 36 | 1,716 | 75,504 | 1,461 | 168 |
| 40 | 2,101 | 102,949 | 1,817 | 187 |
| 44 | 2,497 | 132,341 | 2,188 | 204 |

The full exact physical yz cross section integrates to 14m² and room volume to 56m³ in every case. The genuine roof-only affine weak `|K_hybrid (y-z/4)|` at PPW28 is `2.18e-10` in the physical FEM row units; maximum permitted in the runner `1e-7` on EACH of all five grids. This must not be directly compared to the old strong Cartesian original roof Laplacian `1/m` numeric error.

All four adjacent PPW pairs, full original samples 250ms, full signed frequency bins 40/80 Hz, ALL high modes and full 8+8 source/receiver:

| PPW pair | Complex RMS (≤0.20) | Max relative amplitude (≤0.25) | Max phase (≤15°) | Both bins and 3 gates |
|---|---:|---:|---:|---|
| 28→32 | 0.668244 | 0.897412 | 33.5695° | FAIL |
| 32→36 | 1.309672 | 1.534431 | 174.6334° | FAIL |
| 36→40 | 0.444557 | 0.620523 | 20.1842° | FAIL |
| 40→44 | 0.154201 | 0.182393 | 7.5041° | PASS |

Only the final pair passes, and errors are NOT strictly monotone across all pairs. This new candidate is **NOT VALIDATED**, is **not an original upstream PFFDTD modification** and does NOT qualify the failed original solver for any release. The previous cut-Q1 full-mode and original P1 comparator remain separate observed baselines; none may be retroactively substituted into a winning result.

## Independent fixed first true roof reflection witness (auxiliary only)

For each original native source+receiver HDF5, evaluate the fixed analytical 64-pair finite physical roof Neumann specular image with unmodified weights at the precommitted `t=0.0089668767033651s`, support radius `0.0011s`, compact witness width `0.00035, 0.00060, 0.00085s`. Evaluate the new ALL-mode Newmark interior pressure at EVERY native sample in those windows (no source smoothing, modal truncation, arbitrary time mask or phase fit). Reference is analytic single true-roof reflection; numerical window may retain earlier direct numerical dispersion, so the ratio alone cannot isolate pure roof-reflection causal error.

| PPW | New hybrid: relative error at three frozen widths | Original PFFDTD: same widths |
|---:|---|---|
| 28 | 4.67%, 4.16%, 1.25% | 61.89%, 69.81%, 73.50% |
| 32 | 0.16%, 11.49%, 10.26% | 70.80%, 61.19%, 57.62% |
| 36 | 1.73%, 5.30%, 2.48% | 65.13%, 55.07%, 49.70% |
| 40 | 5.09%, 9.84%, 14.83% | 67.11%, 18.87%, 1.47% |
| 44 | 2.30%, 0.60%, 1.07% | 64.59%, 28.76%, 15.09% |

This is a useful physically grounded indicator for 14/15 individual witnesses, **not** license to replace the full original signed 250ms acceptance metrics. At PPW40, the widest witness actually worsens relative to original and must remain reported.

## Reproducibility and next engineering problem

Run `scripts/run_r130d_original_q0_cartesian_flux_true_roof_hybrid.py --plan benchmarks/acoustics/r130d_original_q0_cartesian_flux_true_cut_roof_hybrid_plan_2026-10-10.json --original-sims-root <actual original SHA-pinned work tree> --output benchmarks/acoustics/r130d_original_q0_cartesian_flux_true_roof_hybrid_evidence_2026-10-10.json`. SHA checks fail closed; the script refuses missing grids or partial modes.

Result: `benchmarks/acoustics/r130d_original_q0_cartesian_flux_true_roof_hybrid_evidence_2026-10-10.json` records raw original SHA, full plan hash, all five 3D spectra, accepted/unaccepted gates, the real native q0 and fixed independent first-roof weak witness. `scratch/` retained.

The next problem is the **nonmonotone long-time coherent broadband point-source/receiver transfer**, especially PPW32→36 phase `174.6°` at 40 Hz, despite true-roof affine Neumann and most single-roof witness checks passing. Do not attribute remaining failure solely to the boundary stair geometry. The full signed 250ms finite-window endpoint observation, source/receiver grid dependence and high-frequency all-mode multi-bounce content must be independently separated using precommitted tests; no post hoc time windows, amplitude/phase fitting, or cutoff. Keep Issue #938 OPEN, PR #1055 Draft, no release.
