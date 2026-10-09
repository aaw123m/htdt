# R130D #938 — ORIGINAL PFFDTD q0 extended last-sample pressure derivative experimental solver A/B: ALL FIVE GRIDS FAIL

2026-10-09 JST. Draft PR #1055, Issue #938 remains OPEN; no GitHub Actions were manually dispatched.

## Strict prospective registration and one-step experimental implementation

The pre-observation plan was committed and **pushed before computing any altered-pressure outcomes** in `03683d53d6fe28f1f754890c36fe9a7c176313de`, based on an earlier exact original unmodified temporal-term attribution in `3a7148f`/`d8b3de9`. This is a **real new pressure-observation finite-difference operator experiment on the ORIGINAL upstream PFFDTD native graph**, not an exact-roof FV repair and **NOT** a requalification of the original PFFDTD's native pressure trace.

Five original PFFDTD PPW28/32/36/40/44 raw HDF5 source/voxel/outputs were SHA-256 checked against true archived originals, upstream SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`. Full original rigid stair-step 6-neighbor graph, **347,154 untruncated true original 3D modes** across 5 grids, native original source (1.5,2,2)m and receiver (2.5,2,2)m with native original 8-node source/receiver weights, original q[0]=1 with q[n>0]=0, native original PPW dependent Ts and Nt, 250ms **unchanged number of scored pressure samples**, 40 and 80Hz signed P_T/Q_T and frozen 0.20 complex / 0.25 magnitude / 15° phase gates are unchanged. **No Gaussian, damping, high-mode deletion, reweighting, taper, frequency deletion or target resetting**.

Three fixed arms, all with the exact original true leapfrog native wave trajectory `phi[n]=A sin(n theta)/sin theta`:
1. `original_one_sided_end`: original second-order forward pressure derivative n=0, original centered interior n=1..N−2, original second-order backward derivative n=N−1; the **only canonical pressure output**, exactly matching archived raw PFFDTD.
2. `extended_centered_end`: preserve original forward n=0 and all original centered interior, but propagate exactly **one extra homogeneous wave state `phi[N]`** AFTER the recorded 250ms interval solely to evaluate the previously recorded last pressure `p[N−1] = rho*(phi[N]−phi[N−2])/(2dt)`. Do NOT add N to the scored output and do NOT change source, room, time step, Nt or interior samples. This changes only the last pressure-sample observation stencil (not original canonical).
3. `hypothetical_all_centered`: previous, plus hypothetical centered first pressure derivative using `phi[-1]=-A`; this is **anti-causal at the q0 injection instant**, physically invalid, and must NEVER be claimed a production fix. It tests only original initial sample's numerical relevance.

The extra last-sample term is independently unit-tested by forming every original leapfrog modal velocity-potential sample including `phi[N]`, computing the actual 2nd-order one-sided and centered finite differences directly, and comparing the complex signed frequency-domain term at both native frequencies and several Nt, including the Neumann zero mode. All 347,154 original modes are retained.

## Full 250ms 8-point-q0 signed 40/80Hz original/experimental results

| ORIGINAL PFFDTD native grid pair | Original complex relative | Experimental centered-last complex relative | Centered-last max relative magnitude | Centered-last max phase |
|---|---:|---:|---:|---:|
| 28→32 | **1.246927** | **1.034286** | **0.546003** | **78.878°** |
| 32→36 | **0.761305** | **0.607005** | **0.177617** | **43.834°** |
| 36→40 | **0.367367** | **0.311328** | **0.413358** | **21.949°** |
| 40→44 | **0.958742** | **0.860760** | **0.295107** | **161.612°** |

All four adjusted-pair global complex relative errors improve, but **ALL FOUR STILL FAIL the original 0.20 complex acceptance**; each also fails magnitude and/or phase. At **32→36**, the adjusted magnitude error is below the original 0.25 limit, but complex/phase remain **0.607 / 43.834°**, so an amplitude-only PASS is strictly forbidden. At **40→44**, final-sample centering modestly improves complex and amplitude, but **worsens the phase** vs original **150.630°** to **161.612°**. None of three arms achieves all original gates or simultaneous monotonically decreasing errors across five grids.

The hypothetical anti-causal both-centered arm is numerically indistinguishable at the saved scored two bins from the centered-last arm: the original 8node source and receiver occupy distinct, nonadjacent physical support, so the n=0 start-sample pressure difference sums to approximately zero across the **complete** eigenbasis, even though individual modal contributions may be substantial and cancel. This confirms initial one-sided endpoint pressure is not the dominant original nonconvergence contributor; that is not license to use anti-causal pressure output.

The new true-original graph native full response preserves archived raw PFFDTD 250ms signed complex transfers with relative controls approximately **1.23e−11, 1.48e−13, 2.48e−11, 2.53e−11, 2.05e−11** per grid. Full exact complex pressure scores and **all adverse per-bin 40/80Hz magnitude/phase metrics** stored without rounding in [evidence JSON](../benchmarks/acoustics/r130d_original_q0_pressure_endpoint_extended_observer_evidence_2026-10-09.json). Runner: `scripts/run_r130d_original_q0_pressure_endpoint_extended_observer.py`; fail-closed direct-wave and saved real q0 regression: `backend/tests/test_r130d_original_q0_pressure_endpoint_extended_observer.py`. No new actual PFFDTD wave run; this is an exact replay of true original frozen native graphs and physical 8node q0 modal excitation.

## Conclusion

**One-step-extended centered last-sample pressure differentiation is a genuine implementable numerical-observation change**, reduces global complex 40/80Hz refinement errors in this limited experiment, and avoids cutting/discarding any true q0 modes. But it **does NOT solve the 5-grid original full q0 convergence failure**. Changing original pressure samples would itself be a solver/observable modification and cannot be retroactively counted as original run25/run76 qualification. Endpoints alone cannot address the 250ms interior, >800Hz broadband finite-window contributions and signed cancellation shown in the preceding unmodified source experiment.

**Canonical original PFFDTD q0: SELF_CONVERGENCE_FAILED; independent physical MFEM/BRAS: NOT_VALIDATED; product NO_GO; PR Draft OPEN; Issue OPEN.**
