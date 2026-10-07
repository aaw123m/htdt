# Issue #792 — Lifecycle / Supportability Authority

REV62 slice implementing the #792 owned authority surface as a sealed
`cad_*` domain module + repository, matching the REV60/REV61
conventions.

## Scope implemented

New module `backend/src/htdt/cad_supportability.py` (sealed records,
evaluators, JA label registry) and
`backend/src/htdt/cad_supportability_repository.py`
(`CadSupportabilityRepository`, `_SealedStore` convention). Native
schema v80 carries five tables:

| Table | Record | Prefix |
| --- | --- | --- |
| `cad_supportability_profiles` | `SupportabilityProfile` | `spro-` |
| `cad_supportability_dependencies` | `ExternalDependency` | `exdep-` |
| `cad_lifecycle_risk_observations` | `LifecycleRiskObservation` | `lro-` |
| `cad_offline_continuity_evidence` | `OfflineContinuityEvidence` | `oce-` |
| `cad_replacement_readiness` | `ReplacementReadiness` | `rpr-` |

Note: `cad_supportability_dependencies` is intentionally *not* named
`cad_external_dependencies` — that table already exists (dependency
resolution domain, a different schema shape).

Helper (non-sealed) sub-models: `DeclaredFunction`,
`LicenceEntitlement`, `SoftwareAvailability`, `IntegrationDependency`,
`ServiceabilitySnapshot`.

## Semantics

- **Function-level dependency** (#792 §1–2): `ExternalDependency` binds
  one of 16 dependency kinds to an exact `function_label`, never to a
  device as a whole. `SupportabilityProfile` holds the project's
  declared function inventory with project-defined criticality
  (essential/important/optional/service_only — no hidden weighting).
- **Offline continuity** (§3, §8): `OfflineContinuityEvidence` cannot
  be sealed unless `authorized` and non-`destructive`.
  `evaluate_offline_continuity` is the "dependency impact if
  unavailable" verdict — dominance `unavailable > degraded > unknown
  > continue_local`; undeclared function or absent evidence →
  `unknown`. Marketing terms never infer continuity.
- **Licence/entitlement** (§4): `LicenceEntitlement` carries
  identity/model/binding/dates/grace/activation/transferability and
  deliberately has *no* field for key material, tokens, or
  credentials (`extra='forbid'`).
- **Manufacturer support** (§5): `LifecycleRiskObservation` binds one
  of 8 evidenced support states to `subject_ref` + `source_label` +
  `observed_at_utc`. No EOL prediction from product age.
- **Recovery composition** (§13): `evaluate_recovery_capability`
  composes #592 backup ref + software availability + licence path +
  restore dependencies + an actual restore test; a backup file alone
  is never proof. Verdicts:
  `offline_recovery_verified` / `recovery_requires_external_service` /
  `recovery_requires_active_entitlement` / `recovery_untested` /
  `recovery_impossible_with_current_evidence` / `unknown`.
- **Security maintenance** (§14): `evaluate_security_maintenance`
  reports available/limited/ended and routes risk decisions through
  #598. It never calls a device compromised and produces no score.
- **Staleness** (§15): strict explicit-UTC timestamps; `review_by_utc`
  bounds the evidence horizon and `superseded_by_ref` composes with
  #765. `evaluate_evidence_freshness` → current / review_due /
  superseded / unknown (no horizon fails closed).
- **Replacement readiness** (§10):
  `evaluate_replacement_readiness` → `replacement_ready` only when all
  portability criteria are `confirmed`, control migration is
  `compatible`, AND the #596 requalification scope + #592
  backup/restore refs are pinned; `not_possible`/`incompatible` →
  `replacement_blocked`; `requires_work`/`migration_required` →
  `replacement_requires_work`; otherwise `replacement_unverified`.

All evaluators fail closed: missing, stale, superseded or
contradictory evidence yields `unknown`/degraded verdicts, never
inferred truth. There is deliberately no aggregate "future-proof" or
vendor-viability score.

## Wiring

- `cad_schema.py`: `NATIVE_SCHEMA_VERSION = 80`, `_migrate_79_to_80`
  replays the baseline DDL (base was v79 — sibling PR #800 claimed
  79 first; this PR claims 80).
- `cad_schema_ddl.py`: five tables + `idx_*_doc` indexes appended to
  `NATIVE_BASELINE_DDL` and `NATIVE_SCHEMA_TABLES`.
- `native_row_integrity.py`: `_ROW_BINDINGS` entries for all five
  tables (nullable refs `profile_ref_id`, `requalification_ref_id`
  bound `optional=True`).
- `native_authority_audit.py`: `'supportability'` repository chain +
  five `_ReplayProbe` entries (`replay_canonical` coverage mode).
- `backend/tests/test_issue_792_lifecycle.py`: 82 tests — seal/id
  derivation, validator rejection paths, every evaluator verdict
  (fail-closed especially), repository round-trip/idempotence/column
  and document_id tamper detection, fresh-migrate table presence, and
  the LFC fixture scenarios (LFC10/20/30/40/50/60/70/80).
- `backend/tests/test_cad_schema.py`: expected-migration entry for v80.

## Remainder (not this slice)

- No UI wiring — handoff-package surfaces (§16), procurement
  integration (§17) and #595 review triggers (§18) compose later.
- `SUPPORTABILITY_LABELS` is the JA label registry (no embedded domain
  prefix); a display layer consumes it when UI lands.
- Pre-existing main rot: `capture_mission_rebase_decisions` (sibling
  PR #800) lacks audit coverage registration, failing
  `test_authority_audit_coverage` + one backup test on main — outside
  this issue's scope.
