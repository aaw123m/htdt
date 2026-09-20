# R170A low-band integration — 2026-09-20

## Scope

R170A establishes a solver-neutral product prediction provider over exact immutable R-series acoustic results. This slice is capability-gated and fail-closed. It does not select a production solver and does not convert candidate solver output into production or owned-room truth.

## Starting authority

- base branch: `main`
- starting commit: `a8491db973c6fcefc66e259903ca668936f63a2e`
- R100B production-adoption decision: `NO_GO`
- RDC usage: 0

## Bounded lane

- single source
- explicit receiver set
- low-band frequency-response magnitude
- phase only when exact complex-pressure capability supports it
- exact valid frequency domain
- exact immutable R130 `AcousticSolverResultEnvelope`

IR, RT60, EDT, C50, C80 and broadband hybrid observables remain unsupported unless a future authority establishes them.

## Required invariants

The implementation must preserve exact SceneRevision, AcousticSceneSnapshot, source, receiver, environment, solver implementation, result-envelope and underlying artifact identity. Missing/tampered/stale or mismatched authorities fail closed. Candidate/validated/production evidence states remain distinct.

## Integration targets

The provider contract will be consumable by N70-style product prediction, O30/O40 objective evaluation, O50 measurement planning, O60 validation and O70 residual/adaptive binding without requiring those layers to parse raw solver payloads or masquerading R130 output as a RoomSim attempt.

## Status

Implementation in progress on the dedicated R170A branch. Test and GitHub Actions evidence will be recorded here before completion.
