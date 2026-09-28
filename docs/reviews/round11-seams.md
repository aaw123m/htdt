# Round 11 — cross-feature integration seams

Scope: the places where features merged in different rounds meet — traced
end-to-end in code, fixed where the join was broken or lying. Branch
`devin/rev11-seam`. Verification is Qt-offscreen under Python 3.12.10 plus
pytest.

## Verdict table

| # | Seam | Verdict |
|---|------|---------|
| 1 | Single-instance forwarding × recovery | FIXED — two honest-behavior gaps found and closed (details below). The 'activate' intent itself routes sanely: the drain pump starts only after `window.show()`, so a recovery/safe-mode dialog already owns the screen when intents drain; `raise_()`/`activateWindow()` over a modal dialog is a no-op, and 'activated' returns early with no popup. A queued file-open intent landing on a safe-mode instance is legitimate — the safe-mode banner says the app is degraded and 'choose_another_project' is exactly the same switch path. |
| 2 | Backup-policy UI × AutomaticBackupRunner | HEALTHY — the runner constructs `AutomaticBackupScheduler(data_dir)` inside the job on every tick, so the saved policy (interval/generations/dir) is re-read from disk each run; the card's "変更は次回の起動時チェックから適用されます" copy is accurate. Minor seam fixed: a manual `create_backup` never marked the scheduler fingerprint, so the next launch tick archived identical bytes — `record_external_generation()` now records coverage without advancing `last_automatic_at_utc`. |
| 3 | Activity Center × command registry × cancel | HEALTHY-ish — cap-20 works per session; uncaught pseudo-ops take the legal QUEUED→FAILED transition; failed commands surface the mapped `warn_user` message with the retry affordance. Two nits: identical crash loops aren't deduped beyond the cap (bounded at 20; the diagnostics log holds the full loop — deferred), and the 'cancelled' state label read "キャンセル" (request, not outcome) — now "キャンセル済み". |
| 4 | RobustnessSpec authoring × joint lane | BROKEN → FIXED — `select_section` refreshed every mounted panel except `joint_optimization_panel`; `refresh_from_authorities` skipped it too. After authoring an O90 spec the joint create button stayed disabled with the stale "O90ばらつき評価仕様…で作成" reason until an app restart. Both paths now refresh the panel; `resolve_baseline` picks the newest spec on the next refresh. |
| 5 | Lifecycle UI × project switching × backups | FIXED — delete was already correct (archive→re-plan→atomic `delete_project(expected_plan)`→refresh, tombstone recorded); archived entries refuse uniformly via `ProjectLibraryError`→JP mapping. Two gaps: blocker `detail` strings rendered verbatim English in both blocked dialogs (now a JP kind→line map fed by a new `count` field on `DeletionBlocker` — fingerprint-safe since it hashes kinds only), and the designed-for `pre_destructive` trigger had zero callers — `_delete_selected` now offers a best-effort safety generation before the irreversible delete, with honest confirm copy gated on `policy.enabled`. |
| 6 | i18n × round-10 additions | FIXED — splash, backup-policy card, robustness authoring panel and retry affordance are all JP. Leaks closed: deletion blocker details (seam 5), storage-inventory category ids (`native-database`/`managed-assets`/`diagnostics`), backup-excluded registry names (`application_preferences`, … — 21 ids mapped, coverage test-pinned against `backup_excluded_names()`), and the joint panel's `Exception(str(error))` type-stripping that demoted every worker failure to the generic mapped message. |
| 7 | Migration v5→v6 × upgrade recovery | HEALTHY — restore routes old-schema archives through `execute_native_upgrade`; `_migrate_5_to_6` runs `NATIVE_COLUMN_ENSURES` which includes `cad_frequency_responses.dataset_sha256`, so a pre-v6 backup restored today gains the column through the same authority as an in-place upgrade. Restore preview renders honest copy for all four `NativeSchemaCompatibility` states including legacy `0`. |
| 8 | measure-display policy × preference surfaces | HEALTHY — the display-length preference is one global `application_preferences` key live-bound to every mounted measure surface via weakref subscriptions; window state persists geometry/workspace/context-ids only, never units — no per-project unit surface exists to disagree. |

## Seam 1 detail — what the router dropped

`_route_launch_intent` called `application._open_project(...)` and, when
the switch didn't happen, showed fixed "save or discard your work" copy —
discarding the REAL reason `_switch_project` returns ('データ処理中は…',
dispose-gate reason, mapped archive/permission error). A queued open on a
frozen or archived target therefore lied about the cause. The router now
calls `_switch_project` directly and repeats its returned reason; the
generic copy survives only as the unreachable-but-defensive fallback.

`drain_launch_intents` had no expiry: a valid queue file persisted
forever, so a drop left by a crashed second instance fired verbatim on
the *next* launch — an open/stage/preview nobody asked for.
`LAUNCH_INTENT_STALE_SECONDS = 900` ages valid drops into `dead/` with a
warning; `max_age_seconds=None` keeps the old behavior for tests.

## Files changed

- `src/htdt/launch_intents.py` — `LAUNCH_INTENT_STALE_SECONDS` +
  mtime-based expiry in `drain_launch_intents`.
- `src/htdt/native_cad.py` — router surfaces `_switch_project`'s own
  reason as the block detail.
- `src/htdt/optimization_workflow_workspace.py` — `select_section('setup')`
  and `refresh_from_authorities` refresh the joint panel.
- `src/htdt/joint_optimization_panel.py` — worker failure passes the real
  exception to `operation_error_message`.
- `src/htdt/project_lifecycle.py` — `DeletionBlocker.count` populated by
  the planner.
- `src/htdt/application_pages.py` — `_DELETION_BLOCKER_LINES` JP map used
  by both blocked dialogs; pre-destructive safety generation with honest
  conditional copy; 'キャンセル済み' label.
- `src/htdt/automatic_backup.py` — `record_external_generation()`.
- `src/htdt/data_management.py` — manual `create_backup` marks coverage.
- `src/htdt/data_management_ui.py` — `_EXCLUDED_COMPONENT_LABELS` /
  `_STORAGE_CATEGORY_LABELS`.
- `tests/test_round11_seams.py` — 11 regression tests.

## Deferred

- Uncaught pseudo-op dedupe beyond the cap-20 bound: bounded and honest
  today; the diagnostics log records the full loop for triage.
