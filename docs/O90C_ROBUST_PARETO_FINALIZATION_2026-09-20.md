# O90C — exact robust Pareto and multi-fidelity finalization

Date: 2026-09-20

## Scope

This slice establishes the typed common-fidelity final authority for legacy O90 candidates.

It connects the generic multi-fidelity screening authority from PR #228 to the existing O90 nominal/robust Pareto semantics.

The authority chain is:

```text
exact SceneRevision / SearchSpec candidate set
+ exact CadObjectiveEvaluation per candidate
+ exact RobustnessSpec / RobustnessEvaluation per candidate
+ explicit RobustParetoSelection
→ existing robust_pareto_front()
→ O90RobustParetoEvaluation
→ O90 multi-fidelity finalization
```

No new Pareto algorithm or aggregate robustness score is introduced.

## Cross-candidate comparison contract

Before robustness evaluations can be placed in one Pareto comparison, every candidate must share:

- exact SceneRevision;
- exact SearchSpec;
- exact candidate-set hash;
- prediction model id/version;
- prediction provider;
- fidelity;
- objective-evaluation spec hash;
- sampling strategy and algorithm version;
- sample budget where applicable;
- uncertainty-axis semantics;
- linked-axis dependence model;
- explicit probability/empirical/discrete uncertainty model where applicable.

For bounded axes, comparison uses the tolerance domain relative to each candidate's nominal position:

- minus / plus delta;
- allowed-min delta relative to nominal;
- allowed-max delta relative to nominal.

Different candidate coordinates are therefore permitted while different tolerance domains are not silently compared.

## O90RobustParetoEvaluation

The immutable evaluation binds:

- exact SceneRevision / SearchSpec / candidate set;
- exact nominal CadObjectiveEvaluation per candidate;
- exact RobustnessSpec per candidate;
- exact selected RobustnessEvaluation per objective;
- explicit RobustParetoSelection;
- compatibility-signature hash;
- existing direction-aware ParetoResult.

The Pareto axes remain explicit:

- `nominal::<objective-id>`;
- `robust.sampled_worst::<objective-id>`.

The implementation delegates vector construction and dominance to the existing:

- `build_nominal_robust_pareto_vector()`;
- `robust_pareto_front()`;
- O40 `pareto_front()`.

## Persistence / reopen

`CadO90RobustParetoRepository` stores evaluations append-only.

Reopen re-resolves:

- every nominal CadObjectiveEvaluation;
- every exact RobustnessSpec;
- every selected RobustnessEvaluation.

The complete robust Pareto is regenerated and must exactly match the persisted authority.

Stale/missing robustness evidence fails closed.

## Multi-fidelity finalization

`finalize_o90_multifidelity()` extends the shared PR #228 finalization contract to `o90_robustness`.

The exact screening survivor set must equal the exact robust-Pareto candidate set.

For every survivor:

- candidate id must match;
- candidate semantic SHA-256 must equal the RobustnessSpec candidate SHA-256.

Only then can the finalization bind the O90RobustParetoEvaluation.

Claim state remains evidence-state semantics:

- `COMPLETE` only after `READY_FOR_COMMON_FIDELITY`;
- `PRELIMINARY_BUDGET` after budget defer;
- `BLOCKED_EVIDENCE` after missing/blocked evidence.

## Repository integration

`CadMultiFidelityRepository` now accepts either typed final authority:

- O100: `TopologyComparisonEvaluation`;
- O90: `O90RobustParetoEvaluation`.

An opaque final-comparison hash is not sufficient.

Save/reopen resolves the correct typed repository according to the plan domain and recomputes finalization.

## Verification

`backend/tests/test_cad_robust_pareto.py` uses real SearchSpec / candidate / objective / robustness builders and verifies:

1. two common-authority candidates form an explicit nominal-vs-sampled-worst trade-off;
2. existing direction-aware robust Pareto semantics are reused;
3. fidelity mismatch makes robustness comparison ineligible;
4. robust-Pareto save/reopen re-resolves exact authorities;
5. stale robustness evaluation fails closed;
6. O90 multi-fidelity screening survivors bind to the exact robust Pareto;
7. finalization save/reopen through typed repository;
8. candidate hash mismatch blocks finalization.

## Relationship to R140

This slice owns final comparison semantics, not execution.

R140/O90C execution/cache authority is implemented separately in PR #236:

- exact execution task identity;
- capacity-bounded scheduling;
- exact cache/resume;
- stale result rejection.

The two authorities compose without the scheduler redefining O90 comparison semantics.

## Deferred

- actual R140 worker execution;
- automatic resource estimation;
- CPU/GPU numerical equivalence fixture;
- O90D UX;
- O90E owned-room robust validation.

RDC usage: 0.
