# Round 14 — self-diagnostics / system-report truth

Scope: `run_health_checks` (`support_diagnostics.py`), the support-bundle builder
(`DiagnosticPackageBuilder` / `_staged_zip_archive`), `environment_summary`, the
previous-session-end probe (`previous_session_unexpected_end` /
`runtime_instance.read_runtime_info`), the startup-recovery ledger
(`startup_recovery.record_launch` / `complete_launch` / `RecoveryMetadata.last_failure_class`
/ `classify_startup_failure`), and the launch bookkeeping call sites in
`native_cad._run_gui`. Method: real fault injection — a store written by a newer build,
an alien sqlite, byte-corrupt and exclusively-locked databases, a missing data
directory, a directory where the database file should be, non-dict `runtime.json`,
a cross-build launch history, and an unwritable diagnostics dir — under
`QT_QPA_PLATFORM=offscreen`, Python 3.12, pytest `-n 4`. Branch `devin/rev14-doctor`.

## Diagnostic contract as verified

| Surface | Injected fault | Observed behavior | Verdict |
|---|---|---|---|
| `storage.database_openable` + `storage.sqlite_quick_check` | Random bytes as `cad-scenes.sqlite3` | Both FAIL, `file is not a database` | Honest |
| Same | `BEGIN EXCLUSIVE` write lock held by this process | Both FAIL, `database is locked` | Honest |
| Same | `cad-scenes.sqlite3` is a *directory* | Both FAIL, `unable to open database file` | Honest |
| `storage.schema_compatibility` (new) | `native_schema_metadata.schema_version=999` | **Before fix**: no such check — opens + quick_check PASSed, so a store the app refuses to load reported `storage: pass`. **After**: FAIL `project database schema is not supported by this build` + restore/install recovery hint | **Fixed** |
| Same | `schema_version=3` (migration-required) | **Before fix**: PASS (silent). **After**: ATTENTION `schema v3 predates this build` + migrates-on-next-open detail | **Fixed** |
| Same | Valid sqlite with unrelated tables (alien store) | **Before fix**: PASS. **After**: FAIL with the `NativeSchemaError` reason (refuses adoption) | **Fixed** |
| Same | Zero-byte store | ATTENTION `empty or pre-versioning` — unusual but adoptable, correctly not FAIL | Honest |
| `storage.data_dir_lock` | Foreign `SingleInstanceGuard` held | ATTENTION with owner metadata | Honest |
| Same | Stale `runtime.lock` metadata, no OS lock | PASS + 'stale' evidence | Honest |
| `storage.disk_space` | Data dir does not exist yet | **Before fix**: ATTENTION `disk space check unavailable` — reported not-measurable, dragging overall to attention on a healthy first-run layout. **After**: measures nearest existing ancestor, PASS with `measured on <ancestor>` detail | **Fixed** |
| `previous_session_unexpected_end` | `runtime.json` containing `[1,2,3]` / `5` / `"marker"` / `true` | **Before fix**: `AttributeError` on `payload.get` — the "was the last session clean?" diagnostic crashed *while deciding recovery*, i.e. the diagnostic itself could blank the recovery path. **After**: treated as no marker, `unexpected_end=False` | **Fixed** |
| `RecoveryMetadata.last_failure_class` | Build-A crash `failure_class=project_data`, then build-B crash unclassified | **Before fix**: returned `project_data` for build-B — a different build's class steered this launch's restore recommendation (cross-build misdiagnosis). **After**: `None`; class is scoped to the build's own streak, matching `repeated_startup_failures` | **Fixed** |
| `classify_startup_failure` | `'NativeSchemaError: ...schema v12 newer...'` etc. | `schema_incompatibility`/`renderer_initialization`/`migration_failure` classified correctly | Honest |
| `record_launch` in `_run_gui` | Diagnostics dir unwritable → `complete_launch`/`record_launch` raises | **Before fix**: the bookkeeping write propagated — a *diagnostic* failure converted into a launch failure, and worse, a `complete_launch` write failure on a clean close left the record unclosed → next launch reported a crash that never happened. **After**: logged, launch proceeds; record stays unclosed only when the write genuinely can't persist | **Fixed** |
| `run_health_checks` per-check isolation | Any built-in storage check raising (e.g. monkeypatched `sqlite3.connect`) | **Before fix**: one crashing check blanked the whole report (callers see nothing). **After**: same contract as probes/integrity_runner — a crashing check is itself a FAIL finding (`storage check crashed`), siblings still report | **Fixed** |
| `integrity_runner` / probes / `capability_inventory` exceptions | Raising runner/probe | Isolated FAIL entry, report completes | Honest (pre-existing) |
| Staleness | Corrupt the store *between* two `run_health_checks` calls | First run PASS → second run FAIL on all storage checks — live state, no cache | Honest |
| Healthy system false-positive audit | Fresh data dir + current v10 store + held own lock (`owns_lock=True`) | All PASS / NOT_APPLICABLE — no phantom faults | Honest |
| `environment_summary` / `schema_summary.json` | Store with `schema_version=999` present | **Before fix**: reported `schema_version: 6` — the *legacy* `htdt.sqlite3` constant, not the native store's authority (`cad_schema` v10) and not the stored version. Support would read 'schema 6' on every incident. **After**: `schema_version` + `supported_native_schema_version` = 10, `stored_native_schema_version` = live value, `native_schema_compatibility` = app's own verdict (`current`/`incompatible_newer`/`unreadable`/`no_store`) | **Fixed** |
| Support bundle completeness | Launch history exists on disk (`recovery-launch-metadata.json`) | **Before fix**: silently excluded — the bounded crash-history a real incident needs (launch mode, clean/unclean, failure_class, workspace, correlation ids) never reached support. **After**: `launch_metadata.json` member; `last_project_ref` redacted to `<set>` unless the `include_project_ids` opt-in is on; a torn file is `skipped` in the manifest, never an empty history | **Fixed** |
| Bundle atomicity | Build to target | `_staged_zip_archive` writes to a temp file then `os.replace` — torn bundles impossible; exclusion list still enforced (`MAX_LOG_BYTES` tail, credential-shaped keys `<excluded>`, paths `<set>`/`<unset>`) | Honest |
| Corrupt bundle member | `recovery-launch-metadata.json` truncated mid-array | Member marked `skipped` in `manifest.json` — the bundle still builds | Honest |
| `workflow_application._export_diagnostics_package` | — | Runs `run_health_checks` live (not cached), `owns_lock=True` matches the app's real guard, prefs snapshot redacted, `project_ids` opt-in | Honest |

## What "dishonest" looked like, concretely

- **Schema blind spot**: `opens + quick_check` proved the file is a well-formed
  sqlite — nothing checked whether *this build can use it*. A v999 store and an
  alien sqlite both reported `storage: pass` while every repository open would
  raise `NativeSchemaError`. The new check reuses
  `check_native_schema_compatibility` — the same read-path gate the repositories
  pay — so the diagnostic can never disagree with the app it describes.
- **Wrong schema authority in the bundle**: `schema_summary.json` hardcoded
  `database.SCHEMA_VERSION` (6, the legacy server store) as `schema_version`.
  The project's own store lives under `cad_schema.NATIVE_SCHEMA_VERSION` (10).
  Support reading the file would chase a version four revs stale.
- **Crash-the-diagnostic**: `read_runtime_info` validated JSON *parse* but not
  *shape*. A `[1,2,3]` marker — plausible after a hand-edit or partial restore —
  raised `AttributeError` inside `previous_session_unexpected_end`, i.e. the
  very check that decides whether to offer recovery could itself kill the
  launch.
- **Bookkeeping as a gate**: launch records are evidence, but a write failure
  propagated like a startup failure — a diagnostics-dir permission problem
  would have been reported to the user as "the app crashed", and a failed
  clean-close record would offer a false recovery next launch.
- **Cross-build blame**: `last_failure_class` walked the rolling window without
  the build boundary the rest of the recovery logic uses — the previous build's
  `project_data` crash could recommend a restore for a crash the new build
  never had.
- **"Unavailable" noise**: a not-yet-created data dir (fresh install, or a
  parent that exists) reported `disk space check unavailable` — an attention
  that says nothing about the disk's real free space, which was measurable all
  along via the nearest existing ancestor.

## Deferred (documented, not fixed)

- `cad_system_health` domain-health checks (baseline drift etc.) are a separate
  axis from app-storage diagnostics and were verified consistent, not merged
  into `run_health_checks` — combining axes is a product decision beyond small
  diffs.
- `storage.schema_compatibility` does not distinguish "empty file" from
  "pre-versioning HTDT v1 store" beyond the ATTENTION note — both adopt on next
  open, so the distinction has no operator action attached.
- The legacy `htdt.sqlite3` (server `Store`) has no equivalent schema gate in
  the health report; it is created by `Store._initialise` on demand and has no
  known incompatible-version path. Left as follow-up if a second store reader
  ever needs it.

## Files changed

- `backend/src/htdt/support_diagnostics.py` — `storage.schema_compatibility`
  check via the native schema authority; missing/open-fail database branches now
  emit all three storage results; `_check_disk_space` ancestor fallback;
  `schema_summary.json` carries supported + stored native schema and the
  compatibility verdict; `PackageCategory.LAUNCH_METADATA` +
  `_write_launch_metadata` (redacted, opt-in identity, skipped-on-corrupt);
  built-in storage checks isolated per-check like the probes.
- `backend/src/htdt/runtime_instance.py` — non-dict `runtime.json` payloads read
  as no marker instead of raising.
- `backend/src/htdt/startup_recovery.py` — `last_failure_class` scoped to the
  queried build's streak; `classify_startup_failure` precedence parenthesized.
- `backend/src/htdt/native_cad.py` — `record_launch`/`complete_launch` call
  sites guarded: bookkeeping failures log and continue instead of becoming
  reported launch failures.
- `backend/tests/test_support_diagnostics.py` — schema-compatibility matrix
  (current/newer/migration/alien/missing), non-dict runtime markers, ancestor
  disk measurement, launch-metadata bundle member (redaction, opt-in,
  corrupt-skip), native schema summary contents.
- `backend/tests/test_startup_recovery.py` — `last_failure_class` build
  scoping.
