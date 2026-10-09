# R130D #938 — exact causal semidiscrete time integration of ORIGINAL native PFFDTD point q0: both new methods fail five-grid refinement

2026-10-09 JST. Branch `feat/r130d-embedded-neumann-fv-20261009`; Draft PR #1055, Issue #938 OPEN.

## Prospective numerical modification and frozen original authority

The three-arm test plan was **committed and pushed before computing any candidate full five-grid results**, at `05760388063313e4e5b0bfa4df5d6318456b99a0` (with `[skip ci]`). The original pinned upstream PFFDTD SHA is `aa319f6c86517cb95aabfae8656277da62c3ead5`. Exactly the original unmodified SHA-checked raw five PPW28/32/36/40/44 native HDF5 8node q[0]=1 source and receiver, physical source (1.5,2,2)m, receiver (2.5,2,2)m, six-neighbor original PFFDTD stair-stepped rigid Neumann graph, original h and native Ts/Nt per grid, original complete finite 250ms record, 40 and 80Hz signed pressure P_T/Q_T and original frozen 0.20 complex / 0.25 relative magnitude / 15° phase acceptance are used. All **347,154 actual original 3D graph eigenmodes** were retained. No high-frequency cut, damping, artificial source smoothing, Gaussian, pressure taper, point displacement, frequency deletion, or post hoc coefficient fit.

Unlike earlier **exact-roof cutcell FV** experiments, this changes only the numerical **time propagator and the mathematical interpretation of the single native q0 acceleration sample**, keeping the **original actual PFFDTD staircase Neumann spatial operator** and original point weights. Experimental methods are not claimed to reproduce the unmodified original PFFDTD time integrator; the original native leapfrog is separately retained and SHA-checked as the canonical baseline.

Let original semidiscrete graph frequencies be `omega_m=(c/h)*sqrt(lambda_m)`, `theta_m=omega_m*dt`, and `A_m=sum(original in_sigs[:,0])*unchanged true 8node source_receiver modal overlap`, including all original high-frequency and Neumann zero modes. Both candidate source interpretations are **causal**:

1. **Original native leapfrog baseline**: native discrete leapfrog angle `theta_L=2asin(sqrt(l2*lambda_m)/2)`, `phi_m[n]=A_m sin(n theta_L)/sin(theta_L)`, exact original full 250ms second-order pressure derivative and finite DFT.
2. **Exact semidiscrete velocity impulse**: instant velocity jump `dot phi_m(0+)=A_m/dt`; `phi_m[n]=A_m sin(n theta_m)/theta_m` for `n>=0`. This mathematically exactly integrates the homogeneous semidiscrete eigen-oscillator after the q0 impulse, with the same original modal coefficient scale and analytically regularized Neumann zero mode.
3. **Exact causal one-native-sample constant force**: acceleration `A_m/dt²` strictly held on `0<=t<dt` for original q0=1 and zero after; `phi_m[0]=0`, `phi_m[n]=A_m[cos((n-1)theta_m)-cos(n theta_m)]/theta_m²` for integer `n>=1`. This exactly solves the forced semidiscrete oscillator over the first original time sample and its source-free propagation thereafter, with removable zero-mode limit `phi_m[n]=A_m*(n-1/2)`. This one-sample hold is a **piecewise constant quadrature interpretation**, **not** the original leapfrog q0 forcing operator.

All three arms apply the **same** ORIGINAL PFFDTD finite sample pressure observation: original second-order forward derivative at n=0, central at n=1..Nt-2 and backward at n=Nt-1, then original signed full time record 40/80Hz frequency sum. No extra samples scored. Both new time schemes implement exact **O(all modes)** full-record closed-form transforms, with numerically stable sinc limits; exact one-sample-hold start n0/n1 causality is handled explicitly. Independently generated oscillator solutions from exact force evolution u1/v1 or direct velocity impulse are tested sample by sample, then direct difference pressure and direct frequency sums, at three distinct Nt/time steps and several frequencies/eigenangles including zero mode. This is a genuinely different temporal numerical solver, not speculative frequency rescaling.

## Actual all-original-graph 250ms point q0 signed results

Two-bin signed complex RMS relative across all original neighboring refinement levels:

| True original PFFDTD graph + frozen 8node q0 | PPW28→32 | PPW32→36 | PPW36→40 | PPW40→44 | Full gates/monotone |
|---|---:|---:|---:|---:|---|
| **Original upstream leapfrog (canonical)** | **1.246927** | **0.761305** | **0.367367** | **0.958742** | FAIL / NO |
| **New exact semidiscrete instantaneous velocity impulse** | **0.972166** | **0.498118** | **0.465488** | **0.529270** | **FAIL / NO** |
| **New exact semidiscrete one-sample constant force** | **0.979563** | **0.554831** | **0.997965** | **0.782810** | **FAIL / NO** |

All three methods fail the fixed complete original 40/80Hz complex relative **0.20** threshold on **every adjacent pair**; amplitude and phase are also not simultaneously within their original thresholds.

For the exact velocity impulse, adverse full metrics by adjacent pair [complex relative / maximum relative magnitude / maximum phase] are:
- 28→32: `0.972166 / 0.618958 / 71.147°`
- 32→36: `0.498118 / 0.179439 / 30.875°`
- 36→40: `0.465488 / 0.521537 / 25.573°`
- 40→44: `0.529270 / 0.411340 / 30.557°`.

For exact one-sample force hold:
- 28→32: `0.979563 / 0.617980 / 71.830°`
- 32→36: `0.554831 / 0.302377 / 32.654°`
- 36→40: `0.997965 / 0.925836 / 126.433°`
- 40→44: `0.782810 / 0.826261 / 138.573°`.

The instantaneous impulse model reduces 40→44 signed complex error from 0.959 to 0.529, but increases 36→40 relative error from 0.367 to 0.465, while violating all three 40→44 gates. The physically plausible exact one-sample force holds worsen 36→40 drastically, from 0.367 to 0.998, and 40→44 retains a **138.573°** maximum phase discrepancy. Convergence is not monotonic; no metric-only/pair-only selection or masked score is permitted.

The unmodified native leapfrog branch exactly reproduces archived *real original* SHA-pinned PFFDTD signed waves to complex relative `1.23e-11 / 1.49e-13 / 2.48e-11 / 2.53e-11 / 2.05e-11` (PPW28/32/36/40/44). Every raw original/candidate 40Hz and 80Hz signed real/imag transfer, all three adverse metrics per bin, all original SHA-256 control hashes and all unfavorable values are stored in the complete [evidence JSON](../benchmarks/acoustics/r130d_original_q0_exact_semidiscrete_forcing_evidence_2026-10-09.json).

Numerical implementation: `backend/src/htdt/r130d_exact_semidiscrete_q0.py`. Actual true original-HDF5 all-mode numerical runner: `scripts/run_r130d_original_q0_exact_semidiscrete_forcing.py`. Fail-closed independent oscillator and original real-wave/score verification: `backend/tests/test_r130d_original_q0_exact_semidiscrete_forcing.py`. [Pre-observation plan](../benchmarks/acoustics/r130d_original_q0_exact_semidiscrete_forcing_plan_2026-10-09.json).

## Root cause and qualification boundary

Exact integration of each causal half-discrete oscillator **is not sufficient** when the original spatial stair-step Neumann geometry, finite-record singular broadband point impulse, eight-point source/receiver, and finite pressure output are fixed. Discrete temporal dispersion and one-sample q0 forcing interpretation affect signed results but do **not** explain all remaining large refinement drift: the spatial model, broadband source/receiver and 250ms rectangular causal measurement must be treated together with a defined continuum target and independently verified physical reference. Neither causal candidate is the **original** native leapfrog solver; even perfect self-convergence of an experimental alternative would not silently requalify native run25/run76 PPW8/10/12 or independent BRAS room/MFEM data.

**Canonical original native PFFDTD q0 = SELF_CONVERGENCE_FAILED; independent physical = NOT_VALIDATED; product = NO_GO; PR #1055 Draft OPEN; Issue #938 OPEN.** All checks performed locally, [skip ci], no GitHub Actions manually launched; scratch/ retained.
