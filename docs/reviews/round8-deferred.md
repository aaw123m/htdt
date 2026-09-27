# Round 8 — deferred-sketch implementation

Scope: the deferred tables of `round6-features.md`, `round6-ux.md`,
`round7-workflow.md`, `round7-frontend.md`. Picked the top items by
operator value that were implementable without product decisions;
each landed item has a regression test under `backend/tests/test_review_round8_*`.

## Outcomes

| Item (source) | Outcome | Notes |
|---|---|---|
| QFileDialog last-directory memory (`round6-ux.md`) | **implemented** | New `file_dialog_memory.py`: `FileDialogMemoryStore` (JSON, atomic write, corrupt→empty) + module-level `configure`/`active_store` + `get_open_file_name(s)`/`get_save_file_name`/`get_existing_directory` wrappers keyed by caller-supplied dialog keys. 19 call sites migrated across `data_management_ui.py`, `equipment_library.py`, `measurement_editor.py`, `measurement_page_workspace.py`, `room_workspace.py`, `standards_profile_editor.py`, `workflow_application.py`; `test_bounded_ingress.py` monkeypatches retargeted to the wrapper module. `files.export_dir` moved from `PENDING_PREFERENCE_KEYS` to live (feeds the export-dialog default). |
| measurement_playback_safety (`round6-features.md`) | **still-blocked** | No production playback/output trigger exists to gate on: REW owns acquisition, device adapters are test-only, so there is no read-back source a preflight could verify. Needs a product decision on which surface (sweep start? export?) owns the check before code can land. |
| capture_retention (`round6-features.md`) | **implemented** | `capture_retention.py` gained `list_capture_revisions()` + `CaptureRevisionListing`; new `capture_retention_ui.py` `RetentionPolicyWidget` (inventory label, revision combo, two-step plan→confirm purge behind a warning dialog, JP error strings, disarms on selection change). Mounted as the "保持管理" tab in `DataManagementDialog` (`retention_panel` param) + `open_retention_settings()`. |
| reference_libraries (`round6-features.md`) | **implemented** | New `reference_library_sources.py`: `_ListingProvider` adapters mapping equipment definitions + speakers (EQUIPMENT), materials (MATERIAL), standards profiles (STANDARD_PROFILE) onto `LibraryEntry`/`LibraryProvider`; `build_reference_library_index(repository, data_dir)` with scope mapping (manufacturer→BUILTIN, `document_id`→PROJECT_LOCAL, published→BUILTIN). `ReferenceLibraryPage` gained `library_index` param + per-family sections (名前/区分/スコープ/バージョン). |
| Palette providers for measurement/variant/revision/inbox (`round7-workflow.md`) | **implemented** | `palette_search.py` gained `PaletteNavigationItem` + `NavigationItemPaletteProvider` (deep-link handoff through `on_deep_link`); `_build_palette_service` registers providers for measurements, scene revisions, system variants, and capture-inbox items. Measurement mount got `focus_kinds={MEASUREMENT}` + `focus_target` via new `select_measurement_id()`. |
| Commissioning-wizard modal link focus (`round7-workflow.md`) | **implemented** | `CommissioningWizard` queues deep links while modal (`_queued_links` + `take_pending_navigations()`); the composition drains the queue after the dialog closes so navigation lands on the workspace instead of behind the modal. |
| `format_measure_result` display-policy wiring (`round6-features.md` D3) | **implemented** | `format_measure_result(result, *, policy=None)` — `None`/`m` preserves byte-identical legacy SI output (so exports/reports are untouched); other units render via `si_to_display` + `_UNIT_SUFFIX`, angles always degrees. `RoomMeasurePanel` gained `display_policy_provider`/`set_display_policy_provider`; wired in `_make_room` to `length_display_policy_from_preferences` so the readout and clipboard follow `display_input.*` live. |
| authority_graph wiring (`round6-features.md`, ~250+150 sketch) | **implemented** | Two new canonical-source adapters in `authority_graph.py`: `measurement_authority_source` (MEASURED_FOR edge to scene revision, stale when `scene_content_hash` ≠ head hash) and `system_variant_authority_source` (DERIVED_FROM baseline, SUPERSEDES parent variant, stale on baseline-hash drift). New `authority_inspector_ui.py` `AuthorityInspectorDialog` (node picker, summary/freshness/evidence, upstream/downstream, stale reasons, "ワークスペースで開く" deep-link button). Entry: Support page "権威グラフを開く" button → `_open_authority_inspector` builds the graph live from repositories. Read-only, never persisted. |

## Files changed

`backend/src/htdt/`: `authority_graph.py`, `authority_inspector_ui.py` (new),
`application_pages.py`, `application_preferences.py`, `cad_measure.py`,
`capture_retention.py`, `capture_retention_ui.py` (new),
`commissioning_wizard.py`, `data_management_ui.py`, `equipment_library.py`,
`file_dialog_memory.py` (new), `measurement_editor.py`,
`measurement_page_workspace.py`, `palette_search.py`,
`reference_library_sources.py` (new), `room_measure_input.py`,
`room_workspace.py`, `standards_profile_editor.py`,
`workflow_application.py`, `workflow_settings.py`.

`backend/tests/`: `test_review_round8_authority_graph.py` (new),
`test_review_round8_capture_retention_ui.py` (new),
`test_review_round8_file_dialog_memory.py` (new),
`test_review_round8_measure_display.py` (new),
`test_review_round8_palette_providers.py` (new),
`test_review_round8_reference_library.py` (new),
`test_review_round8_wizard_navigation.py` (new),
`test_bounded_ingress.py` (monkeypatch retarget).

## Tests

`cd backend && TMPDIR=/c/t C:/devin/python/python.exe -m pytest -q -n 4` —
**0 failures** across the full suite (~498 test files); the two flaky
round-7 failures passed this run. New round-8 suites: 50 tests
(file-dialog memory 14, wizard navigation 3, palette providers 9,
capture-retention UI 6, reference library 4, measure display 6,
authority graph 4 + dialog).
