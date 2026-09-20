# Issue #101 / R140 GPU execution and CPU/GPU equivalence authority

Date: 2026-09-20

## Scope

This task implements the next R140 hardware-aware execution slice without changing HTDT-Capture or the canonical implementation-status/roadmap documents.

The intended authority chain is:

`exact solver input`
→ `GpuExecutionCapability`
→ `GpuResourceEstimate`
→ existing R140 execution task/scheduler admission
→ GPU execution provenance
→ exact CPU reference + exact GPU output
→ `CpuGpuEquivalenceSpec`
→ `CpuGpuEquivalenceEvaluation`.

The existing solver-neutral R140 scheduler already carries exact backend/config/resource/device refs and a resource vector containing CPU threads, GPU slots, host memory, and scratch. The GPU-specific authority therefore remains in a separate module and reuses the existing scheduler contract rather than widening the CPU executor schema unnecessarily.

## Required invariants

- UNKNOWN and zero are distinct. UNKNOWN GPU slot, VRAM, host-RAM, CPU-worker, scratch, or concurrency information must fail closed for GPU admission.
- CPU-only hosts remain usable and report GPU unavailable separately from CPU execution health.
- No synthetic/mock GPU output may become numerical validation evidence.
- CPU/GPU comparison is observable-specific and tolerance-authority driven; bitwise identity is not assumed.
- Backend-specific automatic tolerance relaxation is forbidden.
- Exact solver input, exact grid/mesh identity, precision, CPU reference identity, output observable, and tolerance are part of comparison identity.
- Exact solver implementation, GPU backend implementation/API, runtime, device, driver/runtime identity, and resource authority are preserved in GPU execution provenance.
- Unsupported GPU observables produce UNSUPPORTED rather than an inferred comparison result.
- A stale/mismatched CPU reference, solver input, grid/mesh, or precision fails closed.
- A PASS/FAIL numerical decision is only valid for provenance declaring real GPU hardware execution.
- If no suitable GitHub Actions GPU runner exists, hardware execution acceptance remains NOT_RUN / NOT_VALIDATED / BLOCKED and production GPU support remains false.

## Existing candidate status

The current repository PFFDTD candidate is the Python/Numba CPU path. Repository search at task start found no existing CUDA/GPU candidate integration. Existing R140 PFFDTD evidence remains authoritative as:

- `gpu_validation_state=NOT_VALIDATED`
- `gpu_numerical_equivalence_validated=false`
- `production_solver_selected=false`

This task will not promote those fields without real GPU evidence.

## Parallel ownership / non-claims

This branch will not modify:

- `docs/IMPLEMENTATION_STATUS.md`
- `docs/IMPLEMENTATION_ROADMAP.md`
- R150
- R120B
- R170
- R100B benchmark evidence
- HTDT-Capture

RDC usage: **0**.


## Implemented authority

Implementation lives in `backend/src/htdt/cad_r140_gpu_execution.py` and is intentionally separate from `cad_r140_executor.py`.

Implemented immutable/content-addressed models:

- `IdentityDatum` for KNOWN / UNKNOWN / UNAVAILABLE runtime-device identity values.
- `GpuExecutionCapability` for exact solver/backend/API/runtime/device/driver capability and supported precision/observable sets.
- `GpuResourceEstimate` for GPU slots, VRAM, host RAM, host CPU workers, scratch, max concurrency and constraints.
- `CpuGpuEquivalenceSpec` with exact solver input, exact grid/mesh hash, precision, exact CPU reference, observable and explicit fixed tolerance.
- `GpuExecutionTaskAuthority` binding capability/resource/equivalence identities into a deterministic GPU task configuration.
- `GpuExecutionProvenance` with hardware-evidence origin and a validation-eligibility gate.
- `CpuGpuObservableEvidence` for exact typed CPU/GPU observable values.
- `CpuGpuEquivalenceEvaluation` for PASS / FAIL / BLOCKED / NOT_VALIDATED / UNSUPPORTED decisions.
- `CadR140GpuAuthorityRepository` append-only SQLite persistence.

`build_gpu_multifidelity_execution_task()` bridges the GPU-specific authority into the existing R140 `MultiFidelityExecutionTask` and scheduler. The generic task therefore preserves the exact GPU backend/configuration/resource/device refs while reusing existing bounded batching and cache identity.

No change to `cad_r140_executor.py` was required.

The bridge to `MultiFidelityExecutionTask` establishes deterministic GPU scheduling/admission/cache identity, but the existing `BoundedR140Executor` remains intentionally bound to its CPU `ExecutionResourceEstimate` contract. There is no integrated repository GPU worker backend in this slice, so this record does not claim that the current bounded CPU executor can execute a GPU task. A future real GPU worker must consume the GPU-specific task/resource authority and emit real-hardware provenance before numerical equivalence can be validated.

## Resource admission

`GpuResourceEstimate.admission_resource_vector()` requires all of the following to be KNOWN before scheduling:

- GPU slot requirement
- VRAM requirement
- host RAM requirement
- host CPU worker requirement
- scratch requirement
- max concurrent task constraint

UNKNOWN raises the existing R140 `ResourceAdmissionError` with DEFER. UNAVAILABLE or structurally invalid zero requirements fail with REJECT where appropriate. GPU execution requires at least one GPU slot, positive VRAM, at least one host CPU worker and positive max concurrency.

The existing R140 scheduler continues to enforce aggregate CPU / GPU-slot / RAM / scratch capacity. CPU-only capability remains a separate state and does not invalidate the CPU executor.

## CPU/GPU numerical equivalence

The harness supports explicit comparison authorities for:

- complex pressure: complex Euclidean error against explicit absolute + relative tolerance
- magnitude: scalar absolute + relative tolerance
- phase: circular phase distance against explicit absolute-radian tolerance
- deterministic scalar diagnostics: scalar absolute + relative tolerance

Tolerance is part of `CpuGpuEquivalenceSpec` identity and is never backend-auto-relaxed.

Before numerical comparison, the evaluator rejects mismatches in:

- solver implementation
- GPU backend implementation
- exact solver input
- exact grid/mesh
- precision
- output observable
- exact CPU reference
- GPU provenance/result binding

A GPU capability without the requested observable/precision returns UNSUPPORTED.

PASS/FAIL decisions are structurally impossible unless both provenance and GPU evidence identify `REAL_GPU_HARDWARE`. `gpu_numerical_equivalence_validated` is true only for PASS; a real-hardware tolerance failure records FAIL while the equivalence-validation gate remains false. MOCK/SYNTHETIC evidence returns NOT_VALIDATED and cannot be promoted by setting a validation flag.

`production_gpu_support` remains hard-coded false in this slice; numerical equivalence alone is not a production-adoption decision.

## Current backend / hardware status

Repository search found no integrated CUDA/GPU PFFDTD candidate backend in the current HTDT source. The current bounded PFFDTD candidate remains the Python/Numba CPU implementation.

`scripts/run_r140_gpu_validation_gate.py` probes the runner for an NVIDIA device only to describe hardware availability; it does not treat detection as solver evidence. Because no repository GPU candidate backend exists, the gate records:

- `hardware_numerical_validation=NOT_RUN`
- `gpu_validation_state=NOT_VALIDATED`
- `execution_acceptance=BLOCKED`
- `gpu_numerical_equivalence_validated=false`
- `production_gpu_support=false`
- `synthetic_or_mock_promoted_to_numerical_evidence=false`

If a GPU is present without an integrated candidate backend, the state remains BLOCKED. No mock output is generated as numerical evidence.

## Tests

`backend/tests/test_cad_r140_gpu_execution.py` covers:

- deterministic GPU authority and generic R140 task identity
- UNKNOWN versus zero resource semantics and fail-closed admission
- CPU-only capability behavior
- tolerance-bound comparison-spec identity
- real-hardware complex-pressure comparison path
- mismatched solver-input rejection
- mismatched precision rejection
- stale CPU-reference rejection
- mock GPU non-promotion / fabricated validation-state rejection
- explicit UNSUPPORTED observable state
- circular phase tolerance semantics
- append-only save/reopen persistence

The dedicated workflow also runs the existing `backend/tests/test_cad_r140_executor.py` CPU regression suite. Unit fixtures that set `REAL_GPU_HARDWARE` exercise validation-state invariants only; they are not hardware evidence and are never recorded as the repository hardware-validation result.

## GitHub Actions evidence

Dedicated workflow: `R140 GPU Execution Authority`.

Accepted code-state run before this documentation-only update:

- run #2 / `35500572096`: PASS
- authority + CPU regression job `106051496869`: **22 passed**
- hardware-validation-gate job `106051497082`: PASS
- hardware numerical execution: NOT_RUN
- GPU numerical equivalence: NOT_VALIDATED
- production GPU support: false

The first workflow attempt exposed only an invalid one-stage MultiFidelityPlan test fixture; the implementation tests themselves were otherwise 20 passed. The fixture was corrected to satisfy the existing two-stage plan contract, then run #2 passed.

## Changed files

- `backend/src/htdt/cad_r140_gpu_execution.py`
- `backend/tests/test_cad_r140_gpu_execution.py`
- `scripts/run_r140_gpu_validation_gate.py`
- `.github/workflows/r140-gpu-execution-authority.yml`
- this implementation record

No canonical status/roadmap file and no other R-series ownership area is modified.
