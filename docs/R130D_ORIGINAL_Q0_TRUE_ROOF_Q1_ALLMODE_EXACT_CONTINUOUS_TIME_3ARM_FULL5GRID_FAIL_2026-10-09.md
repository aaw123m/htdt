# Issue #938 — true physical sloped Neumann cut-Q1, exact continuous-time all-mode δ source against native 250ms q0: ALL 3 MASS FAMILIES FAIL

2026-10-09 JST. GitHub `ka0923s-a11y/HTDT`, Draft PR #1055, Issue #938. The original upstream PFFDTD q0 is STILL **SELF_CONVERGENCE_FAILED**, independent BRAS/MFEM/owned-room physics is **NOT_VALIDATED**, production **NO_GO**.

## Preregistered scientific question and strict original criteria

After true-cut Q1 variational consistent mass, positive row-sum lump, and nonfitted theoretical half-consistent/half-lump mass all FAIL the original full 250ms signed 40/80Hz point-q0 three-gate self convergence with native Newmark, we need to determine whether native Newmark *temporal phase propagation* is a substantial source of the failure. The new experimental arm is the **exact continuous-time solution to the SEMIDISCRETE PHYSICAL Neumann wave equation**, sampled at EVERY original native timestamp and scored over the ORIGINAL FULL 250ms. All three fixed preobserved Q1 spatial mass families were prospectively specified and are reported, without selecting the best mass afterward.

**The experiment plan was committed and PUSHED before generating ANY new exact-time scores**: `a2b905b8dfa48e96e27ceff7c86a3f745118b853` [skip ci].

Unchanged original authority: upstream original PFFDTD SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`; all original true native HDF5 `vox_out.h5`, `comms_out.h5`, `sim_outs.h5` 5-grid PPW28/32/36/40/44 with original SHA-256; original Cartesian exactly **eight physical source and eight physical receiver positions/weights** producing source=(1.5,2,2)m and receiver=(2.5,2,2)m; original q[0]=1 and later q[n>0]=0 with input `sum(in_sigs[:,0]) = c² Ts²/h³`; native every grid h/Ts/Nt; original 250ms full forward/centered/backward pressure derivative and signed `P_T/Q_T` at 40/80Hz; immutable all-three-gate complex ≤0.20, max relative magnitude ≤0.25 and max phase ≤15°. No physical point shifts, frequency masking, modal cutoffs, damping, taper, interpolation time shifts, gain fitting or release gate changes. No manually triggered GitHub Actions or new native upstream PFFDTD waves.

## Exact mathematical temporal propagator

For each of the three **identical preobserved spatial FE mass families** (fully consistent Q1, fully positive row-sum lump Q1, fixed theory-half Q1), assemble true physical 56m³ sloping roof-Neumann weak `M`, `K`, with positive masses and `K1=0`, all true sliver-supporting original Cartesian wet Q1 basis nodes, and complete generalized all-mode eigenpairs `K v=λ M v`. True roof normal and shape integration are identical to the prior versions. Total retained 3D physical modes PPW28/32/36/40/44 are **37,835, 53,001, 75,504, 102,949, 132,341** for **every one** of the 3 mass families (401,630 per family, 1,204,890 per whole experiment). All above-original-native-Nyquist modes are retained (they may alias under sampling, but are NEVER deleted).

**Experimentally changed temporal semantics must be explicitly disclosed**: instead of the prior **implicit β=1/4 Newmark discrete kick**, solve exactly

`M φ_tt + K φ = c² s · Ts δ(t)`,

with the ORIGINAL unmodified native eight-node physical source spatial distribution `s` and original q[0]=1 interpreted as an instantaneous causal unit sample with time integral exactly `Ts`. This keeps its physical integrated volume source strength fixed but changes its high-mode exact first-step response compared with the original PFFDTD and prior Newmark time-steppers. This is an independently defined **experiment**, not the original upstream kernel. It cannot independently distinguish source temporal regularization effects from Newmark phase error with causal asymptotic data, nor can it authorize original upstream release.

Every physical eigenmode has analytical response

`φ_e[n]=c² Ts S_e sin(n sqrt(λ_e) Ts)/sqrt(λ_e)`,

with exact rigid λ=0 limit `φ_e[n]=c² Ts² S_e n`. The combined source/observer factor `S_e=(v_e·s)(v_e·r)` is built from the exact original eight HDF5 node weights. Evaluate physical pressure on original sample times using **the exact original pressure observation difference**: n=0 forward, n∈1..Nt−2 centered, n=Nt−1 backward. Then take the exact finite sum `P_T/Q_T = Σ_{n=0}^{Nt−1} p_n exp(+i2πfn Ts)` (since original q[0]=1). Analytical geometric harmonic progressions retain the original complete rectangular 250ms window, BOTH signed 40/80Hz complex transfers, exact one-sided endpoints, and all modes. The sinc frequency multiplier `sin(ω Ts)/(ω Ts)` in sampled center-differenced pressure is the *unavoidable exact discrete OBSERVATION OPERATOR* for this physical-time harmonic, **not** an imposed mode taper or post-hoc cutoff; every mode is still included in modal pressure, sign and phase.

Independent negative/positive tests reproduce the entire signed two-frequency output from a fully constructed native discrete time signal for synthetic high-frequency/eigenmode cases, including modes above Nyquist and the rigid λ=0 mode; stable frequency sum at 2π temporal aliases; exact energy conservation of isolated continuous-time Neumann modes (not Newmark dissipation); all three fixed physical first-roof causal witnesses against brute native centered pressure. Real Q1 allmode HDF5 SHA, all three mass groups and every original three-gate score independently retested.

## Full signed original native 250ms 40/80Hz q0 — all adjacent PPW results

**New EXACT-TIME** 5-grid [complex RMS relative / max relative magnitude / max phase] vs corresponding previously preobserved **original native β=1/4 Newmark** physical Q1 mass family. Frozen gates are complex ≤0.20, magnitude ≤0.25, phase ≤15° **ALL AT ONCE**.

| PPW | New exact-time consistent Q1 | New exact-time row-lumped Q1 | New exact-time fixed half Q1 |
|---|---|---|---|
| 28→32 | **0.641364 / 0.856288 / 16.099° FAIL** | **0.305936 / 1.794427 / 20.841° FAIL** | **0.211320 / 0.661335 / 9.215° FAIL** |
| 32→36 | **0.488018 / 1.046995 / 17.242° FAIL** | **0.275687 / 1.350101 / 177.674° FAIL** | **0.812193 / 0.541325 / 174.778° FAIL** |
| 36→40 | **1.180014 / 0.675400 / 177.266° FAIL** | **0.378072 / 0.797131 / 152.872° FAIL** | **0.942030 / 2.396862 / 172.874° FAIL** |
| 40→44 | **1.784047 / 0.228878 / 179.701° FAIL** | **0.183049 / 1.123437 / 7.750° FAIL** | **0.865662 / 0.796747 / 169.113° FAIL** |

The exact-time experiment keeps all original adverse signed real/imag spectra and no near-zero 40Hz transfer is hidden. **ALL 12 adjacent pair three-gate comparisons for the 3 new exact-time arms FAIL**; strictly monotone three-metric convergence across all five grids FAIL for every arm. The original actual unmodified upstream PFFDTD q0 signed complex relative errors remain PPW28→32/32→36/36→40/40→44 **1.246927/0.761305/0.367367/0.958742, all FAIL**.

The previously measured native Newmark physical Q1 [complex relative] control:
- consistent: `2.449105/0.974794/0.523912/1.314187`, FAIL all.
- row-lump: `0.553834/0.727305/0.343471/0.186295`, FAIL all three-gates despite high-PPW complex/phase partial success.
- half theory: `0.565737/0.458917/0.407076/1.255632`, FAIL all.

**Critical adverse high-PPW observation:** new exact-time row-lumped Q1 PPW40→44 complex 0.183049 and phase 7.750° individually pass their frozen gates, but max relative magnitude 1.123437 exceeds gate .25 by ~4.5×. New exact-time fully-consistent Q1 PPW40→44 mag 0.228878 independently passes the magnitude-only gate, while complex 1.784047 and phase 179.701° massively fail. No combination of separately favorable metrics taken from DIFFERENT incompatible mass schemes is legitimate. These results do not establish original room convergence.

## First physical sloping Neumann roof causal echo remained a failing independent diagnostic

The original already frozen analytical true Neumann finite sloping-roof `z=4−.25y` first-image causal point benchmark was held constant, with single-roof arrival 8.966876703ms and original 8×8 source-receiver image geometry; support radius1.1ms, and previously fixed widths .35/.60/.85ms. All three new experimental exact-time Q1 solver native modal pressure traces were evaluated on those SAME samples and physical witnesses without filtering or selecting modes. The full signed 250ms scores remain primary; short-time witness is independent diagnosis, NOT an alternative acceptance.

New signed (total native roof-window weak pressure)/(independent original 64-pair physical true planar first-image weak reference) ratios, widths .35ms / .60ms / .85ms:

| PPW | Exact-time consistent Q1 | Exact-time row-lumped Q1 | Exact-time half Q1 |
|---|---|---|---|
| 28 | −0.3727 / −0.2223 / −0.0301 | −0.7716 / −0.6032 / −0.4859 | 0.4262 / 0.6410 / 0.7443 |
| 32 | −0.1040 / −0.3160 / −0.2661 | −0.4913 / 0.1348 / 0.4239 | −0.3017 / 1.0349 / 1.5903 |
| 36 | −0.2225 / −0.0047 / 0.0787 | 0.0977 / 0.3176 / 0.4063 | 0.6869 / 1.0104 / 1.1259 |
| 40 | 0.0072 / 0.3469 / 0.3483 | 0.4378 / 0.4520 / 0.2937 | 0.8755 / 0.9527 / 1.1535 |
| 44 | 0.1863 / 0.5902 / 0.7041 | 0.4934 / 0.1486 / −0.1148 | 0.7997 / 1.2167 / 1.6461 |

None of the three families is robustly close to unity on all PPW and widths. All mode contributions remain. Native total waveform may retain a direct dispersive tail inside the chosen roof support; these are NOT measured pure roof reflection coefficient errors and cannot by themselves establish a unique culprit.

## Precise scientific conclusion and next engineering priority

Eliminating the numerical temporal *propagation dispersion* with an exact semidiscrete sinusoidal solution, while using physically fixed original-native-duration impulse strength, does **not** yield full original point q0 250ms signed convergence even with exact physical cut-roof weak Q1. The original PFFDTD remains unqualified, and these experimental sine solutions are not replacements for original q0.

A worthwhile next independent experiment is to isolate **physical source Green function singularity / point-receiver spectral sampling and high-order sloped-boundary wave-front pollution** in a controlled exact-room setting, rather than further retrospective smoothing, fitted 40Hz null, source shift, mass-coefficient search, time window selection or amplitude tolerance relaxation. A stable true roof-flux operator should be verified against all original 250ms and the preregistered first true roof witness, not just low-frequency resonances.

**Reproduction**:

- `benchmarks/acoustics/r130d_original_q0_exact_time_continuum_impulse_cut_Q1_allmodes_plan_2026-10-09.json` preregistered and pushed FIRST.
- `backend/src/htdt/r130d_exact_semidiscrete_causal_q0.py`: causal exact physical semidiscrete time propagator and all original native sampled signed finite transform.
- `scripts/run_r130d_original_q0_exact_time_continuum_Q1_allmodes.py`: original SHA pinned HDF5 source / observer / 15 all-mode physical Q1 experiments, old 3 Newmark and real canonical PFFDTD controls.
- `backend/tests/test_r130d_original_q0_exact_time_continuum_Q1_allmodes.py`: independent synthetic entire pressure time trace, harmonic-sum aliases, all-mode energy, fixed weak roof times, original five grids/3 mass/12 adverse three-gate scores.
- `benchmarks/acoustics/r130d_original_q0_exact_time_continuum_cut_Q1_allmodes_evidence_2026-10-09.json`: ALL 15 original per-grid mass arms and signed full 250ms 40/80Hz, old Newmark and raw PFFDTD, all real negative results and three first-roof temporal widths.

No manual GitHub Actions, no new upstream PFFDTD wave runs, scratch/ retained. **Canonical upstream original PFFDTD q0 SELF_CONVERGENCE_FAILED; independent physical NOT_VALIDATED; product NO_GO; PR Draft OPEN; Issue OPEN.**
