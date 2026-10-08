# R130D #938 — numerical non-convergence follow-up (2026-10-09)

## Decision

**NOT RESOLVED.** The exact sloped-fixture, rigid-boundary, 250 ms,
40/80 Hz, P/Q finite-impulse authority has **failed independent PFFDTD
self-convergence through 44 PPW** and **failed pinned MFEM self-convergence**.
Neither the existing #947 absorbing-halo correction nor a phase diagnostic
fix can be used to claim otherwise. Cross-solver and production gates remain
`CROSS_SOLVER_BLOCKED / NOT_VALIDATED / NO_GO`.

This document records actual CPU numerical runs, not a synthetic or analytic
simulation presented as a physical validation.

## I. Independent MFEM build obstacle: RESOLVED

A new explicit Windows GitHub Actions lane built the exact
`mfem/mfem@d964264cdb9a13e94a201b6c236c7721e0c8765f` reference and
executed refinements 1, 2, 3 (125/729/4913 DOF). This establishes
independent reproducibility, **not** numeric convergence:

- GitHub run: https://github.com/ka0923s-a11y/HTDT/actions/runs/37855143962
- Archived GitHub artifact: `r130d-mfem-reference-replay` id
  `11584137754`, SHA-256
  `553fcd49c3c1d1a9a2df2e277dadeda6b65533857c2406641bd2b76602d85ce1`
- Committed numerical output:
  `benchmarks/acoustics/r130d_mfem_reference_replay_evidence_2026-10-09.json`
- Transfer replay maximum complex deviations at refinements 1/2/3:
  `1.76e-10 / 8.53e-10 / 2.18e-9`, below a replay-only tolerance
  `1e-6`; all three previously recorded transfer values are reproduced.
- **MFEM self-convergence FAIL**: refinement 2→3 complex-RMS relative
  `0.207043` versus frozen limit `0.05`; phase error
  `177.6556°` versus frozen limit `5°`.
- The PR-triggered Actions checkout used a synthetic merge HEAD
  (`df794f27...` recorded in the JSON), not an asserted source main
  commit. The pinned **MFEM source SHA** and action-run head SHA are
  recorded separately; do not treat these identities as identical.

## II. PFFDTD high-PPW: resolution-only failure established through 44 PPW

The follow-up PPW set `[28,32,36,40,44]` and the 24-PPW anchor were
predeclared in a separately committed plan before execution:
`benchmarks/acoustics/r130d_high_ppw_diagnostic_plan.json`.

The same solver
`pffdtd@aa319f6c86517cb95aabfae8656277da62c3ead5`, Python
3.12/NumPy 1.26.4/SciPy 1.14.1/Numba 0.60.0, same sloped polyhedron and
same physical source/receiver, same 40/80 Hz bins, requested 0.25 s,
same frozen `-50 dB` mask, and same thresholds were used.

| PPW pair | complex RMS relative | max magnitude relative | max phase (degrees) |
| --- | ---: | ---: | ---: |
| 24→28 | 0.811733 | 0.535153 | 44.4668 |
| 28→32 | 1.246927 | 3.104124 | 87.1295 |
| 32→36 | 0.761305 | 0.547693 | 63.6516 |
| 36→40 | 0.367367 | 0.982010 | 27.4084 |
| 40→44 | **0.958742** | **0.543991** | **150.6298** |

Frozen limits: complex RMS relative `<=0.20`, max magnitude relative
`<=0.25`, max phase `<=15°`. **Every new adjacent pair fails** and
the trend is nonmonotone. The last pair became markedly worse despite
a smaller grid spacing. Plain increasing PPW is not a demonstrated
convergence remedy under this solver/fixture.

Committed raw complex transfer, grid, timestep, runtime and assessment:
`benchmarks/acoustics/r130d_high_ppw_diagnostic_evidence_2026-10-09.json`.
The preceding 8→24 run had already proved 8/10/12 replay bit-exact.

## III. MFEM low-mode/finite-record contamination isolation

Using the independently exported actual MFEM sparse stiffness and mass
matrices, an exploratory generalized eigensolver extracted low-frequency
Neumann modes with residual checks. The first nonzero room-mode frequency
is approximately `41.9693 Hz` at MFEM r2 and `41.9606 Hz` at r3;
another influential mode is `42.9098 → 42.9007 Hz`. **These do not
cross the 40-Hz evaluation bin.** A simple first-mode pole crossing does
not explain the 177.7° phase flip by itself.

The original full-basis point impulse excites frequencies far above
40/80 Hz; their finite-record contributions can contaminate narrow
spectral bins. We explicitly **changed the model** to retain only
modes below selected cutoffs, keeping the original 250 ms kernel and
point source for this exploratory falsification:

| Exploratory cutoff | MFEM r2→r3 complex-RMS relative | max phase (deg) |
| --- | ---: | ---: |
| full original basis | 0.207043 | 177.6556 |
| ≤90 Hz | 0.227301 | 13.1169 |
| ≤95 Hz | 0.227397 | 13.0924 |
| ≤100 Hz | 0.256474 | 14.7506 |

The phase collapse from ~178° to ~13° **supports a significant
higher-mode/finite-window contribution**, but the complex RMS remains
above `0.05` and the filtered model has **different physical authority**
from the frozen full-basis fixture. This is NOT a successful solution.

Code: `scripts/run_r130d_mfem_lowmode_diagnostic.py`.
Evidence: `benchmarks/acoustics/r130d_mfem_lowmode_diagnostic_evidence_2026-10-09.json`.

## IV. Bounded smoother-source falsification also fails

As another explicitly exploratory *changed-source* model, an LTI
convolution was applied to the actual PFFDTD impulse responses
at 28/32/36/40/44 PPW. All raw impulse transfer samples were replay-checked
against the original committed numerical evidence first. A fixed
Gaussian excitation centered at 12 ms was studied at 2, 3 and 4 ms
standard deviations; it is mathematically an LTI superposition, **not
a separate new solver run driven by the waveform**.

| Gaussian source width | final 40→44 complex RMS relative | final max phase (deg) |
| --- | ---: | ---: |
| 2 ms | 0.649453 | 37.5138 |
| 3 ms | 0.602173 | 34.2780 |
| 4 ms | 0.509888 | 26.6453 |

All exceed the original PFFDTD `0.20` RMS and `15°` phase limits.
A smoother source does not **by itself** resolve the remaining spatial/
geometry numerical issue.

Code: `scripts/run_r130d_exploratory_temporal_source_diagnostic.py`;
evidence:
`benchmarks/acoustics/r130d_temporal_source_exploratory_evidence_2026-10-09.json`.

## Next *implementation*, not threshold adjustment

Because the pinned first-order staircased boundary realization remains
strongly resolution-sensitive, **the current failure is not a removable
phase-scoring or time-window bug**. A defensible numerical correction
requires a separate solver-authority implementation:

1. Implement and validate a conservative oblique rigid-wall boundary
   representation (e.g. independent conformal/cut-cell Neumann operator)
   instead of relying exclusively on axis-aligned voxel staircase.
   Check stability, energy conservation, analytic shoebox eigenfrequencies,
   and convergence for the exact sloped polyhedron. Compare against
   the pinned PFFDTD without silently changing its source identity.
2. Specify a physically bandwidth-limited **input excitation contract**
   and implement it as actual source injection in **both** methods,
   with exact per-solver Q_T normalization. Use a *new predeclared
   numerical plan*, not a rewrite of the frozen full-basis impulse
   acceptance or exploratory postprocessed results.
3. Extend the independent MFEM **sparse low-band modal** reference to a
   finer level (r4) only after setting memory/time ceilings, and compare
   mode frequency, source/receiver residues, complex transfer, and
   numerical error. A modal truncation/regularization is a separate
   model and must be versioned and physically validated as such.
4. Require at least three independent resolution levels, monotonic
   convergence and unchanged acceptance thresholds for the declared
   physical model before cross-solver comparison; protect near-resonant
   bins from selective exclusion or post-hoc frequency retuning.
5. Keep production adoption/BRAS/owned-room gates #801/#809 separate.

**Explicit disposition:** issue #938 remains unresolved. Do not merge
this as an acoustic production solver fix. CI replay execution success
indicates a reproducible failure, not acoustic credibility.

## Tests

30 focused pytest tests passed on the pinned Windows Python 3.12
numeric environment after the new high-PPW and MFEM evidence checks
(plus one additional Gaussian-evidence regression check in the same
test module). The full desktop GUI suite was not executed.
