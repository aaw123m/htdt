# Issue #790 — playback-electronics authority (cad_playback_electronics)

Status: authority core implemented (models, evaluators, sealed repository, schema
v80, replay-audit probes, row bindings, tests). No UI or report surface is wired.

## Scope implemented

A sealed domain authority for the **electrical audio path** — the
AVR / processor / DSP / DAC / preamp / power-amplifier chain — so that an
acoustic residual is never silently attributed to speaker/room while the
upstream electronics were never verified over the levels, frequencies, loads
and channel counts the theater actually uses.

Module: `backend/src/htdt/cad_playback_electronics.py`
Repository: `backend/src/htdt/cad_playback_electronics_repository.py`
Tests: `backend/tests/test_issue_790_electronics.py` (50 tests)

### Model types (all sealed, `ConfigDict(frozen=True)`, digest-derived ids)

| Type | Id | Table | Purpose |
|---|---|---|---|
| `ElectronicAudioPathProfile` | `eapp-` | `cad_electronic_audio_path_profiles` | Exact path/state identity: path class, endpoints, ordered stages, device/hw/firmware, channel scope, sample rate/format, trims, processing preset, provider bypass label vs observed processing states, channels driven, load kind/impedance, supply/thermal state, channel match tolerances. |
| `ElectricalTransferMeasurement` | `etm-` | `cad_electrical_transfer_measurements` | One bound measurement on one channel: magnitude/phase/complex refs, gain, delay + method, stimulus kind/level, noise, de-embedding state + interface-correction ref, method refs, digital-path semantics (formats, reference convention, word length, SRC/dither/clock state, bit-perfect flag), drive/load/thermal state, uncertainty. |
| `ElectronicLinearityEvidence` | `ele-` | `cad_electronic_linearity_evidence` | Level-sweep points (`linear/compressed/limited/clipped/protected/indeterminate/unknown`) + method-pinned nonlinear quantities (THD, THD+N, harmonics, IMD, difference-frequency, DIM-style, level-dependent gain) retaining unit/stimulus/bandwidth/level/load; domain tag (`electronic_path` vs `electroacoustic_system` vs `measurement_chain`); safe level bound. |
| `PlaybackElectronicsQualification` | `peq-` | `cad_playback_electronics_qualifications` | Sealed verdict record: qualification state, evidence composition (`ideal_electronics_assumed` … `device_transfer_unknown`), valid frequency/level domain, limitation reasons, channel matching results, bypass verdict, staleness ref. |

`ElectronicsLevelPoint` and `NonlinearQuantityEvidence` are frozen nested
evidence objects inside the linearity record.

### Evaluator — `evaluate_electronics_claim`

Fail-closed verdict ladder (first match wins):

1. `unknown` — no profile, or sealed qualification state `unknown`.
2. `stale_after_device_change` — supplied current device-state ref no longer
   matches the profile, or the qualification carries `staled_by_ref` (#592/#595).
3. `insufficient_evidence` — no measured evidence bound to this exact profile
   seal (predicted/simulated/vendor-declared origins never bind), or no
   qualification record at all.
4. `state_dependent` — every bound measurement conflicts with the profile on
   material state (currently sample rate), i.e. the evidence exists but for a
   different applicability identity (#739).
5. `interface_confounded` — every bound measurement is `included_in_result` or
   `unknown` de-embedding: the interface transfer may be inside the number
   (#699).
6. `processing_detected` — a provider bypass label is contradicted by observed
   processing states (label is provider semantics until transfer supports it).
7. `nonlinear_limited` — any bound level point observed `clipped` or
   `protected`.
8. `channel_mismatch` — qualification gain/phase difference exceeds the
   profile's declared matching tolerances (matching ≠ crosstalk; #650 stays
   separate).
9. `path_unverified` — sealed qualification says so.
10. `measured_processing_effects` — processing observed on the path (qualifier
    `bypass_verdict`, profile observations, or qualification state).
11. `electronics_qualified_with_limitations` — soft-limit points
    (`compressed`/`limited`), partially uncharacterized de-embedding, or the
    sealed qualification itself is limited.
12. `electronics_qualified` — bound measured evidence + sealed
    `qualified_linear_transfer` with `measured_linear_transfer_applied`
    composition and an explicit valid domain.

Measurement binding requires id **and** sha match on `profile_ref`, the same
`path_class`, and `evidence_origin == 'measured'`. Qualification binding
requires the same `profile_ref` match plus every element of `measurement_refs`
being among the bound measurements — sealed references to other paths or to
unmeasured evidence do not support a verdict.

`qualify_electronics_path` and `playback_electronics_summary` wrap the
evaluator for record-building and reporting; `verdict_allows_linear_assumption`
gates the prediction composition so `ideal_electronics_assumed` is never
returned when any measured evidence exists (#564/#566 must not fit around a
known upstream error).

## Semantics kept distinct

- Predicted vs measured: `evidence_origin` separates `measured`,
  `predicted`, `simulated`, `vendor_declared`; only `measured` binds.
- Magnitude / phase / delay / absolute gain are separate fields/refs — a flat
  magnitude never substitutes for phase or delay.
- Nonlinear quantities keep `method_reference@edition` pinning plus stimulus,
  bandwidth, signal level and load — never collapsed to one scalar.
- De-embedding state is explicit (`de_embedded` requires an interface
  correction ref; `negligible_with_evidence` requires a ref or notes).
- Digital-path semantics (formats, dBFS convention, word length, SRC, dither,
  clock, bit-perfect flag) are first-class on the measurement.
- Channel matching (gain/phase difference vs declared tolerance) is checked
  here; leakage/crosstalk stays #650.
- `none_detected` cannot co-occur with other observed processing states.

## Standards cited

IEC 60268-3:2018 (analogue amplifiers), AES17-2020 (digital audio measurement),
ANSI/CTA-490-B (amplifier/receiver test conditions). Method references use the
`identifier@edition` convention; historical results retain their edition.

## Boundaries respected

#699 (interface loopback), #650 (crosstalk), #651 (gain/noise-floor),
#593 (amplifier↔load/headroom), #649 (DRC/limiter state), #192 (electroacoustic
distortion), #695/#699 (measurement-chain), #606 (hum), #592/#595 (device
epochs), #739 (SRC), #744 (dither), #609/#745 (clock), #564/#566 (residual
fitting), #568/#723 (deployed DSP), #599 (profile registry), #602 (exposure)
keep ownership; this module consumes their state as refs/fields, never
re-derives it.

## Schema / wiring

- `NATIVE_SCHEMA_VERSION` 79 → **80**; `_migrate_79_to_80` runs all baseline
  DDL (idempotent `IF NOT EXISTS`).
- 4 tables + 4 `document_id` indexes in `NATIVE_BASELINE_DDL` /
  `NATIVE_SCHEMA_TABLES`.
- `_ROW_BINDINGS` entries for all 4 payload tables (canonical-payload
  completeness gate passes).
- `_RepositoryChain` branch `playback_electronics` + 4 `_ReplayProbe`s; all 4
  tables report `replay_canonical` in `audit_table_modes()`.
- `test_cad_schema.py` ledger extended with `(80, 'migrate native schema to v80')`.

## Repository semantics

Append-only sealed stores: `save` re-verifies the seal (`payload_sha256 ==
canonical_sha256(identity_payload())`), rejects redefinition of a sealed id
(`ConflictError`), and is idempotent for byte-identical re-saves. `get`
re-verifies id, sha and every bound column (NULL↔None, bool→int); `list`
verifies the `document_id` column per row and re-checks the seal. Any
tampering raises `PlaybackElectronicsIntegrityError`.

## Remainder / not in this slice

- No UI, report generation, or ingestion pipeline wiring (authority core only).
- `qualify_electronics_path` composes qualification records but real
  instrument ingestion must come from a measurement adapter.
- Epoch integration (#592/#595) is via `device_state_ref`/`staled_by_ref`
  references; the device-epoch authority owns the epoch registry itself.
- Crosstalk (#650), amplifier headroom (#593) and noise-floor (#651)
  authorities are consumers' obligations; this module exposes the matching
  tolerances and load/drive state they need.
- `scripts/issue_verification_manifest.yaml` untouched per scope.
