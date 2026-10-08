# Issue 903 — fault-injection recovery drill harness (software slice)

Issue #903 is a product-quality acceptance plan. Its software-ownable
slice is an executable fault-injection / recovery drill over the already
landed recovery authorities — `#883` session journal + restore, `#889`
application update + rollback, `#884` diagnostic bundle export, `#890`
credential vault, `#815` error-boundary honesty. Per the issue
(実装のownerは既存Issueに残し、新規の恒久画面・authority familyの拡張を優先しない)
this is a **drill harness**, not a new UI or authority surface.

## Scope delivered

- `htdt/cad_recovery_drill.py` — deterministic drill harness: manifest
  (the drill matrix as data), per-drill fault injectors, fail-closed
  evaluator, sealed report model.
- `htdt drill list` / `htdt drill run` CLI family wired into the `#888`
  headless dispatcher (`headless_cli.py`). `drill run` mutates a sandbox
  and therefore requires `--arm`; `--dry-run` plans without injecting
  faults. `drill.run` is in `SEALED_VERBS` — every armed run seals a
  `CadHeadlessRunRecord`.
- `scripts/recovery_drill_manifest.json` — verbatim mirror of
  `builtin_drill_manifest()` so #903 acceptance can enumerate the matrix.
  Kept in sync by test.
- `backend/tests/test_issue_903_recovery_drill.py` — every verdict path,
  fail-closed rules, seal integrity, tamper detection, CLI gating.

## Vocabulary

- **Drill** — `RecoveryDrillSpec`: a named `fault` × a declared recovery
  `expectation` (`expected_terminal`, `expected_records`,
  `forbidden_terminals`, `required_checks`), plus `issue_refs` and a
  registered `implementation` key.
- **Observation** — `DrillObservation`: what the injector actually saw —
  observed `terminal`, emitted `AuthorityRef` records, `DrillCheck`s.
- **Check kinds** — `expectation` (the recovery path did what was
  promised), `preservation` (pre-fault user data survived byte-for-byte),
  `honesty` (nothing claimed success without evidence).
- **Verdicts** — `recovered_as_designed` | `loss_detected` |
  `false_success` | `harness_error`. `loss_detected` and `false_success`
  are `FAILING_DRILL_VERDICTS` and fail the drill; `harness_error` means
  the harness could not judge (unknown implementation key, implementation
  exception, or a required check never emitted) — fail-closed, never
  silently green.
- **Report** — `RecoveryDrillReport` (`htdt-recovery-drill-report-1`),
  self-sealing via `report_sha256` (canonical-JSON SHA over the payload
  with the field blanked); `overall_verdict` is `all_recovered` or
  `failures_detected`.

## Evaluator rules (fail-closed)

Terminal in `forbidden_terminals` → `false_success`. Terminal ≠
`expected_terminal` → `loss_detected`. Any failed `honesty` check →
`false_success`. Any failed `expectation`/`preservation` check, or a
required `expected_records` kind absent → `loss_detected`. Any
`required_checks` entry not emitted → `harness_error`. Only all-clean
→ `recovered_as_designed`.

## Drill matrix (9 drills)

| drill_id | fault | authority |
|---|---|---|
| `session-crash-mid-write` | abnormal journal close w/ open write intent, apply txn, acquisition stage | #883 |
| `session-journal-torn-tail` | mid-file journal corruption | #883 |
| `update-crash-mid-swap` | `BaseException` kill between swap renames | #889 |
| `update-crash-mid-rollback` | kill inside `restore_install` | #889 |
| `update-health-unverifiable` | health probe cannot verify | #889 |
| `diagnostics-bundle-collector-failure` | context provider raises | #884 |
| `vault-locked-denies-material` | locked vault deny → unlock restores | #890 |
| `vault-unavailable-fails-closed` | unavailable vault refuses store | #890 |
| `error-boundary-authority-honesty` | tampered sealed row → authority failure classified + reported | #815/#890 |

## Decision: no new sealed table

The slice spec allowed a `v111` sealed drill-run table *or* a JSON
report "if no new authority is warranted". Chosen: **JSON report +
existing `CadHeadlessRunRecord` seal**. Rationale: a drill run's durable
record is a headless run record (verb `drill.run`, spec payload =
manifest sha + selected ids, refs = every authority record the drills
emitted); a separate `cad_drill_*` table would duplicate that contract
without adding an authority boundary. Report files are content-addressed
by `report_sha256`, so `records export` + the `--out` JSON are sufficient
evidence for #903's result-binding row.

## Integration points

- Dispatcher reuse: `_FAMILY_PREFIX['drill']`, `_HANDLERS` entries, the
  shared `--dry-run`/`--arm`/timeout/data-dir flags, standard
  `htdt-headless-result-1` envelope + `OUTCOME_EXIT_CODES`.
- Evidence binds to the sandbox `work_root` (default
  `<data-dir>/recovery-drill`); each drill's artifacts live under
  `<work_root>/<drill_id>` and are rebuilt on each run (deterministic).
- `htdt records list --kind headless_run --verb drill.run` enumerates
  past runs; `drill run --out FILE` writes the self-sealing report.

## Device-only remainder (not software-ownable here)

- Real hardware faults (power loss mid-flash, cable pull mid-acquisition,
  driver crash mid-swap) — the drills inject their software-equivalents;
  physical fault rigs remain owner-tracked under #903.
- Windows-update timing and A/B partition behaviours on real devices.
- Long-haul soak (repeated crash cycles over days) — the matrix is
  single-pass deterministic; soak scheduling is an ops lane.
