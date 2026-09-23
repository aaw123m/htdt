# N80c — native Pareto comparison / measurement loop plan

Tracking: Issue #65

## Scope

This slice connects the already-merged O30 objective-vector and O40 Pareto authorities to the native optimization workspace. It does not create a scalar quality score, recommendation, or browser duplicate.

## Authority

- exact native SceneRevision + content hash
- immutable native SearchSpec + candidate-set hash
- immutable CadObjectiveEvaluation with explicit evidence class
- immutable CadParetoSet recomputed and verified by repository
- candidate preview remains non-authoritative; explicit apply remains one command / one Undo
- measured and predicted evidence remain distinct

## UI

The native Optimization workspace will add a comparison surface that:

1. loads immutable evaluations for the selected SearchSpec;
2. exposes objective IDs as independent columns;
3. allows an explicit objective subset for Pareto recomputation;
4. marks non-dominated vs dominated candidates without assigning an overall score or rank;
5. selects an existing candidate by candidate ID for the established preview/apply path;
6. shows evidence class/source provenance and source revision binding;
7. refuses stale SearchSpec/evaluation/Pareto data against the current document.

## Measurement loop

A measured result may be attached to a candidate only through an immutable evidence reference bound to the exact applied SceneRevision. Predicted evidence is never overwritten or reclassified as measured. Comparison can show both evidence classes side-by-side.

Automated sweep execution is not assumed. Existing N60 REW import/read authority remains the measurement ingestion path unless a separately validated measurement-control contract is introduced.

## Verification

Use GitHub Actions for repository tests/build/preflights. Add focused tests only where the new composition adds meaningful authority invariants. Reserve owned-Windows RDC for one consolidated final interaction gate if native UI behavior cannot be established by CI.
