# Round 3 — deferred-item sweep

Scope: every deferred-with-sketch item carried by `docs/reviews/round1-*.md` and
`docs/reviews/round2-*.md`, re-evaluated for "safe + mechanical" implementation
now. Regression tests for implemented items live in
`backend/tests/test_review_round3.py`.

## Verdict table

| Item | Source round | Verdict | sha / reason |
|------|--------------|---------|--------------|
| `data_management.py` op-thread destroyed-detach | R2-correctness (Low) | **implemented** | `DataManagementController` now mirrors `native_worker._LINGERING_THREADS`: module-level `_LINGERING_OP_THREADS` re-owns the `(thread, worker)` pair when the controller is destroyed mid-operation; `finished → quit → deleteLater` self-cleans. Regression: `test_controller_destroyed_mid_operation_detaches_thread`. |
| **`destroyed`-connect was silently dead (pre-existing bug)** | found while implementing the above | **fixed** | `destroyed.connect(self._slot)` — bound method *or* `@Slot()` — is silently never delivered under PySide6 6.11 (verified: `connect()` returns a `Connection`, the slot never runs; the abort is `QThread: Destroyed while thread is still running`). Both `NativeWorkerPool._detach_all` and the new `_detach_active_thread` are now connected through a lambda. The old `native_worker` wiring would have crashed identically — the safety net was dead code. Regression: `test_worker_pool_destroyed_mid_operation_detaches_thread`. |
| Swagger UI / OpenAPI on the loopback API | R2-security (deferred, product) | **implemented** | `/api/docs` and `/api/openapi.json` are now off by default (`docs_url=None`, `openapi_url=None`); opt-in via `HTDT_LEGACY_API_DOCS=1`. The app itself already requires `HTDT_LEGACY_API=1`/explicit data dir, so docs stay development-only. Regression: `test_api_docs_hidden_unless_opted_in`, `test_api_docs_served_when_opted_in`. |
| `load_sofa_dataset_profile` hash/parse TOCTOU | R2-security | **implemented** | h5py now parses `io.BytesIO(file_bytes)` — the same bounded, hashed bytes — instead of re-opening the path; `source_file_sha256` provably covers the parsed bytes. Regression: `test_sofa_recorded_hash_covers_parsed_bytes` swaps the on-disk file for a rejected convention mid-call. |
| Lax canonical-JSON trio | R2-architecture A | **implemented** | `application_preferences._canonical_json`, `cad_acoustic_environment._canonical`/`_hash`, `cad_search_models.canonical_search_{json,sha256}` all delegate to `canonical_json`/`canonical_sha256` (allow_nan=False — NaN now fails closed like every sibling helper). Companion hardening: `PreferenceDefinition.validate` rejects non-finite numbers before min/max (NaN previously slipped through: NaN comparisons are False), and `AcousticEnvironmentProfile` gained `allow_inf_nan=False` so air-density/pressure/humidity fields covered only by `gt=0`/`ge=0` can no longer admit `inf`. Regressions: `test_number_preference_rejects_non_finite`, `test_canonical_search_json_fails_closed_on_nan`, `test_environment_profile_rejects_non_finite_air_state`. |
| `save_attempts` batch API | R2-performance | still-deferred | Every caller streams single attempts (`cad_roomsim_batch_runner` ×2, `cad_r140_executor` ×2, `cad_synthetic_demo` ×1); the `batches=` memo already collapsed the batch-spec refetch. The remaining per-call cost is one indexed `SELECT` on `roomsim_candidate_attempts` — a batch API would enlarge the repository contract for an unmeasured win. Improved sketch: only revisit if a caller materializes a candidate list before persisting (none today). |
| Shared/pooled repository connections | R1 + R2-performance (carried) | still-deferred | No clearly safe pattern: repositories are shared across Qt worker threads (`check_same_thread` poison) and interleaved `BEGIN IMMEDIATE` scopes on one connection would serialize unrelated writers or cross-contaminate transaction boundaries. The threading-model decision stands as the blocker, unchanged. |
| Local-boundary auth token | R2-security | still-deferred | Product/UX decision: a per-launch token must reach the browser SPA the shell opens (deep-link/embedded-token handoff); pure-backend implementation is trivial but the delivery path is a product choice. Sketch improved: generate token per `create_app()`, inject into the served `index.html` as a `meta` tag, require `X-HTDT-Local-Token` header in `install_local_request_boundary`. |
| `migration_guard._rollback` extractall residual | R1-security F4 | **already-resolved** | `_extract_member_bounded` (present since a post-round-1 commit) streams each member through `archive.open` under a shared `remaining` byte budget with per-member declared-size and decoded-size checks — exactly the round-1 sketch. Nothing to do. |
| Lineage-repoint migration triplication | R1-architecture E | already-resolved | Consolidated in round 2 (`capture_*._repoint_*_lineage_parent` → shared parameterized migration); verified present in round2-architecture notes. |
| pydantic `schema` field shadowing warnings | R1-tests / R2-arch 6 / R2-tests | still-deferred | Resolved as *keep*: `Model.schema` is a serialized wire key pinned by `test_schema_wire_key.py`; no pydantic-level escape hatch exists that keeps it a working attribute. Warnings are accepted noise until a coordinated schema-version bump renames the wire key. |
| Effective-SCC growth gate (175 modules) | R1-arch B / R2-arch B | still-deferred | Needs a CI gate (AST top-level-edge check) — repo runs no CI ("local only" per round-3 instructions); writing an unwatched gate is performative. Sketch stands. |
| PyVista incremental actor updates | R2-performance | still-deferred | Needs picking/actor-id bookkeeping rework; `deferred_render` already landed the safe subset. |
| `cad_adaptive_*` `np.vstack` builds | R2-performance | still-deferred | Once-per-objective over bounded training sets; not hot enough. |
| Test-only modules awaiting UI wiring | R1-arch F / R1-tests | still-deferred (report only) | Re-checked: `authority_graph`, `help_registry`, `activity_center` still have zero intra-package importers — wiring has not landed. Per the round-1 sketch this belongs in `IMPLEMENTATION_STATUS.md` tracking, not a review-round diff. |
| ~17 Qt workspace-shell modules untested | R2-tests | still-deferred (report only) | Low marginal value offscreen; needs the UI-test-harness program decision, not per-file tests. |
| God-object decompositions | R1-arch A | still-deferred (report only) | Explicitly out of scope this round — do not split large files. Verbatim-move extraction one-at-a-time remains the sketch. |
| `pareto_front` typed-exception residual | R1-correctness | already-resolved (R2) | `ParetoEmptyError` landed in round 2; skipped per instructions. |

## Other leftovers fixed while reading these areas

- `test_acoustic_bakeoff_mfem_{concave,modal,transient}_experiment.py` —
  `PLAN_PATH` was CWD-relative (`Path('benchmarks/...')`), so all 26 tests in the
  three files failed with `FileNotFoundError` when pytest ran from `backend/`
  (the mandated invocation). Now anchored to `Path(__file__).resolve().parents[2]`,
  matching `test_acoustic_bakeoff_mfem_spatial_experiment.py`'s convention.
  The plan JSONs are git-tracked — the bug was purely the relative anchor.
- `cad_search_models.search_timestamp_utc` and `cad_acoustic_environment`
  `created_at_utc`/`updated_at` — now use `htdt.clock.utc_now_iso` (the round-2
  consolidation leaf) instead of inline `datetime.now(timezone.utc).isoformat()`.

## Files changed

- `backend/src/htdt/main.py` — docs/OpenAPI gated behind `HTDT_LEGACY_API_DOCS`
- `backend/src/htdt/cad_spatial_reproduction.py` — SOFA parse reads verified bytes
- `backend/src/htdt/data_management.py` — `_LINGERING_OP_THREADS` +
  `_detach_active_thread` (destroyed-detach), lambda-connected
- `backend/src/htdt/native_worker.py` — `_detach_all` connected via lambda
  (previously dead connection)
- `backend/src/htdt/application_preferences.py` — strict canonical delegation +
  `isfinite` gate in `PreferenceDefinition.validate`
- `backend/src/htdt/cad_acoustic_environment.py` — strict canonical delegation,
  `allow_inf_nan=False`, `utc_now_iso`
- `backend/src/htdt/cad_search_models.py` — strict canonical delegation,
  `utc_now_iso`
- `backend/tests/test_review_round3.py` — new; 8 regression tests
- `backend/tests/test_acoustic_bakeoff_mfem_{concave,modal,transient}_experiment.py`
  — repo-root-anchored `PLAN_PATH`

## Test result

`cd backend && TMPDIR=/c/t C:/devin/python/python.exe -m pytest -q -n 4` — see
commit message for the full-suite count; the scoped run over all touched areas
plus the 26 previously-failing bakeoff tests is green.
