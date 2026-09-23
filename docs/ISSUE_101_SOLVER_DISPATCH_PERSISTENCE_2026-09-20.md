# Issue #101 — solver dispatch persistence authority

Date: 2026-09-20

## Scope

This slice persists the solver-neutral dispatch contract introduced by PR #222.

The authority chain becomes:

```text
AcousticSceneSnapshot
→ AcousticPredictionRequest
→ AcousticSolverAdapterDescriptor
→ AcousticSolverDispatchBinding
→ append-only exact save/reopen
→ future solver execution
```

No solver is executed or selected for production.

## Exact external authorities

A solver adapter descriptor contains exact external refs for:

- solver implementation/build;
- solver configuration schema.

A dispatch binding additionally contains the exact solver configuration ref.

These external refs are not treated as verified merely because an id/version/hash was persisted.

`CadAcousticSolverDispatchRepository` requires a caller-provided exact external-authority resolver. Save/reopen fails unless the resolver returns the exact same id/version/hash authority.

This lets future package/build/config registries supply the concrete authority without hard-coding one solver backend into this repository.

## Adapter persistence

`save_descriptor()` and `get_descriptor()` validate:

- descriptor deterministic identity;
- exact solver implementation authority;
- exact solver configuration-schema authority.

Descriptors are append-only in the native CAD database.

## Dispatch persistence

`save_dispatch()` and `get_dispatch()` re-resolve:

1. exact AcousticSceneSnapshot id/hash;
2. exact AcousticPredictionRequest id/hash/deterministic input hash;
3. request → snapshot exact binding;
4. exact persisted AcousticSolverAdapterDescriptor;
5. exact solver implementation authority;
6. exact solver configuration authority;
7. the complete dispatch by rerunning `bind_prediction_request_to_solver_adapter()`.

The recomputed binding must equal the persisted binding exactly.

This means a stale snapshot, request, adapter capability, solver build, or solver configuration cannot silently reuse an old dispatch.

## Persistence boundary

The new tables are append-only native-CAD tables:

- `cad_acoustic_solver_adapters`
- `cad_acoustic_solver_dispatch_bindings`

No sidecar database or solver-specific truth is created.

## READY semantics

Persisted `READY` retains the PR #222 meaning:

> exact input is compatible with the exact adapter/configuration contract.

It still does not mean:

- solver execution completed;
- numerical convergence passed;
- R100/R130/R150 validation passed;
- production solver adoption is approved.

## Focused verification

Tests appended to `backend/tests/test_cad_acoustic_snapshot.py` cover:

1. descriptor + READY dispatch save/reopen;
2. exact snapshot/request/descriptor recomputation;
3. missing solver implementation authority blocks descriptor persistence;
4. removed/stale solver configuration authority blocks dispatch reopen.

No additional numerical solver test is warranted because this slice does not execute a solver.

RDC usage: 0.
