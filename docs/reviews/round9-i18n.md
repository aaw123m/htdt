# Round 9 — Qt UI internationalization & accessibility depth

Scope: every user-facing string in the shipped Qt surface —
`backend/src/htdt/{room_editor,native_editor,wall_editor,theater_editor,
native_cad,native_diagnostics,startup_recovery,launch_intents,launch_router,
workflow_*,measurement_*,optimization_*,prediction_workspace,
field_explorer_panel,standards_*,system_expansion_widgets,room_*,
equipment_library,geometry_import_dialog,data_management_ui,
dirty_state_dialog,capture_retention_ui,constraint_editor,commissioning_wizard,
playback_chain_widgets,intervention_planner_panel,seat_priority_panel,
authority_inspector_ui,room_underlay,room_viewport,native_worker,
theater_workflow,user_facing_error,file_dialog_memory,scientific_plot_style,
ui_theme}.py` — plus an accessibility pass (mnemonics, tab order,
`setAccessibleName`, focus, Esc/Enter defaults).

Method: an AST extractor (`audit_strings.py`, not committed) walked all Qt
files and filtered string literals by call-site context
(`setWindowTitle`/`QLabel`/`addRow`/`showMessage`/`QMessageBox`/
`addItem`/`setAccessibleName`/tree items/file filters/…). Roughly 300
English remnants were reviewed line-by-line before editing.

**String convention (verified, not new):** the product's i18n is hardcoded
Japanese literals. `localization.py` ships an en/ja `LocalizationService`
but no widget calls `tr()` — `test_localization.py` only tests the service
in isolation. This round follows the existing convention (in-place
Japanese); unifying on the service is deferred — see §Deferred.

**Kept English, deliberately:** REW, dB, Hz, SPL, GP, Pareto, MLP, DIP,
FL/FR/etc. channel ids, file-format labels (`JSON (*.json)`), solver/method
names (`wave=fdtd`), enum/role names that double as data (`HEAD`, `ID`),
exception text (never shown raw — see BY DESIGN #12), and diagnostic log
lines (see #11).

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | `room_editor.py` rendered ~60 English strings on canvas overlay, room tree, toolbar actions and all statusBar messages — the known deferred item | HIGH i18n | FIXED |
| 2 | `native_editor.py` base chrome (dock titles Room/Inspector/Objects/Object Details, toolbar Save/Undo/Redo/Delete/Move/Rotate/Show All, snap toggles, scene-view labels Top/Front/Right/Perspective/Fit, object-count status) was English — inherited by every editor window | HIGH i18n | FIXED |
| 3 | `workflow_legacy_bridge._CONTEXT_DOCK_TITLES` looked up 'Room'/'Inspector' while editors create '部屋'/'インスペクター' — context highlighting silently no-opped | MED functional | FIXED |
| 4 | `measurement_workspace._unify_right_context_docks` preferred-titles/anchor used 'Inspector'/'Room' — dock ordering fell through to insertion order after the JP renames | MED functional | FIXED |
| 5 | `native_cad.py` first-run surface: recovery dialog title/body + all 5 choice buttons, launch-intent outcome dialogs, data-update dialogs, launch-failure reports, 'already running' dialog + stderr line, argparse help — all English | HIGH i18n | FIXED |
| 6 | `startup_recovery.decide_launch` reason strings shown in the recovery dialog were English | MED i18n | FIXED |
| 7 | `launch_intents.describe_launch_intent` / `launch_router` `detail=` strings reach QMessageBox — English | MED i18n | FIXED |
| 8 | Workspaces/panels: `measurement_page_workspace` (REW file filters, 'Synthetic fixture', retake reason), `measurement_editor` ('Synthetic', source/quality labels), `prediction_workspace` (tree columns, run/freshness detail), `optimization_workspace` (headers, Pareto columns, fidelity combos, GP rows), `optimization_{adaptive,adaptive_extended,extended,robustness}_controller` (detail-block lines), `field_explorer_panel`, `standards_workspace` (evidence labels), `system_expansion_widgets`, `room_workspace` (reasons/provenance), `room_video_panel` (PASS/FAIL display + verdict counting), `room_prediction` ('Advanced · provenance'), `room_acoustics_panel` ('wave/geometric capability'), `equipment_library`, `geometry_import_dialog`, `data_management_ui` ('DB schema'), `intervention_planner_panel` ('stale proposal'), `playback_chain_widgets` ('ohm'→'Ω'), `commissioning_wizard` ('OK'→'完了'), `theater_workflow` (recovery status messages), `native_diagnostics` ('Diagnostic log:') — residual English | MED i18n | FIXED |
| 9 | `setAccessibleName` values were English identifiers | LOW a11y | FIXED (all 5 → Japanese) |
| 10 | No `&` keyboard mnemonics anywhere in menus/actions/dialogs | LOW a11y | BY DESIGN — Japanese Windows convention; `&` accelerators are unreliable under IME. Actions already carry real shortcuts (Del, Ctrl+S, Ctrl+Z/Y, Esc, F-keys) |
| 11 | Diagnostic log-file lines are English | — | BY DESIGN — log convention kept English for cross-locale bug reports; the user-facing '診断ログ:' label was localized |
| 12 | Exception/`ValueError` message text is English throughout | — | BY DESIGN — `user_facing_error` maps every surfaced error to a generic Japanese message; raw English only reaches the opt-in Details pane |
| 13 | Reason codes like 'repeatability requires N measurements' are English internally | — | BY DESIGN — display goes through `_reason_label`/`status_label` maps that return Japanese |
| 14 | `__main__.py` (retired browser-stack dev launcher) prints/dialogs are English | LOW i18n | BY DESIGN — development-only entry point, not the shipped product |
| 15 | `localization.py` exists but is not wired to any widget | MED spec | DEFERRED — sketch below |
| 16 | Icon-only controls lack `setAccessibleName` | — | VERIFIED absent: every `QAction`/`QToolButton` carries a text label; collapsible headers show text + arrow |
| 17 | Esc/Enter defaults on dialogs | — | VERIFIED: destructive confirms set `setDefaultButton(Cancel/No)`; `QDialogButtonBox` gives Esc-reject; palette bindings use `QKeySequence.StandardKey` |
| 18 | Tab order on key dialogs | — | VERIFIED: forms use `QFormLayout`/`addRow` construction order (Qt default tab order); no custom widgets reorder focus; no `setTabOrder` misuse found |
| 19 | Focus after dialogs close | — | VERIFIED: all dialogs are modal `exec()` over a `QMainWindow` parent — Qt returns focus to the parent automatically |
| 20 | Qt **stock** widgets (QMessageBox/QInputDialog OK・Cancel, native file-dialog buttons) render English — authored literals are JP but no `QTranslator` was ever installed | MED i18n | FIXED — `ui_theme.install_japanese_translations` loads `qtbase_ja`/`qt_ja` inside `apply_dark_theme`; Cancel→キャンセル, Yes→はい(&Y) (verified offscreen) |
| 21 | `--legacy-ui` entry crashes at launch (`AttributeError: search_generate_reason_label` — init-order bug in the optimization controllers) | MED correctness | PRE-EXISTING — both files untouched by this round (`git diff main` empty there); legacy surface is outside the shipped shell, same treatment as round-8 legacy notes |
| 22 | Measurement dock `入力役割` line edit defaults to `unknown` | — | BY DESIGN — that is the stored channel-role token (data vocabulary), not a label; the placeholder documents valid tokens |
| 23 | VTK/pyvista chrome ('X Axis/Y Axis' axes, 'Distance' scalar bar) is English | LOW i18n | BY DESIGN — renderer-internal labels, not Qt widgets; no supported localization path |

## Deferred — needs a product decision

### Wire `localization.py` (or drop it)

`localization.py` implements a `LocalizationService` with en/ja catalogs
(`tr('workspace.room')` etc.) that **no widget calls**. Two options:

- **Adopt it**: wrap each literal in `window.tr()`/`service.tr(key)`, seed
  the `ja` catalog with this round's strings, keep `en` as the
  `locale='en'` override for a future English UI. Effort: ~1 session for
  the ~300 call sites, mechanical.
- **Remove it**: if an English build is not on the roadmap, the dead
  service + its catalog tables are maintenance weight; delete module and
  `test_localization.py`.

Product call: whether an English UI is ever intended.

## Verification

- New `tests/test_review_round9_i18n.py`: pins recovery-choice labels,
  `decide_launch` reasons, and `_CONTEXT_DOCK_TITLES` as Japanese-only
  (ASCII-word regex) — guards the dock-title drift class of bug in #3/#4 —
  plus a QMessageBox stock-button check covering the #20 translator.
- `test_measurement_workspace_layout.py` updated to the Japanese dock
  titles it was actually exercising.
- Existing tests asserting English UI text updated: `test_native_launch`
  (3 recovery button labels), `test_launch_intents` ('backup'→'バックアップ'),
  `test_native_diagnostics` (dialog titles + stderr '使用中'; the *log-file*
  assertion stays English on purpose), `test_native_maintenance_cli`,
  `test_startup_recovery`.
- `pytest -q -n 4` full suite run locally (see PR description for counts).
- UI-tested end-to-end on this machine (workflow shell, wall editor, measurement workspace): recovery dialog buttons, rail/context tabs, ~30 toolbar actions, room sketch→create→cancel status messages, and the repaired right-dock tab stack all render Japanese; no mojibake or clipping. See PR comment for recording/screenshots.
- One full-suite flake: `test_cad_hybrid_prediction_provider.py::test_evidence_lifecycle_rejects_illegal_promotions` (`FileNotFoundError` on an authority `.tmp` write under `-n 4` load) — previously logged as flaky in rounds 6–8 docs; code path untouched by this diff; passes standalone and on main.
