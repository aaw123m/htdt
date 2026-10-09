# R130D #938 — Original PFFDTD native stair-roof Neumann operator fails exact roof-tangential harmonic field; true cut-Q1 weak operator passes

Date: **2026-10-10 JST**. Repository `ka0923s-a11y/HTDT`, working branch `feat/r130d-embedded-neumann-fv-20261009`, Draft PR #1055, open Issue #938.

## Why this independent operator-level experiment

Previous true-geometry numerical experiments have established that the original 8point q0 direct arrival becomes increasingly consistent with an exact free-space retarded Green weak distribution at PPW28–44, while the first sloped-roof reflection window and full unmodified 250ms signed 40/80Hz room response remain nonconvergent. Earlier original native `bn_ixyz`/`adj_bn` inspection established a local geometric **axis-normal mismatch**, but did not actually apply the original **discrete Laplacian** to an analytically admissible true-sloping-Neumann field.

This new test directly applies **both numerical spatial operators** to the SAME physically exact affine manufactured harmonic field in an actual unchanged original HDF5 room: the original PFFDTD Cartesian 6-neighbor staircase strong Laplacian, and the real true-roof Q1 weak Neumann stiffness, without source/wave/timing fitting.

Precommitted exact experiment plan `benchmarks/acoustics/r130d_original_q0_oblique_neumann_manufactured_affine_residual_plan_2026-10-10.json`, commit **`60ad57241412d56eae6102f8f775505e6427ef10`**, was **pushed before any new manufactured-field numerical run**; no retroactive stencil selection/gate changes.

Original upstream SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`, exact original SHA-256 pinned `vox_out.h5`, `comms_out.h5`, `sim_outs.h5` for PPW28/32/36/40/44, unchanged original 8source/8receiver trilinear HDF5 weights/flat indices, source=(1.5,2,2)m, receiver=(2.5,2,2)m, `q0[0]=1`, `q0[n>0]=0`, full native 250ms 40/80Hz signed original `P_T/Q_T` and gates complex RMS ≤0.20/max magnitude ≤0.25/max phase ≤15° all retained. No original upstream wave reruns, no filtering/damping/observability redefinition or GitHub Actions.

## Exact physical affine Neumann harmonic counterexample

True roof plane is `z=4−y/4`; physical 2D cross-section is `0≤y≤4`, `0≤z≤4−y/4` with area exactly **14m²**, extruded across x=0..4 to **56m³**. True roof yz outward normal `n=(1/4,1)/sqrt(17/16)`.

Manufactured affine field:

`u(y,z)=y−z/4,quad grad_{yz}u=(1,−1/4),quad Δu=0,quad n_roof·grad_{yz}u=0.`

The field intentionally need NOT satisfy Neumann conditions on other side/floor walls. For the diagnostic, restrict test **roof-local** basis DOFs so their support is strictly separated from ALL other physical walls (the original near-roof nodes have physical y>2h, y<4−2h and z>2h). Therefore for a roof-local test basis `N_i`, Green's identity gives **exactly**

`[K_Q1 u]_i = c² ∫Ω grad N_i·grad u dA = c² ∫_{roof} N_i n·grad u ds = 0`,

because Δu=0 and the only wall in the support is true oblique roof. Exact physical clipping and six-point positive degree-four quadrature integrate these bilinear Q1 expressions; the true roof gradient and all positive-support slivers are retained, no fitted eigenmodes or physical wall shift. Local `Ku` residual near numerical roundoff checks actual *weak Neumann geometric consistency* rather than just `K 1=0`.

Conversely, **actual original PFFDTD** `vox_out.h5/bn_ixyz` plus `adj_bn` records six blocked/allowed Cartesian original neighbor flags with fixed original order [+x,-x,+y,-y,+z,-z], not true roof-normal flux vectors. For exact affine `u`, on an original room-boundary grid node with a Cartesian blocked roof neighbor, the ACTUAL original original-coefficient numerical strong Laplacian residual is **analytically and independently evaluated**:

`(L_h u)_i = (a_{+y}−a_{−y}−(1/4)(a_{+z}−a_{−z}))/h`,

where `a_d∈{0,1}` is the stored ORIGINAL adjacency. The independent native seven-point row operation `Σ_d a_d(u_{i+d}−u_i)/h²` agrees to ~machine precision. In native interior nodes with all four yz edges, this expression is exactly zero; at a stair-step oblique roof it generally is **O(1/h)**. For EACH original full native HDF5 original room PPW, strictly select actual positive x-interior and true-wet y/z roof support, exclude other walls, and require at least one truly blocked +y or +z edge physically crossing the roof. The physical bounds `0≤4−y/4−z≤2h` and y>2h, y<4−2h, z>2h are frozen, not fitted after seeing the outcomes.

**Important numerical comparability**: The original strong pointwise Laplacian action here has units **1/m** (because u has units m). The Q1 FE integrated weak stiffness row has units of `c² × length`, and is a different mathematical functional. **Never divide them or compare their raw magnitudes as though they were the same physical flux.** Instead test whether each independently annihilates the manufactured field where its mathematical consistency requires. Report `h × RMS(L_h u)` as a same-operator dimensionless scaling diagnostic across original grid refinement.

## Complete actual ORIGINAL PPW28/32/36/40/44 results

| PPW | Selected TRUE original native roof stencil rows | ORIGINAL stair strong ∆_h u RMS (1/m) | Original **h × RMS** dimensionless | TRUE cut-Q1 roof-only weak `|K u|_max` (its own units) |
|---:|---:|---:|---:|---:|
| 28 | 957 | **3.491895** | **0.428007** | **1.60×10⁻¹⁰** |
| 32 | 1,221 | **4.157961** | **0.445941** | **1.86×10⁻¹⁰** |
| 36 | 1,596 | **4.461694** | **0.425348** | **1.24×10⁻¹⁰** |
| 40 | 2,021 | **5.085740** | **0.436356** | **1.89×10⁻¹⁰** |
| 44 | 2,397 | **5.590678** | **0.436073** | **1.09×10⁻¹⁰** |

The original **native** roof-local strong-residual RMS grows as h shrinks, rather than tending to zero, and h×RMS remains approximately 0.43 (O(1)) over all five grid refinements, exhibiting the expected O(1/h) **local original staircase Neumann boundary consistency defect** for this smooth, true-roof-admissible field. The true-roof Neumann Q1 **weak** row on the same near-roof original Cartesian nodes is at floating numerical roundoff (<2×10⁻¹⁰ absolute, in its own physically integrated weak units), satisfying the correct physical roof constraint.

The independent original 6-neighbor per-row arithmetic matches the closed-form affine residual, and SHA checks guarantee that neither original PFFDTD geometry, native source/receiver nor original pressure wave was edited. This is a substantially stronger **spatial operator manufactured-solution** validation than simply counting staircase-face pseudo-normals.

### Canonical original broadband full record remains FAIL

The very same SHA-pinned real native full HDF5 8channel wave `sim_outs.h5/u_out` and original `out_alpha` coefficients were recombined into the unchanged 250ms velocity potential, converted using original forward/center/backward sample pressure derivative, and recomputed with q[0]=1 as **original complete signed `P_T/Q_T` at 40/80 Hz**, independently matching historical original within 2×10⁻⁶ relative at EVERY PPW. Frozen original self-convergence is unchanged:

| Original PPW adjacent comparison | Original full unfiltered 250ms q0 signed complex RMS relative | Frozen complex gate ≤0.20 |
|---|---:|---|
| 28→32 | **1.246927** | **FAIL** |
| 32→36 | **0.761305** | **FAIL** |
| 36→40 | **0.367367** | **FAIL** |
| 40→44 | **0.958742** | **FAIL** |

All original full-record three-gate comparisons FAIL. There is NO revised smoothing, no temporal clipping for original acceptance, no spectral mode cutoff and no release qualification.

## Scope, limitation and next actual numerical solver engineering step

**What this proves:** The original real PFFDTD staircase-graph native strong Laplacian is not locally consistent for a smooth true inclined-roof harmonic Neumann field. A physically cut-Q1 weak Neumann operator **is** consistent with the same manufactured field. Thus boundary-operator geometry is a concrete, independently reproduced source of numerical error and a justified engineering target for an actual upstream-compatible boundary redesign.

**What this does NOT prove:** That roof local inconsistency is **the only cause**, or quantitatively accounts for the full room 250ms signed 40/80Hz nonconvergence. Prior fully consistent physical cut Q1, positive row-lumped Q1, fixed midpoint Q1, and exact-time Q1 all satisfied the roof weak-form manufactured condition but still failed full 250ms original q0. The nonsmooth point impulse, high-modal point source/receiver coupling, native pressure observation discretization, long-time multibounce and original upstream stencil-time coupling remain unsolved. Equating the Q1 weak numeric residual to original PFFDTD strong residual would be dimensionally erroneous.

**Next required implementation:** an original-grid-compatible **symmetric/conservative and stable inclined-boundary discrete flux reconstruction** that is both consistent for harmonic roof-tangential fields AND reproduces the original physical 8source/8receiver q0 full finite 250ms signed P_T/Q_T. Must prove Neumann K1, discrete energy/positive mass, no artificial sliver cutoffs, and full 5grid three-gate convergence, with independent 64-pair first roof echo weak reference and broadband source. No retroactive phase/frequency tuning, no Actions wastage.

### Retained reproducibility

- Precommitted and pushed ahead of experiments: `benchmarks/acoustics/r130d_original_q0_oblique_neumann_manufactured_affine_residual_plan_2026-10-10.json` commit `60ad57241412d56eae6102f8f775505e6427ef10`.
- Actual per-row native boundary Laplacian vs true physical cut-Q1 weak Neumann manufactured counterexample: `backend/src/htdt/r130d_true_roof_manufactured_neumann.py`.
- SHA-pinned original five-grid real native HDF5, independent unchanged original q0 pressure 250ms signed recomputation, true physical Q1 manufactured runner: `scripts/run_r130d_original_q0_true_roof_affine_neumann_residual.py`.
- Independent analytic harmonic, synthetic original axis-blocked counterexample, HDF5 provenance and full 4-pair frozen real q0 negative tests: `backend/tests/test_r130d_original_q0_true_roof_affine_neumann_residual.py`.
- Five real full native original PPW residuals and actual separate weak Q1 consistency measures, no mixed-units interpretation, all raw SHA and 4 original complete full250ms signed FAIL scores: `benchmarks/acoustics/r130d_original_q0_oblique_neumann_manufactured_affine_residual_evidence_2026-10-10.json`.

**Original upstream PFFDTD point q0 SELF_CONVERGENCE_FAILED; independent BRAS/MFEM room NOT_VALIDATED; product NO_GO; Draft PR #1055 and Issue #938 OPEN.** All experiments/tests local, no new upstream PFFDTD waves, no manual GitHub Actions, `scratch/` retained.
