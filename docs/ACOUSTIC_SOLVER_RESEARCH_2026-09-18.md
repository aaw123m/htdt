# Arbitrary-room acoustics research and implementation decision — 2026-09-18

Tracking: Issue #101  
Status: R100A executable fixture/tolerance authority is merged in PR #110. R100B authority is merged in PR #111; pyroomacoustics direct/first-reflection reference evidence is merged in PR #112; PFFDTD pinned Python/Numba Windows platform feasibility is merged in PR #113. PR #115 run `35349358027` passes the R100A rigid rectangular eigenfrequency fixture with three-level grid refinement, and PR #114 provides an independent MFEM v4.10 rigid-room eigenmode reference PASS. Transfer-response convergence, complex impedance, concave/portal evidence, external measured validation, packaging and the solver-selection ADR remain open, so the production backend/crossover/shipping dependencies remain benchmark-gated.

## 1. Decision summary

HTDT should not extend the current rectangular predictor into a silently approximate general-room model. The arbitrary-room path will be a **hybrid, multi-fidelity acoustic engine** with separate authorities for wave acoustics and geometrical acoustics.

The implementation baseline is:

1. **20–300 Hz wave domain**: evaluate a reusable FDTD CPU path first against an independent FEM/reference. This is an evaluation order, not a requirement to author a new kernel or select FDTD for production. Deliver useful low-band prediction and its measurement loop before requiring the broadband hybrid stack.
2. **Mid/high frequency domain**: geometrical acoustics with deterministic direct/specular paths first, then stochastic/diffuse ray energy where justified.
3. **Hybrid result**: combine low-band coherent wave results and upper-band geometric results only inside an explicit overlap/crossover contract. Do not fabricate coherent phase for a stochastic late-field result that does not contain it.
4. **Material authority**: separate energy-domain absorption/scattering from wave-domain complex impedance/admittance. Do not derive a unique phase-bearing impedance from a scalar absorption coefficient without an explicit model.
5. **Source/receiver authority**: keep physical cabinet orientation, acoustic aim, source excitation/level reference and frequency-dependent directivity separate. Receiver position/orientation/calibration/timing authority is equally explicit and binds to the prediction.
6. **Execution**: CPU fallback is mandatory. GPU is an accelerator, not a correctness dependency. Candidate-level parallelism, solver-internal threading and GPU execution must be scheduled together to avoid oversubscription.
7. **Validation**: analytical/reference cases precede owned-room validation of the same capability. R180A validates a low-band model after R170A; R180B later validates hybrid/extended capabilities. Simulation alone never opens a production recommendation gate.

This preserves the existing HTDT evidence model: measured, predicted, derived and hypothesis remain distinct, and production recommendation still requires O60 owned-room evidence.

## 2. Why this architecture

### 2.1 Full-wave over the whole audible band is not a practical default

Mesh/grid resolution scales with the shortest wavelength. A single 20 Hz–20 kHz full-wave model would force the whole room to use high-frequency spatial resolution and becomes wasteful in both memory and compute. The engineering response is a banded/hybrid model rather than pretending one discretization is efficient everywhere.

20–300 Hz is a feasibility target, not a demonstrated consumer-PC performance guarantee. R100B must measure representative room/adjacent-region size, boundary state, simulation duration and requested output on a declared CPU/RAM budget before promising interactive or batch performance.

### 2.2 FDTD first, but not as an irreversible commitment

A structured-grid time-domain solver is the first PoC because it has:

- simple deterministic geometry compilation to a voxel/grid representation;
- excellent data parallelism;
- natural broadband impulse-response output from one time-domain run;
- good suitability for consumer GPUs;
- straightforward reuse of a fixed room/material grid for many receiver probes.

Its main risks are staircase error, thin-surface representation and correct frequency-dependent impedance boundaries. Those risks are explicit benchmark gates.

A frequency-domain FEM implementation remains the main independent reference/alternative because it handles irregular boundaries and impedance conditions naturally. HTDT will benchmark FEM rather than assuming FDTD wins every case.

### 2.3 BEM/FMM is a secondary candidate

Boundary-element methods avoid volume meshing and are valuable for radiation/open-boundary problems. For enclosed room optimization they add dense-operator/interior-resonance complexity and are not the first shipping path. They remain useful as an independent reference for selected fixtures if packaging and performance prove acceptable.

## 3. Candidate software / OSS

The table classifies projects by the role they can realistically play in HTDT. “Reference” does not mean “ship as a dependency”.

| Project | Relevant capability | HTDT classification | Main concern |
|---|---|---|---|
| MFEM | C++ FEM, OpenMP/CUDA/HIP/OCCA/RAJA backends, BSD-3-Clause | **High-value FEM reference / possible backend** | Windows packaging and acoustic-specific implementation still need PoC |
| PETSc/SLEPc | sparse linear/eigen solvers, CPU/GPU backends | **Reference / optional solver infrastructure** | native Windows/HPC packaging is materially more complex |
| Gmsh | CAD/mesh generation, Windows SDK, Python/C/C++ APIs | **Meshing candidate subject to license review** | distribution/license terms must be resolved before embedding |
| FEniCSx | modern FEM formulation stack | **Research/reference** | PETSc/MPI dependency and Windows packaging make it a poor first desktop dependency |
| Bempp-cl | Python BEM with OpenCL/Numba paths, MIT | **Secondary BEM reference** | ecosystem/packaging and room-specific robustness need proof |
| k-Wave | k-space time-domain acoustics, C++ OpenMP/CUDA executables, LGPL | **Algorithm/performance reference** | MATLAB-centric workflow and copyleft/distribution implications |
| pyroomacoustics | C++ image-source/ray tracing, general polyhedral/non-convex rooms, MIT | **Geometric-acoustics PoC/reference** | not a low-frequency full-wave authority; directivity support has method/geometry limits |
| Embree | high-performance ray intersection kernels, Apache-2.0 | **Strong CPU ray-intersection candidate** | acoustics transport/scattering/energy bookkeeping still belongs to HTDT |
| Steam Audio | real-time propagation components | **Architecture/reference candidate** | game-audio assumptions must be separated from engineering room-prediction authority |
| OpenFOAM acoustics | finite-volume CFD/acoustics ecosystem | **Not first-line** | too much CFD/runtime complexity for a small-room desktop predictor |
| COMSOL / ANSYS | mature FEM/multiphysics commercial references | **Independent benchmark/reference only** | closed commercial dependency |
| ODEON / CATT / EASE / Treble | mature room-acoustics workflows | **UX/architecture/reference only** | closed implementations; methods/results cannot be assumed equivalent |

Important corrections to the previous roadmap:

- pyroomacoustics can remain useful for general-polyhedral image-source/ray-tracing work and RIR prototyping, but it must **not** be treated as the exact low-frequency predictor for a non-rectangular room.
- REW Room Simulator remains a rectangular baseline/reference path only.
- GPU support is selected per solver/backend; HTDT must not make CUDA/NVIDIA the only valid execution path.

## 4. Verified current implementation facts used by this decision

The research used project/official documentation current on 2026-09-18. Examples:

- MFEM documents CPU, OpenMP, CUDA and HIP-capable backends and is BSD-3-Clause:
  - https://mfem.org/
  - https://github.com/mfem/mfem
- PETSc documents CUDA/HIP/Kokkos/OpenCL backends, while its Windows documentation describes Windows HPC setup as more difficult:
  - https://petsc.org/main/install/windows/
  - https://petsc.org/main/overview/gpu_roadmap/
- Gmsh provides Windows binaries/SDK and C/C++/Python/Julia/Fortran APIs:
  - https://gmsh.info/
  - https://gmsh.info/dev/doc/texinfo/gmsh.html
- pyroomacoustics contains C++ image-source/ray-tracing support for 2D/3D and general polyhedral rooms, and exposes absorption/scattering materials:
  - https://github.com/LCAV/pyroomacoustics
- Embree is an Apache-2.0 ray-intersection kernel library:
  - https://github.com/RenderKit/embree
- k-Wave provides C++ OpenMP and NVIDIA CUDA executables and is LGPL:
  - https://www.k-wave.org/
  - https://www.k-wave.org/license.php
- Bempp-cl is MIT and uses Numba/OpenCL-oriented BEM kernels:
  - https://github.com/bempp/bempp-cl

Exact third-party version pins and redistribution obligations are **not** frozen by this document. They are part of the R100 license/package gate and must be rechecked at the commit/version actually selected.

## 4A. Deep Research refinement: method scope and corrections

The follow-up Deep Research broadens the R100 comparison without changing the core architecture. The important outcome is **not** that one solver has already won; it is that the bakeoff must compare methods on the same physical fixtures and desktop-product constraints.

### Low-band method scope

R100 must consider the following method families explicitly:

| Method family | R100 role | Why it matters | Primary risks / reasons not to preselect |
|---|---|---|---|
| Structured-grid FDTD / related time-domain finite differences | **Primary PoC** | Broadband IR in one run, simple data-parallel update, natural GPU mapping, repeatable grid compilation | numerical dispersion, staircase geometry, thin-surface loss, impedance-filter stability, memory/time-step cost |
| Frequency-domain FEM | **Primary independent reference / production alternative** | irregular geometry and impedance boundaries fit naturally; strong reference value for convergence studies | repeated-frequency sparse solves, meshing cost, preconditioner/packaging complexity |
| Higher-order / DG / spectral-element variants | **Research/secondary bakeoff** | may reduce dispersion per DOF and improve complex-geometry fidelity | implementation and dependency complexity may outweigh benefit for a Windows personal application |
| BEM / FMM-BEM | **Secondary reference** | attractive for boundary/radiation/opening problems and independent cross-checks | dense/hierarchical operators, interior resonance handling, packaging/performance complexity |
| PSTD / k-space approaches | **Algorithm/performance reference** | very low dispersion in suitable domains and useful as a numerical reference | global transforms / regular-domain assumptions and desktop integration constraints |
| Modal/eigenmode methods | **Verification / acceleration candidate** | excellent for analytical box checks, low-mode inspection and possible reduced-order acceleration | not a general replacement for arbitrary geometry/material broadband prediction |
| Digital waveguide mesh / generic finite-volume acoustics | **Background only unless a concrete implementation proves an advantage** | related time-domain formulations can illuminate dispersion/stability trade-offs | no current evidence that they should displace the primary FDTD/FEM bakeoff |

No universal “N points/elements per wavelength” rule is accepted as the production validity criterion. Such rules can seed a starting resolution, but the declared valid band must come from **backend-specific convergence measurements**.

### Geometrical-acoustics scope

R150 should separate transport mechanisms rather than treating “ray tracing” as one authority:

- deterministic direct path and image-source/specular paths where geometry permits;
- general-polyhedral stochastic ray tracing for higher-order transport;
- explicit scattering/roughness energy models;
- diffraction only when an implemented method has a stated validity range and verification fixture;
- late reverberant energy as an energy-domain result unless a method explicitly provides a defensible coherent phase model.

Beam/cone/pyramid tracing, edge-diffraction methods such as UTD/BTM, phonon/particle mapping and other advanced transport techniques remain candidates for later bakeoff if direct/specular + ray transport cannot meet the acceptance fixtures.

### Hybrid implication

The crossover must be an **overlap contract**, not a hard-coded 300 Hz switch. R160 must retain per-band solver provenance and measure the overlap for:

- magnitude/energy continuity;
- arrival-time continuity;
- decay consistency;
- phase validity only where both inputs actually contain coherent phase.

The room's modal/transition context, wave-solver convergence ceiling, source-data validity and geometric-method validity all constrain the overlap.

## 4B. R100 concrete software shortlist

The first bakeoff should stay deliberately small enough to finish, while keeping independent references:

| Layer | First-line shortlist | Secondary/reference | Explicit non-role |
|---|---|---|---|
| Wave time-domain | existing FDTD CPU engine/adaptor, with PFFDTD evaluated for reuse as well as reference | a minimal HTDT teaching/reference kernel only if an explicit reuse gap warrants it; k-Wave for comparison | do not require a new kernel or adopt CUDA-only correctness |
| FEM | MFEM-based acoustic prototype, with Gmsh or another controlled mesh path if needed | FEniCSx / PETSc / SLEPc as research or solver-infrastructure references | do not make a heavy HPC stack a desktop dependency before packaging evidence |
| BEM | none as first shipping path | Bempp-cl / FMM-BEM research fixtures | do not block R130 on BEM |
| Geometric | pyroomacoustics for general-polyhedral RIR/reference cases; Embree as CPU intersection-kernel candidate | Steam Audio / Wayverb as architecture/behavior references | do not treat game-audio or GA output as low-band full-wave authority |
| Commercial | none as dependency | COMSOL/ANSYS/Actran/VA One for independent numerical comparison when available; ODEON/CATT/EASE/Treble for workflow/GA comparison | do not copy closed implementation assumptions into HTDT authority |

R100 must record exact version/commit, license, redistribution implications, Windows build/install path and backend availability for every candidate actually executed. A project name in a research matrix is not enough to approve a dependency.

R100B evaluates direct reuse, a thin adapter, a maintained port, then a new kernel only for a documented unmet requirement. [PFFDTD](https://github.com/bsxfun/pffdtd) documents CPU execution and frequency-dependent boundaries but a Linux-oriented setup; neither native Windows readiness nor the need to reimplement it has been demonstrated here. Compare total integration/maintenance and packaging cost as well as solve time. If no candidate passes, publish a no-go/next experiment ADR with failed gates; do not force a production selection or keep the bakeoff open indefinitely.

### Deep Research claims intentionally not promoted to architecture facts

The follow-up report contained several broad ecosystem summaries. The roadmap must **not** turn those into requirements without direct verification. In particular:

- do not classify a package as FEM/BEM/FDTD merely because it appears in an acoustics ecosystem list;
- do not equate a partitioner or mesher feature with GPU solver support;
- do not assume a published GPU speedup transfers to HTDT's room sizes, boundary models or consumer hardware;
- do not use a fixed calendar/Gantt date from a research report as the implementation schedule;
- do not use a single elements-per-wavelength heuristic as an acceptance gate.

These remain R100 measurements or source-verification tasks.

## 4C. R100 benchmark and decision matrix

Every primary candidate must be judged against the same fixture families.

**Physics / accuracy**

1. rigid rectangular analytical modal frequencies;
2. grid/mesh convergence and declared valid upper frequency;
3. single impedance boundary with known/reference reflection behavior;
4. L-shaped / concave room;
5. explicit Portal to a modeled adjacent region or an explicit BoundaryTermination;
6. large reflecting obstacle / counter-like geometry;
7. receiver reciprocity/symmetry cases where the formulation permits them.

**Geometry / materials**

- self-intersection/non-manifold/open-boundary diagnostics;
- thin-surface disappearance at coarse resolution;
- material-only change must alter semantic cache identity and prediction;
- no scalar absorption -> unique complex impedance conversion without an explicit model.

**Geometric acoustics**

- direct-path delay;
- first-order reflection point/path length;
- occlusion;
- seeded stochastic repeatability;
- energy-decay behavior;
- diffraction/scattering only when an implemented model can be independently checked.

**Product / execution**

- clean Windows setup or build reproducibility;
- license and redistribution review;
- CPU-only execution;
- CPU thread scaling and oversubscription behavior;
- GPU result within declared numerical tolerance when a GPU backend exists;
- RAM/VRAM estimate versus measured peak;
- cancellation/stale-result behavior;
- cache/resume identity and no duplicate completed candidate work.

R100 produces a decision record with at least: accuracy, runtime, peak RAM/VRAM, setup/build complexity, license, failure modes, valid band and unresolved risks. Production selection is made from this evidence, not from theoretical elegance alone.

## 4D. Plan re-review: benchmark authority before solver bakeoff

R100 is an umbrella with two ordered sub-gates.

### R100A — benchmark authority / fixture contract

Before any solver is compared, define a minimal solver-neutral benchmark representation. This is **not** the final product schema and must stay smaller than R110.

It must represent only what is required to compare candidates without giving each backend a different physical problem:

- acoustic regions / air volumes;
- surfaces with stable fixture IDs;
- explicit portals/openings between regions;
- boundary termination when a portal intentionally leaves the modeled domain;
- source excitation/reference point;
- receiver point/orientation;
- boundary/material model identity;
- environment/air state;
- expected analytical/reference observables and tolerances.

A raw hole in a wall is not a complete acoustic boundary condition. For an opening, the fixture must say whether it connects to an explicitly modeled adjacent air region or to an explicit termination model. Unknown space beyond an opening is never silently converted to an absorbing boundary.

R100A also separates **hard gates** from comparison metrics.

Hard pass/fail gates include:

- physically correct fixture interpretation;
- required geometry/boundary capability for that candidate role;
- demonstrated convergence for the quantity under test;
- reproducible Windows build/run path for shipping candidates;
- acceptable license/redistribution status for shipping candidates;
- CPU correctness path for any shipping wave solver.

Only candidates that pass the applicable hard gates are compared on runtime, RAM/VRAM, implementation complexity and acceleration potential.

### R100B — solver bakeoff / ADR

Run FDTD, independent FEM/reference and geometric reference candidates against the same R100A fixtures. R100B produces the solver-selection ADR and records rejected/secondary candidates with evidence.

This ordering prevents the FDTD and FEM prototypes from each inventing incompatible geometry, opening or material semantics before the production AcousticSceneSnapshot exists.

### External empirical benchmark lane

Analytical solutions and independent numerical references remain the primary correctness gates, but HTDT should also maintain an **external measured benchmark lane**. BRAS (Benchmark for Room Acoustical Simulation; Brinkmann et al., Applied Acoustics 176 (2021) 107867, DOI 10.1016/j.apacoust.2020.107867) is the first reference dataset to evaluate because it was designed to compare room-acoustics simulations against documented measured transfer functions and includes cases isolating reflection, scattering and diffraction behavior.

Use this lane with the following scope:

- R100B may use reproducibly pinned BRAS scenes as additional candidate-comparison evidence when the candidate role and available benchmark inputs match. Failure to map a BRAS scene without inventing missing physics is reported as unsupported, not bypassed by retuning the benchmark.
- R130/R150 capability acceptance should include at least one external measured case relevant to the shipped claim before that capability is treated as production-validated. Analytical and cross-solver fixtures are not replaced by measurement.
- Persist benchmark dataset/version/hash, imported physical assumptions, measurement uncertainty and any preprocessing in the evidence record.
- Do not tune a backend-specific material/source parameter against the same benchmark trace and then report that trace as independent validation. Calibration and validation evidence remain separate.
- Owned-room R180 evidence is still required. External benchmarks validate the method; the owned-room campaign validates applicability to the user's actual room and measurement chain.

This yields four distinct validation layers: **analytical/reference → independent solver → external measured benchmark → owned-room measured holdout**.

### Numerical observable contract and staged gates

R100A must fix units and conventions before solver comparison: source quantity and normalization (for example volume velocity and pressure transfer), density/sound speed, Fourier/phase sign, time zero, peak/RMS convention, boundary-normal direction, and the physical source/receiver coordinates. Each adapter records injection/sampling interpolation and coordinate error; moving probes to convenient grid nodes silently is prohibited.

A rigid **lossless closed** room fixture tests eigenfrequencies, symmetry and finite-time energy behavior. Its steady forced response at an excited eigenfrequency is not a finite reference FR, and its decay is not a finite RT60. Compare FR/phase either away from poles under an explicitly shared formulation, or with a separately specified loss/regularization model; compare finite-time IRs with the same excitation, observation duration and processing. Never introduce hidden damping just to make solvers agree. [COMSOL's enclosed-space example](https://www.comsol.com/blogs/how-to-model-fundamental-sources-in-enclosed-spaces) documents the lossless resonance issue.

The manifest also fixes:

- spatial resolution, time step/stability condition or frequency sampling, duration, precision, solver residual/termination criteria and convergence refinement sequence;
- excitation spectrum and normalization/deconvolution, analysis window, resampling/filtering, frequency grid and output units;
- observable-specific absolute/relative tolerances and reference provenance; near-zero pressure uses an absolute floor and phase-validity mask instead of unbounded relative/dB/phase error;
- independently generated analytical/reference outputs and their own convergence evidence; sharing a buggy compiler must not be the only cross-solver check.

Zero padding does not replace a longer observation for resolving nearby modes. Modal-frequency accuracy alone does not establish transfer-amplitude, phase or decay accuracy. Time step, duration, precision and post-processing belong to result identity and resource estimates as well as the benchmark report.

R100A specifies the fixture families, initial hard tolerances and a bounded workload before R100B evaluation: room/adjacent-volume sizes, required band/observables, source/receiver counts, solve duration, candidate workload and declared CPU/RAM/disk limits. Record numerical error budgets and end-to-end compile/solve/postprocess latency and peak-memory acceptance values in the manifest before performance ranking. Values remain an R100A deliverable; this planning review does not invent a measured hardware target.

R100A specifies these tolerances before candidate evaluation. A reference-derived tolerance may be finalized after a documented reference convergence study, but must be versioned and frozen before candidate acceptance; do not relax it retrospectively to pass a candidate.

| Gate | Evidence required at this stage |
|---|---|
| R100A | Versioned fixture/observable/tolerance manifest, role-to-fixture applicability and analytical/reference provenance plan. No production kernel or GUI required |
| R100B | Executed primary PoCs on their applicable fixtures, reference convergence, initial Windows/CPU resource envelope and ADR. Report unsupported, failed and deferred separately |
| R110–R130 | Product persistence/editor/compiler, stale/cancel/resource preflight and wave boundary fixtures for each delivered capability |
| R170A / R180A | Low-band result adapter and CPU batch/measurement loop, then its own preregistered owned-room validation; no R150/R160 dependency |
| R140 / R150 / R160 / R170B / R180B | Scheduler acceleration, geometric/hybrid and extended optimization fixtures, then validation of those additional capabilities |


Later product integration or hybrid acceptance is not a prerequisite for completing R100B. A shipping candidate must demonstrate the required CPU/Windows path; an independent reference may use another platform when its reproducible environment and exported results are recorded. GPU absence is an explicit not-applicable acceleration comparison, never a waiver of CPU correctness.

## 5. Acoustic data model

### 5.1 AcousticSceneSnapshot

Compile an immutable solver snapshot from an exact SceneRevision.

At minimum bind:

- SceneRevision ID/content hash;
- semantic acoustic geometry hash;
- compiled representation hash when a backend-specific mesh/grid/BVH exists;
- acoustic compiler ID/version/tolerance/simplification policy;
- surface/object material assignment hash;
- source excitation + directivity model hash;
- receiver model/calibration hash and receiver set;
- environment/air-state hash used for sound speed/air loss;
- solver ID/version/backend;
- numerical resolution and frequency validity;
- approximation/simplification rules;
- random seed when stochastic algorithms are used.

The semantic acoustic geometry hash describes the physical acoustic interpretation. The compiled representation hash describes the exact generated grid/mesh/BVH and therefore changes when compiler version, tolerance, meshing/voxelization parameters or backend representation changes.

The snapshot is separate from the editable CAD scene. Editing the scene invalidates/stales the snapshot; it never mutates an existing prediction.

### 5.2 Geometry representations

A canonical acoustic region/surface model feeds backend-specific compiled forms:

~~~text
SceneRevision
  -> AcousticRegion(s)
      -> surfaces / objects / explicit Portal(s) / BoundaryTermination(s)
      -> canonical acoustic triangle/surface representation
          -> selected wave representation: FDTD grid OR FEM volume mesh
          -> independent reference representation when required
          -> ray-tracing BVH when the geometric path is implemented
~~~

This prevents each solver from inventing its own interpretation of doors, openings, furniture and materials. An opening is not represented only as “missing wall geometry”: it either connects two modeled acoustic regions or carries an explicit termination model. Unmodeled adjacent space remains unknown/unsupported rather than being silently treated as anechoic.

The compiler must detect and report:

- self intersections;
- non-manifold surfaces where a backend requires manifold geometry;
- open boundaries not explicitly marked as an opening;
- zero-area/degenerate faces;
- thin surfaces that would disappear at the selected wave-grid resolution;
- simplification error.

An internal open Portal in the same air medium couples pressure and normal volume flow; it is not an extra absorbing wall. Model the aperture/reveal geometry once, remove duplicate coincident walls at the connection, and retain actual closed-door panels as surfaces/solids or explicitly supported transfer boundaries. An exterior truncation/termination has its own radiation/reflection model and validity, not merely a label saying “open”.

R100A/R120 include partition-invariance (one volume versus two connected regions with no physical divider), closed-versus-open door, and flux/energy balance cases. An aperture with a finite wall must retain its obstruction and diffraction geometry. This physical contract follows the distinction between [interior continuity](https://doc.comsol.com/6.3/doc/com.comsol.help.aco/aco_ug_pressure.05.096.html) and [transfer impedance](https://doc.comsol.com/6.3/doc/com.comsol.help.aco/aco_ug_pressure.05.114.html); the exact fixture tolerances are HTDT decisions.

No automatic rectangularization is permitted.

### 5.3 Product input and acoustic participation

The current SceneDocument has a single RoomPrism; the existing editor does not already author arbitrary connected air volumes. R110 must define a persisted, versioned acoustic configuration linked to the exact SceneRevision, plus authoring of material assignments, source/receiver/environment properties, adjacent-region geometry and portal/termination choices. R120 must compile this configuration and retain surface-to-Scene IDs for diagnostics/selection. Save/reopen, Undo/Redo, stale invalidation and old-scene loading are acceptance cases. Old scenes open with unresolved acoustic inputs; they do not acquire invented materials or adjacent rooms.

R120A's first supported product geometry is an explicit subset: concave polygon prisms, supported object surfaces/volumes and connected prism regions or declared terminations. R120B is the tracked general-3D extension: a documented oriented polyhedral surface/air-volume representation, including stepped/sloped ceilings and faceted curved boundaries with saved approximation tolerance. It must supply a native authoring or explicit import-to-SceneRevision path, material/surface IDs and visual diagnostics; a standalone backend mesh is insufficient. Curved CAD kernels and every interchange format are not required. Shapes outside the declared representation remain unsupported, but completion of the prism slice must not close the general-3D requirement.

Separate visibility from acoustic participation. Hiding a sofa or locking a cabinet does not remove its acoustic boundary. Source and receiver markers are not automatically solid obstacles. For each physical entity record whether/how its volume or thin surface participates, including any deliberate omission.

When a loudspeaker cabinet participates as an obstacle, its movement/rotation changes the acoustic domain. Define emitter/baffle coupling or a verified equivalent model; a point source trapped inside a sealed rigid cabinet is invalid. Free-field directivity data that already include cabinet radiation must not receive the same cabinet effect twice without a validated coupling model. Unsupported combinations fail closed.

### 5.4 Thin objects

Rugs, curtains and thin panels should not be forced into thick furniture volumes.

Support a surface/boundary representation with explicit thickness/model metadata when the solver needs it. If a thin object cannot be represented at the selected grid resolution, the solver must either use an equivalent boundary model with provenance or report it as unresolved.

## 6. Material model

Two different physical domains are required.

### Geometrical acoustics

Use banded energy coefficients such as:

- absorption;
- specular/diffuse scattering split;
- optional transmission when implemented.

Octave or one-third-octave data are appropriate inputs for this layer.

### Wave acoustics

Use phase-bearing boundary models such as:

- complex surface impedance/admittance;
- rigid/pressure-release special cases;
- frequency-dependent impedance filters for time-domain execution;
- parameterized porous models where justified.

Potential porous models to evaluate include Delany–Bazley/Miki and Johnson–Champoux–Allard-type families. The chosen model and parameters must be explicit.

A scalar absorption coefficient alone is insufficient to reconstruct unique low-frequency phase/impedance behavior. HTDT therefore stores the original measurement/preset type and does not silently convert an energy coefficient into a unique complex impedance.

Material presets must store source, version, applicable frequency range and uncertainty/assumption status. User-measured/custom materials are separate records.

#### Material measurement semantics

A coefficient value is not sufficient authority by itself. Material records must preserve what physical quantity was measured or inferred and under what test conditions. At minimum distinguish:

- normal-incidence absorption and complex impedance/admittance such as ISO 10534-2-type data;
- reverberation-room/statistical absorption such as ISO 354-type data;
- equivalent absorption area for discrete furniture/occupants when that is the reported quantity;
- random-incidence scattering coefficient such as ISO 17497-1-type data;
- directional diffusion metrics such as ISO 17497-2-type data;
- inferred/model-generated impedance or admittance.

Store test method/standard and revision when known, mounting/backing/air gap/thickness/sample dimensions, incidence/angular applicability, supported frequency range/interpolation policy, source/laboratory/report provenance, license and uncertainty/confidence.

These quantities are **not interchangeable**. In particular:

- do not silently reinterpret reverberation-room absorption as complex impedance;
- do not silently reinterpret a diffusion coefficient as the ray-scattering coefficient expected by a particular geometrical-acoustics model;
- do not silently convert equivalent absorption area for a sofa/person into a surface absorption coefficient;
- do not label impedance inferred from scalar absorption as measured impedance.

If HTDT derives phase-bearing impedance from absorption using a porous/material prior, the output is an explicit inferred boundary model with model family, parameters/prior, uncertainty and provenance. The inverse mapping is non-unique, so one fitted value is not physical ground truth merely because it reduces residual error.

Wave-domain material capability must be explicit. At minimum distinguish:

- `measured_complex_impedance`;
- `parametric_impedance_model`;
- `rigid_assumption`;
- `geometric_energy_only`;
- `unknown`.

A material that only has absorption/scattering data may still participate in geometrical acoustics, but it is not automatically valid for a phase-bearing low-frequency wave solve. Choosing a rigid or other equivalent approximation is an explicit model assumption with provenance, not an implicit default.

### Coefficient and interface conventions

Material metadata must distinguish pressure-amplitude from energy coefficients, normal/angle-dependent from random-incidence data, dimensional specific impedance (Pa·s/m) from normalized impedance, and backing/thickness/air-gap conditions. Distinguish one-sided wall impedance from a two-sided sheet/transfer impedance; a freely hanging curtain is not automatically the same boundary as wall-mounted treatment. These distinctions are supported by [COMSOL's impedance specification](https://doc.comsol.com/6.4/doc/com.comsol.help.aco/aco_ug_pressure.05.023.html).

For a passive geometric boundary, reflected + absorbed + transmitted energy must balance incident energy within the declared numerical tolerance. Scattering redistributes reflected energy and is not an extra absorption term. If transmission is not implemented, require an explicitly opaque model or report unsupported; do not silently absorb transmitted energy. In the overlap, wave and geometric representations of a material must implement a documented consistent physical model. Add normal/oblique reflection, scattering-budget and unsupported-sheet fixtures to R130B/C and R150.

## 7. Source / receiver / environment authority

Internal source authority should support:

- source acoustic reference point;
- physical cabinet orientation;
- acoustic aim independent of body orientation;
- source channel/role;
- excitation/level/phase reference needed by the selected solver;
- frequency-dependent magnitude/phase/directivity dataset;
- directivity coordinate frame;
- dataset/version/provenance.

Do not hard-code a particular commercial loudspeaker format into the domain model. Define an internal DirectivityDataset, then add import adapters where licensing/specification allows.

Directivity capability is tiered rather than a yes/no flag:

1. **complex directional TF/IR**: magnitude and phase/time information with coordinate frame, reference distance/normalization, angular sampling, valid band and provenance;
2. **magnitude-only directional data**: suitable for declared energy/directivity uses but does not authorize coherent phase-bearing transfer synthesis;
3. **summary/analytic prior**: CEA-2034-style summaries, simplified polars or analytic source models remain approximations/hypotheses rather than measured 3D source truth.

Interpolation, angular coverage gaps, extrapolation policy and uncertainty are part of the DirectivityDataset authority. SOFA/AES69-compatible source-directivity semantics are a preferred interchange target where the concrete convention fits; generic polar import remains necessary.

Receiver authority is separate from the source and should support:

- receiver acoustic point / microphone capsule position;
- orientation when the measurement or receiver model requires it;
- receiver response/calibration profile identity;
- absolute/relative level reference;
- timing reference and phase-validity state;
- provenance linking to the existing MicrophoneProfile/AcquisitionContext authority when comparing against REW measurements.

Measurement evidence must also declare what comparisons it can support. At minimum distinguish magnitude-valid, relative-phase-valid, common-time-reference/arrival-time-valid, polarity/reference-chain-known and microphone-correction capability. A magnitude-only capture may validate FR magnitude but must not open IR-arrival, coherent-phase or hybrid timing gates. Acoustic timing references are stored with their reference source/path and electronic delay semantics rather than treated as absolute solver emission time.

Environment authority must record the state/model used by the prediction, including temperature and the resulting sound-speed model at minimum; humidity/pressure/air attenuation are added when the selected valid band/model uses them. Measurement validation compares predictions against the environment known or assumed for that campaign rather than silently using a universal 343 m/s constant.

SOFA is a useful general spatial-acoustics interchange candidate. Generic polar CSV/import is also needed because loudspeaker manufacturer data vary. CLF/GLL and other proprietary ecosystem formats require separate format/license review before implementation.

For very low frequencies, an omnidirectional/monopole approximation can be an explicit source model when its validity is documented; it must not silently replace a supplied directional model outside its valid band.

### Room transfer versus the measured playback chain

Store the room transfer separately from source excitation and the playback/measurement chain. For a declared linear model, a receiver pressure is the complex sum `p_r(f) = sum_s H_rs(f) q_s(f)`; the input-channel-to-source routing, gains, delays, crossovers/EQ and source response determine `q_s`. Save their model/version and phase reference. Source batching does not mean all sources may be excited simultaneously when separate transfers are requested. Accept independent source solves or a verified separation method; include two coherent sources with constructive/destructive interference and a bass-routed input fixture.

The first owned-room lane may use an explicitly verified single physical source. Unknown AVR routing/source response does not become unit excitation and must not be “fixed” by fitting wall absorption. Relative FR-shape comparisons remain possible under a preregistered normalization; absolute SPL and coherent multisource/phase comparisons require their stronger reference conditions. Do not equate dB averaging with coherent summation.

For REW comparisons, preserve which microphone/source/electronic corrections are already applied. Acoustic timing is relative to a reference speaker, not automatically the solver emission time; receiver movement changes that reference path. Bind reference-speaker identity/position, timing offset and known electronic delay, or limit the comparison to justified relative quantities. [REW documents this relative timing behavior](https://www.roomeqwizard.com/help/help/html/makingmeasurements.html). Add moved-receiver timing and double-calibration negative fixtures.

## 8. Hybrid result contract

The hybrid output is not one solver pretending to be exact everywhere.

Store per-band provenance:

- wave-valid band;
- geometric-valid band;
- overlap band;
- crossover/stitch algorithm and version.

Result capabilities are declared independently: sampled coherent FR, time-domain IR, eigenmodes, field samples, deterministic paths and stochastic energy are not interchangeable outputs. A frequency-domain FEM path can deliver FR/phase before IR synthesis passes its own gate.

Initial result types are intentionally distinct:

- `CoherentTransfer` / coherent IR from a solver that carries phase;
- `DeterministicPathSet` for direct/early geometric paths;
- `LateEnergyDecay` for stochastic/diffuse energy that does not carry defensible coherent phase.

Initial hybrid design:

1. produce low-band coherent pressure/IR from the wave solver;
2. produce direct/early specular paths from the geometric solver;
3. produce stochastic/diffuse late energy only as an energy-domain result unless the algorithm explicitly defines phase;
4. identify which direct/early physical components are represented by both domains;
5. time-align and crossfade/combine without double-counting those shared components;
6. apply complementary crossover windows only to compatible quantities over a finite overlap band;
7. validate amplitude/energy continuity and impulse timing at the stitch.

Frequency-domain IR synthesis is an explicit derived operation. R100A/R130/R160 must fix the solved frequency range/grid, phase/time reference, normalization, real-signal symmetry and band window. For a uniform grid, the frequency spacing sets the periodic time span (1/Δf); verify sufficient span and no wraparound for the claimed observation. Adaptive/log-spaced frequency samples need a verified reconstruction or resampling method, not an ordinary IFFT applied directly. Zero outside a deliberately selected band defines a band-limited result; missing samples within the claimed band must not silently become zero. Test a known delayed transfer, a damped reference and insufficient frequency coverage. Band-limited IR/ETC is labeled with its filter/range and cannot justify broadband decay/clarity. [COMSOL's FFT solver documentation](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_solver.36.132.html) distinguishes inverse FFT from nonuniform transforms; HTDT's adequacy tests are design requirements.

A `LateEnergyDecay` is not converted into a complex FR merely to make the API uniform. Metrics are enabled only when their underlying result type and time/energy semantics support them.

Crossover is not hard-coded globally to exactly 300 Hz. The default target can start near the wave-solver upper band, but the final crossover must respect:

- selected wave-grid/mesh convergence limit;
- room transition/Schroeder-frequency context;
- geometric solver validity;
- source/material data validity.

If a metric is not valid for a band/result type, the UI disables it instead of extrapolating. Crossover validity is **observable-specific**: magnitude/energy, coherent phase, deterministic arrival timing and late decay may have different usable overlap ranges. A single global transition frequency must not silently authorize all result types.

A usable overlap is not guaranteed. If wave convergence ends below the geometric method's validated lower limit (including occlusion/diffraction applicability), preserve separate band-limited outputs and mark the gap unsupported. R160 may proceed only after extending verified wave coverage, adding a verified bridging method, or explicitly narrowing the requested output. Smoothing across a gap or quoting a Schroeder estimate is not evidence of valid overlap. Add both valid-overlap and no-overlap fixtures.

Deterministic path geometry alone does not authorize coherent pressure: path-to-transfer conversion also needs compatible source normalization, complex reflection/directivity and timing. When those are absent, retain path/energy output and disable coherent summation. Any synthesized stochastic tail is a separately labeled realization with seed/model provenance, not recovered physical phase.

Decay/clarity output needs its own analysis contract: band filters, direct-arrival time origin, observation length, tail truncation/noise treatment, fit interval/quality and energy coverage. Insufficient decay or a missing tail gives unavailable/qualified output, not a misleading finite RT60 or C50/C80. Distinguish local modal decay from diffuse-field reverberation time, especially in the low-band target; [REW's RT60 documentation](https://www.roomeqwizard.com/help/help/html/graph_rt60.html) explains the small-room limitation. R160 acceptance includes a truncated tail and a non-decaying rigid reference.

## 9. Compute architecture

### 9.1 Correctness baseline

Every solver selected for shipping must have a CPU path that can execute the acceptance fixtures. GPU is optional acceleration.

Even the first CPU prototype needs bounded execution: preflight its grid/mesh, boundary/filter state, solve workspace, receiver histories and requested field output against an explicit memory/disk budget; specify duration/work limit and cancellation checkpoints. Product jobs keep the existing stale/document-switch guard from R110/R130. R140 adds automatic hardware discovery and coordinated planning, rather than introducing these basic protections for the first time.

Full space-by-time field storage is opt-in and budgeted. Probe histories, selected snapshots or frequency slices can be requested separately; requested and executed output coverage is immutable. Do not silently drop requested outputs or restart an oversized GPU job on a CPU without rechecking RAM/work limits.

### 9.2 Scheduler hierarchy

HTDT controls both levels:

- outer parallelism: candidate/source batches;
- inner parallelism: solver threads or GPU kernels.

The scheduler chooses combinations that do not oversubscribe cores.

It must estimate RAM/VRAM before execution and persist:

- backend/device;
- worker count;
- solver thread count;
- precision;
- grid/mesh size;
- actual quality/resolution;
- fallback or downgrade reason.

Silent quality downgrade is prohibited.

### 9.3 Backend policy

Do not couple the prediction schema to CUDA, HIP, SYCL or OpenCL.

Use a solver/backend capability contract with functions equivalent to:

~~~text
capabilities()
estimate_resources(snapshot, request)
compile(snapshot, quality)
execute(compiled, source_batch, receiver_batch, cancellation)
provenance()
~~~

The first implementation can optimize NVIDIA hardware if that is the best measured path, but the CPU authority remains usable and AMD support can be added without changing prediction identity semantics.

## 10. Reuse opportunities for optimization

The scheduler should exploit physics/solver structure before merely adding threads.

For fixed geometry/materials:

- reuse voxel/grid boundary compilation;
- reuse ray-tracing BVH;
- reuse FEM matrix/preconditioner/factorization where frequency/formulation allows;
- collect many receivers from one time-domain wave solve;
- use reciprocity where the exact source/receiver model allows it;
- batch sources/receivers instead of restarting setup work per candidate.

Cache identity includes semantic geometry, compiled representation, material, source excitation/directivity, receiver model/set, environment, solver/backend, numerical parameters and frequency band.

Before adding more outer workers, R170 should exploit exact reusable structure when the formulation permits it: receiver batching from one solve, source-equivalence grouping, reciprocity, reusable FEM matrices/preconditioners/factorizations and reusable grid/BVH compilation. These are optimization opportunities, never assumptions applied where source/receiver models break the required symmetry.

### Reuse and candidate-selection acceptance

Reuse depends on the actual operator, not on candidate membership in one search. Source-only or receiver-only movement can reuse fixed compilation when acoustic geometry/material/environment and discretization remain unchanged. Moving/rotating an acoustically participating cabinet, seat or other obstacle invalidates affected geometry/grid/BVH/matrix caches. Changing material, environment, boundary, frequency/formulation or numerical settings invalidates the relevant layers. A receiver sample not retained in an earlier run requires new evaluation or a solve; receiver batching does not imply an arbitrary saved full field.

Separate reusable numerical artifacts from immutable PredictionRun bindings. A cache hit for equivalent physical inputs creates an explicit result association to the requesting exact SceneRevision and candidate; it never relabels an old run as current. Verify reuse versus a fresh solve, and include cabinet rotation, material-only change, receiver-only change and view-only hide as invalidation fixtures.

Coarse fidelity is an explicitly validated model/resolution, not merely a faster setting. It must preserve portal connectivity and required thin/obstacle behavior or report unsupported. A rectangular-only predictor must not eliminate concave-room candidates. Unsupported/missing objectives are not zero, infinity or poor scores.

Screening uses hard geometric constraints for definitive feasibility rejection. Acoustic coarse screening needs documented discrepancy/uncertainty criteria against finer runs, an audit sample of discarded candidates and recovery when ranking reversals are observed. Without a validated error bound it is heuristic shortlisting, not proof that the global Pareto set was retained. Before reporting a final simulated Pareto comparison, recompute retained candidates with a common validated fidelity, band, source/reference and objective spec; mixed-fidelity scores cannot silently establish dominance. Include a coarse/fine ranking-reversal fixture. If the budget cannot support refinement, label the result preliminary and keep the production recommendation gate closed.

### Future reduced-order and adjoint acceleration

After the full-order path has passed its numerical gates, HTDT may evaluate reduced-order and gradient-based acceleration as a **non-blocking research track**.

Evaluate in this order:

1. exact structural reuse from the governing operator: receiver batching, reciprocity where valid, reusable grid/BVH/matrix/factorization and source-equivalence grouping;
2. modal/Green-function or reduced-basis/model-order-reduction methods over an explicitly bounded parameter domain;
3. adjoint gradients for high-dimensional calibration or optimization after the forward and boundary models are validated;
4. generic statistical surrogates/residual models only with explicit training/holdout authority and uncertainty.

CRUNA/Adjointsound (TU Berlin) is a useful research reference for FDTD plus adjoint acoustic optimization/calibration, and open DG room-acoustics projects are useful references for high-order wave propagation. Relevance as research code does not make them product dependencies.

Any reduced model must bind to the high-fidelity training authority/hash, parameter-domain applicability, independent validation or error estimator and invalidation rules. Geometry/material/source changes outside that domain invalidate the reduced model. Published research speedups are not HTDT performance requirements.

## 11. Validation ladder

### L0 — deterministic math/unit references

- hash/canonical snapshot;
- coordinate transform;
- material interpolation;
- directivity interpolation;
- crossover filters.

### L1 — analytical room references

Rigid rectangular room:

- modal frequencies against closed-form analytical values;
- symmetry and reciprocity cases;
- source/receiver coordinate checks.

The initial wave PoC target is <=0.5% modal-frequency error over the declared converged band for the reference box; tighter limits may be adopted after convergence study.

### L2 — numerical convergence

For each backend:

- refine grid/mesh;
- verify result convergence;
- record error versus DOF/runtime/RAM/VRAM;
- establish a backend-specific valid upper frequency.

Do not derive the valid band from a rule of thumb alone.

### L3 — cross-solver fixtures

Compare FDTD against an independent FEM/reference solver for:

- rectangular rigid room;
- impedance boundary;
- L/concave room;
- opening;
- large reflecting object.

Agreement tolerance is defined from convergence error and the quantity being compared, not one global dB threshold.

### L4 — geometric acoustics references

- direct path delay;
- first-order reflection point/path length;
- deterministic image-source cases;
- scattering seed repeatability;
- ray-count/receiver-estimator/time-bin convergence and variation across independent seeds;
- energy decay sanity cases.

### L5 — external measured benchmark

Use BRAS-class externally measured data for the observables the candidate claims to support:

- preserve dataset/version/hash, geometry/source/receiver/material mapping, uncertainty and preprocessing;
- compare transfer/path/decay observables only where the benchmark defines them;
- keep fitted/calibration scenes separate from independent validation scenes;
- report unsupported input semantics instead of inventing a mapping.

External measurement complements analytical/cross-solver validation; it does not replace owned-room applicability evidence.

### L6 — hybrid stitch

- direct-arrival timing preserved;
- no artificial level discontinuity through overlap;
- energy decay continuity;
- no unsupported high-band phase claim.

### L7 — owned room

Reuse the O60 campaign invariants through an explicit new-result adapter, not by pretending its current service already accepts arbitrary solver outputs:

- calibration/holdout separation;
- residual trend;
- sensitivity;
- repeatability;
- candidate separation;
- applicability.

UMIK-1/REW evidence remains the final owned-room validation path.

Current implementation evidence: `cad_model_validation_service.py` reads `roomsim_repository.get_attempt` and converts with `roomsim_attempt_frequency_response`; `cad_validation_campaign.py` accepts four FR objectives. R170A must add a typed prediction-result binding/provider with model/configuration/result hashes and validated-band/reference metadata for O20/O30/O50/O60/O70. Keep the REW adapter and its stored records valid. Do not put wave/GA data into a fabricated RoomSim attempt or mark a FR-only gate as validation of phase, IR, decay or O80 directional behavior.

Validation scope is explicit: model/configuration, observable, frequency band, source/receiver family and room/material applicability. Unsupported new observables need their own metric/measurement adapter and acceptance before recommendation. An eligible low-band FR record enables only its approved objectives; it cannot enable the whole Issue #101 feature set.

Material/boundary calibration is optional model fitting, separate from microphone calibration and O70 residual correction. Preregister fitted parameters/bounds, fixed parameters, fitting objective and training evidence; check sensitivity/identifiability and report non-unique fits rather than claiming recovered physical materials. Freeze the calibrated model/configuration hash and all gain/delay/normalization rules before evaluating holdout, and produce new predictions without overwriting prior runs. The preregistered campaign must represent that calibration-to-frozen-model transition append-only; it must not require a fitted hash before calibration exists or allow an untracked change after holdout.

After holdout results influence model selection/tuning, treat those measurements as development evidence for the next revision and use fresh independent holdout for a new production claim. Do not repeatedly tune against the same “holdout” until it passes. Add model-hash mismatch, recycled-holdout, non-identifiable-fit and FR-pass/phase-unvalidated fixtures.

## 12. Implementation phases

### R100 — research / bakeoff umbrella

#### R100A — benchmark authority / fixture contract

Deliver the solver-neutral fixture representation, hard pass/fail gates, quantity-specific tolerances and shared fixture corpus before comparing implementations.

Minimum fixtures are the analytical rigid box, impedance/reflection boundary, concave/L-room, explicit region-to-region portal or explicit termination, reflecting obstacle, direct/first reflection geometric cases and later overlap-stitch cases.

#### R100B — solver bakeoff / ADR

Deliver:

- this research note plus the Deep Research refinement;
- third-party version/license/redistribution/Windows-package matrix;
- common solver interface and provenance contract;
- reproducible FDTD CPU evaluation using an existing engine/adapter where feasible; new kernel work requires the documented reuse decision in §4B;
- one independent FEM/reference prototype;
- simple geometric-acoustics prototype/reference;
- measured CPU scaling and, when available, GPU-vs-CPU tolerance/resource evidence;
- an explicit decision record naming the selected first production stack and rejected/secondary alternatives with reasons.

Deliver either a selected first production stack supported by applicable hard gates and measured Windows/CI evidence, or a no-go ADR with bounded follow-up experiments. Only the selected-and-passing branch authorizes R110+ product integration. R100 does **not** freeze the final crossover frequency, GPU vendor/backend, universal mesh density or the long-term BEM/DG/PSTD role.

### R110 — acoustic scene/material/source/receiver authority

Implement immutable AcousticSceneSnapshot, AcousticRegion/Portal/BoundaryTermination semantics, acoustic surfaces, object/surface material capability, source excitation/directivity, receiver/calibration, environment authority and prediction provenance schema.

Define semantic acoustic geometry identity separately from backend-compiled representation identity.

No high-cost solver is required to finish the data contract.

### R120 — acoustic geometry compiler umbrella

#### R120A — supported prism geometry and selected backend

Compile exact Scene geometry and R110 region/portal semantics into canonical acoustic regions/surfaces and the representation required by the R100B-selected backend. An FDTD backend requires its grid/boundary map; a FEM backend requires its volume mesh, element/order/quadrature and boundary mapping. A FEM production path does not require an unused FDTD grid, and its mesh is not optional. R150 adds the ray BVH; independent references may compile a different representation of the same physical fixture.

The backend capability manifest must declare required compiled inputs, precision/resolution controls and output observables. Persist these and compiler version/tolerance/approximation with compiled identity. Add diagnostics for non-manifold/unintended-open/degenerate/thin/unresolved geometry. Validate both candidate adapter contracts during R100B, then implement the selected production path; do not require shipping both solvers.

#### R120B — general-3D Scene input and compilation

Extend R110/Scene authoring or explicit import and R120A compilation to the polyhedral/air-volume scope in §5.3. Acceptance includes a stepped-ceiling room, a sloped face, a curved boundary with declared faceting error, connected regions and obstacles; verify save/reopen, surface material identity, volume/topology, compiler approximation and unsupported diagnostics. Re-run the applicable R130/R150 numerical fixtures on the expanded geometry before its results enter R170B/R180B.

R120B exits on its Scene/geometry/compiler contract; numerical capability acceptance follows in R130/R150, so these gates do not depend on each other's completion. R120A unlocks the first low-band slice; R120B need not delay it. R120B remains required for the umbrella's general-3D claim and is not satisfied by silently extruding a footprint.

### R130 — low-band wave solver

Initial target: 20–300 Hz. Split correctness so boundary-model failures are distinguishable from the core discretization.

#### R130A — rigid-boundary core

- deterministic CPU wave solver;
- rigid rectangular analytical modes;
- concave/portal-capable geometry path as supported by the chosen stack;
- multiple receivers;
- FR/phase/IR/spatial field output subject to the numerical observable contract (lossless eigenmodes and finite-time responses are distinct);
- convergence and independent cross-solver validation.

#### R130B — simple lossy / locally reacting boundary

Add the simplest independently verifiable impedance/admittance boundary required by the selected formulation and verify reflection magnitude/phase against reference cases.

#### R130C — causal frequency-dependent boundary

Add the production frequency-dependent boundary implementation only after R130A/B are sound. For time-domain execution, stability/passivity/causality behavior is part of acceptance.

GPU acceleration follows the same physical contract after CPU correctness is established.

### R140 — hardware-aware execution

R110/R130 already establish immutable identity, stale/cancel semantics and backend provenance. R140 **enhances execution**, adding CPU/GPU capability detection, resource estimation, two-level scheduler, candidate/source/receiver batching, oversubscription control and efficient cache/resume.

A CPU-only machine remains supported. R140 must not redefine prediction identity merely to fit a scheduler.

### R150 — geometric acoustics

Implement/choose:

- direct path;
- early specular reflections;
- image-source where valid;
- general-polyhedral ray tracing;
- banded absorption/scattering;
- source directivity;
- deterministic seed/provenance;
- late/diffuse energy only with explicit semantics.

A repeated seed only establishes repeatability. R150 also fixes ray count/launch distribution, energy weighting, receiver estimator/radius, time-bin width, reflection/time/energy termination and estimator normalization in identity. Refine ray count, receiver sampling and histogram/termination settings against reference energy/delay/decay observables; measure variation across independent seeds with tolerances specified before acceptance. Too few arrivals or a budget-stopped estimate remains insufficient, not zero response. Candidate differences within sampling uncertainty must not establish a reliable Pareto preference. [Pyroomacoustics exposes these ray/receiver/histogram controls](https://pyroomacoustics.readthedocs.io/en/stable/pyroomacoustics.room.html); the convergence gate is HTDT's requirement.

### R160 — hybrid broadband result

Implement overlap/crossover and derive only metrics supported by the combined authority:

- FR;
- IR;
- ETC;
- reflection paths;
- spatial field where meaningful;
- RT60/EDT/C50/C80 where the underlying result is valid.

### R170 — optimization integration umbrella

#### R170A — low-band vertical slice

After R110/R120A and the R130 boundary capabilities required by the declared model, connect one wave result through N70 visualization, bounded CPU candidate batch, O30/O40 comparison, O50 MeasurementPlan and N60/O60 validation. Deliver the typed result adapter in §11 and basic identity/cancel/cache/resume semantics. R140 acceleration, R150 geometric acoustics, R160 hybrid and adaptive search are not prerequisites.

The first slice uses validated single-source/receiver and FR objectives where appropriate; 20–300 Hz remains the target, and any narrower delivered band is explicit. Unknown boundary data permits a labeled numerical hypothesis, not owned-room eligibility.

#### R170B — hybrid / multi-fidelity / extended search

After R170A, R140 and R160 (plus the applicable R120A/B geometry gate), add hybrid batch outputs, validated coarse/fine scheduling, reuse in §10, O70 residual/adaptive integration and O80 directional/multiseat/multichannel capabilities where the source/result contracts support them. R170A retains O60/O70 authority bindings but does not have to ship every adaptive feature. For R120B geometry, R170B also adapts candidate feasibility to the actual 3D air volume and physical entity envelopes: a valid XY footprint alone does not permit a source/cabinet above a sloped ceiling or inside an overhang. Keep existing constraints as a broad-phase filter and require verified 3D containment/collision checks before candidate acceptance; otherwise disable that geometry's search. Include stepped/sloped-ceiling rejection fixtures.

### R180 — owned-room validation umbrella

#### R180A — low-band owned-room validation

After R170A and applicable numerical/reference gates, run the preregistered low-band campaign. Validate actual material/source/measurement assumptions early, before investing in the full broadband stack. Unsupported unknowns keep recommendation closed, but do not require R150/R160 to start this campaign. This lane does not certify phase, decay, broadband or directional behavior by passing FR objectives.

Calibration acceptance separates **predictive fit** from **physical identifiability**. Pre-register fitted parameter families/bounds and fixed quantities, report sensitivity/correlation/non-identifiable groups, and allow a valid state such as “predictive model acceptable for this observable, physical material parameters not uniquely identified.” A low residual alone does not certify a fitted wall/material/source parameter. Timing/phase claims require the corresponding measurement-evidence capability. External measured benchmark evidence and owned-room holdout remain distinct.

#### R180B — additional capability validation

After R170B and its numerical gates, validate each additional claimed observable/band/directional capability. Neither an old RoomSim approval nor R180A grants approval to a new hybrid model. Phase/IR/arrival-time, directional and decay claims require matching measurement capability and independent holdout; identifiability is reported separately from fit quality when calibration is used.

Both lanes preserve O60/Issue #83 evidence rules, the model-freeze/holdout policy in §11, and synthetic-versus-owned-room separation. R170/R180 remain umbrella IDs; their A/B slices are not independently renamed completed features.

## 13. Rejected shortcuts

Do not:

- treat pyroomacoustics ray/ISM output as an exact low-frequency wave solution;
- use the room bounding rectangle as the arbitrary-room solution;
- assign one broadband absorption number to every solver band and claim phase accuracy;
- extend wave simulation to 20 kHz just because GPU compute exists;
- require NVIDIA/CUDA for correctness;
- recompute geometry/meshes/BVH for every placement candidate;
- merge predicted data into measured evidence;
- auto-calibrate materials on holdout measurements and then call the same data validation;
- hide resolution downgrade or geometry simplification.

## 14. Next implementation gate (not started)

Implementation remains not started. Resume at **R100A**, delivering the solver-neutral fixture/observable manifest, role-specific hard gates, tolerance policy and reference provenance plan in §4D. Do not start solver kernels first.

After R100A is accepted, R100B delivers one repeatable benchmark command covering the rigid modal reference, concave geometry, CPU wave PoC, independent FEM/reference, geometric direct/first reflection and runtime/memory report, with lossy/portal/obstacle cases according to the candidate's required capability. Record failed and deferred cases explicitly. This is the smallest executable bakeoff that can falsify the architecture; it is not completion of R110–R180 or an owned-room accuracy claim.

## 15. What is frozen now vs. what remains benchmark-gated

### Safe to freeze now

- arbitrary-room support uses a hybrid/multi-fidelity architecture rather than silent rectangular approximation;
- 20–300 Hz is the initial low-band wave target, not a promise that 300 Hz is the final crossover;
- evaluate reusable FDTD first with FEM as the primary independent reference/alternative; a custom kernel and production FDTD selection are not frozen;
- BEM/FMM, DG/high-order FEM and PSTD/k-space remain secondary/reference candidates unless R100 evidence promotes them;
- geometric direct/early paths and stochastic late energy have different semantics;
- scalar absorption, scattering and complex impedance/admittance are distinct authorities;
- CPU correctness/fallback is mandatory and GPU is optional acceleration;
- exact geometry/material/source/solver/backend/resolution provenance is immutable;
- validation is layered: analytical/reference, independent solver, external measured benchmark and owned-room holdout have distinct roles and provenance;
- owned-room production recommendation remains gated by O60/Issue #83-style measured validation;
- good predictive fit does not imply unique physical parameter identification.

### Must remain open until R100 or later

- production wave solver library/framework;
- exact spatial discretization and per-band resolution presets;
- final crossover/overlap frequency range;
- GPU API/vendor and whether a GPU backend ships in the first production release;
- exact FEM mesher/linear-solver stack;
- whether BEM, DG/high-order FEM, PSTD/k-space, reduced-order or adjoint methods graduate from reference/research to product code;
- diffraction model and late-field synthesis method;
- third-party redistribution/version pins;
- numeric CPU/GPU acceptance tolerances and performance budgets.

This split is deliberate: architecture and evidence semantics are stable enough to implement against, while numerical-backend choices remain falsifiable by the R100 bakeoff.
