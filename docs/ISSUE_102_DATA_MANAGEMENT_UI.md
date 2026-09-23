# Issue #102 — Settings / Data Management UI

> Branch: `feat/issue-102-data-management-ui`  
> Scope: mountable `設定 > データ管理` component over the already-merged
> `DataManagementController`, `ApplicationDataLifecycle`, and `native_backup` authority.  
> `native_cad.py` and `workflow_shell.py` are intentionally unchanged. RDC was not used.

## Implemented surface

`backend/src/htdt/data_management_ui.py` provides:

- `DataManagementWidget`
- `DataManagementComponent`
- `build_data_management_component(controller, ...)`
- native save/open dialog adapter
- restore confirmation adapter
- presentation-only backup metadata view

The page contains:

- **別のPCへ移行**
  - **移行ファイルを作成**
  - **以前のPCの移行ファイルを読み込む**
- **バックアップと復元**
  - **バックアップを作成**
  - **復元ファイルを選択**
- restore-before-write validation preview
- backup metadata
- indeterminate phase progress / busy state
- structured failure presentation
- restart-required presentation
- restored backup and automatic pre-restore backup result

The migration buttons are short entries into the exact same backup/preview/restore
controller methods. There is no second migration format or migration backend.

## Authority boundary

The UI does **not** open, parse, hash, extract, inspect, or copy backup archive
contents. It does not query SQLite for validation and does not implement a
compatibility matrix.

Its only operation calls are:

```text
DataManagementController.create_backup(destination)
DataManagementController.preview_restore(backup_path)
DataManagementController.restore(preview)
```

Those controller calls continue to delegate to the sole authority in
`htdt.native_backup`:

```text
create_backup()
validate_backup()
restore_backup()
```

Therefore destructive restore still passes through:

1. selected-archive validation
2. stale-preview revalidation
3. lifecycle freeze + release of live data handles
4. native staging validation
5. automatic pre-restore backup
6. native live swap
7. post-swap integrity/hash validation
8. native rollback on failure
9. fresh application-data handle rebuild

The UI never offers a raw SQLite copy or direct overwrite path.

## Restore fail-closed behavior

Selecting a `.htdt-backup` never immediately restores it.

```text
file dialog
  -> clear any previous preview
  -> controller.preview_restore(path)
  -> busy / validation progress
  -> restore_preview_ready(preview)
       -> display metadata
       -> display validation-authority PASS summary
       -> enable restore button
  -> explicit confirmation
  -> controller.restore(preview)
```

If preview validation fails, the restore button stays disabled. If restore
fails, the existing preview is discarded so the user must validate again
before another destructive attempt.

The validation summary shown after `restore_preview_ready` is presentation of
the native authority's successful checks, not independently calculated status.
The manifest SHA is deliberately not shown in the normal page.

## Progress and cancellation

`native_backup` currently exposes phase transitions but no authoritative
byte-level progress or safe mid-operation cancellation hook.

The UI therefore uses an indeterminate `QProgressBar` for current production
operations and displays the controller's Japanese phase message. It does not
invent percentages and does not terminate the worker thread.

While the controller is busy:

- backup / validation / migration / restore actions are disabled
- `DataManagementComponent.can_close_application` mirrors the controller close
  boundary
- `DataManagementComponent.before_deactivate()` blocks route deactivation

## Restart-required behavior

If `ApplicationDataLifecycle` cannot rebuild fresh handles after restore or
rollback, the controller reports `restart_required=True`.

The component then:

- keeps all data actions disabled
- displays a persistent **再起動が必要です** state
- makes `before_deactivate()` fail closed
- still permits application close once the worker has stopped, so the user can
  restart HTDT

It does not reattach old repository / WorkingDocument / Scene objects.

## Shell / Settings integration

The component is intentionally not wired into `workflow_shell.py`. A future
Settings route owns placement.

Composition:

```python
backend = DataManagementBackend(data_dir)
lifecycle = ApplicationDataLifecycle(
    freeze_mutations=shell.freeze_data_mutations,
    release_data_handles=shell.dispose_data_workspaces,
    reopen_data_handles=shell.rebuild_data_workspaces,
    thaw_mutations=shell.thaw_data_mutations,
)
controller = DataManagementController(backend, lifecycle, parent=shell)

data_management = build_data_management_component(controller)
settings_stack.addWidget(data_management.widget)
```

Route / close integration:

```python
allowed, reason = data_management.before_deactivate()
if not allowed:
    show_route_block_reason(reason)

if not data_management.can_close_application:
    defer_application_close()
```

The lifecycle callbacks remain application-composition responsibilities because
only the shell/application root can authoritatively release and rebuild all
live data consumers.

## UI presentation rules

The component consumes the shared dark-first `ui_theme` tokens and does not
create a separate visual authority.

Normal metadata shown:

- backup creation time
- HTDT version
- backup schema version
- archive size
- database size
- measurement asset count / bytes
- total managed bytes
- managed file count
- backup path

The normal page does not show manifest hashes or internal operation IDs.

## Tests

Focused tests are in `backend/tests/test_data_management_ui.py` and cover:

- unparented/mountable component contract
- restore disabled before a successful preview
- invalid preview remains fail-closed
- backup and PC-migration entries delegate to the same controller methods
- validation preview and metadata presentation
- explicit restore confirmation before controller restore
- restore result / pre-restore-backup presentation
- busy action lock and phase progress
- restart-required persistent lock and route guard

Authority-level hash/integrity/path traversal/schema/rollback tests remain in
the existing `test_native_backup.py`. The UI tests do not duplicate them.


## Shell integration update — 2026-09-19

The UX120–UX140 integration composition now hosts this component from the shell rail's **設定** utility. `ApplicationDataLifecycle` freezes navigation, checks every mounted workspace guard, disposes all data workspaces, delegates destructive restore to the existing native backup authority, constructs a fresh `SceneRepository`, and lazily rebuilds workspaces from Overview. The accepted legacy launcher remains unchanged until UX160 acceptance.
