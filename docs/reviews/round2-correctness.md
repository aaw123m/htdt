# Round 2 Review — Correctness & Error Handling

Branch: `devin/rev2-correctness`
Scope: `backend/src/htdt` — Qt signal/slot lifecycle (app-level signals, receiver
tracking, worker-thread marshalling), SQLite transaction boundaries, optimizer/
solver numeric edge cases (empty Pareto fronts, degenerate geometry, unit
handling), the round-1 deferred `pareto_front` exception contract, and
dataclass/pydantic mutable defaults + test shared-state isolation.
Round-1 territory (`docs/reviews/round1-correctness.md`: mesh axis matrix,
entity-set undo, backup handle leak) not re-reported.

## Findings

| # | Severity | Location | Description | Status |
|---|----------|----------|-------------|--------|
| 1 | High | `cad_joint_execution.py` `run_joint_execution` ~L792 | `except ValueError: front = None` masked **every** `ValueError` from `repository.pareto_front` as "no front". That call replays each persisted evaluation's full authority graph (spec hash, candidate hash, canonical re-binding, input-ref resolution, evaluator replay); tampered or corrupt evidence raising `ValueError` (e.g. `'belongs to another candidate'`) was silently reported as an empty Pareto front — the exact failure class the replay exists to catch. | **Fixed** — new `ParetoEmptyError(ParetoError)` is raised only for an empty/no-comparable-candidate population; `run_joint_execution` catches just that. Fully-`'unsupported'` vectors (the canonical no-evaluator record) are excluded inside `joint_pareto_front` so they yield the typed empty outcome; partially populated or corrupt vectors still fail closed. |
| 2 | High | `native_editor.py` `NativeEditorWindow.__init__` ~L239 | `app.focusChanged.connect(lambda _o, _n: self._update_actions())` connects to a signal whose sender (`QApplication`) outlives the receiver. Lambda connections carry no receiver, so the connection survives `deleteLater()` teardown; every later focus change invoked `_update_actions` on the destroyed window, touching deleted C++ `QAction`s → `RuntimeError` on each focus transition for the rest of the app session (e.g. after `dispose_mounts`). Verified: `app.receivers(focusChanged)` stayed +1 and the slot still fired after destruction; with a bound method it returns to baseline. | **Fixed** — connect the receiver-tracked bound method `self._on_app_focus_changed` (same idiom as `command_palette.py:99`); Qt drops the connection with the window. Regression test measures `app.receivers(SIGNAL)` returning to baseline after deferred delete + emits `focusChanged` post-teardown. |

## Deferred / observations (no fix)

| Severity | Location | Observation | Deferred reason |
|----------|----------|-------------|-----------------|
| Low | `data_management.py` ~L675 `_OperationWorker` + `QThread(self)` | The op thread is parented to the manager with no `destroyed`-based detach; if the manager were destroyed mid-operation, the running QThread's C++ object would be deleted under it. The manager is app-lifetime today, so this is latent only. `native_worker.py` (`_LINGERING_THREADS` detach map) shows the hardened pattern. | Risky to rework without a concrete trigger; noted for the next hardening pass. |
| Info | SQLite transaction boundaries | All repository writes go through `with closing(self._connect()) as connection, connection:` (commit-on-success / rollback-on-exception) or an inner `with connection:` block; duplicate-recheck + insert are serialized under `BEGIN IMMEDIATE`. `capture_inbox.py` pairs `BEGIN IMMEDIATE` with explicit `rollback()` on error. No write-outside-transaction or missing-rollback path found. No `journal_mode`/`busy_timeout` tuning anywhere — single-process app, deferred. | Clean; WAL/busy_timeout tuning is a perf decision, not a correctness fix. |
| Info | Optimizer/solver numeric edges | Sampled division/normalization sites all guard degeneracy: `_point_on_triangle*` (`normal_length <= tol²`), `cad_colorimetry._xy_from_xyz` (`total <= 0`), `cad_coverage._normalize` (raises on degenerate), `cad_installation_datum._unit` (non-finite/zero length), `cad_mic_response_calibration` (`norm <= 1e-9`), `_relative_error` (zero expected magnitude), `mlp_weighted` seat weights (model-level positive-finite validation). `ObjectiveMetric` validators already enforce `isfinite`. Unit flow is explicit `_m`-suffixed fields; import unit conversion lives behind `geometry_import_dialog._sync_unit_state`. | Clean. |
| Info | Mutable defaults / test shared state | No dataclass/pydantic mutable-default defects: all `= []`/`= {}` hits are function-local accumulators; models are frozen pydantic. `conftest.py` isolates `LOCALAPPDATA` per xdist worker; no module-level mutable repository state found. `except Exception` catches in `optimization_robustness_multidimensional.py` and `cad_objective_repository.py` are deliberate per-sample failure recording / re-raise, not masking. | Clean. |

## Fixed files

- `backend/src/htdt/pareto.py` — added `ParetoEmptyError(ParetoError)`; empty population raises it.
- `backend/src/htdt/cad_joint_optimization.py` — `joint_pareto_front` raises `ParetoEmptyError` for no evaluations and excludes bindings whose vector is entirely non-`'available'` (the canonical unevaluated record) before comparison.
- `backend/src/htdt/cad_joint_execution.py` — `run_joint_execution` catches `ParetoEmptyError` only; integrity failures propagate.
- `backend/src/htdt/native_editor.py` — `focusChanged` connected to bound method `_on_app_focus_changed` instead of an unowned lambda.
- `backend/tests/test_optimization_objectives.py` — `test_pareto_empty_population_is_typed_empty`.
- `backend/tests/test_cad_joint_optimization.py` — `test_joint_pareto_excludes_fully_unevaluated_bindings`.
- `backend/tests/test_cad_joint_execution.py` — `test_execution_without_evaluator_reports_empty_front`, `test_execution_propagates_pareto_integrity_failure`.
- `backend/tests/test_save_focused_editor.py` — `test_focus_changed_disconnects_when_editor_is_destroyed`.

## Tests

- `pytest tests/test_optimization_objectives.py tests/test_cad_joint_execution.py tests/test_save_focused_editor.py` — 33 passed.
- `pytest tests/test_cad_joint_optimization.py tests/test_cad_direct_level.py tests/test_native_worker.py` — all passed.
- Full suite `TMPDIR=/c/t C:/devin/python/python.exe -m pytest -q -n 4` — 4960 passed, 26 failed, all 26 in `test_acoustic_bakeoff_mfem_{concave,modal,transient}_experiment.py` with `FileNotFoundError: benchmarks/acoustics/*.json`. Those tests use a repo-root-relative `Path('benchmarks/...')`, so they cannot resolve from the mandated `cd backend` invocation — verified 26/26 pass when run from the repo root, and they are unrelated to this diff. Preexisting CWD issue.
- Regression check: the new tests fail on the pre-fix tree (integrity tamper masked to `()` instead of raising; `focusChanged` receivers count stays at +1 after window teardown).
