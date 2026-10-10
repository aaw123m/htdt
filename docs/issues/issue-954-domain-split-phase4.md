# #954 htdt domain split — Phase 4 (acoustics package)

Fourth bounded slice of the #807 flat-namespace decomposition, applying
the Phase-1–3 conventions exactly: the acoustics domain — the largest
single domain and the one Phases 2 and 3 explicitly deferred — moves
into a layered sub-package, every old import path preserved, and the
managed-debt inventory regenerated honestly.

## Domain chosen: `acoustics`

Phase 3 deferred acoustics as "most entangled — ~49 mega-SCC members
and ~20 internal direction violations". Re-measured on the post-Phase-3
graph, it remains the biggest shrink on the blob (49 SCC members of 84
stems, ~87 distinct flat importers absorbed by shims) and the only
domain large enough to matter for the mega-SCC's next slice. The
internal-direction story turned out better than the phase-3 estimate:
with **role-honest layering** — solver adapters, the wave-execution
engine, and the UI-facing `solver_output_ledger` in `services` rather
than forced into `domain` by name — the package lands with 49 inventoried
`package_import_direction` edges (14 intra-package + 35 domain →
flat-persistence), the same managed-debt shape every prior phase
recorded. No `lower_layer_imports_ui`, no Qt in lower layers, no new
cycles: acoustics is feasible as-is, honest about its seams, and the
next largest slice is now optimization.

## Boundary shape

```
htdt.acoustics
├── domain/           58 modules — sealed authorities and pure math:
│                                 room modes/reflections, materials,
│                                 treatments, geometry derivations,
│                                 bakeoff/benchmark/validation specs,
│                                 hybrid & wave-solver authorities,
│                                 scattering/diffuseness/modal models,
│                                 solver capability/confidence manifests,
│                                 pffdtd boundary contracts
├── services/         12 modules — solver adapters (pffdtd, geometric,
│                                 hybrid), candidate wave execution,
│                                 stochastic ray receiver, object
│                                 promotion, treatment service,
│                                 solver output ledger
├── persistence/      12 modules — append-only repositories
│                                 (*_repository)
└── ui/               2 modules — room_acoustics_panel,
                                  solver_output_diagnostics_ui
```

Documented import direction: **ui → services → persistence → domain** —
identical to `htdt.measurement`, `htdt.capture`, and `htdt.calibration`;
enforced by the same `package_import_direction` rule.

Membership follows the role-not-name rule of every prior phase: the
solver adapters (`acoustic_pffdtd_*`, `cad_acoustic_solver_adapter`,
`cad_geometric_acoustics_adapter`), the engines
(`cad_candidate_wave_execution`, `cad_stochastic_ray_receiver`), and the
UI-facing ledger (`solver_output_ledger`) are services even though they
carry no Qt; every `*_repository` is persistence; the two Qt importers
are the only ui modules.

### The `acoustics.py` name collision

The flat `htdt.acoustics` module (rect-room modes/reflection math)
shares its name with the package directory — the package shadows the
module. `htdt/acoustics/__init__.py` therefore re-exports the module's
public surface (`ACOUSTICS_ALGORITHM_VERSION`, `RoomMode`,
`ReflectionCandidate`, `rectangular_room_modes`,
`first_order_reflections`, `analyze_rectangular_context`) so both
`import htdt.acoustics` and `from htdt.acoustics import X` keep
resolving; the module itself lives at
`htdt.acoustics.domain.acoustics`. `htdt.acoustics` is the only moved
stem with no flat `.py` shim — its package `__init__` plays that role
(and the audit classifies it as one).

## Conventions reused from Phases 1–3 (unchanged)

- `sys.modules` alias shims at every flat `htdt.<stem>` path — reads and
  writes land on the canonical module object; no `__getattr__`
  forwarding.
- Moved modules use package-relative imports only: `.` siblings,
  `..<layer>.` cross-layer, `...<flat>.` for still-flat dependencies,
  and `...<pkg>.<layer>.` canonical paths for already-moved
  dependencies — never a sibling through a flat shim.
- `PACKAGE_LAYERS` / `PACKAGE_DIRECTION` in
  `scripts/package_boundary_audit.py` declare the package.

## Measured state after the split

- 1305 module nodes, 5862 resolved edges.
- 57 inventoried `package_import_direction` violations (was 8 committed,
  all calibration): +49 acoustics entries, individually named in the
  regenerated inventory:
  - `acoustics.domain.* -> {cad_schema, cad_repository,
    cad_authority_resolver, cad_equipment_repository,
    cad_r110_source_repository, cad_system_variant_repository}` (35
    edges) — the same domain → flat-persistence shape Phases 1–3
    recorded (`measurement.domain -> cad_repository`,
    `calibration.domain -> cad_authority_resolver`): sealed authorities
    import `AuthorityRef`/`SceneRevision`-family reference types and
    schema helpers.
  - intra-package upward edges (14): `domain -> services`
    (`cad_acoustic_solver_result -> cad_acoustic_solver_adapter`,
    `cad_hybrid_stitching -> {cad_hybrid_numerical_composition,
    cad_candidate_wave_execution}`, `cad_geometric_acoustics_response /
    cad_hybrid_acoustic_result / cad_hybrid_late_energy ->
    cad_geometric_acoustics_adapter`,
    `cad_solver_capability_manifest -> cad_acoustic_solver_adapter`,
    `{acoustic_pffdtd_polyhedral_geometry,
    cad_pffdtd_resource_estimator} -> cad_candidate_wave_execution`),
    `domain -> persistence` (`cad_acoustic_treatment_comparison ->
    {cad_acoustic_treatment_repository,
    cad_acoustic_snapshot_repository}`), and `persistence -> services`
    (`cad_acoustic_{snapshot,solver_dispatch}_repository ->
    cad_acoustic_solver_adapter`) — repositories importing the record
    types they store and specs referencing their executors; the same
    honest shape as the inventoried
    `calibration.persistence -> calibration.services` entries.
- 1 cycle: the mega-SCC grew 604 → 632 (the 49 acoustics impl nodes now
  sit inside it beside their shims — moving a cyclic edge does not
  break it). Recorded as grown-cycle debt in the regenerated inventory,
  not a new cycle.
- 10 oversized modules, all exempted
  (`acoustics.services.cad_geometric_acoustics_adapter` at 8222 lines
  keeps its stem-keyed exemption through the move).
- `--diff` against the regenerated inventory is clean:
  `57 inventoried violations, 0 resolved, 0 grown cycles`.

## Adjacent flat modules deliberately not moved

Solver-adjacent modules that are shared infrastructure stayed flat:
`cad_schema` / `cad_repository` / `cad_authority_resolver` (the
persistence kernel every domain leans on), `cad_equipment*` /
`cad_r110_source*` / `cad_system_variant*` (cross-domain
hardware/variant authorities), `r120_geometry_compiler*` (geometry
pipeline, its own domain), `canonical_json` / `clock` (kernel). The ~87
external importer modules stay flat and untouched — that is the point
of the shims.

## Tests updated

`backend/tests/test_issue_954_domain_split.py` now covers the fourth
declared package (166 moved stems × parametrized import forms):

- flat path ≡ canonical module object for all moved stems
- attribute writes through the flat path (measurement + capture +
  calibration + acoustics probes)
- `htdt.acoustics` re-export probe: the package `__init__` returns the
  old flat module's public API from `acoustics.domain.acoustics`
- `PACKAGE_LAYERS` exhaustiveness per package directory — acoustics
  declares all four layers
- no moved module reaches a sibling through a flat shim (source scan)
- shims stay pure aliases
- identity probes: `build_timing_reference`, `build_capture_task_plan`,
  `build_response_calibration_profile`, and now
  `build_threshold_policy` produce identical sealed results via old and
  new paths — the split is import plumbing only
- audit layer classification, inventory-subset check, baseline
  round-trip + diff classifier
- domain-layer shims (`htdt.cad_measurements`, `htdt.capture_bundle`,
  `htdt.cad_calibration_lifecycle`, `htdt.acoustic_benchmark`,
  `htdt.cad_acoustic_snapshot`) stay PySide6-free on import

## Deferred (later #807 slices)

- remove shims once external importers migrate to `htdt.<pkg>.*`
- migrate the next domain — optimization is now the largest remaining
  candidate (large fan-out to re-seam or inventory first)
- resolve the 57 inventory entries — the acoustics domain →
  flat-persistence edges need the same seams as their phase-1–3
  counterparts (AuthorityRef/SceneRevision ownership, repository →
  record-type edges); the intra-package upward edges want executor
  interfaces extracted down into domain
- a "field-return/mission" slice could absorb `field_return_ingestion`,
  `mission_reconciliation`, and ingress-adjacent staging; a
  "sweep/acquisition" slice could absorb `cad_interface_loopback` /
  `cad_sweep_acquisition*`
- require `--diff` in CI so the inventory can only shrink
