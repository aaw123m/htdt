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

The machine-readable diagnostic plan is `benchmarks/acoustics/r130d_target_window_diagnostic_plan.json` with diagnostic id `r130d-general3d-target-window-clipped-left-rectangle-2026-09-20`. Its semantic SHA-256 over canonical sorted JSON is `ff42a7e0c44ed4726ea34edfa2549d018d66a37181786df18bbd4cf59c61d0db`.

## Frozen diagnostic observation operator

Operator: `htdt.r130d.target_window_clipped_left_rectangle@1`.

For native samples `y[n]` at `t_n=n*dt`, the diagnostic keeps the canonical direct-DTFT left-rectangle rule exactly: `y[n] * exp(+i*omega*t_n)`. The only change is the quadrature width: every complete cell keeps weight `dt`, while the final cell is clipped to `T-t_last`, so the total integration interval is exactly `[0,T)`. No value at `T` is included. Pressure and source use the identical operator. The exact frequency grid is 40/80 Hz and the phasor convention remains `exp(-i*omega*t)`.

This differs from the canonical PR #286 extractor only as a diagnostic. Canonical results remain persisted and authoritative.

### Why linear/trapezoidal interpolation is not the frozen method

The established source authority is a discrete impulse: `q[0]=1`, `q[n>0]=0`. A piecewise-linear/trapezoidal reconstruction would half-weight the endpoint impulse and therefore redefine its area unless source semantics were also changed. The clipped left-rectangle operator avoids that confound: the source impulse keeps its original full `dt` weight, and all canonical kernel/sample weights remain unchanged except the final record cell width. No result-dependent choice is permitted after this commit.

## Required machine-readable sampling record

For every MFEM and PFFDTD level the evidence must persist requested duration, native `dt`, generated sample count, first and last sample times, effective integration interval, endpoint convention, native `N*dt`, difference from requested duration, Fourier sign, rectangular weighting, source sampling, pressure sampling, and frequency evaluation rule.

## Independent analytic fixture

Before interpreting solver results, the observation operator must be checked against the frozen `complex-harmonic-pressure-source-v1` fixture: pressure amplitude `2.1+0.4i` at 53 Hz, source amplitude `0.7-0.2i` at 17 Hz, and `dt = 0.007 / 0.0035 / 0.00175 / 0.000875 s`. Every case has non-integer `T/dt`. Relative `P_T/Q_T` error must strictly decrease at each refinement and the finest error must be `< 0.11`. This is an operator validation, not a solver validation.

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


## Frozen PFFDTD non-monotonicity classification

Using adjacent complex-RMS only:

- if aligned 10→12 <= aligned 8→10: `ALIGNED_MONOTONIC`;
- otherwise compute worsening excess `max(second/first - 1, 0)` for canonical and aligned;
- if aligned worsening excess is at least 50% smaller than canonical: `ALIGNED_NON_MONOTONICITY_SUBSTANTIALLY_REDUCED`;
- otherwise: `ALIGNED_NON_MONOTONICITY_REMAINS`.

This classification is diagnostic only and cannot change canonical validation state.
