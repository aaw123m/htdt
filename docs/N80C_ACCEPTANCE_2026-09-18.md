# N80c / O50 Windows acceptance — 2026-09-18

Tracking: Issue #65 / PR #74  
Product head: `2a891dbc1796d3cfdaebbe762d0d6e0d2636563f`  
Gate head: `44628a1e51c199c10b883ed8accba452578bb1eb`  
GitHub Actions: CI #397 / run `35294094501` — PASS

## Scope

This gate accepts the native N80c Pareto comparison and O50 measurement-loop interaction path added by PR #74. It does not claim O70 automatic recommendation validation.

The gate uses the production native `OptimizationWorkspaceWindow`, immutable Scene/SearchSpec/objective/measurement repositories, and real Qt mouse/button interaction helpers. The temporary acceptance database and fixture measurements are synthetic evidence for UI/authority verification only; they are not model-validation evidence.

## GitHub Actions preconditions

CI #397 passed before the owned-Windows gate:

- all backend tests;
- backend/native launcher CLI checks;
- Python compilation of the N80c Windows acceptance harness;
- PowerShell syntax validation for all runner scripts;
- N60/N70/N80/O20/N80c hardware-runner preflights;
- frontend build and built-frontend smoke test.

## Owned-Windows environment captured by the gate

```text
N80C_ENV_APPLIED_DPI=192
N80C_ENV_PYTHON=3.12.10
N80C_ENV_PYSIDE6=6.11.2
N80C_ENV_PYVISTA=0.49.0
N80C_ENV_VTK=9.7.0
```

The accepted session ran on the existing owned Windows device used for prior native acceptance.

## Accepted interaction evidence

```text
N80C_PRODUCT_COMPOSITION True
N80C_SEARCHSPEC_CREATED True
A14_N80_FAST_GENERATION_OBSERVED True
N80C_CANDIDATES_GENERATED True
N80C_PARETO_VISIBLE True
N80C_PARETO_PROVENANCE_VISIBLE True
N80C_PARETO_SEMANTIC_DEDUP True 1 1
N80C_CANDIDATE_SELECTED True
N80C_CANDIDATE_APPLIED True
N80C_CANDIDATE_SAVED_REVISION True
O50_MEASUREMENT_PLAN_CREATED True
O50_EXACT_REVISION_MEASUREMENT_VISIBLE True
O50_PLANNED_TO_MEASURED_APPEND_ONLY True
N80C_STALE_PARETO_REJECTED True
N80C_O50_WINDOWS_RESULT PASS
```

This proves the accepted native path:

1. create an immutable SearchSpec and deterministic candidate set;
2. display independent objective values and Pareto state with evidence provenance;
3. re-running the same Pareto calculation reuses the same semantic snapshot instead of creating duplicate immutable rows;
4. select/apply a candidate and save a new SceneRevision;
5. bind that candidate to the exact saved applied SceneRevision as an immutable O50 Measurement Plan;
6. expose only N60 `measured` evidence with the exact applied revision/content hash;
7. append a new `measured` Measurement Plan state while retaining the original `planned` row;
8. reject Pareto refresh after the selected SearchSpec becomes stale.

## Repository restoration

The hardware runner refused dirty input, detached to the gate head, then restored the pre-existing local checkout:

```text
N80C_ORIGINAL_SHA=5ede848e8e0b0967a50c04c83ff679a649ca439b
N80C_PRE_STATUS_COUNT=0
N80C_GATE_SHA=44628a1e51c199c10b883ed8accba452578bb1eb
N80C_GATE_EXIT=0
N80C_RESTORED_SHA=5ede848e8e0b0967a50c04c83ff679a649ca439b
N80C_POST_STATUS_COUNT=0
N80C_RESTORE_OK=True
N80C_HARDWARE_GATE_RESULT=PASS
```

No acceptance files were left in the repository worktree.

## Boundary retained for O60/O70

PR #74 also establishes O60 residual-validation and cross-evidence persistence authority, but this acceptance does **not** mark a prediction model as independently validated. A residual threshold pass alone leaves automatic recommendation disabled. O60 trend/rank, sensitivity and repeatability validation continues in Issue #75; O70 remains gated until that evidence exists.

## Execution note

An initial RDC wrapper command was rejected before the gate runner started because the outer PowerShell expanded variables intended for the inner command. It did not fetch/checkout the gate branch, run the GUI harness, or change repository state. The corrected consolidated gate then completed successfully and restored the original clean checkout.
