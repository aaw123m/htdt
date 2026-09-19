# Issue #229 — exact proposal robust Pareto binding

Date: 2026-09-20

## Scope

This slice completes the nominal/robust comparison requirement for proposal-aware O100F candidates.

It does not implement a new Pareto algorithm.

The authority chain is:

```text
exact O100D TopologyComparisonEvaluation
+ exact VariantEvaluationBundle for every O100D-eligible candidate
+ exact proposal robustness spec/evaluations
+ explicit RobustParetoSelection
→ existing build_nominal_robust_pareto_vector()
→ existing robust_pareto_front()
→ ProposalRobustParetoEvaluation
```

## Candidate-set rule

The robust Pareto candidate set must equal the exact O100D ELIGIBLE candidate set.

A caller cannot silently:

- drop a difficult eligible candidate;
- add an ineligible candidate;
- substitute a different bundle;
- substitute a different SystemVariant hash.

Each candidate binds the exact O100D bundle id/hash and variant id/hash.

## Objective compatibility

Every selected nominal or robustness objective must already be part of the O100D common-compatible Pareto objective set.

This delegates definition/unit/direction/model/fidelity compatibility to the existing O100D authority before robust Pareto evaluation begins.

For each selected robustness objective, the exact `RobustnessEvaluation` must bind:

- the same candidate SystemVariant id;
- the exact proposal robustness spec id/hash;
- the selected objective id.

The existing `build_nominal_robust_pareto_vector()` additionally checks the nominal and robustness ObjectiveDefinition/unit/direction authority.

## Pareto semantics

The implementation calls the existing `robust_pareto_front()`.

The resulting axes remain explicit and separate:

- `nominal::<objective-id>`
- `robust.sampled_worst::<objective-id>`

No weighted score or hidden robustness aggregate is introduced.

Direction-aware dominance remains the existing O40/O90 authority.

## Persistence / reopen

`CadProposalRobustParetoRepository` persists evaluations append-only.

Reopen re-resolves:

- exact O100D TopologyComparisonEvaluation id/hash;
- exact VariantEvaluationBundle id/hash for every eligible candidate;
- exact proposal robustness spec id/hash;
- every selected RobustnessEvaluation id/hash.

The complete robust Pareto authority is rebuilt and must match the persisted result.

## Focused verification

Tests cover:

1. O100D nominal-only Pareto can differ from the explicit nominal/robust trade-off Pareto;
2. the O100D eligible candidate set cannot be silently reduced;
3. derived objective axes are the existing nominal/robust axis names;
4. append-only save/reopen;
5. missing/stale robustness evaluation fails closed.

## Non-goals

- no new Pareto implementation;
- no weighted objective score;
- no automatic recommendation/deployment;
- no proposed → as-built lifecycle promotion;
- no O60/R180 evidence-gate bypass;
- no probability/empirical/discrete proposal uncertainty extension.

RDC usage: 0.
