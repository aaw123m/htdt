# R130D finite-band physical pulse — numerical convergence PASS

2026-10-10 JST. Repository `aaw123m/htdt`, Issue #53, Draft PR #118.

The user explicitly selected a practical finite-band physical source instead of requiring the singular original q0 protocol to pass. The resulting numerical R130D model **passes every registered grid, temporal and independent-method qualification check**. This is a new, explicitly versioned input and observation contract. The old original PFFDTD q0 remains `SELF_CONVERGENCE_FAILED`; no original HDF5 or score has been relabelled.

## Fixed physical model

- Exact 56 m³ rigid room: x,y ∈ [0,4] m, 0 ≤ z ≤ 4−y/4; sound speed 343.2 m/s and density 1.2 kg/m³.
- Fixed physical Dirac source (1.5,2,2) m and point receiver (2.5,2,2) m. No spatial source blur, grid rounding or coordinate fitting.
- Actual causal volume velocity `q(t)=exp(-0.5*((t-0.040)/0.004)^2)` m³/s for t≥0. Its physical Gaussian spectrum decays continuously; no spatial modes are removed.
- Exactly 250 ms; both signed 40/80 Hz `P_T/Q_T` values with the positive Fourier convention.
- Pressure is `rho*phi_t` from evolved velocity at integration midpoints. The complete physical record is integrated using midpoint quadrature, avoiding a finite-difference pressure stencil across a cut record endpoint.
- Conforming degree-four GLL spectral elements with z=(4−y/4)η. Positive physical nodal mass, natural Neumann stiffness, constant nullspace and exact affine physical energy. No cut-cell slivers, mass floors, damping or post hoc tuning.
- Every spatial mode is retained. The five grids contain 50,653 / 68,921 / 91,125 / 117,649 / 148,877 complete 3D modes.

The finite-band plan was committed and pushed before the new scores: [`046b964`](https://github.com/aaw123m/htdt/commit/046b964). Source width, center, positions, four adjacent pairs, three thresholds and independent-method checks were fixed in that plan.

## Full five-grid convergence

The original numerical thresholds 0.20 complex RMS, 0.25 relative magnitude and 15° phase were copied to this separate finite-band contract. **Every pair passes and all three metrics strictly decrease**.

| PPW pair | Complex RMS relative | Maximum magnitude relative | Maximum phase |
|---|---:|---:|---:|
| 28→32 | 0.01248251 | 0.01380321 | 0.11774162° |
| 32→36 | 0.00855186 | 0.00947127 | 0.06563457° |
| 36→40 | 0.00613412 | 0.00679752 | 0.03984652° |
| 40→44 | 0.00449063 | 0.00497714 | 0.02561954° |

An independent exact-in-time causal Gaussian convolution retains the complete semidiscrete eigenbasis, zero-mode and resonant limits, finite Gaussian tails, and the full physical finite-time integral. Its maximum adjacent spatial-only complex difference is **0.00007448**, or **0.00745%**. All separately registered spatial-control bounds pass. Spatial-only errors at this scale need not be strictly monotone; their nonmonotonic sequence is preserved in the evidence.

## Temporal accuracy

At the fixed finest spatial grid, 1,000 / 2,000 / 4,000 time steps are compared with the exact causal finite-time control:

| Steps over 250 ms | dt | Complex time error |
|---|---:|---:|
| 1,000 | 250 µs | 7.6141% |
| 2,000 | 125 µs | 1.9122% |
| 4,000 | 62.5 µs | 0.4787% |

Observed orders **1.9935 and 1.9979** match second-order midpoint theory. The delivered CLI defaults to **PPW44 / 4,000 steps**, below the registered 1% temporal error target. Errors quoted here are against the exact-time solution on the same spatial grid, not a rigorous bound against an unknown continuum room solution.

## Independent physical numerical comparator

Separately compiled, pinned MFEM P2 tetrahedral mass, stiffness and physical Dirac functionals were actually driven with this same new Gaussian input. They are independent of the SEM operator. Original decompressed export SHA-256 hashes are verified before each solve; MFEM source pin is `d964264cdb9a13e94a201b6c236c7721e0c8765f`.

- MFEM r2/r3/r4: **729 / 4,913 / 35,937 DOFs**; identical 125 µs steps, 250 ms, pressure and source quadrature.
- r2→r3→r4 errors strictly decrease in all three metrics. Finest pair complex error **0.5729%**, magnitude **0.6633%**, phase **0.2242°**.
- SEM PPW44 vs MFEM r4 at the **same** 125 µs step: complex error **0.04219%**, magnitude **0.06248%**, phase **0.01824°**.
- Every MFEM CG step satisfies the unmodified `1e-8` true residual bound; observed maximum **9.9973e-12**, at most **48** iterations out of 350 allowed.

Numerical qualification: **`PASS_FINITE_BAND_R130D`**. Physical acoustic measurements and arbitrary CAD room validation remain outside this bounded numerical model; `physical_measured_room_validation=NOT_VALIDATED` is retained.

## Cause and implementation change

The earlier short-support cut basis problem is eliminated by the conforming roof map. A separate all-five original-q0 SEM trial was also completed before changing the user-selected input; it still fails the original full-record criteria. Its negative evidence is retained in `r130d_boundary_fitted_sem_evidence_2026-10-10.json`.

An independently checked free-space point-impulse calculation provides a specific counterexample: exact stable spatial spectral propagation can converge distributionally while the point potential at a sharp record endpoint retains nondecaying oscillations. Rectangular integration of its pressure derivative carries that endpoint term. This proves that stability and a correct roof alone are insufficient for a singular impulse observer; it does **not** prove every possible original-q0 algorithm must fail. The finite physical Gaussian drive and direct velocity observation resolve the singular numerical observation issue in the new contract.

## Reproduce

Full registered SEM/time/MFEM verification, from repository root:

```powershell
python scripts/run_r130d_physical_pulse_sem.py --output benchmarks/acoustics/r130d_physical_pulse_sem_evidence_2026-10-10.json
```

Normal analysis and waveform export:

```powershell
python scripts/r130d_physical_pulse_cli.py --ppw 44 --steps 4000 --output-dir r130d_results
```

The standalone deliverable contains the same numerical core and CLI, tested on Python 3.12.10, NumPy 1.26.4 and SciPy 1.14.1. Output: waveform CSV, signed transfer CSV and provenance/accuracy JSON. The bundled result is reproducible without ChatGPT, the original PFFDTD installation or a LaTeX environment.

Verification includes exact polynomial quadrature, physical volume and affine Neumann roof tests, full-state implicit recurrence versus modal transform, matrix-exponential traces above Nyquist, Gaussian transform versus independent quadrature/ODE including resonance, observed time order, and independent recomputation of every actual grid/cross-method gate. No new original PFFDTD runs or GitHub Actions were initiated; scratch assets are preserved.

References: the [original spectral element formulation](https://academic.oup.com/gji/article/139/3/806/587264) motivates the GLL mass/stiffness construction. [NIST distributional derivatives](https://dlmf.nist.gov/1.16) describe the weak interpretation of singular sources. [Discrete multipole weak convergence research](https://par.nsf.gov/servlets/purl/10301929) explains why ordinary smooth-source convergence results do not automatically apply to singular wave sources. The quantitative results above are from the committed local numerical evidence, not these references.
