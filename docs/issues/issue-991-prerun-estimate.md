# #991 実行前の計算時間/メモリ/精度条件と予測の提示

`solver/UX`: before committing an acoustic prediction or joint-
optimization run, the operator sees the exact configuration that will
run, a range estimate of runtime / peak memory / storage, the accuracy
conditions it is bound to, and explicit blocking vs warning verdicts.

## Authority: `htdt/cad_prerun_estimate.py`

- `PrerunJobPlan` — sealed record of the compute-driving axes of one
  pending run (solver id/version, backend, subject binding, numeric
  axes, non-numeric `bindings`). `plan_sha256` pins the plan to the
  exact configuration; two different configs can never share an
  estimate silently.
- `SolverCostModel` — declared calibrated ranges + per-axis cost terms.
  `basis` can never be `observed_run`: measured cost belongs to
  `ComputeObservation`. Declared models
  (`RECTANGULAR_MODES_COST_MODEL`, `PROVIDER_RESPONSE_COST_MODEL`,
  `JOINT_EXECUTION_COST_MODEL`) carry constants measured on the
  Windows/Python 3.13 dev box (2026-10-09) — runtime ~5µs/mode-grid
  cell, ~1KB/accepted mode, ~0.19s per joint candidate.
- `PrerunEstimate` — sealed estimate hash-pinned to the plan. Every
  metric is a *range* with an evidence basis, not a fabricated point
  value. `confidence` ladders `calibrated → bounded_extrapolation →
  unknown`; out-of-range or missing axes leave the metric `None` and
  record the reason — never a fake number.
- `estimate_prerun_cost` — classifies each axis
  (`in_range/extrapolated/out_of_range/unmodeled/missing`), evaluates
  declared terms, computes budget verdict (`evaluate_budget`-compatible
  vocabulary: within/over/unknown/incomparable) against *declared*
  limits only, and runs the real #770 `fidelity_cost_gate` so the
  claim label is identical to the existing authority. Blocking
  reasons (`estimated_peak_memory_exceeds_device`, `gpu_unavailable`)
  stay fail-closed; soft conditions (long runtime, extrapolation,
  undeclared limits, non-rectangular geometry) are warnings only.
- `record_prerun_observation` — mints a sealed `ComputeObservation`
  *only* from real measurement (positive finite `runtime_s`, RSS
  stored as `process_rss_bytes` — never `peak_memory_bytes`). The
  observation binds both the run record (`run_ref`) and the estimate
  (`profile_ref`) so observed-vs-predicted comparison is auditable.
  Cancel/failure paths record nothing — a failed run can never mint
  a success-looking observation.

## Persistence — `cad_prerun_estimates` (schema v118)

Append-only sealed table inside `CadFieldMetricRepository` next to the
#770 budget authorities, with row-integrity bindings, audit replay
probe (`prerun_estimate`), lifecycle label
「実行前計算コスト推定レコード」, and the v117→v118 migration.
Forgeries are rejected by the generic sealed-store re-verification;
same-id/different-sha conflicts raise `FieldMetricConflictError`.

## UI surfaces

- `PrerunCostCard` (`prerun_cost_card.py`) — read-only JA card: config
  line, per-axis status labels, runtime/memory/storage ranges (or
  「不明（校正範囲外）」), budget + fidelity + confidence verdicts,
  blocking/warning lines, and any past observation measured for the
  same plan (observed-vs-predicted on the next selection).
- `RoomPredictionPanel` — card sits between the run options and the
  action row; refreshed on model/receiver/max-mode/environment changes
  (advisory only — previews never persist). The run button stays
  disabled while `prerun_card.blocking`; `run_prediction` re-checks at
  commit. On `start()` the controller seals+persists the estimate and
  starts the wall clock; on the accepted-success path it records the
  measured observation bound to the run and the estimate.
- `JointOptimizationPanel` — card under the preflight label; with no
  spec selected it previews the estimate for the spec the authoring
  controls would create (mode/budget edits update it live); with a
  spec selected it shows that spec's exact estimate. Blocking reasons
  disable 実行 and re-check inside `_execute_selected`. Execution
  persists the estimate before `run_joint_execution` and records the
  observation on non-cancelled completion.

## Honesty guarantees

- Estimates are labelled estimates — ranges with basis, never point
  values, never `observed_run` basis.
- Out-of-calibrated-range inputs produce `UNKNOWN` + reason, not a
  fabricated number; bounded extrapolation widens the upper bound.
- Actuals come only from measured elapsed time / process RSS; a
  cancelled or failed run leaves no observation.
- The run button is never disabled by soft warnings; blocking reasons
  are the only gates, and both surfaces re-verify at commit.

## Follow-ups

- Real-Win11 performance evidence still belongs to #867; the declared
  cost models can be recalibrated when that data lands.
- GPU-required solver models exist in the vocabulary
  (`required_accelerator`, `gpu_unavailable` blocking) — no current
  lane declares one.
- `process_rss_bytes` is a completion-time sample; a true peak-memory
  sample needs instrumentation in the worker (tracked under #867
  evidence work).
