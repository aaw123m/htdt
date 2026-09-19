# Issue #101 — dispatch-bound solver result envelope

Date: 2026-09-20

## Scope

This slice establishes the exact output boundary after the persisted solver-dispatch contract.

```text
AcousticSceneSnapshot
→ AcousticPredictionRequest
→ AcousticSolverDispatchBinding (READY)
→ external solver execution
→ exact observable artifact authorities
→ AcousticSolverResultEnvelope
```

It does not execute a solver and does not interpret the numerical artifact payload.

## Why this is separate from CadPredictionResult

The existing `CadPredictionResult` authority is retained for the current native/rectangular geometry prediction path.

Arbitrary-room R-series solver output is not forced into that schema because doing so would mix:

- legacy rectangular geometry compatibility;
- new AcousticSceneSnapshot/dispatch identity;
- future wave/geometric/hybrid observable artifacts.

The two authorities remain explicit until a later adapter can prove a safe relationship.

## Result authority

`AcousticSolverResultEnvelope` binds:

- exact READY `AcousticSolverDispatchBinding` id/hash;
- exact `AcousticPredictionRequest` id/hash/input hash;
- exact AcousticSceneSnapshot id/hash;
- exact adapter descriptor id/hash;
- deterministic solver input hash;
- exact solver implementation authority;
- exact solver configuration authority;
- exact execution provenance authority;
- one exact output-artifact manifest per requested observable;
- completion timestamp/execution id.

A result cannot be created from BLOCKED or UNSUPPORTED dispatch.

## Observable artifact manifest

Each `AcousticSolverObservableArtifact` records:

- observable name;
- exact external artifact authority id/version/hash;
- exact encoding/schema authority id/version/hash;
- valid frequency domain.

Source/receiver identity is not duplicated in the artifact manifest. It remains authoritative through the exact dispatch → request → AcousticSceneSnapshot lineage, avoiding a second unchecked source/receiver truth.

The envelope requires the artifact observable set to equal the prediction request observable set exactly.

Each artifact valid frequency range must cover the requested range.

The artifact bytes remain external. This authority does not fabricate or decode values.

## Persistence / reopen

`CadAcousticSolverResultRepository` stores completed result envelopes append-only in the native CAD database.

Save/reopen re-resolves:

- exact persisted dispatch;
- exact persisted prediction request;
- solver implementation;
- solver configuration;
- execution provenance;
- every observable artifact;
- every artifact encoding/schema authority.

The complete envelope is regenerated from those exact authorities and must equal the persisted result.

If any artifact or external authority disappears or changes identity, reopen fails closed.

## Semantics deliberately not claimed

`COMPLETED` means only that the declared exact artifacts were produced for the exact READY dispatch.

It does not imply:

- numerical convergence;
- physical correctness;
- R100/R130/R150 validation;
- owned-room validation;
- production solver adoption;
- hybrid crossover validity;
- derived FR/IR/RT60/ETC semantics.

Those require later typed result adapters and validation authorities.

## Verification

`backend/tests/test_cad_acoustic_solver_result.py` covers:

1. READY dispatch → exact requested observable artifacts;
2. BLOCKED dispatch rejection;
3. missing/extra observable rejection;
4. insufficient artifact frequency coverage rejection;
5. append-only save/reopen;
6. external artifact authority disappearance fails closed.

RDC usage: 0.

Validation authority: Final PR-head GitHub Actions must pass before merge; no earlier pre-fix run is reused as final evidence.
