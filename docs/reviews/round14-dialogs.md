# Round 14 — Dialog & Form Honesty

Scope: every `QDialog` (plus `QInputDialog`/`QMessageBox` call sites) in `backend/src/htdt`. Each was instantiated offscreen and *driven* — fields filled, OK/Cancel clicked, produced values read back — so every verdict below is observed behavior, not a code-read guess. New coverage: `backend/tests/test_round14_dialogs.py` (33 tests).

## Verdicts (fixed this round)

| Dialog | Bug found (observed) | Fix | Verified |
|---|---|---|---|
| `MaterialDialog` (room_acoustics_panel) | Empty 名称/出典 → OK did nothing with no message; unsupported+unsupported defaults closed then failed post-close; table/banded data mismatch lost the form | `accept()` warns in-dialog for missing 名称/出典, both-models-unsupported, table-without-impedance, impedance-on-non-table, banded-without-bands, bands-on-non-banded | Drive tests pass; form survives the warning |
| `TreatmentDefinitionDialog` (same) | Same silent-OK pattern on 名称/層1 材質 | In-dialog warnings + focus | ✓ |
| `ProjectorSpecDialog` (room_video_panel) | No `accept()` at all — empty 仕様ID, inverted スロー比, inverted enabled lens-shift ranges all accepted | `accept()` validates spec_id + throw/shift min≤max | ✓ |
| `ScreenTransferDialog` (same) | Silent OK on empty label/provenance; tier/sample combinations the authority validator rejects were accepted then lost | In-dialog blocks: missing fields, `_SAMPLED_TIERS` without samples, UNKNOWN/AT_CLAIM with samples, inverted freq range, MEASURED_DATASET (needs dataset ref the form lacks — says so) | ✓ |
| `DisplaySpecDialog` (same) | Silent OK on empty 仕様ID/表示名 | In-dialog warnings | ✓ |
| `EnvironmentProfileDialog` (room_prediction) | Silent focus-only reject on empty label; −40 °C displayed 「不明」 but wrote `temperature_c=-40.0` (displayed ≠ applied) | Warning + removed the misleading sentinel text | ✓ |
| `SeatingLayoutDialog` (room_workspace) | Aisle syntax validated post-close in `spec()` → a typo destroyed the whole form; heterogeneous existing rows silently flattened to row-0 values | `accept()` calls `_parse_aisles()` while open (warn_user + stay); heterogeneity warning label shown on load | ✓ |
| `_MeasurementPlanDialog` (system_expansion_widgets) | No accept validation — empty sources/role/repeatability<2 closed then failed in the service | `accept()` mirrors the service's exact JP messages | ✓ |
| `StandardsProfileEditorDialog` | Sentinel bug: `!= maximum()` made untouched max spin send a real bound; worse, fresh spins showed `0.00` (Qt default) so `min`/`equals` rules ALWAYS carried a phantom `maximum=0.0` and could never apply on a fresh form | Bounds now gated by operator (min reads min, max reads max, range both, equals neither) + fresh spins initialized to the なし sentinel | ✓ |
| `PlaybackChainDialog` amp gain | −40 dB displayed 「不明」 but saved as a real bound (the special-value text sat at the wrong end of the range — the working sentinel is 0→None, documented on the row label) | Removed the misleading `setSpecialValueText`; 0 stays the documented unknown sentinel | ✓ |
| `PairingDialog` (capture_receiver_settings) | Empty confirm code bypassed the check → `confirm_pairing` activated without verification | Empty → 「確認コードを入力してください。」 blocks; mismatch unchanged | ✓ |
| `CommissioningWizard` | Typing an EXISTING project name under 「新規作成」 silently attached that document to the plan | Name collision → warning + back to page 0 | ✓ |
| `_record_as_built` attester prompt | Blank recorder name → silent abort (service's own 「記録者名を入力してください」 never reachable) | Empty shows that JP message | ✓ |
| `_new_project` / `_rename_project` / `_duplicate_project` / `_export_analysis_bundle` (workflow_application) | OK with empty name silently returned (indistinguishable from Cancel) | Empty-on-OK → JP warning; cancel still silent | ✓ |
| `save_named_view` / seat-pose save (room_workspace) | Same silent empty-OK | Inline status error instead of a modal (panel convention) | ✓ |
| `_default_restore_confirmation` (data_management_ui) | Text said 「このバックアップ」 without naming which | Names `backup_path.name` + creation UTC | ✓ |
| `_confirm_and_purge` (capture_retention_ui) | Purge box did not name the revision being destroyed | Text now includes the revision id | ✓ |

## Verified honest — no change needed

`GeometryImportDialog` (reference implementation: in-dialog JP + `SemanticState.ERROR`), `EquipmentLibraryDialog` (required-field marks, ValueError→warning, success info), `DataManagementDialog` (`before_deactivate` close guard), `resolve_mount_dirty_state`/`choose_snapshot_action` (explicit choices, destructive marked, cancel→None), `_choose_project`/`_pick_one` (row-0 preselected), measurement correction/disposition reason prompts (show 「理由が必要です」), destructive confirms that DO name targets (relocate, project delete, GC, divergence, duplicate measurement), read-only dialogs (Help/Deliverables/AuthorityInspector/CommandPalette), `PreferencesWidget`. Cancel-path coverage: Material/Seating/Environment/Standards/PlaybackChain/wizard cancel all verified to persist nothing.

## Deferred / noted

- `SeatingLayoutDialog` can only author uniform rows — heterogeneity is now *warned* on load rather than silently flattened, but the dialog still can't edit per-row values (by design; uniform-only authoring).
- `ScreenTransferDialog` cannot author `MEASURED_DATASET` (no dataset-ref field) — now says so instead of accepting an unbuildable tier.
- Long-running accepts: no dialog starts async work on OK (execution always happens in the caller post-close), so no reentrancy guards were needed inside dialogs; destructive confirms already default to the safe button.
