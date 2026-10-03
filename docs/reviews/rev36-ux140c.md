# REV36-UX140C — legacy QMainWindow adapter removal: deletion record

Stage (c) of the UX140 campaign: removes the `--legacy-ui` path and its
adapter outright. Stage (a) (`rev35-ux140.md`) inventoried and parity-mapped
every legacy-only surface; stage (b) mounted the last three stragglers
(`SeatPriorityPanel`, `FieldExplorerPanel`, O531 transfer-matrix grid), so
nothing reachable only through the legacy composition remained.

## Removed

### Window chain (11 modules, ~9.7k lines)

`native_editor`, `room_editor`, `wall_editor`, `cad_composition`,
`theater_editor`, `theater_workflow`, `constraint_editor`,
`measurement_editor`, `measurement_workspace`, `prediction_workspace`,
`optimization_workspace` — the chain-inherited QMainWindow composition
ending in `OptimizationWorkspaceWindow`.

### Launch plumbing (`native_cad.py`)

- `_LAZY_EXPORTS` entries + `__all__`/`__dir__` for the seven window classes
  and the `TheaterEditorWindow` alias.
- `composition = "legacy-optimization" if args.legacy_ui else "workflow-shell"`
  → the launch always reports `workflow-shell`.
- The `if not args.legacy_ui:` guard around `ApplicationPreferenceStore` +
  `CaptureReceiverController`: every launch now gets preferences and the
  capture receiver (the legacy window deliberately ran without them).
- The window ternary → `build_workflow_shell(...)` unconditionally.
- The `--workflow-shell`/`--legacy-ui` mutual-exclusion check (moot).

### Flag contract

`--legacy-ui` is still **accepted** by the parser but hidden from `--help`
and rejected immediately after parsing:

```
htdt-native --legacy-ui
→ error: --legacy-ui は削除されました。HTDT はワークフローシェル構成のみで起動します。
   (exit 2)
```

Rationale: stale shortcuts/scripts get a clear Japanese explanation instead
of argparse's opaque `unrecognized arguments`. Removing the flag outright
would produce the same exit code with a worse message, so the suppressed-
argument + `parser.error` contract is the cleaner of the two options the
spec offered. Pinned by
`test_native_launch.py::test_legacy_ui_flag_is_retired_with_clear_message`.

### Scripts

13 manual Windows validation scripts deleted — each imported the removed
window classes (`validate_n20b/n30a/n30b/n40/n40_precision/n50/n60_a12/
n60_a13/n70/n70_base/n80/n80c_windows.py`, `benchmark_n70_f5_windows.py`).

## Kept — shared code relocated, not deleted

Spec item 2: anything still shared by the workflow path was moved out of
the legacy module rather than deleted.

| Symbol | Was in | Now in | Consumers |
|---|---|---|---|
| `ROLE` (tree entity-id `Qt.UserRole`) | `native_editor.py` | new `tree_item_role.py` | 8 optimization controller mixins, `OptimizationWorkflowController`, `robustness_authoring_panel` |
| `measurement_is_synthetic` / `measurement_evidence_label` | `measurement_editor.py` | `cad_measurement_models.py` (record-level helpers) | `test_cad_synthetic_demo` |
| `candidate_cloud_points` | (already lived in `optimization_search_controller.py`) | unchanged | `test_optimization_workspace` repointed only |
| `_MeasurementScrollArea` | `measurement_workspace.py` | **deleted** — only `optimization_workspace.py` instantiated it; the five controllers importing it never used it | dead imports removed from adaptive / adaptive_extended / extended / measurement / search / validation controllers |

Kept, unmounted legacy-adjacent **domain services** (fail-safe, spec item 5):
`acoustic_treatment_service.py` and `measurement_target_service.py` are
Qt-free services whose docstrings mark them the canonical product path and
which carry their own tests — they are not UI adapters, so they stay.

## Test disposition (spec item 4)

**Deleted** (constructed the removed windows; every pinned behavior has a
workflow-path twin):

- `test_rev30_gui_editors.py` — direct tests of the five removed editor
  windows (superseded by `test_rev35_ux140.py` parity tests).
- `test_measurement_workspace_layout.py` — the combined-scroll dock
  layout, deliberately absent from the workflow path (IA splits it into
  contexts; `rev35-ux140.md` "Deliberately absent").
- `test_prediction_workspace.py` — `_prediction_task_completed` slot on a
  window double; atomic `save_run` / forged-run rejection / partial-commit
  rollback are pinned by `test_room_prediction.py` (`accept_results`,
  `_task_completed`) and `test_cad_predictions.py` (repository batch
  contract).

**Removed legacy-window tests** where a workflow-path twin pins the same
guarantee:

- `test_default_document_seed` — legacy-window open-empty check;
  `test_fresh_default_document_opens_empty_scene` covers the contract.
- `test_rev24_uxflow` — `native_editor` dirty-badge test;
  `test_room_workspace_dirty_badge_survives_notices` covers it.
- `test_accessible_labels` — two `NativeEditorWindow` tests; status-bar
  announcements and named surfaces are pinned on the workflow shell.
- `test_measurement_job_staleness` — `MeasurementEditorWindow` staleness
  test; the sibling test pins the same `MeasurementJobGuard` semantics on
  `OptimizationWorkflowController`.
- `test_round18_cancel_paths` — the legacy dock's `cancel_rew_read` UX;
  the workflow path cancels via `_job_pool` and swallows
  `WORKER_CANCELLED` silently (pinned in `test_workflow_application.py`,
  `test_native_worker.py`).
- `test_save_focused_editor` — `NativeEditorWindow` focused-field save;
  `test_ctrl_s_commits_focused_inspector_position` covers the workflow
  inspector.

**Migrated** (same assertion, new surface):

- `test_bounded_ingress` — bounded REW import dialog now drives
  `MeasurementPageWorkspace.import_rew_text_dialog` (same shared
  `read_file_bounded` + JP oversize message).
- `test_save_focused_editor` — the `focusChanged` disconnect regression
  now exercises `CommandShortcutBinder`, the workflow path's only
  app-level focus hook (QObject-child lifetime guarantees the disconnect).

**Repointed**: `test_round14_search`, `test_round20_search`,
`test_t18_workspace_ux` (`ROLE` → `tree_item_role`),
`test_optimization_workspace` (`candidate_cloud_points` →
`optimization_search_controller`), `test_cad_synthetic_demo` (helpers →
`cad_measurement_models`), `test_native_diagnostics` /
`test_native_launch` (removed legacy seams),
`scripts/test_durations.json` (stale node ids pruned).

## No hidden legacy-only consumers surfaced

The stage-(a) inventory held: every surface deleted here has a verified
workflow-shell mount, and no new consumer appeared during deletion beyond
the symbols relocated above — spec item 5's fail-safe was not triggered.
