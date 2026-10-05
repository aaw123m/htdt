# REV55-REGCAL — Prediction↔Measurement registration authority (issue #564)

Scope: the missing "registration" layer between solver predictions and
measurements — a persisted, content-hashed `PredictionMeasurementRegistration`
authority, a fail-closed comparability gate, a per-observable residual engine,
partition bookkeeping (calibration / holdout / repeatability), staleness
evaluation, and minimal wiring into the measurement comparison workspace.
Sibling issues #566/#568/#569/#570/#571 belong to other tracks and were not
touched; the R180 model-calibration authority (`cad_model_calibration*`) is
reused, not rebuilt.

## Implemented

### `cad_prediction_measurement_registration.py` (new, ~1900 lines)

- `PredictionMeasurementRegistration` — frozen, extra-forbidden sealed record
  (`ConfigDict(frozen=True, extra='forbid')`) covering every §1 field of the
  issue: prediction identity (kind: solver `CadPredictionResult` or imported
  predicted-evidence dataset, id+sha256, scene-revision id+hash, solver label,
  provider_id, provider evidence state, fidelity state), measurement identity
  (measurement/dataset/IR-dataset/acquisition-context/timing-reference/
  level-reference id+sha pairs with cross-consistency validation), source and
  receiver `RegistrationEndpoint`s (predicted vs measured position and
  orientation, `measured_position_declared` raw pre-transform audit field,
  position uncertainty, verified `position_delta_m`), `SpatialRegistration`
  (method `exact_scene_xyz|named_position_binding|local_frame_transform|
  manual_correction|unknown`, frame kinds, optional `LocalFrameBinding`
  origin+quaternion transform, bounded `ManualPositionCorrection`, named
  position binding, provenance `surveyed|tracked|captured_annotated|imported|
  manual|unknown`, position tolerance), `TimingRegistration` (the issue's
  method vocabulary `exact_reference / acoustic_timing_reference /
  known_hardware_latency / estimated_from_direct_arrival /
  estimated_by_correlation / unknown`, applied offset, reference
  channel/event, propagation delay, hardware latency, IR time-zero offset,
  uncertainty, declared reference semantics, t0 semantics, absolute phase
  validity; validators force `estimated_*` methods to carry offset +
  uncertainty and `known_hardware_latency` to carry the latency), 
  `LevelRegistration` (calibrated/uncalibrated/relative-only/unknown plus
  reference-authority binding), `EnvironmentRegistration` (declared
  sound speeds with verified relative difference), and an audit list of
  `ProcessingOperation`s (smoothing, windowing/gating, resample,
  normalization) imported from the authorities' processing JSON.
- `semantic_sha256` over the JSON-mode dump minus id/hash; id
  `pm-registration:<64hex>`; the comparability verdict is *sealed inside* the
  record and re-verified on `build`, on `save`, and on every
  `model_validate` — a tampered row fails closed on read.
- `evaluate_comparability` — deterministic verdict:
  - `incomparable`: `geometry_revision_mismatch`,
    `receiver_position_unknown`, `routing_topology_mismatch`,
    `unsupported_frequency_domain`;
  - `insufficient_evidence`: `frequency_domain_evidence_missing`,
    `magnitude_domain_unavailable`;
  - `comparable_with_limitations`: honest reason codes such as
    `timing_reference_unknown`, `timing_estimated_not_exact`,
    `timing_uncertainty_undeclared`, `absolute_phase_invalidated_by_timing`,
    `relative_level_only`, `microphone_calibration_unavailable`,
    `environment_sound_speed_mismatch`, `spatial_provenance_unknown`,
    `restricted_frequency_range`, etc. Limitations downgrade capability
    flags (`magnitude_supported`, `absolute_level_supported`,
    `phase_supported`, `arrival_time_supported`) and the comparable band;
    UNKNOWNs are never substituted with nominal values.
- `evaluate_registration_freshness` — live-evaluated against the current
  scene-revision head (`current` vs `stale_geometry_revision`); staleness is
  not sealed because the head evolves.
- `assert_partition_disjoint` — refuses the same measurement in both
  `calibration` and `holdout` partitions (issue §8 holdout separation).
- `ResidualComputationSpec` — sealed, replayable computation spec: residual
  frequency bands (default ISO 266 preferred octave centers, 31.5 Hz–16 kHz),
  onset-threshold parameters, reflection/modal match tolerances, correlation
  max lag, and SHA pins for both responses; `spec_sha256` content hash.
- `PredictionMeasurementResidualReport` — sealed report
  (`pm-residual:<sha>`) bound to registration id+sha and the spec; a
  validator refuses to build a report from `incomparable` /
  `insufficient_evidence` verdicts, so no polished residual can be produced
  over an invalid comparison (issue requirement).
- Residual engine `compute_residual_report` — per-observable, never one
  opaque score:
  - `magnitude_db`: per-band `compare_frequency_responses` stats on the
    level-offset-shifted measured response (offset alignment applied by the
    level reference state).
  - `phase_deg`: per-band wrapped mean/absolute phase differences; only when
    the verdict marks phase supported.
  - `direct_arrival_time`: measured IR onset (`detect_ir_onset`, threshold-
    relative `DEFAULT_ONSET_FRACTION=0.05` after noise estimation at 8σ of
    the first 10% window) vs predicted straight-line / direct-length delay;
    the error is also expressed in metres via the declared sound speed.
  - `early_reflection_time`: greedy descending local-maxima peak detection,
    matched against `CadPredictedReflection` delays within
    `DEFAULT_REFLECTION_MATCH_TOLERANCE_S=2 ms`.
  - `modal_peak_frequency`: matched within `DEFAULT_MODAL_MATCH_TOLERANCE_HZ=
    5 Hz` against predicted room modes.
  - `decay_time`: honestly `unsupported` — the prediction authority has no
    decay observable (`prediction_has_no_decay_observable`).
  - `estimate_delay_by_correlation` (Knapp–Carter-style GCC): full
    `np.correlate`, bounded to ±`DEFAULT_CORRELATION_MAX_LAG_S=10 ms`,
    parabolic sub-sample refinement; uncertainty floored at half a sample.
- `aggregate_magnitude_statistics` — per-band count/mean/std across reports
  (keeps the analysis legible without inventing a single score).

### `cad_prediction_measurement_registration_repository.py` (new)

`CadPredictionMeasurementRegistrationRepository` over `connect_sqlite` +
`require_native_tables`: `_insert_once` idempotent/conflict-error writes,
row-vs-payload equality re-checks on read, `save` re-verifies the sealed
verdict and the existence of the bound measurement, `evaluate_freshness`,
`save_report`/`latest_report`/`list_*` accessors.

### `cad_prediction_measurement_service.py` (new)

`PredictionMeasurementService` — the derivation layer over the existing
authorities (measurement, quality/acquisition-context+timing+level
references, prediction, scene). It resolves the measurement's dataset / IR
dataset / acquisition context, maps the persisted `CadMeasurementTimingRef-
erence` semantics into the issue's method vocabulary (only verified
acoustic/loopback/shared-clock/external-sync references promote to
`acoustic_timing_reference`/`exact_reference`; imported/manual/unknown stay
`unknown` — estimated alignments are never promoted), maps level-reference
kinds into absolute/relative states, snapshots environment state
(temperature-derived `c = 331.4 + 0.6·T`), and assembles the audit list of
processing operations. `register_pair` persists; `check_partition_disjoint`
enforces holdout separation; `compute_and_persist_residual_report` refuses
JA-side on missing, stale, or non-comparable registrations — it never
produces an invalid polished residual.

### Schema + integrity wiring

- `cad_prediction_measurement_registrations` and
  `cad_prediction_measurement_residual_reports` tables in
  `NATIVE_BASELINE_DDL`, `NATIVE_SCHEMA_TABLES` (alphabetical),
  `NATIVE_SCHEMA_VERSION` 19→20 with `_migrate_19_to_20` + migration-ledger
  test entry.
- `_UNBOUND_PAYLOAD_TABLES` entries in `native_row_integrity.py`;
  `_ReplayProbe`s + repository factory (`'prediction_measurement_registration'`)
  in `native_authority_audit.py`.

### UI wiring (`measurement_page_workspace.py`, minimal)

Comparison page gains a "予測↔実測 登録" card: persisted-registrations table
(比較可否・鮮度・位置差・時間方式・パーティション), a pair-state line
(未登録 / 登録済み + verdict + band + limitation count), 「登録レコードを
作成」(enabled for measured↔predicted selections), and 「残差を計算」 with a
per-observable JA summary. `_run_comparison` fails closed for
measured↔predicted pairs: no registration → refuses with instructions;
`incomparable`/`insufficient_evidence`/stale → refuses with the reason.
Measured↔measured and derived pairs are unaffected.

### Calibration / holdout integration

The R180 bounded-calibration authority is untouched and reusable:
registrations carry `partition` (`calibration|holdout|repeatability|
unassigned`) + `campaign_id`, `assert_partition_disjoint` fails closed when
a measurement is claimed by both sides of a split, and every residual report
persists its partition — holdout residuals are computed and reported
separately, never silently merged into fit evidence. Calibration parameters,
bounds, and the derived-state discipline remain governed by the existing
`CadModelCalibrationSpec/Result/Freeze` authorities; this layer feeds them
comparability-honest residuals rather than re-implementing the fitter.

## Literature basis

- Bistafa & Bradley (2000), JASA 108(4):1721 — typical room-acoustics
  predicted-vs-measured discrepancies sit near 1–2 jnd; motivates honest
  per-band residuals and the 5 Hz modal / 2 ms reflection match tolerances
  rather than a global score.
- ISO 3382-1 Annex A.6 — environmental control: temperature ±1 °C, RH ±3 %;
  drives `sound_speed_from_temperature_c` (`c = 331.4 + 0.6·T`) and
  `SOUND_SPEED_RELATIVE_MISMATCH_TOLERANCE = 0.005` (~3 °C equivalent) which
  both downgrades the verdict and disables arrival-time support.
- Defrance & Polack (2008), JASA — absolute RIR time-zero/max-pick onsets
  are unreliable; motivates threshold-relative onset detection with an
  explicit onset fraction + noise estimate (declared in the spec), and the
  `estimated_*` honesty rules (offset + uncertainty mandatory).
- Knapp & Carter (1976) — GCC delay estimation + parabolic interpolation
  for sub-sample precision; implemented in `estimate_delay_by_correlation`
  with a 10 ms default lag bound and a half-sample uncertainty floor.
- ISO 266 preferred frequencies — default residual band edges.

## Tests

`test_rev55_regcal.py` (17 tests): sealed record round-trip through the
repository; UNKNOWN→limitations (never upgrades); revision mismatch →
`incomparable`; report binds registration+spec; report refused on
incomparable and on stale geometry; estimated timing requires offset +
uncertainty and keeps phase unsupported; manual correction requires an
explicit bound and applies the offset; partition-disjoint guard; persisted
tamper fails closed on read; duplicate save idempotent/conflict; onset and
correlation detectors; local-frame transform; phase only with exact timing;
sound-speed law; env-mismatch honesty. UI gate covered in
`test_measurement_workspace_composition.py` (comparison refuses before
registration, succeeds after).

## Remainder / out of scope

- Solver-side registrations for geometry-level predictions beyond the
  imported predicted-evidence dataset path are supported (kind
  `solver_prediction_result`) but only exercised against `CadPredictionResult`
  reflection/mode data in tests; no live solver driver wires `register_pair`
  yet — that belongs with the prediction-runner track.
- Automatic spatial registration (e.g. AR-capture pose solving into scene
  coordinates) is represented by the `local_frame_transform` /
  `captured_annotated` vocabulary but no estimator is implemented; measured
  positions arrive via parameters.
- Phase residuals require a *shared, verified* timing reference plus
  `phase_validity='valid'`; without it they stay `unsupported` by design.
- The old single-metric `compare_datasets` path still exists for
  measured↔measured pairs; a wider migration of every comparison surface to
  the registration gate is follow-up.
- Sibling gates (#566/#568/#569/#570/#571) are other tracks' responsibility.
