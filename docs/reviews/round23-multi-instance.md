# Round 23 — multi-instance / multi-window truth

Scope: everything that happens when the app or its data is touched
concurrently — a second process on the same `--data-dir`, a project
switch while a worker runs, project files edited by another process
while open, lock files surviving a crash, shared on-disk resources
written concurrently, SPA + API races between two browser tabs, and
batch ops spanning multiple seats/documents. Each claim was verified by
running two writers/readers or simulating the external modification in a
test. Branch `devin/rev23-multi`. Verification is Qt-offscreen
(`QT_QPA_PLATFORM=offscreen`) under Python 3.12.10 plus pytest
(`backend/tests/test_round23_multi_instance.py`, 9 tests — every test
fails on the pre-fix tree and passes on the fixed tree).

## The concurrency model (as verified)

* **Single-instance authority.** `SingleInstanceGuard`
  (`runtime_instance.py`) holds an OS byte-range lock on byte 0 of
  `<data_dir>/.instance.lock` (`msvcrt.locking` / `fcntl.flock`). The OS
  releases it when the holder dies, so a stale lock file can never brick
  relaunch — an existing file is opened and re-locked, never trusted as
  evidence. Advisory JSON metadata (app id, pid, host, acquired_at)
  lives at byte 1+ and is informational only: acquisition never reads
  it, `release()` truncates it. A second GUI launch forwards each open
  path plus an activation intent to the running instance via the atomic
  drop-queue in `launch-intents/incoming/` (temp+`os.replace`, `time_ns`
  + per-process seq + uuid names) and exits 0 honestly; a maintenance
  launch (`--backup`/`--restore`/`--migrate-legacy-data`/…) exits 2 with
  an explicit "another process uses this data directory" message —
  verified by `test_runtime_instance.py`,
  `test_launch_intents.py`, `test_native_launch.py`. Intent drain
  tolerates malformed files (→ `dead/`), stale files (>900 s → `dead/`),
  and defers re-delivery while a modal dialog owns the event loop.
* **Two windows / switch mid-op.** One process means one repository; a
  project switch or dispose while work is in flight goes through the
  dirty-state contract (`workspace_dirty_state.py`): `busy` offers an
  explicit destructive `stop_busy` choice — the worker is never killed
  silently. `SceneRepository.save` holds `BEGIN IMMEDIATE` and CASes the
  `scene_document_heads` row inside the transaction: a stale parent or a
  head moved by another writer raises `SceneRevisionConflictError`
  instead of last-write-wins.
* **Crash recovery.** `previous_session_unexpected_end`
  (`support_diagnostics.py`) combines `runtime.json` pid-liveness
  evidence (when produced) with launch records — never the advisory lock
  metadata — to offer recovery honestly. `_probe_lock_state` performs a
  real non-blocking lock attempt rather than trusting file contents.
* **Relocation.** `htdt-relocation.lock` (same byte-range idiom) plus a
  journal that lives outside both roots; stale journals are settled
  deterministically at launch, a live journal blocks open.
* **Shared resources.** `ManagedAssetStore` content-addressed installs
  are temp+fsync+`os.replace` with a Windows replace-race read-back
  fallback; `create_backup` snapshots via the SQLite online-backup API
  (consistent over a live DB); storage GC re-proves unreachability
  inside one `BEGIN IMMEDIATE`, content-verifies each candidate, and
  journals pending deletes so a crash is retryable
  (`storage_maintenance.py`). Restore stages, hash-verifies, audits the
  authority graph on a throwaway clone, then swaps through a rollback
  journal.
* **Multi-seat batch.** `rew_roomsim_batch` does a three-way merge
  (before/owned/current), restores only own-changed positions, and
  raises `RewRoomSimConcurrentChange` on external modification — the
  exemplar of honest conflict handling. `import_project_bundle` applies
  every row in one transaction after validating the manifest and
  installing assets content-addressed first; reimport is idempotent.

## Findings

| # | Area | State | Evidence |
|---|------|-------|----------|
| 1 | Second instance on same `--data-dir` | OK | byte-range lock + intent forwarding; maintenance exits 2; cross-process kill test |
| 2 | Project switch / dispose mid-op | OK | dirty-state `busy`/`stop_busy` contract, head CAS |
| 3 | Stale lock files / release on exit | OK | OS-held locks; release truncates metadata; stale file never gates |
| 4 | External modification — native DB | OK | schema-signature memo re-verifies on file change; SQLite locks + row integrity |
| 5 | External modification — `application_preferences.json` | **FIXED** | store kept a stale document and overwrote interleaved edits |
| 6 | External modification — `library_meta.json` archive flags | **FIXED** | same load-once / blind-overwrite pattern |
| 7 | Attachment/asset store concurrent writes | **FIXED** | failed-import `unlink` could delete a file another request had just adopted |
| 8 | Backups written concurrently | OK | SQLite backup API + staged publish + prune kept to automatic classifications |
| 9 | Logs / diagnostics | OK | activity history is one atomic snapshot write on the GUI thread |
| 10 | SPA + API: `create_context` revision sequence | **FIXED** | `MAX(revision)+1` read-then-write raced to a UNIQUE 500 across tabs |
| 11 | Capture receiver delivery-ledger dedup | **FIXED** | existence check raced the insert → `delivery_key` PK IntegrityError |
| 12 | Launch-intent drop queue | OK | atomic names, dead-letter, redelivery while modal |
| 13 | `runtime.json` absence | OK | launch-record evidence covers it; only an installed forwarder writes it |

## Fixes

1. **`Store.create_context` — BEGIN IMMEDIATE**
   (`database.py`). The revision sequence `MAX(revision_number)+1` was
   computed in autocommit and inserted in a deferred transaction: two
   concurrent requests (two SPA tabs, a retry plus a manual action) both
   read the same maximum and collided on
   `UNIQUE(project_id, revision_number)` → `sqlite3.IntegrityError` →
   HTTP 500 with no conflict semantics. The transaction now begins
   `BEGIN IMMEDIATE` — the codebase's established write-serialization
   idiom — so the second request waits out the first's commit and
   computes the next revision. Verified by
   `test_create_context_waits_for_external_writer` (a held external
   writer → the caller serializes and lands revision 2; pre-fix it
   raised `IntegrityError`) and
   `test_create_context_concurrent_requests_get_distinct_revisions`
   (8 racing callers → revisions 1..8).

2. **`Store` asset install — never delete a content-addressed file on a
   failed transaction** (`database.py`). `import_measurement`,
   `import_rew_api_snapshot`, and `attach_asset` unlinked the asset file
   in the `except` path whenever their own `_store_asset` call had
   created it. Because the files are content-addressed, a concurrent
   importer of the same bytes can adopt the file (INSERT the `assets`
   row + referencing rows) between this caller's create and its failure
   — the unlink then produced a dangling `assets` row: a
   `missing_asset` integrity problem, and the next read raises
   `AssetIntegrityError`. The cleanup is removed entirely: an
   unreferenced content-addressed file is the established safe failure
   mode (same doctrine `storage_maintenance.py` documents: keep orphans
   over deleting referenced data). `_store_asset` now returns
   `(digest, path, already_known)` — the `created` flag existed only to
   arm the unsafe unlink. Verified by
   `test_failed_import_keeps_content_addressed_asset` and the true
   two-`Store` interleave
   `test_failed_import_never_removes_adopted_asset`.

3. **`CaptureReceiverService._record_delivery` — BEGIN IMMEDIATE**
   (`capture_receiver.py`). The dedup `SELECT` ran before the deferred
   transaction's first write, so two racing re-deliveries of the same
   `delivery_key` both saw `existing=None` and collided on the primary
   key instead of deduping — a 500 on what is by design an idempotent
   retry. `BEGIN IMMEDIATE` serializes check and insert, matching the
   repository idiom. Verified by
   `test_record_delivery_dedup_serializes_concurrent_insert` (pre-fix →
   `IntegrityError`; post-fix → the stored row returned).

4. **`ApplicationPreferenceStore` — external-modification merge**
   (`application_preferences.py`). The store consumed the file once at
   construction and every later `set`/`update`/`reset` rewrote the whole
   document from the stale in-memory copy — a manual edit, sync-tool
   write, or a second store on the same path was silently destroyed, and
   an externally corrupted file was happily overwritten (losing the
   honest CORRUPT refusal). The store now records an on-disk signature
   (dev/ino/mtime_ns/ctime_ns/size — the same tuple the schema-checker
   memo uses) at every load and after every successful persist;
   `_refresh_if_modified()` re-reads at the top of each write entry
   point when the signature differs, so the write merges onto the
   freshest document: external edits to other keys survive, a deleted
   file recreates honestly from MISSING, and a corrupt/newer-schema
   external write refreshes into the existing refusal path
   (`IncompatiblePreferencesError`, file untouched). Verified by the
   three `test_preferences_external_*` tests.

5. **`LibraryMetaStore` — same staleness fix**
   (`reference_libraries.py`). Identical load-once/blind-overwrite
   pattern for archive flags; same signature tracking +
   refresh-on-write. `_load_current` also clears `_archived` first so a
   refresh that lands on a corrupt file degrades to the documented
   empty set instead of resurrecting stale flags. Verified by
   `test_library_meta_external_edit_merged`.

## Deferred (verified, deliberately unchanged)

* **`FileDialogMemoryStore`** (`file_dialog_memory.py`) shares the
  load-once/blind-overwrite pattern, but its payload is a remembered
  dialog directory — losing an external edit costs one re-navigation.
  The file is documented UI-convenience state (with an ephemeral
  safe-mode variant); staleness machinery is disproportionate.
* **Storage GC post-commit unlink window**: a writer could reference a
  digest in the ~ms between the registry delete commit and the file
  unlink. Closing it fully needs a cross-statement lock the asset
  authority doesn't take; the pending-ledger design already covers the
  crash case, and a dangling row surfaces loudly via `missing_asset`.
* **Concurrent automatic-backup `run_due`** (periodic runner vs.
  pre-destructive trigger): both can pass evaluation and produce two
  generations. The outcome is a duplicate backup — never corruption —
  and state files are atomic last-writer-wins.
* **Two `import_as_copy` bundle imports** of the same bundle create two
  document copies — intended semantics, each in its own transaction.
* **Residual stat→replace window** on the JSON side-stores: refresh
  narrows silent loss to the milliseconds between signature check and
  `os.replace`; the single-instance guard means the only external
  writers are sync tools/manual edits, for which this is adequate.
* **8-second startup race** in `wait_for_existing_instance`
  (`__main__.py`, dev launcher only): a still-booting primary yields an
  honest exit-2 report rather than a silent second instance.
* **`activity_history.json`** is a GUI-thread snapshot of in-process
  state; external tampering only costs display history.

## Test additions

`backend/tests/test_round23_multi_instance.py` — 9 tests, all verified
to fail on the unpatched tree and pass after the fixes:

* `test_create_context_waits_for_external_writer`
* `test_create_context_concurrent_requests_get_distinct_revisions`
* `test_record_delivery_dedup_serializes_concurrent_insert`
* `test_failed_import_keeps_content_addressed_asset`
* `test_failed_import_never_removes_adopted_asset`
* `test_preferences_external_edit_survives_set`
* `test_preferences_external_delete_then_set_recreates`
* `test_preferences_external_corruption_refuses_and_preserves`
* `test_library_meta_external_edit_merged`
