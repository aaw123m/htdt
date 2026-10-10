# Issue #955 — typed error boundaries for the launch/viewport/adapter lanes

Status: implemented (this PR)
Refs: #955, seeded by the Phase B exit audit of #815; follow-up to
`issue-955-error-boundaries.md` (measurement/optimization UI tranche) and
`issue-955-capture-boundaries.md` (capture/persistence tranche)

## Scope

The launch/viewport/adapter lane held 103 broad `except` sites across 6
modules (`native_cad*.py`, `native_diagnostics*.py`, `room_viewport.py`,
`room_workspace.py`, `*_adapter*.py` under `backend/src/htdt/`). This pass
converts every one of them to a typed boundary under the #815 taxonomy —
nothing left for a later slice in this lane.

## Boundary contract

Same contract as the earlier tranches (`htdt/error_boundary.py`):

- `except EXPECTED_OPERATION_ERRORS as exc:` at degraded-read and
  operation surfaces.
- `is_authority_failure(exc): raise` — sealed-store failures re-raise
  rather than degrade into a falsified default.
- `report_boundary_failure(exc, operation='<JA 操作名>')` on
  user-visible degraded surfaces.
- Unexpected exceptions (`TypeError`, `AttributeError`, ...) propagate.
- Legitimately broad boundaries keep `except Exception`/`BaseException`
  with a justification comment on the except line **and surface the
  failure identity** (log); bare `pass` is never allowed.

### Architecture notes specific to this lane

- `native_diagnostics.py` is the pre-Qt bootstrap/reporting sink — it
  *is* the bottom error layer and cannot import `error_boundary` (the
  chain pulls `ui_theme` → PySide6 before Qt is guaranteed usable). Its
  converted sites narrow to `except ImportError` (optional-Qt probes) or
  stay broad with `_LOGGER`/`write_stderr` identity.
- `native_cad.py` keeps headless entry points Qt-free via lazy
  `__getattr__` exports — every `error_boundary` import added there is
  function-local, never module-top.
- `cad_avr_lan_adapter.py` is Qt-free (transport `Protocol` only) — its
  catches narrowed to the transport tuple `(OSError, EOFError,
  RuntimeError)` rather than `EXPECTED_OPERATION_ERRORS`, keeping the
  module free of the UI-side boundary dependency.
- `cad_adapter_conformance.py` catches stay broad by design: the raised
  exception object *is* the scored observation (`ok = isinstance(exc,
  AdapterCapabilityError)` etc.) — narrowing would break the scoring.

## Sites converted (103 across 6 files)

| File | Sites | Treatment |
|---|---|---|
| native_cad.py | 38 | 32 kept broad with justification (launch/journal/splash bookkeeping, recovery-dialog/background-lane starts, failure-recording write-throughs, maintenance boundary); 6 narrowed — `_restorable_backups`, post-update audit probe + revalidation op, `_route_launch_intent` project switch + restore preview, `_run_gui` preview_restore |
| room_viewport.py | 27 | 25 narrowed — pick/camera/probe/marquee helpers degrade honestly via `report_boundary_failure` or `_LOGGER.debug` (hot per-item paths); 2 kept broad (scalar-bar teardown, event-filter removal) |
| native_diagnostics.py | 18 | 5 narrowed to `except ImportError` (Qt probe blocks); 13 kept broad — stderr/hook/sink plumbing now logs identity or `write_stderr` where logging could recurse |
| cad_adapter_conformance.py | 15 | all kept broad — the exception is the scored observation; markers upgraded from bare `noqa` to `error-boundary:` lanes |
| cad_avr_lan_adapter.py | 3 | all narrowed to `(OSError, EOFError, RuntimeError)` — apply send loop, read-back query, rollback write+verify |
| room_workspace.py | 2 | both narrowed to `EXPECTED_OPERATION_ERRORS` + `is_authority_failure` re-raise — feasibility-preview clearance repo construction + envelope list |

**Totals: 41 narrowed to typed catches, 62 kept broad with justification —
all 103 carry the `error-boundary:` marker.**

## Behavior changes (defect fixes only)

- `room_workspace._installation_feasibility_preview`: a sealed-authority
  failure in the clearance repository or envelope list was swallowed into
  `qualification = None` / `clearances = {}` — falsifying 'no service
  clearances'. Authority failures now propagate; expected failures degrade
  with `report_boundary_failure` (was silent).
- `cad_avr_lan_adapter`: a bug-class error raised inside a transport
  (e.g. `TypeError` from a malformed stub) was laundered into
  `AvrLanApplyError` / `AdapterCapabilityError` / `outcome='failed'` —
  indistinguishable from a real link failure. Bug classes now propagate.
- `native_cad` silent bookkeeping `pass` catches (journal `note_failure`
  + `deactivate_journal` on the schema/migration/generic failure paths,
  the generic `complete_launch` write, the per-mount workspace dirty
  probe, `splash.finish`, `_notify_instance_active`): now log identity
  (`diagnostics.logger.exception`); control flow unchanged.
- `native_diagnostics` silent `pass` catches (stderr write, flush,
  handler teardown, hook restore, qt-message-handler chaining): now log
  identity via `write_stderr`/`_LOGGER`; control flow unchanged.
- `room_viewport` silent `pass` catches (pick-preview actor removal, snap
  label removal, prop traversal, marquee per-actor probes): now log
  identity at DEBUG; degrade semantics unchanged.
- `native_cad._restorable_backups` + restore-preview follow-throughs:
  narrowed so unexpected errors propagate instead of masquerading as
  'no restorable backups' / a warning; expected failures keep the prior
  user-visible copy (JA message via `operation_error_message` /
  `warn_user` unchanged).

## Remaining

None in this lane — the glob scope was fully converted (103 ≤ the ~120
cap). Other lanes retain `structural_followup_remaining` for later
tranches.

Out-of-scope repairs this PR also makes so the landed guards stay green:

- `workflow_application.py:2722` — a broad catch added after tranche 1
  landed inside its guarded module list; the justification comment
  existed but lacked the `error-boundary:` token the tranche-1 AST guard
  requires. Marker added only.
- `test_native_launch.py::_fake_window_with_app` — the SimpleNamespace
  double lacks `enable_first_run_wizard_autoshow`, which `_run_gui`
  calls unconditionally; already failing on main. Double updated only.
- `test_room_viewport_r9.py::test_marquee_selects_entities_intersecting_projected_bounds`
  — surfaced by this tranche: the candidates-read narrowing lets the
  double's missing `plotter.iren` propagate instead of being swallowed;
  the real QtInteractor always has `iren`. Double updated only.

## Tests

`backend/tests/test_issue_955_ui_boundaries.py` adds:

- the AST guard (`test_no_unmarked_broad_catches_in_ui_scope`) over all 6
  converted modules;
- failure-injection coverage for the changed-semantics boundaries: AVR
  apply/read-back/rollback transport failures keep their typed outcomes
  while bug-class failures propagate unwrapped, and the workspace
  clearance read propagates authority failures while degrading expected
  ones to an empty clearance map with a boundary report.
