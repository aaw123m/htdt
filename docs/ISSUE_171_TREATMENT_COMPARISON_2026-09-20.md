# Issue #171 — named treatment design comparison authority

Date: 2026-09-20

## Scope

This slice closes the comparison gap between the existing treatment authorities.

The established chain is now:

```text
SceneRevision
+ optional exact SystemVariant
+ exact AcousticTreatmentPlacement set
+ optional exact treatment-bearing AcousticSceneSnapshot
+ optional exact AcousticPredictionRequest set
→ TreatmentDesignComparisonCandidate
→ TreatmentDesignComparisonSpec
```

The comparison authority does not recompute acoustic objectives and does not invent a treatment score.

## Candidate semantics

A candidate is one named design state and records:

- exact baseline SceneRevision id/hash;
- optional exact SystemVariant id/hash;
- exact treatment placement id/version/hash;
- exact definition id/version/hash and lifecycle for every placement;
- optional exact AcousticSceneSnapshot id/hash and snapshot schema;
- optional exact AcousticPredictionRequest id/hash/input hash.

The two candidate roles are:

- `no_treatment`: the exact baseline/variant has an empty treatment placement set;
- `treatment`: one or more exact treatment placements are required.

No synthetic zero-absorption treatment object is created for the no-treatment case.

## Prediction trace

When an AcousticSceneSnapshot is attached, its treatment overlay lineage is reduced only to the exact treatment-placement hashes already carried by the underlying `TreatmentBoundaryOverlay` authorities.

The candidate requires that the snapshot placement-hash set exactly equals the candidate placement-hash set.

Attached prediction requests must bind the exact same snapshot id/hash.

This gives an exact trace:

```text
named treatment candidate
→ exact placement(s)
→ TreatmentBoundaryOverlay / composition
→ AcousticSceneSnapshot
→ AcousticPredictionRequest
```

without flattening base construction and treatment or fabricating a numerical before/after result.

## Persistence / reopen

`CadAcousticTreatmentComparisonRepository` stores the comparison append-only in the native CAD database.

Save/reopen re-resolves:

- baseline SceneRevision;
- every SystemVariant;
- every AcousticTreatmentPlacement;
- when present, the exact AcousticSceneSnapshot through `CadAcousticSnapshotRepository`;
- when present, every exact AcousticPredictionRequest.

A prediction-traceable comparison requires a typed `CadAcousticSnapshotRepository`; an opaque snapshot hash alone is not sufficient.

## Relation to O100 topology comparison

This authority does not replace `SystemTopologyComparisonSpec`.

- O100 topology comparison owns numerical objective/Pareto comparison over SystemVariants.
- TreatmentDesignComparisonSpec owns the exact design lineage for treatment/no-treatment alternatives.

A future evaluated treatment comparison may attach its prediction-derived objectives to the existing O100 comparison/evidence machinery rather than introducing a second scoring system.

## Focused verification

`backend/tests/test_cad_acoustic_treatment_comparison.py` covers:

1. deterministic named no-treatment / A / B comparison;
2. exact SystemVariant and placement identity;
3. append-only save/reopen with typed re-resolution;
4. wrong-variant placement rejection;
5. no-treatment candidate cannot hide a treatment placement.

Prediction/snapshot exact treatment lineage is already exercised by the merged treatment snapshot fixtures from PR #219; this slice consumes that identity rather than duplicating solver tests.

## Deferred

- numerical before/after acoustics;
- treatment optimization;
- automatic model fitting;
- weighted treatment score;
- cut-list rendering in InstallationOutput;
- measured validation execution;
- R130/R150 solver execution.

RDC usage: 0.
