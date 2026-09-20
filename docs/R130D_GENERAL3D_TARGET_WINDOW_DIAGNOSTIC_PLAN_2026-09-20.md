# R130D general-3D finite-record sampling diagnostic — frozen plan

Issue: #101  
Predecessor: PR #286  
Task-start main: `c77b8b50d7e4f9706c08e64a1daa7a7a51e8ed96`

This plan is committed before the authoritative numerical run. The refinement series, frequencies, target duration, thresholds, and diagnostic observation operator are frozen before results are observed. The diagnostic cannot promote canonical validation state.

## Frozen parent authority

- Parent plan id: `r130d-independent-sloped-mfem-v2-coherent-window-2026-09-20`
- Parent plan SHA-256: `5c753073a88d6705ee2962aa387006d4c563f90f0249b722dcf309a8155f62ec`
- MFEM refinements: 1 / 2 / 3
- PFFDTD: 8 / 10 / 12 PPW
- Requested target duration: 0.25 s
- Scored frequencies: 40 / 80 Hz
- Rectangular/no taper
- PR #282/#286 acceptance thresholds unchanged

The machine-readable diagnostic plan is `benchmarks/acoustics/r130d_target_window_diagnostic_plan.json`. Its semantic SHA-256 over canonical sorted JSON is `632d7d18b348e387496477bfdbcc457df0d0c7a7f3148f35d11ece457ef0dfb9`.

## Frozen diagnostic observation operator

Operator: `htdt.r130d.target_window_zoh_exp_integral@1`.

For native samples `y[n]` at `t_n=n*dt`, the diagnostic interprets each sample as a zero-order hold on `[t_n,t_{n+1})`. The exact target interval is `[0,T)`. Every hold cell is clipped to `T`; no value at `T` is included. Each Fourier contribution is the native left sample multiplied by the analytic integral of `exp(+i*omega*t)` over the clipped cell. Pressure and source use the identical operator. The exact frequency grid is 40/80 Hz and the phasor convention remains `exp(-i*omega*t)`.

This differs from the canonical PR #286 extractor only as a diagnostic. Canonical results remain persisted and authoritative.

### Why linear/trapezoidal interpolation is not the frozen method

The established source authority is a discrete impulse: `q[0]=1`, `q[n>0]=0`. A piecewise-linear/trapezoidal reconstruction would half-weight the endpoint impulse and therefore redefine its area unless source semantics were also changed. That would confound the window diagnosis with a source-contract change. The zero-order-hold operator preserves the existing discrete source meaning while allowing the final native cell to be clipped exactly at the requested target duration. No result-dependent choice is permitted after this commit.

## Required machine-readable sampling record

For every MFEM and PFFDTD level the evidence must persist requested duration, native `dt`, generated sample count, first and last sample times, effective integration interval, endpoint convention, native `N*dt`, difference from requested duration, Fourier sign, rectangular weighting, source sampling, pressure sampling, and frequency evaluation rule.

## Independent analytic fixture

Before interpreting solver results, the observation operator must be checked against an analytic complex-harmonic finite-window fixture at multiple `dt`, including non-integer `T/dt`. The aligned operator must converge toward the analytic `P_T/Q_T` as `dt` decreases. This is an operator validation, not a solver validation.

## Decision semantics

The final evidence must keep separate:

- solver execution
- canonical observable contract
- diagnostic observation-operator validation
- canonical self-convergence
- aligned-diagnostic self-convergence
- cross-solver eligibility
- general-3D validation state

Even if aligned-diagnostic self-convergence passes, canonical `CROSS_SOLVER_BLOCKED` and `NOT_VALIDATED` remain unchanged unless a later separately approved slice formally changes the canonical observation contract.

RDC calls are expected to remain zero. HTDT-Capture is out of scope and must have zero diff.
