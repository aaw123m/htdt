# Round 14 — store / repository layer truth

Scope: the two SQLite stores (`htdt.sqlite3` via `Store` in `database.py`, `cad-scenes.sqlite3`
via `SceneRepository` + the `cad_*` repositories in `cad_schema.py`), `migration_guard`,
`native_backup` (backup/restore), `project_bundle` import, `capture_retention` purges,
`native_upgrade`, `native_authority_audit` / `authority_revalidation` readers, and
`user_facing_error._map_exception` (where store failures become operator text). Method: real
fault injection — `os._exit` mid-transaction, a second process holding `BEGIN IMMEDIATE`,
byte corruption of hash-covered and uncovered columns, hand-edited pointers, mid-DDL kill —
under `QT_QPA_PLATFORM=offscreen`, Python 3.12, pytest `-n 4`. Branch `devin/rev14-stor`.

## Store contract as verified

| Mechanism | Injected fault | Observed behavior | Verdict |
|---|---|---|---|
| `SceneRepository._save_in_transaction` (`BEGIN IMMEDIATE` → head CAS → revision upsert → snapshot delete) | `os._exit(1)` between revision insert and head upsert in a child process | Reopen shows the pre-save head and `PRAGMA foreign_key_check` clean; the half-written revision never became visible | Honest |
| Same, no-op path | Re-save identical scene content | Second save returns `created=False`, no new revision row — the content-hash no-op path is real, not a claimed one | Honest |
| Two-process contention | Process B opens the same `cad-scenes.sqlite3` and runs `BEGIN IMMEDIATE` while A holds a write txn | B's `sqlite3.connect(timeout=5)` waits, then raises `OperationalError: database is locked` (~5.5 s) — a real wait + honest failure, no silent interleave | Honest behavior, **error text dishonest** (see fix 2) |
| Journal mode | `PRAGMA journal_mode` on both stores | `delete` — rollback journal, matching what the code comments describe; no WAL claim anywhere | Honest (no claim violated) |
| `native_backup._snapshot_database` | Read snapshot source mid-`INSERT` burst from a second connection | Uses `sqlite3.Connection.backup()` — the online backup API takes a consistent snapshot; a torn page-level copy is impossible | Honest |
| Restore staging (`native_backup`) | — | Restored file is journal-recovered, fsynced, then `_assert_staged_database_openable` replays `SceneRepository` open + integrity on the *clone* before swap — restore never hands back a file that can't open | Honest |
| Row hash coverage (`native_row_integrity`) | Byte-flip `content_json` and `document_id` on a `scene_revisions` row | Load raises the hash-mismatch `ValueError`; the scan reports the row — tampered rows cannot be read as healthy | Honest |
| Row-only columns (`created_at_utc`, `detached`) | Bit-flip on both | Not detected — these columns are intentionally unbound from `_ROW_BINDINGS` (per code comment) | **Deferred limitation** (documented, not silently wrong) |
| `scene_document_heads` ghost pointer | `UPDATE heads SET head_revision_id='ghost'` | `current_head` reads `None` — the app falls back, but nothing flags the dangling pointer; `foreign_key_check` would catch it and isn't run on this path | **Deferred** |
| `Store._initialise` hash backfills (legacy `htdt.sqlite3` v5→v6) | Legacy DB + `ALTER` committed + backfill UPDATEs rolled back (the real kill window — DDL autocommits outside the txn) | **Before fix**: reopen sees columns present → backfill skipped → every row flagged `*_hash_mismatch`/`missing_hash` forever and `get_frequency_response` raised `DatasetIntegrityError`. All repairable rows were permanently condemned. | **Fixed**: backfills now run as `UPDATE ... WHERE <col> IS NULL` unconditionally after the ALTER gate — crash-safe and idempotent |
| `integrity_problems()` sweep | Corrupt `metadata_json`, `spec_json`, `quality_reasons_json` | Detected correctly each time (`dataset_hash_mismatch`, `spec` hash mismatch) — the auditor itself is honest | Honest |
| `list_measurements` / `get_dataset_descriptor` / `list_constraint_sets` / `get_constraint_set` / `list_search_specs` / `get_search_spec` | One corrupt `metadata_json`/`spec_json`/`quality_reasons_json` cell | **Before fix**: `JSONDecodeError` propagated — one bad row bricked the entire listing, hiding all healthy rows while `integrity_problems` knew exactly which row was bad | **Fixed**: `_loads_or_none` per row; the row is returned with `integrity_valid=False` and parsed fields `None` — visible and flagged instead of aborting the whole view |
| `user_facing_error._map_exception` on real sqlite errors | Contended write (`SQLITE_BUSY`), garbage file (`SQLITE_NOTADB`), `max_page_count` overflow (`SQLITE_FULL`) | **Before fix**: all three mapped to `operation.failed`「操作を完了できませんでした」non-retryable — 'database is locked' reached the operator as a generic failure | **Fixed**: `_map_exception` maps `sqlite_errorcode & 0xFF`: BUSY/LOCKED → `storage.locked`「データベースが他の処理によって使用されています」(retryable), FULL → `io.no_space`, CORRUPT/NOTADB → `storage.corrupt` + 「最新のバックアップを復元してください」, READONLY → `io.permission`, IOERR → `io.error`, CANTOPEN → `storage.cantopen`, else `storage.error` |
| `capture_ingestion_transaction` convergence commits | Mid-outer-txn commits inside `_repoint_lineage_parent`/`_migrate_run_identity` | Deliberate + idempotent: each commit persists a monotonic convergence step; re-running is a no-op | Honest by design |
| `project_bundle` import | — | All imported rows commit inside one txn with `verify_native_row_integrity(connection)` run *before* commit | Honest |
| `capture_retention.purge_capture_revision` | — | `BEGIN IMMEDIATE`, plan recomputed inside the txn, `PRAGMA foreign_key_check` before commit — a purge can't leave dangling refs | Honest |
| `native_upgrade` quarantine | — | Marker written before mutation; recovery copy exists; post-commit verification states enumerated | Honest |
| `audit_native_authority_graph` (revalidation/audit reader) | — | Opens via `connect_sqlite`, probes `_table_exists` + required columns before replay (schema guards honored on the read path), closes in `finally` | Honest |
| `authority_revalidation` connection lifecycle | — | `with ctx.connect() as connection:` relies on context-manager close semantics; CPython refcount disposes the connection, but the handle is technically GC-deferred | Cosmetic note, not a leak (no accumulating handles observed) |
| Windows `-journal` residue | Hard-kill + rollback | Stale `-journal` file persisted on disk after successful rollback (Windows fs semantics); contents already rolled back, next open handles it | Cosmetic platform artifact, documented |

## What "dishonest" looked like, concretely

- **Convergence lie**: `ALTER TABLE … ADD COLUMN` autocommits even inside `db.executescript`
  batches, but the population `UPDATE`s ran inside the same branch *and* the same transaction.
  A kill between the two committed the column and rolled back the fill; the next open saw the
  column present and skipped the fill entirely — the DB then reported every legacy row as
  corrupt (`*_hash_mismatch`) when nothing was wrong with the data. The fix keys the fill on
  `IS NULL`, so any crash leaves a resumable state.
- **Availability lie**: the integrity sweep knew which row was corrupt, but the listing reader
  raised `JSONDecodeError` on the first bad row — the user lost the whole list to protect them
  from one flagged row. Flagged-but-readable is the honest middle ground already used for
  hash mismatches (`integrity_valid=False`).
- **Cause lie**: three distinct storage realities — another process holds the lock, the file
  isn't a database at all, the disk is full — all surfaced as the same generic
  「操作を完了できませんでした」 with no retry hint. Lock contention in particular is the
  routine case for a two-instance app and is now retryable + named.

## Deferred (documented, not fixed)

- Row-only columns (`created_at_utc`, `detached`, ordering/auxiliary fields) are not covered by
  `_ROW_BINDINGS` — an intentional bound noted in code. Widening coverage changes hash
  semantics for every existing row; out of small-diff scope.
- `scene_document_heads.head_revision_id` dangling → `current_head=None` is a silent fallback;
  a `foreign_key_check` on the read path would surface it. Left as follow-up (touching the
  head read path has wider blast radius than this round's diff budget).
- `authority_revalidation` GC-deferred connection close — cosmetic; no leaked handles
  accumulate in practice.

## Files changed

- `backend/src/htdt/database.py` — convergent NULL-marker backfills in `_initialise`;
  `_loads_or_none` + per-row `integrity_valid` degradation on the six hash-covered listing
  readers.
- `backend/src/htdt/user_facing_error.py` — `sqlite3.Error` branch keyed on
  `sqlite_errorcode & 0xFF`; `storage.locked`/`storage.corrupt`/`storage.cantopen`/
  `storage.error` codes; `storage.locked` added to `RETRYABLE_ERROR_CODES`.
- `backend/tests/test_round14_stor.py` — fault-injection regressions: crashed-upgrade
  convergence through the real `migration_guard` path, per-row corrupt-JSON flagging on
  every fixed reader, real `BUSY`/`NOTADB`/`FULL`/generic sqlite errors captured from live
  connections and asserted against their mapped JP messages.
