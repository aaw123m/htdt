# Round 20 — Corrupted-state honesty

Scope: every persisted artifact in `PERSISTED_DATA_REGISTRY`
(`persisted_data.py`) deliberately corrupted each way a real user/disk
could, and each load path driven to verify the app degrades honestly —
a clear Japanese error naming the artifact and the problem, or the
documented clean fallback for convenience stores — never a crash, never
silently loading wrong data, never a blank/dead surface.

Corruption modes applied per artifact: truncated mid-content, valid JSON
with wrong shape (schema_version mismatch, missing/extra keys, wrong
types), invalid UTF-8 inside strings, partially missing members
(dangling FK/head pointer, deleted sidecar), read-only file, file locked
by a live writer (BEGIN IMMEDIATE held), size-preserved garbage bytes,
and zero-length.

Method: a corruption harness (`C:\t\round20\corrupt_audit.py`) builds one
real managed data dir — `cad-scenes.sqlite3` with a saved revision and a
managed measurement asset, `commissioning-plans.json`,
`application_preferences.json`, `reference_library_meta.json`,
`window-state.json`, `file_dialog_dirs.json`, `activity_history.json`,
automatic-backup policy+state, `.native-upgrade-state.json`,
`runtime.json`, `.instance.lock`, a queued launch intent,
`upgrade-recovery` markers, plus a `.htdt-backup` archive and a
`.htdtproject` bundle — applies each corruption mode, and drives the
artifact's real read path (~140 probes). Code review covered paths the
harness can't reach cross-process (restore journal, legacy migration,
capture receiver TLS).

Branch `devin/rev20-recovery`. Tests:
`backend/tests/test_review_round20_corrupted_state.py` (28 cases).

## Verdict table

| # | Artifact / finding | Severity | Verdict |
|---|--------------------|----------|---------|
| 1 | `.native-upgrade-state.json` present but unreadable → `execute_native_upgrade` returned `outcome='no_upgrade'`, silently skipping quarantine resolution and opening a committed-but-never-verified generation | **HIGH** — wrong data trusted | FIXED — raises `NativeUpgradeQuarantineError` |
| 2 | `cad-scenes.sqlite3` existing zero-byte file → treated as legacy v0 → `ensure_native_schema` seeded a fresh schema; a truncated-to-empty database silently became an empty project | **HIGH** — silent data-loss presentation | FIXED — `NativeSchemaError` naming the file |
| 3 | `commissioning-plans.json` corrupt → `UnicodeDecodeError` escaped `_load`; valid non-dict JSON → `AttributeError` inside `save`/`get`/`latest`/`list_plans` | MED — crash on open | FIXED — tolerant load + shape check |
| 4 | `activity_history.json` corrupt → `JSONDecodeError`/`UnicodeDecodeError`/`AttributeError` escaped `load_history` → broke the whole diagnostics export (`workflow_application.py:2130`) | MED — diagnostics dead | FIXED — tolerant `_load_payload` |
| 5 | `upgrade-events.jsonl` with damaged UTF-8 → `UnicodeDecodeError` escaped `list_upgrade_events` (`except OSError` only) | LOW — diagnostics read | FIXED — `(OSError, ValueError)` |
| 6 | `scene_revisions.payload_json` corrupt → bare `JSONDecodeError` surfaced from `_row_to_revision` (honest but didn't name the artifact) | LOW — vague error | FIXED — `SceneRevisionIntegrityError` names revision id |
| 7 | `measurement-assets/<sha>` corrupt/missing/wrong-size | — | VERIFIED OK — `ManagedAssetError`/`read_verified` hash contract, all modes |
| 8 | `cad-scenes.sqlite3` truncated/garbage/not-a-db/locked/readonly | — | VERIFIED OK — `NativeSchemaError` / retryable `storage.locked` surface |
| 9 | `scene_document_heads` dangling head pointer | — | VERIFIED OK — `SceneDocumentHeadIntegrityError` names document+revision |
| 10 | `application_preferences.json` all modes | — | VERIFIED OK — `PreferenceLoadState.CORRUPT` + JP `load_error`, writes refused |
| 11 | `reference_library_meta.json`, `file_dialog_dirs.json`, `window-state*.json`, `runtime.json`, `.instance.lock` metadata | — | VERIFIED OK — `(OSError, ValueError)` + shape checks → clean fallback |
| 12 | `activity_history.json` foreign-but-valid dict | — | VERIFIED OK — wrong schema_version → `()` |
| 13 | `automatic-backup-policy.json` / `-state.json` | — | VERIFIED OK — policy → `defaults()`, state → `{}` (foreign keys inert) |
| 14 | `.htdt-backup` archive all modes | — | VERIFIED OK — `BackupError` naming the archive; member hash/size/manifest identity all enforced |
| 15 | `.htdtproject` bundle all modes incl. missing | — | VERIFIED OK — `ProjectBundleError`/`BundleManifestInvalidError`; manifest+table+asset hashes verified |
| 16 | `launch-intents/incoming/*.json` corrupt | — | VERIFIED OK — unreadable drops move to `dead/`, queue survives |
| 17 | `restore-journal.json` corrupt | — | VERIFIED OK — journal rejected → `_recover_unjournaled_swap` recovery path |
| 18 | `htdt-legacy-migration.journal` | — | VERIFIED OK — presence-only sentinel; content can't be corrupted |
| 19 | legacy `htdt.sqlite3` / `htdt.migrated.*` | — | VERIFIED OK — `inspect_legacy_store` maps `DatabaseError` → `unreadable` |
| 20 | `capture-receiver/receiver-*.pem` corrupt | — | VERIFIED OK — `start()` catches `SSLError`, regenerates once |
| 21 | `automatic-backup-state.json` foreign dict returned verbatim | LOW | DEFERRED — inert: callers read only known keys |
| 22 | Non-authoritative `payload_json` row readers across ~12 repositories raise bare `JSONDecodeError`/`ValidationError` rather than named integrity errors | LOW | DEFERRED — still fail closed + generic JP error; `verify_native_row_integrity` catches them at audit depth |

## 1 — Upgrade quarantine silently defeated by marker corruption (HIGH, fixed)

`.native-upgrade-state.json` is documented as *the authoritative
quarantine signal*: `write_upgrade_state` commits it before each
migration boundary via temp+`os.replace`, and `execute_native_upgrade`
is supposed to re-verify any generation whose marker says
`migrating`/`committed_pending_verification`/`failed_after_commit`.

`read_upgrade_state` returns `None` for a corrupt file — correct for its
callers — but `execute_native_upgrade` then treated corrupt-identical-to-
absent: the `QUARANTINED_UPGRADE_STATES` check saw `None`, fell through
to `outcome='no_upgrade'`, and opened the live generation with zero
verification. A marker that reads `committed_pending_verification` means
exactly "this DB committed a migration and verification never ran";
disk-level damage to that file must not downgrade it to silence.

Reproduced with every corruption mode: harness `upgrade_execute
[truncated|wrong_shape|bad_utf8|empty]` all returned
`outcome=no_upgrade live=unchanged` before the fix.

Fix (`native_upgrade.py`): in the `compatibility == 'current'` branch,
`marker is None and upgrade_state_path(data_dir).is_file()` now raises
`NativeUpgradeQuarantineError` naming the marker — the same error class
the unresolved-verification path already raises, mapped to the existing
JP `migration.quarantine` surface ("再起動して検証を再試行してください").
Absent marker still returns the silent `no_upgrade` event; a parseable
quarantined marker still re-runs read-only verification.

## 2 — Zero-byte database silently re-seeded as fresh project (HIGH, fixed)

`read_native_schema_version` and `check_native_schema_compatibility`
shared `if not path.is_file() or path.stat().st_size == 0: return 0` —
version 0 = legacy/unversioned = eligible for adoption. A real SQLite
database is never zero bytes once written (its first page lands inside
the create transaction), so an *existing* 0-byte `cad-scenes.sqlite3` is
always torn or truncated state. Yet `ensure_native_schema` opened it,
saw no tables, validated the empty legacy signature vacuously, and ran
v1→v11 migrations: harness `database [empty]` produced a valid empty
schema and `current_head` returned `None` — the project opened silently
empty over evidence of data loss.

Fix (`cad_schema.py`): `_reject_empty_database_file` raises
`NativeSchemaError('native database file exists but is empty: …')`,
called from `read_native_schema_version`,
`check_native_schema_compatibility`, and `ensure_native_schema`.
Missing files still return 0 / initialize fresh — a real fresh install
has no file at all. All surfaces stay honest: `plan_native_upgrade`
propagates the schema error at launch; `_sqlite_health` wraps it as
`BackupError` for a 0-byte staged member; `run_health_checks` reports
`unreadable`. Dead `st_size == 0` clause removed from
`plan_native_upgrade`; docstring updated.

## 3 — Commissioning registry crashed on any non-JSON-decode corruption (MED, fixed)

`CommissioningPlanRepository._load` caught only
`(json.JSONDecodeError, OSError)`: a UTF-8-damaged file escaped as
`UnicodeDecodeError`, and a well-formed non-dict payload (`[1,2,3]`,
`5`, `"text"`, `{"plans": 5}`) was returned unchecked so every method
crashed on `.get`/`.setdefault`/`.values` — `AttributeError`, breaking
project open on one torn sidecar.

Fix: `_load` catches `(OSError, ValueError)` and requires
`dict` + `dict.get('plans')` to be a dict, else the documented
empty-registry fallback. `latest()`/`list_plans()` skip non-dict rows
(`isinstance(raw, dict)`); `list_plans` also tolerates `KeyError`/
`TypeError` on a malformed row. A corrupt row still fails closed with
`CommissioningPlanError` on `get()` — typed, named, honest.

## 4 — Activity history could kill the diagnostics export (MED, fixed)

`ActivityCenter.load_history`/`load_active_operations` did
`json.loads(path.read_text('utf-8'))` unguarded, then
`payload.get('schema_version')`: truncated → `JSONDecodeError`,
bad UTF-8 → `UnicodeDecodeError`, non-dict JSON → `AttributeError`,
non-list `operations` → `TypeError`. The one caller that matters,
`_export_diagnostics_package` (`workflow_application.py:2130`), wraps
the whole export — one corrupt diagnostics file broke the entire
support package.

Fix: `_load_payload` reads tolerantly (`except (OSError, ValueError)` →
log + `None`), requires dict + matching `schema_version`;
`_load_operations` requires `items` to be a list (per-row validation was
already hardened). Corrupt file → `()` for both readers; valid payloads
unchanged.

## 5 — Upgrade journal encoding crash (LOW, fixed)

`list_upgrade_events` reads `diagnostics/upgrade-events.jsonl` and
caught `OSError` around `read_text` — a `UnicodeDecodeError` (a
`ValueError`) escaped the whole diagnostics read. Fixed to
`except (OSError, ValueError)`; per-line `ValueError` handling already
skipped malformed entries.

## 6 — Scene revision payload errors now name the artifact (LOW, fixed)

`_row_to_revision` let `json.loads`/`model_validate` raise bare
`JSONDecodeError`/`ValidationError` — honest (fail-closed, never loads
wrong data) but surfaced without identifying which authority row is
corrupt, unlike sibling `SceneDocumentHeadIntegrityError`/
`AuthoringConstraintIntegrityError`. New
`SceneRevisionIntegrityError(ValueError)` now wraps payload parse
failures, the content-hash mismatch, and the document-id mismatch —
all named by `revision_id`; `ValueError` subclassing keeps every
existing `except ValueError` boundary valid.

## Deferred

- **`automatic-backup-state.json`** returns a foreign-but-valid dict
  verbatim (`{'totally': 'foreign'}`). Inert today — every consumer reads
  only known keys — but a stricter shape check would match the
  commissioning fix. One-line change, low value.
- **Non-authoritative `payload_json` row readers** across ~12
  repositories raise raw `JSONDecodeError`/`ValidationError`. They fail
  closed and the audit lane (`verify_native_row_integrity`) detects the
  corruption semantically; naming each artifact would need a per-repo
  integrity-error family — large surface, low marginal honesty.

## Already-honest paths verified this round

`ManagedAssetStore.read_verified`/`verify_managed_asset` — every mode
raises `ManagedAssetError`/`ValueError` naming the digest contract;
`inspect_backup`/`_stage_backup` — `BadZipFile`→`BackupError`, member
set/hash/size/manifest-identity all enforced; `import_project_bundle` —
`BadZipFile`→`ProjectBundleError`, manifest+per-table+per-asset hashes;
`ApplicationPreferenceStore` — typed `PreferenceLoadState` + JP
`load_error`, writes refused while corrupt; `drain_launch_intents` —
unreadable drops quarantined to `dead/`; `_read_restore_journal` —
corrupt journal → None → unjournaled-swap recovery; `runtime_instance`
readers, `LibraryMetaStore`, `FileDialogMemoryStore`, `load_window_state`,
`AutomaticBackupScheduler`, `inspect_legacy_store`,
`_ensure_certificate` — all already tolerant or typed-error.
