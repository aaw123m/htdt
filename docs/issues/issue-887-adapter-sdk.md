# Issue #887 slice — stable provider/device adapter SDK + conformance suite

Scope: one versioned adapter contract every provider/device adapter in
the tree declares (REW, CamillaDSP, miniDSP, AVR LAN, file targets,
discovery backends, delegated providers), plus an executable
conformance suite that gates whether an adapter may *claim* conformance
— and honest recorded outcomes for every in-repo adapter.

New modules:

- `cad_adapter_sdk.py` — the versioned `AdapterSdkContract` descriptor
  (sealed, `asd-`), `AdapterSafetyInvariants`,
  `FORBIDDEN_OPERATION_KINDS`, `descriptor_ref`,
  `conformance_is_stale`, and the fail-closed `assert_conforming` gate.
- `cad_adapter_conformance.py` — the executable suite: 14 named
  scenarios, `ConformanceSuiteRunner`, sealed `ConformanceResultRecord`
  (`acr-`), per-scenario sealed `ConformanceScenarioResult` evidence
  (transcript + transcript sha + probes), the `ConformanceSubject`
  protocol, seven shipped subjects (one per in-repo adapter family),
  `in_repo_conformance_subjects`, `run_in_repo_conformance`.
- `cad_adapter_sdk_repository.py` — `CadAdapterSdkRepository` with two
  `_SealedStore`s over the v108 tables
  `cad_adapter_sdk_descriptors` / `cad_adapter_conformance_results`.

## Contract vocabulary (no parallel vocabulary)

The descriptor embeds the #878 `AdapterCapabilityReport` **verbatim**
and pins it with `capability_sha256` — mechanism literals
(`machine_write`/`file_export`/`manual_entry`/`none`,
`machine_exact`/`operator_captured_file`/`none`,
`previous_config`/`operator_only`/`none`, `telemetry`/`none`), filter
classes, and `protocol_authority` are reused, not re-declared.

`contract_status` records what the evidence *could* prove:
`production` (the full lane contract is verifiable), `assisted_only`
(operator-assisted lanes like miniDSP/APO), `read_only` (adapters that
never mutate), `simulated` (simulated adapter kind/authority — a
fixture may never mint `production` for a simulated adapter).

`AdapterSafetyInvariants` seals the contract's safety surface:
`mutation_requires_one_shot_authorization` (always true — adapters may
never auto-mutate), `forbidden_operations` drawn from the closed
`FORBIDDEN_OPERATION_KINDS` literal set (ambient LAN scans, credential
storage/echo, vendor-UI automation, unbounded retry), a required
`credential_boundary` (handles/vault names only — never values), and an
optional `rollback_note`.

## Verdict vocabulary

Four verdicts, never a bare pass:

- `conforming` — every required scenario passed, no limitations.
- `conforming_with_limitations` — required scenarios pass but optional
  scenarios are unverifiable, the contract isn't `production`, or the
  evidence provenance isn't `live`.
- `non_conforming` — at least one *required* scenario failed.
- `unverifiable` — a required scenario could not be determined; unknown
  is never silently promoted.

Required scenarios are derived from the adapter's own declared lanes:
every adapter proves `happy_path` + `unsupported_feature`; `apply` adds
`authentication_failure`; `read_back`/`file_verify` adds
`readback_mismatch`; `rollback` adds `rollback_success`. Optional
scenarios the adapter can't exercise are recorded as limitations, never
faked.

`evidence_provenance` on the subject (`live`/`simulated`/`file`/
`read_only`) is always honest: simulated transports can never claim
protocol conformance, so their outcomes stay
`conforming_with_limitations` with `evidence_provenance=simulated`.

## Re-derivable conformance

Every `ConformanceResultRecord` pins `descriptor_ref`
(`AuthorityRef(kind='adapter_sdk_contract')` → descriptor sha) +
`suite_version` + `contract_version`; each scenario pins
`transcript_sha256`. `conformance_is_stale` is the staleness predicate:
descriptor-sha drift or suite-version bump → stale. `assert_conforming`
is the downstream fail-closed gate raising `AdapterConformanceError`
with a typed `.kind`: `insufficient_version` · `unproven` (no result) ·
`stale_descriptor` · `stale_suite` · `non_conforming` · `unverifiable` ·
`limited` (`conforming_with_limitations` with `allow_limitations=False`).

## Recorded in-repo outcomes

`run_in_repo_conformance` records today's honest matrix:

| adapter | verdict | honest limitations |
|---|---|---|
| `htdt-avr-lan` | `conforming_with_limitations` | simulated transport, `contract_status=simulated`, cancellation lane unverifiable |
| `htdt-camilladsp-deploy` | `conforming_with_limitations` | production contract, simulated evidence provenance |
| `htdt-minidsp-biquad-export` | `conforming_with_limitations` | `assisted_only`, file provenance; live-deploy scenarios unverifiable |
| `htdt-file-adapter` | `conforming_with_limitations` | `assisted_only`, file provenance |
| `equalizer-apo-installer` | `conforming_with_limitations` | `assisted_only`, file provenance |
| `configured-endpoint-scan` | `conforming_with_limitations` | `read_only`, read-only provenance |
| `htdt-rew-api` | `conforming_with_limitations` | `read_only`, read-only provenance |

No in-repo adapter mints unconditional `conforming` — the honest lane
is always recorded.

## Integration points

- `assert_conforming(descriptor, result, suite_version=...)` is the
  check downstream deployment (#868/#878) and discovery (#879) paths
  call before trusting an adapter; `latest_result_for(descriptor_sha)`
  on the repository returns the freshest sealed result for exactly the
  descriptor held.
- Schema v108 adds `cad_adapter_sdk_descriptors` +
  `cad_adapter_conformance_results` (append-only, replay-canonical
  audit coverage, row-integrity bound, JA lifecycle labels registered).

## What stays device-only

Live transports can never be exercised here: hardware timeouts,
partial-write counts on a real AVR, CamillaDSP daemon rollback on a
real device, real LAN discovery — all remain device-only. The suite
verifies the *contract* behaviors over deterministic fakes and records
the provenance ceiling; live evidence is a future `live` provenance run
against real hardware, minting fresh results under a new descriptor
sha — never an upgrade of today's records.
