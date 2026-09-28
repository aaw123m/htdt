# Round 9 — settings, preferences & persisted-session-state journey

Scope: every `ApplicationPreferenceStore` key (declared vs. consumed), the
persisted session-state surface (`window-state*`, `file_dialog_dirs.json`,
launch record, project recents), the Settings journey (immediate-commit,
pending keys, resets, write-refused), `--safe-mode` completeness, and the
data-root registry vs. relocation. Prior context: `round8-lifecycle.md`
(window state, safe mode, backup runner), `round8-deferred.md` (file-dialog
memory, item-12 per-project window state).

## Verdict table

| # | Finding | Severity | Disposition |
|---|---|---|---|
| 1 | `.native-upgrade-state.json` — the durable upgrade-quarantine marker (#750) — was classified as "operational residue" by `_is_operational_residue` (`startswith('.')`) and **silently dropped** by data-root relocation; the moved root loses its unresolved-upgrade gate | High (data safety) | **FIXED** — registered OPERATIONAL/CARRY; relocation carries it |
| 2 | Safe Mode still auto-opened the most-recent project: `resolve_startup_document` ran unconditionally, so the crash-suspect project was reopened (and re-marked `last_opened`) — `SafeModePolicy.auto_open_last_project=False` was declared but never enforced | High (recovery UX) | **FIXED** — `skip_last_opened` param; `_run_gui` passes it under the policy; opens next-most-recent or a fresh default project |
| 3 | `window-state.json` was global: last-closed project's layout leaked into the next project opened (round-8 deferred item 12; the stated blocker — composition not knowing its project ref — no longer holds: `project_entry` resolves before restore) | Medium UX | **FIXED** — per-project `window-state/<project_ref>.json`, global file kept as fallback for unbound windows & upgraded installs |
| 4 | `file_dialog_dirs.json` + `window-state*` + `htdt-legacy-migration.journal` warned-and-stranded on relocation; `launch-intents/`, `capture-receiver/` (TLS cert/key), `assets`, `htdt.migrated.*` were skipped silently — the unclassified-file warning only iterated *files*, never directories | Medium (data loss on move) | **FIXED** — all registered in `PERSISTED_DATA_REGISTRY` with explicit CARRY/NEVER_COPY policies; warning loop now covers directories |
| 5 | `htdt.migrated.sqlite3.N` / `htdt.migrated.assets.N` numbered archive generations couldn't be classified or carried (exact-path registry) | Low-Medium | **FIXED** — `component_for_path` numbered-sibling rule + relocation carries `.<n>` siblings of CARRY components |
| 6 | Safe Mode restored dialog-memory state and wrote to `file_dialog_dirs.json` (and the tempfile fallback store wrote to disk when unconfigured); safe-mode sessions also overwrote the user's real saved window layout on close | Medium (policy violation) | **FIXED** — `FileDialogMemoryStore.ephemeral()` bound under safe mode; `_save_window_state` no-ops under safe mode |
| 7 | `AutomaticBackupPolicy.save_policy` has **no caller** — `automatic-backup-policy.json` persists a policy no UI can change (interval/dir frozen at defaults) | Medium UX | **DEFERRED** — needs a backup-policy surface in Data Management; see sketch |
| 8 | `RESET_SCOPES` (window_layout / application_preferences / optional_integrations) is dead declarative data — no consumer anywhere; the recovery dialog never offers scoped resets | Medium UX | **DEFERRED** — recovery-dialog reset actions are a product UX decision; see sketch |
| 9 | `display_input.theme` pending (correct), but `apply_dark_theme(app)` runs unconditionally at launch — the definition's default `'system'` is misleading: there is no system-theme detection at all | Low | **DEFERRED** — implementing 'system'/'light' needs a real light palette + QStyleHints hookup; keep pending |
| 10 | `general.language` pending, yet `localization.resolve_locale(policy)` machinery exists unused — `detect_system_locale` called directly at both help/palette sites | Low | **DEFERRED** — wire `resolve_locale` once a live preference lands; needs a runtime locale-swap UX decision (restart_required already declared) |
| 11 | `integrations.rew_host/rew_port` pending; `rew_api_base_url()` helper exists on the store but has zero callers — the only REW REST consumer is the legacy FastAPI `main.py` via `HTDT_REW_API_URL` env/`DEFAULT_REW_API_URL` | Low | **DEFERRED** — native REW acquisition path doesn't consume REST prefs yet; pending markers are honest |
| 12 | `general.startup_destination`/`general.reopen_last_project` pending while the shipped behavior always reopens most recent — with round-9 safe-mode gating these are now the only honest "pending" startup knobs | Low | **DEFERRED** — startup-destination picker semantics need a product decision (landing page vs. last workspace vs. projects) |

## Preference inventory map

`ApplicationPreferenceStore` → `application_preferences.json` (atomic
tmp+fsync+replace; `PreferenceLoadState` OK/MISSING/PARTIAL_INVALID_VALUE/
CORRUPT/INCOMPATIBLE_NEWER_SCHEMA; write-refused on CORRUPT & newer schema;
`.recovery` preserved by reset). `PreferencesWidget` renders every
definition: pending keys disabled + 「準備中」, live keys commit immediately
via `_commit`→`store.set`; `restart_required` flag (only `general.language`)
is displayed; load_error & write-refused surface as warning banners;
reset-all reachable. Verdict: **no silent coercion without disclosure**.

| Key | Live? | UI | Consumer | Verdict |
|---|---|---|---|---|
| `display_input.length_unit` | yes | combo | `cad_display_units.length_display_policy_from_preferences` → inspector + room measure panel | healthy |
| `display_input.numeric_precision` | yes | spin | same chain | healthy |
| `integrations.capture_receiver_enabled` | yes | checkbox | `CaptureReceiverController` (native_cad + settings) | healthy |
| `diagnostics.include_project_ids` | yes | checkbox | `_export_diagnostics_package` (workflow_application.py) | healthy |
| `files.export_dir` | yes | path | `_default_export_dir` → export save dialogs | healthy (nonexistent dir silently falls through to dialog default — cosmetic) |
| `general.language` | pending | disabled | none (resolve_locale unused) | deferred #10 |
| `general.startup_destination` | pending | disabled | none | deferred #12 |
| `general.reopen_last_project` | pending | disabled | none — behavior hard-wired to always-reopen | deferred #12 |
| `display_input.angle_unit` | pending | disabled | none (measure panel always degrees) | consistent pending |
| `display_input.theme` | pending | disabled | none (apply_dark_theme unconditional) | deferred #9 |
| `display_input.reduced_motion` | pending | disabled | none | consistent pending |
| `display_input.high_contrast` | pending | disabled | none | consistent pending |
| `integrations.rew_host/rew_port` | pending | disabled | none in native app | deferred #11 |
| `compute.preferred_backend` | pending | disabled | none | consistent pending |
| `compute.max_concurrency` | pending | disabled | none — `NativeWorkerPool` is unbounded-per-task, `cad_r140_executor` owns its own cap | consistent pending |
| `compute.scratch_dir` | pending | disabled | none | consistent pending |
| `compute.storage_ceiling_mb` | pending | disabled | none | consistent pending |
| `files.portable_bundle_include_libraries` | pending | disabled | none | consistent pending |

No dead keys (UI-without-consumer beyond the declared pending set) and no
unreachable consumers (readers of keys nothing writes) were found —
`PENDING_PREFERENCE_KEYS` honestly marks all 14 unwired definitions.

## Persisted session state — end to end

- `window-state.json` → now per-project (`window-state/<project_ref>.json`,
  schema-version 1 + `project_ref` self-tag; sanitized filename); global
  file = migration fallback. Restored at composition init, saved on
  committed close — both skipped under Safe Mode.
- `file_dialog_dirs.json` → all 20 `QFileDialog.get*` call sites use the
  wrapper module (verified: zero raw `QFileDialog.get` calls in `src/`).
  Covers every directory a user touches through the UI (imports, exports,
  relocation destination, diagnostics package). Under Safe Mode an
  ephemeral store keeps in-session memory without touching disk.
- Recents/working set: `ProjectLibraryRepository.list_projects` ordering
  (opened-outrank-never-opened + rowid tiebreak) verified;
  `resolve_startup_document` is the single auto-open point; safe mode now
  honors `auto_open_last_project=False` (explicit `--document-id` still
  wins).
- Launch intents queue (`launch-intents/`) drains post-window via QTimer;
  classified TRANSIENT/NEVER_COPY (undelivered items abandoned by design).
- `capture-receiver/` (TLS cert/key + state) registered OPERATIONAL/CARRY
  + `sensitive` (carried on relocation so pairings survive; excluded from
  backup archives so credentials never enter portable backups).

## Settings journey

Every visible control in `PreferencesWidget` is exercisable (live) or
deliberately disabled+labeled (pending); commits are immediate — no
apply/restart button exists and none is needed since the only
`restart_required` key is pending. Resets: per-app reset via
`reset_persisted_file` (preserves `.recovery`), exposed on the widget; the
scoped `RESET_SCOPES` recovery-dialog resets are declared but unreachable
(#8). Settings entry points: palette ids `settings.preferences`,
`settings.capture`, `settings.data` → `DataManagementDialog` tabs
(データ管理 / 環境設定 / キャプチャ / 保持管理), `settingsRequested` →
`open_settings`.

## `--safe-mode` coverage (after fix)

| Restored state | Skipped? |
|---|---|
| Window layout restore (`restore_saved_layout`) | yes — early return |
| Window layout **save** on close | yes — now skipped too (#6) |
| File-dialog memory | yes — ephemeral store bound |
| Last-project auto-open | yes — `skip_last_opened` |
| Launch-intent auto-open | already skipped (round-8) |
| Capture receiver / live integrations | already skipped (round-8) |
| Automatic-backup tick | already skipped (round-8) |

Not enforced (deferred): `project_authority='read_only'` — composition has
no read-only mode; renderer 'reduced'/'skip_3d' beyond viewport init are
partially advisory.

## Deferred sketches

- **Backup-policy UI (#7):** add a "バックアップ" section to
  `DataManagementDialog` bound to `AutomaticBackupPolicyStore` — enabled
  checkbox, interval spin, dir picker (reuse `file_dialog_memory`
  `get_existing_directory` with key `backup.policy_dir`). `save_policy`
  already validates; mount behind the existing データ管理 tab.
- **Scoped resets (#8):** `RESET_SCOPES` needs a recovery-dialog action
  `reset_scope:<name>` rendered beside the existing choices; execution =
  delete artifacts per scope (window_layout → `window-state*`,
  application_preferences → `reset_persisted_file`, optional_integrations →
  `preferences.set('integrations.capture_receiver_enabled', False)`).
  Preview text already lives on `ResetScope.description`.
- **Theme (#9):** implement `'system'` via `QStyleHints.colorScheme`
  change-watcher + a light palette; keep dark as the only applied theme
  until then.
- **Locale (#10):** replace the two `detect_system_locale()` call sites
  with `resolve_locale(preferences.get('general.language'))` once a
  language switch is live; restart_required is already declared.
- **REW prefs (#11):** when the native REW path lands, bind
  `rew_api_base_url()` and remove the legacy `HTDT_REW_API_URL` env path.

## Files changed

`backend/src/htdt/`: `window_state.py` (per-project scope + sanitized ref +
global fallback), `workflow_application.py` (ephemeral dialog store under
safe mode, project-ref pass-through, safe-mode no-save), `native_cad.py`
(`skip_last_opened` wiring), `project_library_repository.py`
(`skip_last_opened`), `file_dialog_memory.py` (`ephemeral()`/path=None),
`persisted_data.py` (9 new components + numbered-sibling classification),
`data_relocation.py` (carry numbered siblings, warn on unclassified dirs).

`backend/tests/`: `test_review_round9_prefs.py` (new, 13 tests),
`test_window_state.py` (close-persist asserts project-scoped file),
`test_native_launch.py` (fake repo records `skip_last_opened`; asserts
True under safe mode, False otherwise).

## Tests

`cd backend && TMPDIR=/c/t PYTHONIOENCODING=utf-8 C:/devin/python/python.exe -m pytest -q -n 4`
— full suite result recorded in the PR description; scoped touched-file
runs green (86 tests).
