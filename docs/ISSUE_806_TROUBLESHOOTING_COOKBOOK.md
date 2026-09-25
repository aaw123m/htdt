# Troubleshooting Cookbook (issue #806 §6)

Task-oriented recipes built from real failure modes. Each entry states
what remains usable, what evidence is missing, and where to inspect.
This complements embedded help concepts — it does not replace them.

## Prediction says NOT_VALIDATED

- What remains usable: the room model, system definition, measurements
  and reports are intact; only the prediction artifact is withheld.
- What is missing: a coverage/qualification state for the solver
  provider you selected — check the prediction-readiness report's
  `reason_codes` for the exact gap (e.g. coverage scope does not
  include this room/solver combination).
- Where to inspect: the `PredictionReadinessReport` artifact and the
  provider qualification matrix entries (#727 semantics). If the gap
  is a missing validation campaign, see the validation evidence
  program (#793) — E1–E6 coverage rows show which evidence class is
  empty.

## Measurement cannot use delay optimization

- What remains usable: magnitude/equalization paths still work.
- What is missing: a common timing reference across channels — the
  measurement must have been acquired with a shared clock/timebase.
- Remedies: re-acquire with a common timing mode (loopback reference
  or hardware sync), or accept magnitude-only optimization and
  document the limitation in the calibration plan.

## Imported room is not solver-ready

- What remains usable: the raw visual mesh renders and can be
  inspected; it is never dropped.
- What is missing: semantic conversion authority — diagnostics report
  `solver_ready: False` and `semantic_conversion_required: True`.
- Walk-through: build a `MeshHealthSummary` (#762,
  `htdt.raw_mesh_health.build_mesh_health_summary`) over the
  `RawMeshDiagnosticResult`. Read per-target `MeshSolverReadiness`:
  `wave_closed_volume` is blocked by `open_boundary`/`watertightness`
  failures; `ga_direct_early` may remain `ready_with_limitations`.
  Classify candidate repairs with `classify_repair_operation`:
  A_deterministic ops (duplicate removal, unreferenced vertices) are
  safe; B_bounded (weld, fill hole) needs an explicit tolerance;
  C_semantic (acoustic room inference) is a user decision, never
  auto-applied.

## Device settings cannot be applied

- What remains usable: materialized settings payloads can still be
  exported for manual/vendor loading.
- What is missing: a capability matrix row covering this adapter +
  firmware + capability + direction (#792).
- Walk-through: `resolve_firmware_capability` on the
  `DeviceCompatibilityMatrix`. `NEEDS_REQUALIFICATION` means the
  capability was qualified on different firmware — read-only probing
  may continue if the capability probe proves it; writes stay
  policy-gated until a `DeviceQualificationRecord` is captured for the
  new firmware. `UNKNOWN` means no row exists — contribute a sanitized
  fixture run through `run_conformance_harness` to reach
  `FIXTURE_VERIFIED` (hardware tiers require real-device records).

## Model fits calibration but fails holdout

- What remains usable: the calibration-fit numbers are honest about
  the training set only.
- What is missing: evidence the model generalizes — a locked holdout
  dataset (#793) never used during tuning.
- Next steps: register a `holdout_role='holdout'`, `locked=True`
  dataset in the validation registry, re-run evaluation, and check the
  `ValidationEvidenceProgram` rows for `NEEDS_FRESH_CAMPAIGN`
  (model may be overfit to a stale room state).
