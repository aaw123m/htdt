# R130D #938 — independent 250 ms full-state corrected roof FV/Newmark-CG wave: exactly reproduces full-mode FAIL

2026-10-09 JST. No GitHub Actions dispatched. Draft PR #1055 / Issue #938 OPEN.

## Prospective, bounded, physically fixed numerical experiment

The [true full-state solver replay plan](../benchmarks/acoustics/r130d_exact_roof_q0_kmk_direct_wave_replay_plan_2026-10-09.json) was committed AND pushed before numerical execution as `e9532e3ca3cdfa9dec8efb3547d72f68ca0816eb`. The earlier [all-mode analytical KM⁻¹K correction experiment](../benchmarks/acoustics/r130d_exact_roof_q0_kmk_dispersion_evidence_2026-10-09.json) was itself frozen in `64a268f` and saved as `d6c5af6` with negative results. This new experiment is a **genuine independent full-space dynamic computation** directly using the new **2,197,891 / 2,968,940-nonzero** physical K4 sparse stiffness matrix, diagonal physical exact cutcell M, implicit beta=1/4 Newmark and preconditioned SciPy CG, **not** a spectral resummation. It is not native original PFFDTD qualification.

Original upstream PFFDTD HDF5 source/receiver and voxel SHA-256, original 8-point trilinear source (1.5,2,2) m and receiver (2.5,2,2) m, q[0]=1 and zero after q0, true native per-grid Ts and Nt, 250ms full pressure record, 40/80Hz signed P_T/Q_T and original frozen gates 0.20/0.25/15° are unchanged. Exact planar sloped roof has original physical 56m³ volume; no source smoothing, damping, mode removal, waveform taper, data exclusion or retroactive score criterion.

The corrected operator is **K4 = K + h²/(12c²) K M⁻¹ K**, with physical M unchanged. All 3D original cutcells are propagated numerically, including the high-frequency point excitation. The same independent full-mode candidate had earlier been computed from all x×yz generalized modes, with closed-form finite Newmark transfer. The direct original-clock full wave is now independently checked against those *previously saved, SHA-bound* complex 40/80 Hz bins at a **precommitted maximum complex relative 2e−5**.

## Full real numerical replay and validation

| Exact-roof full-state K4 FV + original q0 | PPW40 | PPW44 |
|---|---:|---:|
| True wave active original cutcells | **91,415** | **123,032** |
| True 250 ms full record samples | **1,734** | **1,908** |
| Actual true CG maximum iterations per solve | **18** | **18** |
| Relative energy drift after point source | **2.3686e−9** | **2.7772e−9** |
| New full-state direct wave vs independent prior ALL-mode transfer complex relative | **2.3882e−9** | **1.6851e−8** |

The full-state method reproduces independently computed, untruncated all-mode pressure response far below the preregistered **2e−5** bound at both original grid sizes; exact 56m³ wall geometry, source and finite-record observation contract are unchanged. The strong verified energy preservation and true linear-system residual show the **remaining nonconvergence is not attributable to a defective spectral-only reconstruction or unstable CG implementation of K4**.

The **actual full-state direct pressure waves**, not only spectral predictions, give:

| Original physical point q0 250ms PPW40→44 | Complex relative | Maximum magnitude relative | Maximum phase | Original gate |
|---|---:|---:|---:|---|
| True 3D K4 full-state Newmark-CG | **0.3343250173** | **6.4430599100** | **116.707959°** | **FAIL** |
| Earlier all-mode K4 analytical propagator | **0.3343250317** | **6.4430583365** | **116.707979°** | **FAIL** |

As before, the **40Hz** near-null signed transfer on PPW44 is particularly unfavorable; that frequency is NOT excluded. All exact full two-bin signed transfers, raw solver iteration/energy metadata, original HDF5 SHA hashes and every adverse comparison are retained in [replay evidence](../benchmarks/acoustics/r130d_exact_roof_q0_kmk_direct_wave_replay_evidence_2026-10-09.json). Actual full-state runner: `scripts/run_r130d_exact_roof_q0_kmk_direct_wave_replay.py`. Fail-closed test: `backend/tests/test_r130d_exact_roof_q0_kmk_direct_wave_replay.py`.

## Qualification boundary

- A real mathematical spatial-operator change is implemented, physically conserved and numerically independently reproduced, but **fails** the original fixed complex, magnitude and phase acceptance for PPW40→44.
- A two-grid experimental solver result cannot be promoted to full original PPW8/10/12 run25/run76 PFFDTD self-convergence, independent MFEM comparison, BRAS measurements or product.
- Never reinterpret the better global complex error as a PASS or discard the harmful signed 40Hz output.

**Original upstream PFFDTD q0 = SELF_CONVERGENCE_FAILED; external BRAS/owned-room = NOT_VALIDATED; product = NO_GO; Draft PR #1055 OPEN, Issue #938 OPEN.**
