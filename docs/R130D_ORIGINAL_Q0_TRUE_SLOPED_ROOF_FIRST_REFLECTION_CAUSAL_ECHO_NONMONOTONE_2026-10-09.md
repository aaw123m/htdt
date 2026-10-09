# R130D Issue #938 — original PFFDTD single sloping Neumann roof reflection causal witness: grid-sensitive FAIL

2026-10-09 JST; branch `feat/r130d-embedded-neumann-fv-20261009`, Draft PR #1055 and Issue #938 OPEN. This is a scientific **root-cause localization diagnostic** using true original native waves. It does **NOT** replace original 250ms full signed qualification.

## Prior independently established baseline and preregistration

Previously, the exact continuum retarded point Green reference and the original real PFFDTD q0 first-direct-arrival diagnostic established that, for compact **physical-time test witnesses BEFORE any reflection**, original real native PFFDTD source/receiver wave propagation approaches the physically normalized exact Green distribution with grid refinement. Using widths **0.65ms / 1.0ms / 1.5ms**, at PPW44 the real native divided by exact original 64node free-space causal reference was **0.966703 / 0.989891 / 0.996179**, respectively. The physical source PDE normalization erratum was explicitly disclosed before those second widths were calculated, and original full 250ms point-q0 score never changed. This previous outcome was already observed when planning the roof echo work.

To isolate the **first true sloping-roof Neumann image** instead of the direct wave, an entirely new causal witness experiment was frozen and **pushed BEFORE ANY new roof-echo outputs** as **`3406d54b99cc1af9f6eea010db88f51453c39e3d`** [skip ci]. All original PFFDTD native source/receiver HDF5 SHA checks, original q[0]=1 and q[n>0]=0, original 8 individual Cartesian source and receiver node indices and weights, original PPW28/32/36/40/44 native h/Ts/Nt, source strength c² dt²/h³, exact original 250ms wave and **full signed** 40/80Hz canonical three-gate complex .20/magnitude .25/phase 15° were retained. The original upstream PFFDTD was not modified and no extra PFFDTD waves or GitHub Actions were launched.

The true physical 56m³ convex room has source **(1.5,2,2)m**, receiver **(2.5,2,2)m**, sound speed 343.2m/s, rho=1.2kg/m³ and a **true sloping Neumann roof** `z=4−.25y`. Its first reflection via the infinite-plane image, checked to hit the physically finite sloping roof face, is **8.966876703 ms** after the source input; the direct geometric wave arrived **2.913752914 ms** earlier. The nearest other physical single-wall image arrives no sooner than x=0/x=4 at **11.655011655 ms** for the true physical point. All 64 original 8node source/receiver finite-face *nonroof* reflection paths were individually checked against the selected causal support.

Frozen **auxiliary roof weak test witness**: true physical roof image center `τ_roof=8.966876703 ms`, compact support radius `R=1.1 ms`, so the entire witness has support in **[7.866876703,10.066876703] ms**, before nonroof first specular wall echoes, while the earlier free-space *analytic* direct `δ(t−r/c)` has no overlap. Three physical widths frozen prospectively: **σ=0.35 / 0.60 / 0.85 ms**. For each σ,

`w(t)=((t−τ_roof)/σ) exp(−(t−τ_roof)²/(2σ²)) exp(1−1/(1−((t−τ_roof)/R)²))` inside `|t−τ_roof|<R`, otherwise exactly zero.

Its derivative is computed analytically; this is a **test function** of the original *unchanged* 250ms sample record, **not** an injected Gaussian, observation cut applied to the original canonical signed score, or a mode filter. It evaluates `W_native=dt Σ p[n]w(n*dt) / (dt Σ q[n]) = Σ p[n]w(n*dt)`, using exact original 8node `sim_outs.h5/u_out`, original out_alpha, rho=1.2 and original n0/last one-sided plus interior centered potential-to-pressure finite difference.

The physical Neumann reflection sign is +1. Every one of the native 64 original pairs is independently reflected across the actual sloping roof plane, and the optical/specular foot must lie on the **FINITE** true sloping roof before contributing. The correctly normalized causal single-roof *continuum* weak reference is

`W_ref=−ρ Σ_{i,j valid} s_i r_j w'(r_roof_ij/c)/(4π r_roof_ij)`.

The old invalid extra c² has NOT been used in this new experiment. All original source coefficients, pulse and receiver weights are unchanged; no fitted amplitude or variable time shift. Full raw native 250ms P_T/Q_T independently recomputed from the very same waves before evaluating this separate temporal witness, and compared against the original frozen saved signed result to ≤2e−6 relative.

**Critical limitation:** Although the **analytic** direct δ and all other **single-wall** image responses are outside this compact window, the ORIGINAL *numerical* direct pulse may have a long, dispersive tail extending into the roof window, and early edge/multiple scattering may also differ from infinite-plane images. Therefore `W_native/W_ref` is **NOT** a measured pure physical roof reflection coefficient; it is a controlled causal-window diagnostic of the real original numerical room response near the earliest physical roof echo. The difference cannot by itself uniquely diagnose or fully explain the later 250ms result.

## Five-grid actual untouched original PFFDTD q0 vs physical Neumann first-roof image

**Ratio: original total actual native room pressure weak functional in roof-centered window / exact single finite-face roof-image weak reference.** 1 would match the one-image waveform under the above mathematical restrictions; the full original room is more complex.

| Original PPW | Roof width 0.35ms | Roof width 0.60ms | Roof width 0.85ms |
|---|---:|---:|---:|
| 28 | **0.381068** | **0.301868** | **0.265012** |
| 32 | **0.292039** | **0.388148** | **0.423804** |
| 36 | **0.348730** | **0.449347** | **0.502977** |
| 40 | **0.328913** | **0.811291** | **0.985285** |
| 44 | **0.354144** | **0.712416** | **0.849137** |

Unlike the comparatively well behaved **direct** arrival, the first physical roof-echo region exhibits **large variations and significant non-monotonicity** under all three fixed physically specified widths. Notably, σ0.85ms PPW40→44 changes from 0.985285 to 0.849137, **worsening**, despite finer native grid. σ0.60ms PPW40→44 changes from .811291 to .712416. σ0.35ms remains ~0.29–0.38 across all five levels with no monotone trend. The 64pair image is a meaningful independent direct physical geometry reference, but native direct dispersive tails mean the ratio cannot be called the pure reflection amplitude error.

**Unchanged canonical FULL 250ms signed original PPW q0 room scores** (complex relative norm gate ≤0.20):

| Original actual native PPW pair | Original full 250ms P_T/Q_T complex relative |
|---|---:|
| 28→32 | **1.246927 — FAIL** |
| 32→36 | **0.761305 — FAIL** |
| 36→40 | **0.367367 — FAIL** |
| 40→44 | **0.958742 — FAIL** |

All original full complex/magnitude/phase gates fail all four pairs and strict monotonic improvement is absent. The new auxiliary roof weak functional is **not** an acceptance criterion, and no favorable roof window/grid is used to replace the original.

## Numerical physical conclusion and next engineering target

The comparison now separates THREE physical/algorithmic questions using the **same original SHA-checked five native waves and untouched source**:
1. **Direct causal continuum propagation** within pre-roof-arrival test support: tends toward the exact physical retarded Green weak distribution at high PPW; σ0.65ms ratio improves monotonically from .8705 to .9667.
2. **First true sloped-roof interaction region**: nonmonotonic and sometimes 50–70% below the exact true roof's single image in fixed weak functionals; numerical direct-wave tail is a known unresolved confounder.
3. **Original entire room reverberant 250ms signed 40/80 Hz**: nonmonotonic and FAIL all four pairs at original frozen gates.

A justified next numerical-solver task is to **directly examine the original PFFDTD staircase boundary Neumann reflection operator**, geometric normal/flux at the sloped roof, and a true continuous Neumann boundary correction checked against *both* full 250ms q0 and this frozen early physical echo witness. Prior FV, P1 and cut-Q1 space schemes failed full 250ms, so none can be asserted qualified. A future new solver must preserve original source/receiver/impulse/whole 250ms, keep all high modes, satisfy SPD/energy/Neumann and independent physical Green phase on all grids, preregister gates and compare to canonical upstream. No removal of small cells, filtering or roof timing fit.

- Prospective plan: `benchmarks/acoustics/r130d_original_q0_first_roof_echo_causal_weak_plan_2026-10-09.json`.
- Exact true finite-face Neumann image analytical code: `backend/src/htdt/r130d_causal_first_roof_echo.py`, reusing `r130d_retarded_point_green.py`.
- Real original SHA-native `sim_outs` 250ms HDF5 full wave + roof witness runner: `scripts/run_r130d_original_q0_first_roof_echo_causal_weak.py`.
- Independent true specular roof image, physical causal compact derivative, unchanged native wave verification and all old complete signed adverse score tests: `backend/tests/test_r130d_original_q0_first_roof_echo_causal_weak.py`.
- Entire real original wave and true physical finite roof signed weak 3×5 outcomes, 64pair nonroof causal constraints, all original 8node and previous corrected-direct controls, full original 250ms 40/80Hz scores: `benchmarks/acoustics/r130d_original_q0_first_roof_echo_causal_weak_evidence_2026-10-09.json`.

**Original native PFFDTD q0 = SELF_CONVERGENCE_FAILED. Independent BRAS/MFEM/owned-room = NOT_VALIDATED. Product = NO_GO. PR #1055 Draft OPEN; Issue #938 OPEN.** No GitHub Actions manually triggered, original source native wave never regenerated, scratch/ retained.
