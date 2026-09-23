# Issue #174 — joint physical placement + DSP foundation

Date: 2026-09-19

## Scope

This slice adds a dedicated, solver-neutral orchestration authority for joint
physical placement and DSP candidate comparison. It deliberately does not add a
new canonical O-stage, acoustic solver, room-response synthesizer, placement
engine, production recommendation gate, or O90 implementation.

The implementation reuses:

- `SceneRevision` and immutable `SystemVariant` for physical identity.
- O10 `CadSearchSpec` and optional extended-search authority for physical
  decision-variable ownership.
- #173 `CadCalibrationPlan`, `MeasurementQualityReport`, and
  `evaluate_calibration_support` for DSP authority and post-generation
  capability checking.
- Existing `ObjectiveDefinition`, `ObjectiveVector`, and direction-aware
  `pareto_front`.
- Existing O90 identity by exact reference plus an explicit joint-variable to
  O90-axis mapping. DSP perturbations remain `none` in this slice.

## New authorities

`backend/src/htdt/cad_joint_optimization.py` defines:

- immutable/versioned `JointOptimizationSpec`
- exact physical and DSP decision-variable declarations
- exact source measurement, quality report, device capability, routing snapshot,
  evaluator/model/fidelity, hard-constraint, and O90 references
- `JointCandidate` as exact composition of a physical `SystemVariant` and,
  when applicable, a separate `CalibrationPlan`
- deterministic candidate semantic hash/ID and canonical decision vector
- post-generation CalibrationPlan support evaluation
- explicit required-frequency bands on magnitude-bearing DSP decision variables
- explicit resolution and routing-rewrite blocking
- objective evaluation bindings without fake response generation
- explicit unsupported objective vectors for absent numeric evaluators
- existing Pareto reuse with model/fidelity/spec compatibility checks
- selection references whose state is always `selected_only`,
  `not_applied`, and `not_exported`

The DSP variable vocabulary for this first slice is intentionally restricted to
gain, PEQ frequency/Q/gain, crossover frequency/order, delay, and polarity.
All-pass, arbitrary phase correction, and implicit routing rewrite are not
search variables.

`backend/src/htdt/cad_joint_optimization_repository.py` adds append-only
persistence for specs, candidates, evaluation bindings, and selections, with the
persisted candidate count capped by the spec candidate budget. It
validates persisted SceneRevision/SystemVariant/CalibrationPlan references but
does not call SystemVariant application or CalibrationPlan export/lifecycle
operations.

## Capability behavior

A DSP or joint candidate is not treated as supported merely because its search
bounds were legal. After the candidate CalibrationPlan exists, the joint layer
calls #173 `evaluate_calibration_support` again. This preserves the existing
measurement/device authority for:

- magnitude response plus required-band coverage
- common timing for delay
- polarity authority for polarity inversion
- filter count
- boost/cut limits
- gain/delay bounds
- physical output/crossover constraints

Magnitude-bearing joint decision variables also carry an explicit required band and
are checked with `gate_measurement_claim` after candidate generation, including
full-band gain decisions for which #173 cannot infer a band from a single filter
frequency.

The joint layer additionally fail-closes values that are not aligned to declared
device gain, delay, frequency, Q, or filter-gain resolutions. Routing/output
mapping is snapshotted from the base CalibrationPlan and a candidate rewrite is
blocked.

Synthetic deterministic fixtures may be used only with evaluator provenance
that explicitly records `fixture_only=true`, `synthetic=true`, and
`production_eligible=false`.

## Focused acceptance fixtures

`backend/tests/test_cad_joint_optimization.py` covers the requested first
slice:

1. position-only candidate
2. DSP-only candidate
3. joint candidate
4. magnitude-safe PEQ
5. delay blocked without common timing
6. polarity blocked without polarity authority
7. boost-limit violation
8. filter-count violation
9. compatible objective Pareto across candidate classes
10. incompatible spec/model/fidelity comparison rejection
11. deterministic candidate identity
12. deterministic save/reopen
13. selected candidate does not mutate SceneRevision
14. selection does not auto-export or advance applied state

The fixture evaluator is explicitly synthetic and production-ineligible. No
synthetic frequency response or room response is generated.
