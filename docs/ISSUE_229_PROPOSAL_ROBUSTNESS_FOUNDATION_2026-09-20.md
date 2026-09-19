# Issue #229 — proposal-aware O90 local robustness foundation

Date: 2026-09-20

## Scope

This slice connects an un-applied O100B placement proposal to existing O90A local robustness semantics without pretending that proposed entities already exist in the baseline SceneRevision.

The exact authority chain is:

```text
baseline SceneRevision
+ template SystemVariant
+ TopologyPlacementSearchSpec
+ TopologyPlacementCandidate
+ persisted candidate SystemVariant
+ VariantEvaluationBundle
→ ProposalRobustnessSpec
→ existing O90 local stencil / perturbation semantics
→ proposal perturbation samples
→ existing RobustnessEvaluation
```

No proposed SystemVariant is applied to the baseline.

## Why a separate proposal authority is required

Current O90 `RobustnessSpec` is intentionally bound to:

- exact SceneRevision;
- persisted `CadSearchSpec`;
- `CadCandidate | CadExtendedCandidate`;
- `CadObjectiveEvaluation`.

An O100B placement candidate lives instead on a virtual proposal scene and may contain speaker entities that do not exist in the baseline revision.

Converting such a proposal into a normal CadCandidate would falsely assert that the proposed speaker belongs to baseline truth.

This slice leaves existing O90A/B identities unchanged.

## ProposalRobustnessSpec

The immutable proposal spec binds:

- exact baseline SceneRevision id/hash;
- exact template SystemVariant id/hash;
- exact candidate SystemVariant id/hash;
- exact O100B placement search id/hash;
- exact placement candidate id/hash;
- exact candidate-set hash;
- exact materialized proposal scene hash;
- exact O100B constraint snapshot / G10 spec hashes;
- exact nominal O100D VariantEvaluationBundle id/hash;
- explicit objective IDs;
- exact objective definition/evaluator/model/fidelity signature hash;
- existing O90 `UncertaintyAxis` definitions;
- existing O90A deterministic-local-stencil algorithm version.

The spec identity excludes creation timestamp, matching existing immutable-authority identity practice.

## Exact lineage validation

The builder proves:

1. template and candidate variants bind the same exact baseline;
2. candidate variant descends from the template;
3. topology search binds the exact template and baseline;
4. topology candidate binds the exact search/topology option;
5. candidate SystemVariant O100B provenance matches search/candidate hashes;
6. `topology_candidate_document()` and `materialize_system_variant()` produce the same proposal scene hash;
7. nominal VariantEvaluationBundle binds the exact candidate SystemVariant.

Persistence additionally re-resolves `CadTopologySearchRepository.comparison_ref()`, including candidate-set hash and proposed-content hash.

## Objective bridge

The nominal objective vector comes from the existing O100D `VariantEvaluationBundle`, not from a forged `CadObjectiveEvaluation`.

Each selected objective requires:

- available comparison value;
- exact ObjectiveDefinition;
- exact source authority;
- declared evaluator/model/fidelity signature.

Perturbed objective results carry per-objective `ExactAuthorityRef` evidence.

A perturbed result must preserve the nominal objective schema and source signature. Authority IDs/hashes may differ because each perturbation produces a different result, but authority kind/version, evaluator identity, model identity and fidelity must remain compatible.

## Reused O90 semantics

This slice reuses existing:

- `UncertaintyAxis`;
- `build_local_stencil()`;
- `apply_local_perturbation()`;
- G10 constraint evaluation;
- O80 orientation constraint evaluation;
- `build_robustness_evaluations()`;
- sampled-worst direction semantics;
- local sensitivity semantics.

The proposal path does not create a second worst-case or Pareto implementation.

## ProposalPerturbationSample

Proposal samples preserve:

- exact spec id/hash;
- deterministic local sample id/index/delta;
- perturbed scene content hash;
- G10 results;
- O80 rejection IDs;
- uncertainty-domain rejection IDs;
- exact objective contract hash;
- exact per-objective result authority refs;
- objective vector when available;
- explicit failure reason otherwise.

An infeasible sample remains unscored evidence.

## Persistence

`CadProposalRobustnessRepository` uses separate append-only native-CAD tables so existing O90 tables and identities do not change.

Save/reopen re-resolves:

- baseline SceneRevision;
- template and candidate SystemVariants;
- O100B search/candidate/candidate-set/proposed-content lineage;
- nominal VariantEvaluationBundle;
- every non-nominal exact result authority;
- deterministic local stencil membership.

Stored `RobustnessEvaluation` results are regenerated from persisted samples and must match exactly.

## Focused verification

`backend/tests/test_cad_proposal_robustness.py` covers:

1. un-applied proposed speaker materialization without baseline mutation;
2. speaker XYZ, aim-yaw and body-yaw through existing O90 perturbation semantics;
3. proposal placement hard-constraint violations retained as infeasible/unscored evidence;
4. existing direction-aware sampled-worst RobustnessEvaluation reuse;
5. invalid template-as-candidate binding rejection;
6. append-only save/reopen with exact O100B/SystemVariant/bundle re-resolution;
7. stale perturbed result authority fails closed.

## Deferred

This is the O90A/local foundation only.

Still required before Issue #229 completion:

- O90B multidimensional bounded proposal sampling;
- explicit probability/empirical/discrete proposal uncertainty sampling where supported;
- robust Pareto binding across multiple proposal candidates;
- O100F canonical roadmap completion;
- production recommendation evidence gate.

RDC usage: 0.
