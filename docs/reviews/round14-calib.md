# Round 14 — calibration / correction-chain depth

Calibration data (measurement calibration, channel gains, EQ/filter
corrections, level alignment) flows from import/config into solver
inputs, measurement comparisons, and UI indicators. A correction
applied twice, applied to the wrong channel, or silently ignored
produces wrong numbers presented as right ones. This round enumerated
the chain and fixed the paths where the wrong number was produced.

## The calibration chain, as enumerated

External-format calibration enters through two importers that both
produce the shared `ImportedCalibrationArtifact` contract
(`cad_external_calibration.py`):

- `build_equalizer_apo_artifact` — Equalizer APO `config.txt` (Preamp,
  Delay, Filter, Channel/Device scope, Include, Convolution).
- `build_camilladsp_artifact` — CamillaDSP YAML/JSON (Biquad, Gain,
  Delay, Conv; pipeline `channels`/`channel`/`bypassed`).

Downstream consumers:

- `compare_imported_vs_exported` — field-level audit of the imported
  artifact against an HTDT export snapshot; emits
  `ImportedFieldComparison` items per channel/field.
- `cad_calibration_workflow.py` — honest apply lifecycle
  (`mark_user_applied` validates the export belongs to the plan
  revision).
- `cad_wave_excitation.py` `VolumeVelocityTableConverter.calibration_scale`
  — linear scale applied to volume-velocity solver input (real and
  imag parts; correct — linear domain at the solver boundary).
- `comparison.py` `level_offset_db` — computed fresh per comparison
  over the reference band; feeds `shape_rms` only, never stored
  "applied" — no double-apply path.
- `IrCalibrationState` (measurement_ir / repository) — declaration
  sealed via `dataset_sha256`; importer rederives samples only.
- UI 'calibrated' indicators (`measurement_instrument_onboarding`,
  `readiness`, `cad_auralization_service`, `App.tsx`) — honest: a
  record/measurement exists behind each flag.

## Fixed findings

### Equalizer APO importer (`cad_external_calibration.py`)

- **F14-1 Preamp/Delay overwritten instead of accumulated (HIGH).**
  Multiple `Preamp:` commands summing in dB is documented E-APO
  behavior since 0.8; the parser assigned `bucket['preamp_db'] = value`
  (last-wins). `Preamp: -3` then `Preamp: -1` imported -1 dB instead of
  -4 dB. Same overwrite for `Delay:` and for every value merged from
  an `Include:` file (parent -1 dB + sub -2 dB → -2 instead of -3).
  All paths now accumulate.

- **F14-2 `Include:` dropped channel scope (HIGH).** The recursive
  parse used a fresh `scope = ()`, so a `Channel: L`-scoped include
  landed its filters on the `ALL` bucket instead of L, and a `Channel:`
  inside the include never propagated out to the commands after it.
  E-APO's `Include:` is textual inclusion at the current position —
  scope now threads in (`initial_scope`) and out (returned scope).
  `test_convolution_recorded_as_file_dependency` pinned the bug; its
  expectation is updated.

- **F14-3 `Channel: L ALL` double-apply (MEDIUM).** A token list
  containing `ALL` meant the delay/filter hit both the `L` bucket and
  the `ALL` bucket — and, with F14-4's ALL-folding, would count twice
  on L. `ALL` in a list now selects all channels (scope `()`), as in
  E-APO.

- **F14-4 Comparator mis-computed the effective correction (HIGH).**
  `compare_imported_vs_exported` used
  `channel.preamp_db or global_preamp_db` (channel-scoped preamp masked
  the global one — effective gain is their sum), compared delay only
  from the channel bucket (unscoped `Delay:` reported
  `missing_in_import` on every mapped channel), and compared peq only
  from the channel bucket (unscoped filters never reached a comparison;
  worse, positional `peq[i]` indexing then misaligned the channel's own
  bands against the export). The compare now composes
  global + ALL-bucket + channel-scoped for gain/delay, and
  ALL-bucket-peq then channel-peq for the band list.

- **F14-5 `enabled` never compared; OFF band invisible (MEDIUM).**
  `Filter n: OFF` (bypassed) imported with `enabled=False` but no
  comparison item referenced it — identical parameter numbers produced
  `exact_match` against an active export band. A `peq[i].enabled` item
  is now emitted (`exact_match` when ON, `value_differs` when OFF).

- **F14-6 `BW Oct` band reported `missing_in_import` for Q (LOW).**
  A `PEQ`-style band carries `bandwidth_oct` instead of `q`; the
  comparator claimed the parameter was missing. It now emits the
  previously-unused `unsupported_external` state with a detail noting
  the BW-Oct representation. `PEQ` was also missing from
  `_FILTER_TYPE_MAP` — the standard peaking-EQ mnemonic landed opaque
  instead of importing; added.

### CamillaDSP importer (`cad_camilladsp.py`)

- **F14-7 Pipeline channel semantics missed entirely (HIGH).** Steps
  were read with `step.get('channel')` (singular). The documented
  `channels:` list was invisible — every list-scoped step became the
  opaque "lacks a channel index" instead of applying its correction to
  each listed channel. Omitted/`null` `channels` (documented:
  all channels at that pipeline point) went opaque instead of
  applying everywhere; `bypassed: true` was ignored entirely — a
  disabled correction imported as live. Now: `channels:` list applied
  per index (filters imported once, assigned to each channel);
  omitted/null applies to all channels (via `devices.playback.channels`);
  `channels: []` and `bypassed` steps are opaque instead of silently
  applied/dropped.

- **F14-8 `Gain` `scale: linear` read as dB (HIGH — wrong unit
  domain).** `float(params['gain'])` returned the raw number as dB:
  `gain: 0.5, scale: linear` imported as +0.5 dB instead of −6.02 dB —
  the exact dB-vs-linear boundary bug this dimension hunts. Linear
  gains now convert via `20*log10|g|`; `gain: 0` (silence) is opaque
  (unrepresentable in dB); a negative linear factor or `inverted: true`
  emits a polarity diagnostic; `mute: true` is opaque (a muted channel
  is not a gain setting); unknown scales are opaque.

- **F14-9 `unit: s` accepted as seconds (LOW).** CamillaDSP Delay
  units are `ms`/`us`/`mm`/`samples` only — `s` is not a unit.
  `_DELAY_UNITS` accepted it, silently importing a value the device
  would reject. Removed; such delays now record opaque.

- **F14-10 Fabricated `enabled` on biquads (LOW).** Biquads have no
  `enabled` parameter in CamillaDSP (pipeline `bypassed` controls
  application). The importer recorded `enabled: bool(params.get(
  'enabled', True))` — a config carrying a meaningless `enabled: false`
  produced a band claiming to be bypassed that the device would
  actually apply. Bands now record `enabled: True` and the presence of
  the parameter raises a diagnostic.

## Verified clean (no fix needed)

- **No double-apply in measurement comparisons** — `level_offset_db`
  is computed fresh each comparison; nothing stores a "calibrated"
  sample that gets re-scaled at read.
- **`VolumeVelocityTableConverter.calibration_scale`** — linear scale,
  applied to both real and imag of the m³/s value at the solver
  boundary; correct domain and validation (`>0`).
- **UI 'calibrated' indicators** — instrument onboarding/readiness
  and auralization authority all check a real record or a real
  declaration; no string-level phantoms found.
- **CamillaDSP accumulation** — scoped preamp/delay already summed
  correctly (the pattern the E-APO importer was missing).

## Deferred findings

- **D14-1 Per-`Device:` bucketing (E-APO).** Commands after a second
  `Device:` line still fold into the same channel buckets; a
  multi-device config attributions filters to the union of channels
  rather than per-device groups. Requires a device-keyed bucket model —
  not a small diff. Filters do land on *some* channel (loud, not
  silent).
- **D14-2 Shelf slope parameters (both importers).** E-APO `LSC x dB` /
  `HSC x dB` slope and CamillaDSP `slope` (dB/oct) have no field on
  `ImportedFilterBand`; the band's Q slot stays empty. The compare now
  reports Q-vs-BW as `unsupported_external` where a representation
  exists; raw text preserves the slope. A real `slope` field is a
  schema change — deferred.
- **D14-3 `BenchmarkReceiver.calibration_state` /
  `calibration_profile_id`** — declared and validated, no consumer
  reads them (dead calibration field). Harmless schema residue; flag
  for a future schema-round.
- **D14-4 `IrCalibrationState` declaration-only.** `calibrated` is a
  sealed declaration, not a pointer to a calibration record —
  `cad_auralization_service` grants `absolute_amplitude_authority` on
  the word alone. Honest by design (the producer asserts it), but
  phantom-able in principle — the record model doesn't bind an actual
  calibration artifact.
- **D14-5 E-APO `Device:`/`Stage:` context mixing** — recorded opaque
  as designed; not modeled downstream (documented behavior).

## Provenance note

Every comparison carries `artifact_sha256` + `export_settings_sha256`
(content hashes), and every artifact carries `source_sha256` +
`importer_id`/`importer_version` — the chain from calibration file
bytes to compared number is hash-pinned end to end.
