# R130D #938 — exact-roof conservative KM^-1K full-mode q0 dispersion correction: FAIL

2026-10-09 JST. **Actual new numerical spatial solver implementation, all modes re-evaluated; no GitHub Actions initiated.** Draft PR #1055 remains Draft, Issue #938 remains OPEN.

## Prospective registration and invariant real wave sources

The experiment plan was committed AND pushed as `64a268f98de5fdedb6f518628aca1f175de6b767` **before observing numerical data**: [preregistered plan](../benchmarks/acoustics/r130d_exact_roof_q0_kmk_dispersion_plan_2026-10-09.json). Original upstream PFFDTD SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`, original PPW40/44 HDF5 voxel/8-point source and receiver SHA checks, source (1.5,2,2)m, receiver (2.5,2,2)m, exact continuous cutcell roof physical volume 56m³, native Ts/Nt per grid, original 8-node q[0]=1 and q[n>0]=0, no taper 250ms, both signed 40/80Hz pressure-transfer bins, frozen original 0.20 / 0.25 / 15° criteria are unchanged. **This is an experimental exact-roof FV, not the original PFFDTD canonical self-convergence or an independent physical reference.**

## Actual implemented conservative solver

In `backend/src/htdt/r130d_conservative_dispersion_correction.py` we implement the *true sparse physical* corrected mass/stiffness operator

`M4 = M`, `K4 = K + (h² / (12c²)) K M⁻¹ K`.

The coefficient is fixed a priori to cancel the leading uniform-grid central-difference spatial-dispersion defect; it is **not fitted to either 40/80Hz output or any PPW pair**. No artificial cutcell mass, nonlocal modal truncation, damping, altered source deposition, or window changes. The correction is symmetric and positive semidefinite when K is, preserves the original Neumann constant-null mode, and increases high spatial eigenvalues. It gives fourth-order leading interior dispersion on a uniform 1D grid; **global fourth-order accuracy is NOT established on R130D's sloped cutcells**. A genuine 3D sparse matrix is constructed for each full native grid, independently verifying mass-weighted corrected eigenpairs against the exact untruncated original full generalized x×yz eigenbasis:

- PPW40: **91,415 modes**, true corrected 3D sparse K nonzeros **2,197,891**, constant-mode residual normalized **3.925e−16**, independent sparse corrected generalized eigen residual **1.046e−12**.
- PPW44: **123,032 modes**, true corrected 3D sparse K nonzeros **2,968,940**, constant-mode residual normalized **3.846e−16**, eigen residual **1.481e−12**.

**ALL 214,447 3D modes** (including lowest, highest, and Nyquist-near high modes) are retained and analytically propagated with the same original beta=1/4 native-clock Newmark point-q0 initial sample and pressure derivative. No high-band masking; no new upstream PFFDTD wave run. The unchanged exact-roof Newmark control again reproduces the archived **real** 250ms Newmark-CG full wave with complex relative **1.7467e−8** / **1.7294e−8**, respectively.

## Signed original q0 full-record results

| Native 8/8 point impulse 250ms transfer | PPW40→44 complex relative | max relative magnitude | max phase | Frozen three-gate verdict |
|---|---:|---:|---:|---|
| Existing exact-roof FV M/K Newmark | **0.872666** | **3.207787** | **170.011°** | **FAIL** |
| New true conservative dispersion-corrected M/K4 Newmark | **0.334325** | **6.443058** | **116.708°** | **FAIL** |

Original two-bin signed complex physical transfers Pa/(m³/s), `40Hz; 80Hz`:
- Baseline PPW40: `(+101.301713568−6.578474670i); (+130.569024197−230.036031000i)`
- Baseline PPW44: `(−23.438722176+5.715537793i); (+2.762765026−205.606105522i)`
- Corrected PPW40: `(−46.402669504+4.747861015i); (−24.777269737−209.213572363i)`
- Corrected PPW44: `(+3.371823566+5.282508477i); (+24.624621040−208.243958356i)`

**40Hz** is the critical adverse bin: relative magnitude error **6.443058**, phase **116.708°**, per-bin complex relative **7.942895**. The full two-bin norm weighted complex metric `0.334325` cannot hide that gross local error. **80Hz** improves considerably in amplitude: relative magnitude **0.004677**, phase **13.498°**, but complex relative **0.235635**; none of this qualifies the original full 40/80Hz acceptance. Since the corrected PPW44 40Hz complex signed response is small, a near-null amplifies relative amplitude error. Preserve this physical same-point near-null instead of excluding it.

## Engineering conclusion

The new conservative `K M⁻¹ K` finite-volume spatial-operator correction **does** reduce global complex error 0.873→0.334 for this frozen high-PPW pair, and preserves rigorous mass/K symmetry and zero-mode conservation, but **FAILS** the unchanged original complex 0.20, magnitude 0.25 and phase 15° thresholds. In particular it worsens the most adverse relative amplitude by approximately 2×. A single grid pair is insufficient to establish asymptotic convergence even if the metric had passed. This is NOT a claim that applying a uniform-grid fourth-order term on a sloped cutcell boundary yields global fourth-order accuracy. It does not alter original upstream native run25/run76, its PPW8/10/12, independent MFEM or BRAS measurements.

**Canonical original PFFDTD point q0 = SELF_CONVERGENCE_FAILED; independent physical BRAS/room = NOT_VALIDATED; product = NO_GO; PR Draft; Issue OPEN.**

Reproducible numerical runner: `scripts/run_r130d_exact_roof_q0_kmk_dispersion.py`; true correction operator `backend/src/htdt/r130d_conservative_dispersion_correction.py`; fail-closed regression `backend/tests/test_r130d_exact_roof_q0_kmk_dispersion.py`; [all raw signed evidence JSON](../benchmarks/acoustics/r130d_exact_roof_q0_kmk_dispersion_evidence_2026-10-09.json). Original source data SHA-pinned outside git, never fabricated.
