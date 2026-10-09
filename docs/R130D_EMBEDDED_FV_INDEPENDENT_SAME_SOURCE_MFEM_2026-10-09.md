# R130D #938: independent same-source comparison — 2026-10-09

## Verdict

**First independently assembled, actually driven MFEM vs embedded Neumann FV comparison is executed and reproducible.** The cross-method discrepancy improves monotonically with FV spatial resolution, but **independent MFEM is not yet self-converged** at the finest r3 reference. The experiment is diagnostic, not a canonical R130D numerical-acceptance pass.

Both solvers use the same exact rigid sloped polyhedron, 56m³ geometry, sound speed 343.2m/s, air density 1.2kg/m³, source (1.5,2,2)m, receiver (2.5,2,2)m, Gaussian volume velocity centered 12ms with sigma 3ms, **actual time-dependent source injection**, implicit midpoint dt=250µs, duration 250ms, common exp(+iωt) midpoint P_T/Q_T at 40 and 80 Hz. FV has cell-centred trilinear source/observation functionals; MFEM uses independent quadratic continuous FEM delta-point assembly.

### Independent MFEM provenance

Pinned original MFEM source: `d964264cdb9a13e94a201b6c236c7721e0c8765f`. The mass, stiffness and point functional CSR systems were **compiled and independently generated** on GitHub Actions [run 37855143962](https://github.com/ka0923s-a11y/HTDT/actions/runs/37855143962) (artifact 11584137754). The new experiment reads exactly pinned SHA-256 export files (all three hashes fixed in the prospective plan) and runs **new per-step Gaussian source injection**, not impulse convolution.

Predeclared authority: `benchmarks/acoustics/r130d_fv_mfem_same_drive_plan_2026-10-09.json`, committed **before numerical execution**. Independent actual output: `benchmarks/acoustics/r130d_fv_mfem_same_drive_evidence_2026-10-09.json`. Execution script: `scripts/run_r130d_fv_mfem_same_drive_comparison.py`.

### Numerically measured same-source results

| Level comparison | Normalized complex L2 | Max 40/80Hz phase gap |
|---|---:|---:|
| MFEM r1→r2 (125→729 DOF) | 0.852079 | 58.162° |
| MFEM r2→r3 (729→4913 DOF) | 0.181413 | 9.256° |
| FV n20→n24 | 0.068819 | 3.081° |
| FV n24→n28 | 0.037275 | 1.581° |
| FV n28→n32 | 0.025719 | 1.205° |

| FV grid vs fixed independent MFEM r3 | Normalized complex L2 | Max phase gap |
|---|---:|---:|
| n20 | 0.203484 | 9.851° |
| n24 | 0.141189 | 6.770° |
| n28 | 0.106770 | 5.188° |
| **n32** | **0.082436** | **3.983°** |

MFEM r2→r3 80-Hz transfer amplitude and phase are still noticeably different; **MFEM r3 is not a continuum exact solution**, and FV n32 vs r3 residual 8.24% cannot be mistaken for an independently verified bounded relative error against the physical limit.

### Implementation checks and actual scientific limitation

- MFEM: quadratic P2, exact sloped tetra mesh, rigid natural Neumann, export `M`, `Kc²`, point-source and point-receiver functionals independently via pinned MFEM.
- FV: exact clipped fluid volumes and open grid-face areas, conservative two-point flux, rigid no-throughflow, implicit midpoint.
- Both: `M phi_tt + K phi = c² b q(t)`, `p(t) = rho rᵀ phi_t(t)`, identical sampled source, identical midpoint time integration and frequency kernel. No source-response amplitude or phase fitting.
- Old full-impulse `SELF_CONVERGENCE_FAILED` from both canonical PFFDTD and pinned MFEM remains unchanged; new bandlimited-source numerical authority is separate.
- Candidate runtime implementation is **not wired into the production solver dispatch**, deliberately avoiding unvalidated CAD shape generalization or clandestine solver substitution.
- At least MFEM r4 or another independent higher-accuracy continuum reference is required before claiming cross-solver spatial convergence at fixed physical input. FV needs nonorthogonal-boundary error analysis and further high-resolution validation. Repeated pair shrinkage is supportive but not a mathematical convergence theorem.

## Remaining executable work

1. Rebuild independently pinned MFEM P2 reference at **refinement r4** with strict CPU/RAM limits, actual same-waveform implicit midpoint, and compare r3→r4; do not use uncontrolled full modal dense eigensolve at 35,937 DOF.
2. Confirm that FV spatial error against MFEM r4 or Richardson-extrapolated reference is acceptably bounded at all 40/80 Hz bins; ensure no selective frequency exclusions.
3. Run source/receiver amplitude and position invariance, 3D non-planar CAD cut geometry, and sliver cell/CFL stress tests.
4. Register the new candidate as an opt-in, explicitly versioned and provenance-bearing solver only after independent numerical acceptance. The production gates BRAS and owned-room evidence #809/#801 remain separate.

**Gate summary:** `SELF_CONVERGENCE_FAILED` (original contract) / `CROSS_SOLVER_BLOCKED` / `NOT_VALIDATED`; production `NO_GO` maintained. This is a reproducible experimental numerical improvement, not a claim that #938 has been fully resolved.
