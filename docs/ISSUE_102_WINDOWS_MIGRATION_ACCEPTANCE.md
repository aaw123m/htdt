# Issue #102 — Windows PC migration acceptance

Date: 2026-09-19

This acceptance closes the remaining software-verifiable migration gate without RDC by
running the **packaged Windows executable** on `windows-latest`.

## Scenario

The workflow simulates two independent PCs with two independent data roots:

1. **Old PC**
   - start with an empty old-PC data root
   - run packaged `HTDT.exe --seed-synthetic-demo`
   - this creates persisted SceneRevision, measurement records/assets, O10 search,
     O60 validation, O70 adaptive plans, O80 extended search and O80A records
   - snapshot the logical content of every SQLite user table
   - snapshot SHA-256 for every managed measurement asset
2. **Transfer**
   - run packaged `HTDT.exe --backup <archive>`
   - the archive is produced by the existing `native_backup.create_backup()` authority
3. **New PC**
   - assert the new-PC data root does not exist
   - run packaged `HTDT.exe --restore <archive>` into that fresh root
   - because the destination is fresh, no pre-restore backup should be created
4. **Acceptance**
   - SQLite `integrity_check` is `ok`
   - SQLite `foreign_key_check` is empty
   - every user table has identical columns and logical rows after restore
   - a canonical logical-database SHA-256 matches old PC vs new PC
   - all measurement asset relative paths and content SHA-256 values match
   - the synthetic project is queryable in the restored `scene_revisions`
   - key persisted authorities are explicitly required to be non-empty:
     - `scene_revisions`
     - `cad_measurement_assets`
     - `cad_measurements`
     - `cad_frequency_responses`
     - `cad_search_specs`
     - `cad_model_validations`
     - `cad_adaptive_plans`
     - `cad_extended_search_specs`
     - `cad_adaptive_extended_plans`

The test compares logical database content rather than the raw SQLite file bytes because
`native_backup` intentionally uses SQLite's online backup API to create a consistent
snapshot; physical page layout is not an application-data contract.

## Harness

`scripts/validate_backup_pc_migration_windows.py`

The harness imports only the Python standard library. It does **not** import HTDT source
modules. All product operations are exercised through the packaged `HTDT.exe`, so this
is package-level acceptance rather than an in-process unit test.

The harness writes:

`pc-migration-report.json`

with the database logical digest, backup size, measurement asset count, required table
row counts, and packaged backup/restore command output.

## CI integration

`.github/workflows/windows-release.yml` runs the harness after the locked native package
is built and before installer creation. A successful run uploads:

`HTDT-windows-pc-migration-acceptance`

containing the JSON evidence report.

This complements the existing tests for:

- archive manifest/hash validation
- path traversal / decompression bounds
- SQLite integrity / FK validation
- automatic pre-restore backup
- rollback on failed live swap
- stale restore-preview rejection
- application-level release/reopen lifecycle
- installer preserving user data on uninstall

## Acceptance interpretation

Issue #102 acceptance criterion “旧PC → backup → 新PC → restore を実機または同等のWindows
acceptanceで検証する” is satisfied when the Windows Release Artifact workflow containing
this harness passes on the merged implementation.

The final UI/first-use visual acceptance remains part of Issue #118 UX160 and is not
redefined by this data-integrity acceptance.

RDC is not used.
