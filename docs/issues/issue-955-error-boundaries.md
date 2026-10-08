# Issue #955 — typed error boundaries for the measurement/optimization UI layer

Status: implemented (this PR)
Refs: #955, seeded by the Phase B exit audit of #815

## Scope

The measurement and optimization UI layer caught broad `except Exception`
sites that silently swallowed failures — a degraded read could falsify a
default (an empty journey strip that looks like "nothing saved yet", a
provenance probe that drops to `None` with no trace), and recovery paths
could pass without ever recording what failed.

This pass converts every broad catch in the measurement + optimization
UI/workflow/controller modules to a typed boundary, and marks the handful of
boundaries that legitimately stay broad. No success-path behavior changed.

## Boundary contract

All converted sites follow the `#815` taxonomy (`htdt/error_boundary.py`):

- `except EXPECTED_OPERATION_ERRORS as exc:` — the set of expected operation
  failure classes (`ValueError`, `KeyError`, `OSError`, `RuntimeError`,
  `sqlite3.Error`, `pydantic.ValidationError`).
- `is_authority_failure(exc): raise` — sealed-store failures (integrity,
  conflict, schema, corrupt database) are re-raised. A sealed authority that
  cannot be read must fail loudly; degrading it to a falsified default is
  the exact failure mode this issue exists to remove.
- `report_boundary_failure(exc, operation='<JA 操作名>')` — expected failures
  are reported through the existing honest-surface mechanisms
  (`htdt.errors` log, `UserFacingError` surface, status line / error label
  where the site already had one).
- Unexpected exceptions (`TypeError`, `AttributeError`, `IndexError`, ...)
  are deliberately *not* caught — they propagate to the uncaught-diagnostics
  sink instead of being laundered into a benign-looking default.
- Legitimately broad boundaries (Qt slot teardown, stage atomicity,
  per-sample compute evaluation, best-effort volatile journaling) keep
  `except Exception` with a justification comment and surface the exception
  identity (log or sealed `failure_reason`); bare `pass` is never allowed.
- Every broad catch carries `# error-boundary:` **on the `except` line** —
  `test_no_unmarked_broad_catches_in_955_scope` walks the AST of all
  converted modules and fails on any unmarked broad catch.

## Sites converted (86 markers across 20 files)

| File | Sites | Notes |
|---|---|---|
| measurement_workflow.py | 13 | batch import engine-session probe, dataset/report reads, quality-report derivation, stage/parse boundaries; pending-import journal and stage teardowns stay broad with justification |
| optimization_workflow_workspace.py | 9 | `_refresh_journey` head read, spec list/freshness, lineage walk, per-spec plan/campaign/evaluation/pareto/validation counts |
| optimization_validation_controller.py | 9 | campaign save/list, readiness probes, campaign detail, materialize, validation record save, REW read start |
| optimization_extended_controller.py | 8 | capability saves, evidence list, extended axis/spec, candidate apply, overlay removal, preview degrade |
| optimization_search_controller.py | 7 | axis/preset/linked-variable adds, spec save + re-author, candidate apply, overlay removal |
| measurement_record_surfaces.py | 7 | accept boundaries on AV sync, latency, health baseline/plan/run, preset record dialogs |
| joint_optimization_panel.py | 5 | preflight estimate, spec create, staleness probe, spec list, baseline resolve |
| measurement_authority_dialogs.py | 4 | record-dialog accept, dataset probe, excitation asset save, output-device label probe |
| optimization_workflow_controller.py | 4 | pareto build/save, decision-verdicts wrapper, REW save, quality report |
| commissioning_panel.py | 4 | operations derivation, command invocation, authority read, render |
| revalidation_queue_panel.py | 3 | compose surface, compare save, validation record |
| measurement/robustness controllers & panels | 7 | robustness_authoring_panel (2), measurement controller (2), robustness/search-domain/adaptive controllers (3) |
| workflow_shell.py | 1 | closeEvent hook teardown — stays broad (every close hook must run), logs identity |
| optimization_robustness*.py | 4 | per-sample evaluation loops — stay broad with justification; failure identity is sealed into the record's `failure_reason`, never passed silently |

## Remaining sites (follow-up inventory)

A repo-wide AST scan finds **425** further unmarked broad catches across
~110 other `htdt` modules — out of scope for this issue's measurement +
optimization UI layer. Largest concentrations (priority order for follow-up
issues):

- `native_cad.py` (38), `native_diagnostics.py` (18), `room_viewport.py` (18),
  `cad_adapter_conformance.py` (15), `session_recovery.py` (15),
  `application_pages.py` (14), `data_management.py` (13),
  `cad_candidate_wave_execution.py` (12), `authority_revalidation.py` (11),
  `native_backup.py` (10), `presentation_workspace.py` (10)
- Long tail: ~95 modules with 1–9 sites each (device/LAN adapters, capture
  pipeline, repositories, installers, report/export paths).

## Tests

`backend/tests/test_issue_955_error_boundaries.py` — 19 tests:

- **Guard**: `test_no_unmarked_broad_catches_in_955_scope` — every broad
  catch in the 20 converted modules must carry the marker.
- **Measurement workflow**: pending-import journal failure is logged (never
  silent); engine-session probe expected→report+`None`, authority→propagates,
  unexpected→propagates; batch stage teardown unstages entries and re-raises
  on unexpected failure; quality-report derivation expected→warn+`None`,
  unexpected→propagates.
- **Dialogs/panels**: `_RecordDialog.accept` expected→error label (stays
  open) vs unexpected→propagates; output-device label probe
  expected→report+degrade, authority→propagates; revalidation queue compose
  expected→status surface, unexpected→propagates.
- **Optimization controllers**: decision-verdicts wrapper and measurement
  plan save — expected→status/label surface, unexpected→propagates.
- **Optimization journey**: head-read authority→propagates;
  expected→report+degraded guide (strip still renders);
  per-spec count authority→propagates; unexpected→propagates; success-path
  regression (six steps render).
