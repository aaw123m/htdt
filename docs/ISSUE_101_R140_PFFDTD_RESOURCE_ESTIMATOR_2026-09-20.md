# Issue #101 / R140 PFFDTD solver-specific resource estimator — 2026-09-20

## Scope

This slice connects the existing solver-neutral R140 bounded executor to the real R130A PFFDTD Python/Numba CPU candidate workload through a dedicated solver-specific resource estimator.

It does **not** select PFFDTD as a production solver, validate a GPU backend, claim CPU/GPU numerical equivalence, or provide owned-room evidence.

## Boundary

The generic R140 contract remains in `cad_r140_executor.py`.

PFFDTD-specific derivation lives in:

- `backend/src/htdt/cad_pffdtd_resource_estimator.py`
- `PffdtdCandidateResourceEstimator`
- `PffdtdCandidateWorkloadEstimate`
- `PffdtdR140Worker`

The estimator produces two exact objects:

1. a solver-specific workload authority with grid/time/resource derivation and component breakdown;
2. the existing generic `ExecutionResourceEstimate` consumed by R140 admission/scheduling/telemetry.

No PFFDTD physics or allocation formulas are added to the generic executor.

## Exact source binding

Estimator v1 is intentionally pinned to:

- repository: `bsxfun/pffdtd`
- commit: `aa319f6c86517cb95aabfae8656277da62c3ead5`
- backend: PFFDTD Python/Numba CPU
- R130A adapter: exact `CandidateWaveExecutionInput`
- solver configuration: exact `PffdtdCandidateConfiguration`
- runtime identity: exact `CandidateRuntimeIdentity`
- estimator id: `htdt.r140.pffdtd_python_numba_cpu.resource_estimator`
- estimator version: `1`

A different solver configuration, candidate execution input, compiled solver model, runtime/backend identity, or estimator version changes the exact estimate identity.

Estimator v1 refuses a different PFFDTD commit rather than applying stale formulas.

## Grid and simulation-length derivation

The derivation mirrors the inspected pinned source.

For sound speed `c`, maximum frequency `fmax`, and points per wavelength `PPW`:

`h = c / (fmax * PPW)`

PFFDTD `sim_setup(... offset=3.5 ...)` expands each geometry bbox side by `3.5 h`. For each axis extent `L`:

`Naxis = ceil((L + 7 h) / h) + 1`

Cartesian/FCC-disabled Courant scaling in the pinned source is:

`lambda = sqrt(1/3) * 0.999`

`Ts = h / c * lambda`

`Nt = ceil(duration / Ts)`

The workload authority records:

- exact geometry bbox extent;
- grid spacing;
- `Nx, Ny, Nz`;
- total grid cells;
- `Ts`;
- `Nt`;
- `grid_cells * Nt` cell-time workload magnitude;
- receiver count;
- the PFFDTD eight-point receiver-grid trace count.

The estimator also enforces the existing candidate `max_grid_cells`, `max_time_steps`, and raw-output cap before admission.

## CPU semantics

The current candidate is CPU-only.

- `inner_solver_threads` is the explicit candidate `solver_threads` request.
- logical CPU demand is `max(setup_processes, solver_threads)`, not their sum, because setup and solve are sequential phases.
- the outer R140 worker is not added again to that count.
- this preserves the R140 no-oversubscription policy: capacity smaller than the requested logical CPU reservation is rejected.
- physical-core demand remains `UNKNOWN`; it is not fabricated from logical-thread count.

The exact runtime identity remains part of `CandidateWaveExecutionInput`. The existing candidate backend may cap its NumPy/Numba thread use to runtime availability, but R140 admission still reserves the configured demand instead of silently weakening the requested resource contract.

## Peak RAM semantics

The estimator does not use a fixed “GB per room” coefficient.

For the pinned rigid CPU path, source-derived components include:

- three full-grid `float64` solver arrays: `u0`, `u1`, `Lu1`;
- full-grid boundary mask;
- loaded voxel boundary arrays with `Nb <= Ngrid`;
- Cartesian grid axes;
- ABC arrays derived from exact `Nba`;
- source/receiver communication arrays;
- `u_out` receiver traces;
- rigid material coefficient storage;
- finite-record post-processing copies, pressure records, complex frequency kernel and spectra;
- setup geometry, voxel arrays, consolidated boundary arrays and an explicit estimator-v1 NumPy temporary safety reserve.

The estimate is a **task-incremental scheduler RAM reservation**, not total process RSS. Shared Python/NumPy/Numba/HDF5 interpreter/library baseline belongs to the machine capacity authority and is not guessed by this estimator.

Pinned setup with `setup_processes == 1` has a deterministic conservative task-incremental RAM estimate. For `setup_processes > 1`, portable process copy/share working-set behavior is not bounded by the inspected source, so peak RAM becomes `UNKNOWN`. R140 therefore defers admission rather than treating it as zero.

Portable observed peak-RSS telemetry remains `UNSUPPORTED` in the generic R140 core; this slice does not fabricate a runtime measurement.

## Scratch and output storage

Scratch is separated into explicit components instead of assuming full-space/full-time field storage.

The estimator reserves:

- exact compiled model JSON bytes;
- all-voxels-nonempty upper bound for temporary voxel HDF5 payloads;
- Cartesian full-grid uint8 adjacency-check memmap;
- upper bound for persistent setup HDF5 numerical payloads;
- the exact configured `max_output_bytes` cap for `sim_outs.h5`;
- explicit estimator-v1 per-HDF5-file metadata/allocation reserve;
- bounded final complex-pressure JSON artifact reserve.

The real candidate stores receiver traces, not an implicit full-space-by-full-time field history.

Actual solver work-directory usage is reported through `ExecutionInvocationContext.report_scratch_usage()` and appears in R140 telemetry. The dedicated integration check requires observed work-directory usage not to exceed the reserved scratch quantity.

## GPU semantics

The current R130A candidate execution path is CPU-only.

- GPU slot demand: `KNOWN 0`
- VRAM: `UNAVAILABLE`
- GPU numerical-equivalence validation: not performed

This preserves the generic interface for a future GPU-specific estimator without claiming current GPU support.

## Executor integration

`PffdtdR140Worker` is an adapter over the existing `PffdtdCandidateWaveExecutor`.

Before execution it re-resolves and compares the exact candidate input and checks that the R140 task binds the same:

- candidate input;
- solver implementation;
- solver configuration.

It forwards cooperative cancellation through the existing candidate `cancel_check`, reports progress/scratch usage through the generic R140 invocation context, and returns opaque exact result/provenance references to the R140 core.

The generic R140 executor remains solver-neutral.

## Acceptance coverage

Focused tests cover:

- same exact input -> same resource/workload identity;
- grid/config sensitivity;
- thread-demand sensitivity;
- RAM shortage rejection;
- scratch shortage rejection;
- logical CPU shortage/oversubscription rejection;
- `UNKNOWN` multiprocess setup RAM remains `UNKNOWN` and defers admission;
- CPU-only GPU semantics;
- existing generic R140 executor regression suite.

The dedicated GitHub Actions integration job additionally checks one real bounded PFFDTD candidate task through R140, exact reserved-resource telemetry, exact grid/`Nt` agreement with actual PFFDTD provenance, and a second run that must be an exact cache/resume hit with zero new attempts.

## CI evidence contract

`.github/workflows/r140-pffdtd-resource-executor.yml` records an artifact containing at least:

- exact PFFDTD commit;
- exact candidate input id/hash;
- estimator id/version;
- solver-specific workload estimate;
- generic R140 resource estimate;
- admitted resource vector;
- R140 execution result;
- telemetry/reserved resources;
- cache/resume state;
- `production_solver_selected=false`;
- `gpu_validation_state=NOT_VALIDATED`;
- `gpu_numerical_equivalence_validated=false`;
- `owned_room_evidence=false`;
- `rdc_calls=0`.

## Accepted bounded executor evidence

PR #259 dedicated workflow run #2 (`35489694817`) passed on the final code state before this documentation-only evidence commit.

Focused estimator/generic executor job `106022394297`:

- `16 passed, 1 warning`;
- deterministic estimate/config/thread/admission/UNKNOWN semantics covered;
- existing generic R140 executor regression suite included.

Real PFFDTD-through-R140 job `106022394163`:

- exact PFFDTD commit: `aa319f6c86517cb95aabfae8656277da62c3ead5`;
- candidate input SHA-256: `2079712e3c2183fceb9295a3f09652f26519ea00e547b36074ce926661729002`;
- workload estimate SHA-256: `3617888bccc2f3df5359179ed5908f52c6f74aafdabbd08670227a5558a2f760`;
- generic R140 resource estimate SHA-256: `84faf5b34b5fa40a2313ff6528b920fb46fa51daa42d25445dbe9b44cbb1728a`;
- R140 execution result SHA-256: `3d03ae73157c6818ac2a71031a32cf0e793efd3149f8ed3235dddbaef30978f7`;
- estimated and actual grid: `18 x 18 x 18` / 5,832 cells;
- estimated and actual time-step count: `42`;
- workload magnitude: 244,944 cell-time updates;
- reserved logical CPU / inner threads: `4 / 4`;
- reserved peak task-incremental RAM: `1,763,808 bytes`;
- reserved scratch: `36,221,733 bytes`;
- reserved GPU slots: `0`;
- observed work-directory scratch usage: `87,280 bytes`;
- portable peak-RSS runtime measurement: `UNSUPPORTED`, unchanged from generic R140 semantics;
- first run attempts: `1`;
- second run attempts: `0`;
- second run exact cache entries reused: `1`;
- `production_solver_selected=false`;
- `gpu_validation_state=NOT_VALIDATED`;
- `owned_room_evidence=false`;
- `rdc_calls=0`.

The uploaded `r140-pffdtd-resource-executor` evidence artifact has GitHub artifact digest `sha256:8fe8f69cc8f3e57166f5b930d6fabb86a2aff62c0609c9dafb6dd4b1efc2f49a`.

## Limitations / remaining gates

Still open after this slice:

- production wave-solver adoption;
- GPU execution support and a GPU-specific estimator;
- CPU/GPU numerical-equivalence evidence if a GPU path is introduced;
- broader solver/runtime resource-estimator qualification beyond the pinned R130A CPU path;
- owned-room R180 evidence;
- candidate-wide numerical acceptance gates.

R140 therefore remains partial at the program level even though the real PFFDTD CPU candidate now has a solver-specific resource-estimation vertical path.

RDC usage for this slice: **0**.
