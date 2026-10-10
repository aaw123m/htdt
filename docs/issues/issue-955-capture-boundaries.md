# Issue #955 — typed error boundaries for the capture/persistence lanes

Status: implemented (this PR)
Refs: #955, seeded by the Phase B exit audit of #815; follow-up to
`issue-955-error-boundaries.md` (the measurement/optimization UI tranche)

## Scope

The capture/ingestion/watch/persistence + worker lanes held 66 broad
`except` sites across 22 modules (`capture_*.py`, `*_ingestion*.py`,
`*_watch*.py`, `native_worker*.py`, `worker_*.py`, `*_storage*.py`,
`*_repository*.py` under `backend/src/htdt/`). This pass converts every one
of them to a typed boundary under the #815 taxonomy — nothing left for a
later slice in this lane.

## Boundary contract

Same contract as the #955 UI tranche (`htdt/error_boundary.py`):

- `except EXPECTED_OPERATION_ERRORS as exc:` at degraded-read and
  operation surfaces (config probes, pairing loads, retention reads,
  wire-reject paths).
- `is_authority_failure(exc): raise` — sealed-store failures re-raise
  rather than degrade into a falsified default or a wrong wire status.
- `report_boundary_failure(exc, operation='<JA 操作名>')` — expected
  failures report through the honest-surface mechanisms with JA operation
  names where the failure reaches a user-visible surface.
- Unexpected exceptions (`TypeError`, `AttributeError`, ...) propagate to
  the uncaught-diagnostics sink — never laundered into benign defaults.
- Legitimately broad boundaries — transaction atomicity
  (rollback-then-re-raise), cleanup before re-raise, per-item sealing
  (the failure becomes the item's sealed problem record), per-drop watch
  routing, thread-boundary dispatch, interpreter-exit teardown,
  best-effort journaling — keep `except Exception`/`BaseException` with a
  justification comment **and surface the failure identity** (log or
  sealed record); bare `pass` is never allowed.

## Sites converted (66 across 22 files)

| File | Sites | Treatment |
|---|---|---|
| capture_receiver.py | 9 | 3 kept broad (delivery listener, audit staging — now logs identity, was bare `pass`); 6 narrowed: wire-reject paths (bundle read/ingest/inbox staging/field-return) now let authority failures propagate instead of falsifying a 4xx; receipt JSON parse narrowed to `(UnicodeDecodeError, json.JSONDecodeError)` |
| capture_inbox.py | 8 | `reconcile_orphaned_ingestions` per-digest catch narrowed — authority failures propagate instead of reporting a partial recovery as complete; executor promotion + 6 transaction-rollback sites stay broad |
| capture_receiver_settings.py | 6 | all narrowed: offer/confirm/revoke probe failures surface as status text; pairing-list/port/project-ref probes report via `report_boundary_failure` |
| capture_receiver_controller.py | 5 | `_start`/`_stop` narrowed (expected → `last_error`); config + pairing-count probes narrowed with report; shutdown teardown stays broad |
| capture_retention_ui.py | 3 | narrowed: inventory read, delete confirm, purge execute |
| capture_import.py | 2 | CLI inbox staging + rejected-artifact audit staging stay broad; audit staging now logs identity (was bare `pass`) |
| native_worker.py | 2 | job-dispatch boundary stays broad (every failure must cross as the completion payload object — consumers map by exception class); atexit drain stays broad and now logs identity (was bare `pass`) |
| capture_ingestion_transaction.py | 3 | retarget/migration/ingest rollback-then-re-raise — stays broad |
| capture_connected_space.py | 2 | stage + persist transaction atomicity — stays broad |
| capture_semantic_promotion.py | 3 | migration/compose/promote transaction atomicity — stays broad |
| capture_watch_runner.py | 3 | watch-dir config read narrowed; per-drop routing + in-flight flag reset stay broad |
| capture_watch_failures.py | 2 | temp-file cleanup `except BaseException` — stays broad (covers KeyboardInterrupt), re-raises |
| capture_retention.py | 1 | purge transaction atomicity — stays broad |
| storage_watch_runner.py | 2 | settings read narrowed (`ストレージ監視設定の読み取り`); in-flight flag reset stays broad |
| cad_repository.py | 7 | borrow-lock release stays broad; `_close_for_release`/`__del__` stays broad and now logs identity (was bare `pass`); release-sweep path probe narrowed to `(OSError, RuntimeError)`; 3 journal write-throughs stay broad and now log identity (were bare `pass`) |
| cad_project_template_repository.py | 1 | save transaction atomicity — stays broad |
| cad_roomsim_repository.py | 1 | memoized batch-spec failure — stays broad (any failure type cached + re-raised) |
| cad_objective_repository.py | 1 | page-scan failure recorded on the scan for identical re-raise — stays broad |
| cad_robustness_repository.py | 2 | per-sample perturbation failures seal into `domain_rejections` + `perturbation_failure_reason` — stays broad |
| cad_acceptance_repository.py | 1 | per-evidence verify seals the exception type as the problem record — stays broad |
| cad_acoustic_treatment_repository.py | 1 | managed-asset-file cleanup before re-raise — stays broad |
| cad_apply_transaction_repository.py | 1 | #883 journal write-through — stays broad, now logs identity (was bare `pass`) |

**Totals: 23 narrowed to typed catches, 43 kept broad with justification —
all 66 carry the `error-boundary:` marker.**

## Behavior changes (defect fixes only)

- `native_worker._drain_worker_threads_at_exit`: a drain failure at
  interpreter exit was silently swallowed — now `logging.exception`
  records the identity; drain still never raises.
- `capture_receiver` rejected-envelope audit staging (do_POST `reject`
  path): `except Exception: pass` → logged; the wire verdict is unchanged.
- `capture_receiver` wire-reject boundaries (bundle read, ingest, inbox
  staging, field-return): narrowed so a sealed-authority failure
  propagates as a connection drop instead of being falsified into a 4xx
  reject — an honest wire outcome per the taxonomy.
- `capture_import._stage_rejected`: `except Exception: pass` → logged;
  the CLI exit code is unchanged.
- `cad_repository` `_close_for_release`/`__del__` and the three #883
  journal write-throughs: `except Exception: pass` → logged; release and
  journaling semantics unchanged.
- `capture_inbox.reconcile_orphaned_ingestions`: narrowed so an authority
  failure propagates instead of letting a partial sweep masquerade as a
  completed recovery.

## Remaining

None in this lane — the glob scope was fully converted (66 ≤ the ~120
cap). Other lanes retain `structural_followup_remaining` for later
tranches.

## Tests

`backend/tests/test_issue_955_capture_boundaries.py` adds:

- the AST guard (`test_no_unmarked_broad_catches_in_capture_scope`) over
  all 22 converted modules;
- failure-injection coverage for the converted boundaries: worker
  dispatch payload identity (expected + bug-class),
  `reconcile_orphaned_ingestions` expected-failure logging/authority
  propagation, and the `clear_recovery` journal write-through logging.
