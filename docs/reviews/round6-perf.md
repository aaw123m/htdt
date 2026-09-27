# Round 6 — performance at user scale + numerical accuracy

Scope: user-visible speed/load wins and solver-side accuracy, per the
round-6 brief. Rounds 1–5 already covered lazy `native_cad` imports, render
batching, hot-path indexes, audit N+1s, evaluator fail-opens, and the
`connect_sqlite` consolidation — none of those are re-reported. Branch
`devin/rev6-perf`. All verification is local (`pytest -q -n 4`).

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | STFT spectrogram dB half-scale (`10·log10` on a magnitude spectrum) | HIGH accuracy | FIXED |
| 2 | Joint-optimization repo replays the entire parent-spec authority + variant/plan/report fetch per candidate/evaluation/selection row | HIGH perf | FIXED (`authorities=` per-operation memo) |
| 3 | `_sync_video_panel` / `receiver_options`: ~3 SQLite connections per seat per refresh | MED perf | FIXED (batch pose APIs) |
| 4 | `_refresh_saved_specs` re-runs full `list_candidates` replay per spec just to show a count | MED perf | FIXED (`count_candidates`) |
| 5 | `cad_intervention_study_repository` re-fetches revision+variant per spec row and scans every authority table per ref, per alternative, on a fresh connection | MED perf | FIXED (shared `resolved` set + shared connection) |
| 6 | `intervention_planner_panel._selected_alternative` re-runs `list_alternatives` (full revalidation) on every row click and Apply | MED perf | FIXED (cached per study selection) |
| 7 | `WorkingDocument`/`TheaterWorkingDocument` recompute `scene_content_hash` on every `is_dirty` poll and twice per edit op | MED perf | FIXED (memoized `_content_hash`) |
| 8 | `cad_repository` save paths serialize the canonical scene twice per save (once for `payload_json`, once for `scene_content_hash`) | LOW perf | FIXED (`sha256(payload_json)`, byte-identical) |
| 9 | `field_explorer_panel.refresh_sessions` calls `scene_repository.get` per session row; `acoustic_treatment_service.list_placements` calls `get_definition` per placement; `cad_topology_search`/`cad_proposal_robustness` repeat `scene_content_hash` on the same scene | LOW perf | FIXED (per-call memos) |
| 10 | `cad_listener_poses.seat_entity_id` has no index (the new `IN` query scans the table) | LOW | DEFERRED — needs a `NATIVE_SCHEMA_VERSION` migration; table is bounded by seats×poses |
| 11 | `workflow_application` imports every workspace eagerly at startup | LOW | DEFERRED — lazy-ifying Qt panel imports risks init-order/cycle regressions; needs a dedicated pass |
| 12 | Pooled connections, PyVista incremental actors, `save_attempts` batch API, `_point_in_triangle` tolerance | — | DEFERRED (carried from round3-deferred; unchanged) |

## 1 — STFT spectrogram dB half-scale (accuracy, fixed)

`cad_ir_analysis.py` computed `10.0 * np.log10(c / peak)` over
`np.abs(np.fft.rfft(frame))` — a **magnitude** spectrum. Amplitude ratios need
`20·log10`: a component at 1% of peak rendered as −20 dB instead of −40 dB,
and the −120 dB floor actually clamped at 10⁻¹² magnitude rather than 10⁻⁶.
The rest of the codebase already follows the convention correctly
(`_hilbert_envelope_db` squares to energy before `10·log10`).

Fix: `20.0 * np.log10(np.maximum(c / peak, 1e-6))` — the 1e-6 ratio clamp
lands exactly on the −120 dB floor and removes the existing `log10(0)` divide
warning path. `IR_ANALYSIS_ALGORITHM_VERSION`/`_SHA256` deliberately **not**
bumped: `valid_result` rejects stored results whose `algorithm_sha256`
differs, so a bump would brick every persisted analysis. Pre-fix rows stay
loadable; `replay_ir_analysis` will fail closed on them (spec_levels differ)
— that is the intended fail-closed behavior, surfaced rather than masked.

Regression: `test_spectrogram_levels_are_amplitude_db` (a 200 Hz tone +
600 Hz tone at 1/100 amplitude must read −40 dB at the 600 Hz bin, not −20).

## 2 — Joint-optimization authority replay per row (perf, fixed)

`list_candidates`, `list_evaluations`, `pareto_front`, and the
`run_joint_execution` candidate loop each re-ran the whole authority graph
per row: spec row fetch + `_require_spec_authority` (scene revision,
SystemVariant, SearchSpec, robustness spec, calibration plan, quality
report), then per candidate `get_variant`/`get_plan`/`get_report`, then per
evaluation `_evaluation_context` re-resolved revision/variant/plan/report
again. At a 64-candidate budget this is ~100s of redundant connections and
full replays per refresh.

Fix follows the round-1/2 `batches=`/`scans=` convention: an opt-in
`authorities: dict[tuple[str, str], object]` memo threaded through
`get_spec`/`list_specs`/`get_candidate`/`save_candidate`/`list_candidates`/
`save_evaluation`/`get_evaluation`/`list_evaluations`/`pareto_front`/
`save_selection`/`get_selection`/`latest_selection`. Internal list reads
share a memo automatically; `run_joint_execution` shares one across the
whole candidate loop. Memo keys carry every caller-supplied value the checks
compare, so a hit is identical to re-running fetch + comparisons; misses are
never cached, so rows inserted mid-operation are still seen.

New `count_candidates(spec_id)` — a `SELECT COUNT(*)` for display counters
(metadata only, documented as not replaying payload authority); used by
`joint_optimization_panel._refresh_saved_specs`.

**Measurement** (micro-benchmark on the test fixture, instrumented
`_connect()` counts + `perf_counter` wall time, 2 persisted candidates/
evaluations):

| Operation | connections | wall time |
|-----------|------------|-----------|
| 10× `get_evaluation` unshared | 90 | 2309 ms |
| 10× `get_evaluation` shared memo | 28 | 568 ms |
| 10× `get_candidate` unshared | 44 | 1109 ms |
| 10× `get_candidate` shared memo | 25 | 522 ms |

Regression: `test_shared_authority_memo_replays_identically` — shared-memo
reads return the same validated objects as unshared reads and tampered
payloads still raise through the memo.

## 3 — Listener-pose reads (perf, fixed)

`_sync_video_panel` ran `list_poses_for_seat` (1 connection) + `selected_pose`
(2 connections) per seat per refresh; `receiver_options` ran `selected_pose`
per seat; `selections_for_document` fetched each pose individually.
`cad_listener_pose.py` gains `list_poses_for_seats` (one `IN` query grouped
per seat), `selected_poses_for_document` (selections query + one batched
pose fetch, same raise-on-hash-mismatch contract as `selected_pose`), and a
shared `_poses_by_id` helper now used by `get_pose`/`list_poses_for_seat`/
`selections_for_document`. Both call sites now cost 2 connections total
regardless of seat count.

Regression: `tests/test_cad_listener_pose_repository.py` — grouping,
per-seat equivalence, and fail-closed hash-mismatch on both the per-seat and
batch paths.

## 4–9 — Smaller revalidation/recompute fixes (fixed)

- `cad_intervention_study_repository`: `list_specs` shares a `resolved` set
  (revision/variant/spec-dependency keys carry every compared field) and its
  connection; `list_alternatives` keeps its connection open across the row
  loop and shares `resolved` — previously each alternative opened a fresh
  connection and re-scanned `_AUTHORITY_TABLES` per ref.
- `intervention_planner_panel._selected_alternative`: reuses the validated
  alternatives cached by `_on_study_selection` instead of re-listing per
  click.
- `field_explorer_panel.refresh_sessions`: per-refresh `scene_repository.get`
  memo keyed by `scene_revision_id`.
- `acoustic_treatment_service.list_placements`: per-listing `get_definition`
  memo keyed by `(definition_id, version)`.
- `WorkingDocument`/`TheaterWorkingDocument`: `self._document` is now a
  property whose setter clears `_content_hash_cache`; `is_dirty` and the
  before/after hashes in every edit op amortize to one serialization per
  committed state (was 2 per edit + 1 per poll). `SceneDocument` is frozen,
  so memoizing the hash of the committed value is safe.
- `cad_repository._save_in_transaction`/`save_recovery`:
  `hashlib.sha256(payload_json.encode('utf-8'))` — byte-identical to
  `scene_content_hash(document)` (verified), halves per-save serialization.
- `cad_topology_search.build_topology_placement_search_spec` and
  `cad_proposal_robustness.build_proposal_robustness_spec`: hoist
  `scene_content_hash(virtual_scene)`/`scene_content_hash(nominal_scene)`
  out of identity dict + spec field (2 serializations → 1).

## Deferred

- `cad_listener_poses.seat_entity_id` index (item 10).
- `workflow_application` eager workspace imports (item 11).
- Round-3 deferred items still stand (item 12): shared/pooled DB
  connections are a Qt-threading decision; PyVista incremental actor
  updates; `save_attempts` batch API; `_point_in_triangle` dimensional
  tolerance (needs an equivalence pass).

## Tests

- `backend/tests/test_cad_ir_analysis.py::test_spectrogram_levels_are_amplitude_db`
- `backend/tests/test_cad_listener_pose_repository.py` (new, 3 tests)
- `backend/tests/test_cad_joint_optimization.py::test_shared_authority_memo_replays_identically`
- Scoped suite (ir_analysis, listener_pose, joint_optimization,
  joint_execution, intervention_study, planner/joint panels,
  acoustic_treatment_service, video_geometry, room_sidecar,
  prediction_request_identity): all green.
- Full suite: `cd backend && TMPDIR=/c/t C:/devin/python/python.exe -m pytest -q -n 4`.
