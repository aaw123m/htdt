# Round 9 — data integrity & recovery journey

Scope: multi-instance safety on one data dir, the corrupted-store recovery
journey (launch dialog → restore), backup browse/restore on the Data
Management page, migration honesty (pre-upgrade copies, quarantine,
journaling), and project switch/delete honesty. Prior context:
`round8-lifecycle.md` items 10/11 (deferred with sketches),
`round8-deferred.md`, `round9-prefs.md` (data-root registry/relocation).

## Verdict table

| # | Finding | Severity | Disposition |
|---|---|---|---|
| 1 | Round-8 #10: a second `htdt` launch against a live data dir dead-ended on a critical "ディレクトリが使用中です" error — nothing told the user the running window was raised. The lock core already exists (`SingleInstanceGuard`: OS byte-range lock on `.instance.lock` byte 0, auto-released by the OS on process death — strictly better than the sketch's PID-mtime heartbeat since stale locks are impossible) | High (usability) | **FIXED** — second launch queues an `'activate'` launch intent to the running instance (file opens first, then activation) and shows an informational "すでに起動しています" dialog; exit 0. Falls back to the old error path only if the queue write itself fails |
| 2 | Round-8 #11: the corrupted-store recovery dialog offered "verify data" but never "restore from backup" even though `AutomaticBackupScheduler` generations already exist | High (recovery UX) | **FIXED** — `decide_launch` gains `backup_restore_available`; `restore_backup` choice （バックアップから復元） renders only for data-relevant failure classes AND ≥1 restorable archive; post-launch opens Data Management and previews the newest generation through the normal validate→confirm→restore pipeline |
| 3 | Restoring over a **corrupt** live store was impossible: `_restore_backup` first ran `_create_backup` on the live DB, whose `_sqlite_health` check aborts on unreadable bytes — the exact scenario the button exists for | High (data safety) | **FIXED** — pre-restore-backup failure falls back to a raw forensic copy (`*-pre-restore-unverified-*.sqlite3`); the journaled swap still evacuates/rolls back live bytes |
| 4 | Pre-upgrade snapshots (`<data_dir>/upgrade-recovery/pre-upgrade-*.htdt-backup`) were invisible to every restore surface — `list_generations` only scans the `-backups` sibling | Medium | **FIXED** — `list_restorable_backups` merges generations + upgrade snapshots, ordered by filename-encoded creation stamp (never mtime), newest first |
| 5 | Data Management "バックアップと復元" card had no awareness of saved generations — restore required hunting the archive file in a picker | Medium UX | **FIXED** — 保存済みバックアップ combo lists every restorable archive; the row hides when none exist; selection flows through the same preview→confirm→restore path |
| 6 | Migration journey: `execute_native_upgrade` does bounded disk preflight → mandatory validated pre-upgrade snapshot → migration → post-verify → journaled `UpgradeEvent`; failure quarantines and `upgrade_copy_ja` tells the user a recovery copy is created first | — | **VERIFIED** — honest, no half-migrated states; dialog before/after states consequences |
| 7 | Multi-instance write safety: SQLite serializes writes; `SingleInstanceGuard` is the mutual-exclusion layer; `runtime_instance` lock release on death verified; `test_native_cli_rejects_data_dir_already_in_use` keeps the maintenance path strict | — | **VERIFIED** — the only gap was UX (#1) |
| 8 | Project lifecycle (`plan_project_deletion` per-authority consequence counts, hard blockers incl. `project_not_archived`, atomic single-transaction delete + `PRAGMA foreign_key_check` + tombstone; `archive_project`/`unarchive_project`) is reachable **only from tests** — `ProjectLibraryPage` exposes 開く/新規プロジェクト only | Medium (feature gap) | **DEFERRED** — lifecycle UI is a product-surface decision; sketch below. The delete path itself is atomic — no half-deleted states possible |
| 9 | `--maintenance` + positional open paths previously forwarded intents then skipped the maintenance op (silent no-op of the CLI request) | Low | **CHANGED** — now exits 2 (`使用中`) rather than half-serving both verbs; forwarding happens only for the GUI launch |
| 10 | Integrity-check journey on Data Management: storage scan/GC surface covers managed-asset drift; DB-level integrity rides every restore preview (manifest/SHA-256/SQLite/foreign-key) and `verify_data` opens this page | — | **VERIFIED** — acceptable coverage; a dedicated "今すぐ整合性チェック" action remains a candidate but no integrity task is unreachable |
| 11 | Import/export completeness: migration export/import buttons + backup create/restore + relocation cover the three archive contracts; `project_bundle` import runs descriptor validation + ID remapping on the route path | — | **VERIFIED** |

## What was verified

- `runtime_instance.SingleInstanceGuard`: `msvcrt.locking` byte-range lock
  on `<data_dir>/.instance.lock`; the OS releases on process death, so no
  stale-lock detection is needed — adopted instead of the round-8 sketch's
  PID-mtime heartbeat file.
- Launch-intent queue (`launch_intents.py`): atomic JSON drop files in
  `launch-intents/incoming/`, at-least-once drain on an 800 ms timer,
  `done`/`failed`/`dead` completion triage. The running instance's dispatch
  already calls `raise_()`/`activateWindow()` before semantic routing —
  the `'activate'` intent piggybacks on that, so the raise is
  forward-only with zero new plumbing in the running process.
- Recovery choice contract: `decide_launch` stays pure (no I/O);
  `backup_restore_available` is computed once in `_run_gui` and the choice
  is rendered verbatim by `_choose_recovery_action` through
  `_RECOVERY_CHOICE_PRESENTATION`. Non-data-relevant crashes never see the
  button even when archives exist.
- Restore pipeline (`native_backup.restore_backup`): staged manifest +
  SHA-256 + SQLite integrity + foreign-key validation → pre-restore
  archive (or raw forensic copy when the live store is unreadable) →
  journaled swap with rollback → post-swap health check → journal close.
  `recover_interrupted_restore` replays an interrupted swap.
- Deletion honesty: `plan_project_deletion` fingerprints the plan against
  live authority state (staleness guard), lists per-authority row counts
  and the shared/local asset split, and hard-blocks unarchived projects;
  `delete_project` runs one transaction with `foreign_key_check` and a
  tombstone — there is no partial-delete window.

## Deferred sketches

- **Project lifecycle UI (#8):** `ProjectLibraryPage` row actions →
  `plan_project_deletion` preview dialog rendering the planner's per-
  authority counts + shared-asset split verbatim (the dialog must state
  ブックマーク/レビュー/アセットの削除数), confirm → `archive_project` (for
  unarchived) → `delete_project`. Archive/unarchive toggle on the same row.
  Everything needed already exists in `project_lifecycle.py` — this is a
  surface decision, not new plumbing.
- **Backup policy UI:** unchanged from `round9-prefs.md` #7 — the
  generations browser added here becomes the top of that section.

## Files changed

`backend/src/htdt/`:
- `launch_intents.py` — `'activate'` intent kind, `'activated'` outcome,
  `build_activation_intent` factory, Japanese description.
- `launch_router.py` — `'activate'` routes to `'activated'` (the window
  raise already happened in `_route_launch_intent`'s pre-dispatch).
- `startup_recovery.py` — `'restore_backup'` recovery choice +
  `backup_restore_available` flag threaded through `decide_launch`
  (pure function, no I/O).
- `automatic_backup.py` — `list_restorable_backups` merging generations
  and upgrade snapshots ordered by filename-encoded creation stamp.
- `native_upgrade.py` — `_list_upgrade_snapshots` → public
  `list_upgrade_snapshots` with contract docstring.
- `native_backup.py` — `_restore_backup` pre-restore backup failure now
  falls back to a raw forensic copy so a corrupt live store can't veto
  its own replacement.
- `native_cad.py` — lock-contention restructure (forward intents +
  activation → `_notify_instance_active` + clean exit), `'restore_backup'`
  presentation + post-launch settings/restore-preview wiring,
  `maintenance`+`open_paths` now refuses cleanly.
- `data_management_ui.py` — 保存済みバックアップ combo +
  "このバックアップを検証して復元…" on the operations card; hidden when
  empty, refreshed on show.

`backend/tests/`:
- `test_native_launch.py` — second-launch activation roundtrip, ordering
  (file intent before activate), forward-failure fallback to exit 2,
  recovery-dialog バックアップから復元 → preview-newest-generation.
- `test_startup_recovery.py` — `restore_backup` gating
  (data-class + archives required, never offered otherwise).
- `test_launch_intents.py` / `test_launch_router.py` — activate intent
  roundtrip/routing/outcome contract.
- `test_automatic_backup.py` — merged listing incl. upgrade snapshots.
- `test_native_backup.py` — restore over an unreadable live DB proceeds
  and preserves forensic bytes.
- `test_data_management_ui.py` — generations browse + preview dispatch.

## Tests

`cd backend && TMPDIR=/c/t PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen C:/devin/python/python.exe -m pytest -q -n 4`
— scoped touched-file runs green (147 tests); full-suite result recorded
in the PR description.
