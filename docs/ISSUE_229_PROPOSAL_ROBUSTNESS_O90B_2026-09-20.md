# Issue #229 — proposal-aware O90B bounded multidimensional robustness

Date: 2026-09-20

## Scope

This slice extends the proposal-aware local foundation from PR #231 to the existing O90B bounded multidimensional design.

It does not add a second sampler or a second robustness summary algorithm.

The authority chain is:

```text
ProposalRobustnessSpec (exact local parent)
→ ProposalMultidimensionalRobustnessSpec
→ existing O90B deterministic multidimensional sampling plan
→ proposal perturbation samples
→ shared bounded-envelope summary
→ existing RobustnessEvaluation
```

## Exact parent authority

A multidimensional proposal spec can only be derived from one exact local `ProposalRobustnessSpec`.

The child binds:

- exact parent spec id/hash;
- exact candidate SystemVariant / O100B search/candidate lineage inherited from the parent;
- explicit sample count;
- explicit deterministic seed;
- explicit linked perturbation groups;
- existing O90B bounded sampling strategy/version.

The child is append-only and cannot be persisted before the exact local parent.

## Sampling semantics

The implementation reuses existing `build_multidimensional_sampling_plan()`.

Therefore the proposal path uses the same O90B rules for:

- nominal sample at index 0;
- explicit negative/positive corners;
- deterministic stable normalized coordinates for remaining samples;
- asymmetric minus/plus axis deltas;
- linked axis groups and multipliers;
- no probability meaning for bounded interval samples.

## Proposal materialization

Every sample starts from the exact un-applied candidate SystemVariant scene.

For each declared axis delta the implementation reuses existing `apply_local_perturbation()`.

The perturbed proposal is never applied to the baseline SceneRevision.

Every sample reruns:

- G10 constraints;
- O80 orientation constraints;
- explicit uncertainty-domain bounds.

Infeasible samples remain unscored evidence.

## Objective authority

Nominal objectives remain exact O100D `VariantEvaluationBundle` evidence.

Every scored perturbed sample requires per-objective exact `ExactAuthorityRef` evidence with the same declared:

- authority kind/version;
- evaluator id/version;
- model id/version;
- fidelity.

Different result IDs/hashes are expected for different perturbations.

## Shared bounded summary

`optimization_robustness_multidimensional.py` now exposes
`build_multidimensional_evaluations_from_provenance()`.

This helper owns only the already-established O90B result semantics:

- sampled min/max envelope;
- direction-aware sampled worst;
- feasible fraction;
- explicit non-probabilistic percentile semantics;
- existing `RobustnessEvaluation` identity.

The existing O90B path calls the helper with the same provenance fields it used before, so legacy O90B semantics and output identity remain unchanged.

The proposal path supplies proposal-specific exact provenance:

- parent proposal spec id/hash;
- sample design;
- candidate SystemVariant id/hash;
- O100B search/candidate/candidate-set hashes;
- nominal bundle id/hash;
- objective contract hash.

## Persistence / reopen

`CadProposalRobustnessRepository` accepts both:

- local `ProposalRobustnessSpec`;
- bounded `ProposalMultidimensionalRobustnessSpec`.

Reopen re-resolves the local parent, candidate lineage, nominal bundle and every scored non-nominal result authority.

Multidimensional samples are checked against the exact deterministic O90B plan.

Stored `RobustnessEvaluation` results are regenerated from the persisted samples and exact proposal sampling provenance.

## Verification

Focused tests cover:

1. deterministic 5-sample proposal bounded design;
2. negative/positive O90B corner samples;
3. hard-constraint violations retained as infeasible/unscored evidence;
4. sampled envelope and feasible fraction;
5. non-probabilistic percentile semantics;
6. parent-before-child persistence requirement;
7. child spec/sample/evaluation save/reopen;
8. stale perturbed result authority fails closed;
9. existing O90B test suite remains authoritative for unchanged legacy identities.

## Deferred

Still outside this slice:

- explicit probability-distribution proposal sampling;
- empirical/discrete proposal uncertainty models;
- cross-candidate robust Pareto binding;
- O100F final completion/roadmap closure;
- production robust recommendation evidence gate.

RDC usage: 0.
