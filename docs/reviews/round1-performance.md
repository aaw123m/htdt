# Round 1 review — performance & resource efficiency (+ docs/type hygiene)

Scope: `backend/src/htdt` — SQLite evidence repositories, the native
authority audit, campaign/validation services, robustness caches.
Method: cProfile + microbenchmarks on `test_native_authority_audit.py`,
`test_cad_synthetic_demo.py`, `test_cad_validation_campaign_service.py`
hot paths; before/after timings on a seeded synthetic-demo database.

## Headline measurement

| Path | Before | After | Δ |
|---|---|---|---|
| `audit_native_authority_graph` on seeded synthetic demo DB | 41.3 s | 10.2 s | **4.1× faster** |
| `test_audit_passes_on_persisted_synthetic_fixture` (seed+audit) | 46.7 s | 36.1 s | −23% |
| `test_holdout_record_read_and_audit_replay` | FAILED at HEAD | passes | correctness fix |

Root cause behind the audit cost: every repository method opened a fresh
`sqlite3.connect`, and the first query on a fresh connection pays ~3–5 ms
of schema-catalog parsing because the schema defines **1071 tables**
(microbenchmark: first query ≈ 4.6 ms vs ≈ 0.02 ms on a warm connection,
~150×). On top of that, `CadObjectiveRepository.get_evaluation` regenerated
the SearchSpec's canonical candidate set (`generate_search_space` paged
replays) for *every* row read — ~54 ms per call, 296 calls in one audit.

## Findings

| Sev | Location | Finding | Status |
|---|---|---|---|
| HIGH | `native_authority_audit.py` `_RepositoryChain` + `objective_evaluation` probe | Per-row `get_evaluation` re-ran the SearchSpec candidate-set replay; list verifiers (`list_measurement_plans`, `list_lineage`, `list_samples`, `list_evaluations`) re-listed the same parent collection once per child row | FIXED — chain-level `objective_scans` + `list_once` memo; audit 41.3→10.2 s |
| HIGH | `cad_objective_repository.py` | `get_evaluation`/`save_evaluation` opened a fresh connection and replayed the candidate set per call; no batch API existed | FIXED — `get_evaluations`/`save_evaluations` share one connection + one `_CandidateSetScan` per spec; `scans=` kwarg threads through `get_evaluation`, `save_evaluation`, `_require_pareto_authority`; scan errors are memoized and re-raised so per-row failures are identical |
| MED | `cad_model_validation_repository.py` `_validate_record` | Per record: 2 `get_evaluation` per objective sample + `_evidence_measurement_ids` re-walked (with fresh eval reads) by lifecycle/scope/raw-asset helpers; `list_for_search_spec`/`integrity_problems` repeated this per record | FIXED — `_validated_objective_evaluations` prefetches all sample evals in one `get_evaluations` call (falls back to per-id reads on duck-typed collaborators); one evaluations dict feeds all helpers; one shared `scans` across records |
| MED | `cad_validation_campaign_service.py` | `materialize_objective_evidence`/`readiness` called `list_batch_specs`, `latest_measurement_plans`, `list_evaluations` once per candidate; `_reuse_or_save_evaluation` re-listed all evaluations per save (O(E²)) | FIXED — listings hoisted to one fetch per operation; sha→evaluation map for reuse |
| MED | `cad_robustness_repository.py`, `cad_proposal_robustness.py` `save_evaluations` | Each item paid `_persisted_spec`/`get_spec` + full `list_samples` revalidation → O(E×S) plus 3 connections per item | FIXED — one shared connection + per-spec evidence memo per batch; per-item commit semantics unchanged |
| HIGH (correctness) | `native_authority_audit.py` | `_verify_calibration_evidence_event` defined twice (2-tuple def at ~L1460 shadowed the 6-tuple def at ~L1278); the 6-column `model_calibration_evidence_event` probe therefore raised `too many values to unpack` on every row → spurious `stale_authority` diagnostic, real defects masked | FIXED — renamed the 2-tuple verifier to `_verify_calibration_evidence_event_ref`; `test_holdout_record_read_and_audit_replay` now passes (verified failing identically at HEAD) |
| LOW | `cad_measurement_repository.py` | `list_measurement_plans`/`latest_measurement_plans` untyped in an otherwise typed module | FIXED — `-> tuple[CadMeasurementPlan, ...]` via `TYPE_CHECKING` import (avoids the `cad_measurement_loop` circular import; matches the file's existing local-import convention) |

## Deferred (with sketch)

- **`comparison.py::_interpolate`** — 429 k Python-loop calls per audit for
  per-point bisect + `log2` interpolation. Vectorizing risks 1-ulp float
  drift vs the `COMPARISON_ALGORITHM_SHA256`-pinned replay contract;
  needs a bitwise-equality check (`np.log2` vs `math.log2`) before landing.
- **Connection-per-method repository pattern** — systemic: ~150× first-query
  penalty on the 1071-table schema hits every repository call. A
  per-repository shared connection pool needs a threading-model decision
  (Qt worker threads share repos; `check_same_thread`, transaction
  isolation) — out of scope for a safe-win pass.
- **`CadRoomSimRepository.list_candidate_attempts`** re-fetches and
  re-validates the batch spec row per call — batch a `batch`-aware variant.
- **`ensure_native_schema`** runs `recover_interrupted_restore` + a version
  check on every repository `__init__` (~95 repos per session) — per-path
  memoization is unsafe mid-schema-migration; needs a versioned gate.
- **pydantic `schema` field shadowing** warnings on `BaseModel` subclasses
  — renaming the field changes serialized payloads; needs a migration plan.

## Notes / invariants kept

- Batch writes keep **per-item commits** — a mid-batch failure persists
  exactly the same prefix as sequential `save_*` calls.
- Shared `scans` memos are keyed by `search_spec_sha256`, and a page
  generator that raised once re-raises the stored exception — repeated
  lookups report identical errors to fresh replays.
- Narrow duck-typed collaborators (test doubles implementing only
  `get_evaluation(id)`/`save_evaluation(e)`/`list_evaluations(spec)`) are
  still supported: the batch path activates only on the declared
  `CadObjectiveRepository` type.
- Pre-existing suite failures unrelated to this change (verified identical
  at HEAD): `test_acoustic_bakeoff_mfem_*` (resolve `benchmarks/` relative
  to CWD — need repo-root invocation), `test_bounded_ingress`
  `test_workspace_file_dialog_rejects_oversized_file`, `test_dependency_lock`
  (`pyyaml==6.0.3` pin absent from `requirements-n05-windows.lock`).
