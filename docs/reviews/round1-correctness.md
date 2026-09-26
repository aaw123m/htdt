# Round 1 Review — Correctness & Error Handling

Branch: `devin/rev1-correctness`
Scope: `backend/src/htdt` — undo/redo command pattern, save/load (SQLite `closing()` idiom, CAS save transaction), mesh import/repair pipeline, PFFDTD adapter math, spatial IR metrics, freshest diffs (~20 commits / ~15.6k changed lines), `except`-site inventory (254 `except: pass`/`except: return sentinel` sites triaged).

## Findings

| # | Severity | Location | Description | Status |
|---|----------|----------|-------------|--------|
| 1 | High | `backend/src/htdt/mesh_import_authority.py` `mesh_import_axis_matrix` | Handedness inversion: `right = up × forward` yields a reflection (det = −1) for every `'right'`-handed source — a source whose convention is identical to HTDT's (Z-up, Y-forward) imported *mirrored across X* instead of as identity; the `'left'` branch was correspondingly swapped. Operator-declared axes therefore mirrored all imported geometry. | **Fixed** (`right = forward × up`, left flips) + regression test `test_axis_matrix_preserves_handedness` |
| 2 | Medium | `backend/src/htdt/cad_document.py` `EntitySetEditCommand.revert` | Undo of an atomic mixed edit re-appended removed entities at the tail instead of restoring their original indices. Entity order (covered by `scene_content_hash`) diverged after apply→undo, so `is_dirty` stayed true on a fully reverted document and list/render order silently changed. Unlike `DeleteEntitiesCommand`, which records `(index, entity)` pairs for exact restore. | **Fixed** — `apply_entity_set_edit` records `removed_indices`; `revert` re-inserts at recorded positions, deduped, fail-closed if positions were not recorded. Regression test `test_entity_set_edit_undo_restores_entity_order` |
| 3 | Low | `backend/src/htdt/automatic_backup.py` `_database_fingerprint` | `database.open('rb').read(28)` leaked the file handle until GC each time the backup fingerprint was computed. | **Fixed** — `with database.open('rb')` (cosmetic; behavior unchanged) |

## Deferred / observations (no fix)

| Severity | Location | Observation | Deferred reason |
|----------|----------|-------------|-----------------|
| Low | `cad_joint_execution.py` ~L790 | `except ValueError: front = None` swallows *any* `ValueError` from `pareto_front`, not just "no evaluations"; a genuine integrity error would be masked as "no front". | Needs the `pareto_front` error contract (which failures mean "empty" vs. corruption) — typed-exception decision. |
| Info | 254 `except`-site inventory | `except: pass` / `except: return sentinel` sites are concentrated in UI fail-soft paths (overlay removal, label building, diagnostics flush) and typed-failure sentinels (e.g. `ParsedSpectralDoc('invalid', …)`, `CommandAvailability.blocked(…)`). | No data-loss path found; intentional design. |
| Info | All `sqlite3.connect` sites | Every repository access goes through `closing(self._connect())`; save uses `BEGIN IMMEDIATE` compare-and-swap in one transaction. | Clean. |
| Info | Solver/metric boundaries | PFFDTD pressure derivation (2nd-order one-sided endpoints + centered interior) validates shape/finiteness/`dt>0`/`rho>0`; IACC/lateral metrics guard zero-energy denominators and window length; decision grids validate `step>0`, `max≥min` via pydantic before reaching `_numeric_grid`. | Clean. |

## Fixed files

- `backend/src/htdt/cad_document.py` — `EntitySetEditCommand` records `removed_indices`; `revert` restores exact order.
- `backend/src/htdt/mesh_import_authority.py` — `mesh_import_axis_matrix` handedness corrected.
- `backend/src/htdt/automatic_backup.py` — closed the fingerprint file handle.
- `backend/tests/test_cad_document.py` — `test_entity_set_edit_undo_restores_entity_order`.
- `backend/tests/test_mesh_import_authority.py` — `test_axis_matrix_preserves_handedness` (+ corrected comment in the existing axis test).

## Tests

- `pytest backend/tests/test_cad_document.py` — 20 passed.
- `pytest backend/tests/test_mesh_import_authority.py` — 13 passed (incl. new test).
- `pytest backend/tests -k "backup or document or mesh_import" -n 4` — 235 passed, 0 failures.
- Broader touched suites (`test_cad_room_tools`, `test_authoring_constraint_authority`, `test_cad_room_authoring`) — 72 passed.
