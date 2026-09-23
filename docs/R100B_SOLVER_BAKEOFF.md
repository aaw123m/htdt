# R100B Solver Bakeoff Authority and Execution Plan

> Tracking: Issue #101
> Depends on: R100A merged by PR #110 (`1714c078d4063f59da93f0d733171547f7eb486d`)
> Current state: R100B authority, pyroomacoustics direct/first-reflection + stochastic evidence, PFFDTD Windows reuse + rigid modes + complex-pressure convergence + native impedance gate + exact-concave evidence, MFEM rigid + concave independent-reference evidence, fail-closed low-band adoption profile, MFEM Portal continuity, R100A-3 radiation authority, and MFEM radiation candidate gate are merged. R100A-4 now freezes solver-neutral finite-record `P/Q` semantics for the rectangular-convergence and concave fixtures. PR #151 impedance representation passes; PR #154 concave reference is FAIL and MFEM complex-R extraction is BLOCKED; PR #155 stochastic evidence is FAIL/non-converged; PR #160 Portal continuity is PASS; PR #177 radiation fixture is FAIL because the frozen 360 s resource budget is exceeded. These mixed results are retained as bakeoff evidence. PFFDTD exact-concave candidate evidence is now merged as PR #181 and is FAIL/non-converged; solver-neutral spatial reflection decomposition, a qualified independent finite-record concave reference, obstacle, candidate-wide hard gates, and production solver selection remain pending.

## Purpose

R100B consumes the exact R100A benchmark manifest and produces evidence for solver selection. It must not let each solver redefine the room, openings, materials, source normalization, receiver timing, comparison convention or resource budget.

A candidate appearing in `r100b_candidates.json` means only that it is authorized for a bounded probe. It does **not** mean that its declared probe capabilities have passed, that its Windows packaging works in HTDT, or that it is selected for production.

## Version-pinned initial candidates

| Candidate | R100B role | Pinned source | License | Windows state before HTDT probe |
|---|---|---|---|---|
| PFFDTD | FDTD primary evaluation | `main@aa319f6c86517cb95aabfae8656277da62c3ead5` | MIT | upstream README targets Linux; native/portable Windows path must be proved or rejected |
| MFEM | independent FEM reference / production alternative | `v4.10@d964264cdb9a13e94a201b6c236c7721e0c8765f` | BSD-3-Clause | upstream CMake/Visual Studio path exists; HTDT acoustic/package probe still required |
| pyroomacoustics | geometric reference | `v0.10.1@f02b01dd6609709e2089aefa5d1e59c91d3a0601` | MIT | modern pyproject/CMake/cibuildwheel release; exact HTDT Python 3.12 Windows probe pending |

Upstream evidence is recorded in `benchmarks/acoustics/r100b_candidates.json`. Transitive dependencies are not approved merely because the top-level project has a permissive license.

## Bakeoff authority

`backend/src/htdt/acoustic_bakeoff.py` defines immutable, canonical records for:

- candidate identity, pinned upstream commit and source/license evidence;
- evaluation role and shipping-vs-reference scope;
- Windows/CPU/GPU probe state;
- required compiled representation;
- **probe capabilities**, explicitly distinct from verified capabilities;
- platform/thread/device authority;
- fixture evidence and per-observable status/error summaries;
- compile/solve/postprocess time, RAM, generated disk usage and output size;
- hard-gate evidence;
- final decision state.

Every run binds to both:

1. R100A manifest semantic SHA-256;
2. R100B candidate-manifest semantic SHA-256.

A run against a changed physical fixture or changed candidate pin is therefore stale by construction.

## Fail-closed selection

The validator rejects:

- the wrong R100A manifest/hash;
- a changed candidate manifest or source commit;
- unknown fixture evidence;
- execution of a fixture whose required capability is outside the candidate's authorized probe capability set;
- duplicate fixture/gate evidence;
- a passing fixture without all required observable records;
- a PASS observable whose reported error exceeds the R100A quantity-specific tolerance;
- a PASS fixture with missing or over-budget compile/solve/postprocess/RAM/disk/output/thread evidence;
- missing required hard-gate evidence;
- `not_applicable` for a hard gate that applies to the candidate;
- selecting a reference-only candidate for the production stack;
- selecting a shipping candidate while any applicable hard gate or applicable fixture is not PASS.

Performance never rescues a failed correctness gate.

## Current coverage

The current candidate capability envelope authorizes at least one candidate probe for every low-band wave-adoption fixture under R100A-4. Authorization is **not** verified capability or PASS evidence:

- MFEM declares `portal_continuity`; PR #160 records Portal fixture PASS;
- MFEM declares `wave_radiation_termination`; PR #177 records the radiation fixture FAIL on the frozen resource budget;
- PFFDTD and MFEM both carry concave evidence, but the current concave numerical evidence is non-converged/FAIL and no qualified finite-record independent reference exists.

`hybrid-overlap-continuity-v1` remains intentionally without a candidate because it belongs to the later hybrid role and is explicitly deferred by the low-band wave adoption profile. The preflight command reports missing capability/evidence rather than silently treating authorization as success.

## Repeatable command

Windows CI executes:

~~~powershell
python -m htdt.acoustic_bakeoff preflight `
  --manifest benchmarks\acoustics\r100a_manifest.json `
  --candidates benchmarks\acoustics\r100b_candidates.json `
  --adoption-profile benchmarks\acoustics\r100b_wave_adoption_profile.json
~~~

A recorded candidate run is validated with:

~~~powershell
python -m htdt.acoustic_bakeoff validate-run `
  --manifest benchmarks\acoustics\r100a_manifest.json `
  --candidates benchmarks\acoustics\r100b_candidates.json `
  --run <run-evidence.json>
~~~

The preflight is authority validation only. It is not a numerical benchmark result.

## Raw observation boundary

Third-party adapters do not emit PASS/FAIL. They emit typed raw samples through `RawFixtureObservation` / `RawObservableObservation`; HTDT then compares those values against the R100A expected samples and quantity-specific tolerances.

The sampled evaluator:

- requires exact fixture/observable/sample identity;
- requires observable kind and unit to match R100A;
- compares scalar, complex and 3D vector samples without backend-specific tolerance logic;
- computes absolute/relative/phase error centrally;
- refuses observables without explicit expected samples and requires a specialized evaluator for convergence, cross-fixture and independent-reference cases;
- preserves compile/solve/postprocess/RAM/disk/output evidence for the existing R100B budget gate.

The first external probe is `pyroomacoustics v0.10.1` against `geometric-direct-first-reflection-v1`. GitHub Actions resolves the official CPython 3.12 Windows wheel, records its SHA-256, maps the exact R100A box/material/source/receiver into a first-order image-source room, and stores raw image-derived path observations plus R100B evidence as an artifact. Missing Windows wheel/installability is recorded as a blocked candidate probe rather than silently switching version/backend.

## R100A-2 replay boundary

PR #116 intentionally changes R100A identity before pressure-transfer evidence is accepted. Revision 1 omitted air density even though the research contract requires density/sound speed authority. R100A-2 adds explicit density and removes the contradictory duplicate zero-degree phase gate from the complex RMS convergence observable without changing its 0.02 Pa / 2% tolerances.

Because every R100B run binds the whole manifest semantic hash, revision-1 artifacts below remain useful historical measurements but are stale for current solver selection until their dedicated workflows replay against R100A-2. This is expected fail-closed behavior, not a reason to weaken hash binding.

## R100A-3 radiation termination boundary

Issue #161 advances the benchmark authority because `BoundaryTermination(kind='radiation')` in R100A-2 did not define a mathematical boundary condition. R100A-3 freezes a dedicated `wave_radiation_termination` capability and the local first-order outgoing relation `p/u_n=rho*c`, outward normal from the modeled region, `k=omega/c`, and therefore `dp/dn-i*k*p=0` under the global `exp(-i*omega*t)` convention.

The termination fixture also carries a semi-analytical rectangular-waveguide modal reference on the full 20–300 Hz / 1 Hz grid. `scripts/check_r100a_radiation_reference.py` independently reconstructs the reference and checks N=8 -> N=12 modal convergence before accepting the stored samples.

This R100A revision intentionally changes the manifest semantic hash. It does **not** add `wave_radiation_termination` to any candidate merely to make adoption pass. Existing candidate evidence must replay under R100A-3; a later solver adapter may claim the new capability only after implementing and verifying the exact frozen boundary.

## R100A-4 finite-record transfer boundary

Issue #180 advances the benchmark authority because the 2 s rectangular-convergence and concave fixtures previously depended on adapter-local assumptions about temporal impulse sampling and finite-record Fourier normalization. R100A-4 freezes a solver-neutral contract instead of copying one backend's internal source representation.

For exactly `wave-rectangular-convergence-v1` and `wave-concave-l-room-v1`, R100A-4 requires a causal unit-sample volume-velocity source at `source_t0`, solver-native recorded `dt`, the half-open record `[0,2 s)`, time-harmonic dependence `exp(-i*omega*t)` with its dual direct analysis kernel `exp(+i*2*pi*f*n*dt)`, no window/filter/zero-padding interpolation, and normalized `P_T(f)/Q_T(f)` using the physical pressure record and the physical volume-velocity source samples on the same solver time grid before any solver-internal numerical source scaling. A zero/non-finite source spectrum blocks the evidence.

The revision intentionally does **not** prescribe one common `dt`; independent solvers may refine time differently as long as the actual time grid and source record are retained as provenance and the same transfer definition is used. Harmonic Portal/radiation evidence is not silently reclassified as finite-record evidence.

The R100A semantic hash therefore changes again. Existing R100B workflows must replay under R100A-4. This authority change does not alter any existing physics tolerance or turn the current MFEM/PFFDTD concave negative evidence into PASS.

## Accepted external evidence so far

### pyroomacoustics geometric reference

PR #112 / merge `5725f8f2eecb150773202bf324132a34a40ba492` established the raw-observation evaluator and a Windows reference probe for `geometric-direct-first-reflection-v1`.

- official pyroomacoustics v0.10.1 CPython 3.12 Windows AMD64 wheel was resolved and SHA-256 pinned;
- direct distance, direct delay, first y-min reflection point and reflected path length all passed the R100A tolerances with zero error;
- the candidate remains reference-only and this evidence does not authorize a wave solver.

### pyroomacoustics stochastic seed / convergence evidence

PR #155 extends the same pinned pyroomacoustics v0.10.1 candidate to the frozen R100A-2 `geometric-seed-repeatability-v1` stochastic gate. Final reviewed head binds HTDT probe/evaluator/authority source identities and its dedicated workflow completed successfully, while the candidate fixture evidence remains **FAIL / non-converged** under the pre-registered controls.

- same seed `20260918` reproduces the exact same raw selected-band histogram SHA-256 on both repeats;
- extracted decay-curve repeatability remains FAIL because the frozen fine estimator has insufficient positive cumulative energy at 500 Hz / 240 ms;
- four independent seeds are retained at each 8,192 / 32,768 / 131,072-ray level;
- coarse and medium seed variation is retained, while all four fine independent seeds are `insufficient_support`; no zero response is invented;
- the frozen medium-to-fine convergence criterion is therefore not satisfied and no tolerance/fixture control is relaxed;
- exact R100A/fixture/stochastic-authority/candidate hashes, raw NPZ, official wheel/source provenance and per-budget resource scaling are recorded in `docs/R100B_PYROOM_STOCHASTIC_GATE.md`.

This is valid bakeoff FAIL evidence and is not a production-solver selection.

### PFFDTD Windows source-checkout feasibility

PR #113 / merge `ea5f5b8631e5097d37788210b2652b3089a28807` proved that pinned PFFDTD `aa319f6c86517cb95aabfae8656277da62c3ead5` can execute its existing Python/Numba CPU pipeline on GitHub-hosted Windows.

The accepted platform smoke records three exact-source-checked runtime shims required by the pinned upstream on Python 3.12 / modern NumPy:

1. replace the removed `np.float` alias used only to obtain machine epsilon;
2. release two NumPy views before closing shared memory in `vox_grid_base.py`;
3. release the final NumPy shared-memory view in `vox_scene.py`.

No FDTD stencil, voxel-intersection, material or source algorithm is modified. The accepted smoke completed geometry -> voxelization -> HDF5 setup -> CPU FDTD -> `sim_outs.h5` -> upstream-equivalent receiver interpolation on Windows. This is platform/reuse evidence only: the R100A eigenfrequencies were deliberately not scored, and Windows product packaging remains a separate gate.

## Accepted PFFDTD rigid-mode physics evidence

PR #115 numerical run `35349358027` passes `wave-rigid-rectangular-modes-v1` on the current branch. The durable summary is `benchmarks/acoustics/evidence/r100b_pffdtd_rigid_modes_2026-09-18.json`; raw traces remain bound by the recorded Actions artifact/hash.

- `backend/src/htdt/acoustic_pffdtd_adapter.py` centralizes the three source-checked compatibility shims, rigid R100A geometry compiler and upstream receiver interpolation contract.
- R100B fixture evidence now requires generated disk usage and enforces `disk_budget_mb` in addition to compile/solve/postprocess/RAM/output/thread limits.
- the modal probe solves the same room at Cartesian grid spacings 0.5 m, 0.25 m and 0.125 m;
- every grid spacing divides the 4 x 5 x 2.5 m room dimensions exactly;
- receiver traces are archived for audit;
- each mode is extracted from the actual time trace with a deterministic Hann-window spectral estimator;
- the 0.5 -> 0.25 -> 0.125 m sequence must show decreasing refinement deltas;
- the final sample passed to the central R100A evaluator is a declared second-order Richardson extrapolation from the 0.25 and 0.125 m results.

PFFDTD internally derives 343.2 m/s at 20 deg C while the R100A fixture authority is exactly 343.0 m/s. The adapter records that difference instead of silently rewriting either authority.

Accepted results:

| Observable | h=0.5 m | h=0.25 m | h=0.125 m | p=2 extrapolated | R100A abs error | Result |
|---|---:|---:|---:|---:|---:|---|
| m010 | 34.218337 | 34.287349 | 34.304913 | 34.310768 | 0.010768 Hz | PASS |
| m100 | 42.715319 | 42.856153 | 42.890952 | 42.902552 | 0.027552 Hz | PASS |
| m110 | 54.827426 | 54.910278 | 54.931240 | 54.938228 | 0.031438 Hz | PASS |
| m001 | 67.877782 | 68.453956 | 68.593824 | 68.640446 | 0.040446 Hz | PASS |

The coarse-to-medium / medium-to-fine delta ratios are 3.93–4.12, consistent with the declared second-order refinement model. The total evidence uses 6.014 s compile/setup/JIT, 0.586 s solve, 0.066 s postprocess, 202.66 MiB peak RSS, 1.238 MiB generated disk and 0.387 MiB raw output, all within the fixture budget.

Evidence authority:

- workflow run: `35349358027`;
- Actions artifact: `10549576187`;
- artifact digest: `sha256:0f97ada6fd1555e89de24168316526b20b7a6a874a0281b4f1f0ac22aa96632e`;
- raw signal archive SHA-256: `019766f208fa4e6a4bee93bb26d56700c095e5ff2f0b6b76289db4ae5b9c6b85`;
- BakeoffRun semantic hash: `c54f7ec2758f94e5ed4b433a002b0cc30ccfe0a841d42ad2fbe0bf35470855f9`.

This is a fixture-level physics PASS, not a candidate-wide solver acceptance.

## MFEM independent rigid-mode reference

PR #114 / merge `245a3efc66144b81742d65c62ad99ba081fe7426` adds the first independent FEM numerical reference. Pinned MFEM v4.10 is built serially on Windows without MPI/METIS/LAPACK; MFEM assembles the H1 Neumann Laplacian stiffness/mass matrices and the probe solves the small deterministic generalized eigensystem. Latest revision-1 run `35350707920` used order 5 / 216 DOF and passed all four rigid-mode observables with maximum absolute error 9.06e-6 Hz, 0.0787 s eigensolve, 9.32 MiB peak RSS and explicit disk evidence. This reference must replay under R100A-2 before it is current selection evidence.

## Complex-pressure convergence boundary

PR #116 added the specialized common convergence evaluator; R100A-4 now types its input explicitly as unsampled `complex_pressure_transfer_pa_per_m3_s` / `Pa/(m3/s)`. Adapters return keyed complex transfer samples for ordered coarse-to-fine representations; HTDT centrally computes complex RMS absolute/relative error against the finest level, requires decreasing error, and applies the frozen final tolerances.

For PFFDTD, the adapter does not label native `u` as Pa. Pinned upstream treats `u` as acoustic velocity potential. Under R100A-4 HTDT first converts the finite native potential record to a physical pressure record with the declared `p=rho*d(phi)/dt` second-order adapter stencil, then applies the same dt-weighted direct DTFT to pressure and the physical pre-grid volume-velocity source record and forms `P_T/Q_T`. It does not use a finite-window `-i*omega*rho*Phi_T` shortcut. The probe runs the complete half-open `[0,2 s)` record at h=0.5/0.25/0.125 m, maps the PFFDTD temperature control so the internal wave speed is exactly the frozen 343.0 m/s, and evaluates the exact 20–300 Hz / 1 Hz grid with no window or filter.

PR #151 preserves the R100A-2 impedance fixture and maps only the exact frequency-independent purely resistive subset to PFFDTD `DEF=[0,2,0]`. The pinned upstream reflection function returns `R=1/3+0j` at 100/200/300 Hz and the central evaluator reports PASS. Reactive/frequency-varying fitting remains unsupported, and no scalar absorption coefficient is used. This gate is not evidence that a full spatial FDTD run recovers the same reflection coefficient.

## MFEM Portal continuity evidence

PR #160 qualifies the frozen `wave-portal-split-room-v1` semantics on pinned MFEM v4.10. The peer rectangular room and Portal case use the same two-hexahedron conforming H1 mesh split at x=3 m. The only semantic difference is the volume-region attribute partition: peer `[1]`, Portal `[1,2]`. The x=3 m face remains the single internal face and receives no boundary/material condition.

Current-head run `35416697808` reports Portal fixture **PASS** under the frozen cross-fixture tolerances: 281 magnitude samples and 281 phase samples, maximum magnitude absolute/relative error `0`, maximum phase error `0 deg`, and complex RMS relative difference `0`. Topology evidence records 2 elements, 11 total faces, 10 exterior faces, one internal face and equal peer/Portal DOF counts.

The candidate-manifest update adds `portal_continuity` only as probe permission. The same replay preserves MFEM concave **FAIL** and independent impedance extraction **BLOCKED**. No production solver decision follows from the Portal PASS.

## Low-band wave adoption profile

R100B numerical runs and the final product-adoption decision are intentionally separate authorities. Historical run hashes remain bound to the exact R100A semantic hash and candidate manifest; the final low-band wave-solver selection additionally binds `benchmarks/acoustics/r100b_wave_adoption_profile.json`.

The adoption profile requires one shipping wave candidate to cover and PASS all of:

- `wave-rigid-rectangular-modes-v1`;
- `wave-rectangular-convergence-v1`;
- `wave-normal-incidence-impedance-v1`;
- `wave-concave-l-room-v1`;
- `wave-portal-split-room-v1`;
- `wave-explicit-radiation-termination-v1`.

The corresponding required capability union is `wave_rigid`, `wave_impedance`, `wave_radiation_termination`, and `portal_continuity`. A candidate cannot make a required fixture disappear from the selection gate merely by omitting a probe capability. Missing capability, missing/non-PASS required fixture, stale adoption-profile binding, or a failed hard gate blocks a `selected` decision.

Geometric-reference fixtures and `hybrid-overlap-continuity-v1` are deliberately deferred from this **low-band wave solver** adoption profile. Their exclusion is explicit scope separation, not implicit PASS evidence. R150/R160 own those later roles.

The adoption profile is independent authority from R100A. R100A-3 changed the benchmark semantic hash because radiation semantics were previously incomplete; R100A-4 changes it again to freeze finite-record transfer semantics. Historical artifacts remain preserved but are stale for current selection until replayed under the current authority.

## Next R100B implementation slices

The numerical bakeoff proceeds in this order:

1. R100A-2 replay and PFFDTD rectangular complex-pressure convergence: **implemented in PR #116**; workflow completes but the candidate convergence evidence remains FAIL under frozen tolerances.
2. explicit complex-impedance reflection: **native PFFDTD DEF boundary/reflection-function gate PASS in PR #151**; this does not claim spatial FDTD incident/reflected propagation validation.
3. MFEM concave/impedance independent reference: **implemented in PR #154**; concave p-refinement FAIL, impedance independent complex-R extraction BLOCKED. Do not promote the finest non-converged trace to reference truth;
4. pyroomacoustics v0.10.1 stochastic seed/convergence: **implemented in PR #155**; workflow PASS, fixture FAIL/non-converged because the frozen fine estimator has insufficient support. Same-seed raw histogram identity is repeatability evidence only;
5. R100A-3 explicit radiation termination semantic/reference authority: **implemented in PR #162**; exact Robin sign/normal/transfer authority and semi-analytical reference are frozen;
6. MFEM R100A-3 radiation candidate gate: **implemented in PR #177**; workflow/compile PASS, fixture FAIL on the frozen 360 s resource budget. No pressure samples are fabricated after timeout and the FAIL remains current candidate evidence;
7. PFFDTD exact-concave candidate evidence: **implemented in PR #181**. Exact L-prism geometry is preserved, but h=0.5/0.25/0.125 m self-refinement is FAIL/non-converged. The current MFEM concave reference is also FAIL and cannot be promoted to truth. Issue #180 tracks the missing finite-record independent reference authority; Issue #179 retains the spatial incident/reflected extraction BLOCKED until solver-neutral decomposition authority exists;
8. cover geometric obstacle and applicable external-measured/candidate-wide hard gates without inventing unsupported capabilities;
9. publish the R100B ADR only after applicable hard gates have real evidence.

If no shipping candidate clears the gates, R100B exits with a no-go ADR and a bounded next experiment. It must not force a winner.

## Non-claims

This slice does not:

- select PFFDTD, MFEM or pyroomacoustics for production;
- claim candidate-wide numerical accuracy from the single accepted PFFDTD rigid-modal fixture;
- claim Windows packaging for PFFDTD;
- claim that an upstream capability maps exactly to HTDT Portal/BoundaryTermination/object semantics;
- validate the owned room;
- authorize R110+ production integration yet.

RDC is not used for R100B. GitHub Actions is the execution authority for work that can be tested in the cloud.
