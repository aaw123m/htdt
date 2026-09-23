# O60 Model Validation implementation plan

Tracking: Issue #75  
Base authority: accepted N80c/O50 head `fc3e37b0e4eb9e40b563a61ba95b4a7008c86c43`

## Goal

Extend the existing immutable O60 residual record into an independently inspectable validation record that can answer, per model/search authority:

- whether holdout predicted-vs-measured objective trends preserve pairwise ordering;
- whether small placement perturbations produce unstable objective changes;
- whether repeated same-condition measurements establish a noise/repeatability floor;
- whether candidate differences are distinguishable above that floor;
- whether model geometry/band/routing applicability is explicitly satisfied.

No scalar sound-quality score is introduced. Each objective remains independent.

## Authority

Every validation remains bound to exact:

- native document ID;
- SearchSpec ID + SHA;
- candidate-set SHA;
- model ID/version;
- completed O20 prediction attempts;
- completed O50 Measurement Plans;
- N60 `measured` evidence.

Calibration and holdout candidate IDs are disjoint. Synthetic fixtures validate algorithms only and may never make automatic recommendation eligible.

## Validation components

### Trend / pairwise ordering

For each objective independently, compare every usable holdout candidate pair:

- predicted delta sign;
- measured delta sign;
- concordant / discordant / tie/ambiguous result;
- comparable-pair count and agreement ratio.

A configurable objective-value tie tolerance is saved in the record. No cross-objective rank is created.

### Sensitivity

For explicitly selected nearby candidate pairs, save:

- placement distance in metres;
- predicted objective delta;
- measured objective delta;
- absolute measured-vs-predicted delta error;
- normalized error per metre;
- configured per-objective threshold.

Sensitivity is evaluated per objective and cannot be collapsed into one global score.

### Repeatability

Repeated measurements bound to the same SceneRevision are compared pairwise in the validation band. Save:

- measurement IDs;
- pairwise response RMS/shape RMS;
- aggregate repeatability floor;
- sample count.

Candidate measured-response separation is then compared with that floor. If candidate separation is not larger than the configured repeatability multiple, automatic recommendation remains disabled.

### Applicability

Save explicit checks such as:

- requested band within validated model band;
- room geometry supported without silent approximation;
- routing/channel evidence known;
- required acoustic-reference semantics present.

Any failed applicability check is an immutable stop reason.

## Gate semantics

`recommendation_gate=eligible` requires all of the following:

1. evidence scope is real/owned-room, never synthetic fixture;
2. residual holdout gate passes;
3. every required objective trend check has enough comparable holdout pairs and meets its agreement threshold;
4. every required sensitivity check passes;
5. repeatability evidence exists and candidate separation exceeds the configured noise floor margin;
6. all required applicability checks pass.

Otherwise the record stays `disabled` and stores concrete reasons.

This gate only permits O70 to consume the record. It does not itself rank candidates or claim a model is universally valid.

## Implementation slices

1. Pure immutable O60 v2 models/calculators with deterministic focused fixtures.
2. Repository cross-evidence validation for all prediction/measurement IDs.
3. Native validation panel showing calibration/holdout, residual, trend, sensitivity, repeatability and stop reasons separately.
4. GitHub Actions verification.
5. Owned-room validation only when real repeat/holdout evidence exists. RDC is not used merely to exercise synthetic fixtures.

## Non-goals

- automatic physical movement, AVR control or REW playback;
- single composite score;
- synthetic-only validation claim;
- O70 Bayesian/adaptive recommendation before this gate is independently satisfied.
