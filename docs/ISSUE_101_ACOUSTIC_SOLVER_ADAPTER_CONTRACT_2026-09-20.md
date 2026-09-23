# Issue #101 — solver adapter dispatch contract

Date: 2026-09-20

## Scope

This slice establishes the exact boundary between the existing solver-neutral authorities and a future R130/R150 implementation:

```text
AcousticSceneSnapshot
→ AcousticPredictionRequest
→ AcousticSolverAdapterDescriptor
→ AcousticSolverDispatchBinding
→ future solver-specific execution
```

It does not select or execute a production solver.

## AcousticSolverAdapterDescriptor

The descriptor is immutable/versioned and binds:

- adapter id/version;
- exact model/solver role id;
- acoustic domain: wave or geometric;
- exact solver implementation authority id/version/hash;
- exact solver configuration-schema authority id/version/hash;
- supported AcousticSceneSnapshot schema versions;
- supported observables;
- supported frequency domain.

The descriptor states only capability. It is not a ranking and does not authorize production adoption.

## AcousticSolverDispatchBinding

A dispatch binding evaluates one exact persisted-style prediction input against one exact adapter/configuration authority.

Its deterministic solver-input identity includes:

- AcousticPredictionRequest id/hash/input hash;
- adapter descriptor id/hash;
- exact solver implementation authority;
- exact solver configuration authority.

Therefore a solver build change or solver configuration change changes the solver-input identity even when the acoustic scene/request is unchanged.

`READY` means only that the exact input contract can be handed to that adapter. It does not mean that a solve has run, converged, passed R100/R130/R150 validation, or is approved for production.

## Fail-closed capability gating

The binding rejects or blocks:

- model/solver role mismatch;
- unsupported snapshot schema;
- unsupported observable;
- request frequency outside the adapter domain;
- request frequency outside the snapshot declared/valid domain;
- snapshot geometry/receiver readiness failure;
- wave source/boundary/environment readiness failure for wave adapters;
- geometric directivity/boundary readiness failure for geometric adapters;
- observable-level BLOCKED/UNSUPPORTED state.

No missing capability is converted into a default.

## Current wave-source state

Current R110 v1 intentionally does not fabricate an electrical-to-acoustic wave excitation authority.

As a result, the focused fixture proves that an R130-style wave adapter remains `BLOCKED` for complex pressure even when geometry/boundary/environment are otherwise available.

This slice does not weaken that gate.

## Geometric path

The existing snapshot fixture has exact geometry, material/boundary authority, directivity, receivers and environment sufficient for deterministic geometric paths.

The focused fixture therefore proves that an R150-style geometric adapter can reach `READY` for `deterministic_paths` without invoking a solver.

## Deliberately deferred

- production solver selection;
- PFFDTD/MFEM adoption;
- actual R130 wave execution;
- actual R150 ray execution;
- solver output/result schema;
- mesh/grid/BVH generation;
- GPU/CPU execution planning;
- cancellation/scheduler integration;
- solver-configuration parameter modeling beyond an exact external authority ref;
- production adapter registry.

R100/R130/R150 validation remains authoritative for numerical capability.

## Verification

Focused tests are appended to `backend/tests/test_cad_acoustic_snapshot.py` and cover:

1. READY geometric deterministic-path dispatch;
2. current wave-excitation BLOCKED propagation;
3. role/observable/frequency fail-closed handling;
4. solver build/configuration identity changes.

GitHub Actions is the validation authority for this slice.

RDC usage: 0.
