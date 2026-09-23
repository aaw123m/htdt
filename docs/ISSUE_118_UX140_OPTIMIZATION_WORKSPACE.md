# Issue #118 / UX140 — Optimization workspace integration

## Scope

UX140 replaces the visible one-scroll optimization composition with four task pages:

1. **探索設定** — O10 SearchSpec and advanced O80 extended-search setup
2. **候補** — O10/O80 candidate generation, paging, 3D preview, explicit apply
3. **比較** — O30 objective vectors and Pareto comparison
4. **測定・検証** — O50 measurement plans, O60 campaign/validation, O70/O80A next-measurement planning

The implementation is in `backend/src/htdt/optimization_workflow_workspace.py`.
It deliberately does **not** add optimization domain semantics. The accepted
`OptimizationWorkspaceWindow` and its focused controllers remain the execution
adapter; UX140 only recomposes their controls into page-oriented presentation.

## Shell mount contract

The shell integration point is:

```python
from htdt.optimization_workflow_workspace import build_optimization_workspace_mount

mount = build_optimization_workspace_mount(repository, document_id)
```

The returned `WorkspaceMount` uses the existing UX110 contract:

- `on_activate`: refresh a clean stale legacy document, then refresh O-series views
- `before_deactivate`: reuse the fail-closed legacy editor guard for draft / preview /
  recovery / background-worker state
- `on_context_changed`: route the shell context directly to
  `OptimizationWorkflowWorkspace.select_section()`

No change is required in `workflow_shell.py` itself. UX140 also does not modify
`native_cad.py`; launcher composition can swap the optimization factory to this
builder in the later integration pass without changing the workspace contract.

Canonical optimization context IDs are now:

| Context | Japanese label | Main authority |
|---|---|---|
| `setup` | 探索設定 | O10 SearchSpec; advanced O80 capability / Extended SearchSpec |
| `candidates` | 候補 | O10 candidate set; advanced O80 extended candidates |
| `comparison` | 比較 | O30 objective evaluations + existing Pareto set builder/repository |
| `validation` | 測定・検証 | O50 measurement plan, O60 validation, O70 adaptive, O80A adaptive extended |

The new workspace accepts the previous `objectives` → `comparison` and
`measurement-plan` → `validation` section names as compatibility aliases.
They are not additional canonical navigation contexts.

## O-series authority map

### O10 — search space / feasible candidates

Reused without semantic changes:

- `SearchControllerMixin`
- `cad_search.build_cad_search_spec()`
- `cad_search.generate_cad_candidates()`
- `cad_search.search_spec_current_working()`
- `cad_search.apply_candidate_positions()`
- `CadSearchRepository`

SearchSpec still binds exact SceneRevision/content and constraint workspace hashes.
A stale SearchSpec is not made current by the UI.

### O30 — objective vectors / Pareto

Reused without introducing a total score:

- `CadObjectiveRepository`
- `build_pareto_set()`
- `OptimizationWorkspaceWindow.refresh_pareto_comparison()`

The comparison page preserves independent objective IDs and units. It does not add
an aggregate sound-quality score, ranking authority, or recommendation winner.
The existing consistency checks reject candidate comparisons with mismatched
objective sets or units.

### O50 — measurement plan

Reused:

- `MeasurementPlanControllerMixin`
- `cad_measurement_loop.build_measurement_plan()`
- `cad_measurement_loop.complete_measurement_plan()`
- `CadMeasurementRepository`

The page only exposes the existing exact candidate / SceneRevision binding and
measured-evidence matching rules.

### O60 — preregistered campaign / model validation

Reused:

- `ValidationControllerMixin`
- `CadValidationCampaignRepository`
- `CadValidationCampaignService`
- `CadModelValidationRepository`
- `CadModelValidationService`

Calibration/holdout assignment, preregistration, evidence readiness, applicability,
and `recommendation_gate` remain authority-owned. UX140 does not recompute or
reinterpret these gates.

### O70 — adaptive measurement planning

Reused:

- `AdaptiveControllerMixin`
- `CadAdaptivePlanRepository`
- `CadAdaptivePlannerService`

The existing `development_synthetic` and `production_owned_room` scopes remain
explicit in the UI. Production still requires the accepted current O60 authority.

### O80 / O80A — extended search / adaptive extended

Reused:

- `ExtendedSearchControllerMixin`
- `CadExtendedSearchRepository`
- existing model capability and extended-candidate builders
- `AdaptiveExtendedControllerMixin`
- `CadAdaptiveExtendedRepository`
- `CadAdaptiveExtendedPlannerService`

Acoustic aim and physical body yaw stay capability-gated. The UI does not promote
REW Room Simulator to a directional model and does not convert synthetic capability
or evidence into owned-room authority.

## Evidence boundary

The UX140 composition must preserve the existing fail-closed boundary:

- Synthetic development data may exercise O70/O80/O80A software paths.
- Synthetic data does not satisfy owned-room O60 evidence.
- Synthetic validation does not unlock `production_owned_room` recommendation.
- Production capability/recommendation remains tied to the exact current
  campaign-backed eligible O60 record and its persisted hashes/provenance.
- User-facing pages may summarize the state, but they do not replace repository or
  service validation.

## UI composition boundary

`optimization_workspace.py` is intentionally not enlarged. Its existing focused
controllers continue to own operations and mutable UI state. The UX140 module:

- hides the inherited permanent dock/toolbar composition,
- moves the existing controller-created controls into task-specific pages,
- keeps the 3D viewport in the Candidates page,
- puts O80/O80A controls behind progressive disclosure,
- makes Pareto comparison the primary content of Compare,
- combines measurement planning and validation into one workflow page.

This is a migration adapter rather than a second optimization implementation.
When the old launcher is retired, the controller-owned controls can be constructed
directly by page components without changing O-series repositories/services.

## Verification

Focused contracts:

- canonical four-page navigation and Japanese labels,
- compatibility mapping for old optimization deep-link section names,
- `optimization.compare_candidates` routing to Compare,
- shell `WorkspaceMount` interface.

Repository CI remains the regression authority for O10–O80 behavior and packaged
native application behavior. No RDC is required or used for UX140 implementation.


## Integration update — 2026-09-19

The UX120–UX140 integration composition now mounts this workspace for `WorkspaceId.OPTIMIZATION`. Canonical contexts are defined once in `workflow_navigation.py`; legacy `objectives` and `measurement-plan` links are normalized at the shell boundary. `optimization.compare_candidates` now deep-links to `comparison`. This remains a transitional controller/state adapter; final removal of the inherited legacy QMainWindow structure is a later cleanup.
