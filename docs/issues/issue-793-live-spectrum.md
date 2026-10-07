# Issue #793 — single-channel live-observation authority

`cad_live_spectrum.py` + `cad_live_spectrum_repository.py`
(native schema v80).

## Scope implemented

A sealed authority for the single-channel live observables that field
tuning depends on while HVAC, projector, DSP, routing, gain or room
state changes — the complement to #663's dual-channel transfer-function
authority.

### Records (sealed, digest-derived ids)

| Type | Table | id prefix |
|---|---|---|
| `RealtimeMeasurementSession` | `cad_realtime_measurement_sessions` | `rms-` |
| `LiveSpectrumObservation` | `cad_live_spectrum_observations` | `lso-` |
| `SPLTimeHistory` | `cad_spl_time_histories` | `sth-` |
| `CapturedLiveTrace` | `cad_captured_live_traces` | `clt-` |
| `LiveEventAnnotation` | `cad_live_event_annotations` | `lea-` |

`RealtimeMeasurementSession` pins the exact monitored input —
`input_channel`, `instrument_ref`, `calibration_ref` (#611),
`interface_path_ref` (#699), `linearity_ref` (#695), `timebase_ref`
(#609), `orientation_ref` (#732), `receiver_location_ref`, gain/range,
sample rate and `system_state_ref` (#573). `channel_semantics` is a
`Literal['single_channel']`: dual-channel transfer-function claims are
structurally unrepresentable here (#11).

### Modes (#1)

`LiveMeasurementMode` covers `realtime_spectrum`, `fractional_octave_rta`,
`spectrograph`, `spl_instantaneous`, `spl_time_weighted`,
`leq_time_averaged`, `peak_max_min_hold`, `time_history` and
`other_profiled_live_observable`. Each mode carries its own binding:

* spectrum modes require `SpectrumEstimatorBinding` (#3 — #749 stays
  canonical for the math; the binding records estimator_ref, FFT size,
  `WindowKind`, record length, overlap, averaging, detector, scaling,
  resolution, smoothing, peak-hold, update rate, estimator version);
* `fractional_octave_rta` requires `FractionalOctaveBinding` (#4 —
  `iec_61260_class_*_eligible` is only expressible with a pinned
  `IEC 61260-1@<edition>` standard reference AND bound class evidence);
* `spectrograph` requires `SpectrographBinding` (#7 — method, window,
  time step, frequency grid, dynamic range, weighting, normalization;
  `display_palette` rides along as visualization metadata only);
* SPL/Leq/peak modes require `SplQuantityBinding` (#5 — frequency
  weighting, time weighting, integration/Leq interval, peak detector
  semantics, max/min reset state, reference pressure). A bare
  `quantity_value_db` is rejected unless the binding declares a
  frequency weighting — `92 dB` alone is never a quantity.

### Time history (#6)

`SPLTimeHistory` retains `TimeHistoryPoint` series with explicit kinds
(`measured`, `aggregate`, `missing`, `session_boundary`,
`calibration_changed`). Missing/boundary points cannot carry levels and
must carry a marker reason; `evaluate_time_history_integrity` flags an
unmarked jump larger than ~1.5 sample intervals as `gap_unmarked` — a
viewer can never draw a continuous line through a dropout.

### Live vs captured (#8, #16)

`CaptureState` keeps `live_view_only` distinct from `captured_trace` /
`captured_session` / `derived_summary`. Promotion requires a canonical
`payload_ref` (`raster_image` can never be the canonical capture) and,
for summaries, the `derived_from_refs` it summarizes.
`CapturedLiveTrace` additionally pins source observations, the raw
stream (policy permitting), a `LiveCaptureSettings` snapshot, event
annotations, the state snapshot/delta, software version and export refs.
`comparison_signature()` digests the ref-free settings snapshot so two
captures on different sessions are comparable iff their measurement
settings are identical.

### Annotations and state correlation (#9, #10)

`LiveEventAnnotation` binds session, text, operator/source and time,
with optional `related_state_ref`/`related_hypothesis_ref` composition
to #573/#719. `claim_kind` admits only `contextual_observation`,
`hypothesis`, `other` — an annotation record cannot assert causality.
`evaluate_state_correlation` returns `contextual_correlation` at best,
never a causal claim.

### Evaluators — all fail closed

* `evaluate_instrument_binding` — `insufficient_evidence` /
  `diagnostic_only` / `uncalibrated` / `timebase_unbound` /
  `instrument_bound` (#2).
* `evaluate_live_observation` — `diagnostic_only` without instrument
  identity; `overload_limited` when the #695 overload flag is set
  (LIVE40); `alert_denied_uncalibrated` for safety-critical alerts on
  uncalibrated inputs (#14); `estimator_unqualified` without declared
  estimator identity/window; `banding_display_only` when RTA banding is
  display/project-defined; `uncalibrated`/`unweighted_quantity` for
  SPL-family claims; unbound spatial averages degrade to
  `diagnostic_only` (#15/LIVE70).
* `banding_eligibility` — self-downgrades `iec_61260_class_*` claims
  lacking the pinned standard revision or class evidence, even on a
  model constructed to skip validators (#4, #18).
* `evaluate_capture_promotion` — `live_view_only` /
  `captured_evidence` / `qualified_capture` / `uncalibrated_claim` /
  `diagnostic_only` (#8, #16; LIVE10).
* `evaluate_time_history_integrity` — `history_complete` /
  `history_with_declared_gaps` / `gap_unmarked` /
  `insufficient_evidence` (#6; LIVE50).
* `evaluate_state_correlation` — `no_correlation` /
  `annotation_only` / `contextual_correlation` (#9, #10; LIVE20).
* `evaluate_noise_evidence_eligibility` — `preview_only` for live or
  uncaptured spectra; `captured_noise_candidate` only when an immutable
  capture exists — and even then #580 owns the profile/duration
  qualification (#12; LIVE30).
* `compare_captured_traces` — `comparable` /
  `settings_differ` / `insufficient_evidence` (#16).
* `spl_quantity_state` — `quantity_semantics_bound` /
  `unweighted_quantity` / `no_quantity_semantics` (#5).

### Repository

`CadLiveSpectrumRepository` follows the post-REV61 `_SealedStore`
convention: `_assert_sealed` re-verifies canonical sha + derived id on
every save; `save` is idempotent for the same sha and
`LiveSpectrumConflictError` for a different sha under an existing id;
`get` re-verifies id, sha and every bound column; `list` verifies
`document_id`. Rows are append-only, `ORDER BY seq ASC`.

### Schema

`NATIVE_SCHEMA_VERSION` 79 → 80: five new tables + indexes in
`NATIVE_BASELINE_DDL`, registered in `NATIVE_SCHEMA_TABLES`,
`_migrate_79_to_80`, `_ROW_BINDINGS` (all bound columns re-verified),
audit `_ReplayProbe`s under the `live_spectrum` repository factory, and
`_LIFECYCLE_TABLE_LABELS` JA labels.

## Boundaries held

* **#663** — `channel_semantics` is pinned to `single_channel`; RTA is
  never conflated with transfer function (LIVE60).
* **#749** — estimator mathematics is referenced via
  `SpectrumEstimatorBinding`/`estimator_ref`, never duplicated.
* **#580** — a momentary RTA is `preview_only`; only an immutable
  capture is a *candidate* for noise qualification (LIVE30).
* **#602** — `exposure_interpretation` is pinned to
  `delegated_to_issue_602`/`not_assessed`; a room SPL log is never
  labelled personal exposure (LIVE80).
* **#575/#581** — `spatial_average` requires
  `combination_semantics_ref` (LIVE70).
* **#695** — `overload_detected` limits/invalins quantitative claims
  (LIVE40).
* **#573/#719** — state-change correlation and hypotheses compose by
  sha-pinned refs and stay contextual (LIVE20).
* **#598** — remote transport is out of scope; capture state is local
  and cannot be silently altered by a remote view.

## Remainder

* No UI wiring — the authority core only; a combined live UI may later
  compose this with #663.
* No #599 standards-registry entries are written by this module —
  IEC 61672-1/61672-3/61260-1 lifecycle rows belong to that issue.
* `instrument capability/profile` for exposure (#13) is declared on the
  session's refs; the profile semantics themselves are #602's.
* Hardware multi-input sessions are separate sealed sessions; this
  module provides no multi-session sync primitives beyond per-input
  calibration/timebase binding.
