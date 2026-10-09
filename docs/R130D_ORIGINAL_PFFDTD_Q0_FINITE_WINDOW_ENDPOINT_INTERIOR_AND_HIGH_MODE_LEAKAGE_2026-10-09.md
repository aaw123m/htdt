# Issue #938 — ORIGINAL PFFDTD original q[0]=1 finite-record pressure, endpoints and broadband mode alias/leakage: exact 5-grid numerical proof

Date 2026-10-09 JST. PR #1055 **Draft**; Issue #938 **OPEN**. Original PFFDTD remains **SELF_CONVERGENCE_FAILED**.

## Exact prospective frozen control and what this **does not** change

This analysis was preregistered, committed and pushed at **`3a7148f969d955613a238e67bd8594090a41c737` before computing the endpoint attribution**. Original pinned upstream PFFDTD SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`, five real native PPW28/32/36/40/44 comms/voxels/sim_outs SHA-256 checks, the **original** source (1.5,2,2)m and receiver (2.5,2,2)m original **eight-node** trilinear weights, `q[0]=1, q[n>0]=0`, native Ts/Nt and native 6-neighbor staircase rigid Neumann, 250 ms rectangular record and the **same signed 40Hz/80Hz** P_T/Q_T and frozen 0.20 / 0.25 / 15° qualification limits are all held unchanged. No new PFFDTD waves, no Gaussian or smooth pulse, modal cutoff, damping, taper, altered pressure derivative, or output masking. **This is an exact mathematical decomposition of the same native solver output, not an alternative filtered transfer.**

The original real room graph is proven to factor exactly as `Ax⊗I + I⊗Ayz` on every grid and the eight-node source/receiver factors exactly; all **347,154** original 3D native eigenmodes are included. We use their true original leapfrog angle `theta=2 asin(sqrt(l2*lambda)/2)` and exact original q0 amplitude `A=sum(in_sigs[:,0])*unchanged_source_receiver_modal_overlap`. The resulting discrete velocity potential is `phi_n=A sin(n theta)/sin(theta)`, including exact Neumann zero-mode continuation. No temporal model approximation is added.

For the original **existing** sample-space pressure estimator, the finite N-sample complex DFT at either fixed original scored frequency splits into exactly three additive, complex signed contributions (mode by mode):

- Start `n=0`: `rho*A/dt*(2-cos(theta))` (original second-order one-sided forward velocity-potential derivative).
- Interior `n=1..N-2`: `rho*A/dt*sum[n=1..N-2] exp(i*omega_bin*dt*n)*cos(n*theta)` (original centered pressure derivative).
- End `n=N-1`: `rho*A/dt*exp(i*omega_bin*dt*(N-1))*(R(N-1)+(cos(theta)-2)*R(N-2))`, `R(n)=sin(n*theta)/sin(theta)` (original second-order one-sided backward derivative).

The full saved 250 ms signed original physical transfer **must equal** the signed sum of all three terms across all modes. Additionally each term is independently split into the five true original *semidiscrete* frequency bands 0–100,100–200,200–400,400–800, and ≥800 Hz. Per-grid five-band signed sums are compared to the **previous** original full-mode HDF5-controlled evidence, not to new fit parameters.

## Independent real numerical reconstruction

| ORIGINAL original native grid | Complete original 3D modes | Relative exact decomposition vs raw frozen original PFFDTD 40/80Hz transfer | Three time regions vs prior exact full-mode temporal transfer |
|---|---:|---:|---:|
| PPW28 | **31,185** | **1.2321e−11** | **4.85e−16** |
| PPW32 | **44,659** | **1.4840e−13** | **1.62e−15** |
| PPW36 | **64,848** | **2.4816e−11** | **3.38e−16** |
| PPW40 | **89,723** | **2.5254e−11** | **7.16e−16** |
| PPW44 | **116,739** | **2.0509e−11** | **2.30e−15** |

The new three-term decomposition is also **independently unit-tested** on multiple small synthetic native leapfrog modal traces: explicitly form all pressure samples using the actual forward, centered, and backward second-order finite differences, then direct DFT the exact samples and verify each analytical term separately (including Neumann zero mode, different Nt, nonzero omega). These tests prove the endpoint attribution formula itself, not only its grand sum, against direct discrete pressure data.

## Measured signed original native adjacent-grid drift

The table gives each signed temporal component's **two-bin complex difference L2 norm divided by the original complete two-bin coarse–fine difference norm**. Signed temporal components **cancel**, so individual ratios may exceed 1 or sum to more/less than 1. They are **not percentages of physical error**.

| Original PPW comparison | Unmodified original two-bin complex relative | First forward sample contribution norm ratio | Interior contribution norm ratio | Last backward sample contribution norm ratio | ≥800 Hz full-time high-mode band norm ratio |
|---|---:|---:|---:|---:|---:|
| 28→32 | **1.246927** | ~2.8e−14 | 0.806870 | 0.511083 | **0.242566** |
| 32→36 | **0.761305** | ~1.0e−13 | 0.641850 | 0.623220 | **0.540583** |
| 36→40 | **0.367367** | ~1.9e−13 | **1.757108** | **2.102888** | **1.771040** |
| 40→44 | **0.958742** | ~1.2e−12 | **1.107946** | **0.133310** | **0.735842** |

The **full** original acceptance also fails relative magnitude and/or phase (see saved complete two-bin signed/per-bin metrics). An almost vanishing aggregated start sample is expected for the source and receiver being physically distinct and the strictly local 8-point original excitation; it is **not** evidence that any individual high-frequency band of that sample is zero (its signed mode components cancel precisely). The end sample, on the other hand, contributes **2.103×** the *full original signed drift norm* at 36→40 due to cancellation with the interior, but only **0.133×** at 40→44. Therefore **neither removing/replacing a final-sample derivative alone nor blaming only a one-sided endpoint stencil can explain or cure the entire four-pair original nonconvergence**. Removing the endpoint would itself violate the frozen original full 250ms transfer and is NOT a proposed qualification hack.

The actual ≥800Hz original source-excited native modes contribute signed coarse-fine differences of **1.771×** the full original coarse-fine difference at 36→40 and **0.736×** at 40→44. This is real *finite-record broadband impulse leakage and cancellation*, not high-frequency sensor noise to omit: original q0 populates these modes. Their contributions are separately tabulated for **all three time regions and both signed 40/80 Hz bins**, and checked to recombine exactly. At 40→44 the **interior** dominates the overall difference (1.108×), with >800Hz band contributing 0.736× overall; the end derivative is substantially smaller, precluding an endpoint-only 'fix'.

## Next engineering implications

The original native PFFDTD is numerically stable and exactly reconstructible with its original graph, but that is distinct from **asymptotic self-convergence of broadband singular point-source/point-receiver finite-record transfer**. Proposed future solver changes require a well-posed continuum target and jointly consistent point forcing, physical sloped Neumann boundary, sample-time integration and receiver/pressure operator. A space-only formal fourth-order correction, window truncation, modal filtering, relative-score mask, or swapping an endpoint stencil cannot legitimately requalify the original native test.

**All unfavorable values and physical signs remain retained** in [the full 5-grid 3-time × 5-band native evidence JSON](../benchmarks/acoustics/r130d_original_q0_finite_window_endpoint_leakage_evidence_2026-10-09.json). Exact rerun: `scripts/run_r130d_original_q0_finite_window_endpoint_leakage.py`; fail-closed direct-time synthetic and five-real-grid original HDF5 regression: `backend/tests/test_r130d_original_q0_finite_window_endpoint_leakage.py`.

**Canonical original q0 = SELF_CONVERGENCE_FAILED. Independent BRAS/MFEM/owned-room = NOT_VALIDATED. Product NO_GO. PR Draft. Issue OPEN.** No GitHub Actions dispatched.
