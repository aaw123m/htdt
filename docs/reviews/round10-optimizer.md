# Round 10 — convergence round: deferred items implemented

Scope: the three deferred items assigned from `round9-optimizer.md`'s
"Deferred, with sketches" — #4 (O90 RobustnessSpec authoring UI), #5
(remaining heavy lanes onto workers), #8 (re-author affordance after
apply). Each sketch was verified against current code at both ends before
implementation; where the docs had drifted, the code won. Branch
`devin/rev10-optim`.

Method: implementation + new Qt-offscreen regression tests
(`backend/tests/test_round10_convergence.py`), scoped re-runs of every
suite touching an edited module, then the full `backend/tests` suite
(`pytest -q -n 4`, `QT_QPA_PLATFORM=offscreen`).

## Verdict table

| # | Item | Verdict |
|---|------|---------|
| 4 | O90 RobustnessSpec authoring UI | FIXED — new `RobustnessAuthoringContext` + `RobustnessAuthoringPanel` on Optimize > ばらつき耐性. Candidates come from `latest_evaluations_by_candidate`; axes from `physical_variables_from_authority` mapped through `_o90_parameter` and probed with `_axis_value` so only axes the candidate document can actually perturb are offered. Compilation via `build_robustness_spec`, persistence via `save_spec`, evaluation via `evaluate_local_robustness` on a `NativeWorkerPool` (cancel + `before_deactivate`/`dirty_state` gate, JP strings). Bounded defaults: ±5 cm / ±2°, first two axes checked (1 + 2n samples). Joint panel create button now names the missing authority (`不足authority: O90ばらつき評価仕様… / O30目標評価`) instead of dead-ending on `create_spec`'s exception. |
| 5 | Remaining heavy lanes onto workers | FIXED for every lane the sketch names plus the extended twin — `execute_topology_comparison` (`TopologyComparisonCancelled` at the lane-planning and evaluate/persist loops), `create_topology_proposal` (cancel inside candidate enumeration + the per-candidate variant-save loop, and the Room-side `SystemExpansionRoomPanel` now runs it on a pool instead of the click handler), `SystemExpansionOptimizePanel._evaluate` → `evaluate_proposals` on a pool with cancel + run-state gating, `CadAdaptivePlannerService.build_and_save` and `CadAdaptiveExtendedPlannerService.build_and_save` (enumeration pages + inner proposal loops honour `is_cancelled`; both controller lanes moved to `_adaptive_pool` with cancel buttons and lifecycle gates in `before_deactivate`/`dirty_state`/`dispose`). |
| 8 | Re-author affordance after apply | FIXED — `同じ条件で再探索` under the saved-specs card calls `SearchControllerMixin.reauthor_selected_search_spec`: re-derives the grid on the current saved revision via `build_cad_search_spec(revision, constraint_set, spec.axes, candidate_limit=spec.candidate_limit, name=spec.name, linked_variables=spec.linked_variables)`, saves it, selects it, clears stale page/preview state, and mirrors the `save_search_spec` status message. Button is enabled only for a stale spec on a clean saved baseline; re-authoring a spec that already matches the working state is a no-op. **Sketch staleness caught:** the sketch omitted `linked_variables` — `CadSearchSpec` carries them, and dropping them would silently flatten linked grids; the implementation forwards them. |

## Sketch-vs-code corrections made while implementing

- **`evaluate_local_robustness` had no `is_cancelled`** (the sketch assumed a
  hook). Parameter added; checked at the top of the per-sample loop.
  Cancelling raises `RobustnessEvaluationCancelled` and persists nothing —
  `build_robustness_evaluations` requires the full stencil, so a half-written
  evaluation set would be unpersistable anyway; the spec itself is saved
  earlier and stays re-evaluable.
- **Perturbed objective evaluation is honest, not replayed.** O30 vectors
  cannot in general be recomputed without a predictor, so the authoring
  context recomputes `candidate_movement` vectors on the perturbed document
  (pure geometry, rebound onto the nominal metric schema) and lets any other
  authority record `objective_evaluation_failed:unsupported…` — samples show
  未対応 rather than fabricated values.
- **Provenance labels derived, not invented.** `model_id`/`model_version`/
  `prediction_provider_id`/`fidelity` come from the nominal evaluation's
  `evaluation_spec_json`/`input_refs`, falling back to `o30:<authority>`,
  the algorithm version, the predicted `source_kind`, and the predicted
  `evidence_class`.
- **Constraint workspace replayed from the spec snapshot.** `evaluate_spec`
  parses `search_spec.constraint_snapshot_json` — the exact frozen
  `CadConstraintSet` the spec compiled — so `evaluate_local_robustness`'s
  authority check passes by construction.

## Verified

- `test_round10_convergence.py` — 15 new tests covering: candidate listing,
  axis filtering to perturbable axes only, spec compile + persistence +
  exact binding, movement-metric recomputation on ±0.05 m samples,
  unsupported-authority fail-closed samples, cancel-aborts-without-persisting,
  the panel's full click-to-persisted-samples path, joint-panel gating with
  the O90 reason string, cancel inside `execute_topology_comparison`, both
  adaptive enumeration and inner-loop cancels, `create_topology_proposal`
  cancel, and the re-author round-trip (stale spec → new spec bound to the
  current revision with identical axes/limit/name/linked_variables → button
  disables once current).
- Scoped re-run green (174 tests): `test_joint_optimization_panel`,
  `test_optimization_workflow_workspace`, `test_cad_topology_comparison_execution`,
  `test_system_expansion_workflow`, `test_cad_adaptive_planner`,
  `test_cad_adaptive_extended`, all `test_optimization_robustness*`,
  `test_room_workspace`, `test_workspace_dirty_state`.
- Full `backend/tests` suite result: see Tests below.

## Still deferred (with reason)

- **Campaign-evidence materialization / validation-record build remain
  synchronous** (`materialize_objective_evidence`, `build_validation_record`,
  `complete_measurement_plan`). Round 9's finding listed them next to the
  heavy lanes, but they iterate bounded, already-persisted campaign/measurement
  rows — no solver math, no enumeration — and a cancel surface would be
  dead UI weight for realistic campaign sizes. Revisit if campaigns grow
  past a few hundred evidence rows.
- **#9 Pareto double replay** (`find_pareto_set_by_sha` +
  `save_pareto_set` re-validating) — unchanged from round 9; a save-time
  `validated` fast path is still the right fix but is an optimization, not
  a correctness gap.
- **#11 Ctrl+Z/Ctrl+Y in Optimize** — unchanged from round 9; needs a
  shared `CommandShortcutBinder` decision across workspace mounts.
- **Topology-lane per-page rescan** (out of round-9 #1) — `cad_topology_search.py`
  still re-enumerates per page; same LRU treatment applies when it next hurts.

## Tests

| Suite | Result |
|-------|--------|
| `backend/tests/test_round10_convergence.py` | 15 passed |
| Scoped touched-module suites (above list) | 174 passed |
| `backend/tests -q -n 4` (full suite) | 5694 passed, 1 failed — `test_cad_hybrid_prediction_provider.py::test_evidence_lifecycle_rejects_illegal_promotions`, a pre-existing xdist temp-path flake (unrelated code path — `scripts/run_r130a_candidate_wave_execution.py` fixture write under `pytest-of-Administrator/popen-gw1`); passes standalone. |
