# R130D Issue #938 — real sloping-roof Cartesian Q1 positive row-sum lumped mass: 40→44 complex/phase PASS but full original convergence FAIL

2026-10-09 JST. Repository `ka0923s-a11y/HTDT`; branch `feat/r130d-embedded-neumann-fv-20261009`; Draft PR #1055 and Issue #938 OPEN.

## Implemented scheme and experiment provenance

This experiment changes the **actual semidiscrete acoustic wave solver's physical mass discretization**. It does not simply reprocess an existing spectrum. Its prospective numerical plan was committed **and pushed before any new PPW outcome** in **`dac5b5ed0bd45205f0ab7c75ad6282132c6de364`**, with original 250ms signed acceptance plus previously frozen independent first-roof causal weak test fixed. The earlier full consistent-mass Cartesian Q1 cut-roof scheme, true physical Neumann geometry, original native PFFDTD and early roof echo are already preobserved and used as independently saved adverse controls. No retuning after observed scores.

Experimental physical operator:
- True planar roof `z=4−0.25y`, real `0≤x,y≤4`, exact **56m³**. Preserve original Cartesian native x nodes and original Q1 bilinear y/z rectangle basis *including all roof-outside vertices with genuine positive physical cut support*.
- Integrate y/z physical area, Q1 gradient stiffness `Kyz=c²∫physical ∇N_i·∇N_j` with same original 6-point positive degree-4 triangle quadrature over true cut polygons (no boundary shape modifications, sliver floor or ghost node cutoff). Same physical x P1 stiffness `Kx`.
- Starting from the prior exact full consistent physical Q1 FEM mass **only**, form the **positive row-sum lump** `MxL=diag(MxConsistent 1)`, `MyzL=diag(MyzQ1Consistent 1)`, including **ALL positive-support sliver masses**. Resulting physical acoustic operators:
  `M=MxL⊗MyzL` and `K=Kx⊗MyzL + MxL⊗Kyz`. Thus **the one-dimensional x stiffness and two-dimensional y/z true Neumann stiffness are unchanged**, while the full 3D Kronecker-weighted stiffness tensor factors necessarily change along with mass; it is incorrect to claim the entire 3D K remains numerically identical to the fully consistent Q1 baseline.
- Strong true conservation checks: exact `1ᵀM1=56m³` with positive every diagonal value; full true 3D `K1≈0`; independent manufactured full 3D physical affine `u=x+z` has **exact physical Neumann gradient energy `uᵀKu=112c²`** within 2e−9 relative. Row-lumped discrete `uᵀMu` does **NOT** equal true continuum `∫(x+z)²=2780/3`; that known mass quadrature error is explicitly captured, never hidden.
- Original SHA-256-pinned actual PFFDTD native `vox_out.h5`, `comms_out.h5`, `sim_outs.h5` for **all five PPW28/32/36/40/44**. Preserve EXACT original source **(1.5,2,2)m**, receiver **(2.5,2,2)m**, original eight node flat indices and **all eight native source and receiver weights**, original q[0]=1 and every later q[n>0]=0 with original sum `in_sigs[:,0]=c²dt²/h³`, original grid h / native Ts / Nt and original full **250 ms** no taper. Preserve original `p=rho*∂tφ` with one-sided initial/final and centered interior derivative and both **SIGNED** 40/80Hz P_T/Q_T. The same implicit beta=1/4 causal Newmark numerical integrator and all **401,630 actual physical 3D modes** are retained across all five native grids, with strict generalized mass Gram/eigenresidual proof; no damping or high mode cutoff.
- All original frozen **complex≤0.20, max magnitude relative≤0.25, max phase≤15°**, all four adjacent grid comparisons, and three-metric strict monotonic requirement preserved in advance. All signed complex values and adverse per-bin metrics saved; no favorable isolated metric promoted.
- A **separate preregistered** roof causal witness (first true Neumann sloped-roof image at **8.966876703ms**, physical compact support radius **1.1ms** and fixed test widths **0.35/0.60/0.85ms**) is evaluated using exact analytical native Newmark modal pressure *inside the witness support* with all modes retained, and independently compared with the frozen actual original 64-node finite-face roof image. That witness does **not** change the original full 250ms acceptance. Even though the analytic single-wall image is causal, a native waveform can retain direct dispersive tails/edge contributions; witness/image ratio is not a true isolated reflection coefficient.

## Real complete original 250ms source q0 five-grid test

Frozen 40/80Hz signed complex RMS relative, max magnitude relative, max phase are reported below:

| Actual original grid pair | Previously observed exact physical Q1 **consistent mass** complex | NEW positive row-sum **lumped Q1** complex | NEW lump max mag relative | NEW lump max phase | Original all-three fixed gate verdict |
|---|---:|---:|---:|---:|---|
| PPW28→32 | 2.449105 | **0.553834** | **0.830941** | **144.939°** | **FAIL** |
| PPW32→36 | 0.974794 | **0.727305** | **5.283255** | **165.513°** | **FAIL** |
| PPW36→40 | 0.523912 | **0.343471** | **0.778315** | **15.320°** | **FAIL** |
| PPW40→44 | 1.314187 | **0.186295** | **0.316710** | **8.359°** | **FAIL magnitude** |

**Partial physically substantive improvement:** new physical row-sum-lumped Q1 **PPW40→44** has original complex gate PASS (`0.186295≤0.20`) and phase gate PASS (`8.359°≤15°`), but fails required magnitude gate (`0.316710>0.25`), so **no adjacent pair meets all three** and **all-five-grid self convergence FAIL**. At 36→40 phase `15.320°` also just fails, along with complex/magnitude. New results are not monotone and PPW32→36 still very far from acceptance. This is an actual numerical solver improvement in one two-metric coarse/fine comparison, **not** proof of any validated original/upstream or product acoustic model.

The **original actual PFFDTD** complex errors for the same PPW adjacent pairs remain **`1.246927 / 0.761305 / 0.367367 / 0.958742`**, all original gates FAIL and no monotone recovery. The earlier fully consistent cut Q1 is also FAIL on every pair. All negative per-frequency amplitude and complex signed values, plus the unchanged original source time, are retained in the new full JSON.

## Independent first physical Neumann roof reflection diagnostic on NEW mass scheme

All physically supported 64 source-to-receiver roof-image paths retain exact geometry. The native full-mode Q1 row-lumped implicit Newmark modal pressure is evaluated with the **identical** already frozen physical roof witness, *without altering the full native 250ms pulse*. Its observed roof-window `W_Q1-L / W_analytic-single-roof` diagnostic is:

| Original PPW | Frozen width 0.35ms | Frozen width 0.60ms | Frozen width 0.85ms |
|---:|---:|---:|---:|
| 28 | **−0.94997** | **−0.82826** | **−0.73197** |
| 32 | **−2.25814** | **−1.48066** | **−0.97322** |
| 36 | **−0.36554** | **−0.10403** | **0.02910** |
| 40 | **0.97342** | **−0.23302** | **−0.73569** |
| 44 | **1.45565** | **−0.91213** | **−1.77226** |

These **severely sign-changing, non-monotone** first roof-window responses warn that even though PPW40→44 full 40/80 complex and phase metrics improved, the causal roof impulse is *not* physically or grid stably recovered by this mass scheme. Since the window can also collect numerically dispersive direct wave tails, it cannot uniquely distinguish true reflection coefficient from earlier waveform contamination. **Do not select the favorable PPW40 width0.35 ratio 0.97342 as a validated roof reflection.** Earlier actual original PFFDTD true roof causal weak ratios remain untouched in the JSON side-by-side.

### Tests and reproducibility

- Precommitted experiment: `benchmarks/acoustics/r130d_original_q0_exact_roof_Q1_lumped_mass_plan_2026-10-09.json` (push `dac5b5e` **before** any new Q1 lump outcomes).
- Genuine new semidiscrete positive-lumped Q1 Neumann solver and exact all-mode native roof-window formula: `backend/src/htdt/r130d_native_cut_Q1_positive_lumped.py`.
- Five real SHA-pinned original HDF5 PPW28/32/36/40/44 full 250ms signed Newmark and roof witness runner: `scripts/run_r130d_original_q0_exact_roof_Q1_lumped_mass.py`.
- Independent small physical 56m³ affine gradient/M mass Neumann SPD, no-positive-support-truncation, finite true original HDF5, full 401,630 modes, real all four adverse original three-gate replay and independent synthetic exact Newmark pressure witness tests: `backend/tests/test_r130d_original_q0_exact_roof_Q1_lumped_mass.py`.
- Full original real 8node signed 40/80 outcomes, all adverse 4-pair complete original scores, 3×5 new physical causal roof windows, existing full consistent-mass Q1 and canonical PFFDTD controls: `benchmarks/acoustics/r130d_original_q0_exact_roof_Q1_lumped_mass_evidence_2026-10-09.json`.

**Production/physical authority remains unchanged:** Original upstream PFFDTD q0 **SELF_CONVERGENCE_FAILED**; external BRAS/MFEM/owned-room physical validation **NOT_VALIDATED**; product **NO_GO**; PR #1055 Draft OPEN, Issue #938 OPEN. Tests/calculations local, no new original native PFFDTD waves or manually triggered GitHub Actions, `scratch/` preserved.

**Next real solver engineering target:** Prove spectral/energy physically convergent off-axis Neumann reflection against the *true inclined roof* under original q0 using a boundary-normal-consistent scheme that also maintains high-frequency point impulse semantics. Mass lumped Q1 may improve some full-room low-frequency complex errors but fails first reflection causal integrity and the original magnitude gate, so it is not a candidate to approve as-is.
