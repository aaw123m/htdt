# REV65-CORE — mission/field-return pipeline + journey/envelope integration

## Scope

Post-REV61 non-cad_* modules: `capture_receiver.py` (2.2k lines: wire endpoints, mission packages, delivery ledger), `field_return_ingestion.py` (container verification, staging, ref resolution), `mission_reconciliation.py` (drift classification, rebase decisions, apply), `golden_path_journey.py`, `overview_readiness.py` journey wiring, `error_boundary.py`.

## Method

- Adversarial audit of authority-binding paths: identity-vs-header agreement, settled-state immutability, dedup scopes, digest computations.
- Pattern sweeps across all changed files: broad catches vs `error-boundary:` convention, f-string SQL, naive datetimes, unsorted `json.dumps` in digest paths, salted `hash()`/`random`.
- Re-read the recently-landed hotfix areas (superseded package pull lane, settled-verdict immutability) for sibling gaps.

## Defects found and fixed

1. **Mission task bound twice across contributions** (`mission_reconciliation.py`).
   `field_return_applications` only carries `UNIQUE(contribution_id, task_id)`; the apply dedup also only checked the same `contribution_id`. A second field-return contribution carrying the same mission task inserted a second application row — potentially with a different `applied_target_id`/`decision_id` — minting conflicting authority for one mission task. Fix: dedup at mission scope (bulk apply silently skips the already-bound task; an explicit `task_ids` request for a cross-bound task raises `MissionReconciliationError`). Test: `test_rev65_core.py` (2 tests) + updated `test_apply_flags_refs_the_container_cannot_resolve` (its second contribution now carries a different mission task, as the new rule requires).

## Findings (report only)

- `capture_receiver.py` broad `except Exception` catches at the wire boundary (~8 sites) are semantically correct (every failure maps to a structured reject or is logged) but do not carry the `# error-boundary:` marker — the marker AST-guard only covers the three #815 scope modules, so these evade the convention. Worth a marker-pass, not a rewrite.
- `_handle_field_return_delivery` stages the artifact before checking header-vs-artifact `artifact_id` agreement — a reject still leaves the staged row. Content-addressed staging makes this survivable (the artifact is truth; the header is transport), but it means wire disagreement can't prevent store growth. Domain-judgment, flagged.
- `stage_rejected` binds the *declared* (unverified) sha to the rejected-envelope record; a mismatched declared sha makes the rejection record findable under the attacker's claimed identity rather than the actual bytes. `declared_sha or _sha256_text(body)` already falls back — consider always recording the computed sha beside the declared one.
- Verdict mapping `result not in ('imported','duplicate','superseding') → 'failed'` is fail-closed by design; a device self-reporting 'superseded' would read as a failure. The wire vocabulary never emits it — confirmed not reachable via the current contract.
