# Issue #101 / R140 actual executor slice — 2026-09-20

## Scope

This slice connects the solver-neutral R140 execution authority from PR #236 to an actual bounded worker/executor layer. It intentionally does **not** implement acoustic solver physics.

## Added authority

- `ExecutionResourceEstimate` with explicit `KNOWN / UNKNOWN / UNAVAILABLE` resource quantities.
- `DeclaredCpuResourceEstimator` as the CPU correctness baseline. It preserves upstream declared/derived estimates and does not infer solver physics.
- admission requires known logical CPU demand, inner solver threads, peak RAM, scratch/disk and GPU-slot demand; UNKNOWN is never treated as zero.
- `ExecutionTaskResult` gives deterministic immutable success identity for one exact execution task/output.
- `ExecutionAttemptRecord` stores immutable final runtime evidence.
- append-only native persistence for estimates, results and attempts.

## Actual bounded executor

`BoundedR140Executor` uses a real `ThreadPoolExecutor` worker pool and the existing deterministic R140 schedule.

- outer worker concurrency is independent from per-task inner solver thread count.
- exact resource estimates must reproduce the task's `ExecutionResourceVector`.
- schedule batching remains the capacity authority; the executor refuses stale/mismatched task, backend, configuration or estimate authorities.
- queued tasks can be cancelled before worker start.
- running cancellation is cooperative. A callback that ignores cancellation may continue until it returns, but cancellation is re-checked before success/cache materialization, so a cancelled task cannot become success.
- failure is retained as failure with reason, exit condition, wall time and optional partial diagnostic.
- exact cache reuse/resume uses the existing PR #236 cache semantics; completed exact tasks are reused and only unfinished tasks execute.

## Telemetry

Final attempt evidence stores:

- queued / started / finished UTC timestamps
- wall time
- task state
- worker identity, process id, thread name and outer worker slot
- reserved logical CPU / inner solver threads / RAM / scratch / GPU slots
- peak-memory state
- scratch usage state/value when reported
- cancellation status
- failure reason / exit condition / partial diagnostic
- last reported progress

Peak memory is currently marked `UNSUPPORTED` by the portable core instead of fabricating an OS-specific value.

## Adapter boundary

The executor calls an `ExecutionWorkerPort` callback with an `ExecutionInvocationContext`. A future PFFDTD/MFEM/pyroomacoustics adapter can use this interface without embedding solver physics in R140 core.

`DeterministicSyntheticWorker` exists only for CI. Its authorities are explicitly named `synthetic_execution_result` / `synthetic_execution_provenance` and are not acoustic production evidence.

## Verification

Focused tests cover CPU resource admission, UNKNOWN handling, inner-thread oversubscription rejection, serial execution, bounded parallel execution, queued/running cancellation, deterministic success identity, exact cache hit, backend/config cache miss, failure retention, resume, stale-authority rejection and explicit shutdown/SQLite-handle cleanup.

RDC was not used.

## Completion status

This is a **partial R140 completion**. The actual CPU worker/executor vertical slice exists, but R140 must not be declared fully complete until remaining canonical acceptance is audited, including real solver-specific resource estimators and any required CPU/GPU numerical-equivalence evidence.
