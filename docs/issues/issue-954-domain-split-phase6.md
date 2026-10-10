# #954 htdt domain split — Phase 6 (optimization re-seam)

Sixth bounded slice of the #807 flat-namespace decomposition: re-seam
the 20 `package_import_direction` violations Phase 5 recorded as
managed debt when the optimization domain moved into
`htdt.optimization{domain,services,persistence,ui}` (PR #126). Every
edge is resolved by moving code or adding a real seam — no new
`EXEMPTIONS`/`KNOWN_CYCLES` entries and no hand-edited baseline.

## Seam vocabulary applied (from PRs #123/#124/#128)

- `SceneRevision` imports come from `cad_scene_revisions`, never from
  the `cad_repository` facade.
- `AuthorityRef` imports come from `cad_authority_registry`, never
  from `cad_authority_resolver` (persistence).
- Contract/record surfaces move to `<pkg>.domain.*`; services engines
  import them back (services -> domain is legal).
- Cross-layer edges a domain module cannot legally take go behind a
  module-local `Protocol` port declared by the consuming layer.

## The 20 violations and how each resolved

**`optimization.domain.* -> cad_repository` (7 edges).** Every site
imported only `SceneRevision`: repointed to
`cad_scene_revisions` in `cad_joint_optimization`, `cad_objectives`,
`cad_objective_authority`, `optimization_robustness`,
`optimization_robustness_multidimensional`,
`optimization_robustness_uncertainty`, and
`optimization_robustness_validation`.

**`optimization.domain.cad_objective_authority -> acoustics.services.
cad_hybrid_prediction_provider[_integration]` (2 edges) +
`-> cad_roomsim_repository` (1 edge).** The O30 authority replay bound
three upward dependencies:

- The R170B contract surface — `HybridPredictionProviderRef`,
  `hybrid_provider_frequency_response`,
  `HybridPredictionProviderObjectiveInput`,
  `build_hybrid_provider_objective_input`,
  `HYBRID_PROVIDER_OBJECTIVE_INPUT_AUTHORITY_VERSION`, `_validate_band`
  — moved verbatim into the new domain module
  `htdt.acoustics.domain.cad_hybrid_prediction_objective_contracts`,
  registered in `PACKAGE_LAYERS['acoustics']['domain']`. The module is
  deliberately **blob-free**: `valid_frequency_domain` is typed against
  a `HybridProviderFrequencyDomain` `Protocol` instead of importing
  `cad_equipment.FrequencyDomain` (already inside the mega-SCC), and
  `CadObjectiveInputRef` is not imported — so the new module does not
  enlarge the recorded cycle. `as_objective_input_ref`, which produced
  an optimization-domain type from an acoustics contract, moved to
  `acoustics.services.cad_hybrid_prediction_provider_integration` as
  the `hybrid_objective_input_ref(objective_input)` function (3 call
  sites updated — the only callers). Both services modules re-export
  every moved name, so the flat shims and packaged-path imports keep
  resolving to the same objects.
- The provider itself stays at services rank (it is the validated
  evidence record). Domain code binds the `HybridObjectiveProvider`
  `Protocol` — `source_entity_id`, `receiver_id`,
  `valid_frequency_domain`, `absolute_pressure_samples`,
  `require_observable()`, `ref()`, `model_dump()` — mirroring #128's
  `CandidateWaveExecutor` port.
- `scene_repository: SceneRepository` became
  `scene_repository: ObjectiveSceneRepository`, a domain-local
  `Protocol` port (`get(revision_id) -> SceneRevision | None`).
- The lazy `isinstance(repository, CadRoomSimRepository)` batch-memo
  check became a capability flag `roomsim_batch_memo_capable` on
  `ObjectiveAuthorityContext`; the wiring layer
  (`optimization.persistence.cad_objective_repository`, which may
  legally import `CadRoomSimRepository`) computes it once. Duck-typed
  test doubles keep working unchanged — `NamedTuple` field default
  preserves every existing constructor call.

**`optimization.domain.optimization_robustness_validation ->
measurement.services.cad_measurement_loop` (1 edge) and
`optimization.persistence.cad_robustness_validation_repository ->
measurement.services.cad_measurement_loop` (1 edge).** Both imported
only `CadMeasurementPlan`, a sealed record — repointed to
`measurement.domain.cad_measurement_plan`.

**`optimization.domain.cad_optimizer_qualification ->
cad_authority_resolver` (1 edge).** `AuthorityRef` repointed to
`cad_authority_registry`.

**`optimization.ui.* -> native_worker` (7 edges).** The controllers
and panels imported `WORKER_CANCELLED`/`NativeWorker`/
`NativeWorkerPool`/`WorkerShutdownReport` through
`htdt.native_worker`, which the audit classifies `application` — but
`native_worker` is itself only a `sys.modules` alias: the real module
is `htdt.worker_pool`, classified `ui`. Repointing the imports to
`worker_pool` is the honest seam — packaged layers import the worker
module directly, same convention the shim itself documents.

## Measured state after the re-seam

- `package_import_direction` violations: **20 -> 0**; the regenerated
  inventory's `violations` list is empty.
- 1 cycle: the mega-SCC **shrank 644 -> 641** — removing
  `cad_objective_authority`'s upward edges broke three modules out of
  the component, and the new contracts module was kept blob-free by
  design so it did not add one back.
- `--diff` against the regenerated inventory is clean:
  `0 inventoried violations, 0 resolved, 0 grown cycles`.

## Tests

- `backend/tests/test_issue_954_domain_split.py` and
  `test_issue_807_boundaries.py`: green (the registry-exhaustiveness
  probe now covers `cad_hybrid_prediction_objective_contracts`; the
  flat-shim identity checks pass through the services re-exports).
- Every test file importing a moved module was run (40 files, ~1500
  tests): all green except **one pre-existing failure on main** —
  `test_round11_seams.py::test_excluded_component_labels_cover_registry`
  fails because #992 registered `restore_drill_journal` in
  `persisted_data.py` without adding its `_EXCLUDED_COMPONENT_LABELS`
  entry in `data_management_ui.py`; neither file is touched by this
  slice. `test_review_round20_locale.py` was not run: it has a
  pre-existing collection error on main (`_capability_from_source`
  missing from `htdt.equipment_library`).

## Deferred (later #807 slices)

- remove shims once external importers migrate to `htdt.<pkg>.*`
- migrate the next domain — the O60/O60E model-validation campaign
  cluster remains the largest candidate; the decision/study cluster
  and the adaptive-planning lane are smaller follow-ons
- shrink the recorded mega-SCC (641 members) — the next wins are the
  flat `cad_schema`/`cad_adaptive_extended_repository` ->
  `cad_objective_repository` edges and the
  `cad_seat_priority`/`cad_equipment` upward reaches that keep domain
  modules cycling through the flat persistence facade
- fix `test_excluded_component_labels_cover_registry`'s missing
  `restore_drill_journal` label (pre-existing main failure, #992)
- fix `test_review_round20_locale.py`'s `_capability_from_source`
  collection error (pre-existing main failure)
- require `--diff` in CI so the inventory can only shrink
