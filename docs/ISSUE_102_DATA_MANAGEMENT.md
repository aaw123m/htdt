# Issue #102 — Data management controller and restore lifecycle

> Implementation branch: `feat/issue-102-data-management-controller`  
> Scope: backend/controller foundation for the future UX110 shell `設定 > データ管理`.  
> The legacy dock-based GUI is intentionally unchanged.

## Authority

The only backup/restore authority remains `htdt.native_backup`:

- `create_backup()`
- `validate_backup()`
- `restore_backup()`
- `BackupManifest`
- manifest/file SHA-256 validation
- SQLite integrity and foreign-key validation
- native schema compatibility validation
- bounded/path-safe extraction
- measurement-asset size/hash validation
- automatic pre-restore backup
- rollback on failed live swap

`data_management.py` does not parse ZIP members, copy SQLite files, validate hashes, implement compatibility rules, or reproduce rollback behavior.

## Added interfaces

### `DataManagementBackend`

A thin synchronous facade intended to be straightforward to test and to call from a worker thread.

- `create_backup(destination) -> BackupCreateResult`
  - delegates directly to `native_backup.create_backup()`
  - returns display metadata derived from the returned `BackupManifest`
- `preview_restore(backup_path) -> RestorePreview`
  - delegates directly to `native_backup.validate_backup()`
  - invalid/tampered/incompatible archives fail before a restore control is enabled
- `restore(preview) -> RestoreResult`
  - re-runs `validate_backup()` immediately before destructive restore
  - rejects a valid archive that changed after the preview was shown
  - delegates the actual destructive operation to `native_backup.restore_backup()`
  - therefore still passes through native staging validation, automatic pre-restore backup, live swap verification and rollback

The extra pre-restore validation is a stale-preview guard, not a second backup contract.

### `BackupMetadata`

Read-only presentation data derived only from `BackupManifest` and the selected archive file size:

- creation time
- application version
- backup schema version
- archive size
- database size
- measurement asset count / bytes
- managed file count / bytes
- manifest SHA-256

The hash is retained for provenance/stale-preview diagnostics; the normal Settings UI should not show it unless a diagnostic/detail view explicitly needs it. No DB query or new metadata store is introduced for GUI display.

### `DataManagementController`

Qt-facing asynchronous controller. All archive/database work is executed by a dedicated `QThread`; public methods return immediately with an operation ID.

Signals:

- `busy_changed(bool)`
- `progress_changed(DataOperationProgress)`
- `backup_created(BackupCreateResult)`
- `restore_preview_ready(RestorePreview)`
- `restore_completed(RestoreResult)`
- `operation_failed(DataOperationFailure)`

Progress is intentionally phase-based and indeterminate. The native backup authority currently has no safe byte-level progress/cancellation callbacks, so the GUI must not invent percentages or interrupt archive mutation mid-operation.

Only one data-management operation may run at a time. While busy, the shell should disable the page's other actions. `can_close_application == False` while a worker is active; because the native authority is not mid-operation cancellable, the shell should defer normal application close until completion instead of terminating the worker.

## Application-level restore lifecycle

`ApplicationDataLifecycle` is the integration boundary that prevents stale DB / Scene state after restore.

The UX110 shell must provide four callbacks:

1. `freeze_mutations`
   - disable scene/data mutation commands across workspaces
2. `release_data_handles`
   - dispose current workspace pages and release every live repository / WorkingDocument / Scene reference
   - do not retain objects for later reattachment
3. `reopen_data_handles`
   - construct fresh repositories from the restored data directory
   - reconstruct current shell/workspace state from persisted IDs/records, not old Python objects
4. `thaw_mutations`
   - re-enable mutation commands after fresh handles are installed

Restore sequencing is fixed:

```text
preview_restore (worker, read only)
  -> user confirms
  -> freeze_mutations (GUI thread)
  -> release_data_handles (GUI thread)
  -> validate preview again (worker)
  -> native_backup.restore_backup (worker)
       -> native validation/staging
       -> automatic pre-restore backup
       -> live swap
       -> post-swap integrity/hash validation
       -> rollback on failure
  -> reopen_data_handles (GUI thread)
  -> lifecycle generation += 1
  -> thaw_mutations
```

The same fresh-handle rebuild runs after a failed restore attempt, because a restore failure may have exercised rollback and the old in-memory Scene objects were deliberately detached before the operation.

If rebuilding the application state itself fails, lifecycle state becomes `restart_required`. Old handles remain detached and mutations remain frozen; the UI should report that the data operation completed/rolled back but HTDT must be restarted. It must not reattach the pre-restore repository or Scene objects.

## UX110 `設定 > データ管理` integration

Recommended composition:

```python
backend = DataManagementBackend(data_dir)
lifecycle = ApplicationDataLifecycle(
    freeze_mutations=shell.freeze_data_mutations,
    release_data_handles=shell.dispose_data_workspaces,
    reopen_data_handles=shell.rebuild_data_workspaces,
    thaw_mutations=shell.thaw_data_mutations,
)
controller = DataManagementController(backend, lifecycle, parent=shell)
```

Flow:

- **バックアップを作成**
  - choose `.htdt-backup` destination
  - call `controller.create_backup(path)`
  - show phase progress
  - on `backup_created`, show `BackupMetadata`
- **バックアップから復元**
  - choose archive
  - call `controller.preview_restore(path)`
  - only `restore_preview_ready` enables the destructive confirmation button
  - show metadata from the preview
  - after confirmation call `controller.restore(preview)`
  - on success show restored metadata and `pre_restore_backup`
  - if failure has `restart_required=True`, keep the shell mutation-disabled and request restart
- **バックアップを検証**
  - use the same `preview_restore()` path and simply omit the restore confirmation

Do not add a new `QDockWidget` for this feature. The final view belongs in the UX110 shell's settings/data-management route.

## Concurrency and lock boundary

The existing `SingleInstanceGuard` remains the cross-process authority for the data directory. The in-process lifecycle freezes GUI mutation while backup/restore is active. The controller does not create a second filesystem-lock format.

The guard's authority is an exclusive OS byte-range lock on `.instance.lock` inside the data directory (`msvcrt.locking` on Windows, `flock` elsewhere), so exclusion follows the filesystem path rather than the Windows session: holders in other sessions (RDP, fast user switching) contend on the same lock, and on shared filesystems that honor byte-range locking (SMB) the exclusion extends across machines. A filesystem that cannot honor the lock raises OSError and the launch fails closed. The OS owns the lock and releases it on process termination; the lock file is never deleted and the owner metadata stored after the locked byte is advisory diagnostics only, so stale files or metadata can never brick a data directory.

A backup can use SQLite's existing consistent snapshot behavior, but GUI mutations are still frozen for the operation so measurement asset copying cannot race user edits.

## Tests

`backend/tests/test_data_management.py` covers only the new boundary contracts:

- display metadata is derived from a successfully validated native manifest
- a valid archive changed after preview is rejected before destructive restore
- wrapper restore still produces the native automatic pre-restore backup and restores native data/assets
- restore lifecycle releases old handles before restore and rebuilds fresh handles afterward
- failed application reload leaves lifecycle in `restart_required` without thawing or reattaching stale handles

The existing `test_native_backup.py` remains the authority-level integrity/path traversal/hash/rollback coverage and is not duplicated.

## Scope / remaining integration

Completed in this PR:

- backup creation controller/backend
- restore validation and preview
- backup metadata presentation model
- non-blocking Qt worker/controller with progress and failure model
- stale-preview protection
- application-level stale-handle lifecycle contract
- new-shell integration contract

Intentionally deferred to UX110 shell integration:

- actual Settings/Data Management widgets and file dialogs
- visual progress/error presentation
- persisted "last backup time" UI preference, if desired
- first-run migration wizard
- Windows packaged visual acceptance of the final shell route

The existing backup archive format and native authority are unchanged. Issue #101 / PR #116 acoustic-solver files are untouched.
