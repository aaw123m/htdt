# R100B MFEM concave independent numerical reference

Issue #152 extends the existing R100B MFEM v4.10 reference path. It does not replay the merged rigid-room eigenmode gate, does not consume PR #151 PFFDTD numerical output, and does not select a production solver.

## Frozen authority

The probe reads `benchmarks/acoustics/r100a_manifest.json` and fails closed if the relevant R100A-2 authority differs from the frozen contracts.

### Concave fixture

The numerical reference targets `wave-concave-l-room-v1` exactly:

- horizontal polygon: `(0,0)-(6,0)-(6,4)-(4,4)-(4,2)-(2,2)-(2,4)-(0,4)`;
- extrusion: `z=0..2.5 m`;
- source: `(1,1,1) m`, unit volume velocity `Q=1 m3/s`, zero phase, omnidirectional;
- receiver: `(5,1,1) m`;
- all exterior boundaries: explicit R100A-2 rigid material;
- density: `1.2 kg/m3`;
- sound speed: `343 m/s`;
- frequency grid: `20..300 Hz` at `1 Hz`;
- Fourier convention: `exp(-i*omega*t)`;
- interpolation: `linear_complex`;
- no window or filter.

The MFEM volume mesh is the exact union of five conforming hexahedra. It tiles the L-prism only; the `x=2..4, y=2..4` concave notch is not filled. No rectangular fallback, geometry simplification, portal substitution, or solver-side fixture edit is permitted.

### Harmonic formulation

The probe uses serial conforming MFEM H1 finite elements. Under the frozen `exp(-i*omega*t)` convention and point volume-velocity source,

`div(grad p) + k^2 p = i*omega*rho*Q*delta`.

With rigid natural Neumann boundaries the assembled real system for the imaginary pressure component is

`(K - k^2 M) p_i = -omega*rho*Q*b_delta`.

The receiver uses an independent MFEM delta functional at the frozen receiver coordinate. Raw pressure is stored as complex pressure in Pa. The reported transfer is `p/Q`; magnitude evidence uses `20*log10(|p/Q|)` and phase uses wrapped `atan2(Im, Re)`.

The linear solve uses serial MINRES for the symmetric-indefinite `K-k^2M` operator with a diagonal-Jacobi preconditioner built from positive `K+k^2M`, relative solver tolerance `1e-10`, and maximum 8000 iterations. A raw relative residual above `1e-8` blocks reference qualification rather than being accepted as a reference.

## Discretization and convergence

The geometry mesh stays fixed and exact while H1 polynomial order is refined from p=2 through p=5. Every order produces the complete frozen 281-frequency grid. The artifact records, for every level:

- exact element count and true DOF count;
- polynomial order;
- FEM assembly time;
- total solve time;
- per-frequency MINRES iteration count and residual;
- raw complex pressure at every frequency.

Reference qualification is deliberately stricter than candidate acceptance. The R100A-2 tolerances remain unchanged; the reference must additionally satisfy a fixed `0.25 x` qualification margin between the final p=4 and p=5 levels and strict decrease of the consecutive complex RMS p-refinement error.

For the frozen current tolerances this means:

- magnitude absolute delta <= `0.1875 dB`;
- magnitude relative delta <= `0.0125`;
- phase delta <= `2 deg`;
- p2->p3, p3->p4, p4->p5 complex RMS relative error must strictly decrease.

The existing central `observable_tolerance_violations()` and `validate_bakeoff_run()` remain the candidate/reference evidence authority. The stricter reference margin does not modify or replace R100A tolerances.

A non-converged finest trace is therefore **FAIL** or **BLOCKED** evidence, not reference truth. No tolerance is adjusted from observed results.

An additional p=6 extension was executed after the canonical p2-p5 sequence failed convergence. That p=6 run reached a maximum linear relative residual of `1.5750854042217432e-08`, above the fixed `1e-8` solver-qualification limit, so p=6 is retained only as **BLOCKED** extension evidence. It is not used to replace the canonical p2-p5 convergence result and does not trigger any tolerance or fixture change.

## Raw result / central evaluator boundary

R100A-2 intentionally leaves the two concave independent-solver observables without embedded expected samples. The probe therefore does not fabricate expected values in the benchmark manifest.

Instead it persists a `RawFixtureObservation` containing the full finest-order:

- `lroom-fr` frequency/magnitude trace;
- `lroom-phase` frequency/phase trace;
- adapter/backend identity and resource metrics.

This is central-evaluator-ready raw reference evidence for later candidate/reference comparison. `evaluate_sampled_fixture()` is not invoked against invented R100A samples.

## Explicit impedance capability: BLOCKED reference extraction

The same run validates the frozen `wave-normal-incidence-impedance-v1` authority:

- only `room-xmax` uses `b-impedance`;
- explicit material `z-2z0`;
- `Z = 823.2 + 0j Pa*s/m` at 100, 200, and 300 Hz;
- `rho*c = 411.6 Pa*s/m`;
- normalized impedance `Zn = 2`;
- normalized admittance `Yn = 0.5`;
- no scalar absorption value is used or inferred.

MFEM can represent the corresponding complex Robin boundary exactly. Under the frozen Fourier convention, `p=Z*u_n` gives `d(p)/dn=i*omega*rho*p/Z`, which maps to a complex boundary mass term.

However, the frozen fixture defines a point volume-velocity source in a finite room while its required observable is incident/reflected complex reflection coefficient `R`. An independent spatial MFEM `R` would require a plane-wave excitation, incident/reflected decomposition, or another extraction rule that R100A-2 does not freeze. Computing `R=(Z-rho*c)/(Z+rho*c)` directly from the input impedance would only repeat the closed form and would not be independent MFEM numerical evidence.

Accordingly the MFEM impedance reference result is recorded as **BLOCKED**. The fixture is not changed to fit MFEM, and no absorption-to-impedance conversion is introduced.

## Provenance and resources

The dedicated artifact records:

- R100A semantic hash and candidate-manifest hash;
- exact MFEM source commit `d964264cdb9a13e94a201b6c236c7721e0c8765f`;
- exact HTDT source commit;
- MFEM/CMake build switches and CMake cache hash;
- probe source/executable hashes;
- Python dependency versions;
- geometry, source, receiver, environment, material and comparison snapshots;
- p-refinement settings and raw MFEM output;
- native build, FEM assembly, solve and postprocess times;
- peak process-tree RAM, work disk, raw-output and executable sizes;
- PASS / FAIL / BLOCKED results.

The dedicated GitHub Actions workflow may succeed when a numerical reference is FAIL or BLOCKED. Workflow success means that evidence generation completed; it is not a solver/reference PASS.

## Deliberate non-claims

This slice does not:

- reuse PFFDTD #151 PASS as MFEM evidence;
- alter PFFDTD implementation;
- complete candidate-wide R100B hard gates;
- finalize the R100B solver-selection ADR;
- select MFEM or PFFDTD for production;
- implement R110/R130 or later authority;
- modify O90/O100 or GUI code.

RDC usage for Issue #152 implementation: **0**.
