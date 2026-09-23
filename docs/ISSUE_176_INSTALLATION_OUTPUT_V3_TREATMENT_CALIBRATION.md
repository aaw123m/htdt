# InstallationOutput v3 — Treatment + CalibrationPlan integration

## Scope

This follow-up extends InstallationOutput v2 with exact AcousticTreatment and
CalibrationPlan authority summaries. It does not infer either section from
Scene entity presence.

New generation uses:

- schema version `3`
- authority version `installation-output-3`
- renderer version `installation-report-3`

Serialized InstallationOutput v1 and v2 remain loadable and retain their
version-specific semantic identity rules.

## Treatment authority

A treatment section is `AVAILABLE` only when the caller supplies all of:

- exact immutable `AcousticTreatmentDefinition`
- exact current `AcousticTreatmentPlacement`
- exact `TreatmentSurfaceBindingEvaluation`

Each placement is validated against the output SceneRevision. A placement that
is SystemVariant-bound must match the output SystemVariant exactly. Definition
id/version/hash and host SemanticSurface id/hash must match, and the binding
evaluation must be `exact` with `placement_authority_valid=true`.

Stale SceneRevision/semantic geometry, removed/replaced surfaces, definition
hash mismatch, or SystemVariant mismatch fail closed. A stale placement is
never rendered as available.

The installation summary preserves:

- proposed versus installed lifecycle
- placement and definition immutable identities
- exact host SemanticSurface and binding-evaluation identity
- position, quaternion orientation, coverage, dimensions, thickness and air gap
- definition/model/material identity
- evidence basis and uncertainty
- explicit wave and geometric material capability
- `solver_prediction_readiness` from the source authority

Prediction readiness is never promoted from `UNKNOWN`. The report does not
interpret material capability as solver/compiler readiness.

Quantity/cut-list rows are deterministic and grouped by exact definition
identity **and lifecycle**, so proposed and installed instances are never
combined.

## Calibration authority

A calibration section is `AVAILABLE` only when an exact `CalibrationPlan`
is supplied and its SceneRevision and SystemVariant bindings match the
InstallationOutput target.

The summary keeps requested plan settings separate from actual exported
settings. It includes:

- plan id/version/semantic hash and support state
- channel, role, source speaker and physical output mapping
- sample rate, gain, delay, polarity, crossover, routing and ordered PEQ
- deterministic target-curve identity and normalization condition
- maximum boost/cut
- device capability id/version and deterministic constraints identity
- optional exact generic ExportSnapshot id/hash and quantization status
- optional exact VerificationMeasurementPlan id/hash
- lifecycle state plus exact lifecycle event identities

Export plan id/hash mismatch, verification plan/export mismatch, and lifecycle
plan/export/verification hash mismatch fail closed.

## Lifecycle semantics

The existing calibration lifecycle ordering contract is reused:

`proposed -> exported -> user_applied -> remeasured -> validated`

Lifecycle events are consumed in persisted append order. InstallationOutput
does not sort by `created_at_utc`; timestamps are not used as authority for
which state is latest.

An ExportSnapshot is explicit evidence of `exported` only. It never implies
`user_applied`, `remeasured`, or `validated`.

## UNKNOWN semantics

When no explicit Treatment authority is supplied:

`treatment_plan = UNKNOWN`

When no explicit CalibrationPlan authority is supplied:

`calibration_plan = UNKNOWN`

No Scene appearance, speaker presence, treatment-like geometry, or other
heuristic promotes either section.

## CSV / HTML

CSV v3 adds machine-readable authority records for Treatment and Calibration,
plus deterministic installation-facing rows for:

- treatment instances and grouped quantity/face-area summary
- requested and exported calibration channel settings

HTML v3 adds Treatment and Calibration sections and embeds the exact v3
semantic snapshot as JSON.

`exported_at` remains renderer metadata and is excluded from
`semantic_sha256`.

## Validation coverage

Focused installation tests cover:

- proposed and installed treatment lifecycle
- deterministic quantity summary for repeated definitions
- exact surface binding and stale-surface rejection
- treatment SceneRevision/SystemVariant/definition mismatch rejection
- Treatment UNKNOWN semantics
- supported CalibrationPlan summary
- generic and quantized export snapshots
- export-only state remaining `exported`
- explicit `user_applied`, `remeasured`, and `validated` states
- SceneRevision/SystemVariant/export/verification/lifecycle mismatch rejection
- Calibration UNKNOWN semantics
- deterministic CSV
- deterministic HTML embedded semantic payload
- exported timestamp outside semantic identity
- serialized v1/v2 reopen compatibility

No Treatment or Calibration core authority schema is changed by this slice.
