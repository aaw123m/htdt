# Round 22 — Dialog & widget-state truth

Scope: every modal dialog and complex widget in the app, audited for honest
interaction state — not visual polish. Eight dimensions, each verified by
instantiating the widget offscreen (`QT_QPA_PLATFORM=offscreen`) and driving
it like a user: rows selected, buttons clicked, enabled state and side
effects read back. No verdict rests on code reading alone.

1. **OK-ENABLED-INVALID** — action buttons enabled over a form state the
   handler will reject or silently no-op (dead click or late error vs
   upfront gating).
2. **DEFAULTS** — default field values that would create nonsense.
3. **STATE PERSISTENCE** — dialogs reopen with the user's last values where
   they should, and never with another project's state.
4. **DESTRUCTIVE CONFIRMS** — destructive actions confirm; the confirm names
   the affected items honestly; Cancel truly cancels.
5. **TAB ORDER** — focus order, Enter→accept, Esc→cancel-without-commit.
6. **MULTI-OPEN** — same dialog open twice: independent state.
7. **VALIDATION MESSAGING** — error names the field, the constraint, and a
   fix hint.
8. **RESIZE/min-size** — dialogs that clip content at default size or shrink
   below content needs.

Dialogs exercised: `PairingDialog`, `CommissioningWizard`,
`EquipmentLibraryDialog`, `StandardsProfileEditorDialog`,
`_MeasurementPlanDialog`, `GeometryImportDialog`, `PlaybackChainDialog`,
`DeliverablesDialog`, `MaterialDialog`, `TreatmentDefinitionDialog`,
`EnvironmentProfileDialog`, `ScreenTransferDialog`, `DisplaySpecDialog`,
`ProjectorSpecDialog`, `SeatingLayoutDialog`, `AuthorityInspectorDialog`,
`HelpDialog`, `DataManagementDialog`, `CommandPalette`, plus the panel-level
action surfaces (joint optimization, robustness authoring, intervention
planner, measurement editor, capture receiver, retention, system
expansion).

Branch `devin/rev22-dialog`. Tests:
`backend/tests/test_round22_dialog_truth.py` (7 cases).

## Verdict table

| # | Dimension / finding | Severity | Verdict |
|---|--------------------|----------|---------|
| 1 | **DESTRUCTIVE CONFIRMS** — `PairingDialog._revoke_selected` called `service.revoke_pairing()` with zero confirmation. Every other destructive op in the app (project delete, backup restore, relocation, capture purge, as-built record, orphan-asset GC) confirms naming the affected items | **HIGH** — one click revokes a paired device permanently | FIXED — warning box naming the device (`receiver_instance_id` + state + scope label as shown in the list), `Yes|Cancel` with Cancel default; verified Cancel never calls `revoke_pairing`, Yes revokes exactly the selected `pairing_id` |
| 2 | **OK-ENABLED-INVALID** — `EquipmentLibraryDialog`: `save_version_button` / `import_button` enabled whenever the selection slot never fired (empty library at open, or selection cleared by refresh). Handlers `return` silently on `definition is None` → dead click, zero feedback | **MED** — dead click on enabled buttons | FIXED — `refresh_definitions()` now evaluates `_selection_changed(currentRow())` unconditionally after repopulating, so button state always tracks real selection; verified buttons gate off when empty, arm on row selection, re-gate on emptying refresh |
| 3 | **OK-ENABLED-INVALID** — `CommissioningWizard`: 「保存して閉じる」always enabled; empty プロジェクト名 in new-project mode produced a late `QMessageBox.warning` and a forced jump back to page 0 | **MED** — late error vs upfront gating | FIXED — `_update_save_enabled` gates Save on (new ⇒ non-blank name) / (existing ⇒ selected document), driven by `textChanged`/`toggled`/`currentIndexChanged`; verified disabled→enabled transitions both directions. Late validation kept as the safety net |
| 4 | **OK-ENABLED-INVALID** — `StandardsProfileEditorDialog._remove_criterion` silently no-ops (`if 0 <= row < len`) while 基準を削除 stays enabled with nothing selected | LOW — dead click | FIXED — button starts disabled, arms via `itemSelectionChanged` only when `0 <= row < len(_criteria)`, re-gates after `_refresh_criteria_table` (selection gone post-mutation) |
| 5 | **DEFAULTS** — field defaults across all form dialogs | — | VERIFIED OK — `SeatingLayoutDialog` empty name falls back to 「座席ブロック」; all spec dialogs carry sane physical defaults (screen gain 1.0, projector lumens ranges, aisle ≥ 0.9 m); `GeometryImportDialog` refuses submit until a real unit is declared (「（単位を選択してください）」`None`-item); `PlaybackChainDialog` documents 0 dB = 不明扱い sentinel |
| 6 | **STATE PERSISTENCE** — stale state across reopens / cross-project leaks | — | VERIFIED OK — dialogs are per-open instances (no singletons except `CommandPalette`, whose `prepare_to_show` clears the search field); `SeatingLayoutDialog(existing)` repopulates from the current document (round-14 roundtrip test); `DataManagementDialog` is per-shell and the shell is rebuilt on project switch — verified by construction + `test_project_switch_lifecycle.py` |
| 7 | **DESTRUCTIVE CONFIRMS — sweep** — every `clicked.connect` into delete/remove/revoke/clear/purge handlers across UI files | — | VERIFIED OK elsewhere — `_default_relocate_confirmation`/`_default_restore_confirmation` name bytes+counts; project delete names the project and offers safety backup; capture purge names revision+count (`test_capture_purge_confirm_names_revision`); entity/wall/opening deletes are undoable controller ops (status 「元に戻せます」), correctly confirm-free |
| 8 | **TAB ORDER / Enter / Esc** | — | VERIFIED OK — round-14 suite covers Enter→accept, Esc→reject-without-commit for every form dialog; `CommandPalette` Esc hides (and `prepare_to_show` clears stale text on reopen — verified); `DataManagementDialog.closeEvent` honors `component.before_deactivate` |
| 9 | **MULTI-OPEN** — same dialog instantiated twice concurrently | — | VERIFIED OK — `MaterialDialog` pair driven programmatically shows independent state (label set on one, empty on the other); `PairingDialog`/`EquipmentLibraryDialog` hold per-instance `_pairing`/form state; the list content is fetched per instance from the service — no shared-widget corruption possible |
| 10 | **VALIDATION MESSAGING** | — | VERIFIED OK — accept-time warnings name the field and the fix (「プロジェクト名を入力してください。」, 「ラベルを入力してください。」, unit declaration refusal, name-collision text offering the alternate path); service rejections surface via `warn_user`/`operation_error_message` |
| 11 | **RESIZE/min-size** — `sizeHint` vs `minimumSizeHint` vs enforced `minimumSize` for 9 dialogs | — | VERIFIED OK — every dialog's enforced minimum equals its layout `minimumSizeHint` (Qt applies it); content fits at defaults; none clips required content |

## Fixed

### F1 — `PairingDialog._revoke_selected` had no confirmation (HIGH)

`_revoke_selected` went straight to `service.revoke_pairing(pairing_id)` —
one click on an enabled button permanently revoked a paired device, the
only unconfirmed destructive action in the app. Verified offscreen:
clicking 解除 with a row selected produced no `QMessageBox` at all while
`revoke_pairing` fired.

Fix in `capture_receiver_settings.py`: a `QMessageBox` (Warning icon) that
names the device exactly as the list shows it (`instance-id prefix · state
· scope`), explains the consequence (「このデバイスからの新しい取り込みは
受け付けなくなります」), `Yes|Cancel` with Cancel the default — the same
pattern as `_default_relocate_confirmation`. Cancel never reaches the
service.

Tests: `test_pairing_revoke_confirms_and_cancel_preserves`,
`test_pairing_revoke_confirmed_executes`.

### F2 — `EquipmentLibraryDialog` selection buttons enabled over no selection (MED)

`save_version_button` / `import_button` enablement updated only via
`currentRowChanged`, which never fires for an empty list — so both buttons
sat enabled when `service.definitions()` was empty (or whenever the current
row was lost), and the handlers' `if ... is None: return` made the click a
silent no-op. Verified offscreen: empty library → both buttons enabled →
click → nothing happens, no message.

Fix in `equipment_library.py`: `refresh_definitions()` now calls
`_selection_changed(currentRow())` unconditionally after repopulating, and
`_selection_changed` clears `preview_label` when no definition is selected
(a stale preview would misrepresent the current selection).

Tests: `test_equipment_library_selection_actions_disabled_when_empty`,
`test_equipment_library_actions_track_selection`.

### F3 — `CommissioningWizard` Save enabled over unsavable intent (MED)

「保存して閉じる」stayed enabled with an empty プロジェクト名 in
new-project mode; the click surfaced a late warning and yanked the user
back to page 0 — the late-error half of the OK-ENABLED-INVALID class, and
the only wizard in the app without upfront gating.

Fix in `commissioning_wizard.py`: `_update_save_enabled` runs on
`name_edit.textChanged`, both radio `toggled`, and
`existing_combo.currentIndexChanged`, and once after `_build_pages()`; Save
requires a non-blank name in new-project mode or a selected document in
existing mode. `_resolve_document_id`'s validation remains as the last
line of defense (name collision with an existing project can't be gated
upfront — it needs the repository lookup).

Tests: `test_wizard_save_gated_on_required_inputs`,
`test_wizard_save_enabled_for_existing_project`.

### F4 — `StandardsProfileEditorDialog._remove_criterion` dead click (LOW)

基準を削除 stayed enabled with no row selected; the handler's
`if 0 <= row < len(self._criteria)` guard made it a silent no-op —
the same class as F2.

Fix in `standards_profile_editor.py`: the button starts disabled and its
state is owned by `_criterion_selection_changed` (armed only when
`0 <= row < len(_criteria)`) plus an explicit re-gate inside
`_refresh_criteria_table` (mutations drop the selection).

Test: `test_standards_remove_criterion_gated_on_selection`.

## Notes / deferred

- `PairingDialog.revoke_button` stays enabled with no selection but
  clicking it posts status feedback (「解除するデバイスを選択してください。」)
  — the codebase's accepted convention for status-line widgets; not a dead
  click. Left as-is.
- `WorkflowApplication._pick_one` returns `None` silently when a pick list
  is empty; callers guard with an information box (「対象のプロジェクトが
  ありません」) — acceptable.
- `SeatingLayoutDialog` leaves anchor at 0,0 when `document.room` is `None`
  — defensible: placement anchors need a room; the dialog is only reachable
  in a room context.
