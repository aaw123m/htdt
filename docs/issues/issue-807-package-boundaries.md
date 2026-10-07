# #807 domain package boundaries — audit + enforcement

Issue #807 is a planning issue ("Implementation changes: none"). The
implementable slice landed here: the dependency inventory, the automated
cycle check, the testable layer separation, and the size budget the issue
asks for — all without moving a single module (moves stay out of scope
until the boundary spec below is reviewed).

## Measured state (scripts/package_boundary_audit.py)

- **955 modules** in `backend/src/htdt`, ~687k lines.
- Layer counts (AST classification): 652 domain, 216 persistence, 77 ui,
  5 application, 5 kernel (`cad_schema_ddl`, `canonical_json` are true
  zero-import leaves; `cad_schema`/`cad_repository`/
  `cad_authority_resolver` are persistence-adjacent infrastructure).
- **One giant SCC (~470 modules) spanning the cad_\*/authority
  namespace** — the flat package is effectively a single strongly
  connected component. This is the quantitative version of the issue's
  "flat namespace" risk: domain, persistence, resolver and integration
  modules are mutually referential today, so per-domain sub-packages are
  the only real boundary mechanism (an intra-package SCC is expected;
  a cross-package SCC is a violation).
- Three known small cycles recorded as debt:
  `dirty_state_dialog ↔ workflow_shell`,
  `project_bundle ↔ project_library_repository`,
  `cad_project_template ↔ project_setup_intent`.
- Qt discipline is already good: only `cad_input.py` (a QWidget canvas
  with a domain-style name — rename under the decomposition plan) leaks
  PySide6 into a cad_* module.

## Declared layer rules (enforced by test_issue_807_boundaries.py)

| Layer | Members | May import | Must not import |
|---|---|---|---|
| kernel | `cad_schema_ddl`, `canonical_json` | kernel | anything else |
| persistence | `*_repository`, `cad_schema`, `cad_repository`, `cad_authority_resolver`, `native_*` integrity/audit/schema/backup | kernel + domain | Qt, ui, application |
| domain | everything else without Qt | kernel + persistence + domain | Qt, ui, application |
| ui | Qt-importing panels/pages/workspaces | all | — |
| application | composition roots (`native_cad`, `workflow_application`, …) | all | — |

Enforced invariants:

- `no_qt_in_lower_layers` — the "domain models must not depend on Qt
  widgets" rule, mechanically.
- `persistence_must_not_import_ui` — "persistence adapters must not own
  product semantics" backstop.
- `kernel_imports_only_kernel` — keeps the leaves leaf.
- `no_new_cross_layer_cycles` — new cycles touching ui/application
  fail; the three known cycles are pinned verbatim (a grown member set
  is a new violation).
- `size_budget_lines = 5000` — modules above budget need a
  `SIZE_EXEMPTIONS` decomposition note; six giants are grandfathered
  with named plans (`native_row_integrity`, `cad_schema_ddl`,
  `native_authority_audit`, `cad_geometric_acoustics_adapter`,
  `room_workspace`, `measurement_page_workspace`).
- Exemptions are self-cleaning: an unused exemption fails the test.

## Incremental migration plan (matches "without changing authority semantics")

1. Introduce `htdt.authority` sub-package by moving `cad_*` modules in
   leaf-first order — a moved module's dependents update via a thin
   re-export shim; remove the shim in the same change where cheap.
   Authority hashes never move (sealed records hash field contents, not
   module paths — verified against `identity_payload()`).
2. `htdt.persistence` gets `*_repository` + `native_*` + `cad_schema*`;
   `cad_schema` may keep importing domain during transition
   (persistence→domain edge is already the enforced direction).
3. `htdt.ui` gets the 77 Qt modules; `htdt.application` the 6
   composition roots; `htdt.scene` / `htdt.acoustics` / others emerge as
   authority sub-packages once `htdt.authority` drains.
4. Saved-project/backup regression gate for every move PR: open a
   v-current `cad-scenes.sqlite3`, replay probes green, sealed digests
   unchanged — the existing audit suite is the check.

## Remaining (manual / follow-up)

- The domain *split* itself (which modules belong to `htdt.acoustics`
  vs `htdt.measurement` vs …) needs maintainer review — the audit's
  `layers` + SCC data is the input.
- `cad_input.py` rename to a `*_canvas` name.
- Saved-project/backward-compat regression criteria per moved package
  are declared above; running them is per-PR work.

Refs #807 — issue stays open until the actual restructure proceeds.
