# Round 14 — regression audit of the review round itself

Scope: every change merged onto `main` by round 14
(`git log 629c146e..140ca03c` — GEO, UPDATE, ERRMSG, DIALOG, NOTIFY,
AUDIT, SEARCH, RENDER merges; ~210 fixes, 62 files), audited for bugs the
*fixes* introduced — the same class round-4's regression audit found:
signature drift past un-updated callers, test doubles shadowing stale
signatures, behavioral narrowing, JP-string coupling, cross-fix
composition on multiply-edited files, and doc-vs-behavior drift.

Regression tests for the findings below live in
`backend/tests/test_data_management_ui.py`
(`test_revalidate_button_runs_revalidation_and_shows_report`),
`backend/tests/test_native_upgrade.py`
(`test_quarantine_retry_event_journals_declared_stale_authorities`), and
`backend/tests/test_authority_revalidation.py`
(`test_summary_ja_points_at_the_real_backup_checkbox_label`). Each failed
before its fix.

## Regressions introduced by round-14 fixes — found and fixed

| # | Site | Introduced by | Regression | Fix |
|---|------|---------------|------------|-----|
| 1 | `backend/tests/test_data_management_ui.py` `_FakeController` | REV14-UPDATE (revalidation lane) | The controller test double shadows `DataManagementController` but was never extended with the new `revalidate()` method — the exact test-double drift the brief warns about. The update merge fixed the `create_backup` double in `test_workflow_application.py` (fae2bef2) but missed this one, leaving the page's revalidation button path untestable: any test driving it produced the error surface (`AttributeError` caught by the broad except), masking the real contract. | `_FakeController` gained `revalidate()` (returns a stub report); a button-click test asserts `controller.revalidate` is invoked and `QMessageBox.information` shows the report summary. |
| 2 | `backend/src/htdt/native_upgrade.py` `_resolve_quarantined_generation` | REV14-UPDATE (stale-aware upgrade) | The quarantine-retry completion event omitted `stale_authority_count`, which the first-pass completed event journals (`event.stale_authority_count == len(declaration)`). A generation re-verified over tolerated stale rows journaled `None` — per the field's own contract (「0/None for a fully verified generation」) that reads as *fully verified*, under-reporting the exact condition the first-pass path reports. Same contract, two writers, one forgot it. | Retry event now sets `stale_authority_count=len(marker.declared_stale_authorities)` — the marker's declared set is the same set the verify pass tolerated. |
| 3 | `backend/src/htdt/authority_revalidation.py` `summary_ja` | REV14-UPDATE (kept-stale guidance) | The kept-stale guidance told users to enable 「要再検証の記録を含めてバックアップ」 — a checkbox label that does not exist. The actual operations-card checkbox reads 「検証を通過しない記録を含めてバックアップする（対象はマニフェストに明記されます）」 (`data_management_ui.py`). The label was renamed during the merge and the summary text written earlier in the same merge wasn't updated — a user reading the revalidation report was pointed at a control they cannot find. | `summary_ja` now names the real checkbox label; a test pins it so a future label rename fails loudly. |

## Audited, no regression found

- **Signature compatibility across the round:** every new parameter is a
  back-compatible keyword addition — `create_backup(*, allow_stale=False)`,
  `report_launch_failure(technical_detail=None)`,
  `DataOperationFailure.technical_detail: str = ''`,
  `BackupMetadata.stale_authority_count: int = 0`,
  `_verify_upgraded_database(*, tolerated_stale=())`. All callers updated;
  the one *required* new parameter
  (`promote_hybrid_provider_evidence(external_payload_resolver=…)`) has its
  only production call site (repository `_validate`) updated, and the
  production GUI path never constructs that repository
  (`hybrid_provider_repository=None` default) — strictness is confined to
  tests/embedders as intended.
- **Other test doubles:** `test_workflow_application.py`'s monkeypatched
  `create_backup` was already fixed for `allow_stale` (fae2bef2);
  `_FakeController.create_backup` in `test_data_management_ui.py` already
  carries the kwarg. `revalidate()` was the only missing shadow (finding 1).
- **Behavioral narrowing in the 19 dialogs:** every new `accept()`
  validation mirrors the backend model validator the dialog feeds
  (`_SAMPLED_TIERS` on ScreenTransferDialog, all-unsupported-materials on
  MaterialDialog, aisle syntax on SeatingLayoutDialog — whose `spec()`
  already warned and returned `None`, so `accept()` only moved the same
  rejection earlier). No previously-accepted legitimate input is newly
  refused; dialogs that can't produce a valid spec now explain why while
  open. The `standards_profile_editor` sentinel fix (`!= minimum()` where
  `!= maximum()` stood) verified to point the right direction.
- **JP string/contract coupling:** `operation_error_message`'s
  `_NAME_PATTERNS` is MRO-ordered correctly (NativeUpgradeQuarantineError
  before NativeUpgradeError, RewApiUnavailable before RewApiError,
  MigrationOpenError before NativeSchemaError); `_looks_localized` keeps
  authored single-line JP `ValueError`s intact; `last_error` consumers are
  display-only (`capture_receiver_settings`, status lines) — nothing
  branches on message content; `_LAUNCH_CLASS_REASON_JA` covers every
  non-`unknown` class `classify_startup_failure` can return, and `unknown`
  correctly falls back to the mapped generic.
- **New-module edge coverage:** `authority_revalidation` sorts work by
  `_REVALIDATOR_ORDER` (profile → wiring-check → comparison, the
  dependency order), dedupes diagnostics per (authority, record_ref),
  per-diagnostic try/except keeps one bad row from aborting the pass,
  `_semantic_drift` keeps semantically-tampered rows stale instead of
  re-signing them, and the post-pass audit flips falsely-claimed
  'revalidated' outcomes back to kept_stale. The ~37 probe authorities
  without revalidation lanes are documented-deferred, not silently
  dropped.
- **Degraded-backup symmetry:** `create_backup(allow_stale=True)`
  declares stale rows on the manifest; `inspect_backup` surfaces
  `stale_authority_count`; `_stage_backup` refuses undeclared failures
  `(authority, record_ref, failure_class)` and *any* unclassified table —
  the same check on the restore side; `AutomaticBackupScheduler` and the
  pre-restore safety copy deliberately pass `allow_stale=True` so safety
  generations never silently stop — consistent intent, verified end to
  end in `test_plain_backup_refuses_but_degraded_backup_roundtrips`.
- **Cross-fix composition on `data_management.py`** (touched by GEO,
  UPDATE, ERRMSG, NOTIFY): `result_summary = lifecycle_message or
  _result_summary(kind, result)` — `_result_summary`'s isinstance
  dispatch verified against every result model's actual field names;
  `detail=operation_error_message(exc)` composes with
  `technical_detail=str(exc)`; `controller.revalidate()` exists on the
  real backend and controller.
- **Capture timeline (AUDIT):** `capture_promoted` now emits only per
  durable promotion record (records deleted → no fabricated event — the
  fix's intent); new `capture_superseded`/`capture_deferred` kinds added
  to `ACTIVITY_EVENT_KINDS` and the Literal together; `_event_sort_key`
  normalizes naive/unparseable timestamps without crashing; no consumer
  holds an exhaustive kind→label map that could KeyError.
- **Doc-vs-behavior spot checks:** the `UpgradeEvent.stale_authority_count`
  docstring exposed finding 2; `summary_ja` vs the real checkbox label
  exposed finding 3; `test_round9_error_surfaces`/`test_native_diagnostics`
  expectation updates (localized `reason` + `technical_detail`) verified
  as honest contract moves, not pinned bugs.

## Deferred / noted, not fixed

- **`_queue_refresh` listener lifetime (NOTIFY):**
  `activity_center.subscribe` has no unsubscribe — the data-management
  page's closure keeps the widget alive and `_emit` invokes listeners
  with no exception guard. A widget can only be deleted via
  `dispose_mounts()` (shell shutdown), after which an in-flight operation
  emitting once more would raise `RuntimeError` (deleted C++ receiver)
  inside `_emit` on the mutating thread. Reachable only during teardown;
  left for a follow-up that adds listener-unsubscribe or emit guards.
- **`write_launch_marker` stamped before the once-per-build audit:** a
  transient audit failure permanently suppresses the post-update offer.
  Deliberate per the code comment (no re-prompt every launch); the
  revalidation lane remains reachable from the data-management page.
- **`_stage_backup` compares triples, not dependency/message:** a row
  failing on the destination with the same (authority, record_ref,
  failure_class) but a different dependency detail still passes — the
  declared set is honest at the identity level the manifest records.
- **Pre-existing:** `xdist` fixture flake noted in round14-errmsg.md's
  deferred table observed once during the baseline run; unrelated to
  round-14 changes.

## Verification

- Each finding got a failing test *before* its fix; all three now pass.
- Affected files in full: `test_data_management_ui.py`,
  `test_native_upgrade.py`, `test_authority_revalidation.py` — green.
- Full backend suite (`pytest backend/tests -q -n 4`,
  `QT_QPA_PLATFORM=offscreen`) run to completion on this branch.
