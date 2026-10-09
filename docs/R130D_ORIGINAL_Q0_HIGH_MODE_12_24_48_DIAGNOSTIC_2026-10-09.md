# R130D ORIGINAL q0 point impulse: 12 / 24 / 48 actually solved spatial modes

Date: 2026-10-09. Issue #938, draft PR #1055. The original temporal unit discrete impulse **q[0]=1, all later q=0** is maintained exactly, as are the point source and receiver operators. The purpose is *not* to filter or repair the canonical waveform but to localize higher-spatial-modal sensitivity of the finite-window transfer.

## Prior freeze and methodology

Before observing any numerical values, original 56 m³ sloped Neumann room, FV n20 (7200 DOF), independent pinned MFEM P2 r3 (4913 DOF), the first 12, 24, 48 actual generalized eigenmodes including physical zero mode, eigensolver tolerance and the true residual cap 1e-7, dt=250 μs / T=250 ms / both signed 40 and 80 Hz bins, and all source/receiver conventions were committed in [prospective plan](../benchmarks/acoustics/r130d_high_mode_count_original_impulse_plan_2026-10-09.json), commit `4bb6156c624d4e37c5ba3141a9d26a556170ef0e`.

The original **actual full-state point impulse** is an external fixed control: [original archived evidence](../benchmarks/acoustics/r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json), SHA-256 `fa2a4aae2c6b00f4e1eccda9d0e9bf9271c420eb0a5f971b26927d9ed76e3cf2`. Independent P2 r3 mass/stiffness and source/receiver linear functionals were loaded from original pinned MFEM source matrix SHA-256 `6a43a624223fa512152059d103c4d841723fa1266ea8a22842322fb50db42511`; never replaced by FV operators. Both matrices solve 48 mass-normalized eigenpairs from `K x=lambda M x` and project the **same** point source and receiver. Each reduced mode is driven by the original 1000 midpoint-step discrete q0, and no time or frequency taper is applied.

## Actual original q0 to full-state complex finite-window differences

| Fixed spatial method / comparison | First 12 actual modes | First 24 modes | First 48 modes |
|---|---:|---:|---:|
| FV n20: modal vs same n20 full-state complex relative L2 | 0.131825 | 0.087682 | **0.131636** |
| Independent P2 MFEM r3: modal vs same r3 full-state complex relative L2 | 0.579904 | 0.451723 | 0.358732 |

**The FV metric is nonmonotone at 48 modes**, despite the 24-mode error being smaller than at 12 modes. This nonmonotonicity is retained and must not be suppressed. The MFEM r3 error improves with increasing included modes but is still **0.358732** at 48 modes. Thus 48 is not proven sufficient for original broadband transfer. None of these values is a numerical spatial mesh-self-convergence metric: each compares a modal truncation with its own full discretized system.

All **96 independently solved eigenpairs** passed the preregistered true relative eigen-residual ceiling \(10^{-7}\): observed maxima **3.633e−13 (FV)**, **5.772e−13 (P2)**. Actual solve times: FV 2.660s, P2 1.315s locally. All frequencies, eigenvalues, signed coupling weights, individual 40/80Hz complex modal observables, full-wave controls, frequency-specific magnitude and phase discrepancy, and raw residuals are retained in [complete evidence](../benchmarks/acoustics/r130d_high_mode_count_original_impulse_evidence_2026-10-09.json).

## Interpretation and authority

This establishes that a low-mode-only interpretation is insufficient; even using **48 actual modal pairs**, high-frequency modal truncation makes substantial and method-dependent differences to the finite-window original q0 response. The FV nonmonotonicity means that **more modes does not guarantee a monotonically smaller two-bin error**. No measurement was available to decide physical fidelity. It does not establish a unique causal defect in the original PFFDTD, nor the continuum limit of a delta-function point source.

Canonical original PFFDTD fullband **SELF_CONVERGENCE_FAILED**, independent full point-impulse candidate **NOT_QUALIFIED**, external BRAS/owned-room **NOT_VALIDATED**, product **NO_GO**, PR Draft and Issue #938 OPEN. The prospectively registered high-mode diagnostic is descriptive with **no new acceptance threshold** or original solver promotion.

Do not assert GitHub Actions replay success until the actual CI run completes.
