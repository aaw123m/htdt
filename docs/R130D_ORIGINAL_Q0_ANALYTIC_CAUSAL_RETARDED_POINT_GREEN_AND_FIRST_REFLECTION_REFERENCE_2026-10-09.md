# Issue #938 — Independently solved 3D causal continuum retarded point Green reference for the original PFFDTD 8-node q0 geometry

2026-10-09 JST; branch `feat/r130d-embedded-neumann-fv-20261009`; PR #1055 **Draft OPEN**, Issue #938 **OPEN**.

## Numerical research motivation and prospective controls

After true finite volume, exact causal temporal, roof-conforming P1 mass-lumped and fully consistent, variational weak point source/observer and true wet-domain Cartesian Q1 all failed the original full 250ms q0 multi-grid signed score, the next necessary *independent continuum physics anchor* is the retarded **point-source Green function**, not another numerical spectrum fit. This reference makes exact original eightpoint Cartesian source/receiver interpolation error empirically separable from full-room temporal propagation errors.

The plan was committed and pushed **BEFORE** evaluating any new analytic original point-q0 native-grid results as **`6cc3b1e638d259809f63c3e4b0bfedda54202fca`** (`[skip ci]`). The physically original upstream PFFDTD SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`, original real native SHA-pinned PPW28/32/36/40/44 HDF5 `comms_out.h5`, `vox_out.h5`, `sim_outs.h5`, physical source **(1.5,2,2)m**, receiver **(2.5,2,2)m**, **all original eight q[0]=1 source coefficients and all eight receiver coefficients**, original each native h/Ts/Nt, full original **250 ms** original full pressure time samples and both **SIGNED 40Hz and 80Hz** P_T/Q_T outputs were retained. Frozen original three-gate scores **complex ≤0.20 / magnitude ≤0.25 / phase ≤15°** were independently recomputed on all four original actual room pairs. No high-mode removal, time or frequency masking, source fitting/taper/smoothing, original PFFDTD runtime changes or GitHub Actions.

## Independent analytic continuum calculation, assumptions and physical domain of validity

In homogeneous unbounded **3D** air with sound speed 343.2 m/s, the scalar wave operator has the distributional causal Green function

`G(t,r)=δ(t-r/c)/(4πr)`, for **r>0**.

For the unchanged **positive** sign frequency convention `∫G(t,r) exp(+iωt)dt`, the EXACT signed continuum Green coefficient of a causal instant volume source is `G_+(ω,r)=exp(+iωr/c)/(4πr)`. For pressure equal to rho·∂φ/∂t, the corresponding full-time Fourier derivative is **`−iωrho G_+`**. The unknown continuum amplitude mapping from original PFFDTD's **discrete kick normalization** to a physical point volume-velocity distribution is *not fitted*: comparing original 8node analytic-vs-physical point **ratios** cancels the common unknown amplitude.

At every true original PPW grid, the native HDF5 point indices are converted to their original Cartesian **node coordinates** from its own SHA-pinned voxel axes. Exactly the *original* source weights `in_sigs[i,0]/sum(in_sigs[:,0])` and receiver weights `out_alpha[j]` are used to form all **64 causal Green source-to-receiver pair contributions**, with **both signed real and imaginary** coefficients:

`G_8(f)=Σ_{i=1}^{8}Σ_{j=1}^{8} s_i r_j exp(+i2πf |x_i−x_j|/c)/(4π|x_i−x_j|)`.

The independent *true physical point* direct continuum reference is `G_point(f)=exp(+i2πf/c)/(4π)` because **physical source-receiver separation = 1m**. Both frequency bins are evaluated; every actual original physical source and receiver spatial first moment is checked against its unmoved coordinate with numerical error ≤3e−10m. All per-PPW exact original native node indices, locations, individual source/receiver weights, SHA controls, and all 64-pair signed results are saved.

For rigid Neumann **FIRST reflections only**, the six *true finite planar room faces* x=0, x=4, y=0, y=4, z=0, and the exact sloped roof z=4−0.25y are analytically reflected by the image method. Each image-source to physical receiver segment must specularly intersect its **own finite room wall face**, rather than only the infinite plane. The independent point-source arrivals at original physical positions are:

- Direct causal arrival: **2.913752914 ms** (1m / 343.2m/s).
- First true specular roof reflection: **8.966876703 ms** (3.077432085m / 343.2m/s).
- x=0 and x=4 first images: **11.655011655 ms** each (4m).
- y=0, y=4 and z=0 first images: **12.013711030 ms** each (4.123106m).

Thus there is a mathematically identified **approximately 6.053 ms direct-path arrival interval** before the earliest true finite-wall first reflection for the *physical point*. The original eight native nodes have their own nearby geometrically distributed arrival times; each individual image reflection's valid finite-wall foot and all 64 original weight contributions are also explicitly calculated and stored.

**This analytic model is NOT the entire finite 250ms sloped Neumann room Green function.** In 250ms, multiple wall bounces, edge effects, modal reverberation and the initial singular-wave numerical representation matter; a six-first-image sum cannot replace the true 250ms room pressure. In particular **no direct-only window, first-arrival region, or truncated mode set is used to replace or relax the ORIGINAL 250ms acceptance**. The exact impulse point Green distribution must never be substituted for the full-room solution in production.

## Actual original q0 SHA HDF5 geometric-causal reference vs full-room numerical FAIL

The newly computed independently analytical causal **eight-node vs exact physical Dirac point direct-field relative errors** are:

| Actual original PPW | Original native 8node vs exact single true point, signed direct Green complex relative |
|---:|---:|
| 28 | **0.00352709** |
| 32 | **0.00314768** |
| 36 | **0.00382385** |
| 40 | **0.00187663** |
| 44 | **0.00185804** |

The exact original q0 original native HDF5 *eight-node spatial geometry alone* therefore gives approximately **0.19%–0.38% complex error** in the 40/80Hz direct continuum free-space field, without source fitting, smoothing or changes. It is not strictly monotone, because original physical points fall at different fractions of each native grid spacing.

**All four PPW-adjacent comparisons, frozen original 40/80 signed complex norm**:

| True original PPW pair | TRUE original upstream PFFDTD full 250ms rigid sloped-room q0 (canonical gate complex 0.20) | Independent ANALYTIC free-space original 8node direct-G geometric-only comparison (not an original room acceptance score) |
|---|---:|---:|
| 28→32 | **1.246927 FAIL** | **0.002940601** |
| 32→36 | **0.761305 FAIL** | **0.003884972** |
| 36→40 | **0.367367 FAIL** | **0.001954520** |
| 40→44 | **0.958742 FAIL** | **0.002185056** |

The original real upstream finite-250ms room PFFDTD q0 still FAILS all four original three-gate checks; no artificial reinterpretation as a physical pass or credit to a new FV/FEM arm. Original complete 347,154 Neumann modes across all five grids and original actual raw room wave archived comparisons remain unchanged and SHA-verified. The independent analytic direct-field geometric mismatch is **two to three orders of magnitude smaller than** the observed original room full-record intergrid drift. These are **different physical observables**, so this discrepancy is compelling evidence that *low-frequency free-field eight-node geometric interpolation error alone cannot account for the whole nonconvergence*, **not a proof** that broadband/high-frequency source coupling or finite-window physics has no contribution.

## Reproducibility and limitations

- Prospective plan: `benchmarks/acoustics/r130d_original_q0_continuum_retarded_green_plan_2026-10-09.json`.
- New analytic causal benchmark implementation: `backend/src/htdt/r130d_retarded_point_green.py`.
- Real original SHA-pinned 8node HDF5/actual full-room original 250ms signed checker: `scripts/run_r130d_original_q0_continuum_retarded_green.py`.
- New independent analytic/physical tests: `backend/tests/test_r130d_original_q0_continuum_retarded_green.py`. Tests independently verify analytic retarded sign convention, source↔receiver reciprocity, translation symmetry, six true-wall specular reflection image law and causal travel paths, Gaussian **test-function-only** distributional weak integral (NOT applied to any original q0 data), original 64-pair full SHA controls, all original 40/80 complex gates and immutable fail-closed three-score thresholds.
- Full signed real/imag per-PPW 8node original and true-point Green, first finite-wall reflections, original raw full room scored 250ms results, all source positions/weights/hashes and adverse score metrics: `benchmarks/acoustics/r130d_original_q0_continuum_retarded_green_evidence_2026-10-09.json`.

**Still missing for an actual complete physical convergence fix:** a mathematically convergent point Green function numerical discretization for a *closed* 56m³ three-dimensional sloped Neumann cavity with original original-q0 broadband impulse and exact original 250ms signed receiver functional, including multibounce/irregular boundary waves and an external owned-room/BRAS/MFEM physical target. Single-bounce continuum Green serves as an independently validated *local wavefront* physics standard, but cannot be passed off as full-room acceptance.

**Canonical original upstream PFFDTD q0 = SELF_CONVERGENCE_FAILED; independent physics = NOT_VALIDATED; product = NO_GO; PR #1055 Draft OPEN; Issue #938 OPEN.** No manual GitHub Actions, no new original native PFFDTD wave runs, `scratch/` retained.
