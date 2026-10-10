# #954 htdt domain split — Phase 3 (calibration package)

Third bounded slice of the #807 flat-namespace decomposition, applying
the Phase-1/2 conventions exactly: one more domain moved into a layered
sub-package, every old import path preserved, and the managed-debt
inventory regenerated honestly.

## Domain chosen: `calibration`

Same scoring as Phases 1–2 — measured dependency shape on the
post-Phase-2 graph (fan-in/fan-out edges, mega-SCC membership,
intra-package direction violations under
`ui -> services -> persistence -> domain`):

| domain | modules | fan-in (srcs) | fan-out (tgts) | in mega-SCC | intra-package violations | verdict |
|---|---|---|---|---|---|---|
| **calibration** | 14 | 83 (56) | 56 (22) | 13 | 2 | **chosen** |
| acoustics | ~76 | ~310 (245) | ~315 (72) | ~49 | ~20 | defer — most entangled |
| optimization | ~40 | 62 (36) | ~350 (92) | 18 | 4 | defer — giant fan-out |
| field-return/mission | ~4 | n/a | n/a | ~5 | n/a | defer — too thin to exercise the stack |

`calibration` is the smallest remaining domain the Phase-2 table named
("calibration or acoustics per the measured table"). Its one weakness is
the highest inbound coupling — 56 distinct flat source modules import
the 14 `*calibrat*` stems — but inbound edges are exactly what the
`sys.modules` alias shims neutralize: every external importer keeps
resolving `htdt.<stem>` to the same module object, so fan-in carries no
mechanical risk. On the axes that do drive move risk — member count,
mega-SCC members, internal direction cleanliness — calibration is the
lowest-risk candidate, matching Phase 2's "lowest-risk /
highest-independence" rule. Acoustics stays deferred: it shrinks the
blob the most but carries ~49 SCC members and ~20 internal direction
violations under honest layering. A dedicated field-return/mission
slice (`field_return_ingestion`, `mission_reconciliation`, ingress-adjacent
staging) has ~4 members — too thin to exercise a real
services/persistence boundary — and remains its own later slice.

## Boundary shape

```
htdt.calibration
├── domain/           6 modules — sealed authorities: calibration plans,
│                                 deployment verification, instrument
│                                 lifecycle, external-artifact import,
│                                 mic-response and model calibration
├── services/         2 modules — wizard state machine, workflow UX
│                                 backend
├── persistence/      6 modules — append-only repositories
└── ui/               — none —
```

Documented import direction: **ui → services → persistence → domain** —
identical to `htdt.measurement` and `htdt.capture`; enforced by the same
`package_import_direction` rule.

Calibration is the first slice with **no ui layer**: nothing named
`*calibrat*` imports PySide6. Its user-facing surfaces live in the
shared application pages (`application_pages`, `room_workspace`,
decision/commissioning panels) — dedicated calibration widgets do not
exist, so the package honestly ships services as its highest layer
rather than pulling a shared page across the boundary to fill the slot.
The Phase-1 "all four layers" criterion was needed once, to prove the
boundary shape; by Phase 3 the shape is pinned and an absent layer is
recorded, not faked. Membership follows the same role-not-name rule as
Phase 2: the two orchestration modules (`cad_calibration_wizard`,
`cad_calibration_workflow`) are services even though they carry no Qt,
and every `*_repository` is persistence.

## Conventions reused from Phases 1–2 (unchanged)

- `sys.modules` alias shims at every flat `htdt.<stem>` path — reads and
  writes land on the canonical module object; no `__getattr__`
  forwarding.
- Moved modules use package-relative imports only: `.` siblings,
  `..<layer>.` cross-layer, `...<flat>.` for still-flat dependencies,
  and `...<pkg>.<layer>.` canonical paths for already-moved
  dependencies (measurement) — never a sibling through a flat shim.
- `PACKAGE_LAYERS` / `PACKAGE_DIRECTION` in
  `scripts/package_boundary_audit.py` declare the package.

## Measured state after the split

- 1200 module nodes, 5702 resolved edges (14 shims add one edge each).
- 40 inventoried violations (was 31 committed): 18
  `lower_layer_imports_ui` + 22 `package_import_direction`. The +9 are
  all pre-existing edges re-expressed by the move, individually named in
  the regenerated inventory:
  - `calibration.domain.cad_calibration -> cad_repository`,
    `cad_calibration_deployment -> cad_authority_resolver`,
    `cad_calibration_lifecycle -> cad_authority_resolver` — the same
    domain → flat-persistence shape Phase 1 recorded for
    `measurement.domain -> cad_repository`: sealed authorities import
    `AuthorityRef`/`SceneRevision` reference types.
  - `calibration.domain.cad_calibration ->
    measurement.{services,persistence}.*` — `CadEffectiveMeasurementResolver`
    plus two TYPE_CHECKING repository annotations. Cross-package edges
    score on the same rank axis; recorded, not hidden through the flat
    shim (which would classify the target as flat-domain and pass).
  - `calibration.persistence.cad_calibration_repository ->
    measurement.services.cad_measurement_effective`,
    `cad_calibration_wizard_repository -> cad_calibration_wizard`,
    `cad_calibration_workflow_repository -> cad_calibration_workflow`
    — repositories importing the record types they store; the same
    persistence → services shape as the inventoried
    `cad_measurement_repository -> cad_measurement_loop` entry.
- 4 cycles: the mega-SCC grew 591 → 603 (the calibration impl nodes now
  sit inside it beside their shims — moving a cyclic edge does not
  break it), plus the 3 known small cycles. `--diff` reports this as
  `grown_cycles` debt, not a new cycle.
- 11 oversized modules, all exempted (calibration adds none — largest
  moved module is `cad_calibration_wizard` at 2416 lines).
- `--diff` against the regenerated inventory is clean:
  `40 inventoried violations, 0 resolved, 0 grown cycles`.

## Adjacent flat modules deliberately not moved

`cad_interface_loopback`, `cad_sweep_acquisition`, and
`cad_sweep_acquisition_evidence` sit on the calibration boundary (the
wizard drives them) but are shared sweep-engine infrastructure consumed
by measurement lanes — a future sweep/acquisition slice should take
them, not this package. `cad_device_adapter` and
`cad_delegated_provider` are likewise cross-domain seams. The 56
external importer modules stay flat and untouched — that is the point
of the shims.

## Tests updated

`backend/tests/test_issue_954_domain_split.py` now covers every declared
package (82 moved stems × parametrized import forms):

- flat path ≡ canonical module object for all moved stems
- attribute writes through the flat path (measurement + capture +
  calibration probes)
- `PACKAGE_LAYERS` exhaustiveness per package directory — calibration's
  missing `ui/` directory is allowed: the registry declares only the
  three real layers
- no moved module reaches a sibling through a flat shim (source scan)
- shims stay pure aliases
- identity probes: `build_timing_reference`, `build_capture_task_plan`,
  and `build_response_calibration_profile` produce identical sealed
  results via old and new paths — the split is import plumbing only
- audit layer classification, inventory-subset check, baseline
  round-trip + diff classifier
- domain-layer shims (`htdt.cad_measurements`, `htdt.capture_bundle`,
  `htdt.cad_calibration_lifecycle`) stay PySide6-free on import

## Deferred (later #807 slices)

- remove shims once external importers migrate to `htdt.<pkg>.*`
- migrate the next domain — acoustics (shrinks the blob the most but
  needs its ~20 internal direction violations inventoried or
  re-seamed first) or optimization (large fan-out)
- resolve the 40 inventory entries — the calibration entries need the
  same seams as their phase-1/2 counterparts (AuthorityRef/SceneRevision
  ownership, repository → record-type edges)
- a "field-return/mission" slice could absorb `field_return_ingestion`,
  `mission_reconciliation`, and ingress-adjacent staging; a
  "sweep/acquisition" slice could absorb `cad_interface_loopback` /
  `cad_sweep_acquisition*`
- require `--diff` in CI so the inventory can only shrink
