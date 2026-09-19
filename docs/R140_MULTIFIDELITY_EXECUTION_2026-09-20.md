# R140 / O90C — multi-fidelity execution and exact cache authority

Date: 2026-09-20

## Scope

This slice establishes the execution/cache boundary shared by:

- R140 hardware-aware execution;
- O90C multi-fidelity screening/refinement;
- O100E staged-fidelity execution.

It does not start worker threads, execute a solver, or choose a production backend.

The authority chain is:

```text
MultiFidelityPlan
+ exact stage
+ exact candidate
+ exact evaluator
+ exact execution backend/config/device
+ exact resource estimate
→ MultiFidelityExecutionTask
→ bounded hardware-capacity schedule
→ external execution
→ exact result authority
→ exact cache entry
→ resume without duplicate execution
```

## Execution task identity

`MultiFidelityExecutionTask` binds:

- exact MultiFidelityPlan id/hash;
- exact stage id/order;
- exact candidate authority;
- exact stage evaluator authority;
- exact execution backend authority;
- exact execution configuration authority;
- exact resource-estimate authority;
- exact device authorities;
- declared CPU/GPU/memory/scratch resource request.

All of these fields contribute to the deterministic execution input hash.

Changing backend, config, device, estimate, candidate, stage, evaluator or plan therefore creates a different task/cache identity.

## External authority re-resolution

Task save/reopen re-resolves:

- stage evaluator;
- execution backend;
- execution configuration;
- resource estimate;
- device authorities.

The plan itself is re-resolved through the typed `CadMultiFidelityRepository`.

An opaque stored hash is not sufficient.

## Hardware capacity / oversubscription

`ExecutionCapacityAuthority` binds one exact capacity authority to a declared capacity vector:

- CPU threads;
- GPU slots;
- memory bytes;
- scratch bytes.

`build_multifidelity_execution_schedule()` uses deterministic bounded batching.

A task larger than the declared capacity is rejected.

A batch whose aggregate requested resources would exceed any capacity dimension is split before admission.

This is admission/scheduling authority only. It does not claim that the estimate is physically accurate; the estimate provenance is a separate exact authority.

## Cache / resume

A completed cache entry binds:

- exact execution task id/hash/input hash;
- exact result authority;
- exact execution-provenance authority;
- completion timestamp.

Only one completed cache authority may exist for one exact execution input.

`partition_resume()` separates:

- exact reusable completed results;
- pending exact tasks.

A changed execution configuration or any other task input produces a cache miss rather than approximate reuse.

If a cached result authority or execution provenance disappears or changes identity, reopen/reuse fails closed.

## Separation from native UI job guard

Existing `PredictionJobGuard` remains a UI stale-completion/supersession guard.

It is not reused as R140 cache/scheduler truth.

R140 task/cache identity is persisted and exact; native UI job IDs are transient process-level state.

## Persistence

`CadMultiFidelityExecutionRepository` adds append-only native-CAD persistence for:

- exact execution tasks;
- deterministic schedules;
- completed exact cache entries.

Save/reopen regenerates task and schedule authority from the exact persisted plan and external authorities.

## Focused verification

`backend/tests/test_cad_multifidelity_execution.py` verifies:

1. execution task identity changes when exact configuration changes;
2. deterministic batching prevents declared GPU oversubscription;
3. task larger than declared capacity is rejected;
4. task/schedule/cache save-reopen;
5. exact completed result is reused while another task remains pending;
6. changed config produces a cache miss;
7. stale cached result authority fails closed;
8. stale stage evaluator authority fails task reopen.

## Deferred

- actual worker pool;
- process/thread spawning;
- GPU device discovery;
- automatic resource estimator;
- runtime telemetry;
- failure/cancellation attempt ledger;
- retry policy;
- priority/fairness;
- distributed execution;
- CPU/GPU numerical reproducibility fixture;
- solver-specific dispatch execution.

Those can be layered on this exact identity/cache authority without redefining O90C/O100E evidence semantics.

RDC usage: 0.
