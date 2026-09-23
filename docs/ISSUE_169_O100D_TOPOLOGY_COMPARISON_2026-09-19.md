# Issue #169 — O100D Named System Topology Comparison Authority

Date: 2026-09-19

## Scope

This slice adds the solver/evaluator-neutral comparison layer that binds already-computed
SystemVariant evidence into one named topology comparison. It does **not** recompute
coverage, SPL, headroom, standards, FR, reflections, or amplifier capability.

The implemented authority chain is:

```
exact baseline SceneRevision
+ exact candidate SystemVariant authorities
+ exact existing evaluation/evidence references
+ exact ObjectiveDefinition authorities
-> SystemTopologyComparisonSpec
-> VariantEvaluationBundle per SystemVariant
-> comparison eligibility
-> independent ObjectiveVector set
-> existing Pareto authority
-> optional selected-only TopologyComparisonSelection
```

No weighted score is introduced.

## New modules

- `backend/src/htdt/cad_topology_comparison.py`
- `backend/src/htdt/cad_topology_comparison_repository.py`

Focused fixture:

- `backend/tests/test_cad_topology_comparison.py`

## Authority model

### SystemTopologyComparisonSpec

`SystemTopologyComparisonSpec` is immutable/versioned and carries:

- deterministic comparison id + semantic SHA-256
- user-facing comparison name
- exact baseline `SceneRevision` id/hash
- exact candidate `SystemVariant` id/hash entries
- explicit candidate role: `current`, `proposed`, or `as_built`
- explicit comparison label
- optional exact layout/profile ref
- exact required and optional `ObjectiveDefinition` authorities
- exact `StandardsProfile` id/version/hash
- explicit compatibility and missing/unsupported policies

Topology names such as `3.0.2 current` and `5.0.2 proposed A` are stored as
comparison labels. They are not inferred from speaker counts. If a future exact layout
authority exists it can be attached separately via `layout_profile_ref`.

`as_built` is accepted only with an explicit exact as-built authority reference.

### VariantEvaluationBundle

Each bundle binds one exact comparison candidate to an already-computed
`ObjectiveVector` and exact evidence refs.

First-class slots exist for:

- `CoverageEvaluation`
- `DirectLevelEvaluation`
- future amplifier/headroom evaluation
- `StandardsEvaluation`
- FR prediction refs
- reflection prediction refs
- installation/evidence refs

The amplifier slot is only an extension point in this slice. No amplifier authority or
electrical headroom calculation is implemented here.

Every metric in the bundled `ObjectiveVector` must have an
`ObjectiveEvidenceBinding` that resolves to a non-Standards evidence authority.
Standards evidence therefore cannot silently become an objective score.

### Required vs optional objectives

Required objectives fail comparison eligibility when:

- absent from the vector
- explicitly `missing`
- explicitly `unsupported`
- objective id/quantity/unit/direction/domain differs
- comparison model id/version differs
- declared evidence source model differs
- declared evaluator/model/fidelity authority is incompatible across otherwise
  eligible candidates

No zero, infinity, sentinel numeric value, or other favorable substitute is generated.

Optional objectives may be absent or unsupported without making the candidate
ineligible. An optional objective enters Pareto only if it is available and exactly
compatible for **every comparison-eligible candidate**. This preserves the existing
Pareto requirement that all compared candidates share one exact objective authority.

### Standards

`StandardsEvaluation` remains independent criterion-level evidence. PASS/FAIL/UNKNOWN
is not transformed into an objective score and is not an implicit topology filter.

A fixture explicitly gives the current variant a failing Standards criterion while it
remains comparison-eligible. A hard Standards constraint still belongs to the existing
explicit constraint adapter, not this comparison layer.

### Pareto

Only comparison-eligible bundles are reduced to the required/common-optional objective
axes and forwarded to the existing `pareto_front` implementation.

Existing direction semantics are preserved, including mixed maximize/minimize
objectives. No hidden normalization or weighted aggregate is added.

### Selection lifecycle boundary

`TopologyComparisonSelection` is an optional immutable decision record with fixed
semantics:

- `selection_state = selected_only`
- `applied = false`
- `installed = false`
- `measured = false`

Selection accepts only a comparison-eligible candidate, but it does not call
`SystemVariant` application APIs and cannot create a `SceneRevision`.

## Persistence

`CadTopologyComparisonRepository` adds append-only tables for:

- specs
- variant bundles
- comparison evaluations
- selections

On save and reopen it re-resolves:

- exact baseline SceneRevision
- exact SystemVariant id/hash
- exact StandardsProfile
- exact StandardsEvaluation
- Coverage/DirectLevel authority where their repositories are supplied
- other extension authorities through explicit exact resolvers

Persisted JSON alone is never sufficient when the referenced authority can be
re-resolved. A hash/version mismatch fails closed.

The repository exposes no apply/deploy/install/measure operation.

## Focused fixture coverage

The focused test includes:

1. a current 3.0.2-equivalent SystemVariant over FL/C/FR + two height speakers
2. proposed 5.0.2-equivalent variants that add SL/SR as proposed entities
3. coverage maximize objective
4. SPL optional maximize objective
5. headroom maximize objective
6. off-axis loss minimize objective
7. criterion-level StandardsEvaluation evidence
8. required objective missing
9. required objective unsupported
10. optional objective missing without candidate failure
11. incompatible unit
12. incompatible comparison model version
13. comparison-ineligible candidates excluded from Pareto
14. mixed maximize/minimize Pareto comparison
15. a Standards FAIL that does not remove the candidate
16. selected-only selection that creates no SceneRevision
17. append-only save/reopen with exact authority re-resolution
18. reopen rejection after an external evaluation hash is changed

## Explicit non-goals

This slice does not implement or modify:

- topology candidate generation
- coverage evaluation
- direct SPL/headroom evaluation
- amplifier/output-device authority
- FR/reflection prediction
- Standards scoring
- weighted objective scoring
- UI
- SceneRevision apply
- installed/measured lifecycle promotion
- O90
- common roadmap/status documents

## Validation

Focused implementation tests are intentionally limited to the new comparison authority.
The pull request CI is the repository-level validation authority. The final workflow
result is recorded on the PR; successful jobs are not re-run without a new change or
failure requiring it.
