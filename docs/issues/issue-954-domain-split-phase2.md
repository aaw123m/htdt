# #954 htdt domain split — Phase 2 (capture package)

Second bounded slice of the #807 flat-namespace decomposition, applying
the Phase-1 conventions exactly: one more domain moved into a layered
sub-package, every old import path preserved, and the managed-debt
inventory regenerated honestly.

## Domain chosen: `capture`

Same scoring as Phase 1 — measured dependency shape on the post-Phase-1
graph (fan-in/fan-out edges, mega-SCC membership, intra-package direction
violations under `ui -> services -> persistence -> domain`):

| domain | modules | fan-in (srcs) | fan-out (tgts) | in mega-SCC | intra-package violations | verdict |
|---|---|---|---|---|---|---|
| **capture** | 22 | 25 (12) | 57 (24) | 4 | 0 | **chosen** |
| acoustics | 57 | 88 (37) | 198 (51) | 24 | 6 | defer — most entangled |
| calibration | 19 | 102 (65) | 66 (25) | 15 | 1 | defer — highest inbound coupling |
| optimization | 17 | 42 (31) | 230 (79) | 5 | 0 | defer — no persistence layer |

`capture` is the lowest-risk / highest-independence candidate on every
axis that matters for a mechanical move: the fewest external importers
(12 source modules — the next domain in dependency order, consumed by
the application shell rather than by the domain blob), the fewest
members inside the 580-module mega-SCC, and zero internal violations
under the documented layering. It also already *names itself*: the
HTDT-Capture wire vocabulary is `htdt.capture.*` (`htdt.capture.bundle`,
`htdt.capture.ingestion-plan`, `htdt.capture.inbox-item.v1`, …), so the
Python package boundary matches the schema boundary the code already
documents. All four layers are real.

This branch was rebased onto `main` `a6abf16b` before opening: upstream
landed `capture_watch_guard` (#1019 watch-folder link defense) while
the slice was in flight, and it joins the package here as a domain
module — 23 stems total (the table's counts were measured on the
pre-rebase graph; the measured-state section below is post-rebase).

## Boundary shape

```
htdt.capture
├── domain/          10 modules — wire schemas, validators, plan and
│                                 mission models, pure computation
│                                 (incl. capture_watch_guard, the #1019
│                                 link-defense classifier that landed
│                                 mid-slice)
├── services/         6 modules — import pipeline, receiver, retention,
│                                 promotion executors, authoring surface
├── persistence/      3 modules — inbox staging, ingestion transaction,
│                                 semantic-promotion write paths
└── ui/               4 modules — Qt panels, controller, watch runner
```

Documented import direction: **ui → services → persistence → domain** —
identical to `htdt.measurement`; enforced by the same
`package_import_direction` rule. Membership follows the module's actual
role, not its name: `capture_inbox`/`capture_ingestion_transaction`/
`capture_semantic_promotion` are persistence (they own the staged-write
repositories), which is also the only layering that keeps every real
internal edge direction-legal (e.g. `capture_inbox ->
capture_semantic_promotion`, `capture_entity_promotion -> capture_*`
orchestration edges).

## Conventions reused from Phase 1 (unchanged)

- `sys.modules` alias shims at every flat `htdt.<stem>` path — reads and
  writes land on the canonical module object; no `__getattr__`
  forwarding.
- Moved modules use package-relative imports only: `.` siblings,
  `..<layer>.` cross-layer, `...<flat>.` for still-flat dependencies.
- `PACKAGE_LAYERS` / `PACKAGE_DIRECTION` in
  `scripts/package_boundary_audit.py` declare the package; the
  `is_shim` impl-path construction is now package-generic (it was
  hard-coded to `measurement.` in Phase 1).
- Latent audit fix: `diff_against_baseline` sorted violation dicts
  directly and crashed the first time a diff actually contained new
  violations — Phase 2 is the first slice that exercises it; now sorted
  by `violation_key`.

## Measured state after the split

- 1182 module nodes, 5686 resolved edges.
- 31 inventoried violations (was 27 committed): 18
  `lower_layer_imports_ui` + 13 `package_import_direction`.
  - `capture.domain.capture_watch_failures -> native_diagnostics` is the
    pre-existing edge re-expressed under the stricter package rule —
    the old `lower_layer_imports_ui` entry resolved, the
    `package_import_direction` entry took its place. Net zero.
  - `capture.ui.capture_watch_runner -> native_worker` is newly visible
    debt, not a new edge: a packaged `ui` module may not reach a flat
    `application` module, while the flat rules never flagged
    `ui -> application`. Same shape as the recorded
    `measurement.ui.measurement_page_workspace -> native_worker` entry.
    +1 honest debt; removing it needs the worker-pool seam, which is a
    refactor, not a move — recorded, not hidden.
  - The remaining +3 are upstream-introduced between inventory
    baselines, not from this move: `joint_optimization_context ->
    perf_harness` and `reference_library_browser -> equipment_library`
    / `standards_profile_editor` landed on `main` without an inventory
    regeneration; regenerating the baseline records them honestly
    rather than leaving `--diff` red for the next slice.
- 4 cycles: the mega-SCC grew 580 → 591 (the capture impl nodes now
  sit inside it beside their shims, plus unrelated upstream growth —
  moving a cyclic edge does not break it), plus the 3 known small
  cycles. `--diff` reports this as `grown_cycles` debt, not a new cycle.
- 10 oversized modules, all exempted (capture adds none; upstream
  growth pushed `application_pages` and `room_viewport` over the
  budget — recorded the same way).
- `--diff` against the regenerated inventory is clean:
  `31 inventoried violations, 0 resolved, 0 grown cycles`.

## Adjacent flat modules deliberately not moved

`field_return_ingestion`, `mission_reconciliation`, and `ingress` sit on
the capture boundary but are shared infrastructure:
`ingress` is a generic bounded-read safety helper imported by ~23
modules across every domain; `field_return_ingestion` and
`mission_reconciliation` are the field-return lane consumed by capture
and by the reconciliation surface — a future "field-return/mission"
slice should take them together rather than stretching this package's
prefix-clean membership. `capture_contract` is already its own
subpackage (the vendored wire contract) and is untouched.

## Tests updated

`backend/tests/test_issue_954_domain_split.py` now covers every declared
package (68 moved stems × 3 import forms):

- flat path ≡ canonical module object for all moved stems
- attribute writes through the flat path (measurement + capture probes)
- `PACKAGE_LAYERS` exhaustiveness per package directory
- no moved module reaches a sibling through a flat shim (source scan)
- shims stay pure aliases
- identity probes: `build_timing_reference` sealed record and
  `build_capture_task_plan` `plan_sha256` produce identical results via
  old and new paths — the split is import plumbing only
- audit layer classification, inventory-subset check, baseline
  round-trip + diff classifier
- domain-layer shims (`htdt.cad_measurements`, `htdt.capture_bundle`)
  stay PySide6-free on import

## Deferred (later #807 slices)

- remove shims once external importers migrate to `htdt.<pkg>.*`
- migrate the next domain — calibration or acoustics per the measured
  table (acoustics shrinks the blob the most but carries 24 SCC members
  and 6 internal violations; calibration is smaller with 15/19 in the
  SCC and the highest fan-in)
- resolve the 31 inventory entries — the watch-lane pair
  (`capture_watch_failures -> native_diagnostics`,
  `capture_watch_runner -> native_worker`) needs an application-seam
  refactor, not a move
- a "field-return/mission" slice could absorb `field_return_ingestion`,
  `mission_reconciliation`, and `ingress`-adjacent staging
- require `--diff` in CI so the inventory can only shrink
