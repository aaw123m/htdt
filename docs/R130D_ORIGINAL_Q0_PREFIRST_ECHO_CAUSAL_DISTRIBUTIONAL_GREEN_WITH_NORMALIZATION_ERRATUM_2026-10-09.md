# R130D #938 — ORIGINAL PFFDTD q0 causal first-arrival weak Green: direct field improves while full 250ms room still FAILS

2026-10-09 JST. Branch `feat/r130d-embedded-neumann-fv-20261009`, Draft PR #1055, Issue #938.

## Actual hands-on original q0 diagnostic and prospective integrity

After independently implementing the exact free-space causal point Green and individual first-specular planar Neumann roof/box reflections, we examined the **actual unchanged original PFFDTD full unfiltered native wave** in a compact causal time-window **strictly before ANY first rigid room-wall reflection**. This is a distributional diagnostic against the continuum retarded fundamental solution, **NOT** the original 250ms scored P_T/Q_T (which remains entirely unchanged, signed and FAIL). It does not alter the original finite record, physically move source/receiver, smooth q0, erase high-frequency modes, fit the source amplitude or relax any threshold.

Two separately precommitted experiments must NOT be conflated:

1. Initial causal distribution plan precommitted and pushed as **`0c405ce1233d060d343756a4c9771c958ec02a99`** before observing native weak direct responses at widths **0.50, 0.80, 1.20 ms**. Its analytic absolute prediction **mistakenly multiplied by an extra c²**. This is a **PHYSICAL UNIT/OPERATOR NORMALIZATION ERROR**, and those first analytic absolute-value discrepancies **ARE INVALID** as physical-model-vs-wave verification. Preserve the first immutable JSON `benchmarks/acoustics/r130d_original_q0_causal_prefirst_weak_evidence_2026-10-09.json` with full original HDF5 fields and incorrect analytic expected values, unequivocally labeled INVALID here. Raw native weak pressure observations in that record are genuine, but the original c²-scale comparisons MUST NOT be promoted.
2. An **explicit prospective source-normalization ERRATUM and NEW not-previously-evaluated witness widths** was committed and pushed before the second results as **`0e997c358f4ab25bd6968207f523f09b0661eb9e`**, [skip ci], preserving disclosure that the first widths and results were already observed. **New widths = 0.65, 1.00, 1.50 ms**, not retrospectively selected from the first record. The native HDF5 source/wave still is exactly identical and the new physical source scale is *derived from the wave equation*, with **zero fitted gain**. The new witness widths are prospective relative to their own newly computed outcomes, but they are not a blind independent physical data set (same original saved five q0 waves).

### Independent correct continuum causal source normalization (fundamental solution)

The original PFFDTD discrete one-sample source has **`sum(in_sigs[:,0]) = (c Δt/h)² / h = c² Δt² / h³`**, at exactly the eight original native grid points and weights. The 3D normalized wave operator is

`[(1/c²)∂²_t − Δ] φ = δ³(x−xs) q(t)`,

with exact causal retarded fundamental solution `G(t,r) = δ(t−r/c)/(4πr)` for `r>0`. The equivalent conventional equation `[∂²_t−c²Δ] φ = c² δ³ q(t)` has fundamental solution **`G/c²` for its right-hand-side unit δ³δ(t)**. The original native q[0]=1 over one time sample has integrated source time area `Δt`; therefore `φ(t)≈Δt G(t,r)`, NOT `c²Δt G(t,r)`. Physical pressure is `p=ρ∂t φ`, and dividing its weak-integrated response by the original `Q_T=Δt` gives the EXACT distributional free-space direct expected value for a compact C∞ test function w:

`W_cont = −ρ ∑_i∑_j [s_i r_j/(4π |xi−xj|)] w'(|xi−xj|/c)`.

**NO extra `c²`; NO empirically fitted gain.** The erroneous first reference multiplied this expression by `c²=117786.24`; the corrected reference is explicitly recorded as analytic old value divided by exactly this known physical constant. Both original PFFDTD full room q0 and the independent previous causal continuum Green benchmark are unchanged.

### Diagnostic temporal witnesses, no high mode cut or canonical gate change

For original physical source (1.5,2,2)m, receiver (2.5,2,2)m, direct path `τ=1m/c=2.913752914ms`. Earliest physically valid single-wall reflection is from the true **sloped Neumann roof at 8.966876703ms**; x/y/z walls reflect later. All original HDF5 eight-node source-to-receiver reflected first-arrival paths were independently checked, not only the one ideal physical point.

The declared compact physical test function is an **odd Gaussian multiplied by a C∞ bump** (a *test function* for the distribution, not an injected Gaussian or replacement source):

`w(t) = ((t−τ)/σ) exp[−(t−τ)²/(2σ²)] exp[1−1/(1−((t−τ)/R)²)]` when `|t−τ|<R`; otherwise exactly zero.

Set `R=3.2ms`, so **support ends at 6.113752914ms**, strictly before the real roof reflection at 8.966876703ms; old unused whole-record q0 samples remain in their original 250ms storage. Actual original native `sim_outs.h5/u_out` eight receiver wavechannels are recombined with **the actual original eight `out_alpha` weights**; physical pressure is the original adapter **one-sided n=0/N−1, centered n=1..N−2 derivative**, unchanged density 1.2kg/m³. The auxiliary witness evaluates `dt∑p[n]w(n dt) / [dt∑q[n]]` with the original unmodified `q[0]=1`, i.e. `∑p[n]w(n dt)`. The entire 250ms signed 40/80Hz full pressure P/Q is **independently recomputed and checked identical** to the frozen original PFFDTD record to relative tolerance 2e−6 on every grid.

### Actual newly prospectively evaluated weak point-source causal witness results

**Actual native original q0 pre-first-echo weak pressure divided by the correctly normalized exact continuum analytical 64-pair Green weak reference** (1.000000 means agreement; not a release acceptance score):

| Original PPW | New test width 0.65ms | New test width 1.00ms | New test width 1.50ms |
|---|---:|---:|---:|
| 28 | **0.870490** | **0.969839** | **1.002222** |
| 32 | **0.911603** | **0.977904** | **0.986856** |
| 36 | **0.931326** | **0.983297** | **0.990155** |
| 40 | **0.951481** | **0.987285** | **0.999155** |
| 44 | **0.966703** | **0.989891** | **0.996179** |

This is **real new supporting diagnostic evidence**: for the most time-localized **0.65ms** physical test, the original native physical direct wave's absolute relative error to the exact 64-node causal continuum weak Green shrinks from **12.95%** PPW28 to **3.33%** PPW44, monotonically over the five grids. For the 1.00ms test, error shrinks from **3.02%** to **1.01%**, monotonically. The 1.50ms sequence is already within ~1.4% and **is not strictly monotone**, which is candidly retained. No rigid roof echo can physically have arrived within this compact test support. That supports the original PFFDTD direct-wave *weak distributional propagation* approaching its causal continuum 64-point source representation, without claiming high-frequency pointwise waveform convergence or the full-room Green solution.

**Critically, original unchanged full 250ms q0 with both SIGNED 40/80Hz bins continues to FAIL all four adjacent PPW pairs**:

| PPW pair | TRUE original entire 250ms original PFFDTD complex relative, gate <=0.20 |
|---|---:|
| 28→32 | **1.246927 FAIL** |
| 32→36 | **0.761305 FAIL** |
| 36→40 | **0.367367 FAIL** |
| 40→44 | **0.958742 FAIL** |

The independent bounded direct arrival weak test cannot identify a single unique root cause of the late-time complex drift: real room high-frequency modal accumulation, reflection at the sloped roof, multibounce, grid phase dispersion and point-source/point-receiver interactions remain. This is, however, a useful **causal isolation result**: native original directly propagated first pulse can agree with a correct continuum point Green in pre-reflection weak testing, while original entire room response remains nonconvergent.

### Code and retained evidence

- First frozen (wrong dimensional amplitude) plan and **historical invalid analytic-reference evidence**: `benchmarks/acoustics/r130d_original_q0_causal_prefirst_weak_test_plan_2026-10-09.json`, `benchmarks/acoustics/r130d_original_q0_causal_prefirst_weak_evidence_2026-10-09.json`. Do not delete or rewrite and do not use its absolute comparison for release.
- Precommitted normalization correction + new widths: `benchmarks/acoustics/r130d_original_q0_causal_prefirst_green_normalization_erratum_plan_2026-10-09.json`.
- Actual NEW correctly normalized per-grid, per-width native/HDF5/analytic full signed trace evidence, old SHA retained with invalid flag: `benchmarks/acoustics/r130d_original_q0_causal_prefirst_green_normalization_evidence_2026-10-09.json`.
- C∞ compact function and exact derivative, real HDF5 unchanged native weak test: `backend/src/htdt/r130d_causal_prefirst_weak.py`, `scripts/run_r130d_original_q0_causal_prefirst_weak.py`.
- Fixed prospective normalization runner: `scripts/run_r130d_original_q0_causal_prefirst_green_normalization.py`.
- Independent fixed causal-source units, compact derivative, fail-closed source/gates, true original full 250ms and exact new HDF5 evidence negative regression: `backend/tests/test_r130d_original_q0_causal_prefirst_green_normalization.py`.

### Next engineering decision

Do **NOT** replace original 250ms gates by causal witnesses: the user requires genuine full-room original point q0 convergence. A high-value **next preregistered** study is to independently test the **first true sloped-roof reflection** against the analytic 8×8 first-image reflection distribution in an appropriately isolated causal time interval before the second return, to determine whether the nonconvergence begins at *boundary interaction* rather than the already well-behaved initial direct wave. The exact roof reflection geometry and all six image paths have already been implemented. The same test must clearly disclose that a Neumann wedge may also have edge-diffracted/multiple early waves, and the complete 250ms q0 failure remains valid.

**Original upstream PFFDTD q0: SELF_CONVERGENCE_FAILED. Independent physical BRAS/MFEM/owned-room: NOT_VALIDATED. Product: NO_GO. PR #1055 Draft OPEN; Issue #938 OPEN.** All waves/tests local, original upstream frozen, no manual GitHub Actions, scratch/ preserved.
