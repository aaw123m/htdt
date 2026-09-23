# Issue #169 — O100D coverage / directivity / worst-seat authority

Date: 2026-09-19

## Scope

This slice consumes the merged O100C DirectivityDataset numerical authority and the
direction-aware ObjectiveDefinition authority. It does not alter the direct SPL /
headroom evaluator from PR #205.

Implemented in dedicated modules:

- backend/src/htdt/cad_coverage.py
- backend/src/htdt/cad_coverage_repository.py
- backend/tests/test_cad_coverage.py

## Evaluation authority

CoverageEvaluationScenario is immutable and semantically hashed. Its identity contains:

- exact source entity and channel role
- exact required SeatPopulation
- exact EquipmentDefinition id/version/hash
- exact DirectivityDataset id/version/hash
- requested frequency set
- explicit frequency aggregation semantics
- explicit relative-level coverage threshold
- equal_unweighted seat weighting
- fail_closed_required_population missing-seat policy
- source-angle conversion convention
- versioned off-axis loss equation
- algorithm id/version

Different threshold, requested frequencies, aggregation, seat population, equipment, or
dataset therefore produce a different scenario hash and a different ObjectiveDefinition
comparison model.

No partial required-seat population is silently evaluated.

## Source-relative geometry

The existing CAD semantics are preserved rather than redefined:

- SceneEntity.aim_xyz is the explicit world-space acoustic reference axis.
- SceneEntity.orientation remains body pose and is not substituted for missing aim.
- EquipmentDefinition.acoustic_reference_point_m is transformed by body pose to obtain
  the exact source acoustic reference position.
- seat acoustic references are resolved with the existing
  acoustic_reference_position() authority.

For the source frame, explicit aim is forward. Body local +Z, transformed by body pose
and projected orthogonal to aim, supplies only the roll/up reference. A missing aim or a
degenerate projected up reference is UNSUPPORTED instead of guessed.

The conversion then computes the normalized source-to-receiver direction and explicit
left-positive horizontal / up-positive vertical angles. The formula is versioned as
explicit-aim-body-up-source-frame-1 and converts to the DirectivityDataset declared
horizontal_vertical or spherical_azimuth_elevation convention.

## Directivity evaluation and loss

Each required seat and requested frequency calls the existing evaluate_directivity()
with request=magnitude. Complex directivity is not required for coverage.

The result retains:

- requested frequency
- source-relative angles
- exact DirectivityEvaluation semantic hash
- exact on-axis reference DirectivityEvaluation semantic hash
- dataset relative level dB
- reference level dB
- off-axis loss dB
- SUPPORTED / UNSUPPORTED state and reason

Off-axis loss is explicitly:

loss_db = reference_level_db - evaluated_relative_level_db

The authority version is
reference-level-db-minus-evaluated-relative-level-db-1. Negative loss is not clamped, so
an off-axis lobe stronger than the reference direction remains representable.

## Frequency and seat aggregation

The initial authority supports two explicit frequency aggregations:

- worst_over_requested_frequencies
  - relative level: minimum over requested frequencies
  - off-axis loss: maximum over requested frequencies
- mean_over_requested_frequencies
  - arithmetic mean of dB relative levels
  - arithmetic mean of dB loss values

There is no implicit averaging.

Coverage criterion is:

aggregated_relative_directivity_level >= coverage_threshold_db

When every required seat is supported, the evaluation exposes:

- per-seat aggregated relative directivity level
- per-seat aggregated off-axis loss
- per-seat coverage pass/fail
- useful coverage fraction
- worst-seat relative directivity level
- worst-seat off-axis loss
- seat-to-seat directivity spread

If any required seat cannot be evaluated, population aggregates are UNSUPPORTED and no
coverage fraction is calculated from only the evaluable subset.

## Objective authority

Coverage evaluation maps to formal ObjectiveDefinition values only; no composite cinema
score is introduced:

| Objective | Unit | Direction |
| --- | --- | --- |
| o100d.coverage.useful_fraction | ratio | maximize |
| o100d.directivity.worst_seat_relative_level_db | dB | maximize |
| o100d.directivity.worst_seat_off_axis_loss_db | dB | minimize |
| o100d.directivity.seat_to_seat_spread_db | dB | minimize |

Coverage fraction has valid domain [0, 1]. Spread is nonnegative. Relative level and
off-axis loss are finite-real domains; off-axis loss is intentionally not constrained to
nonnegative values.

The exact CoverageEvaluationScenario SHA-256 is the comparison_model_version, making
different coverage semantics comparison-incompatible through the existing Pareto gate.

## Persistence

CadCoverageRepository uses append-only native SQLite records for scenarios and
evaluations. Save/reopen validation resolves and checks:

- exact SceneRevision id/content hash
- exact SystemVariant id/hash/baseline
- exact EquipmentDefinition id/version/hash
- exact DirectivityDataset id/version/hash
- exact source equipment binding
- exact scenario/evaluation semantic identities

## Fail-closed behavior

No 0 dB or partial-population substitution is made for:

- dataset/equipment/scenario mismatch
- frequency outside dataset domain
- angle outside dataset domain
- unsupported interpolation
- missing required seat
- missing seat acoustic reference
- missing explicit speaker aim
- ambiguous/degenerate source-frame orientation

Structural exact-authority mismatches are rejected. Geometry/directivity capability
failures are retained as explicit UNSUPPORTED evaluation evidence.

## Deliberate boundaries

This slice does not:

- reimplement cad_directivity.py
- change PR #205 direct-level SPL/headroom evaluation
- apply off-axis correction to SPL
- add room gain or reflections
- add percentile semantics
- add multi-channel coverage
- modify Pareto or O90 core
- edit common roadmap/status documents

## Focused verification

backend/tests/test_cad_coverage.py covers:

1. on-axis seat
2. off-axis seat
3. body yaw changing the source acoustic reference geometry without redefining aim
4. multiple required seats
5. multiple requested frequencies
6. coverage threshold and useful fraction
7. worst-seat relative level
8. worst-seat off-axis loss
9. seat-to-seat directivity spread
10. explicit worst versus mean frequency aggregation identity
11. frequency-domain outside fail-closed
12. missing required seat fail-closed
13. missing explicit source aim fail-closed
14. exact EquipmentDefinition / DirectivityDataset mismatch rejection
15. negative off-axis loss without clamp
16. deterministic scenario/evaluation save and reopen
17. coverage maximize plus loss minimize direction-aware Pareto

The existing O90 regression
test_o90a_maximize_sampled_worst_uses_low_side_and_round_trips is retained unchanged and
is part of the verification target for maximize sampled_worst semantics.

RDC used: 0.
