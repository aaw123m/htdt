# Round 13 — Settings & option effect truth

Scope: every user-facing toggle/dropdown/spinbox must do something
observable, persist correctly, and apply at the right scope. Branch
`devin/rev13-set`. No GitHub Actions — verified locally with pytest
(`QT_QPA_PLATFORM=offscreen`).

## Method

Every settings surface was enumerated, each control's write path traced
to its store, and every store key traced to a live consumer. Findings
were verified by flipping the value and observing the effect — the new
`backend/tests/test_review_round13_settings.py` pins each one as an
executable check (start/stop the real TLS listener, read the client's
`base_url`, inspect the enabled-state of the rendered editor).

## Stores

| Store | File / location | Scope |
|---|---|---|
| `ApplicationPreferenceStore` | `<data_dir>/preferences.json` (atomic tmp+fsync+replace) | app-local, per data root |
| `AutomaticBackupPolicy` | `<data_dir>/automatic-backup-policy.json` | app-local, per data root |
| `PersistedWindowState` | `<data_dir>/window-state/<project_ref>.json` + legacy `window-state.json` | per-project, global fallback |
| `FileDialogMemoryStore` | `<data_dir>/file_dialog_dirs.json` | app-local, per data root |
| `capture_receiver_config` row | project SQLite DB | operational record (actual state), not policy |
| Safe-mode policy | launch record (`startup_recovery`) | per-launch, not user-editable |

No `QSettings` anywhere — all persistence is the JSON stores above.

## Setting → store → consumer → verdict

### `preferences.json` keys (Settings > 環境設定)

| Key | Consumer | Verdict |
|---|---|---|
| `general.language` | `LocalizationService` (help topics, help palette, retry affordance) via `_language_policy`/`_presentation_locale` | **was wrongly 準備中** — consumer exists; `restart_required=True` honestly marks apply timing. Fixed: un-pended. |
| `general.startup_destination` | none — launch always reopens last-opened project | dead; stays disabled 準備中 (landing-without-project path doesn't exist) |
| `general.reopen_last_project` | none — `resolve_startup_document` always takes recents[0]; `skip_last_opened` is the *safe-mode* suspect skip, not this policy | dead; stays disabled 準備中 |
| `display_input.length_unit` | `LengthDisplayPolicy` → inspector + measure panel, live rebound on commit | live |
| `display_input.numeric_precision` | same policy (`decimals`) | live |
| `display_input.angle_unit` | none — domain is degree-only | dead; stays disabled 準備中 |
| `display_input.theme` | none — `ui_theme` ships only `DARK_THEME`; no light palette exists | dead; stays disabled 準備中 |
| `display_input.reduced_motion` | none | dead; stays disabled 準備中 |
| `display_input.high_contrast` | none | dead; stays disabled 準備中 |
| `integrations.rew_host` | `rew_api_base_url()` — **was dead code, never called**; all three `RewApiClient()` sites hardcoded the default | **was dead** — wired into measurement + optimization controllers (constructor + live `base_url` rebind); definition tightened to the consumer's loopback-only contract |
| `integrations.rew_port` | same | **was dead** — same fix; `min_value` 1→1024 to match `validate_rew_api_url` |
| `integrations.capture_receiver_enabled` | `CaptureReceiverController` — **but only via its own `set_enabled`**: a raw `store.set` (the 環境設定 checkbox) persisted the flag while the receiver kept its old live state; and in the other direction, a キャプチャ-side flip left the 環境設定 checkbox stale (no store subscription on the widget) | **was half-dead cross-surface** — controller now subscribes to the store (apply live from either surface) and PreferencesWidget mirrors external writes |
| `compute.preferred_backend` | none | dead; stays disabled 準備中 |
| `compute.max_concurrency` | none | dead; stays disabled 準備中 |
| `compute.scratch_dir` | none | dead; stays disabled 準備中 |
| `compute.storage_ceiling_mb` | none | dead; stays disabled 準備中 |
| `files.export_dir` | `_default_export_dir` → export/save-dialog default | live |
| `files.portable_bundle_include_libraries` | none — `project_bundle` has no reader | dead; stays disabled 準備中 |
| `diagnostics.include_project_ids` | `DiagnosticPackageBuilder.plan(include_project_ids=…)` | live |

### Second store — 自動バックアップ (`automatic-backup-policy.json`)

| Control | Consumer | Verdict |
|---|---|---|
| 有効 checkbox | `AutomaticBackupScheduler` (`evaluate` at launch tick) | live |
| 作成間隔 spin (0.5–2160h) | same (`interval_hours`) | live |
| 保持世代数 spin (1–100) | `prune_generations` | live |
| 日次保持 spin (0–366) | same | live |
| 保存先 変更… button | `backups_dir(policy)` | live |
| Apply timing | label + status message state "次回の起動時チェックから適用" — honest, no fake live-apply claim | honest |

### Settings > キャプチャ

| Control | Consumer | Verdict |
|---|---|---|
| 有効/無効 combo + 適用 | `controller.set_enabled` → pref + live start/stop | live (and now shared with 環境設定 via the subscription) |
| ポート spin + ポートを変更 | `service.set_port` → durable port, live rebind while running | live |
| デバイスをペアリング | `begin_pairing`/`confirm_pairing` | live |

### Settings > 保持管理

Per-revision picker + dry-run + confirmed purge (`CaptureRetentionService`).
Action surface, not a persisted setting — each click has an effect; live.

### Window state (`window-state/<ref>.json`)

geometry / workspace / selected contexts, per-project wins with legacy
global fallback; unknown destinations pruned; Safe Mode neither reads nor
writes. Save on every committed close; restore at launch. Live — verified
by existing `test_review_round9_prefs.py` suite.

### File-dialog memory (`file_dialog_dirs.json`)

Per-dialog-key last directory, GLOBAL_KEY fallback, nonexistent dirs
skipped, ephemeral under Safe Mode. Live.

## Findings fixed in this round

1. **`capture_receiver_enabled` cross-surface divergence** — two editors
   wrote the same key through different paths; only one applied. The
   controller now subscribes to the store, so the store is the single
   write path and both surfaces stay in sync with the live service. A
   `_shutdown_requested` flag prevents a post-exit write resurrecting the
   socket; `_apply_requested`/`_stop` never raise inside a store
   notification.
2. **`integrations.rew_host`/`rew_port` dead options** — `rew_api_base_url`
   existed precisely for this but was never called. The workflow
   composition now builds `RewApiClient(rew_api_base_url())` for the
   measurement and optimization controllers (`rew_client` injection param
   added where missing) and rebinds `base_url` live on endpoint changes.
   Definitions tightened to the consumer's contract: host enum
   `{127.0.0.1, localhost}` (the API is loopback-only by
   `validate_rew_api_url`), port min 1024. A legacy persisted value that
   no longer validates is dropped to default with the existing honest
   load-error banner.
3. **`general.language` stale 準備中** — the LocalizationService already
   consumes it (help topics, help palette, retry label); un-pended.
   `restart_required=True` continues to mark the apply timing honestly.

## Verified live (code + tests)

- `display_input.length_unit`/`numeric_precision`: `subscribe`-based live
  rebind into inspector and measure panel — observable via
  `length_display_policy_from_preferences`.
- `diagnostics.include_project_ids`, `files.export_dir`: consumed by the
  diagnostics builder and export dialogs respectively.
- Backup-policy controls: write → `save_policy` → scheduler reads at the
  next launch tick; UI text discloses the timing.

## Still pending by design (rendered disabled + 準備中)

`general.startup_destination`, `general.reopen_last_project`,
`display_input.angle_unit`, `display_input.theme`,
`display_input.reduced_motion`, `display_input.high_contrast`,
`compute.*` (4 keys), `files.portable_bundle_include_libraries`.
Each genuinely has no consumer — the honest-disabled treatment is the
correct interim state.

## Out of scope / documented surfaces

- FastAPI server REW endpoint: `HTDT_REW_API_URL` env var in `main.py` —
  a server-side launch config, deliberately separate from app prefs.
- `measurement_editor.py` legacy window (`--legacy-ui`): composes its own
  `RewApiClient()` default — a deprecated surface, not wired to prefs.
- Developer mode: `HTDT_DEVELOPER_MODE` env var — launch flag, not a
  settings control.
- SPA frontend (`frontend/`): no settings UI — only attachment-kind
  selects (domain data) and an OS `prefers-color-scheme` media query.

## Tests

New: `backend/tests/test_review_round13_settings.py` (12 tests) —
cross-surface apply both directions, shutdown guard, endpoint wiring +
live rebind, editor enabled-state, definition↔consumer bound checks,
legacy-value drop.

Adjusted: `test_application_preferences.py` (value `1` no longer a valid
`rew_port` — the atomic-persist invariant is exercised with `9000`) and
`test_optimization_workflow_workspace.py` (bare-object fixture gains the
`preferences` attribute `_make_rew_client` now legitimately needs).
