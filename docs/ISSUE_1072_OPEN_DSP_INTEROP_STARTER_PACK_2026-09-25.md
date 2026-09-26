# Issue #1072 — Open DSP interoperability starter pack

Status: **software slice landed** — fixture-verified; real CamillaDSP /
Equalizer APO processes are the later Windows gate. Code:
`backend/src/htdt/cad_camilladsp.py` + extensions in
`cad_external_calibration.py`; tests: `test_cad_camilladsp.py`,
`test_cad_external_calibration.py`.

## Equalizer APO text configs (extends #808 importer)

- `Convolution:` lines are now tracked file dependencies
  (`ImportedCalibrationArtifact.file_dependencies`, reusing
  `IncludeDependency` shape) plus an `external_file_reference` opaque
  section — resolved via `include_resolver` with sha256 when the caller
  supplies the file; unresolved stays diagnostic, never dropped.
- Conditional blocks (`If:`/`ElseIf:`/`Else:`/`EndIf:`) block
  interpretation: every enclosed line becomes a `conditional_block`
  opaque section with a diagnostic — HTDT does not evaluate expressions
  and does not silently import commands whose guard may be false
  (E-APO's own silent-ignore behavior is explicitly rejected).
- Existing Device/Channel scope, Preamp, Delay (ms/s only — samples stay
  opaque pending a sample rate), Filter, Include-with-cycle-detection
  semantics are unchanged.

## CamillaDSP config import

`build_camilladsp_artifact` accepts YAML (new `PyYAML` dep, lazy import)
or JSON and produces the shared `ImportedCalibrationArtifact`:

- playback channels bucket as `playback:N` labels driven by pipeline
  `channel:` indices;
- Biquad (Peaking/shelves/passes/notch/bandpass/allpass + first-order)
  → exact `ImportedFilterBand`s; Gain filters sum into `preamp_db`;
  Delay converts ms/µs/s → seconds (samples/mm stay opaque);
- Conv/Fir `filename` params → `file_dependencies` (FIR asset closure —
  missing file = unresolved dependency, not a zeroed filter);
- mixers, processors and unrecognized filter types stay opaque sections
  with raw JSON — routing is never flattened into a per-channel PEQ list.

## CamillaDSP live adapter

`CamillaDSPAdapter` (device_kind `dsp`) over the WebSocket command seam
(`CamillaDSPTransport` — injected, so no websocket dependency in the
backend):

```text
probe   -> GetVersion / GetConfigJson / GetState
observe -> GetState + canonicalized GetConfigJson
plan    -> config_json (canonical JSON) -> SetConfigJson; reload -> Reload
apply   -> ValidateConfigJson FIRST, then SetConfigJson; operator_confirmed gate
read-back -> GetConfigJson again; verify_action_outcome compares canonical bytes
```

A `SetConfigJson` `Ok` is ACK only — `verified` requires the read-back
canonical config to equal the requested bytes exactly. Validation
failures reject the ACK (`accepted=False` + device error detail), so a
rejected config never reads as applied.

## Verified locally / not yet verified

Verified: YAML+JSON import, channel/filter/dependency normalization,
opaque retention, conditional blocking, probe→plan→apply→read-back
`verified`, validation rejection, reload. Not verified: a live
CamillaDSP process, a real Equalizer APO install, or UI surfaces for the
three-way compare (existing `compare_imported_vs_exported` covers
import-vs-export; live-config comparison reuses the same field-diff
primitives).
