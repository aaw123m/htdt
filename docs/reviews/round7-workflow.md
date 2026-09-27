# Round 7 — End-to-End Workflow Coherence

Dimension: whether the app's golden paths actually connect — deep links,
focus/selection handoffs between pages, step-to-step state, and whether
every emitted navigation target has a surface that can display it. Method:
traced each emitted `NavigationTarget`/`htdt://nav/v1` URI to the mount that
receives it and verified the receiver can resolve the carried id.

## Golden-path trace (post-fix state)

- **create → place → optimize → import → compare**: scene revisions land on
  room/history (row now selectable), variants land on optimization/comparison
  (selector now selects the specific variant), measurements land on
  measurement/quality (page now switches to the row), captures land on the
  Inbox row (was aimed at the import wizard which has no row to select).
- **activity timeline → anywhere**: every timeline row's link now targets a
  destination that (a) is registered and (b) has `focus_kinds` covering the
  link's kind; rows whose authority has no page focus their own timeline row.
- **stale links**: unknown workspace sections degrade to a status-bar message
  instead of raising inside the navigation slot.

## Fixed this round

| Finding | Severity | Fix |
|---|---|---|
| Room **history page crashed on open**: `_sync_history_panel` passed `head_revision_id`/`labels` positionally into `RoomHistoryPanel.sync_revisions(revisions, *, head_revision_id, labels, ...)` — keyword-only params → `TypeError` on every call (activate, context switch, label save, restore). The entire 履歴 surface — revision browse/diff/preview/restore — was unreachable. | **Critical** | `room_workspace._sync_history_panel` passes keywords. |
| Measurement **calibration/onboarding page unreachable**: the workspace's 6th page (`機器の準備` UMIK-1 checklist) had no canonical context, the `calibration → campaign` alias redirected links away from it, and no caller ever `set_context("calibration")` — only tests reached it. | High | `calibration` added to `CANONICAL_WORKSPACE_CONTEXTS[MEASUREMENT]` (context bar shows it); alias removed. |
| **Timeline links aimed at dead surfaces**: capture staged/rejected/promoted → MEASUREMENT/'import' (the item lives on the Inbox page); preset created/applied, health baseline/check, AV-sync, note events → OVERVIEW (mount has no `on_entity_requested`; nothing there can resolve those ids); checkpoint created/restored → room/history by a checkpoint id (not a revision id — unselectable); `measurement_imported` → 'comparison' (a measurement id resolves to a selectable row only in the quality table). | High | Links retargeted: captures → `INBOX`; presets/health/AV-sync/notes/checkpoints → `ACTIVITY` (their own timeline row is the focusable record); `measurement_imported` → MEASUREMENT/'quality'. |
| `activity_focus` **never matched its declared kinds**: ACTIVITY was registered with `focus_kinds={ACTIVITY_JOB, PROJECT_CHECKPOINT}` but the handler only scanned the revision-ledger table (which holds scene revision ids) → every 'focused' resolution reported "アクティビティ一覧に該当の記録がありません". | High | `activity_focus` now scans the events table (matching on the row's stored nav-URI kind + object id), then the operations table (operation id stored in `UserRole`), then the ledger. ACTIVITY `focus_kinds` extended to the pageless authorities (`OPERATING_PRESET`, `HEALTH_BASELINE`, `HEALTH_CHECK_PLAN`, `AV_SYNC_CONDITION`, `PROJECT_NOTE`). |
| Room `focus_target` rejected two reachable kinds: `SCENE_REVISION` ids were treated as scene entities → `KeyError` → "対象の項目がこのプロジェクトに存在しません" (the revision exists — it was just the wrong authority); `INSTALLED_EQUIPMENT_INSTANCE` ids hit the same wall although a repository maps instance → scene entity. | Medium | `SCENE_REVISION` routes to `history_panel.select_revision` (new method on `RoomHistoryPanel`); `INSTALLED_EQUIPMENT_INSTANCE` resolves through `CadInstalledEquipmentRepository.get_instance(...).scene_entity_id` (clear messages for unbound instances). `SCENE_REVISION` added to the room mount's `focus_kinds`. |
| Optimization `focus_target` ignored `primary_id` — a `SYSTEM_VARIANT` deep link landed on the comparison page but left whatever variant was previously selected; `OPTIMIZATION_*`/`COMMISSIONING_EVALUATION` behaved the same (page right, item not selected). | Medium | `SYSTEM_VARIANT` now selects the variant via `system_expansion_compare_panel.selector.select_variant` and reports a focused miss with a message when the id isn't in the comparison list. (Candidate/comparison-level selection deferred — see below.) |
| `navigate_to_target` let `select_context`'s `ValueError` propagate through a Qt slot: any deep link carrying a stale/foreign `section` (old URI schema, hand-edited URI, renamed context) crashed the navigation instead of landing. | Medium | Unknown sections now land on the workspace with a status-bar message naming the missing section. |
| Measurement `focus_entity` selected invisible rows: on a match it selected the quality-table row and updated the detail pane while the *current* page stayed put (e.g. 'comparison') — the selection was correct but invisible. | Medium | `focus_entity` switches to the 'quality' page before selecting when the match lives there. |
| Test-side drift hiding real behavior: `test_measurement_workspace_composition.test_legacy_calibration_context_normalizes_to_campaign` asserted the (now removed) alias; `test_save_focused_editor` built a bare `WorkflowApplicationComposition` stub without `.preferences` (required by `_make_room` since the display-length binding landed) and its position-commit test expected meter input while the shipped default unit is mm — the suite had been red on main for both reasons. | Low | Alias test rewritten to the new contract (calibration → onboarding page, index 5); the stub composition gets a real `ApplicationPreferenceStore` pinned to `m` so the meter-scale inputs parse as metres again. |

## Deferred (concrete integration sketches)

| Gap | Why deferred | Sketch |
|---|---|---|
| `ActivityCenter` only mirrors data-management ops. Prediction runs, optimization candidate searches, REW-import parsing and measurement writes — the golden path's *compute* steps — never appear in the operations table, the status rail, or the persisted activity history. | Each producer is a different worker (`NativeWorkerPool`/controller threads); wiring needs a per-op submit/progress/complete adapter at each call site plus result deep-links — product-level plumbing, several places at once. | Give each long-running entry point (prediction refresh, optimization search, REW import) a `center.submit(operation_kind=..., operation_class=OperationClass.WORKFLOW, title=..., deep_link=NavigationTarget(...).as_uri())` + `mark_running`/`complete`/`fail` hooks where progress is already reported to the page's own status label. `ApplicationOperation.deep_link` field already exists — populate it so the row's "開く" navigates to the produced result (measurement id → 'quality', variant id → 'comparison'). |
| `OPTIMIZATION_CANDIDATE` / `OPTIMIZATION_COMPARISON` / `COMMISSIONING_EVALUATION` ids land on the right page but the specific row isn't selected. | The candidate/comparison/validation tables need an id-indexed lookup by the target id (none exists — panels key rows by view position). | Same pattern as `SYSTEM_VARIANT`: give each panel a `select_*_id(id) -> bool` that maps the id → row (`CandidateSetPage` rows already carry the candidate id in `UserRole`-style data — verify), call it in `focus_target`. |
| Write-only timeline authorities: `CadOperatingPresetRepository`, `CadSystemHealthRepository`, `CadAVSyncRepository`, `CadDesignCheckpointRepository`, `CadProjectActivityNoteRepository` have **no production writer** — their timeline families can never populate in a shipped app (only tests and `service.add_note` produce rows). | Producing those events is product behavior (UI buttons: "プリセットを保存", health-check runner, AV-sync recorder, checkpoint save, note entry) — not a handoff fix. | Smallest writer set: "メモを追加" button on the Activity page calling `service.add_note` (writes `CadProjectActivityNoteRepository`); a "チェックポイント作成" command binding calling `CadDesignCheckpointRepository.save_checkpoint` after a save; preset save in the operating-preset row of a system variant. |
| `CommissioningWizard` summary-page "開く" buttons navigate *behind* the still-modal wizard (`exec()` keeps the dialog on top; `navigate_requested` fires the shell beneath it). | Needs a product decision: close-and-navigate vs. non-modal wizard. | Connect `navigate_requested` to `accept()` + deferred `navigate_to_target` (queued), or convert the summary page's open-actions into "wizard closes then navigates" semantics. |
| Palette coverage: providers index commands/entities/settings/help only — measurements, system variants, scene revisions, and inbox items are not palette-reachable even though every one now has a resolvable deep link. | Provider surface is a product feature (new `PaletteResultKind`s + ranking), not a wiring fix. | Add one provider per authority following `HelpTopicPaletteProvider`: results carry `NavigationTarget(...).as_uri()` and activate through the existing `on_deep_link` handoff. |

## Files changed

`src/htdt/workflow_navigation.py` (canonical calibration context, alias removal),
`src/htdt/cad_project_activity.py` (timeline deep-link retargeting),
`src/htdt/application_pages.py` (`activity_focus` covers events/operations tables; operation ids stored on rows),
`src/htdt/workflow_application.py` (ACTIVITY focus_kinds; room focus_target handles SCENE_REVISION + INSTALLED_EQUIPMENT_INSTANCE; optimization focus_target selects the variant),
`src/htdt/workflow_shell.py` (unknown-section degradation),
`src/htdt/measurement_page_workspace.py` (focus_entity switches to the owning page),
`src/htdt/room_history_panel.py` (`select_revision`),
`src/htdt/room_workspace.py` (`sync_revisions` keyword call — the crash fix),
`tests/test_review_round7_workflow.py` (new),
`tests/test_measurement_workspace_composition.py` (calibration contract update),
`tests/test_save_focused_editor.py` (stub preferences store — previously failing on main).

## Tests

`backend/tests/test_review_round7_workflow.py` — 8 tests covering: canonical
calibration context + removed alias, timeline note links resolving to a
focusable ACTIVITY target, `activity_focus` selecting the event row /
operation row / reporting a miss, unknown-section navigation degrading
gracefully, `RoomHistoryPanel.select_revision`, and `focus_entity` switching
to the quality page.
