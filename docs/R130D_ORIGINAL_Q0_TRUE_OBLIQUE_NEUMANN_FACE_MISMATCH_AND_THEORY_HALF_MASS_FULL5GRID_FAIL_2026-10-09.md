# Issue #938 — original staircase normal versus exact inclined Neumann roof, parameter-free 50:50 Q1 dispersion correction: all five-grid q0 FAIL

2026-10-09 JST. Repository `ka0923s-a11y/HTDT`; PR #1055 **Draft OPEN**; Issue #938 **OPEN**. This is an experiment on existing SHA-pinned original high-PPW 8-point source/receiver q0 data and a new energy-stable alternate physical weak acoustic solver, not a production PFFDTD patch/qualification.

## Prospective registration and unchanged physical acceptance

The experimental design and *both* independent tests were committed and **pushed before any new outputs**, as `d717e99e13e8712729f20f55a4a69f049748f4ac` [skip ci]. Previous real PFFDTD original native point q0, original Neumann graph, preobserved true physical consistent-mass cut Q1, preobserved row-sum-positive mass cut Q1, causal direct Green and roof-first-echo window outcomes were already known; they are retained as fixed controls.

Original PFFDTD upstream `aa319f6c86517cb95aabfae8656277da62c3ead5`, original SHA-256 raw `vox_out.h5/comms_out.h5/sim_outs.h5` native **PPW28/32/36/40/44**, point source **(1.5,2,2)m**, receiver **(2.5,2,2)m**, original **8 source indices+weights AND 8 receiver indices+weights**, q[0]=1/q[n>0]=0, original h/Ts/Nt and q0 strength `c² dt²/h³`, true room volume **56m³**, air `c=343.2m/s`, rho=1.2kg/m³, native no-taper **250ms** pressure P_T/Q_T one-sided/centered/last derivatives, fully signed real+imag **40/80Hz** and all original frozen complex RMS≤0.20, maximum relative magnitude≤0.25, maximum phase≤15° were not changed. Every actual 3D high mode/sliver that exists in the candidate physical Q1 solver is retained. No wave filtering, time-window substitution, source reshaping, coefficient fit or manual GitHub Actions.

## Exact original-graph Cartesian roof-normal defect, not a full-wave causality proof

Actual original PFFDTD SHA-256 `vox_out.h5` contains `bn_ixyz` and the real six-neighbor `adj_bn` boundary adjacency map. We inspected **original boundary nodes/blocked stencil directions** that lie strictly within physical x/y walls, near the true inclined roof, with missing +y or +z neighbor **physically beyond z=4−0.25y**; edges at physical y=0/4,z=0 and domain halos are excluded. This is not a synthetic assumed staircase graph: it uses the real original native HDF5 adjacency.

The exact physical normal on the roof is `n_yz=(0.25,1)/sqrt(1.0625)`; the affine tangential field `u(y,z)=y−0.25z` has gradient `(1,−0.25)`. Its **exact inclined-roof Neumann normal derivative is identically zero**: `n·∇u=(.25−.25)/sqrt(1.0625)=0`. The *same field* has nonzero Cartesian-axis derivatives `∂y u=1` and `∂z u=−0.25` across staircase faces. Every true physical Cartesian Q1 bilinear basis can reproduce this affine function exactly, including true clipped roof cells. The original PFFDTD stair-neighbor graph omits normal adjacency along those Cartesian axes, not along the true roof normal.

| Actual original PPW | Blocked +y original native roof stencil faces | Blocked +z original native roof stencil faces | Combined detected faces | RMS of axis-directed ∂u on detected staircase faces | Exact true inclined normal ∂n u |
|---:|---:|---:|---:|---:|---:|
| 28 | 231 | 1,023 | 1,254 | **0.484972** | **0** |
| 32 | 333 | 1,295 | 1,628 | **0.504243** | **0** |
| 36 | 420 | 1,680 | 2,100 | **0.500000** | **0** |
| 40 | 517 | 2,115 | 2,632 | **0.496640** | **0** |
| 44 | 612 | 2,499 | 3,111 | **0.496917** | **0** |

These axis-face RMS values are *per blocked original Cartesian face*, without physical area weighting and **not** the integral wall flux of the original wave scheme. They rigorously establish local **boundary-normal geometric misalignment**; they **do NOT prove that it uniquely causes the original full 250ms nonconvergence**, nor do they show an actual PFFDTD unmodeled flux equal to the numerical RMS. A consistent weak form using true roof geometry provides natural Neumann boundary treatment, but previous cut-Q1 consistent and positive row-sum lumped operators still FAIL full point q0, so the normal defect is only one plausible contributor.

## Second independent prospective numerical arm: non-fitted 50:50 exact Q1 mass blending

To isolate uniform-grid temporal/spatial phase dispersion **without tuning any coefficient to result**, a second Q1 acoustic semidiscretization uses the mathematical fixed blend `M_half = 0.5 M_consistent + 0.5 diag(M_consistent·1)` in physical x (P1) and y/z (true sloping-roof Q1) separately. This is theory-based: on the infinite uniform 1D mesh, with `xi=k h`,

- `K(xi)=(4c²/h)sin²(xi/2)`;
- `M_consistent(xi)/h = 2/3 + cos(xi)/3`;
- `M_lumped(xi)/h=1`;
- **`M_half(xi)/h=5/6 + cos(xi)/6`**; `K/(M_half*c²*k²)=1+O((kh)^4)`, because the leading order-(kh)² stiffness and mass terms analytically cancel. The coefficient 0.5 is not fitted after looking at any Q1 curve.

This **does not prove global fourth-order convergence on the cut and obliquely bounded physical room**. Exact y/z cut-Q1 weak stiffness `Kyz`, physically accurate integration of the true 14m² roof-clipped cross-section and positive-support original external Cartesian nodes are kept. The full semidiscrete operators are `M=Mx_half⊗Myz_half` and `K=Kx⊗Myz_half + Mx_half⊗Kyz_true`. The full 3D stiffness tensor weights necessarily vary with the mass scheme; no claim that the *full* K stays unchanged. Checks of mass positivity/Neumann constant/physical **56m³** and true affine field `u=x+z` weak energy **`uᵀKu=112c²`** all passed. Q1 original 8node source and receiver tensor factorizations passed. All true 3D generalized physical eigenmodes are included: PPW28/32/36/40/44 **37,835/53,001/75,504/102,949/132,341 = 401,630**, with no sliver floors, ghost deletions, modal cutoff or fitted wall reflection time.

Uniform-theory direct dispersion witness: for `kh=.4` the new half-mass eigenvalue over the exact wave value is **0.999892664** (0.0107% error), vs lumped **0.986737575** and consistent **1.013403257**. Thus the theoretical leading error cancellation was confirmed independent of the room data. This does *not* mean the true original broadband room impulse will converge.

## TRUE complete 250ms ORIGINAL q0 signed 40/80Hz five-grid results

All actual real raw native HDF5 original source and observation data, all modes, all full original 250ms P_T/Q_T and unfitted signed real/imag coefficients were used.

| Actual original PPW pair | NEW theory 50:50 mass complex RMS relative | NEW max magnitude relative | NEW max phase | Frozen original three-gate verdict |
|---|---:|---:|---:|---|
| 28→32 | **0.565737** | **0.792534** | **26.997°** | **FAIL** |
| 32→36 | **0.458917** | **0.856872** | **13.014°** | **FAIL** |
| 36→40 | **0.407076** | **0.533643** | **15.218°** | **FAIL** |
| 40→44 | **1.255632** | **60.530920** | **155.039°** | **FAIL** |

**ALL four original three-gate comparisons FAIL.** Three-metric strict monotonicity fails. Particularly, PPW40→44 shows an unfavorable near-zero signed 40Hz response and amplitude ratio singularity, giving max mag relative **60.530920**. The previous pure positive row-sum lump Q1 gave last pair **0.186295/0.316710/8.359°**, which was also overall FAIL but substantially better in two individual metrics; the theory-half blend demonstrably *worsens* this case. Previously full consistent Q1 last pair was **1.314187/11.659067/37.079°** and also FAIL. **No cherry-picked partial win or mass-fitted parameter allowed.** Original upstream real PFFDTD scores remain unchanged **1.246927/0.761305/0.367367/0.958742** (all FAIL).

The exactly prior frozen first physical Neumann sloped-roof reflection causal witness (center 8.966876703ms; support radius1.1ms; widths .35/.60/.85ms) was computed with the theoretical-half operator's **all-mode exact native implicit Newmark pressure**, not from a filtered wave. New native total roof-window / independent true 64-pair finite roof-image weak ratios for 0.35/0.60/0.85ms, PPW28→44 respectively:

| PPW | .35ms | .60ms | .85ms |
|---:|---:|---:|---:|
| 28 | **2.93770** | **0.84059** | **−0.12236** |
| 32 | **1.86647** | **−0.19685** | **−1.06852** |
| 36 | **0.38348** | **0.23627** | **0.20228** |
| 40 | **0.40002** | **0.01028** | **0.12360** |
| 44 | **0.29140** | **0.34544** | **0.70138** |

These **highly nonmonotone, sign-changing** responses do not support physical recovery of first roof reflection. The real native total roof-window waveform may include the dispersive direct-wave tail and other edge paths; these numbers must not be misrepresented as reflection coefficients. None of the causal short-time witnesses replaces/relaxes the original full 250ms signed three-gate criterion.

## Reproducibility / next engineering priority

- Prospectively pushed physical normal and fixed 50:50 acoustic solver plan: `benchmarks/acoustics/r130d_original_q0_oblique_roof_and_fixed_half_Q1_mass_plan_2026-10-09.json` (`d717e99`, BEFORE results).
- Real original native HDF5 roof adjacency audit and theory-mass exact cut-Q1 semidiscretization: `backend/src/htdt/r130d_oblique_roof_theory_blended_q1.py`.
- Original native SHA-sourced all-mode 5-grid Newmark q0 and frozen independent early roof replay: `scripts/run_r130d_original_q0_oblique_roof_theory_half_Q1.py`.
- Independent mathematical unit tests (exact tangent Neumann/axis mismatch, 1D fourth-order symbol without fitting, positive 56m³ mass/roof energy, all-mode 5-grid actual SHA controls, full 4-pair negative scores): `backend/tests/test_r130d_original_q0_oblique_roof_theory_half_Q1.py`.
- Full all-5-grid actual original boundary direction audit, original frozen source SHA, physical 40/80 complex real+imag, existing original/full-consistent/row-lump controls, all three new first-roof echo values and 1D dispersion theory: `benchmarks/acoustics/r130d_original_q0_oblique_roof_theory_half_Q1_evidence_2026-10-09.json`.

**Next**: a *genuine boundary-normal-consistent stable Neumann flux* on the true roof, verified on manufactured fields and earliest physical reflection plus the entire original broadband 250ms q0. Merely changing mass integration or canceling uniform-grid bulk dispersion does not resolve sloping-boundary full-record point impulse failure. The previous true weak Q1 boundary has already been implemented, but no experimented full geometry/time/source-coupled solver meets all three gates on all PPW.

**Canonical upstream PFFDTD q0: SELF_CONVERGENCE_FAILED. Independent BRAS/MFEM physical validation: NOT_VALIDATED. Product: NO_GO. PR #1055 remains Draft OPEN, Issue #938 remains OPEN.** Calculations/tests local, no new upstream PFFDTD native simulation, no manual GitHub Actions, `scratch/` retained.
