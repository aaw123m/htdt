# Round 6 — Feature Gaps / Completeness

Dimension: capabilities the codebase already pays for but doesn't deliver —
test-only modules, dead UI ends, docs-promised features, missing low-cost
capabilities. Prior rounds left ~140 modules exercised only by tests; this
round took the top ones by user value and either wired them or wrote a
concrete deferred sketch.

## Wired this round

| Module / feature | Prior status | Action taken |
|---|---|---|
| `help_registry` (#623) | test-only; palette had 2 static help rows | `HelpTopicPaletteProvider` indexes the shipped bilingual registry (JA + EN + keyword aliases) in the command palette; `_open_help_topic` falls back to `HelpDialog.topic()` rendering the topic's localized summary + sections. `SupportPage` HELP_TOPIC focus now resolves every registry topic, not just the two static ones. |
| `activity_center` (#603) | test-only; operations invisible app-wide | `WorkflowApplicationComposition` owns an `ActivityCenter`. `DataManagementController` mirrors every backup/restore/relocate/scan/GC operation (submit → running → indeterminate progress → complete/fail, incl. pre-start lifecycle failures) so activity is visible outside the settings dialog. Terminal ops persist to `data_dir/activity_history.json` for next-session history + diagnostics. |
| `cad_project_activity` + `cad_project_activity_repository` (#772) | test-only; Activity page showed only raw revisions | Activity destination now shows three sections: live/recent app operations (from the ActivityCenter), a projected project timeline (revisions, variants, captures, measurements, calibrations, checkpoints, presets, health runs, AV-sync, notes — all 10 sources wired), and the raw revision ledger. Timeline rows double-click-navigate through their `htdt://nav/v1/...` deep links. |
| `DiagnosticPackageBuilder` (#604) + `diagnostics.include_project_ids` pref | builder test-only; preference write-only | Support page gains "診断パッケージをエクスポート": health checks + operation failures (live center + persisted history) + preferences summary + (opt-in) project ids → bounded ZIP via `QFileDialog`. |
| write-only preferences (15 keys) | settings dialog showed dead controls | `PENDING_PREFERENCE_KEYS` in `application_preferences.py`; `PreferencesWidget` renders them disabled with a "（準備中）" label and explanatory tooltip instead of letting a dead control pretend to work. Keys still load/persist — forward-compatible. Remove a key when its consumer lands. |

Files changed:
`src/htdt/palette_search.py`, `src/htdt/workflow_help.py`,
`src/htdt/application_pages.py`, `src/htdt/data_management.py`,
`src/htdt/workflow_application.py`, `src/htdt/application_preferences.py`,
`src/htdt/workflow_settings.py`, `tests/test_review_round6_features.py`.

## Preference consumer audit (dead-setting check)

| Key | Consumer | Status after this round |
|---|---|---|
| `display_input.length_unit`, `display_input.numeric_precision` | `cad_display_units` | live (unchanged) |
| `integrations.capture_receiver_enabled` | `CaptureReceiverController` | live (unchanged) |
| `diagnostics.include_project_ids` | diagnostic package export | **wired** |
| `general.language` | none — `detect_system_locale` reads env only | pending (marked) |
| `general.startup_destination`, `general.reopen_last_project` | none — `resolve_startup_document` always opens most-recent | pending (marked) |
| `display_input.angle_unit` | none — `cad_display_units` covers length/precision only | pending (marked) |
| `display_input.theme`, `.reduced_motion`, `.high_contrast` | none — app is hardcoded `apply_dark_theme` | pending (marked) |
| `integrations.rew_host` / `rew_port` | `rew_api_base_url()` exists but has no caller (REW import lives in the legacy FastAPI server via env var) | pending (marked) |
| `compute.*` (4 keys) | none — native compute paths don't read them | pending (marked) |
| `files.export_dir` | none — export dialogs use default dirs | pending (marked) |
| `files.portable_bundle_include_libraries` | none — bundle export has no library-toggle | pending (marked) |

## Deferred (concrete integration sketches)

| Module / feature | Status | Entry point & sketch | Est. diff |
|---|---|---|---|
| `authority_graph` (#619) | partial — only `scene_revision_authority_source` exists; no second producer | `AuthorityInspectorDialog` off the Support page or a "係統" tab on Room history. Entry: `WorkflowApplicationComposition` builds `build_authority_graph([scene_revision_authority_source(revisions, head_map)])`; render `inspector.summary()` + `lineage_view(node)` + `why_stale()`. Needs ≥1 more `AuthoritySource` adapter (e.g. measurement/variant authorities) before the view carries information the revisions list doesn't already show — that adapter is the real work, the dialog is thin. | ~250 lines + adapter ~150 |
| `reference_libraries` (#630) | unwired hub — aggregates equipment/source/speaker-definition libraries | `ReferenceLibraryPage` currently lists only `EquipmentLibraryService.definitions`. Wire `ReferenceLibraryHub.list_families()` as additional sections/tables on the same page (same factory `_make_library`). Check `hub.families()` shape first; low risk, additive rows. | ~120 lines |
| `capture_retention` (#620) | unwired | Data Management dialog third tab: `RetentionPolicyWidget` listing `capture_retention` policies + "dry-run" button showing `plan_retention(...)` counts. Backend is pure/deterministic; UI is a table + confirm. | ~180 lines |
| `measurement_playback_safety` (#634) | unwired | Sweep-start preflight in `MeasurementWorkflowController` (or the measurement workspace's play button handler): call the safety evaluator before arming output; surface `verdict.message` in a dialog on WARN/BLOCK. Needs the app's actual playback entry to call it — find where sweep playback is triggered (measurement workspace controller). | ~80 lines + wiring |
| `cad_ir_analysis`, `cad_fir_filter`, `cad_auralization` (+`cad_auralization_player`) | unwired — measurement post-processing & auditioning | Measurement workspace: an "IR解析" tab on a selected measurement (impulse metrics) and an "オーラライゼーション" preview panel using the player. Verify analysis entry points return domain models the panels can render without computing on the UI thread (worker + ActivityCenter op, now available). | ~300–500 lines each |
| `cad_camilladsp`, `cad_pjlink`, `cad_hue_lighting`, `cad_yamaha_rxa4a`, `cad_cec_adapter` | unwired device/adaptor layers | Integration settings pages (per-device host/port + test-connection) plus calibration-delivery export for CamillaDSP (from `CadCalibrationRepository.list_exports`). Requires target hardware/endpoints to validate — code-only wiring is untestable without a fake adapter surface. | ~200–400 lines each |
| `project_performance` | headless by design | Dev-only benchmark harness; no user surface exists for it. Keep test-only; document in module docstring that it's a benchmarking tool, not a product feature. | 0 (doc) |
| `native_accessibility` | contract-only | The module defines the a11y contract (focus order, screen-reader labels) without widget wiring. Sweep `setAccessibleName`/`setAccessibleDescription` across the shell in a dedicated pass — too broad for this round's wiring scope. | ~200 lines sweep |
| `workflow_legacy_bridge` | dead | Legacy embed path is gone (`--legacy-ui` opens `OptimizationWorkspaceWindow` directly; bridge has no callers). Candidate for deletion in a cleanup round — verify no packager references it first. | −1 file |
| `__main__`, `server` | not dead | Entry points: `__main__.py` launches the native app; `server.py` is referenced via uvicorn string `htdt.server:app`. Census artifacts, not gaps. | 0 |
| ~120 remaining test-only modules | catalogued | Mostly repository/service layers whose only caller is another test-only module (domain machinery staged ahead of its UI). Per-module disposition needs a product decision on which domain features ship next — outside a wiring pass. | — |

## Docs-promised check

`docs/IMPLEMENTATION_STATUS.md` outstanding items are gate-level standards work
(DTS:X criteria, O70 auto-recommendation, wave/portal splits) — none are cheap
finishes; left untouched. Command-registry audit found all 66 registered
`command_id`s bound — no dead palette actions.

## Tests

`tests/test_review_round6_features.py` — 10 tests covering the provider,
topic dialog, ActivityCenter mirroring (running + immediate-failure paths),
timeline rendering + deep-link dispatch, and pending-preference rendering.

Results: new file 10/10; all touched-module test files
(`test_palette_search`, `test_workflow_help`, `test_application_pages`,
`test_data_management{,_ui}`, `test_activity_center`,
`test_cad_project_activity`, `test_help_registry`,
`test_support_diagnostics`, `test_ia_v2_integration`) — 99 passed.
Full suite `TMPDIR=/c/t C:/devin/python/python.exe -m pytest -q -n 4` ran to
completion; the run flagged
`test_cad_hybrid_prediction_provider.py::test_evidence_lifecycle_rejects_illegal_promotions`,
which passes in isolation and on re-run (transient under `-n 4`; untouched
module — hybrid prediction provider, no shared state with this diff).
