# R130D #938 — exact-roof original point-q0 K M^-1 K directional decomposition: ALL FAIL

2026-10-09 JST. Draft PR #1055, original Issue #938 OPEN; no GitHub Actions manually dispatched.

## Prospective numerical operator experiment

Frozen, GitHub-pushed prospective plan: `7b0f09a3212eda6e90296aeb3ea17b0cdb18b506`, before any numerical scores. Original upstream PFFDTD SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`; all PPW40/44 original native HDF5 eight-node q[0]=1 source/receiver, physical source (1.5,2,2)m and receiver (2.5,2,2)m, exact physical 56m³ roof geometry, original individual native Ts/Nt, full 250ms no taper and both signed 40/80Hz pressure transfer bins preserved. **No artificial high-mode cutoff, damping, source smoothing, amplitude fitting, point relocation, waveform changes or threshold rewrite**. Original PFFDTD code and production simulation qualification unchanged.

The prior *physical true 3D* conservative exact-roof FV geometry rigorously factors into `M=Mx⊗Myz`, `K=Kx⊗Myz+Mx⊗Kyz`. The previous mathematical high-order spatial correction `K4=K+alpha K M^-1 K`, with `alpha=h²/(12c²)`, therefore splits exactly into

`K4 - K = alpha [ (Kx Mx^-1 Kx)⊗Myz + Mx⊗(Kyz Myz^-1 Kyz) + 2 Kx⊗Kyz ]`.

With *no fitted parameters*, five predeclared operator arms are assembled as actual full true sparse symmetric positive-semidefinite 3D FV stiffness operators: unmodified baseline; x-only; yz-only; additive x+yz (no mixed term); and full K4 including mixed x-yz coupling. All five have the **same physical diagonal cutcell mass**, real face Neumann stiffness, original 8-node source/receiver, and original Newmark q0 time propagator. **Every PPW40 91,415 and PPW44 123,032 original exact-roof generalized mode** (all 214,447) is included in the full 250ms complex signed response. True sparse candidate eigenpair residuals are independently checked against the analytically separated modes; constant Neumann nullspace is retained. The recomposed full-K4 arm exactly reproduces the previous physically assembled true-K4 full-mode transfers, with **zero observed relative response difference**; no mode selection is responsible for apparent changes.

## Frozen numerical results: actual all-mode point-q0 PPW40→44

| Predeclared actual FV operator | Complex relative L2 | Maximum relative magnitude | Maximum phase | Frozen original 0.20 / 0.25 / 15° |
|---|---:|---:|---:|---|
| Baseline exact-roof FV/Newmark | **0.872666** | 3.207787 | 170.011° | FAIL |
| **x-axis correction only** | **0.491963** | 0.620293 | 160.685° | FAIL |
| **sloped roof y-z correction only** | **0.711329** | 2.000263 | 170.992° | FAIL |
| **x and y-z corrections, no mixed term** | **1.383694** | **0.072088** | **178.103°** | FAIL |
| Full correction including `2 Kx⊗Kyz` | **0.334325** | **6.443058** | **116.708°** | FAIL |

**All five FAIL.** The x-only arm reduces signed complex error from 0.873 to 0.492, but violates all three frozen limits. The additive separate x+yz arm achieves *only* the magnitude relative gate (0.072), but fails signed complex and nearly 180° phase; a magnitude-only score is physically misleading and must not be promoted. The complete K4 mixed term strongly improves the combined complex error (0.334) while worsens the 40Hz near-null magnitude error (6.443) and still fails phase. Thus, the x-yz mixed term is not an inconsequential implementation detail: the two-frequency signed complex transfer has large cancellation/interference among all numerical spectral contributions. This is **not proof of a unique physical or numerical root cause**. The current experiment is only two high-PPW grids and is not evidence of asymptotic convergence.

Each actual true 3D directional corrected stiffness, not just a table of speculative modal eigenvalues, is implemented in `backend/src/htdt/r130d_tensor_directional_dispersion.py`, verified for real sparse mass-orthogonal spectral eigenvectors and constant-Neumann null, and separately tested on an independent small **nonuniform positive-cutcell-mass tensor Neumann fixture**. All per-grid raw signed complex 40Hz and 80Hz transfers, full errors, adverse per-bin magnitudes/phases and original SHA-pinned source/geometry inputs are retained in [the complete evidence JSON](../benchmarks/acoustics/r130d_exact_roof_q0_tensor_directional_dispersion_evidence_2026-10-09.json). Reproducible runner: `scripts/run_r130d_exact_roof_q0_tensor_directional_dispersion.py`; negative-preserving regression: `backend/tests/test_r130d_exact_roof_q0_tensor_directional_dispersion.py`.

## Release decision and next numerical step

The **original native PFFDTD point-q0** remains **SELF_CONVERGENCE_FAILED**. Independent MFEM/cross-solver and BRAS/owned-room **NOT_VALIDATED**, product **NO_GO**, PR Draft, Issue OPEN. The physically meaningful next test must compare each fixed scheme at additional held-out grid sizes, then diagnose boundary-accuracy and point-source/receiver consistency without cherry-picking a favorable metric or changing the original canonical point-q0 source. Original run25/run76 PPW8/10/12 canonical qualification remains blocked.
