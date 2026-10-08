# Issue #888 — headless `htdt` CLI + automation API

## Scope

One `htdt <family> <verb>` dispatcher (`htdt.headless_cli:main`, installed as
the `htdt` console script) that drives the sealed commissioning authorities
built by #869/#875/#876/#877/#878/#885/#879 without the GUI. It is an
automation lane, not a parallel authority: every verb reuses the canonical
services and writes through their sealed repositories.

The headless path never imports Qt. `htdt.headless_cli`,
`htdt.cad_headless_cli`, and `htdt.cad_headless_cli_repository` pull only
`cad_*` modules; `test_headless_cli_import_is_qt_free` asserts the module
graph stays Qt-free under `QT_QPA_PLATFORM=offscreen`.

## Verb map

| Verb | Authority driven | Mutating? |
|---|---|---|
| `htdt status` | build info + backend availability | no |
| `htdt project create` | `ProjectLibraryRepository` | yes |
| `htdt project inspect` | project lookup by id / document_id | no |
| `htdt sweep run` | `MeasurementAcquisitionEngine` (#869) | yes |
| `htdt campaign plan` | `build_campaign_execution_plan` (#875) | yes |
| `htdt campaign run` | campaign runner (#875) | yes |
| `htdt channel-verify plan` | `build_verification_plan` (#876) | yes |
| `htdt channel-verify run` | `run_verification_plan` (#876) | yes |
| `htdt calibration run` | `CalibrationWizard` lanes (#877) | yes |
| `htdt deploy run` | `DeploymentPipelineService` (#878) | yes |
| `htdt diagnose run` | `DiagnosticOrchestrator` (#885) | yes |
| `htdt records list` | sealed-record enumeration | no |
| `htdt records export` | sealed-record payload dump | no |

Every mutating verb (membership in `SEALED_VERBS`) seals exactly one
`CadHeadlessRunRecord` (`hrun-…`) per invocation — including refused
(`unauthorized`), `blocked`, and `failed` attempts. Refusals are evidence,
not silent exits.

## Machine-readable contract

`--json` emits a single envelope on stdout:

```json
{
  "format": "htdt-headless-result-1",
  "verb": "sweep.run",
  "outcome": "succeeded",
  "exit_code": 0,
  "verdict": "limited",
  "reason": null,
  "run_record_id": "hrun-…",
  "run_sha256": "…",
  "records": [{"kind": "sweep_acquisition_run", "ref_id": "…", "ref_sha256": "…"}],
  "data": { }
}
```

`records` carries `AuthorityRef` entries pinning every sealed artifact the
verb produced (plans, runs, verdicts, sessions). `run_record_id` is null for
read-only verbs, dry runs, and failures that occurred before a document
context existed. Without `--json` the same envelope is pretty-printed —
there is no prose output channel.

## Exit-code lattice (fail-closed)

| code | outcome | meaning |
|---|---|---|
| 0 | `succeeded` | the workflow's own verdict succeeded |
| 0 | `dry_run` | planned; nothing persisted |
| 1 | — | CLI usage error (bad/missing flags) |
| 2 | `failed` | workflow verdict failed |
| 3 | `blocked` | honest insufficiency (precheck blocked, parked at an operator gate, ambiguous/incomplete/unknown evidence) |
| 4 | `unauthorized` | required authorization flag absent |
| 5 | `missing_evidence` | spec/plan/record/document not found or invalid |
| 6 | — | unhandled internal error |
| 7 | `cancelled` | operator/timeout cancellation |

`0` is only ever returned on `succeeded` or `dry_run`. Partial progress,
ambiguous evidence, and parked operator gates are never zero.

## Authorization flags (session-only, never in spec files)

- `--arm` — required by `sweep run`, `campaign run`, `channel-verify run`,
  `calibration run`, `diagnose run`: explicit operator arming for device I/O.
- `--auto-confirm-position` — `campaign run`: attests operator position
  confirmations instead of parking.
- `--confirm-hardware` — `calibration run`: passes `await_*` hardware gates
  (bounded by `spec.max_steps`).
- `--authorize-apply OP` / `--authorize-rollback OP` — `deploy run`: the
  operator id minted into one-shot `DeploymentOperatorAuthorization` tokens.
- `--authorize OP` — `diagnose run`: authorizes `device_mutation` plans.

No credential flags exist: credentials live in the vault authority (#890) as
handles only, and never appear in argv or spec files.

## Evidence / reproducibility model

`cad_headless_run_records` (schema v108, append-only, `_SealedStore`)
persists one `CadHeadlessRunRecord` per mutating invocation. Each record
pins:

- `spec_sha256` + canonical `spec_json` (or flag-derived spec material for
  spec-less verbs like `project create`);
- `tool_version` (build-info version incl. `+g<sha>.dirty`), `tool_commit_sha`,
  `tool_commit_dirty`, `python_version`, `platform`, `env_fingerprint`
  (sha1 of interpreter + OS + machine + installed distributions),
  `argv_sha256` — the full reproducibility identity per #833 conventions;
- `backend_id` / `backend_is_simulated` — simulated evidence is labeled,
  never upgraded;
- `verb`, `outcome`, `verdict`, `reason`, `record_refs`, `dry_run`,
  `cancelled`, `timeout_seconds`, `started_at_utc`, `finished_at_utc`,
  `elapsed_ms`, `authority_version` (`headless-cli-1`).

`--dry-run` performs no sealed writes: sweep does engine precheck only;
`*.plan` verbs return the built plan unwritten; calibration uses an
in-memory `WizardStores()`; deployment/diagnostic return before touching a
repository. Dry runs never create `cad-scenes.sqlite3`.

`--timeout-seconds` arms a cooperative deadline: engine `cancel()` on a
running acquisition, runner/wizard/orchestrator cancel or abort in driver
loops → `cancelled` exit 7.

Plan reruns are replay-safe: `--plan FILE` payloads are re-validated through
the sealed store before execution, so tampered plan files fail closed.

## What remains device-only

- `backend: wasapi` — `WasapiAudioBackend` reports `available() == False`
  here (headless box has no audio stack); verbs that select it fail closed
  at engine precheck rather than pretending. `status` reports it as
  unavailable with `unavailable_reason`.
- Channel verification on a simulated backend can only reach
  `machine_assisted_ambiguous` → `blocked`; `verified` requires real
  acoustic arrivals (issue #876's evidence-strength ladder).
- `deploy run` apply/readback on the `avr-lan` adapter exercises the real
  telnet grammar through `FakeAvrLanTransport` (simulated) unless an
  operator-approved remote endpoint is configured.
- Operator position confirmations, hardware attaches, and SPL-calibrator
  attaches are operator acts — the CLI records them as
  `operator_attest`/`--confirm-hardware` decisions rather than faking them.

## Integration points

- #833 verification manifests can invoke `htdt` verbs as subprocesses: the
  envelope format (`htdt-headless-result-1`) + exit lattice are stable
  contracts.
- `records export --kind <kind> --id <id>` replays sealed payloads for CI
  assertions; `records list` enumerates per document/kind.
- The e2e fixture in `test_issue_888_headless_cli.py` covers the acceptance
  slice: project create → fake-backend sweep → channel-verify plan+run →
  records export, entirely headless.
