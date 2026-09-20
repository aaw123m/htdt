# R160 Typed Hybrid Result Authority — 2026-09-20

Status: **foundation / partial**

Issue: #101

Implementation branch base: `5791d615e2e180600fb75357b1e9cf15235597a0` (latest `main` observed at implementation start).

## Scope

This slice establishes the solver-neutral R160 result authority that can refer to
R130A wave candidate evidence and R150 deterministic geometrical-acoustics
evidence without numerically mixing them.

It deliberately does **not** implement a physical crossover, pressure stitching,
late-tail synthesis, solver selection, or owned-room validation.

The authority is implemented in:

- `backend/src/htdt/cad_hybrid_acoustic_result.py`
- `backend/tests/test_cad_hybrid_acoustic_result.py`

The R150 public contract is consumed as-is. This slice does not modify
`backend/src/htdt/cad_geometric_acoustics_adapter.py`, minimizing overlap with
the parallel general-polyhedral R150 work.

## Exact hybrid model

`HybridAcousticResult` is immutable, versioned and content-addressed. It binds:

- exact `AcousticSceneSnapshot` id/hash;
- exact SceneRevision id/content hash;
- exact semantic acoustic geometry id/hash;
- exact source binding identities, including R110 source hashes;
- exact receiver identity/order hashes;
- exact environment binding and sound-speed source authority;
- the exact prediction request set;
- participating `AcousticSolverResultEnvelope` ids/hashes;
- each participating result's dispatch, adapter, solver implementation,
  configuration and observable artifact refs;
- typed component validity;
- exact stitching-policy identity;
- R160 algorithm id/version.

The hybrid result stores references and derived compatibility metadata only.
It does not copy wave complex-pressure arrays or R150 path payloads into a
second numerical truth.

## Observable separation

### CoherentTransfer

R160 v1 recognizes a `complex_pressure` artifact as `CoherentTransfer` only
when the external artifact itself supplies:

- paired real/imaginary pressure arrays;
- `Pa` units;
- an explicit pressure reference;
- an explicit complex representation;
- phasor convention;
- analysis Fourier kernel;
- exact receiver identity/order;
- an exact source R110 hash that resolves uniquely in the snapshot;
- an explicit valid frequency domain.

Magnitude-only or phase-only result artifacts are rejected rather than promoted
to coherent transfer.

### DeterministicPathSet

`DeterministicPathSet` holds an exact ref to the existing R150
`DeterministicPathArtifact`.

R160 does not copy the individual paths. It exposes typed availability metadata
such as direct/specular path counts only after exact R150 re-resolution.

The R150 phase state remains
`UNAVAILABLE_NOT_SYNTHESIZED`. Phase-free geometrical energy transport is
never added to or promoted into coherent pressure.

### LateEnergyDecay

The contract supports typed states:

- `AVAILABLE`
- `UNAVAILABLE`
- `UNSUPPORTED`
- `NOT_PROVIDED`

The current foundation generates no synthetic late decay. In the absence of an
exact solver artifact, only one of the non-available states is stored, together
with an explicit reason.

## Validity semantics

Every available component has `HybridObservableValidity` with:

- exact minimum/maximum frequency through `FrequencyDomain`;
- typed hybrid observable;
- source solver observable;
- phase capability;
- exact solver result id/hash;
- exact adapter id/hash;
- exact solver implementation/configuration refs;
- validation/evidence state.

Current R130A/R150 candidate execution is represented only as
`EXECUTED_UNVALIDATED`. This R160 foundation has no exact validation-authority
binding, so callers cannot promote a component to `VALIDATED` by supplying a
flag or string. A future validated state must bind and re-resolve an explicit
validation authority. This slice does not convert candidate execution into
numerical or owned-room validation.

No global 20–300 Hz crossover is encoded.

## Stitching policy

`HybridStitchingPolicy` is a separate immutable/versioned authority. Initial
modes are:

- `disjoint_by_observable`
- `frequency_partition_no_blend`
- `overlap_preserve_components`

All v1 policies hard-code the safety semantics, not crossover frequencies:

- numerical blend: forbidden;
- cross-observable numerical combination: forbidden;
- validity-domain extrapolation: forbidden;
- unsupported phase generation: forbidden.

`frequency_partition_no_blend` requires explicit, canonical, non-overlapping
partitions, and each partition must remain inside the selected component's own
validity domain.

An overlap between wave and GA validity domains is therefore only an overlap of
available evidence. It does not authorize a crossfade or any hidden numerical
blend.

## Compatibility gate

Construction and persistence fail closed on:

- different snapshot / SceneRevision;
- different semantic geometry;
- stale or mismatched prediction request identity;
- stale or mismatched solver result identity/provenance;
- receiver identity/order mismatch;
- source authority mismatch;
- environment/sound-speed authority mismatch;
- missing complex artifact;
- missing R150 path artifact;
- wrong observable type;
- magnitude/phase-only promotion attempts;
- wrong unit/reference for coherent pressure;
- path/result dispatch, request, implementation or configuration mismatch;
- path phase claims inconsistent with the R150 phase-free contract;
- implicit frequency extrapolation or invalid explicit partitions.

Exact same-snapshot binding is the primary authority for source, receiver,
geometry and environment compatibility; component-specific artifacts are also
checked against their own explicit source/receiver metadata where present.

## Persistence

`CadHybridAcousticResultRepository` uses the native SQLite database and calls
the central `ensure_native_schema()` gate before initialization and every
connection.

Persistence is append-only and content-addressed through:

- `cad_hybrid_stitching_policies`
- `cad_hybrid_acoustic_results`

Save uses an explicit `BEGIN IMMEDIATE` transaction and explicit
commit/rollback. Every SQLite handle is scoped by `closing(...)`; no retained
connection handle is stored on the repository.

Reopen re-resolves:

- snapshot;
- prediction requests;
- solver result envelopes;
- R150 path artifact where present;
- coherent external artifact payload;
- stitching policy;

and regenerates the complete R160 semantic identity before returning the
persisted result.

Missing or changed external artifacts therefore fail closed after reopen.

## Typed downstream queries

`HybridAcousticResult` provides typed queries for:

- `has_frequency_response()`
- `has_phase()`
- `has_direct_or_early_paths()`
- `has_late_energy_decay()`
- `late_energy_decay_state()`
- `valid_frequency_domain_for(...)`
- `frequency_relationship()`
- `numerical_blend_permitted()`

Downstream callers do not need to parse the R130A complex-pressure JSON schema
or the R150 path artifact schema merely to answer availability/validity
questions.

## Focused tests

The focused test module covers the requested R160 foundation cases:

1. wave-only coherent component;
2. GA-only deterministic path component;
3. same-snapshot wave + GA hybrid;
4. different SceneRevision rejection;
5. different receiver-set rejection;
6. stale/mismatched solver-result rejection;
7. phase-free GA not promoted to coherent transfer;
8. explicit non-overlapping frequency validity;
9. explicit overlap without silent blend;
10. unavailable `LateEnergyDecay`;
11. deterministic semantic identity;
12. SQLite save/reopen equality;
13. missing external artifact fail-closed.

No large solver fixture or owned-room fixture is introduced.

## Validation record

Latest-main synchronized PR head before this record update:
`05692946d875cf0b28376dd5410c5381a1dec697`, with `main` at
`fc3dcf086f0cb9d349d85722799a94d143c8b851`.

GitHub Actions on that synchronized head all passed:

- CI run `35484314723` / run number 1288: **PASS**.
  - backend suite: **855 passed, 2 skipped, 3 warnings** in 230.49 s;
  - R100B authority preflight: PASS;
  - R100A-3 radiation reference validation: PASS;
  - backend/native CAD launcher checks: PASS;
  - O60R inventory smoke: PASS;
  - Windows CAD harness compile + PowerShell syntax: PASS;
  - N60/N70/N80/N80-O20/N80c/N90/O60R hardware-gate preflights: PASS.
- R130A Candidate Wave Execution run `35484314707` / run number 36:
  **PASS**, including focused candidate-execution tests, pinned PFFDTD bounded
  READY execution and candidate-only evidence semantics.
- Windows Release Artifact run `35484314709` / run number 643: **PASS**,
  including locked native package build, maintenance/migration smoke, installer
  build, install/uninstall data-retention smoke and artifact uploads.

No R160 change touched `cad_geometric_acoustics_adapter.py`; the diff remains
limited to the new R160 authority/tests plus R160/status documentation.

RDC usage: **0**.

## Explicit non-claims / remaining work

This foundation does **not** establish:

- physical crossover optimization;
- numerical pressure stitching;
- coherent GA phase;
- stochastic ray tracing;
- diffraction;
- a late-reverberation solver;
- production wave or GA solver selection;
- room-calibrated SPL synthesis;
- O30/O40 objectives;
- O70/O80 integration;
- R170 visualization/integration completion;
- R180 owned-room validation.

R160 remains **partial** until later numerical combination/continuity semantics
are justified by explicit physical authority and validation evidence. Issue #101
must remain open.
