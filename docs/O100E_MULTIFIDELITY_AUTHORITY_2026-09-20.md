# O100E — auditable multi-fidelity topology authority

Date: 2026-09-20

## Scope

This slice establishes the solver-neutral staged-fidelity authority required by O100E and reusable by O90C.

It does not implement a scheduler, a new optimizer, or a new acoustic solver.

The authority chain is:

```text
exact baseline authority
+ exact candidate authorities
+ immutable fidelity-stage definitions
→ per-stage exact outcomes/evidence
→ screening evaluation
→ surviving exact candidate set
→ existing O100D common-fidelity final comparison
→ persisted multi-fidelity finalization
```

## Shared stage semantics

`MultiFidelityStageDefinition` distinguishes four policies.

### hard_gate

A candidate may be pruned only with exact evidence from the declared hard-gate evaluator.

Examples:

- collision;
- geometric infeasibility;
- explicit layout/profile hard constraint.

### validated_screening

A cheaper/approximate model may prune only when the stage also binds an exact
`validated_screening_relationship_ref`.

The existence of a cheap score alone never authorizes elimination from a stronger downstream objective.

### shortlist_only

This policy may reduce compute expenditure, but candidates omitted because of budget are recorded as `DEFERRED_BUDGET`, not `PRUNED`.

A run containing budget deferral remains `PRELIMINARY_BUDGET`.

### final_common_fidelity

Exactly one final stage is required and it must be last.

The final stage itself is not represented as another cheap screening result. O100E instead binds to the existing exact O100D `TopologyComparisonEvaluation`.

## Stage outcomes

Every candidate entering a stage receives exactly one ordered outcome:

- `ADVANCE`
- `PRUNED`
- `DEFERRED_BUDGET`
- `BLOCKED_EVIDENCE`

Pruning requires exact evidence. Budget deferral and missing evidence are preserved separately.

Stage N+1 must consume exactly the survivor set produced by stage N. A later stage cannot silently reintroduce a pruned candidate or omit an advanced candidate without recording the appropriate decision.

## Screening state

`MultiFidelityScreeningEvaluation` derives one of:

- `READY_FOR_COMMON_FIDELITY`
- `PRELIMINARY_BUDGET`
- `BLOCKED_EVIDENCE`

This state is an evidence-completeness state, not an acoustic score.

## O100D finalization

`finalize_o100_multifidelity()` binds the exact screening survivor set to one existing `TopologyComparisonEvaluation`.

The final O100D eligible candidate set must equal the exact screening survivor set.

This preserves the existing O100D rules for:

- objective definition/unit/direction;
- evaluator/model identity;
- declared fidelity compatibility;
- required missing/unsupported semantics;
- direction-aware Pareto.

No second Pareto or scoring implementation is introduced.

A complete screening lane yields `COMPLETE`.
A budget-limited lane yields `PRELIMINARY_BUDGET`.
A missing-evidence lane yields `BLOCKED_EVIDENCE`.

## Persistence / reopen

`CadMultiFidelityRepository` stores append-only:

- plans;
- stage results;
- screening evaluations;
- finalizations.

Save/reopen deterministically regenerates stage results and screening evaluation from the persisted plan.

O100 finalization additionally requires a typed topology-comparison resolver and re-resolves the exact persisted final `TopologyComparisonEvaluation`; an opaque comparison hash alone is insufficient.

## O90C relationship

The generic plan/stage/outcome/screening authority is intentionally domain-neutral between:

- `o100_topology`
- `o90_robustness`

This PR completes the O100E final-comparison adapter only.

A later O90C slice should bind an exact RobustnessEvaluation / robust Pareto authority to the same screening contract instead of creating separate staged-fidelity semantics.

## Deliberately deferred

- job scheduler / worker pool;
- CPU/GPU planning;
- cache execution policy;
- resource estimation;
- actual acoustic solver execution;
- automatic selection of stage thresholds;
- statistical calibration of screening relationships;
- O90C final robustness adapter;
- UX.

Those belong to R140/O90C/O100G or later capability-specific work.

## Verification

`backend/tests/test_cad_multifidelity.py` verifies:

1. deterministic hard-gate pruning and exact stage chaining;
2. validated approximate screening requires an exact relationship authority;
3. shortlist budget omission remains PRELIMINARY rather than PRUNED;
4. shortlist-only stages cannot claim approximate pruning;
5. O100D final eligible set must exactly equal screening survivors;
6. plan/stage/screening/finalization save/reopen;
7. finalization reopen requires a typed final-comparison authority resolver.

RDC usage: 0.
