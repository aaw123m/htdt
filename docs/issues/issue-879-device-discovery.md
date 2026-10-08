# Issue #879 slice — safe device discovery + capability handshake

Scope: the read-only commissioning front of #879. HTDT can now run an
operator-scoped device discovery, identify endpoints, negotiate
capabilities, record the operator's explicit bind decision, persist a
trusted endpoint, and resolve it into exactly the coordinates #868
(orchestrator), #806 (deployment) and #878 (`DeploymentPipelineService`)
consume — all fail-closed, nothing auto-deploys.

New modules:

- `cad_device_discovery.py` — `DiscoveryBackend` ABC + real-backend stubs
  (mDNS/SSDP/vendor-documented fail closed, configured-endpoint scan is
  the bounded lane), `FakeDiscoveryBackend`/`FakeDiscoveryScenario`,
  `CapabilityProber` protocol + `AdapterCapabilityProber`/
  `FakeCapabilityProber`, the six sealed records, the
  `DeviceDiscoveryService` ladder driver, `resolve_trusted_target`, and
  `DEVICE_DISCOVERY_LABELS` (JA).
- `cad_device_discovery_repository.py` — `CadDeviceDiscoveryRepository`
  with six `_SealedStore`s.

## The ladder

`DISCOVER → IDENTIFY → CAPABILITY PROBE → USER BIND → TRUSTED ENDPOINT →
USE ADAPTER`

Each rung is a distinct sealed record; a later record references only
records sealed before it (`AuthorityRef`). Nothing in the module mutates
device state: the only device-touching calls are the backend's read-only
`discover`/`probe_identity` and the prober's read-only
`probe_capability`.

## State / verdict vocabulary

**Discovery mechanisms** (`DiscoveryMechanism`): `mdns` · `ssdp` ·
`vendor_documented` · `configured_endpoint_scan` · `manual_entry`.
Real backends (`MdnsDiscoveryBackend`, `SsdpDiscoveryBackend`,
`VendorDiscoveryBackend`) report `available()=False` on this build and
produce an honest `outcome='unavailable'` run — never a fake sweep.
`ConfiguredEndpointScanBackend` contacts exactly
`scope.approved_endpoints` (capped by `max_endpoints`) through an
injected read-only prober.

**Scan scope** (`DiscoveryScanScope`): at least one of
`approved_endpoints` / `approved_service_types` / `approved_networks`
must be non-empty — an empty scope is an unrestricted silent LAN sweep
request and fails closed at construction. The run record pins the scope
sha + `scope_approved_by`.

**Run outcomes** (`DiscoveryRunRecord.outcome`): `completed` ·
`unavailable` · `scope_rejected` · `failed`.

**Identity states** (`DiscoveredDeviceRecord.identity_state`):
`identified` (manufacturer+model observed) · `partial` (some identity
fields) · `unidentified` (nothing identifying answered) · `ambiguous`
(identical advertised identity on several endpoints with no stable
discriminator — `ambiguity_group` links the cluster) · `manual_entry`
(operator-declared fallback, `evidence_basis='operator_declared'`).

**Ambiguity rule**: duplicates are ambiguous only when pairwise stable
identity cannot tell them apart; distinct stable identities make each
unit individually bindable. The service never picks one — binding an
ambiguous device requires `disambiguation_basis`.

**Probe outcomes** (`CapabilityProbeRecord.outcome`): `probed` ·
`unreachable` · `refused` · `insufficient` (endpoint answered but no
manifest) · `unsupported_adapter` (manifest belongs to another adapter).
Probing is read-only and runs on any endpoint — including ambiguous or
unidentified ones — because probe output may be the operator's
disambiguation evidence. Identity gates live in `bind()`.

**Trust states** (`TrustedDeviceBinding.trust_state`, chain semantics):
`trusted` → `invalidated_drift` / `revoked` / `superseded` /
`unverifiable`. One logical binding is a chain of records sharing
`binding_id`; transitions append a successor pinning
`supersedes_record_sha256`. `derive_binding_state` reads the chain head.
A binding is `trusted` only with a real identity basis
(`device_stable_id`, `operator_declared`, or an explicit
`disambiguation_basis`); endpoint-only identity stays `unverifiable`
and can never resolve.

**Drift verdicts** (`DeviceIdentityDriftReport`): `unchanged` ·
`replacement_suspect` (stable identity changed) · `drift_invalidates`
(firmware/capability change or endpoint unreachable) · `unverifiable`
(stable identity lost / endpoint-only binding cannot prove sameness).
`recheck_binding` re-observes the endpoint through the backend's
`probe_identity`; confirmed drift demotes the chain head to
`invalidated_drift`/`unverifiable` — a stale binding is never silently
re-used. `RebindingDecision` seals the operator's response
(`rebound_same_device` / `bound_replacement` / `kept_invalidated` /
`revoked`).

## Evidence model

| Record | Id | Purpose |
| --- | --- | --- |
| `DiscoveryRunRecord` | `disc-run-` | one sealed pass over one approved scope |
| `DiscoveredDeviceRecord` | `disdev-` | one endpoint observation + identity state |
| `CapabilityProbeRecord` | `cprob-` | negotiation result + capability snapshot sha |
| `TrustedDeviceBinding` | `tdbr-` | operator bind decision (chain head = state) |
| `DeviceIdentityDriftReport` | `didr-` | sealed drift verdict on re-verification |
| `RebindingDecision` | `rbd-` | operator's drift response |

Tables: `cad_discovery_runs`, `cad_discovered_devices`,
`cad_capability_probe_records`, `cad_trusted_device_bindings`,
`cad_device_identity_drift_reports`, `cad_device_rebinding_decisions`
(schema v105). Row bindings + replay probes are wired so the audit
machinery replays every row.

## Credentials

`credential_ref` carries only a secret *name* (the #726 convention —
values containing `pass`/`token`/`secret_value`/`key=` are rejected at
validation). Credentials never enter discovery evidence, binding
records, or the resolved target.

## Integration points

- `resolve_trusted_target(binding, probe, device)` →
  `ResolvedTrustedTarget{target_ref, adapter_id, adapter_binding
  (AdapterDeviceBinding), capability (AdapterCapabilityReport),
  credential_ref, warnings}` — exactly the fields a #878
  `DeploymentPathCandidate`/`DeploymentPipelineService` or the #868
  orchestrator needs; `adapter_binding.device_serial` carries the
  endpoint per the AVR/CamillaDSP locator convention.
- `AdapterCapabilityProber` wraps any adapter exposing
  `capability()` (#878 contract) plus an optional
  `firmware_version(endpoint)` read hook.
- `DEVICE_DISCOVERY_LABELS` provides the JA strings; lifecycle table
  labels registered for all six tables.

## What remains device-only

- Real mDNS/SSDP/vendor-documented enumeration is stubbed — the bounded
  lane today is `ConfiguredEndpointScanBackend` with an operator-supplied
  read-only prober, or `manual_entry`.
- Firmware/capability re-negotiation on real hardware depends on the
  adapter's own read hooks; the contract only consumes what a prober
  actually observed.
