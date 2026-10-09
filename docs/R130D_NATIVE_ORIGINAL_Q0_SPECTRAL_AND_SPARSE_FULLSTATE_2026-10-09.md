# R130D #938: Original native PFFDTD q0 spectrum and an independent, fully untruncated CSR wave solver

2026-10-09 JST — Draft PR #1055. **All computations were run locally. No new GitHub Actions workflow was created or dispatched.**

## Objective and frozen pre-observation plans

Two experiments operate on **the exact original production-like PFFDTD rigid sloped geometry, original eight-node source and receiver, original 0.25-second discrete temporal impulse q[0]=1 and q[n>0]=0, and both original physical 40/80 Hz frequencies**. Original PFFDTD source pin: `aa319f6c86517cb95aabfae8656277da62c3ead5`. Each case is bound to the exact originally recorded `vox_out.h5`, `comms_out.h5` and `sim_outs.h5` HDF5 inputs by SHA-256 from earlier original-run evidence. No new room voxelizer, source smoothing, temporal window, amplitude normalization, physical boundary or acceptance criteria.

* [Prospective native 20-eigenpair plan](../benchmarks/acoustics/r130d_original_native_modal_drift_plan_2026-10-09.json) committed as `61ddc7781282b5379a4680831419c8571518bab9` before observing any eigenvalues. Five source-connected original PFFDTD Neumann graphs PPW28,32,36,40,44; original full C-ordered Cartesian rigid boundary adjacency from SHA-pinned voxels, native room node counts 31,185 to 116,739, native 7-point graph Neumann A = degree minus adjacency. `scipy.sparse.linalg.eigsh(A,k=20,which='SM',tol=1e-9,maxiter=6000,ncv=60)`; every eigenpair residual explicitly measured and retained, including zero mode. Continuous-time semidiscrete eigenfrequency f = (c/2πh) sqrt(λ); **actual native Leapfrog** modified eigenfrequency f = asin(sqrt(l² λ)/2)/(π Ts). True original 8-node spatial source and receiver projected onto normalized actual native eigenvectors. Reconstructed the original **finite 0.25s q0 impulse**, native input scaling, native pre-step output sample timing and native second-order physical-pressure derivative separately for each eigenpair; saved every signed 40/80 Hz complex modal response and sum. Exactly 20 modes are retained, not an approximation silently treated as the full-band solution.
* [Prospective native full-state sparse integration plan](../benchmarks/acoustics/r130d_original_native_sparse_fullstate_independent_plan_2026-10-09.json) committed as `0b34f6d880c99b559c2ea21140e19e0559ba1dbe` before observing any independent full-state wave traces. Separately build actual original SHA-pinned Neumann graph as a SciPy sparse CSR matrix. Execute an independent time loop **for ALL source-connected room degrees of freedom, ALL original Nt samples, and ALL five native PPW**, no eigenmode truncation. Native physical q0 original source is added after the update (original PFFDTD `SimEngine` source timing). Compare every sample of **all eight raw receiver-node traces** against the original immutable PFFDTD raw HDF5 output, independently recombine using the original out_alpha, derive pressure using original `p=rho*d(phi)/dt`, then compare both signed full-window 40/80 Hz `P_T/Q_T` values. Do not invoke PFFDTD's upstream Numba stencil/time update kernels in this independent solver.
* For the autonomous sparse solver, check homogeneous **discrete native energy** `E_n = ||u_n-u_{n-1}||²/l² + u_n^T A u_{n-1}` at sample indices 1, Nt/4, Nt/2, Nt-1 after the q0 source has ceased. Original preregistered pass thresholds require every raw receiver-node trace, recombined receiver and complex transfer relative L2 ≤1e-8, with relative energy drift ≤1e-7. The physical original convergence limits remain **unchanged** and this independent identity test does not qualify continuum behavior.

## A. Actual original room modified-eigenfrequency results

| Original PPW | Nearest mode to 40 Hz | Nearest mode to 80 Hz | Relative error: 20-mode q0 finite-time complex transfer vs native full original |
|---:|---:|---:|---:|
| 28 | 41.32182 Hz | 77.92827 Hz | **0.587536** |
| 32 | 42.08201 Hz | 78.93313 Hz | **0.136026** |
| 36 | 41.75097 Hz | 78.47132 Hz | **0.087669** |
| 40 | 41.55142 Hz | 78.41600 Hz | **0.242517** |
| 44 | 42.03473 Hz | 78.83487 Hz | **0.541924** |

Nearest eigenmode to 40 Hz was sorted native mode index 1 and to 80 Hz was index 7 at all five PPWs. These frequencies are **NOT monotone** in PPW. All 100 actually measured true eigenvector residuals were ≤`5.64×10⁻¹¹`, substantially within prospective `5×10⁻⁷`; zero Neumann mode near 0 Hz was also preserved. The PPW28–44 variations near 40 Hz span roughly 0.76 Hz, and those near 80 Hz roughly 1.00 Hz; over a 0.25s record, such shifts can affect modal interference. This is a **plausible contributor**, not demonstrated unique causal explanation. Ordering modes by eigenvalue is only diagnostic when near-degenerate eigenvectors can rotate or swap.

The 20-low-mode approximation can miss **54%–59%** of the original full complex transfer's L2 magnitude (relative) on PPW28/44. Conversely its error is below 14% on PPW32/36. Therefore it would be erroneous to infer that just the first 20 modes explain original full-band nonconvergence. Preserve high modal tail contributions and all five PPW cases. [Complete original native 20-mode complex evidence](../benchmarks/acoustics/r130d_original_native_modal_drift_evidence_2026-10-09.json) stores each of the 100 actual eigenpairs: lambda, continuous-time and native discrete eigenfrequencies, eigenvector residual, signed point-source/receiver overlap, real and imaginary native q0 250ms modal pressures at BOTH bins, summed modal/full native transfer error, nearest modes and original graph SHA identities.

## B. Independent full-state alternative time integrator vs actual native original PFFDTD

| Original PPW | Entire original 8-node receiver raw records, relative L2 | Original signed 40/80 Hz complex transfer, relative L2 | Native discrete energy drift | Independent CSR time loop wall time |
|---:|---:|---:|---:|---:|
| 28 | 8.02e−15 | 3.44e−14 | 6.66e−16 | 0.21 s |
| 32 | 4.37e−15 | 8.24e−15 | 6.59e−16 | 0.48 s |
| 36 | 1.52e−14 | 1.26e−14 | 3.02e−16 | 1.17 s |
| 40 | 9.83e−15 | 7.63e−14 | 6.80e−16 | 2.14 s |
| 44 | 1.05e−14 | 1.13e−14 | 5.60e−16 | 2.99 s |

**Every source-connected original PPW full-state independent CSR integration met all prospectively frozen source/boundary/raw-wave/transfer/energy tolerances by many orders of magnitude.** The zero-input homogeneous discrete energy is preserved to numerical roundoff. In addition to saved full original and independent **complex** 40/80 Hz responses, [complete signed native sparse-wave evidence](../benchmarks/acoustics/r130d_original_native_sparse_fullstate_evidence_2026-10-09.json) retains original exact input/output HDF5 SHA-256, original 8-node source injection, room and matrix sizes, original native Nt and Ts, timing, raw eight-channel relative L2 and maximum absolute differences, recombined receiver error and time-local discrete energy probes.

This is a **different numerical implementation of the same native discrete model**, not an independent different physical discretization. Thus agreement verifies that the observed original native PFFDTD waveforms are internally consistent with the correct source timing and an energy-conserving symmetric Leapfrog update. It **rules against an implementation-level arithmetic or explicit-time-update bug as the main cause in these five source-connected rigid rooms**. It **does not rule out** nonconvergence of the *spatial model* itself: staircase rigid-wall Neumann approximation, the singular mathematical point source's growing high-frequency content, and finite 250ms modal interference remain.

## C. Decision and next remedial experiment

The original 27-node signed quadratic physical point stencil had already failed actual five-grid original q0 PFFDTD convergence; original native graph symmetry was then proved at all five levels; this new 20-mode and full-state independent work further rules out a trivial native code arithmetic/time-step defect. **There is no evidence-backed justification to patch the pinned Leapfrog or symmetrize the original Neumann graph.** Prospective next solver changes should target spatial geometric fidelity and/or mathematically justified singular-source observation at the canonical physical point, with *original source and acceptance frozen*; no smoothing/damping/shortening/full-band frequency removal to force an approval.

**Final and immutable qualification status: Original eight-node point-source q0 R130D = SELF_CONVERGENCE_FAILED; physical BRAS/owned-room = NOT_VALIDATED; product NO_GO; PR #1055 Draft; Issue #938 OPEN.** This work purposely introduces no new GitHub Actions workflow. All numerical evidence is locally reproducible from the original external five SHA-bound raw HDF5 setups; CI cannot truthfully re-run those full native setups without preserving those external assets. Plain unit/regression tests and an actual local independent solver run are the verification evidence.
