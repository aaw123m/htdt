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
