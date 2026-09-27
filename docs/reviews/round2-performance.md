# Round 2 review — performance & resource efficiency

Scope: `backend/src/htdt` — comparison math, SQLite repositories, the native
authority audit, GUI startup/import time, PyVista render paths.
Method: same protocol as round 1 — cProfile + microbenchmarks on the seeded
synthetic-demo database; before/after timings on this box (Windows Server
2022, CPython 3.12, venv `backend[dev]`). Round-1 items already fixed there
are not re-reported.

## Headline measurements

| Path | Before | After | Δ |
|---|---|---|---|
| `import htdt.native_cad` (module import time; also what every `htdt-native` CLI invocation paid) | ~2.6–4.06 s, loads PySide6+PyVista+VTK | **0.085 s**, zero Qt/VTK modules | ~30–45× faster; `htdt-native --backup/--restore/--automatic-backup/--version` no longer touch Qt at all |
| `audit_native_authority_graph` on seeded synthetic demo DB | 9.51 s (post-round-1 baseline this session) | **7.12–7.29 s** | −23–25% (41.3 s at round-0 baseline → ~5.8× cumulative) |
| `compare_frequency_responses` (943-point grid) | 1.646 ms/call | **0.669 ms/call** | 2.5× — `_interpolate` vectorized; log-grid + response prep shared across member responses |
| `ensure_native_schema` per repository `__init__` on existing DB | full `recover_interrupted_restore` + version probe + DDL scan per repo instance | **0.35 ms/call amortized** (stat-signature memo) | ~50–100× less work per repeat open |
| Room workspace `_render` composite | up to 6 full `plotter.render()` passes (document + constraint + measure + video + proposed + prediction renderers each drew once) | **1 pass** via `RoomViewport3D.deferred_render()` | intermediate draws were invisible state — only the final scene mattered |

## What changed

### `_interpolate` vectorization (was deferred in round 1)

- `comparison.py`: `_grid` now memoizes via `@lru_cache _grid_cached(k_min, k_max)`;
  `_prepare_response` extracts frequencies/levels once into NumPy arrays;
  `_interpolate_prepared` does `searchsorted` + `clip` + masked `np.where` on
  the whole grid; `_interpolate_many` amortizes this across member responses;
  `compare_frequency_responses` shares prepared responses and the valid-grid
  log between the reference and candidate sides; `cad_multi_seat_analysis`
  uses `_interpolate_many` for its per-member row sweep.
- **Replay-hash safety (the round-1 blocker):** `COMPARISON_ALGORITHM_SHA256`
  pins `linear_in_log2_frequency` semantics and persisted comparisons replay
  for exact equality. `np.log2`/`np.power`/`np.sum` are NOT bit-stable across
  platforms (libm/pairwise), so the vectorized path keeps `math.log2` for the
  log-grid (verified identical to `math.log2` on 22k samples on this platform,
  but `math.log2` is the replay-safe choice), Python-level `2**(k/PPO)` power,
  and scalar `sum()`/`_mean` for the aggregates. Verified: 500 randomized
  cases produce **bitwise-identical** outputs to the scalar path.
- Tests added (`test_comparison.py`): scalar-equality over 200 random grids,
  degenerate frequency-axis fallback, extrapolation rejection, replay
  equality.

### roomsim batch-spec refetch + objective-repo threading (deferred in round 1)

- `cad_roomsim_repository.py`: `_batch_spec(batch_run_id, *, batches=None)`
  memoizes the *validated* batch spec or the stored exception per call-scope
  dict (re-raises identically; absent rows are never cached). `get_batch_spec`,
  `list_attempts`, `list_candidate_attempts`, `completed_candidate_ids`,
  `get_attempt`, `save_attempt` all accept the opt-in `batches=` memo. One
  `save_attempt` previously replayed the spec twice (authority + attempt
  listing) — now once.
- `cad_objective_repository.py`: `batches=` threaded through
  `_require_evaluation_authority`/`_validated_evaluation`/`get_evaluation(s)`/
  `save_evaluation(s)`/`_require_pareto_authority`, passing into
  `ObjectiveAuthorityContext.roomsim_batches`.
- `cad_objective_authority.py`: `_resolve_cad_roomsim_attempt` uses the shared
  memo when the repository is a real `CadRoomSimRepository` (deferred import;
  duck-typed doubles still get the plain `get_attempt` path).
- `native_authority_audit.py`: `_RepositoryChain` owns one `roomsim_batches`
  dict for the whole audit. New `_verify_roomsim_attempt` list-verifier checks
  membership against `list_attempts(batch_run_id)` — strictly stronger than
  `get_attempt` (validates every stored column against the payload) — replacing
  ~90 per-attempt `get_attempt` replays with ~1 batch replay per batch.

### `ensure_native_schema` init cost (deferred in round 1)

- `cad_schema.py`: `_db_file_signature(path)` captures
  `(st_dev, st_ino, st_mtime_ns, st_ctime_ns, st_size)`;
  `ensure_native_schema` early-returns `NATIVE_SCHEMA_VERSION` on a signature
  hit. **Why safe:** no connection uses WAL (verified — rollback journal only),
  so every committed write mutates the main DB file and bumps the signature.
  `recover_interrupted_restore` still runs unconditionally before the gate —
  restore semantics unchanged. After any real write the signature changes and
  the next open re-validates normally.

### GUI startup import time (new territory)

- `native_cad.py` entry point: module-scope PySide6/PyVista/repository imports
  replaced by PEP-562 `__getattr__` + `_LAZY_EXPORTS` covering every lazy name
  (window classes, `QApplication`/`QIcon`, `SceneRepository`, upgrade helpers,
  launch intents, backup/migration/seed entry points). `_run_gui` materializes
  its collaborators via `sys.modules[__name__]` attribute reads — so
  `monkeypatch.setattr(native_cad, 'QApplication', fake)` in
  `test_native_diagnostics.py`/`test_native_launch.py` keeps working
  (module-attr lookup sees the patched global before `__getattr__`).
  Dead `zipfile`/`import_project_bundle` module imports removed;
  `default_data_dir` switched to the identical `runtime_instance` copy (drops
  the `native_editor`→`pyvista` chain from every path).
- Maintenance entry points (`--backup`, `--restore`, `--automatic-backup`,
  `--seed-synthetic-demo`, `--migrate-legacy-data`, `--version`) now boot in
  ~85 ms instead of paying the full Qt/VTK import stack (matters for the
  scheduled `--automatic-backup` invocations).
- `htdt-capture-import` already lazy enough (290 ms, no Qt). `room_editor`/
  `wall_editor` still load Qt at module scope but are `python -m` tools where
  the GUI is the product — `--help` speed isn't worth a refactor.

### PyVista/VTK render batching (new territory)

- `room_viewport.py`: new `_render()` wrapper + `deferred_render()`
  contextmanager; all 16 `self.plotter.render()` sites route through it.
  Composites (`room_workspace._render` — document rebuild + constraint +
  measure + video + proposed + prediction overlays;
  `optimization_search_domain.refresh` — document + constraint + domain
  preview; `measurement_page_workspace._render_spatial` — document + ghost
  proposals + measurement overlay) now draw once instead of 2–6 times.
  Duck-typed viewport doubles keep working via `getattr(..., nullcontext)`
  fallbacks.
- Tests added (`test_room_viewport_semantics.py`): coalesce-to-one, nested
  scope, flush-on-exception, unbatched pass-through.

### SQLite indexes (new territory — checked, no work needed)

`EXPLAIN QUERY PLAN` on the hot queries (`cad_roomsim_candidate_attempts` by
`batch_run_id`/`candidate_id`/`attempt_id`, `cad_roomsim_batch_specs` by
`batch_run_id`/`search_spec_id`, `cad_objective_evaluations` by
`evaluation_id`/`search_spec_id`, `cad_pareto_sets`, `cad_model_validations`):
every one resolves through `idx_*` search/covering indexes created in round 1;
the `ORDER BY` clauses are served by index order (no `USE TEMP B-TREE`). No
missing-index findings.

## Deferred (with sketch)

- **Connection-per-method repository pattern** (carried over): the ~3 ms
  first-query schema-catalog cost on the 1071-table schema still hits every
  fresh `sqlite3.connect`. `require_native_tables`'s `sqlite_master` scan is
  now gated by `ensure_native_schema`'s signature memo for repeat opens, but a
  true shared/pooled connection needs a threading-model decision (Qt worker
  threads share repos; `check_same_thread`, transaction isolation) — still
  out of scope for a safe-win pass.
- **`list_candidate_attempts` still re-lists per candidate** inside
  `save_attempt` when `batches=` isn't shared — the memo covers one call; a
  batch-level `save_attempts` API is a bigger API change.
- **PyVista actor lifecycle**: `render_document` still does `plotter.clear()` +
  full `add_mesh` rebuild per refresh. Incremental actor updates (keep a
  persistent actor map keyed by entity_id, only add/remove/update diffed
  meshes) would cut mesh construction ~entirely for selection-only changes —
  needs the picking/actor-id bookkeeping reworked alongside; deferred as a
  bigger refactor. `deferred_render` is the safe subset.
- **`cad_adaptive_extended`/`cad_adaptive_planner` `np.vstack`** builds are
  once-per-objective over bounded training sets — not hot enough to touch.

## Notes / invariants kept

- Bitwise replay equality of persisted comparisons is the hard contract —
  the vectorization keeps `math.log2`-fed arrays and scalar `sum()`/`_mean`
  on the aggregate side, never `np.log2`/`np.sum`.
- `batches=` memos are opt-in and per-operation (not per-repo-instance): the
  per-read re-validation contract is preserved; exceptions are memoized and
  re-raised so a failing spec reports identically on the 1st and Nth access.
- `recover_interrupted_restore` is not gated by the signature memo — an
  interrupted restore can leave the file untouched-yet-incomplete, and that
  check must stay unconditional.
- `deferred_render` renders on exit even if the body raised — the drawn state
  is whatever the partial build reached, matching the old last-render
  semantics.
- Pre-existing suite failures unchanged and unrelated (same list as round 1:
  `test_acoustic_bakeoff_mfem_*` CWD-relative `benchmarks/`,
  `test_bounded_ingress` oversized-file dialog, `test_dependency_lock`
  `pyyaml==6.0.3` pin).
