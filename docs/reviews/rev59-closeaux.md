# REV59-CLOSEAUX — manifest→verification bridge authority

Parent self-track on `devin/1791294195-rev59-closeaux` (schema v75).

## Scope

`cad_manifest_verification.py` seals `issue_verification_manifest.yaml`
checks into the same fail-closed ledger as VERAUTO, so issue closure is
evaluated on committed evidence rather than hand-tracked status:

- `ManifestGate` (mgt-) — one manifest check made addressable:
  issue_ref + check_id + kind (pytest/script/manual) + description +
  test targets / argv / optional acquisition cells; `manifest_sha256`
  pins the file bytes so any manifest edit re-keys every gate.
  Device cells are structurally refused on automated checks — cells
  only belong on manual gates.
- `GateRunResult` (grr-) — one gate execution or physical-evidence
  commit. `evidence_committed` is the only manual outcome that
  satisfies a manual gate and requires a bound `AuthorityRef`.
- `load_manifest_gates(document_id, path)` — YAML → sealed gates,
  fail-closed on unknown kinds / missing ids / shapeless entries.
- `derive_verification_requirement(gate)` — a manual gate with
  declared cells synthesizes a VERAUTO `VerificationRequirement`
  (gate_label = manifest check id, evaluator `manual_review`), so the
  device run becomes a generated checklist; a manual gate without
  cells honestly stays a procedure entry.
- `evaluate_gate` / `evaluate_issue_verdict` — the manifest's own
  verdict rule on sealed records: `failing` (any automated check
  failed) > `manual_required` (no automated gates) > `verified` (all
  automated passed AND every manual gate has bound evidence) >
  `partially_verified`. Manual gates cannot be satisfied by a passing
  script — only committed evidence.

`cad_manifest_gate_repository.py` — two append-only stores
(cad_manifest_gates, cad_gate_run_results) with full row↔payload
integrity compare, idempotent saves, append-only conflict errors.

## Registration

schema v75 (`_migrate_74_to_75`); `NATIVE_BASELINE_DDL` +
`NATIVE_SCHEMA_TABLES` (2 tables); `_ROW_BINDINGS`; audit replay
probes (repo name `manifest_gates`); JA labels
(検証マニフェストゲート / ゲート実行結果).

## Tests

`test_rev59_closeaux.py` — 13 tests: kind/shape/cells validation,
manifest loading + sha pinning + malformed rejection, requirement
derivation, gate verdict ladder (manual ≠ script-passable), issue
verdict ladder, store roundtrip, tamper detection (id + sha columns),
idempotent save, fresh-migrate.

## Remainder

- Guided verification wizard UI consumes this bridge (child track).
- `evaluate_issue_verdict` drives the zero-knowledge closure display.

## Runner bridge

`scripts/commit_manifest_verification.py` — turns an
`issue_verification_report.json` run into sealed records: every
manifest check becomes a `ManifestGate` (file-sha pinned), every
reported automated-check status becomes a committed `GateRunResult`,
and per-issue `evaluate_issue_verdict` verdicts print JA status.
Manual checks stay for the wizard — only committed evidence satisfies
them. `--dry-run` evaluates without writing.
