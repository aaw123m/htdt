# REV53-PASS4 — Fourth-pass review of REV52's merged changes

Scope: PR #560 (`5da0f903` REV52-DATA — integrity fixes to relocation/
backup/bundle/fingerprint), PR #561 (`e2ffd6c8` REV52-GAEMIT — GA
capability emit + `MatrixRunVerification`), and `e7769acf`
(`NON_BUNDLE_SCHEMA_DOCUMENTS`). Each diff was re-read and the riskier
surfaces exercised with fabricated crash/corruption states. Regression
tests live in `backend/tests/test_rev53_pass4.py`; every finding below
was confirmed by a failing test against the pre-fix code before the fix
was written.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | Relocation rollback `os.replace(parked, source)` could not restore the parked generation when a post-crash **empty** directory stood at the source name — the same Windows WinError 5 trap REV52 fixed on the forward path survived at the rollback site, and recovery cleared the journal and reported the source as unrestorable while the generation still sat parked | HIGH availability/data-loss-adjacent | FIXED — the rollback leg now routes through `_promote_directory` (removes an existing-empty destination, refuses foreign content); `test_recovery_restores_parked_over_empty_source_dir` |
| 2 | `_rollback_restore_swap` blessed the interrupted swap's **own staged database** as recovered state: with `pre_restore_live.database=False` the canonical rollback slot is free at evac time, so the restored generation round-tripped through it back into live, passed the contract check, and the recovery reported `rolled_back` over the very payload the swap failed to commit (reachable when the staged dir is swept — e.g. `TemporaryDirectory` cleanup after a kill) | HIGH correctness — failed payload adopted as recovered truth | FIXED — `_recover_journaled_swap` passes `restored_database_sha256` (journal's `restored_manifest` database entry); a canonical-cycled live db matching that sha under `expected_database=False` is parked again instead of blessed; `test_crashed_restore_into_empty_root_does_not_bless_restored_generation` |
| 3 | Same rollback ignored the journaled `pre_restore_live.measurement_assets`/`auxiliary` truth for the canonical assets slot: with `measurement_assets=False` an occupied canonical slot (post-crash residue or the swap's own staged assets) was moved back to live and adopted | MED correctness | FIXED — `expected_assets` gate on the canonical assets restore; the same test covers it (restored-assets also stay parked, root ends empty) |
| 4 | Bundle export/import asymmetry: export accepted any `relative_path` under the data root while import pins rows to `measurement-assets/<digest>` — a corrupt row pointing at the asset's real bytes stored at a wrong path exported **cleanly** (size+sha both pass the streamed write check), producing a bundle every import must reject; both sides also resolved `store.asset_path(digest)` without validating the digest shape (out-of-root stat/read only — still fail closed) | MED — unimportable artifacts could be produced | FIXED — export pins registry rows to `measurement-assets/<digest>` (matching the import invariant) and both sides now reject non-`[0-9a-f]{64}` digests before touching the filesystem; `test_export_rejects_asset_row_pointing_outside_store` |
| 5 | `MatrixProviderVerification.coverage_complete` counted *cells* (`not unverified`), not the provider's *covered receivers*: a provider covering receivers the matrix never ran still got `coverage_complete=True` → `promote_provider_via_matrix_run` would claim "verifies every receiver this provider covers" on partial evidence | MED — promotion over-claims validation evidence | FIXED — `build_matrix_run_verification` tracks each entry's verified receiver set and requires `provider.receiver_responses ⊆ verified`; `test_matrix_coverage_incomplete_when_provider_receivers_unrun` |
| 6 | `managed_data_fingerprint` folded only a directory component's own `stat` — an inner-file edit inside an `is_directory` backup-INCLUDE component leaves the directory size/mtime untouched, so periodic backups could skip real managed-state changes (latent: today's INCLUDE set is flat files only) | LOW — latent forward-compat hole | FIXED — directory components fold a per-member `name:size:mtime` subtree signature; `test_fingerprint_tracks_directory_component_contents` (edit, add, rename all invalidate) |

## Verified-clean surfaces

| Surface | How verified |
|---------|--------------|
| `_promote_directory` / `_park_directory` forward paths: rmdir failure on non-empty/permission/AV-held destinations propagates honestly (never merges into foreign content); populated park slots divert to `<parked>.<n>` and the journal records the actual name | Code review + REV52's existing tests; finding 1 covers the missed call site |
| `pre_restore_live` old-journal compatibility: absent/None/non-dict → strict `expected=True` reading — orphan and older-format journals keep REV52's fail-closed behavior; journal write failure raises before any swap move | Code review of `_recover_journaled_swap` guard + `_write_restore_journal` ordering |
| `pre_restore_live.auxiliary` leg: live aux files already route to the `evacuated-live` side dir and can never claim a canonical slot — the journaled truth was already honored there | Code review; the aux path is structurally separate from the db/assets canonical slots |
| Bundle streamed write path: a wrong-bytes-at-right-path asset row still fails closed at write (member sha256 verify), independent of the new path pin | Code review + first (bytes-mismatched) variant of the finding-4 test raised during ` _write_bundle` |
| `safe_managed_relative_path` / `managed_asset_path`: rejects `..`, `.` segments, absolute paths, drive letters, and `:`-containing roots, then double-checks resolved containment under the data root | Code review; the pin added in finding 4 makes the escape surface unreachable for registry rows anyway |
| `_row_matches_asset_where`: `col='lit'` and `col IS NOT NULL` forms handled; unknown predicate shapes raise (fail closed) rather than silently including/excluding rows | Code review |
| GA `save_descriptor` emit: manifest build is content-addressed (`manifest_id` = semantic sha); identical re-persist → dedup returns persisted → emit produces the identical manifest → `save_capability_manifest` dedup no-op. Emit runs after the descriptor commit for every lane — a descriptor never persists without its declared manifest | `test_rev52_gaemit.py` idempotency assertions + dedup code path |
| Artifact-commit produced emit: requires the persisted descriptor's `semantic_sha256` to match the artifact's binding; produced-narrowed rows come from the same derivation table with `produced_observables` — an undeclared observable raises (fail closed, can't over-claim) | `test_rev52_gaemit.py` + code review of `_emit_produced_capability_manifest` |
| `MatrixRunVerification` mutation detection: per READY/CACHED cell re-derives `_cell_transfer_result_sha256` and demands exact `MatrixCellTransfer` equality; `cell_state_counts` must reproduce from the persisted result set; non-terminal runs/cells raise; the persisted record replays through `model_validate` (hash-bound ids) | `test_rev52_gaemit.py` doctored-ref rejection + build-path code review |
| Promotion honesty: rebuild lands exactly at `validated`/`synthetic_fixture` with `validation_authority_ref=<verification ref>` — no `owned_room` claim possible (model validator rejects it) | `test_rev52_gaemit.py` promotion assertions + `LowBandPredictionProvider` validators |
| `NON_BUNDLE_SCHEMA_DOCUMENTS` honesty: `test_support_matrix_consistency` covers BOTH rot directions — `NON_BUNDLE <= on_disk` fails if the name rots, `NON_BUNDLE ∩ referenced` fails if it becomes a bundle doc, and orphans are still flagged | Test code review — the exemption cannot silently rot |
| `provider_entry` first-match on multi-source-same-provider: coverage is uniform per provider (same provider + same receiver grid → same verified/blocked pattern), so first-match cannot pick a luckier entry | Code review — reachability argued, not a defect today |

## Residual notes (not defects, recorded for the next pass)

- **`pre_restore_live` vs a *valid foreign* occupant db**: when the journal
  says the pre-restore database was absent, a live db that is neither the
  restored generation nor corrupt is still adopted as "recovered" state
  (REV52's deliberate tolerance for files a user dropped post-crash).
  Finding 2's sha check narrows the window to genuinely foreign files.
- **`providers[source_id]` collapse**: a matrix source whose transfers
  bound *different* providers keeps only the last — but those cells then
  fail the transfer/provider equality checks and the build raises
  honestly. Functional gap, not a mis-verification risk.
- **Pre-REV52 descriptors** (upgraded databases) hold no declared
  capability manifest until the next `save_descriptor` — which every
  adapter registration performs, so the emit self-heals on the next normal
  run. The produced-emit path was left as produced-only.
- Rollback debris (invalid occupants, journaled-absent assets, the parked
  restored generation) is deleted with the rollback dir on success — the
  established fail-closed cleanup contract, unchanged.
