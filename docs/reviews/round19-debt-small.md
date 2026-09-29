# REV19-DEBT1 — small concrete deferreds + red tests on main

Scope: residual items verified implementable from `docs/reviews/round*.md`,
plus the three Windows-only red tests on main (`4f5135c4`).

## 1. Persisted path separator convention — POSIX (fixed)

**Decision:** persisted/storage paths inside project data are always
POSIX-form (`/` separators). A project or backup moved between machines and
platforms must resolve identically; the separator is part of the persisted
contract, not of the host.

- **Write boundary:** already uniform — all eight
  `INSERT INTO cad_measurement_assets` sites store
  `target.relative_to(data_dir).as_posix()`, and the backup-manifest
  builders use `as_posix()`/literal `auxiliary/` paths. Audit found no
  other persisted path surface with a platform-dependent shape
  (`file_dialog_memory`/`launch_intents` store absolute host paths by
  design — they are per-machine UI conveniences, not portable data).
- **Read boundary:** `safe_managed_relative_path` already normalizes `\` →
  `/`, so rows written by older native-separator builds still resolve;
  regression-pinned by `test_managed_asset_path_accepts_legacy_backslash_row`.
- **Red tests fixed:** `test_persisted_dataset_reopens_exact_source_bytes`,
  `test_concurrent_same_digest_distinct_measurements_share_single_asset`,
  `test_imported_excitation_persists_reopens_and_replays` asserted
  `str(Path('measurement-assets') / digest)` — the *host* separator — against
  the stored POSIX value; on Windows the expectation became `...\digest` and
  the rows honestly stored `.../digest`. Expectations now pin the POSIX
  convention (`f'measurement-assets/{digest}'`); new
  `test_persisted_asset_relative_path_is_posix_shaped` fails loudly if any
  writer ever stores `\`.

## 2. BackupError (r14-errmsg deferred) — implemented

- `native_backup.BackupError(ValueError)`: all 44 bare `raise ValueError`
  sites in `native_backup.py` (manifest validation, archive-path safety,
  sqlite health, staging/member verification) now raise `BackupError`.
  `ManagedAssetError` escaping `verify_managed_asset` inside the backup
  contract is re-wrapped as `BackupError` so a corrupt managed asset inside
  a backup surfaces in the backup domain.
- Mapper: `user_facing_error._NAME_PATTERNS` gains
  `('BackupError', 'backup.invalid', 'バックアップデータを処理できませんでした')`
  via the module's established class-name convention — no import, no cycle.
  `except ValueError` handlers and existing `pytest.raises(ValueError)`
  callers are unaffected (subclass).
- `AutomaticBackupError` already existed as the runner-domain type; left as
  is (it is never raised bare — the scheduler surfaces via its own state).

## 3. `localization.format_datetime` dead code — wired

`saved_label` hand-formatted `f'{parsed:%Y-%m-%d %H:%M} の保存'` — an ISO
stamp inside a Japanese sentence. It now renders through
`format_datetime(parsed)` (product-default locale), producing
`2026年9月24日 18:42 の保存`. All label surfaces (`revision_display_label`,
`variant_display_label`, `spec_display_label`, `format_versioned_label`)
inherit it since they delegate to `saved_label`; the unparseable fallback
returns the raw string unchanged. `data_management_ui._format_created_at`
was *not* rewired — its explicit `UTC` suffix is load-bearing information
the localized form intentionally drops.

## 4. Naive-timestamp cosmetics — honest UTC

- `acoustic_treatment_service.create_definition`: id entropy token was
  `datetime.now().timestamp()` (naive → host-local interpretation) →
  `datetime.now(timezone.utc).timestamp()`.
- `cad_assumption_decision_repository._instant` and
  `cad_design_brief_repository._instant` (r15-time documented residual):
  `parsed.astimezone(timezone.utc)` on a naive stored value reinterpreted
  it as *host-local* — silent skew on foreign/restored rows. Naive values
  are now pinned to UTC before conversion (writers only ever write UTC
  ISO-8601).

## 5. Deferred re-check — nothing else small remains

Sampled every `docs/reviews/round*.md` deferred entry. Already fixed by
earlier rounds: r15-time `ORDER BY created_at_utc` tiebreaks (fixed in
r18). Correctly still deferred (design/product/schema calls, not ≤30-line
honest diffs): workers D1/D2/D3/D6, `capture_disposition_transitions`
migration, `scene_document_heads` head-read FK check (silent-fallback vs
fail-closed is a product call), import-as-copy remap,
`response_model`/pagination, `PARSER_VERSION` pin (bumping invalidates
every sealed dataset), per-`Device:` bucketing, settings lazification,
MAX_PATH, `progress_sink` lifecycle, append-only table growth policy.

## Files changed

- `backend/src/htdt/native_backup.py` — `BackupError`, 44 raise swaps,
  `ManagedAssetError` re-wrap.
- `backend/src/htdt/user_facing_error.py` — `backup.invalid` mapping.
- `backend/src/htdt/cad_display_labels.py` — `saved_label` →
  `format_datetime`.
- `backend/src/htdt/acoustic_treatment_service.py` — tz-aware id stamp.
- `backend/src/htdt/cad_assumption_decision_repository.py`,
  `cad_design_brief_repository.py` — naive ⇒ UTC in `_instant`.
- `backend/tests/test_cad_directivity_source_assets.py`,
  `test_cad_measurement_asset_atomicity.py`,
  `test_cad_wave_excitation_evidence.py` — POSIX expectations.
- `backend/tests/test_cad_display_labels.py` — localized label
  expectations.
- `backend/tests/test_review_round19_debt.py` — 8 regression tests.

## Suite result

Scoped run (`QT_QPA_PLATFORM=offscreen pytest -n 4`, 21 files, 278 tests):
all pass — including the three previously red tests, verified green on
Windows.
