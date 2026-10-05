# REV54-PASS5 — Fifth-pass review of REV53's merged changes

Scope: `baf69460` (REV53-CAPTURE — ingest commit() 化, newest-per-space
authority, `arrival_source` forwarding, CLI `cli_import` staging) and PR
#563 / `c7b08768` (REV53-PASS4 — six fixes to relocation/backup/bundle/
matrix/fingerprint). Each diff was re-read and the riskier surfaces
exercised with fabricated crash/journal states. Regression tests live
in `backend/tests/test_rev54_pass5.py`.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | Relocation rollback trusted the journaled `parked_dir` as the parked generation's *actual* location. Until the SOURCE_PARKED journal write lands, `parked_dir` is only the *planned* name: a crash between `_park_directory`'s rename and that journal write leaves the real generation at a diverted `<parked>.<n>` sibling while the journaled slot can hold an older parked generation (reachable on same-second relocation retries, where the timestamp-named plan slot is still occupied by the previous run's park). Recovery would promote the wrong generation back live — a silent data-generation regression | HIGH correctness — wrong generation restorable | FIXED — `_locate_parked_source` resolves the parked generation by the `source_database_sha256` recorded at PREPARED across `parked` and its numeric diverted siblings (content-addressed, fail closed); digest-less legacy journals keep name-only trust; `test_recovery_restores_diverted_parked_generation` |
| 2 | Same rollback leg always raised "...rolled the source back to {source} and cleared the transaction" even when no restore ran — source slot occupied, source already holding the generation, or no parked generation surviving. The journal was cleared while the error text claimed a rollback that never happened, masking a stranded generation | MED observability — false recovery report | FIXED — the raise now reports the actual outcome: restored / already-holds-the-generation / parked-left-in-place / no-restorable-generation; `test_rollback_reports_*` (3 tests) |

## Verified-clean surfaces

| Surface | How verified |
|---------|--------------|
| Ingest idempotent-branch `connection.commit()` (baf69460): `connect_sqlite` uses deferred isolation — one implicit txn holds `_register_capture_revision` + `_persist_capture_bundle` repairs, the run materialization, and `_resolve_persisted_quality`'s UPDATE; the single final commit lands them atomically, and no intermediate commit exists between repair INSERTs and the quality UPDATE | Code review of `connect_sqlite` (default isolation_level) + ingest/verify control flow; both commit() call sites sit inside try/except → rollback → re-raise |
| `verify_persisted_ingestion()` commit: persists only the quality-repair UPDATE (single statement) — revision/bundle registry repair lives in `ingest()`/`_migrate_revision_registry`, not the verify path, so no partial-write persistence is possible | Code review of `_verify_persisted_materialization`/`_resolve_persisted_quality` write set |
| `_world_to_scene_by_space` last-write-wins sort: `(created_at_utc, promotion_id)` is ascending so the last iteration per space is the newest; all writers stamp `datetime.now(timezone.utc).isoformat()` (uniform `+00:00`, so lexicographic == chronological, and non-fractioned strings sort first = μs=0, also correct); promotion_id is a content hash — the tie-break is deterministic (arbitrary ordering on same-µs writes only) | Code review of the sort + writer sites (`capture_semantic_promotion.py:1485,2058`) + ASCII ordering check ('+' 0x2B < '.' 0x2E) |
| `arrival_source` forwarding: `route_capture_intent` now passes its caller's value to `import_capture_artifact` (`document_open` default at the API boundary); `capture_import.main` passes `cli_import`; `stage()` stores the value verbatim (free-text provenance column — unknown sources cannot fail, by design) | Code review of `launch_router.py:257,301` + `capture_import.main` + `capture_inbox.stage` signature |
| CLI `cli_import` staging failure vs exit code: `main()` wraps `inbox.stage` in try/except → stderr JSON warning, exit 0 — the persisted ingestion (the real artifact) already succeeded; staging is advisory and `reconcile_orphaned_ingestions` restages on restart | Code review of `capture_import.py:257-270`; intentional fail-soft |
| `restored_database_sha256` acquisition (PR #563): `BackupManifest.valid_manifest` requires exactly one `kind='database'` entry with `sha256` matching `^[0-9a-f]{64}$` — a journaled manifest that validates always yields the sha, so the "unrecorded sha" path is unreachable; invalid/missing `restored_manifest` falls to the strict orphan path (`pre_restore_live=None` → `expected=True`) | Code review of `BackupManifest` validator + `_recover_journaled_swap` guards |
| `expected_assets` gate symmetry: live assets are evacuated to the canonical rollback slot before the gate reads it, so a staged (restore-payload) assets dir cannot be adopted as pre-restore state | Code review of `_rollback_restore_swap` ordering |
| Bundle export/import pin backward compatibility: the import-side `relative_path == measurement-assets/<digest>` pin pre-exists at `c7b08768~1` — PASS4 added only the export-side pin + digest-shape checks on both sides, so every bundle importable before remains importable; export now refuses rows that could never import | `git show c7b08768~1` comparison of `project_bundle.py` |
| `provider.receiver_responses ⊆ verified_receivers` (PR #563): `receiver_responses` is a required persisted field on `LowBandPredictionProvider`; an empty set satisfies the subset trivially but `provider is None` or any unverified cell still forces `coverage_complete=False` — under-claiming only, never over-claiming | Code review of `build_matrix_run_verification` + provider model |
| Fingerprint subtree signature (PR #563): `rglob` is iterative (no recursion-depth limit); rename-only edits invalidate via the relative-path fold; OSError mid-walk degrades to a partial view that self-corrects next evaluation. Latent today — no `backup_included_components()` entry has `is_directory=True`; the branch is forward-compat defense | Registry enumeration + code review; PASS4's `test_fingerprint_tracks_directory_component_contents` covers edit/add/rename |

## Residual notes (not defects, recorded for the next pass)

- **`source` slot occupied by a file (not dir)**: treated as "holds
  content" — the parked generation is left in place and reported, never
  overwritten. Deliberate fail-closed; a file at the source name is
  foreign and needs operator attention anyway.
- **Digest-less journals** (written before `source_database_sha256`
  existed): fall back to trusting the journaled name only — the finding-1
  ambiguity is unresolvable without a recorded digest, so behavior
  matches pre-fix recovery exactly.
- **`capture_revisions`/`capture_bundles` row coverage**: the persisted-
  run verifier checks run materialization only; registry completeness is
  repaired at next ingest (`_register_capture_revision` /
  `_persist_capture_bundle` are idempotent) and by
  `_migrate_revision_registry`. Scope choice, not a gap — the rows are
  written in the same transaction as the run, so they cannot diverge
  post-commit.
- **Non-UTC `created_at_utc` strings** would break the lexicographic
  ordering in `_world_to_scene_by_space` — unreachable via current
  writers (all stamp `timezone.utc`); a foreign row could only shift a
  same-instant tie.
- **Empty subdirectories inside a directory fingerprint component**
  don't contribute to the signature (files only) — consistent with the
  fingerprint's stat-level contract; an empty-dir-only change is not
  managed state worth a backup.
