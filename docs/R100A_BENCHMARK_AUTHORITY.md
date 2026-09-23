# R100A Acoustic Benchmark Authority

> Issue #101 / R-series first implementation slice  
> Schema: `r100a-4`  
> Canonical machine-readable manifest: `benchmarks/acoustics/r100a_manifest.json`

## Revision 2 pressure authority correction

R100B pressure-field work exposed an omission in revision 1: the research contract required both density and sound speed to be fixed, but `BenchmarkEnvironment` did not carry density. Revision 2 makes `density_kg_m3` mandatory and fixes the canonical fixture density at 1.2 kg/m3. This is required for adapters whose native state is not pressure; for example, PFFDTD evolves acoustic velocity potential and HTDT converts it using `P/Q = -i*omega*rho*Phi/Q` under the manifest Fourier convention.

The complex-pressure convergence observable continues to use the original 0.02 Pa absolute and 2% relative tolerances. Its redundant `phase_deg: 0` field was removed because phase is already contained in the complex RMS error; keeping a second exact-zero-degree requirement would contradict an unsampled numerical convergence test.

Revision 2 changes the manifest semantic SHA-256 intentionally. Revision-1 R100B artifacts remain historical records, but they cannot satisfy current solver-selection gates until replayed against revision 2.

## Revision 3 radiation termination authority

Revision 2 still left `BoundaryTermination(kind='radiation')` mathematically ambiguous. Revision 3 fixes that gap rather than allowing each solver adapter to assign its own meaning to the word `radiation`.

For `wave-explicit-radiation-termination-v1`, R100A-3 now freezes:

- outward normal from the modeled `AcousticRegion`;
- local first-order outgoing model `p/u_n = rho*c`;
- `k = omega/c` using the fixture's frozen environment;
- under `exp(-i*omega*t)`, the equivalent Robin form `dp/dn - i*k*p = 0`;
- explicit binding to boundary `b-interface` on the x=6 m aperture;
- dedicated `wave_radiation_termination` capability, distinct from rigid or impedance support;
- absolute transfer magnitude is explicitly `20*log10(|P/Q| / (1 Pa/(m3/s)))`, so it cannot be confused with SPL dB re 20 uPa.

This is intentionally a local first-order/Sommerfeld-type approximation. It is not described as an exact exterior-domain radiation solution for arbitrary incidence.

The same revision embeds 281 semi-analytical 20–300 Hz reference samples. A repository checker reconstructs them from normalized Neumann transverse modes plus the exact one-dimensional Green function for the frozen Robin boundary. The N=8 -> N=12 modal refinement must remain below `1e-9` complex RMS relative and `1e-8` maximum point-relative error before the N=12 samples are accepted as authority.

Revision 3 intentionally changes the manifest semantic SHA-256. Earlier R100B artifacts remain historical evidence and must replay under the new hash before they can participate in a current selection decision.

## Revision 4 finite-record transfer authority

Revision 3 still left finite-record wave transfer partially adapter-defined. In particular, the rectangular convergence and concave L-room fixtures used a 2 s record, but did not freeze a solver-neutral temporal excitation / record interval / direct-DTFT normalization contract. Revision 4 fixes that gap without forcing independent solvers onto one common numerical time step.

R100A-4 applies the finite-record contract **only** to `wave-rectangular-convergence-v1` and `wave-concave-l-room-v1`:

- source excitation is a causal discrete unit-sample volume-velocity record on the solver's own time grid: `q[0]=source.amplitude`, `q[n>0]=0`, with source phase 0 deg;
- sample zero is exactly `source_t0`;
- scored samples are exactly the half-open interval `[0,T)` with frozen `T=2 s`; each solver records its actual `dt`, sample count and last/next sample times as provenance;
- solver `dt` is **not** common authority. It remains a discretization/refinement parameter and therefore `comparison.time_step_s` is null for these fixtures;
- `comparison.fourier_sign = exp(-i*omega*t)` is the time-harmonic/phasor dependence; the corresponding analysis transform is its dual;
- direct scored-frequency DTFT therefore uses `exp(+i*2*pi*f*n*dt)` at the frozen frequency grid, with no window, filter or zero-padding interpolation;
- finite-record spectra are `X_T(f)=dt*sum_n x[n]*exp(+i*2*pi*f*n*dt)`;
- the numerator record is **physical pressure**. A solver that stores another primary field may use a declared conversion to physical pressure, but that conversion and its numerical stencil must be recorded as adapter provenance before the DTFT is taken;
- normalized transfer is `H_T(f)=P_T(f)/Q_T(f)` from the physical pressure record and the **physical volume-velocity source samples on that same solver time grid, before solver-internal numerical scaling**; the rectangular complex-convergence observable is therefore explicitly typed as `complex_pressure_transfer_pa_per_m3_s` with unit `Pa/(m3/s)`, not pressure `Pa`;
- the concave magnitude observable is absolute transfer level `dB re 1 Pa/(m3/s)`, i.e. `20*log10(|P_T/Q_T|/(1 Pa/(m3/s)))`;
- `Q_T(f)` must be finite and non-zero at every scored frequency; otherwise evidence is invalid/BLOCKED rather than replaced by an invented response.

The common `dt` factor cancels in `P_T/Q_T`, but it is still part of the written authority so implementations cannot disagree on Fourier/normalization units. For PFFDTD specifically, native `u` is treated as velocity potential and HTDT first forms the finite physical pressure record with the declared `p=rho*d(phi)/dt` adapter conversion (second-order centered interior derivative with second-order one-sided endpoints), then applies the common direct DTFT. HTDT does **not** substitute `-i*omega*rho*Phi_T` for the DTFT of a truncated finite pressure record, because finite-window endpoint terms would make those quantities non-equivalent.

Portal continuity and the explicit radiation-termination analytical reference are **not** inferred to be finite-record fixtures merely because they carry timing metadata. Applicability is explicit: a fixture uses finite-record transfer semantics only when `comparison.finite_record_transfer` is present.

Revision 4 intentionally changes the manifest semantic SHA-256. Earlier R100B artifacts remain historical evidence and must replay under the R100A-4 hash before they can participate in a current adoption decision.

## Purpose

R100A freezes the **solver-neutral comparison authority** before any FDTD, FEM, BEM or geometric-acoustics candidate becomes a product dependency.

The manifest is not a solver implementation and does not claim owned-room validation. It defines the physical and numerical problem that R100B adapters must consume without silently changing geometry, openings, materials, source normalization, receiver timing, environment, comparison rules or resource budgets.

The implementation is intentionally backend-independent:

- `AcousticRegion` owns modeled air volumes.
- `AcousticPortal` explicitly connects two modeled regions with pressure/velocity continuity semantics.
- `BoundaryTermination` explicitly terminates an opening when the adjacent volume is not modeled; radiation termination carries an exact model/sign/normal/impedance authority rather than a label alone.
- `AcousticObstacle` represents participating solid/thin objects separately from editor visibility.
- wave material capability and geometric material capability are independent.
- phase-bearing wave impedance is explicit complex authority; it is never synthesized from scalar absorption.
- source excitation/normalization, receiver calibration/timing and environment, including explicit air density, are part of each fixture.
- coordinate system, Fourier sign, time zero, precision, interpolation, frequency/time sampling, window and filter are fixed in the numerical comparison contract.
- expected observables carry quantity-specific tolerances.
- correctness hard gates are separate from compile/solve/postprocess/resource budgets.

## Canonical fixtures

The first manifest revision contains ten common fixtures.

| Fixture | Role | Gate |
|---|---|---|
| `wave-rigid-rectangular-modes-v1` | wave | analytical rigid rectangular eigenmodes |
| `wave-rectangular-convergence-v1` | wave | grid/mesh refinement and transfer convergence |
| `wave-normal-incidence-impedance-v1` | wave | closed-form complex reflection from explicit impedance |
| `wave-concave-l-room-v1` | wave | exact concave polygon-prism comparison; no rectangularization |
| `wave-portal-split-room-v1` | wave | region split / Portal continuity invariance |
| `wave-explicit-radiation-termination-v1` | wave | explicit opening termination; no invented adjacent space |
| `geometric-direct-first-reflection-v1` | geometric | direct delay and first specular reflection geometry |
| `geometric-reflecting-counter-v1` | geometric | participating reflecting obstacle changes path authority |
| `geometric-seed-repeatability-v1` | geometric | stochastic seed/repeatability contract |
| `hybrid-overlap-continuity-v1` | hybrid | compatible overlap continuity without invented phase/data |

R150 will still need ray-count, receiver-estimator/radius, time-bin and termination convergence beyond same-seed repeatability. Likewise, FR acceptance does not make frequency-domain IR synthesis or decay metrics accepted automatically.

## Hard gates

R100B candidates are evaluated only after the applicable hard gates are satisfied:

1. physics correctness against the fixture's declared reference/tolerance;
2. CPU correctness baseline without GPU dependency;
3. reproducible Windows 11 x64 packaging/embedding;
4. license/redistribution compatibility;
5. required physics/output capability;
6. reproducible authority including backend/version/precision/grid/seed/approximation controls.

Speed and memory are comparison criteria **after** hard correctness/capability gates. A fast solver that fails a physics or authority gate is not promoted.

## Resource contract

Each fixture records an initial bounded R100B workload:

- CPU thread budget;
- RAM and disk budget;
- candidate count;
- maximum compile time;
- maximum solve time;
- maximum postprocess time;
- maximum produced output size.

These values are benchmark decision limits, not numerical accuracy tolerances. R100B records measured values and may propose a versioned manifest revision if evidence shows a budget itself is inappropriate; adapters must not silently weaken the current manifest.

## Identity

`AcousticBenchmarkManifest.canonical_json()` uses sorted, non-NaN canonical JSON. `semantic_hash()` is SHA-256 over that exact canonical representation.

R100B evidence must bind to the manifest semantic hash plus candidate/reference backend identity. Editing geometry, material data, source/receiver/environment, tolerance, seed or resource budget therefore changes authority identity.

## Fail-closed behavior

The Pydantic authority models reject, among other cases:

- dangling region/face/material/boundary/source/receiver references;
- a Portal connecting a region to itself;
- impedance termination without an impedance boundary;
- radiation termination without explicit boundary/model/sign/normal/characteristic-impedance authority;
- `wave_impedance` fixture capability without explicit phase-bearing impedance data;
- geometric scattering capability without explicit non-zero scattering bands;
- stochastic ray capability without a fixed seed;
- analytical/closed-form observable without expected samples;
- peer-comparison observable referencing an unknown fixture;
- malformed frequency/time/tolerance authority;
- R100A-4 finite-record transfer attached to any fixture outside the exact rectangular-convergence / concave set, fixed common `time_step_s`, wrong source phase/normalization, or missing `[0,T)` / direct-DTFT / `P/Q` semantics.

Unsupported physics stays unsupported. No default material, anechoic exterior, rectangular room, coherent phase or zero response is invented to make a candidate pass.

## Next implementation gate

R100B consumes this manifest through candidate/reference adapters and records:

- backend/package/version and license evidence;
- required compiled representation;
- Windows packaging result;
- CPU numerical evidence per applicable fixture;
- compile/solve/postprocess time and peak/resource evidence;
- supported and unsupported result capabilities;
- adoption decision or no-go/next-limited-experiment ADR.

FDTD-first remains evaluation order, not a preselected production solver. Existing OSS reuse/adapter/port is preferred over a new kernel unless benchmark evidence establishes a concrete gap.

## Verification boundary

R100A verification is repository/CI based and requires no Remote Desktop Commander. It validates the authority schema, canonical manifest, cross-references and fail-closed semantics. Numerical solver accuracy starts in R100B/R130; owned-room evidence remains gated by R180/O60 and is not created by these fixtures.
