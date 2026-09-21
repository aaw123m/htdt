# CalibrationPlan authority (Issue #173)

Status: initial device-neutral foundation.

## Authority boundary

`CadCalibrationPlan` is an immutable/versioned proposal bound to one exact
`SceneRevision`, one exact `SystemVariant`, one exact source
`Measurement`/`Dataset`, and one exact `MeasurementQualityReport`.
Persistence re-resolves every referenced authority and verifies the recorded
semantic/content hashes. The initial authority requires the SystemVariant
baseline to be the exact source SceneRevision.

The plan does not imply device installation, as-built state, or validation.
It does not synthesize missing measurement capability and does not bypass the
owned-room production recommendation gate.

## Signal path order

The device-neutral per-channel order recorded by this slice is:

1. channel/role/source-entity to physical-output mapping,
2. routing selection,
3. polarity,
4. channel gain,
5. absolute delay,
6. crossover settings,
7. ordered PEQ biquads.

The tuple order of PEQ filters is authoritative. Crossover settings retain their
explicit type, frequency and filter order rather than being silently converted
to another device-specific representation.

## Measurement capability matrix

| Requested setting | Required MeasurementQualityReport authority |
| --- | --- |
| target curve / gain / crossover / PEQ magnitude work | `magnitude_response` over the explicit required band |
| absolute delay | `common_timing` |
| polarity inversion | `polarity` |
| phase inspection | `phase_response` only where explicitly consumed |
| all-pass correction | unsupported in `calibration-plan-1` even when phase/common timing are present, because coherent inter-channel phase-correction authority is not established |

A phase array alone never establishes common timing. Magnitude-only evidence
never authorizes phase correction. Missing/blocked claim gates make the plan
`UNSUPPORTED` with reasons rather than causing inferred values or silent
omission.

## Generic biquad convention

`CadBiquadFilter` uses a normalized direct-form denominator:

`H(z) = (b0 + b1 z^-1 + b2 z^-2) / (1 + a1 z^-1 + a2 z^-2)`

The fixed coefficient contract is:

- normalization: `a0_normalized`
- ordering: `b0,b1,b2,a1,a2`
- sign convention: `denominator=1+a1*z^-1+a2*z^-2`
- sample rate is stored on every filter
- supported generic types in this authority: peaking, low-pass, high-pass,
  and all-pass definition; all-pass recommendation/export remains blocked by
  the plan capability gate
- coefficients must be finite, parameter-consistent and have poles strictly
  inside the unit circle

Transfer evaluation is deterministic from the stored normalized coefficients.
The focused reference check verifies that a peaking filter reaches its requested
gain at the reference frequency.

## Device capability and unsupported semantics

`CadDeviceCapabilityConstraints` records supported sample rates/filter types,
filter count, boost/cut limits, gain/delay ranges, crossover orders, physical
outputs and optional parameter resolutions. Unknown finite limits are not
treated as unlimited where the requested setting depends on them.

Filter-count overflow, max boost/cut overflow, unsupported output/type/order,
missing timing capability and all-pass correction are explicit unsupported
reasons. Export never clips a parameter, drops a filter or substitutes another
filter type.

## Export contract

The first adapter is `htdt-generic-biquad@1`. It produces an immutable
`CadCalibrationExportSnapshot` containing the exact actual settings emitted by
the adapter.

Requested plan and exported settings are separate authorities:

- plan: `plan_semantic_sha256`
- actual export: `exported_settings_semantic_sha256`

If parameter quantization/resolution exists, the adapter quantizes parameters,
rebuilds the normalized coefficients, validates the resulting setting against
the original plan/device limits, and stores the changed values plus
quantization notes. It does not rewrite the requested plan.

Stable headless representations are provided as canonical JSON and deterministic
CSV. JSON readback validates the exported-settings semantic hash and the biquad
coefficient contract.

## Export/application/verification lifecycle

Lifecycle facts are append-only and follow one explicit edge set:

`proposed -> exported -> user_applied -> remeasured -> validated`

The plan itself is the proposal authority. An export event requires an exact
export ID/hash. `user_applied` is a separate user-confirmed fact; export does
not imply it. `remeasured` and `validated` require an exact preregistered
VerificationMeasurementPlan plus re-measurement IDs that reproduce a persisted
verification completion bound to that contract — bare same-scene measurement
IDs are not evidence, and `validated` additionally requires the completion's
reproduced `pass` result. `exported` may open the chain as a root fact (a plan
can be exported before lifecycle recording begins); `validated` is terminal.

Each persisted event after the first claims the exact chain head via
`supersedes_event_sha256`, and the head check runs inside the same
`BEGIN IMMEDIATE` transaction as the insert. A successor that names a stale
predecessor, skips a state, extends a terminal `validated` head, or omits the
claim is rejected with `CalibrationLifecycleConflictError`; two writers
building on the same head cannot both commit. Reopen replays the chain and
rejects any historical fork or invalid edge rather than trusting insertion
order (rows persisted before predecessor tracking carry no claim and are
tolerated as implicit extensions).

No lifecycle transition mutates SceneRevision/SystemVariant into an as-built
state.

## Verification MeasurementPlan preregistration

`CadVerificationMeasurementPlan` is the immutable re-measure contract. It binds:

- exact CalibrationPlan ID/hash,
- exact exported-settings ID/hash,
- exact SceneRevision and SystemVariant,
- measurement point positions,
- routing,
- reference SPL,
- required measurement capability claims,
- explicit before Measurement IDs.

The contract is preregistered before any after measurement can exist:
`save_verification_plan` commits the contract row together with a
`CadVerificationMeasurementPlanRegistration` under one `BEGIN IMMEDIATE`
transaction. The repository generates `registered_at_utc` at commit — the
caller-claimed `created_at_utc` can never postdate it — and registration fails
when the contract claims already-collected `after_measurement_ids` or when a
qualifying measured capture (exact SceneRevision, preregistered point and
routing, outside the before lineage) already persists. A contract built after
seeing after data can therefore never pose as the original preregistration.

After evidence attaches later through append-only
`CadVerificationMeasurementCompletion` records built by
`build_verification_measurement_completion` and persisted by
`save_verification_completion`. Each evidence row resolves the exact
measurement/dataset/quality-report identities, requires `evidence_type` of
`measured` on the contract's exact SceneRevision at a preregistered point on
preregistered routing, and must be captured no earlier than
`registered_at_utc`. The completion's `pass`/`fail` result reproduces the
contract's required-capability evaluation over the bound quality reports; the
repository rebuilds the whole record on save/read, so no claimed outcome is
trusted on payload alone.

Verification plan rows persisted before this authority existed — recognizable
by a missing registration row or a non-empty `after_measurement_ids` — stay
readable for audit but are legacy post-hoc records: no registration is ever
inferred for them, and every attestation path (completion saves, `remeasured`/
`validated` transitions) fails closed.

This is the lineage foundation for the later re-measure/holdout validation loop.
This slice does not manufacture new quality claims for the after measurements.

## Persistence and reopen

`CadCalibrationRepository` stores plans, exports, verification plans,
verification registrations, verification completions and lifecycle events in
additive native SQLite tables. Reopen revalidates every source authority and
semantic hash. A source MeasurementQualityReport hash mismatch is rejected.

## Deferred adapters and algorithms

This slice intentionally defers:

- Dirac/Trinnov or other proprietary correction implementations,
- guessed proprietary file formats,
- REW/miniDSP adapters until a stable explicit contract is selected,
- advanced automatic PEQ generation,
- coherent inter-channel phase prediction/correction,
- device installation automation,
- owned-room production recommendation enablement.

## API reused by Issue #174

Joint optimization should reuse rather than replace:

- `CadCalibrationChannel`
- `CadBiquadFilter`
- `CadTargetCurve` and `CadTargetNormalizationCondition`
- `CadDeviceCapabilityConstraints`
- `evaluate_calibration_support`
- `CadCalibrationPlan` semantic/source binding
- generic export snapshot semantics

Issue #174 may search plan parameters, but candidate plans must pass the same
measurement claim gates, hardware limits and non-clipping export contract before
they become eligible outputs.
