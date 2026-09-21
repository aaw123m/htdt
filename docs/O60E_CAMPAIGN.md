# O60E Owned-Room Validation Campaign

Tracking: Issue #81

## Problem

O60 software validation already separates calibration and holdout inside a
ValidationRecord, but the split can currently be supplied when the record is
built. For real owned-room evidence that permits choosing holdout candidates
after observing measurements.

O60E preregisters the study before candidate measurements are completed. O70
must not consume an owned-room validation that is not tied to such a persisted
campaign.

## Immutable campaign authority

A campaign freezes:

- document, SearchSpec id/SHA and candidate-set SHA;
- model id/version;
- requested validation band and holdout residual threshold;
- candidate ids and calibration/holdout split;
- canonical target response, reference/excluded bands, objective-evaluation spec/SHA and objective ids;
- trend tolerances/minimum comparable pairs/minimum agreement ratio;
- sensitivity candidate pairs and thresholds;
- candidates requiring repeated measurements and minimum repeat count;
- candidate-separation pairs and repeatability reference candidate;
- required applicability check codes;
- caller-claimed creation timestamp and campaign identity SHA.

The repository regenerates the exact SearchSpec candidate set before saving and
rejects unknown candidate ids or a changed candidate-set SHA.

The caller-claimed `created_at_utc` is campaign identity data only. Durable
preregistration is attested by a `CadValidationCampaignRegistration` row the
repository creates inside the same `BEGIN IMMEDIATE` write transaction as the
campaign row, after proving that no qualifying candidate-linked measurement
evidence already exists. Downstream capture-time and model-validation gates
compare against `registered_at_utc` and the registration id/SHA, never the
caller-supplied timestamp. A campaign row without a registration row fails
closed; no historical registration time is silently inferred.

For every candidate in the campaign, qualifying measured evidence must not
already exist at campaign-commit time: no measured Measurement Plan for a
campaign candidate, no measured capture under a candidate's applied scene
revision, and no measurement already claiming the campaign through owned-room
provenance. Planned-but-not-measured plans are allowed because they can be
prepared before physical measurement. Because the evidence check and the
insertion serialize inside one write transaction, a racing measurement
completion versus campaign registration resolves deterministically: either the
campaign commits first, or the registration fails.

## Readiness

Readiness is derived from immutable repositories and never creates or
reclassifies evidence. For each campaign candidate it reports:

- one exact completed Room Simulator prediction attempt for the campaign model;
- one completed Measurement Plan and its measured N60 evidence;
- predicted/measured objective evaluations whose evaluation-spec SHA exactly
  equals the preregistered campaign spec;
- repeatability count for candidates that require repeated measurements;
- measurement capture time must be after campaign preregistration;
- measured provenance must contain `validation_scope=owned_room` and the exact campaign id.

Missing or ambiguous evidence is listed explicitly. No default ranking or
automatic physical action is performed.

## Native owned-room measurement ingestion

Normal N60 import remains generic measurement ingestion and does not become
owned-room validation evidence automatically.

The optimization workspace exposes an explicit
`選択REW→Campaign実測` action. It is available only when:

- a persisted campaign is selected;
- a planned Measurement Plan for one of that campaign's candidates is selected;
- the current clean SceneRevision exactly equals the plan's applied revision;
- the REW measurement is read through the API path.

That action freezes `evidence_type=measured`, `validation_scope=owned_room`
and the exact campaign id into measurement provenance. REW text import is not
promoted to O60E evidence because it does not provide a trustworthy capture
timestamp.

REW 5.40 may expose a local wall-clock date without an offset, for example
`2026-Sep-16 12:11:10`. Because the supported REW API is localhost on the
owned Windows machine, the adapter interprets that legacy form with the host's
local timezone rules for the measurement date, stores an offset-aware ISO
timestamp, and preserves both the raw date and interpretation source in
provenance. An already offset-aware source timestamp is preserved.


## Validation build

Owned-room full validation is built from the persisted campaign. Candidate split
and thresholds come from the campaign, not from a caller-supplied BuildSpec.

Evidence selection is deterministic and fail-closed:

- the response measurement for a candidate is the earliest captured measured
  evidence in its completed Measurement Plan;
- prediction requires exactly one matching completed attempt, otherwise
  readiness is ambiguous;
- objective evidence requires exactly one matching predicted and one matching
  measured evaluation for each objective and campaign evaluation-spec SHA;
- repeatability uses all measured evidence in the campaign candidate plan;
- required applicability codes must be supplied exactly once when the record is
  built;
- applicability checks are never auto-passed by caller claim. Each persisted
  `CadApplicabilityCheck` binds the registered evaluator identity and version,
  the canonical evaluated subject and typed `evidence_refs` sealed by
  `decision_sha256`. Automated codes (`geometry`, `band`, `routing`) are
  re-derived from the exact SceneRevision/SearchSpec/prediction-batch/
  Measurement Plan authorities; `manual` codes bind a persisted immutable
  `CadApplicabilityAttestation` (actor, timestamp, subject, evidence). The
  repository replays every check at save and on authoritative reads, so a
  fabricated or foreign PASS fails closed. The native UI exposes
  `未確認/自動評価/手動証跡` for geometry, band and routing.

A resulting owned-room ValidationRecord stores the campaign id and SHA. The
validation repository independently rechecks the campaign, candidate split,
objective-evaluation spec, thresholds, sensitivity/repeatability/separation
requirements, applicability codes, capture time and measurement campaign
provenance before persistence. The same binding is rechecked when O70 asks for
the latest eligible record.

## O70 boundary

The O70 entry API may return only a persisted `eligible` ValidationRecord whose
owned-room campaign exists and matches the record. Synthetic fixtures and legacy
owned-room records without a campaign never open the automatic-recommendation
gate.

## Non-goals

- no autonomous speaker movement;
- no autonomous REW playback;
- no synthetic promotion to owned-room evidence;
- no adaptive planner implementation before a genuine campaign passes O60.
