# #954 htdt domain split — Phase 5 (optimization package)

Fifth bounded slice of the #807 flat-namespace decomposition, applying
the Phase-1–4 conventions exactly: the optimization domain — the slice
Phase 4 named "the next largest slice" — moves into a layered
sub-package, every old import path preserved, and the managed-debt
inventory regenerated honestly.

## Domain chosen: `optimization`

Phase 4 deferred optimization as "large fan-out to re-seam or inventory
first". Re-measured on the post-Phase-4 graph it proved smaller than
feared: the 40-member core carries only 17 modules inside the mega-SCC
(the rest of the cluster is acyclic), and its fan-out is absorbed by
the flat shims exactly like measurement (56 importers) and calibration
(56 importers) before it. With **role-honest layering** — the
Qt-free UI-facing context/presenter/overlay modules in `services`, the
three sqlite-owning robust-* stores in `persistence` beside the
`*_repository` modules, and the Qt widget `optimization_search_domain`
in `ui` despite its name — the package lands with 20 inventoried
`package_import_direction` edges, the same managed-debt shape every
prior phase recorded. No `lower_layer_imports_ui`, no Qt in lower
layers, no new cycles: optimization is feasible as-is.

## Boundary shape

```
htdt.optimization
├── domain/           14 modules — sealed authorities and pure math:
│                                 objective-vector models, pareto
│                                 fronts, placement constraints,
│                                 joint/multi-sub optimization specs,
│                                 robustness specs and sampling/
│                                 uncertainty engines, the O90E
│                                 validation authority, the optimizer-
│                                 qualification authority, and the
│                                 numbered optimization-journey model
├── services/         4 modules — Qt-free UI-facing orchestration:
│                                 joint-optimization binding context,
│                                 robustness-spec authoring context,
│                                 robustness overlay + presenter models
├── persistence/      10 modules — append-only repositories and
│                                 sqlite-owning stores (objective,
│                                 joint, multi-sub, optimizer-
│                                 qualification, robust-design,
│                                 robustness, O90E validation, robust
│                                 pareto ×2, proposal robustness)
└── ui/               12 modules — joint/multi-sub panels, workflow
                                  workspace + stage controllers
                                  (search/validation/robustness/
                                  adaptive/extended), search-domain
                                  authoring widget, robustness
                                  authoring panel
```

Documented import direction: **ui -> services -> persistence -> domain**
— identical to `htdt.measurement`, `htdt.capture`, `htdt.calibration`,
and `htdt.acoustics`; enforced by the same
`package_import_direction` rule.

Membership follows the role-not-name rule of every prior phase:
`optimization_search_domain` is a Qt authoring widget (ui), the
Qt-free `optimization_robustness_overlay`/`_presenter` and the two
`*_context` modules are services even though they carry no Qt,
`cad_{robust,proposal_robust}_pareto` and `cad_proposal_robustness`
own sqlite tables and are persistence beside the `*_repository`
modules, and `cad_optimizer_qualification` — the authority for the
*optimizer's* claim quality — is optimization, not model validation.

## Conventions reused from Phases 1–4 (unchanged)

- `sys.modules` alias shims at every flat `htdt.<stem>` path — reads and
  writes land on the canonical module object; no `__getattr__`
  forwarding.
- Moved modules use package-relative imports only: `.` siblings,
  `..<layer>.` cross-layer, `...<flat>.` for still-flat dependencies,
  and `...<pkg>.<layer>.` canonical paths for already-moved
  dependencies — never a sibling through a flat shim.
- `PACKAGE_LAYERS` / `PACKAGE_DIRECTION` in
  `scripts/package_boundary_audit.py` declare the package.
- No name collision this phase: there is no flat `optimization.py`, so
  `htdt/optimization/__init__.py` needs no public-surface re-export
  (unlike `htdt.acoustics`).

## Measured state after the split

- 1351 module nodes (was 1306), 5907 resolved edges (was 5867).
- 69 inventoried `package_import_direction` violations (was 49):
  +20 optimization entries, individually named in the regenerated
  inventory:
  - `optimization.domain.* -> {cad_repository, cad_roomsim_repository,
    cad_authority_resolver}` (9 edges) — the same domain →
    flat-persistence shape Phases 1–4 recorded: sealed authorities
    importing `SceneRevision`/`AuthorityRef` reference types and schema
    helpers.
  - cross-package upward edges (4): `domain -> services` —
    `cad_objective_authority -> acoustics.services.
    cad_hybrid_prediction_provider[_integration]`,
    `optimization_robustness_validation -> measurement.services.
    cad_measurement_loop` — and `persistence -> services` —
    `cad_robustness_validation_repository -> measurement.services.
    cad_measurement_loop`; authorities referencing provider/loop
    services they replay against, the same honest shape as the
    inventoried `calibration.persistence -> calibration.services`
    entries.
  - `optimization.ui.* -> native_worker` (7 edges): the stage
    controllers and panels submit candidate generation to the flat
    worker pool — a pre-existing ui -> application edge now visible at
    package granularity.
- 1 cycle: the mega-SCC grew 633 → 644 (17 optimization impl nodes now
  sit inside it beside their shims — moving a cyclic edge does not
  break it). Recorded as grown-cycle debt in the regenerated inventory,
  not a new cycle.
- 10 oversized modules, all exempted (no optimization member is over
  budget).
- `--diff` against the regenerated inventory is clean:
  `69 inventoried violations, 0 resolved, 0 grown cycles`.

## Adjacent flat modules deliberately not moved

- `cad_search*` / `cad_extended_search*` / `cad_topology_search*` /
  `search_space` / `cad_joint_execution` / `cad_field_metric_repository`
  — the search-spec/candidate authorities the optimizer binds. They are
  shared substrate, consumed by measurement
  (`cad_measurement_loop`, `cad_measurement_repository`), acoustics
  prediction providers, the validation campaign, `cad_roomsim*`, the
  adaptive lanes, and composition roots — the same reason Phase 4 kept
  `cad_equipment*` / `cad_system_variant*` flat.
- `cad_adaptive*` — the adaptive-planning lane shares authorities with
  measurement (`cad_adaptive_measurement_design` already lives in
  `measurement.domain`); a candidate for a future adaptive slice.
- seating/speaker authorities (`cad_seating`, `cad_seat_priority`,
  `cad_multi_seat_analysis*`, `cad_speaker_*`, `cad_loudspeaker_*`,
  `cad_substitution_impact*`, `cad_listener_pose`, `seat_priority_panel`,
  `room_seat_coverage_panel`) — room-model and hardware-library
  authorities *consumed by* optimization, not optimization machinery
  (`cad_seating_acoustics` already lives in `acoustics` by the same
  reasoning).
- The O60/O60E model-validation campaign cluster (`cad_validation_*`,
  `cad_model_validation*`, `cad_*_qualification` other than
  optimizer-qualification, `cad_*_campaign*`, `cad_evidence_invalidation*`)
  — a distinct validation domain; now the largest remaining candidate
  for a later slice.
- The decision/study cluster (`cad_intervention_study*`,
  `intervention_planner*`, `cad_decision_*`, `cad_analysis_study*`,
  `cad_assumption_decision*`, `cad_design_decision*`,
  `decision_brief_panel`) — a separate decision/study domain.
- `optimization_measurement_controller` already lives in
  `measurement.ui` (Phase 1): it drives measurement-plan capture for
  the optimization workspace's measure step and stays there.
- `native_worker`, `cad_schema`/`cad_repository`/`cad_authority_resolver`
  (persistence kernel), `canonical_json`/`clock` (kernel) — shared
  infrastructure every domain leans on.

## Tests updated

`backend/tests/test_issue_954_domain_split.py` now covers the fifth
declared package (206 moved stems × parametrized import forms):

- flat path ≡ canonical module object for all moved stems
- attribute writes through the flat path (measurement + capture +
  calibration + acoustics + optimization probes)
- `PACKAGE_LAYERS` exhaustiveness per package directory — optimization
  declares all four layers
- no moved module reaches a sibling through a flat shim (source scan)
- shims stay pure aliases
- identity probes: `build_timing_reference`, `build_capture_task_plan`,
  `build_response_calibration_profile`, `build_threshold_policy`, and
  now `build_optimization_problem` produce identical sealed results via
  old and new paths — the split is import plumbing only
- audit layer classification, inventory-subset check, baseline
  round-trip + diff classifier
- domain-layer shims (`htdt.cad_measurements`, `htdt.capture_bundle`,
  `htdt.cad_calibration_lifecycle`, `htdt.acoustic_benchmark`,
  `htdt.cad_acoustic_snapshot`, `htdt.pareto`,
  `htdt.cad_optimizer_qualification`) stay PySide6-free on import

## Deferred (later #807 slices)

- remove shims once external importers migrate to `htdt.<pkg>.*`
- migrate the next domain — the O60/O60E model-validation campaign
  cluster is now the largest remaining candidate; the decision/study
  cluster and the adaptive-planning lane are smaller follow-ons
- resolve the 69 inventory entries — the optimization additions want
  the same seams as prior phases (AuthorityRef/SceneRevision
  ownership); the `ui -> native_worker` edges want an
  application-facing worker-submission seam; the cross-package
  `domain/persistence -> services` edges want provider/loop interfaces
  extracted down into domain layers — **the 20 optimization entries
  were re-seamed in Phase 6** (`issue-954-domain-split-phase6.md`);
  the earlier packages' entries were resolved by the intervening
  seam PRs (#123/#124/#128)
- require `--diff` in CI so the inventory can only shrink
