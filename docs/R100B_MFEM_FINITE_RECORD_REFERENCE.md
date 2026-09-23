# R100B MFEM finite-record independent reference

Issue #180 continues R100B after R100A-4 froze the solver-neutral finite-record transfer authority. This slice does **not** reuse PFFDTD output as truth and does **not** promote the historical PR #154 steady-state MFEM finest trace.

## Authority cross-check

The dedicated runner fails closed unless `wave-concave-l-room-v1` still has the exact R100A-4 contract:

- exact concave L-prism geometry, with the 2 m x 2 m notch absent;
- rigid boundary material on every exterior face;
- source `(1,1,1) m`, physical volume-velocity normalization, amplitude `1 m3/s`;
- receiver `(5,1,1) m`;
- density `1.2 kg/m3`, sound speed `343 m/s`;
- source time zero at sample zero;
- record interval `[0,2 s)`;
- solver-native recorded `dt`;
- harmonic convention `exp(-i*omega*t)` and finite-record analysis kernel `exp(+i*2*pi*f*n*dt)`;
- physical-pressure numerator and physical volume-velocity denominator;
- direct scored-frequency DTFT at 20..300 Hz in 1 Hz increments;
- no interpolation in the DTFT, no window, no filter, no zero padding;
- the existing magnitude/phase tolerances remain unchanged.

No R100A authority revision is introduced by this PR.

## MFEM transient formulation

The reference uses the already-approved MFEM v4.10 candidate pinned at source commit
`d964264cdb9a13e94a201b6c236c7721e0c8765f`.

MFEM evolves a scalar velocity potential `phi` on the exact conforming five-hexahedron L-prism:

`M phi_tt + c^2 K phi = c^2 b q`.

The physical pressure conversion is performed in the time domain before spectral analysis:

`p = rho * d(phi)/dt`.

For R100A-4's causal discrete source, the physical source record remains exactly
`q[0] = 1 m3/s`, `q[n>0] = 0`. The solver-internal application of that one native-time-step sample is the initial rate jump

`phi_t(0+) = c^2 * dt * M^-1 * b * q[0]`.

The denominator used by the reference is the recorded **physical pre-scaling** `q[n]`; the internal `c^2*dt*M^-1` mapping is never substituted for `Q_T`.

After the causal source kick the system is homogeneous. Time integration is Newmark average acceleration
`beta=1/4, gamma=1/2`, which introduces no algorithmic damping for the linear undamped problem. No absorbing term, artificial damping, window, smoothing, or filter is added.

Sample zero is the post-source-kick state at source `t0`, before the first homogeneous step. Samples are then recorded at the native fixed time grid while `0 <= n*dt < 2 s`.

## Direct finite-record transfer

For each scored frequency, the runner evaluates the R100A-4 sums directly:

`P_T(f) = dt * sum_n p[n] * exp(+i*2*pi*f*n*dt)`

`Q_T(f) = dt * sum_n q[n] * exp(+i*2*pi*f*n*dt)`

`H_T(f) = P_T(f) / Q_T(f)`.

The denominator must be finite and non-zero at every scored frequency. FFT interpolation, zero padding, a steady-state Helmholtz solve, and the historical harmonic MFEM trace are not accepted substitutes.

## Frozen refinement sequence

The refinement controls are fixed in source before observing transient results:

| level | H1 order | native sample rate | native dt |
| --- | ---: | ---: | ---: |
| p2-dt1over6000 | 2 | 6000 Hz | 1/6000 s |
| p3-dt1over8000 | 3 | 8000 Hz | 1/8000 s |
| p4-dt1over10000 | 4 | 10000 Hz | 1/10000 s |
| p5-dt1over12000 | 5 | 12000 Hz | 1/12000 s |

All levels retain the same exact five-cell geometry. The sequence is deliberately a coupled spatial/time refinement: polynomial order increases while native `dt` decreases. No result-dependent refinement controls are selected.

Reference qualification preserves the existing pre-R100A-4 conservative rule from the prior MFEM reference work: the final refinement delta must fit within `0.25x` the frozen candidate tolerance, and consecutive complex RMS refinement error must decrease strictly. This is not a tolerance change: candidate tolerances remain `0.75 dB`, relative magnitude `0.05`, and phase `8 deg`.

The fixed linear-system qualification limit is `1e-8` relative residual. Exceeding it produces **BLOCKED** evidence rather than a usable finest trace.

## PASS / FAIL / BLOCKED semantics

- **PASS**: the complete R100A-4 quantity was produced, the fixed transient/refinement qualification passed, and the finest observation is exposed as `qualified_reference_observation`.
- **FAIL**: the transient quantity was produced but the frozen convergence qualification failed. The finest observation remains explicitly `raw_finest_unqualified_observation` and is not reference truth.
- **BLOCKED**: build/runtime/source-record/solver-residual/contract validation prevented a technically admissible reference. A harmonic quantity is never substituted.

Workflow success means evidence generation completed; it does not imply reference PASS.

If MFEM were unable to express the frozen R100A-4 quantity, the required authority change would have to be made in R100A explicitly. Current MFEM v4.10 exposes a second-order transient wave operator and pure-Neumann path, so this slice first attempts the existing authority without revision.

## Evidence and provenance

The artifact records:

- R100A manifest id and semantic hash;
- candidate-manifest semantic hash;
- exact MFEM source commit;
- exact HTDT PR-head/check-out commit;
- CMake cache, runner, C++ source, executable, and raw-output SHA-256 values where available;
- MFEM/Python dependency versions;
- exact geometry/source/receiver/environment snapshot;
- physical source record and pressure conversion;
- every solver-native `dt` and `[0,2 s)` sample count;
- direct finite-record complex transfer at every scored frequency;
- raw pressure/source records for every refinement level;
- convergence metrics and qualification;
- configure/build timing, FEM assembly, solve/postprocess timing, peak process-tree RAM, disk/output size.

The dedicated workflow is `.github/workflows/r100b-mfem-finite-record.yml`. It builds and runs only the new transient reference target. It intentionally does not replay PFFDTD, Portal, radiation, or the historical harmonic MFEM concave probe.

## Deliberate non-claims

This slice does not select a production solver, does not alter R100A tolerances, does not modify PFFDTD evidence, does not make a steady-state/finite-record equivalence claim, and does not advance to R110.

RDC usage: **0**.
