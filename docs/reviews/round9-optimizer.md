# Round 9 — the optimization journey end-to-end

Scope: the full loop a user runs in the Optimize workspace — SearchSpec
authoring (axes, linked variables, constraints) → candidate generation →
paging/preview → apply-to-scene → extended (directional) lane → O90
robustness → #174 joint placement+DSP specs → measurement campaigns →
O70 adaptive plans → Pareto comparison. Prior rounds were read first
(`round6-ux`, `round7-workflow`, `round8-journey`, `round8-cadux`,
`round9-report`); their findings are not re-reported. Branch
`devin/rev9-optimizer`.

Method: read both ends of every hop (backend authority + widget) and
exercise the seams with new tests on a Windows box
(`QT_QPA_PLATFORM=offscreen`, `C:/devin/python/python.exe`).

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | `generate_search_space` must enumerate the **entire** raw Cartesian product to compute `candidate_set_sha256`/`feasible_candidate_count`, so every page request — UI prev/next, `iter_cad_candidate_pages`, and the extended lane's `_all_base_candidates` — re-ran the full O(raw × constraint-checks) scan. Extended generation over B base pages × E pages was O(B·E·raw). | HIGH compute | FIXED — `generate_search_space` now returns `all_candidates`; `cad_search.generate_cad_candidates` serves pages from a bounded (4-entry, lock-guarded) LRU keyed on `search_spec_sha256`, which pins axes/limit/both compiled payloads. Per-call `require_search_spec_authority` is unchanged, and a `cancelled` flag still aborts cached reads. Topology lane (`cad_topology_search.py`) has the same per-page rescan — deferred, see below. |
| 2 | `run_joint_execution` resolved each candidate's persisted SystemVariant via `list_variants(document_id)`, which replays full authority validation (`_require_variant_authority` → `materialize_system_variant`) on **every** variant row — and the run itself appends a row per candidate, so cost grew quadratically. | HIGH compute | FIXED — new `CadSystemVariantRepository.variant_for_sha256(document_id, sha256)` uses the UNIQUE `variant_sha256` index for a single-row lookup that still runs `_validated_variant`; joint execution calls it directly. |
| 3 | `JointOptimizationPanel._execute_selected` ran `execute_spec` **synchronously on the UI thread** — a run of up to 50,000 candidates froze the entire window with no progress and no cancel. `before_deactivate`/`dirty_state`/`closeEvent` had no knowledge of joint runs. | HIGH UX | FIXED — execution now runs on a `NativeWorkerPool` (`is_cancelled`/`on_progress` were already in the backend signature): live `実行中: 候補 n/total …` progress, a `実行を中止` button, partial results kept on cancel, and workspace deactivation/dirty-state/`closeEvent` now gate on `joint_optimization_panel.is_running()` + `dispose()`. |
| 4 | **Journey dead-end: no UI can create an O90 `RobustnessSpec`.** `JointOptimizationContext.create_spec` *requires* `baseline.robustness_spec`, `CadRobustnessRepository.save_spec`/`save_evaluation` have zero production callers (tests only), and the workspace robustness page is read-only ("既存O90実行workflowで評価を作成してください") — a workflow that does not exist in the UI. The 配置+DSP joint panel can therefore never pass spec authoring in production. | HIGH completeness | DEFERRED — authoring an uncertainty model is a product decision; sketch below. |
| 5 | Other heavy journeys are still synchronous on the UI thread: `SystemExpansionOptimizePanel._evaluate` → `evaluate_proposals` (per-variant `execute_topology_comparison` with 7-frequency coverage), `SystemExpansionProposalsPanel._create_proposal` → `create_topology_proposal` (topology grid enumeration + per-candidate variant saves), `build_and_save_selected_campaign_validation`, `complete_measurement_plan`, and O70 `build_and_save` (GPR fit over the pool). | MED UX | DEFERRED — same worker pattern as #3 applies, but `execute_topology_comparison`/`build_and_save` do not yet accept `is_cancelled`; sketch below. |
| 6 | Joint spec tree rendered raw internal values to users: mode column showed `'joint'`/`'placement_only'`, stale column showed `stale: scene_revision_changed`-style reason codes, and execute tooltip echoed raw codes. It also derives mode from `dsp_variables`, so a `dsp_only` spec displays as `joint` (the spec persists no mode field). | LOW polish | FIXED — JP labels via `_MODE_LABELS`/`_STALE_REASON_LABELS`. The `dsp_only` mislabel needs a persisted mode field; deferred. |
| 7 | Extended lane leaked raw `str(exc)` into the status bar in five places (capability save, parameter evidence read, spec save, apply) and mixed English into JP surfaces (`missing capability`, `current`/`stale`, `raw N · feasible N`, `extended候補preview`). | LOW polish | FIXED — all errors route through `operation_error_message`; labels/summary JP-ified (`能力情報なし`, `最新`/`変更あり`, `総候補/有効/集合`). Base lane's `候補preview`/`1 command` strings JP-ified too. |
| 8 | Post-apply dead-end: `apply_candidate_positions` mutates the committed working document → content hash changes → the just-used SearchSpec becomes stale, and there is no affordance to re-author the same axes on the new revision (must re-enter every axis/preset manually). | MED completeness | DEFERRED — sketch below. |
| 9 | `find_pareto_set_by_sha` replays `_require_pareto_authority` (all evaluations + front recompute), then `save_pareto_set` replays it again — double O(evals) per view action. | LOW compute | DEFERRED — safe fix is a save-time `validated=True` fast path or an authority memo; sketch below. |
| 10 | After apply, spec rows correctly flip to stale — but **Undo/Redo only called `_rebuild()`**, so spec/extended trees kept showing stale labels after the undo restored positions until a save or re-selection (found during E2E verification: undo restored state yet rows still read 以前の部屋). | LOW polish | FIXED — `undo()`/`redo()` now call `_refresh_search_specs()`/`_refresh_extended_specs()` like `save()` does. |
| 11 | **Ctrl+Z/Ctrl+Y do nothing in the Optimize workspace.** `edit.undo` is bound in the command registry on activation, but `CommandShortcutBinder` — the class that materializes QShortcuts — only exists inside `CadInputController`, which only the Room workspace instantiates. Undo is reachable via the Ctrl+K palette (元に戻す), yet the apply status message advertises "Undoで全位置を復元できます" and users will hit the dead shortcut. | MED UX | DEFERRED — needs a shared binder decision (lift `CommandShortcutBinder` to the workspace mount, or bind per-workspace); sketch below. |

## What already works (verified by reading both ends)

- **Spec authoring & validation.** `validate_search_spec` bounds axes to the
  room, rejects a linked slave axis doubling as a grid axis (clear error,
  verified in test_cad_search `test_linked_slave_axis_cannot_also_be_a_grid_axis`),
  and computes the raw-count estimate before `candidate_limit` is written.
  Search-range presets (nudge ±0.5 m / wide ±1.0 m / room / height ±0.25 m)
  are sane for real rooms; default `candidate_limit=10_000` and
  `search_page_limit=250` are bounded by `SYSTEM_MAX_RAW_CANDIDATES=50_000`
  and `MAX_SEARCH_PAGE_SIZE=500`.
- **Progress honesty in the grid lanes.** Base and extended generation run on
  `NativeWorkerPool`s with cooperative cancel, stale-result discard, and
  authority recheck on completion; `before_deactivate`/`dirty_state` block
  page switches while they run; `dispose()` bounds shutdown and detaches
  stragglers. No "running forever" state — `WORKER_CANCELLED` is a first-class
  completion.
- **Apply provenance & preview.** Preview is non-mutating
  (`candidate_preview_document`); apply rechecks spec currency against the
  working document + constraint workspace, binds by exact
  `search_spec_sha256`, and lands as ONE history command — Undo restores all
  positions (`test_candidate_preview_is_non_authoritative_and_apply_is_one_undo`).
  Extended apply additionally replays orientation constraints.
- **Fail-closed reads.** Every repository read replays pinned authorities
  (spec recompile byte-exact, variant materialization, evaluation replay) —
  tampered rows raise `ValueError` rather than serving stale data
  (`test_variant_for_sha256_fails_closed_on_tampered_row` added for the new
  fast path).
- **Pareto presentation.** `refresh_pareto_comparison` uses
  `latest_evaluations_by_candidate` and per-candidate status; selecting a
  candidate not on the current page shows an honest "off-page" message
  instead of guessing. The no-total-score design principle is maintained.
- **Bounded record-level writes.** `build_validation_record`,
  `complete_measurement_plan`, adaptive plan creation are synchronous but
  bounded by single-campaign scope — acceptable, unlike the
  unbounded-by-candidate-count paths in finding #5.

## Deferred, with sketches

### #4 O90 RobustnessSpec authoring UI (unblocks the joint lane)

The smallest viable path reuses existing authority pieces: an authoring
dialog on the Optimize>robustness page that takes the selected SearchSpec
(reuse `physical_variables_from_authority` for axis choices),
`build_robustness_spec(...)` for compilation, `repository.save_spec` for
persistence, and `evaluate_local_robustness` behind the same
`NativeWorkerPool` pattern (it already accepts per-sample evidence hooks).
A bounded `perturbations` default (e.g. ±5 cm on 2 axes, 8 samples) keeps
the first-run cost honest. Until then the joint panel's create button will
always fail with 'joint optimization requires an existing O90
RobustnessSpec…' — consider gating the button with that exact reason so the
dead-end is at least legible.

### #5 Move remaining heavy lanes onto workers

`JointOptimizationPanel` is the template: pool + `is_cancelled` + progress
label + cancel button + `before_deactivate`/`dirty_state` gate.
`execute_topology_comparison` and `CadAdaptivePlanService.build_and_save`
each need an `is_cancelled` parameter threaded to their inner loops
(per-variant / per-training-point); `create_topology_proposal` needs its
page-generation step moved off the click handler. All are backend-signature
additions, no schema changes.

### #8 Re-author affordance after apply

After apply, offer `同じ条件で再探索` on the search spec row: it calls
`build_cad_search_spec(new_revision, current_constraints, spec.axes,
candidate_limit=spec.candidate_limit, name=spec.name)` and saves the result —
one click restores the exact same grid on the new baseline. The pieces are
all public; only the button and a `spec.stale` state join are missing.

### #9 Pareto double replay

`save_pareto_set` could accept the authority memo pattern used by
`run_joint_execution` (`authorities=…` dict), or `find_pareto_set_by_sha`
could hand its replayed front to a private `_persist_validated` — either
keeps the fail-closed contract while halving comparison-view cost.

### #11 Keyboard undo in Optimize

`CommandShortcutBinder` lives in `command_palette.py` but is instantiated
only inside `CadInputController` (Room workspace). The optimization mount
(`build_optimization_workspace_mount` / workspace `activate`) would need to
instantiate one binder against the shared registry — a ~20-line change,
deferred because shortcut ownership between coexisting workspaces is a
shell-level decision (Room and Optimize mounts must not both answer Ctrl+Z).

### Topology-lane per-page rescan (out of #1's diff)

`cad_topology_search.py` paginates `generate_search_space` identically —
it now receives `all_candidates` in each response, so switching its loop to
one call + local slices is a small follow-up if wanted.

## Tests

New `backend/tests/test_round9_optimizer.py` (6 tests): single-enumeration
paging, page-concat integrity, cancel-on-cache-hit, per-spec cache keying,
`variant_for_sha256` happy/miss/cross-document paths, tampered-row
fail-closed. `test_joint_optimization_panel.py` updated for worker
execution + one new test covering live UI/cancel/partial-results.
`test_optimization_workflow_workspace.py::test_undo_redo_refresh_spec_trees`
covers #10. Ran the scoped suites (search space, cad search, extended
search, joint execution, joint optimization, panel, workflow workspace,
system expansion): all pass.

E2E on the real app (seeded synthetic demo, `--seed-synthetic-demo`):
spec authoring → save → 401-candidate generation → prev/next paging
consistent and instant (memo), non-mutating preview → apply in one command →
Undo restores positions, extended tree JP labels + generation summary
JP, joint panel renders with the new 実行を中止 button. Joint execution
itself is untestable end-to-end until #4 is resolved.
