# Issue #866 — Geometry intake readiness (P1)

REV66 slice. Implements the sealed intake chain

```
SOURCE_GEOMETRY -> HEALTH_CHECK -> REPAIR_PROPOSAL ->
EXPLICIT_ACCEPTANCE -> DERIVED_GEOMETRY_REVISION -> SOLVER_READINESS
```

over imported scene geometry, with provenance at every link and no
silent mutation of the source model. All records are content-addressed
sealed models persisted through `_SealedStore` tables.

## Scope

| Stage | Record | id | Table |
|-------|--------|----|-------|
| Source geometry | `GeometryIntakeSubject` | `gis-` | (in-memory; sealed but not stored — it is derived, not authored) |
| Health check | `GeometryIntakeReport` | `gdr-` | `cad_geometry_intake_reports` |
| Repair proposal | `GeometryRepairProposal` | `grp-` | `cad_geometry_repair_proposals` |
| Explicit acceptance | `GeometryRepairAcceptance` | `gra-` | `cad_geometry_repair_acceptances` |
| Derived revision | `DerivedGeometryRevision` | `gdv-` | `cad_derived_geometry_revisions` |
| Solver readiness | `GeometrySolverReadinessVerdict` | `srv-` | `cad_geometry_solver_readiness` |

`GeometryIntakeSubject` is deliberately *not* persisted: it is a pure,
deterministic projection of `SceneDocument` (+ optional IFC/blob
refs). Its `subject_sha256` is pinned inside the report, so a stored
report always names the exact geometry it diagnosed. Re-import or
scene revision changes the subject hash and therefore retires every
downstream record deterministically (see Invalidation).

## Modules

- `backend/src/htdt/cad_geometry_intake.py` — subject builder,
  diagnostics evaluator, repair proposal/acceptance, derived
  revision, readiness evaluator, invalidation + execution gate, JA
  label registry.
- `backend/src/htdt/cad_geometry_intake_repository.py` —
  `CadGeometryIntakeRepository` over five `_SealedStore` tables.
- `backend/src/htdt/geometry_intake_panel.py` — `GeometryIntakePanel`
  (Qt widget): defect list split by origin, locate affordance,
  repair accept/reject with operator parameters, readiness panel.
- `backend/tests/test_issue_866_geometry_intake.py` — 47 tests.
- Schema: `NATIVE_SCHEMA_VERSION = 97` (`_migrate_96_to_97`, DDL +
  `NATIVE_SCHEMA_TABLES`, `_ROW_BINDINGS`, `_ReplayProbe` +
  `_RepositoryChain` 'geometry_intake' in `native_authority_audit`).

## Defect vocabulary

`GeometryDefect.kind`:

| kind | severity | origin | repairable |
|------|----------|--------|-----------|
| `open_boundary_edges` | critical | source_model | automatic (weld/consolidate) |
| `non_manifold_edges` | critical | source_model | operator_required |
| `disconnected_regions` | warning | source_model | automatic (fragment removal) |
| `duplicate_or_overlapping_faces` | warning | source_model | automatic |
| `inverted_normals` | warning | source_model | automatic (winding) |
| `degenerate_or_tiny_features` | warning | source_model | automatic (area tolerance) |
| `portal_opening_ambiguity` | warning | source_model | operator_required (resolve_portal) |
| `material_assignment_gap` | warning | source_model | operator_required (assign_material) |
| `coordinate_unit_anomaly` | warning/info | source_model | operator_required (declare_units) |
| `not_watertight` | warning | source_model | operator_required |
| `solver_unsupported_condition` | critical | solver_limitation | none |
| `geometry_unavailable` | critical | source_model | none |
| `diagnostic_evidence_gap` | warning | source_model | none |

Each defect carries `severity`, `origin` (`source_model` vs
`solver_limitation`), `part_refs`/`entity_refs`/`opening_refs`, an
`evidence` dict, `consequence`, `suggested_action`, and `repairable`.

## Repair model

- `propose_geometry_repairs` never mutates; it only emits proposed
  `GeometryRepairAction`s, one per repairable defect. Automatic kinds
  map onto the bounded raw-mesh repair operations
  (`ExactDuplicateVertexConsolidation`, `ToleranceVertexWeld`,
  `RemoveUnreferencedVertices`, `RemoveExactDuplicateFaces`,
  `CorrectConsistentWinding`, `RemoveDegenerateFaces`).
  `remove_disconnected_fragment` re-imports the part minus the named
  component (honest `replaced_mesh` state, not a bounded-repair
  lineage, since component deletion is outside the bounded op set).
- `record_geometry_repair_acceptance` requires an exact cover: every
  proposed action decided exactly once; accepted operator-required
  actions must supply their parameters (`material_label`,
  `source_unit`+`scale_to_meters`, `resolution`, `component_index`).
- `derive_geometry_revision` applies *accepted* actions to a new
  immutable `DerivedGeometryRevision`: `parts` carry
  `unchanged_source` / `repaired_mesh` (+ `repair_lineage`) /
  `replaced_mesh`; openings and unit authority update at subject
  level. Residual defects are re-diagnosed on the derived geometry —
  a repair is never assumed to have fixed anything.

## Solver readiness

`evaluate_solver_readiness` takes exactly one geometry authority
(source subject or derived revision), the latest intake report, the
selected `AcousticSolverAdapterDescriptor`, and optionally the
`SolverCapabilityManifest`.

`SolverGeometryContext` (derived from descriptor + manifest):
`wave` requires watertight volume, material assignments, resolved
portals, consistent winding; `geometric` requires materials and
resolved portals; a manifest `portal_region_coupling=UNSUPPORTED` row
flips open portals into a `solver_limitation` blocking reason.

Verdict vocabulary (`SolverReadinessState`):

- `supported` — no unresolved defects.
- `degraded` — only warning-level effects remain.
- `unsupported` — at least one blocking reason (critical defect,
  required-but-missing contract, solver-unsupported condition).
- `unknown` — evidence gaps (undeclared units, undetermined
  diagnostics, unavailable geometry evidence) — fail closed.

`ReadinessReason` distinguishes `origin='source_model'` defects from
`origin='solver_limitation'` conditions, and names the manifest
`phenomenon` where relevant.

## Invalidation

`readiness_evidence_state(verdict, current_geometry_sha256,
current_adapter_sha256, current_manifest_sha256)` returns:

- `current` — everything the verdict pins still matches.
- `stale_geometry` — re-import or revision changed the geometry hash.
- `stale_solver` — a different adapter descriptor is selected.
- `stale_manifest` — the capability manifest changed.

`geometry_solver_execution_gate` denies `unsupported`, `unknown`, and
stale-geometry verdicts; `assert_geometry_solver_execution_permitted`
raises `GeometrySolverExecutionBlockedError` (carrying verdict id +
reason texts) for the same cases. `degraded` is allowed and surfaces
its reasons for the operator record.

## UI integration

`GeometryIntakePanel` (offscreen-testable):

- defect tables split into `source_model` and `solver_limitation`
  groups (different remediation paths — never merged visually);
- *locate* button emits `locateRequested(tuple[entity_ids])` using
  `defect_locate_targets` — the room-workspace controller maps entity
  ids to the 3D selection/highlight set (same idiom as constraint
  result selection);
- repair table carries per-action accept/reject buttons plus inline
  parameter editors (material label, unit+scale, portal resolution,
  component index) whose values populate `accepted_parameters`;
- readiness panel shows the verdict label and reason lines
  (`[阻止]`/`[証跡不足]`/`[制限あり]`).

Persistence of decisions, derived revisions, and verdicts is the
owning controller's job — the panel only emits signals.

## What remains device-only

- VTK highlight rendering of located entities inside the 3D workspace
  (the signal contract is headless-tested; pixel-level verification
  needs a real GPU surface — Mesa/VTK on this box can crash).
- IFC/GLB source-asset ingestion producing real `source_refs` —
  fixtures construct subjects directly or from `SceneDocument`.
- Wiring the panel into the Room workspace shell (controller owns the
  repository + decision persistence; this slice ships the panel and
  the authority chain it drives).
