# Round 14 review — update / upgrade / migration user journey

Scope: what a user experiences when they update HTDT across versions —
specifically the flagged hazard where a build that re-keys persisted
authority (the #848 pattern: new algorithm/registration SHAs) strands the
user's evidence *and* their ability to back it up.

All findings below come from an end-to-end replay against real stores
(`scripts/rev14_upgrade_replay.py`): phase `seed` writes a store under the
current build, phase `rekey` applies the next-version re-key in a fresh
process before any repository opens the store, then enumerates every
user-visible consequence.

## Replayed upgrade path — what the user actually hit before this round

Seeded store: one scene (F1 fixture), one routing profile (2 entries),
one wiring check bound to that profile, two measured FR datasets, one
persisted comparison.

Simulated next build:

- `comparison.ALGORITHM_VERSION` `fr-compare-1` → `fr-compare-2`, new
  `COMPARISON_ALGORITHM_SHA256`, the old version dropped from
  `_COMPARISON_RESULT_REPLAY` (the literal #848 hazard).
- `CadRoutingProfile.identity_payload` gains a `seal_version` field, so
  every persisted profile fails its self-hash on parse.

Observed consequences (verbatim from the replay):

| Surface | Before the fix | Failure class |
|---|---|---|
| `audit_native_authority_graph` | 3 diagnostics | profile `noncanonical_derivation`, wiring check `noncanonical_derivation` (cascade via bound profile), comparison `stale_authority` (`algorithm version is not replayable: fr-compare-1`) |
| `create_backup` (`--backup`) | **REFUSED** `AuthorityAuditError` | the flagged trap — user cannot export their only copy |
| `get_comparison` / `list_comparisons` | REFUSED `ValueError` | wall of refusal |
| `get_routing_profile` / `list_routing_profiles` | REFUSED `ValidationError` | wall of refusal |
| `get_wiring_check` / `list_wiring_checks` | REFUSED `ValidationError` | wall of refusal |
| `execute_native_upgrade` with schema stamped to v9 | **REFUSED** at the recovery-snapshot `create_backup` → `NativeUpgradeError` | total lockout — repeats on every launch, the app never opens |

So the honest answer to "what does the user see post-update": on a build
that only re-keyed evidence (schema version unchanged), the app opens —
but every evidence-bearing surface throws raw errors, backup refuses,
and nothing explains why or what to do. On a build that *also* bumped the
schema version, the app cannot finish launching at all, because the
upgrade's own recovery snapshot hit the fail-closed backup audit.

## What was implemented

### 1. 再検証 — a real revalidation lane (`htdt/authority_revalidation.py`)

`revalidate_native_authority_graph(database_path)` audits the store,
dispatches each failing row to a re-deriver in dependency order
(routing profile → wiring check → measurement comparison), then re-audits.
Nothing is blanket-promoted:

- **Routing profile** — parse the stored `payload_json` raw, rebuild via
  `build_routing_profile` (which re-seals under the current convention),
  then a *semantic-drift gate* compares stored vs rebuilt payload
  field-by-field ignoring only the `routing_profile_sha256` that was
  expected to move. Any other field that would change → kept stale.
  The row UPDATE writes new sealed bytes, then `get_routing_profile`
  re-verifies the canonical read; a row that still fails rolls back to
  its original bytes.
- **Wiring check** — same drift gate, ignoring `check_sha256` plus the
  sanctioned embedded `routing_profile_sha256` remap when the bound
  profile was re-sealed in the same pass.
- **Measurement comparison** — the honest re-derivation: reload the two
  persisted datasets through the verified repository read (dataset
  self-hash gate against the stored pins first), re-run the registered
  replay builder for the stored `algorithm_version` when it still
  exists, else the current algorithm — then require every output field
  (band, grid, a/b arrays, differences, offsets, counts) to equal the
  stored result before promoting under the new identity. The version
  stamp is the only field allowed to differ.
- Anything with no re-derivation lane, unparseable stored bytes, drifted
  semantics, or a failing post-write canonical read is `kept_stale` with
  a Japanese reason, and a fresh post-pass audit is the final arbiter —
  `report.resolved` means the audit is clean, not that writes happened.

Entry points: launch dialog (below), `--revalidate` CLI, and a
「記録を再検証」 button on the data-management page
(`controller.revalidate()` → `backend.revalidate()`).

### 2. Degraded backup — the `--backup` trap resolved (`htdt/native_backup.py`)

`create_backup` stays fail-closed by default (unchanged contract), but
gains `allow_stale: bool = False`. With it, a store carrying stale
authority produces a **degraded archive**:

- `manifest.stale_authorities` records every failing row
  (authority/record_ref/failure_class/dependency/message) — folded into
  the manifest identity hash only when non-empty so existing archives
  keep validating.
- Unclassifiable coverage gaps are *never* degradable
  (`unclassified_tables` still refuses unconditionally).
- `_stage_backup` re-audits the staged bytes on restore and tolerates
  exactly the declared set — an undeclared or unexpected failure still
  refuses (subset semantics: a declared row that re-verifies cleanly is
  benign).
- Internal safety copies — the upgrade recovery snapshot, the automatic
  backup scheduler, the pre-restore backup — pass `allow_stale=True` so
  the app never loses its own safety net over flagged data.

Surface points: `--backup-allow-stale` (requires `--backup`, prints a JP
notice listing declared rows), a GUI checkbox
「検証を通過しない記録を含めてバックアップする」, and
`BackupMetadata.stale_authority_count` so restore previews can show the
archive's state.

### 3. First-run-after-update UX (`htdt/native_cad.py`)

A build marker (`htdt-launch-build.json`, written atomically in the data
dir) records the last launched build. On the first GUI launch of a
changed build, launch runs one bounded authority audit before the
workspace opens; if anything needs revalidation, a Japanese dialog
summarizes the count and offers 「再検証を実行」/「あとで」 — deferral is
explicit and the lane stays reachable from データ管理. Safe Mode skips
the offer entirely; the check is fully non-fatal and never blocks launch.
`--revalidate` gives the same lane to CLI users.

### 4. Upgrade survives stranded evidence (`htdt/native_upgrade.py`)

The recovery snapshot now uses the degraded-backup path, and the stale
set it declares is stored on the `UpgradeStateRecord`
(`declared_stale_authorities`) and journaled on the completed event
(`stale_authority_count`). Post-migration verification tolerates exactly
that declared set — anything the migration *newly* broke still fails the
upgrade. The quarantine-retry path (`_resolve_quarantined_generation`)
re-reads the declaration from the durable marker, so a retry after a
mid-upgrade crash keeps the same contract.

### 5. Downgrade — verified already honest

Opening a newer-version store with an older build refuses cleanly:
`native_schema_version > NATIVE_SCHEMA_VERSION` raises
`IncompatibleNewerSchemaError`, the launch shows
`newer_schema_dialog_copy_ja` (「このデータは新しいバージョンのHTDTで作成
されています」…), and restore refuses a newer-schema archive at
`data_management.py` (`native_schema_compatibility == 'incompatible_newer'`
→ explicit error, covered by
`test_restore_refuses_versioned_database_that_cannot_complete_migration`).
No silent corruption — nothing to change.

## After the fix — same replay, now

```
create_backup (fail-closed, unchanged default): REFUSED AuthorityAuditError (3 diagnostics)
create_backup allow_stale=True: OK — degraded manifest, 3 declared stale rows
execute_native_upgrade: OK
get_* / list_* : REFUSED (still honest — nothing silently served stale)
再検証 lane: 3 件の記録を再検証し、すべて現在の形式で再署名されました
create_backup (post-revalidation): OK
```

## Tests

`backend/tests/test_authority_revalidation.py` — 7 tests over real
SQLite stores:

1. `test_rekey_strands_every_authority_kind` — replay assertion, all
   three authorities go stale.
2. `test_revalidate_recovers_rekeyed_authorities` — full recovery, clean
   post-audit, canonical reads work.
3. `test_revalidate_keeps_semantically_tampered_rows_stale` — drifted
   payload stays stale, original bytes untouched, reason explicit.
4. `test_revalidate_comparison_keeps_stale_when_result_changed` —
   tampered comparison output is not promoted.
5. `test_plain_backup_refuses_but_degraded_backup_roundtrips` — degraded
   archive validates, restores the exact declared staleness, then
   revalidates clean on the restored copy.
6. `test_execute_native_upgrade_survives_stale_evidence` — v9→v10
   upgrade over a re-keyed store completes, declaration journaled.
7. `test_launch_marker_tracks_build_identity` — build-change detection.

Test doubles extended for the new signatures:
`_FakeController.create_backup(allow_stale=...)`,
`_fail_verify(**kwargs)` in the quarantine test.

## Deferred / known limits

- Re-derivation lanes exist for the three authorities this round's
  hazard actually strands (routing_profile, wiring_check,
  measurement_comparison). New authority kinds added later must extend
  `_REVALIDATORS`; rows without a lane report `kept_stale` with
  「この機関には再導出経路がありません」 rather than guessing.
- The launch dialog is per-build-change, one-shot (the marker records
  "this build launched here"); the data-management button is the
  repeatable entry point.
- A degraded archive's stale rows stay stale through restore — the
  manifest declaration travels, so the destination build can revalidate
  them; nothing auto-promotes on unpack.
