# Round 13 — migration & upgrade truth

Scope: does the app honestly handle data created by older versions of
itself? Audited every persisted store: the legacy `htdt.sqlite3` store
(`SCHEMA_VERSION = 6`, retired — dev-only behind `HTDT_LEGACY_API`), the
native `cad-scenes.sqlite3` store (`NATIVE_SCHEMA_VERSION = 10`), managed
backups (`.htdt-backup`), exported project bundles (`.htdtproject`), and
analysis results keyed to algorithm constants. Verified in code and by
git archaeology on every released schema era; era-accurate DB fixtures
prove the migration paths that had no test coverage. Branch
`devin/rev13-migr`. Python 3.12.10 + pytest, `QT_QPA_PLATFORM=offscreen`.

## Verdict table — every schema transition

### Legacy store (`htdt/database.py`, `SCHEMA_VERSION = 6`)

The legacy browser store is retired (410'd behind `HTDT_LEGACY_API=1`)
but remains the upgrade path for pre-native data. Every transition has a
conditional migration **and** a real-fixture test.

| Transition | Migration exists | Real old-version fixture |
|---|---|---|
| v1 → v6 | `_initialise` conditional ALTERs (sessions, measurement columns, constraint_sets, search_specs) | `test_migration_guard.test_v1_open_creates_pre_migration_backup_before_upgrade` — real v1 DB + assets |
| v2 → v6 | same chain | `test_v2_open_adds_sessions_without_inventing_session_membership` |
| v3 → v6 | same chain | `test_v3_open_adds_empty_constraint_sets_table_after_pre_migration_backup` |
| v4 → v6 | same chain | `test_v4_open_adds_empty_search_specs_after_pre_migration_backup` + `test_v4_hashless_constraint_set_is_normalized_before_v5_integrity_check` |
| v5 → v6 | `datasets.dataset_sha256` backfill + auralization tables | `test_v5_open_backfills_dataset_sha256_after_pre_migration_backup` |
| > v6 (newer) | refused by `migration_guard` pre-open, no downgrade | `test_newer_schema_is_refused_without_downgrade` |
| migration failure | pre-migration zip backup → bounded rollback restore | `test_failed_migration_restores_v1_database_and_raw_assets` |

Every open is wrapped by `install_migration_guard`
(`htdt/__init__.py:14`): older schema → timestamped pre-migration zip in
`backups/`, post-migration verify, rollback on failure. VERIFIED.

### Native store (`htdt/cad_schema.py`, `NATIVE_SCHEMA_VERSION = 10`)

| Transition | What it does | Verified |
|---|---|---|
| unversioned → v1 | `_validate_legacy_database`: every present table must match `_LEGACY_TABLE_SIGNATURES` exactly (columns + FKs + unique sets + `optional_columns` for lazy ALTERs) + `integrity_check` + `foreign_key_check` | Signatures confirmed identical to the 20 tables creatable at `20a5f513~1` (last pre-versioning commit). Fixtures: `test_cad_schema.py` adoption/rejection suite |
| v1 → v2 | `cad_adaptive_extended_observations`/`plans` + indexes | NEW: `test_v1_stamped_database_migrates_through_every_step` replays the whole chain on a v1-stamped DB built from the real signature set |
| v2 → v3 | `cad_raw_mesh_repair_bundles` + indexes | same chain test |
| v3 → v4 | `CONTENT_BLOB_DDL` (`htdt_content_blobs`) | same chain test |
| v4 → v5 | `scene_document_heads` + `detached`/`detached_reason` columns + `backfill_scene_document_heads` | NEW: `test_v4_stamped_revisions_backfill_heads_and_mark_branch_detached` — mainline chain becomes head, pre-#626 branch row marked detached |
| v5 → v6 | full `NATIVE_BASELINE_DDL` replay + `NATIVE_COLUMN_ENSURES` (11 lazy-column converges) + domain convergences (`capture_*`, `cad_adaptive_extended_repository`) | Git archaeology: all 336 v9-era lazily-created table bodies are column-identical to the 320 baseline CREATEs (0 shape diffs) — replay convergence is honest |
| v6 → v7 | **the only row-moving migration**: `project_registry` → `htdt_project_documents` (status → `archived`, clone lineage preserved, `WHERE NOT EXISTS` = canonical store wins collisions), `project_tombstones` → `htdt_project_tombstones` (column-identical `SELECT *`), then DROPs both + `updated_at_utc` backfill | NEW: `test_v6_project_registry_rows_fold_into_htdt_authority` — real registry/tombstone rows folded, era tables dropped, canonical row survives a collision |
| v7 → v8 | baseline replay (validation-corpus + acquisition registry tables) | chain test |
| v8 → v9 | baseline replay (field-explorer session table) | chain test |
| v9 → v10 | baseline replay + column ensures + heads backfill (#767 took ownership of all repository-local CREATE) | NEW: `test_v9_stamped_table_missing_ensured_column_is_converged` — era table missing `editor_view_states.snap_json` gains it, rows preserved |
| > v10 | `NativeSchemaError` fail-closed, DB untouched | existing `test_newer_native_schema_is_rejected_fail_closed` |

### Registry coverage (`NATIVE_SCHEMA_TABLES`, 347 names)

For **every released era** (v4: 130 tables, v5: 153, v6: 275, v7: 279,
v8: 337, v9: 338) every `CREATE TABLE` name present in the tree is either
in the registry, a legacy-store table (different file), the folded
`project_registry`/`project_tombstones` pair, or parse noise. No table
any released build could create is unregistered — `native_row_integrity`
+ `native_authority_audit` protect all of them. The only tables that
ever vanished (`project_registry`, `project_tombstones`) are folded at
v6 → v7, not orphaned. VERIFIED.

### Backups across versions (`native_backup.py`, `data_management.py`)

- Probe validation runs `ensure_native_schema` + `SceneRepository` on a
  staged copy before the live cutover — the archive's stored
  `native_schema_version` drives `native_schema_compatibility`.
- Older-schema archive → restored bytes land, then
  `execute_native_upgrade` runs the journaled upgrade lifecycle
  (recovery snapshot, migration, verification, `UpgradeEvent`).
- `incompatible_newer` archive → rejected at preview validation before
  any mutation; the preview UI shows the archive's version with JP
  compatibility text (`このアプリでは非対応 — 対応はv{supported}まで`).
- Errors surface as `failure.message_ja` through the
  `user_facing_error` mapper. VERIFIED.

### Exported bundles (`project_bundle.py`)

`schema` + `schema_version` are pinned (`htdt.project-bundle`/`1.0.0`),
manifest/rows/assets are sha256-verified, and all writes commit in one
transaction. **Two honesty gaps found and fixed — see findings 1–3.**

### Persisted analysis results

- `IR_ANALYSIS_ALGORITHM_SHA256`: `IRAnalysisResult.valid_result` raises
  `ValueError` on a stale algorithm hash — honest rejection, never
  silent reuse. There is no migration note marking what happens to
  persisted rows when the constant legitimately changes; they become
  permanently unreadable. `CadIRAnalysisRepository` is currently used
  only in tests — see Deferred.
- `FIR_MATERIALIZATION_AUTHORITY_VERSION` is persisted as provenance
  (`source_version`/`evaluation_version`), not a read gate — stale
  values never block reads. VERIFIED.

## Findings

### 1. FIXED — bundle import crashed on a table this schema lacks (older-build bundles)

A `.htdtproject` exported by a build whose schema still carried
`project_registry`/`project_tombstones` (pre-v7) — or any table since
removed — produced `sqlite3.OperationalError: near ")": syntax error`
mid-import (proved with a crafted era bundle; the transaction rolled
back, so the outcome was safe but the error was inscrutable and told the
user nothing about version mismatch).

`import_project_bundle` now validates every manifest table and every
serialized column against the target schema before writing and raises
`BundleManifestInvalidError` naming the offending table/column and the
exporting build (`source HTDT {source_htdt_version}`).

### 2. FIXED — bundle import silently dropped unknown columns (newer-build bundles)

The row-prep filter `if column in columns_info` silently discarded any
column the target schema lacked. A bundle written by a *newer* build
(extra columns) imported "successfully" while silently losing the data
those columns carried — on hash-bearing rows this severs provenance
(proved: import of a bundle with an added `scene_revisions` column
succeeded, column discarded). The import now rejects such bundles
fail-closed (same `BundleManifestInvalidError` path as finding 1), so
schema drift is an explicit version-mismatch error, never silent data
loss.

### 3. FIXED — dead-end "import as copy" offer for non-collision failures

`_import_project_bundle` offered "コピーとして新しいプロジェクトを作成
しますか？" for *any* `ProjectBundleError` — but copy mode only resolves
`BundleImportConflictError` (record-identity collision). For manifest,
hash, or the new schema-mismatch rejections the retry is guaranteed to
fail identically — a dead end the user can click but never succeed at.
The offer is now scoped to `BundleImportConflictError`; other bundle
rejections go straight to `warn_user`.

### 4. FIXED — `upgrade_copy_ja` returned English inside a JP dialog

`NativeUpgradePlan.upgrade_copy_ja` — the property literally named
`*_ja`, rendered in the "HTDT データ更新" pre-upgrade dialog — returned
"HTDT needs to update project data from format…". Now returns Japanese
matching the sibling success notice.

### 5. FIXED — launch-failure dialogs showed English exception text (twice)

- `IncompatibleNewerSchemaError` handler passed `str(exc)` as **both**
  `reason` and `recovery` — the same English blob rendered twice in the
  JP dialog. The exception now carries `stored_schema_version` /
  `supported_schema_version`, and `newer_schema_dialog_copy_ja(exc)`
  composes JP reason + recovery (install the creating build / restore a
  compatible backup / no downgrade — data unmodified).
- `NativeUpgradeError` handler passed `recovery=str(exc)` — English.
  `upgrade_failure_recovery_ja(exc)` now returns JP copy, and for the
  `NativeUpgradeQuarantineError` subclass names the honest path forward
  that `recovery_choices` described but never surfaced: restart
  re-verifies automatically; otherwise restore the pre-upgrade recovery
  copy from データ管理 or share diagnostics.

### 6. FIXED (coverage) — no era-accurate fixtures for stamped intermediate versions

Existing tests covered fresh→v10, v0 adoption, >v10 rejection, and
re-stamping a *current* database — which never exercises migrations
against era-accurate table shapes (a re-stamped v10 DB already has every
table; the `project_registry` fold's guard was never true in tests).
Added four fixtures on real era shapes (v1 signature set + stamp, v4
lineage rows, v6 pre-fold registry tables with rows, v9 table missing an
ensured column).

## Deferred / observations

- **IR algorithm-keyed results**: `IR_ANALYSIS_ALGORITHM_SHA256` is a
  content hash of the algorithm identity; bumping the version string
  orphans every persisted `cad_ir_analysis_results` row — reads then
  raise `ValueError('IR analysis algorithm hash mismatch')` forever with
  no stale-marking, regeneration, or migration note. Honest (fails
  closed) but terminal. Recommend documenting an algorithm-bump
  protocol; low priority since `CadIRAnalysisRepository` is only used in
  tests today.
- **Baseline replay assumes indexed columns exist**: a persisted table
  that lacks a column indexed by a baseline `CREATE INDEX` aborts the
  migration with a wrapped `OperationalError` (still a `NativeSchemaError`
  — fails closed honestly). Verified no released era can produce this:
  lazily-created table shapes were column-identical to baseline at every
  era, and the 11 known column additions are all in
  `NATIVE_COLUMN_ENSURES`. Noted as a hardening opportunity only
  (column-ensure before index replay, or a friendlier error).
- **`_migrate_6_to_7` collision policy is canonical-wins**: a
  `project_registry` row colliding with an existing
  `htdt_project_documents` row is skipped and dropped with its source
  table; only the registry metadata is lost (document data remains in
  `scene_revisions`). Test now locks this in as deliberate.

## Files changed

- `backend/src/htdt/native_upgrade.py` — JP `upgrade_copy_ja`;
  `IncompatibleNewerSchemaError` version attributes;
  `newer_schema_dialog_copy_ja` + `upgrade_failure_recovery_ja`.
- `backend/src/htdt/native_cad.py` — launch-failure handlers use the JP
  copy helpers instead of `str(exc)`.
- `backend/src/htdt/project_bundle.py` — explicit schema-drift rejection
  (unknown tables/columns) before row preparation.
- `backend/src/htdt/workflow_application.py` — copy-retry offer scoped
  to `BundleImportConflictError`.
- `backend/tests/test_cad_schema.py` — 4 era-accurate stamped fixtures.
- `backend/tests/test_native_upgrade.py` — JP copy + version-attribute
  regression tests.
- `backend/tests/test_project_bundle.py` — unknown-table and
  unknown-column rejection tests.
