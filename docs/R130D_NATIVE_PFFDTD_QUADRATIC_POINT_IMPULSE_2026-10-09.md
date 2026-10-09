# R130D: Actual native PFFDTD five-grid quadratic-discrete-**point** q0 impulse test (FAIL)

2026-10-09 JST — Issue #938 / Draft PR #1055

## Prospective immutable experiment

The experiment definition was written and committed **before numerical wave observation**, commit `1bdd11baf1d8e6b4e8afa9abdd163ea81db0b595`, in [the plan](../benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_plan_2026-10-09.json). This is an **actual pinned upstream PFFDTD FDTD-engine integration**, unlike FV/MFEM stand-in evidence.

* Upstream `bsxfun/pffdtd` exact commit `aa319f6c86517cb95aabfae8656277da62c3ead5`, only the exact three previously audited Python 3.12 compatibility shims, no patched numerical update kernels. Original rigid sloped room and **same original native boundary masks**, sound speed 343.2 m/s, density 1.2 kg/m³, source [1.5,2,2] m, receiver [2.5,2,2] m.
* Frozen 5 raw native PFFDTD setups PPW28,32,36,40,44 from the *independent high-PPW diagnostic's original 8-node wave runs*. The original reference record SHA-256 is `9a0c7c4cf6080fdd75ce42a4e4a4a08256973b1d8c73724974073782dd9aeb34`; each original `comms_out.h5` is separately frozen by physical SHA-256. The original 8-node transfer `P_T/Q_T` was **actually recomputed from the original recorded `sim_outs.h5` potential traces and required to equal the recorded spectra before trying the new solver**.
* The **physical delta location** is unchanged. Each Cartesian axis uses the 3-point quadratic Lagrange weights around the nearest native grid node: `w[-1]=t(t−1)/2`, `w[0]=1−t²`, `w[1]=t(t+1)/2`, where `t=(physical_point-nearest_node)/grid_h`. 3D tensor product yields 27 signed source/receiver nodes, and the weighted point functional exactly reproduces all multivariate polynomials of degree ≤2. Each axis has zero first and **zero second central moment** with constant sum 1. All nodes are required to lie inside the air room and outside wall and absorbing masks; no clipping or boundary repair.
* The *original exact native impulse time contract* remains `q[0]=1, q[n>0]=0`, with PFFDTD's **unmodified** per-grid total native source scale `c² Ts² / h³`. The original source signal `diff=0`, no smoothing, no taper, no fitted amplitude/phase/frequency/temporal change. Each grid's native time step and sample count are unmodified; all 40 Hz and 80 Hz pressure `P_T/Q_T` scores use the native original 0.25s finite record, same second-order pressure derivative and same source Fourier convention.
* The experimental comms dataset lives only in a **fresh copy** of each originally frozen PFFDTD setup; `in_ixyz`, `out_ixyz`, `in_sigs`, `out_alpha`, `out_reorder`, `Ns`, and `Nr` describe its true new 27-node source and receiver. Previous original `sim_outs.h5` is removed from the copy before run to prevent any stale 8-node output from masquerading as Q2 evidence. The untouched original upstream `SimEngine` actually loads `Ns=Nr=27`, performs all time updates and saves raw output. Source/receiver integration values and signed source weights are saved; 13/27 support weights are negative per point for all grids.

## Observed native PFFDTD 40/80 Hz numerical outcome

| Adjacent PPW | Frozen original 8-node complex RMS relative | New 27-node **point** complex RMS relative | Q2 maximum magnitude relative | Q2 maximum phase (degrees) |
|---|---:|---:|---:|---:|
| 28→32 | 1.246927 | **2.437516** | 7.404772 | 84.5925 |
| 32→36 | 0.761305 | **0.854732** | 0.798470 | 58.1144 |
| 36→40 | 0.367367 | **2.825434** | 1.821254 | 165.3594 |
| 40→44 | 0.958743 | **0.612176** | 0.573332 | 29.0436 |

Original frozen PFFDTD phase/magnitude/complex acceptance thresholds: `complex RMS <=0.20`, `max relative magnitude <=0.25`, `max phase <=15 degrees` at the last adjacent pair, with strictly decreasing error in **all three** metrics across subsequent adjacent pairs. All four Q2 adjacent pairs **FAIL**, including the final 40→44. The Q2 series is also decisively nonmonotonic. **Numerical point interpolation with zero second moments does not fix PFFDTD original impulse nonconvergence.**

The signed 40 Hz/80 Hz real/imaginary (P_T/Q_T) outcomes for every level, original unchanged 8-node controls, exact distinct comms SHA-256 and raw output SHA-256, geometry SHA-256, native time steps/number of iterations, true first/second source and receiver moments, signed absolute-weight sums and complete unfavourable magnitude/phase deviations are retained in [the full evidence JSON](../benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json). Every Q2 native q0 impulse integration completed. A second independent replay from the same SHA-pinned original source assets checks reproducibility; no pass threshold is adjusted after observing any results.

## What this establishes and what it does not

The source/receiver 8-node trilinear stencil has a nonzero, grid-dependent second moment and a nonmonotonic source physical RMS width. However, enforcing exact polynomial degree-two reproduction **does not remove** the point-source convergence failure in the pinned native PFFDTD solver. Q2 has signed weights: the absolute source-weight sum ranges **1.3543–1.8415**, and the receiver absolute-weight sum **1.4442–1.8736** (compared to 1.0 for positive trilinear weights), possibly increasing unresolved high-mode coupling; this is a *plausible mechanism, not a proven unique cause*. The grid's staircase rigid-boundary geometry, arbitrary-point singularity, near-resonance finite-time transfer, time sampling, and end-of-record modes remain coupled and must be investigated with further prospectively frozen controls.

This experiment **does not change or upgrade the canonical eight-node production model**, does not re-run the original PPW8/10/12 approval series under any reinterpreted threshold, does not validate general CAD, does not provide independent physical room measurements. **Original point-to-point = SELF_CONVERGENCE_FAILED; physical NOT_VALIDATED; product NO_GO. PR #1055 remains Draft, Issue #938 OPEN.**


## Independent actual native wave replay and CI scope

The complete five-level numerical experiment was independently re-executed from the same hash-checked original 8-node PFFDTD source/setup assets, in a **different fresh set of copied setup folders**, after strengthening the fail-closed requirement that the variant folder must first remove the old native `sim_outs.h5`. For all PPW28/32/36/40/44: **the new 27-node native PFFDTD `sim_outs.h5` raw file SHA-256 matched exactly**, all ten signed 40/80 Hz complex values matched exactly, the original eight-node controls and full adjacent metrics matched exactly. Unfavourable results were the same in both actual runs; timings may differ. Both complete records are retained as [actual wave evidence](../benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json) and [independent five-wave replay](../benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_independent_replay_2026-10-09.json).

The focused Windows regression includes synthetic quadratic-polynomial reproduction, negative-weight support, fail-closed mask and geometry checks, source normalization, all native raw-output hashes, numerical transfer agreement between the two executed recordings, full 40/80 Hz metrics and unchanged NO_GO status. The dedicated GitHub workflow `r130d-native-quadratic-point-provenance.yml` independently **re-evaluates the stored mathematical evidence and both actual native-run fingerprints**. It does **not** perform a new on-runner, full PFFDTD integration because the preexisting frozen high-PPW original setup HDF5 data are not repository artifacts. CI success for this workflow must be called *evidence integrity success*, not "GitHub reexecuted all native waves".
