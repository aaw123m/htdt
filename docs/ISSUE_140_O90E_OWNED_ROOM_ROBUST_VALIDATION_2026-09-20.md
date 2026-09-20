# Issue #140 / O90E — Owned-room Robust Validation Software Authority

Date: 2026-09-20  
Branch: `issue-140-o90e-owned-room-validation`  
Tracking: Issue #140

## Scope

This slice implements the software authority needed to decide whether one exact
O90 `RobustnessSpec` may claim `production_owned_room` robustness support.

The authority flow is:

```
exact RobustnessSpec / candidate
  -> preregistered perturbation validation case(s)
  -> exact existing O60 validation / sensitivity evidence
  -> exact measurement plan / measurement / dataset / acquisition / quality evidence
  -> perturbation-domain applicability and capability checks
  -> immutable O90E production validation decision
```

O90E does **not** create a new model-validation flag. Existing O60 authority is
reused by exact ID/hash binding. An R180 validation reference may be represented
only when a repository authority for it exists; this slice does not invent one.
Until such authority is resolvable, that path fails closed.

## Evidence boundary

- Actual owned-room evidence available in this repository: **no**.
- Synthetic fixtures validate software behavior only.
- Synthetic evidence can never open the production gate.
- A nominal O60 model validation never implies that an O90 perturbation domain
  is validated.
- A measurement existing in the database never means that a robustness claim is
  validated.
- Measurement capability remains the existing
  `CadMeasurementQualityReport` / `gate_measurement_claim` authority.
- O60 sensitivity semantics remain the existing `CadSensitivityCheck` and
  campaign-backed `CadModelValidationRecord` authority.

Therefore the repository's real `production_owned_room` gate remains
**closed** until genuine preregistered owned-room evidence is supplied.

## Preregistration contract

An immutable validation case freezes before capture:

- exact RobustnessSpec ID/SHA;
- exact candidate ID/SHA and SceneRevision/content hash;
- one exact O90 uncertainty axis;
- nominal axis state and one target perturbation state;
- target perturbation candidate / Measurement Plan;
- observable and frequency band;
- receiver entity and position;
- source/channel/radiation identity;
- required measurement capability;
- comparison/tolerance purpose;
- expected O60 sensitivity evidence relation;
- preregistration timestamp;
- schema/version/hash.

A case whose measurement was captured before the effective preregistration
authority is retrospective evidence. It remains auditable but cannot satisfy
the preregistered production gate.

Existing O60 campaign preregistration may serve as an earlier authority only
when the case can be proven to be a strict specialization of the same exact
campaign: same search/model/candidate set, same sensitivity candidate pair and
observable, and the case itself does not broaden the campaign's predeclared
requirements.

## Applicability / coverage contract

A production decision compares:

- exact underlying O60 validated model/search/config authority;
- the tested perturbation magnitude;
- every O90 bounded axis endpoint required by the exact RobustnessSpec;
- observable and requested frequency band;
- exact source/receiver measurement configuration.

Coverage is explicit. Missing one required signed endpoint yields incomplete or
partial support; it is never promoted to full production robustness. A model
that is nominally validated but lacks perturbation sensitivity coverage remains
model-conditioned / unsupported for the robust domain.

## Capability contract

O90E consumes the existing measurement-quality capability matrix. Missing
AcquisitionContext, insufficient usable band, FR-only evidence used for a phase
or timing claim, or any BLOCKED/UNKNOWN required capability keeps the production
gate closed. Missing capability is never converted to a numeric zero or PASS.

## Persistence

O90E records are append-only:

- preregistration cases are immutable;
- validation decisions are immutable historical evidence;
- revalidation creates a new decision;
- save/reopen re-resolves exact O90, O60, measurement-plan, measurement,
  dataset, quality, acquisition, and prediction bindings;
- missing, stale, or tampered bindings fail closed.

## Decision reasons

The typed decision authority distinguishes at least:

- eligible;
- missing underlying model validation;
- missing preregistration;
- missing measurement evidence;
- insufficient measurement capability;
- perturbation domain outside validated applicability;
- stale model/result;
- wrong SceneRevision;
- wrong candidate;
- quality failure;
- incomplete required perturbations;
- retrospective evidence;
- synthetic evidence.

No hidden robustness score is introduced.

## Validation plan

Focused repository tests cover valid and invalid exact bindings, preregistration,
O60 sensitivity reuse, applicability, capability, stale/tampered evidence,
partial coverage, synthetic and retrospective evidence, append-only history,
and save/reopen. CI must pass without physical measurement hardware or owned-room
data; absence of real evidence is expected to leave the production gate closed.

## UI / hardware boundary

No O90D UI rewrite and no Windows visual acceptance are part of this slice.
UX160 remains separate. RDC usage: **0**.
