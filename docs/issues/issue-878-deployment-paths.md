# Issue #878 slice — target-adapter capability negotiation + strongest-path selection

Scope: the deployment-path layer of #878. Given a target binding and a set of
candidate adapters, HTDT can now publish each target's capability manifest,
drive a sealed staged deploy transaction, rank paths by evidence strength, and
fall back to a typed assisted-attestation lane — all fail-closed.

New modules:

- `cad_deployment_pipeline.py` — capability/evaluator/pipeline/assisted/APO
  records plus `DeploymentPipelineService`.
- `cad_avr_lan_adapter.py` — documented-LAN (Denon/Marantz telnet grammar)
  channel-trim adapter with `AvrLanTransport` protocol and `FakeAvrLanTransport`.
- `cad_deployment_pipeline_repository.py` — `_SealedStore` persistence.

## State / verdict vocabulary

**Pipeline stages** (`DeploymentPipelineRecord.stage`):
`opened → baseline_captured | baseline_unavailable → compiled → previewed →
authorized → applied | partial_write | failed → readback_matched |
readback_diverged → rollback_verified | rollback_failed`; `aborted` from any
pre-apply stage. `derive_pipeline_stage()` re-derives current stage from the
sealed record chain — the record is the state.

**Evidence strength** (`DeploymentEvidenceStrength`, strongest→weakest):
`machine_readback > applied_ack > file_verified > assisted_attestation >
unverified > none`. `derive_pipeline_evidence_strength` computes it from the
pinned mechanism/verdict fields; the record validator rejects any stored
strength that disagrees, so machine-readback evidence is never claimable from
file writes or below the declared mechanism.

**Readback verdicts**: `matched / diverged / unavailable / not_attempted`.
**Partial-write state**: `none / complete / partial / failed / not_attempted`
— a `partial` write without readback derives `unverified`, never `applied_ack`.
**Path availability**: `available / conditional / unavailable`.

## Capability manifest

`AdapterCapabilityReport` extends with `deploy_mechanism`,
`readback_mechanism`, `rollback_mechanism`, `runtime_observation`,
`supported_features`, `limit_notes`, `auth_requirements`, `applicability`,
`protocol_authority` (`documented | open_source | simulated | none |
unknown`). All fields default fail-closed (`none`/`unknown`), so existing
producers keep working; the #838 registries (`cad_camilladsp_deploy`,
`cad_device_adapter_file`, `cad_minidsp_export`, `cad_rew_provider`,
`cad_measurement_target_registry`) publish them through
`DeploymentCapabilityDeclaration` via `build_capability_declaration()`.

`protocol_authority='simulated'` (or `adapter_kind='simulated'`) keeps the
rank lane for tests but marks `production_eligible=False` on the verdict —
reverse-engineered/private protocols and GUI click automation are not
production authority.

## Pipeline transaction

`DeploymentPipelineService(adapter, binding, target_ref=..., document_id=...)`
drives one pipeline run:

1. `open()` — baseline via `capture_baseline` (machine capture) else
   `read_back`, else `baseline_unavailable`.
2. `compile(export)` — `materialize` + `validate_materialization_result`;
   undeclared items surface as `unsupported_items`, never silently dropped.
3. `preview()` — sealed `SemanticDiffEntry` set (channel field diffs vs
   baseline; `unobserved_before` when the channel was not observed).
4. `authorize(operator_id, scope, at)` — `DeploymentOperatorAuthorization`
   (scope `apply`/`rollback`, one-shot) pins the candidate materialization sha;
   apply requires `previewed`, rollback requires a mutation stage.
5. `apply(auth)` — validates scope/pipeline/consumed/pin *before* stage gate;
   `AvrLanApplyError`-style exceptions with unit counts record
   `partial_write`; the consumed authorization is persisted.
6. `verify_readback()` — adapter `read_back` → observation snapshot →
   `matched`/`diverged` with mechanism + sha pinned.
7. `rollback(auth)` — rollback-scope authorization + adapter
   `rollback_previous` → `rollback_verified`/`rollback_failed`.

## miniDSP deployability registry

`MINIDSP_DEPLOYABILITY` builds at import from `list_dsp_target_profiles()`
(`target_family == 'minidsp_biquad_export'`) — 23 sealed
`MinidspModelDeployEntry` records, all `assisted_only`: no documented
machine-deploy API is pinned for any miniDSP model, so none can claim
`machine_deployable`. Unknown profile ids raise
`UnknownMinidspModelError` (fail-closed).

## AVR LAN adapter

`AvrLanCalibrationAdapter` claims only the documented telnet grammar:
`CV<ch> <val>` channel trims (38–62, 0.5 dB steps, 50 = 0 dB) with
`CV<ch> ?` machine readback and baseline restore for rollback. Delay,
crossovers, PEQ/FIR and routing are undeclared → `unsupported_items` on
materialize (gate-blocking). Transports resolve from the binding's endpoint;
non-loopback endpoints must be pre-approved
(`approved_remote_endpoints`, `operator_confirmed`). `FakeAvrLanTransport` is
in-memory and documents itself as simulated on the record.

## Equalizer APO install verification

`EqualizerApoInstaller.install()` writes bytes-exact, re-reads the installed
file, and seals `ApoInstallRecord` with `rendered_sha256` vs
`installed_sha256` — `file_verified` evidence requires content-identity
(`verification='content_sha_matched'`), distinct from "file written".
Optional semantic check runs `verify_exported_apo_config` on the *installed*
bytes when `channels` are given.

## Strongest-path evaluator

`evaluate_deployment_path(candidate)` → `DeploymentPathVerdict`
(`evidence_strength`, `production_eligible`, `availability`, `reasons`).
`rank_deployment_paths`, `select_strongest_deployment_path`, and
`select_strongest_production_path` expose ordering; the #868 orchestrator
calls them through `evaluate_deployment_paths` / `select_deployment_path`.
No viable lane → availability `unavailable`, fail-closed.

## Assisted fallback

`AssistedInstructionManifest` (sealed ordered steps, AuthorityRef artifacts,
`post_measurement_required` default True) +
`AssistedDeploymentAttestation` (all declared steps must be attested;
undeclared step indexes rejected; strength pinned `assisted_attestation` —
structurally below `machine_readback` on every record).

## Device-only acceptance gates remaining

- A real Denon/Marantz telnet transport (`AvrLanTransport` over a socket)
  plus on-box validation against physical AVR hardware.
- Any miniDSP machine-deploy path remains assisted-only until a documented
  API is pinned; per-model registry entries would need re-classification.
- Runtime-observation wiring (`runtime_observation='telemetry'`) is
  declared on the CamillaDSP manifest but no streaming consumer exists yet.
