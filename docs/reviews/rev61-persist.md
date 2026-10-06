# REV61-PERSIST — Deep review of the persistence/recovery/byte-integrity slice

Scope: durable state, crash recovery and byte integrity in
`backend/src/htdt/` on `3695e6f8` (latest `origin/main`) —
`data_relocation.py` journal/cutover machinery, `native_backup`,
`native_authority_audit` `_ASSET_TABLES`, `project_bundle`,
`managed_assets`, `storage_maintenance`, `migration_guard`,
`legacy_data`, `automatic_backup`/`managed_data_fingerprint`,
`startup_recovery`, `project_lifecycle`, `capture_retention`,
`capture_inbox`, `capture_ingestion_transaction` persistence legs,
`native_row_integrity`, plus `cad_*_repository.py` only for
persistence-level defects. Regression tests live in
`backend/tests/test_rev61_persist.py` (17 tests); every FIXED finding's
test was proven to fail on the unfixed code via `git stash`.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | `data_relocation._verify_staged_root` verified only 2 of the 8 `_ASSET_TABLES` managed-byte specs (`cad_measurement_assets`, `cad_quality_calibration_files`). A staged root whose `htdt_acceptance_evidence` registry rows pointed at absent/mismatched bytes, or whose digest-only rows (`cad_directivity_source_assets`, `cad_wave_excitation_source_assets`, `cad_projector_spec_source_assets`, managed `cad_equipment_evidence_authorities`, non-NULL `cad_treatment_evidence_authorities`) held no bytes at their content address, passed verification and was promoted to the live data root — the relocate cutover could install a corrupt generation the very next audit would then flag as store corruption | HIGH — staged promotion accepted dangling managed-byte claims; live-root integrity gap the authority audit enforces elsewhere was skipped at the one boundary that rebuilds the data dir | FIXED — `_verify_staged_root` now iterates `_ASSET_TABLES` (lazily imported like the existing audit import): `where` predicates (`col='lit'`, `col IS NOT NULL`, unknown → fail closed), `[0-9a-f]{64}` digest shape check, registry rows resolved through `managed_asset_path` containment, digest-only rows resolved to `measurement-assets/<sha>`; size checked when the spec declares it; sha always re-verified. Tests: missing registry/digest-only/malformed/unsafe/size-mismatch rows rejected, honest coverage incl. both predicate skips accepted |
| 2 | `CadDeviceSnapshotRepository` inserted sealed records without re-verifying them — all six save paths (`save_snapshot`, `save_baseline`, `save_firmware_transition`, `save_restore_record`, `save_backup_artifact`, `save_replacement_assessment`). A `model_copy(update={'snapshot_id':…,'snapshot_sha256':…})` forge landed a row whose persisted sha/id its payload never earned; every read path (`get_*`, `list_*`, `get_*_by_hash`) then raised `ValidationError`, permanently poisoning list/get for the whole document. The `snapshot_sha256 UNIQUE` column caught only the same-sha variant | MED — fail-open write boundary on sealed records; the repo convention requires save-time re-verification of `canonical_sha256(semantic_payload())` | FIXED — shared `_assert_sealed(record, sha_field, id_field)` re-derives the canonical digest and, for digest-derived ids, requires `<prefix>-<sha[:24]>`; raises `DeviceSnapshotIntegrityError`. `artifact_id` is caller-supplied (not digest-derived) so the artifact path checks sha only. Tests: forged sha+id, forged-id-only, forged baseline, forged artifact all rejected; legit saves + identical re-save unaffected |
| 3 | `CadProjectTemplateRepository.save_template` skipped seal re-verification (`template_sha256`) — a forged `model_copy` landed a row `get_template`/`list_templates` then rejected. The instantiation path already re-verified via `_validate_instantiation` | MED — same sealed-record class as #2 | FIXED — `save_template` raises `ValueError` when `template_sha256 != _hash(semantic_payload())`. Test: forged sha rejected, legit save + identical re-save unaffected |
| 4 | `CadProjectActivityNoteRepository.save_note` skipped seal re-verification (`note_sha256`); `_validate` only checked the document existed | MED — same sealed-record class | FIXED — `save_note` raises `ValueError` on a seal mismatch before the document check. Test: forged sha rejected |
| 5 | `AcceptanceRunRepository._insert_evidence` used bare `INSERT OR IGNORE` and no shape checks. `evidence_id` is caller-supplied (uuid, not content-derived), so a ref re-using an id with different bytes was silently masked — the run payload kept claiming the new digest while the registry row bound the old file, and refs with malformed `sha256`/unsafe `relative_path`/negative `size_bytes` landed in `htdt_acceptance_evidence`, an `_ASSET_TABLES` member whose rows must be claims bytes can satisfy | MED-LOW — registry row could diverge from the payload's claim; malformed rows poison the audit/export until flagged | FIXED — write-side shape assertions (`[0-9a-f]{64}` digest, `safe_managed_relative_path`, non-negative size → `NativeSchemaError`), and on an `OR IGNORE` no-op the stored row is compared to the claim: divergence raises instead of masking. Tests: same-id/different-bytes conflict, malformed digest, unsafe path, negative size rejected; row-identical re-save still tolerated |

## Verified-clean surfaces

| Surface | How verified |
|---------|--------------|
| Relocation journal phase machine + `_promote_directory`/`_park_directory`/`_locate_parked_source` (PREPARED→COMPLETED, diverted `<name>.<n>` siblings, digest-based parked-source resolution, rollback outcome reporting) | Code review of all three recovery call sites of `_verify_staged_root` (forward promote, rollback restore, re-drive) + REV54's `_locate_parked_source` digest pin; no remaining raw `os.replace` on dirs in the file |
| `legacy_data._archive_legacy_store` journal: `htdt-legacy-migration.journal` written (fsync'd atomic text) *before* the `os.replace` renames, unlinked only after; `inspect_legacy_store` reports 'interrupted' from the surviving journal and `migrate_legacy_data` refuses to proceed over one | Code review; ordering is journal-first, rename-second, unlink-last — the surviving journal is never cleared ahead of the operation it describes |
| `storage_maintenance.run_storage_gc` two-pass protocol: pass 1 re-derives `referenced_asset_digests` under `BEGIN IMMEDIATE`, inserts `htdt_storage_gc_pending` + deletes registry rows atomically; pass 2 re-proves under a fresh write lock, restores resurrected rows via `INSERT OR IGNORE INTO cad_measurement_assets(sha256,filename,relative_path,size_bytes)` (column set verified against the DDL), refuses unlink when the pending row vanished (db-swap detection) | Code review of both passes + DDL column check |
| `project_lifecycle.delete_project`: `PRAGMA foreign_keys=OFF` + `BEGIN IMMEDIATE`, `_build_deletion_plan` re-run inside the lock with `expected_plan.fingerprint()` staleness pin, `_owned_predicates` fixpoint over document_id+FK chase, unparseable payloads block deletion, tombstone insert + bound, `foreign_key_check` before commit | Code review |
| `migration_guard`: pre-migration backup via sqlite backup API + bounded zip staging + `os.link` atomic publish; post-check restore extracts members through `_validate_archive_member`/`_extract_member_bounded` with the manifest `schema_version` pin | Code review of `_restore_pre_migration_backup` member handling |
| `capture_retention.purge_capture_revision`: single `BEGIN IMMEDIATE`, plan recomputed inside the lock (absent/blocked aborts), dependent DELETEs across all link/registry/bookkeeping tables, blob GC only for unreferenced digests, `PRAGMA foreign_key_check` before commit, rollback on any error | Code review |
| `capture_inbox.stage`/`stage_rejected`/`_update_facets`: `BEGIN IMMEDIATE` + idempotent already-staged path + disposition-transition row committed in the same txn | Code review |
| `capture_ingestion_transaction.ingest`: payload byte/sha verification pre-commit, single `BEGIN IMMEDIATE` covering revision registration, manifest persistence, run insert, link/upsert writes; the idempotent-replay branch re-verifies full materialization and commits its repairs; conflict outcome persisted in its own committed txn | Code review of ingest/verify paths + `_migrate_run_identity` (RENAME/CREATE/DROP inside `BEGIN IMMEDIATE`, link-table rebind join to the unique run) |
| `native_row_integrity.scan_native_row_integrity`: per-table binding checks + ledger payload checks + registry-completeness drift over the live schema (a declared payload table in neither registry is itself drift) | Code review |
| `automatic_backup.managed_data_fingerprint`: db size/mtime + header change counter + `-wal/-shm/-journal` sidecars + flat managed-asset signature + per-component signatures where directory components fold member name/size/mtime (REV53-PASS4 fix intact — rename-only changes are detected via the member path) | Code review; stat-level contract is documented and every current component is covered |
| `project_bundle` export/import `_ASSET_TABLES` closure (REV52): export enforces `relative_path == measurement-assets/<digest>` + digest shape on registry rows and ships bytes at content addresses; import re-proves the manifest agreement before any install | Code review of the export closure + import check — `_ASSET_TABLES` iterated on both sides |
| `installation_handoff` multi-file promotion: members staged+digest-verified, promoted members-first/manifest-last with full rollback on error — a crash mid-promotion leaves old manifest + partial members → manifest digests fail on next read (fail closed) | Code review of the swap ordering |
| `launch_intents` queue: tmp+`os.replace` publish, drain keeps valid files until `complete_queued_intent` retires them (crash-safe redelivery), malformed/expired → `dead/` file-level moves | Code review |
| `startup_recovery`: read-only launch-decision authority over a rolling-10 recovery ledger; no filesystem mutation on the decision path | Code review |
| `data_management` facade: restore re-validates the archive against the preview manifest, pins `native_schema_compatibility` before cutover, journaled `on_commit_point` marks the cancel boundary | Code review |

## Residual notes (not defects, recorded for the next pass)

- **`migration_guard` rollback assets swap is unjournaled**:
  `_restore_pre_migration_backup` does `shutil.rmtree(assets_dir)` then
  `replacement_assets.rename(assets_dir)` — a crash between leaves the
  data dir with no assets. The generation zip survives so data is
  recoverable, but the restore path itself has a torn-write window.
  Deliberate simplicity (the whole path only runs when the guarded
  migration already failed); recorded for the next pass, not fixed —
  a journaled assets swap would be new recovery machinery, not a bug
  fix.
- **`managed_data_fingerprint` is blind to empty-dir create/remove**
  inside a directory aux component (files only) — consistent with the
  stat-level contract: empty dirs carry no managed bytes.
- **Sealed-record write re-verification is a wider pattern than this
  slice**: ~40 mass-generated `cad_*_repository.py` files (~60 save
  methods) insert sealed models without a save-path revalidation — e.g.
  `cad_design_checkpoint_repository.save_snapshot`,
  `cad_system_health_repository.save_*`,
  `cad_external_standards_repository.save_*`,
  `cad_measurement_quality_repository.save_*`,
  `cad_operating_preset_repository.save_*`. Same defect class as
  findings 2-4 but on domain-authority repos the AUTHORITY sibling owns;
  the full method list was produced by a scripted scan and should be
  handed to that slice rather than fixed piecemeal here.
- **Same-id/different-sha `DeviceSnapshotConflictError` is now
  unreachable by construction** (the id derives from the sha it would
  diverge from) — the pre-existing `test_cfg14` forge now raises
  `DeviceSnapshotIntegrityError` at the seal boundary; assertion updated
  with the rationale, matching the append-only convention.
- **`_verify_staged_root` error text names the resolved staged path**,
  not the `..` escape attempt — containment failure still fails closed
  with `ManagedAssetError` text preserved.

## Test evidence

`backend/tests/test_rev61_persist.py` — 17 tests, all green; the 13
behavioral regression tests verified to FAIL on the unfixed code
(`git stash` on `backend/src`, run, `stash pop`): the 3 staged-root
coverage rejections, the 6 seal-rejection tests, the 4 evidence
conflict/shape rejections. The remaining 4 are contract tests that also
pass on unfixed code (honest-coverage positive, size mismatch, unsafe
path which raised under the old code for a different reason, identical
re-save tolerance).

Scoped suite run green (`-n 0`):

- `test_rev61_persist.py` — 17
- `test_rev56_snapstd.py` — 52 (1 assertion updated, see residual notes)
- `test_cad_acceptance.py` — ~38
- `test_cad_project_template.py` — ~15
- `test_issue_864_template_creation_integrity.py`
- `test_cad_project_activity.py`
- `test_data_relocation.py` + `test_data_relocation_cutover.py`
- `test_rev53_pass4.py`, `test_rev54_pass5.py`
- `test_persisted_data.py`, `test_data_management_ui.py`,
  `test_cad_device_adapter.py`, `test_cad_device_compatibility.py`,
  `test_activity_center.py`
