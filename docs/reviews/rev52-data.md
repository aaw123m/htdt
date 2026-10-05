# REV52-DATA — Data-integrity paths: backup/restore, bundle, relocation, upgrade

Scope: the failure-critical persistence paths — `native_backup.py` (manifest,
15-minute always-on evaluation, restore completeness, schema-version mismatch,
partial corruption), `project_bundle.py` export/import authority preservation,
`data_relocation.py` journal recovery, `native_upgrade.py` upgrade-state
resume/rollback and `_MIGRATIONS` chain, SQLite mid-state/WAL resilience
(`integrity_error` fail-closed), and the effectiveness of the audit
completeness mechanism (`_ReplayProbe`/`_ASSET_TABLES`/`_TABLE_POLICY` —
verified by actually planting an unregistered table). Everything below was
verified in real code; corrupted/interrupted states were fabricated where
possible. Branch per merge-test convention.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | Relocation cutover `os.replace(staged, destination)` fails on Windows when the destination is an existing *empty* directory (WinError 5) — a state the planner explicitly allows; recovery retried the identical rename on every startup → permanent brick loop | HIGH availability/data-loss-adjacent | FIXED — `_promote_directory` removes an existing-empty destination before the rename (execute path + journal recovery path); non-empty destinations still fail honestly |
| 2 | `os.replace(source, parked)` in both execute and recovery paths could collide with a leftover/foreign occupant at the park slot — on Windows a non-empty park target also fails, again a recovery-time brick | MED availability | FIXED — `_park_directory` reclaims empty placeholders and diverts populated ones to `<parked>.<n>`, preserving both generations; journaled `parked_dir` updated with the actual name |
| 3 | Bundle asset closure covered only 2 of the audit's 8 `_ASSET_TABLES` specs — a pulled `htdt_acceptance_evidence` row (same sha256/relative_path/size_bytes shape) exported *without* its managed file → row dangles on import into a foreign environment. Import likewise checked only the 2 registry tables | HIGH silent data loss | FIXED — export asset closure + import asset check now iterate `_ASSET_TABLES` itself (single source of truth, can never drift); rows declaring size+path require a shipped member, digest-only rows accept locally retained verified bytes, declared `relative_path` must resolve inside the store; unsafe paths fail closed via `managed_asset_path` |
| 4 | `managed_data_fingerprint` ignored `backup_included_components()` — an aux-only edit (e.g. `commissioning-plans.json`) never invalidated the fingerprint, so the 15-minute periodic evaluation could skip backing up real managed-state changes | MED silent data loss (windowed) | FIXED — fingerprint now folds each included aux component's size+mtime |
| 5 | `_rollback_restore_swap` adopted whatever occupied the canonical rollback slot as the pre-restore generation. With no pre-restore database (restore onto a fresh root), a post-crash occupant could claim the slot, and an honestly-empty live root hit `no restorable database remains` → brick loop on every launch | HIGH availability | FIXED — the restore journal now records `pre_restore_live` (which live objects the swap evacuated) before the first move; journaled rollback honors it: empty-truth settles to empty, a post-crash occupant that can't serve as live is parked back rather than blessed. Orphan/old journals keep the strict behavior |
| 6 | Audit completeness mechanism — does `_TABLE_POLICY`/`audit_table_modes`/`_ASSET_TABLES` really catch a new unregistered table? | — | VERIFIED — planted `unregistered_rev52_probe` table → `coverage_gap` diagnostic + `unclassified_tables` + `AuthorityAuditError` from `assert_native_authority_graph`, and `create_backup` refuses **even with `allow_stale=True`** (the never-degradable path at `native_backup.py:687`). `test_registry_covers_every_persisted_table` additionally greps every `CREATE TABLE IF NOT EXISTS` in the codebase against the registry |
| 7 | Backup manifest ↔ member ↔ staged-database pipeline | — | VERIFIED — member set equality, per-member sha256/size verify, staged-db openability proof (clone → `ensure_native_schema` → `SceneRepository`), manifest↔staged schema equality, `_validate_asset_contract`, declared-stale SUBSET tolerance |
| 8 | Partially corrupt backup handling | — | VERIFIED — BadZipFile→`BackupError`, truncated members→size/sha mismatch at `_stage_backup`, newer schema→rejected at `_sqlite_health`/`check_native_schema_compatibility`, older schema→lands then routes through `execute_native_upgrade` |
| 9 | Restore swap journal design | — | VERIFIED — journal written before any live byte moves; recovery chain complete→rollback→pre-restore-archive; `recover_interrupted_restore` runs journal recovery before the residue sweep (60s floor) so stage dirs can't be swept before their journal settles; legacy-archive `.installed` markers rollback cleanly |
| 10 | `_MIGRATIONS` chain + upgrade-state resume | — | VERIFIED — `UpgradeStateRecord` at `.native-upgrade-state.json`; `QUARANTINED_UPGRADE_STATES={migrating, committed_pending_verification, failed_after_commit}`; unreadable marker on current-schema startup → `NativeUpgradeQuarantineError`; mid-migration failure leaves a quarantined, non-bricked root (resume via `execute_native_upgrade`) |
| 11 | SQLite mid-state / `integrity_error` fail-closed | — | VERIFIED — `_sqlite_health` requires `integrity_check`=='ok' at stage + post-swap validation; corrupt db fails closed before any swap; WAL/journal side files covered by the backup stat pass and the fingerprint; `connect_sqlite` WAL recovery is sqlite-internal ACID |

## 1 — Relocation: `os.replace` onto an existing directory (fixed)

`plan_data_relocation` explicitly permits an existing **empty** destination
(`destination_not_empty` blocker only fires when `any(destination.iterdir())`,
message "choose an empty destination"). But `execute_data_relocation`'s cutover
and `_recover_journal_locked`'s promotion both called raw
`os.replace(staged, destination)` — verified on this box that WinError 5
fires even for an empty target. A crash (or the pre-created-directory user
flow) after `STAGED_VERIFIED` made the journal's recovery retry the identical
failing rename at every startup: the data dir stayed unavailable forever.

Fix: `_promote_directory` removes an existing-empty destination before the
rename on both paths; a non-empty occupant still fails honestly rather than
merging into foreign content. The source park (`os.replace(source, parked)`)
gets `_park_directory`: an empty placeholder is reclaimed, a populated
occupant diverts the park to `<parked>.<n>` so a foreign generation can never
be overwritten silently, and the journal's `parked_dir` is updated with the
name actually used.

Regression tests: `test_execute_relocation_into_existing_empty_destination`
and `test_recovery_promotes_staged_into_existing_empty_destination`.

## 2 — Bundle: byte-evidence coverage mirrors the audit (fixed)

`_ASSET_REGISTRY_TABLES` covered `cad_measurement_assets` +
`cad_quality_calibration_files` — 2 of the audit's 8 `_ASSET_TABLES` specs.
Verified empirically: an `htdt_acceptance_evidence` row anchored into the
export closure (document-scoped `cad_field_evidence` → `evidence_sha256` →
evidence row) exported **without** its managed file; the row would dangle on
any foreign environment. Digest-only evidence rows (directivity/wave-
excitation/projector-spec source assets, managed equipment evidence,
treatment evidence) had no explicit coverage either — today their bytes
normally arrive transitively through a pulled `cad_measurement_assets` row
(every writer pairs retained bytes with a registry row), but nothing enforced
that invariant at either boundary.

Fix: both export asset closure and the import-side asset check now iterate
the audit's `_ASSET_TABLES` directly — one registry, no drift — with the
audit predicates evaluated on exported rows (`col='lit'`, `col IS NOT NULL`;
anything else fails closed). Registry-style rows (declared size+path) require
a shipped manifest member matching size; digest-only rows accept bytes
already retained and verified in the local store; any declared
`relative_path` must resolve to `measurement-assets/<digest>` (every writer
already produces that path — live readers require `name == digest` under the
store root). Export resolves declared paths through `managed_asset_path` so
a hostile/stale `relative_path` can never escape the data root.

Regression tests: `test_exported_acceptance_evidence_row_carries_its_bytes`
(round-trips into a fresh environment) and
`test_import_rejects_manifest_missing_evidence_bytes` (hand-trimmed bundle
with recomputed manifest hash — refused by the coverage check, not tamper
detection).

## 3 — Fingerprint: aux components (fixed)

`managed_data_fingerprint` covered db stat + header change-counter +
`-wal`/`-shm`/`-journal` + the managed-assets dir, but not the
`backup_included_components()` files — `commissioning-plans.json` rides
inside every archive as `auxiliary/...`, so an aux-only edit is real managed
state that previously never fired the periodic evaluation.

Fix: each included component contributes `aux:<path>:<size>:<mtime_ns>`
(or `:absent`) to the fingerprint.

Regression test: `test_fingerprint_changes_when_auxiliary_component_changes`.

## 4 — Restore rollback: empty pre-restate truth (fixed)

`_rollback_restore_swap` treated `rollback_root/cad-scenes.sqlite3` as the
pre-restore database whenever it existed. When the pre-restore truth was
*empty* (restore onto a fresh data root — the primary disaster-recovery
flow), that slot was free, so: (a) crash before the first evacuation + lost
staging dir → no restorable db anywhere → `RestoreRecoveryError` on every
launch = brick loop; (b) a post-crash occupant (e.g. a foreign db copied in
while the root was wedged) got evacuated into the free canonical slot, moved
back live, and blessed as the pre-restore generation.

Fix: the journal gains `pre_restore_live` — which live objects the swap is
about to evacuate — written before the first move. `_rollback_restore_swap`
takes it as an optional record: expected-present keeps today's strict
behavior (raise when nothing restorable remains); expected-absent settles to
empty (a post-crash occupant that fails health/contract is parked back in the
rollback dir rather than blessed; a *valid* occupant still lands live — the
safest reading is "keep whatever the user wrote after the crash", noted
below). Orphan rollback dirs and journals from older builds pass no record →
strict fallback. Journal schema stays v1 (additive optional field).

Regression test: `test_crashed_restore_into_empty_root_recovers_to_empty_state`
(fabricates the crash via the `_inject_swap_crash` kill harness, lost staging
dir, then a normal restore still completes afterwards).

## Remaining issues / noted, not fixed

- **Upgrade quarantine not enforced at the repository-open boundary.**
  `QUARANTINED_UPGRADE_STATES` enforcement lives only inside
  `execute_native_upgrade`; `ensure_native_schema` (called by
  `SceneRepository.__init__` and other non-GUI open paths) does not consult
  the marker. A `.native-upgrade-state.json` in a quarantined state on a
  same-version build therefore does not block non-upgrade database opens —
  it only blocks a *new* upgrade attempt. Making `ensure_native_schema`
  consult the marker needs an opt-out for the `execute_native_upgrade`
  call sites that intentionally open mid-migration (marker already written).
  Cross-domain: left for the upgrade-path owner.
- **Aux rollback park/lookup asymmetry (latent).** `_restore_backup` parks
  live aux at `rollback_root / live_aux.name` while `_rollback_restore_swap`
  reads `rollback_root / component.path`. Identical for today's flat
  `commissioning-plans.json`; diverges the day a nested aux component is
  registered. Flag for whoever adds the first nested aux component — both
  sides should key on `component.path`.
- **Valid foreign occupant adopted live on empty-prestate rollback.** With
  `pre_restore_live.database=False` and a *valid* post-crash database in the
  canonical slot, the rollback moves it back live (user-write preservation)
  while reporting `rolled_back`. Outcome is data-preserving; the event text
  slightly overstates. Deliberate conservative choice.
- **`_ReplayProbe` scope.** The probe verifies *replayable* authorities
  reproduce canonical derivations; it cannot verify a table with no replay
  adapter — that burden is carried by the `coverage_gap` refusal verified in
  row 6 (belt: replay; suspenders: fail-closed classification). No gap found.

## Verified clean surfaces (no fix needed)

- Member↔manifest set equality, per-member sha/size verification, staged-db
  openability proof, manifest↔staged schema equality — staging refuses any
  archive that can't fully reconstruct.
- Declared-stale subset tolerance: restore accepts a manifest whose declared
  stale set ⊆ actual audit failures; undeclared stale still refuses.
- Journal-before-swap ordering, recovery-chain precedence
  (complete→rollback→pre-restore-archive), residue-sweep 60s floor after
  journal recovery, `.installed` legacy-archive rollback markers.
- `automatic_backup` rotation may delete only `AUTOMATIC_CLASSIFICATIONS`
  generations; safety/manual generations are untouchable; the 15-minute tick
  goes through `NativeWorkerPool`, never the GUI thread.
- Relocation journal phases (PREPARED→STAGED_VERIFIED→DESTINATION_PROMOTED
  →SOURCE_PARKED→BOOTSTRAP_SWITCHED→COMPLETED) settle deterministically from
  the journal; `_RelocationLock` first-byte lock; `assert_managed_root_available`
  runs recovery on every startup; an unverified PREPARED stage is never
  promoted (verified by `test_recovery_never_promotes_an_unverified_prepared_stage`).
- `integrity_error` fails closed at every boundary: stage, post-swap
  validation, rollback validation, live open.

## Test evidence

Scoped runs (xdist, basetemp-isolated, this VM, Python 3.12.10):

- `tests/test_project_bundle.py` + `tests/test_automatic_backup.py` — 36 green (incl. 3 new regression tests)
- `tests/test_data_relocation.py` + `tests/test_data_relocation_cutover.py` + `tests/test_native_backup.py` — 76 green (incl. 3 new regression tests)
- `tests/test_automatic_backup_runner.py` + `tests/test_round11_seams.py` — 18 green
- `tests/test_persisted_data.py` + `tests/test_authority_audit_coverage.py` — 22 green
