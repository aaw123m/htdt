# Issue #1030 — lifecycle-gate operator plans + sealed acceptance runs

## Scope

`scripts/issue_lifecycle_manifest.yaml` classifies every open issue's
remaining work into `remaining_gates` of `kind`
`physical` / `manual` / `structural` / `verification`. The physical and
manual gates are the ones software can never clear alone — real rooms,
real devices, maintainer reviews — so until now each gate existed only
as a description string, with no minimal procedure and no sealed record
that the gate was actually performed.

This slice turns every physical/manual gate into a **minimal-step
operator plan** plus a **sealed acceptance-run capture**, wired into the
`htdt` headless CLI (#888) as the `gates` family:

| Verb | Behavior | Mutating? |
|---|---|---|
| `htdt gates plan` | derive + seal one `GateOperatorPlan` per selected gate | yes |
| `htdt gates run` | execute a plan, seal one `CadGateAcceptanceRun` | yes |
| `htdt gates import` | re-verify + persist a run exported on another machine | yes |
| `htdt gates list` | enumerate stored plans/runs | no |

`gates plan`/`run`/`import` are in `SEALED_VERBS`, so every invocation
also seals a `CadHeadlessRunRecord` exactly like the other mutating
verbs.

## Minimal-step plan

Each plan (`cad_gate_operator_plans`, `gplan-…`) is a **pure function of
the manifest bytes**: 3 auto steps + exactly 1 operator step.

1. `environment_snapshot` — pin tool/OS/Python/env fingerprint.
2. `evidence_ref_integrity` — every declared `evidence_refs` must
   resolve repo-locally (same rules as `scripts/issue_lifecycle.py`:
   absolute paths and `..` escapes can never be evidence).
3. `prior_evidence_listing` — list earlier sealed runs for the same
   gate.
4. `operator` — the human step carrying the gate's manifest
   description.

The operator step resolves **only** from `--attest`; nothing in the
toolchain can auto-pass it.

## Honest verdicts

`CadGateAcceptanceRun.verdict` derives fail-closed from step results:

- `gate_satisfied` — all auto steps passed **and** the operator
  attested. `non_claims` records that the operator step is a human
  attestation, not machine proof, and that the record is execution
  evidence — not the issue's domain acceptance itself.
- `awaiting_attestation` — operator step pending → CLI outcome
  `blocked` (exit 3).
- `steps_failed` — any auto check failed → CLI outcome `failed`
  (exit 2).

## Cross-machine evidence

`gates run --out FILE` writes a `htdt-gate-run-export-1` envelope.
`gates import --run-file FILE` re-validates the envelope and re-derives
the record's `run_sha256`/`run_id` inside the model validator — a
tampered or schema-drifted export can never enter the store. Re-import
of an existing run reports `already_imported` (idempotent).

## Schema

`cad_gate_operator_plans` and `cad_gate_acceptance_runs` are append-only
sealed tables behind `_SealedStore` (NATIVE_SCHEMA_VERSION 114). Both
are registered in `NATIVE_SCHEMA_TABLES`, `native_row_integrity`
bindings, `native_authority_audit` replay probes, and the lifecycle
table-label map.

## Files

- `backend/src/htdt/cad_lifecycle_gates.py` — manifest reader, plan
  builder, run executor, export/import codec.
- `backend/src/htdt/cad_lifecycle_gates_repository.py` — two
  `_SealedStore` tables + listings.
- `backend/src/htdt/cad_headless_cli.py` — `HeadlessGatePlanSpec`,
  sealed-verb + alias registration.
- `backend/src/htdt/headless_cli.py` — `gates` family parser/handlers,
  `records` kinds `gate_plan`/`gate_acceptance_run`.
- `backend/tests/test_rev72_gate_plans.py` — 13 tests.

## Honest gaps

- Operator attestations are trust-bound: the record pins *who claimed
  what where*, it cannot prove the physical action occurred.
- `evidence_ref_integrity` resolves refs against the executing machine's
  checkout; refs produced on another machine need `--evidence` files
  attached at run time.
- The operator step is intentionally single-shot; multi-person sign-off
  chains are out of scope.
