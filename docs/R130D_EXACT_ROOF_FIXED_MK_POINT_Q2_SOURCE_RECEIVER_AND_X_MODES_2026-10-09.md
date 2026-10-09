# R130D #938: unchanged physical point q0 — fixed-M/K source and receiver Q2 ablation, exact x Neumann modes

2026-10-09 JST. Draft PR #1055. These are actual original native-clock 250ms full-wave calculations of the **experimental exact-roof cut-volume FV** operator, not original production PFFDTD requalification. No Github Actions workflow, rerun or dispatch was needed.

## Pre-committed contract and source physics

The preregistered plan [r130d_native_exact_roof_one_sided_Q2_point_and_x_mode_plan_2026-10-09.json](../benchmarks/acoustics/r130d_native_exact_roof_one_sided_Q2_point_and_x_mode_plan_2026-10-09.json) was committed as `1528a153a654fd9dad9d84fe94baaf0e3783e950` BEFORE observing any numerical eigenfrequency or new wave result. All levels and cases are retained, including failures.

For the original native PPW40/44 grid coordinates and source HDF5 SHA-256, the mathematical point source stays (1.5,2,2) m, mathematical point receiver (2.5,2,2) m, rigid sloped room exact fluid volume 56m³, c=343.2m/s, rho=1.2kg/m³, **original q[0]=1 and all q[n>0]=0**, original native time steps and sample counts and complete untapered 250ms 40Hz and 80Hz signed `P_T/Q_T` response. Upstream PFFDTD pinned `aa319f6c86517cb95aabfae8656277da62c3ead5`. **No original source waveform or experimental spatial M/K is changed between arms.** Archived prior full native original PFFDTD SHA-pinned unmodified original 8-node source and receiver trilinear indices and weights are independently reconstructed at the actual original physical XYZ; no original PFFDTD solver or raw data is edited.

The **same** exact oblique planar roof cut-cell physical volumes and open-face Neumann stiffness from `backend/src/htdt/r130d_native_grid_exact_roof_fv.py` and the **same** original clock undamped implicit Newmark beta1/4 plus Jacobi-preconditioned CG are used in every source/receiver combination. Native source q0 and no taper are preserved. This is a controlled numerical point *functional* experiment, not a change to spatial material or room geometry.

The alternative numerical representation of the SAME mathematical point Dirac delta is tensor-product, cardinal Lagrange degree-two on the **nearest three original physical native grid nodes per axis** (3×3×3=27), with signed weights. Enforce zeroth, first AND second monomial moments of source and receiver point XYZ: `Σw=1, Σw x=x₀, Σw x²=x₀²` for each axis. The quadratic point stencil has negative weights and **is not a Gaussian smoothing or a physical broad source**. This alternative spatial delta/evaluation operator cannot be assumed superior for a broadband finite-grid impulse. Four arm labels: original `8/8`, `Q2/8` (source only), `8/Q2` (receiver only), `Q2/Q2`. For each PPW only TWO actual full-state source-driven simulations are required (source8 and sourceQ2), with BOTH 8- and Q2-receiver linear observations recorded from each wave at EVERY original native time sample: total FOUR newly executed full 250ms original-clock q0 waves. The original `8/8` point receiver complex full-wave baseline at each PPW is separately required to reproduce archived previous exact-roof original 8/8 controls to ≤5e−8 relative before any adverse variants are accepted.

## 1. x-direction planar-wall Neumann eigenmode under three mesh phases

The oblique roof does not depend on x, so the exact roof FV stiffness and diagonal mass permit an independent one-dimensional x Neumann analysis. For each PPW40/44 and predeclared x-grid offset −h/4, 0, +h/4, the 1D x-cell mass is the clipped interval width, shared-face stiffness c²/h and natural (zero normal-flux) endwalls are at physical x=0 and x=4. Solve the exact symmetric generalized FV eigenproblem via dense `scipy.linalg.eigh` for constant zero mode and first three positive modes. The continuum first x slab frequency is `c/(2*4)=42.900000 Hz`.

| PPW | offset x/h = −0.25 | 0 | +0.25 |
|---:|---:|---:|---:|
| 40 | 42.89175985 Hz | 42.89184398 Hz | 42.89199083 Hz |
| 44 | 42.89323458 Hz | 42.89333815 Hz | 42.89335620 Hz |

The original *physical x coordinates* of source x=1.5m and receiver x=2.5m remain fixed. The first x-mode signed product of source and receiver normalized eigenfunctions is stable and negative, approximately `−0.07315 to −0.07320`, across all six meshes; relative x-mode frequency shifts are tiny compared to the previously observed PPW44 source/receiver full complex transfer grid-phase changes up to `0.38`.

**Negative result:** translation of the computational grid does not materially shift the lowest x-direction Neumann eigenfrequency or its point source-to-receiver coupling. This x-only lowest mode therefore does not independently explain the dramatic 40Hz complex response shift. It **does not rule out** x higher modes and x/YZ coupled eigenmodes, and this 1D test must NOT be confused with a full 3D room eigenvalue solution.

## 2. ALL four original q0 point-source/receiver functional combinations: actual fixed M/K full waves

**Original full 250ms pressure transfer 40/80Hz PPW40→44 adjacent-grid metrics, including the original failed control:**

| Numerical SOURCE / RECEIVER point functional | 2-bin complex relative | max relative magnitude | max phase in degrees | Frozen limits 0.20 / 0.25 / 15° |
|---|---:|---:|---:|---|
| original trilinear 8 / 8 | **0.872666** | **3.207787** | **170.011°** | FAIL |
| quadratic Q2 27 / original 8 | **1.191198** | 1.069870 | **177.654°** | FAIL |
| original 8 / quadratic Q2 27 | **1.354609** | **4.862872** | **170.647°** | FAIL |
| quadratic Q2 27 / quadratic Q2 27 | **1.721050** | **1.991429** | **177.486°** | FAIL |

This is a genuine one-factor-at-a-time ablation: the spatial **same M/K**, source and receiver physical XYZ, original q0 and native sample clock, time integrator, energy, rigid room and full signed 40/80Hz tests remain fixed. The source-only Q2 and receiver-only Q2 each **WORSEN** the high-grid complex ratio versus original trilinear 8/8 baseline. This rules against blindly substituting a higher-order numerical point functional as the missing numerical solution.

**Within-PPW change in BOTH-bin signed complex P_T/Q_T compared against the same M/K original 8/8 control:**

| PPW | Q2 source only | Q2 receiver only | Q2 both |
|---:|---:|---:|---:|
| 40 | 0.097203 | **0.330663** | 0.481643 |
| 44 | **0.236838** | 0.030886 | 0.289594 |

Receiver-only observation at PPW40 changes the signed physical q0 response more than at PPW44, while source-only Q2 has a larger effect at PPW44 than at PPW40. This is consistent with asymmetric *discrete functional approximation* and different mode coupling; it is NOT an asymmetry of the underlying symmetric `M,K`, nor a proven unique source or receiver defect. Original eight-node point source and receiver on PPW40 were reproduced to relative difference `7.87e−15`, and PPW44 to `2.27e−14`, without modifying the frozen prior 8/8 actual full wave evidence.

Every CG max iterations ≤13, every true relative linear residual ≤8.88e−11, and discrete homogeneous source-free modified energy drift ≤9.62e−10. All signed 40/80Hz real/imag values for the **eight actual operator-at-grid observations**, all full original PPW sample counts and exact original HDF5 SHA hashes, polynomial delta moments, Q2 negative-weight counts, actual x Neumann modal frequencies/couplings and all FAIL gates are permanently retained in [the full experimental evidence JSON](../benchmarks/acoustics/r130d_native_exact_roof_one_sided_Q2_point_and_x_mode_evidence_2026-10-09.json). The algorithm and original source validation are implemented in [the executable original native-grid numerical experiment](../scripts/run_r130d_native_exact_roof_one_sided_Q2_and_x_mode.py), which is regression tested, including fail-closed preregistration, independent quadratic polynomial moments, x analytical frequency checks and complete saved signed spectra.

## Remaining numerical convergence work

1. **Original mathematical point q0 singularity**: demonstrate convergence against a genuinely independent spatial high-order reference at physically fixed source/receiver across additional held-out PPW and finite full time-window bins. Pure 8→27 signed functional substitution made the latest four-arm experiment WORSE.
2. **3D high-mode coupling**: isolate near-resonant yz roof modes and higher x harmonic contributions; use a matrix-free symmetric Neumann spectral method with source/receiver matched physical conditions. The x fundamental frequency is demonstrably stable; x alone is not enough.
3. **Cutcell mass/face distribution**: exact room volume and conservation are necessary but not sufficient. Earlier x-grid shift demonstrated finite point numerical grid phase effects even with 56m³ preserved, and the minimum sliver mass did not correlate monotonically.
4. **Acceptance**: Original PFFDTD fullband PPW8/10/12 q0 **SELF_CONVERGENCE_FAILED**, all of these exact roof PPW40/44 alternatives still FAIL frozen thresholds and are not a substitute for original physical BRAS/owned room measurements. No production upgrade or PR merge.

**Qualification: original point q0 SELF_CONVERGENCE_FAILED; physical NOT_VALIDATED; product NO_GO; Issue #938 OPEN; PR #1055 Draft. No new GitHub Actions run or workflow.**
