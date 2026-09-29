# Round 17 — input validation completeness

Branch: `devin/rev17-form`. Scope: every user-editable input surface in the
native app — inspector spinboxes and free-text fields across the room/theater/
measurement/standards/optimization workspaces, dialogs (material, treatment,
equipment, geometry import, commissioning wizard, pairing), preferences
editors, free-text CSV/pair parsers, and the FastAPI request models behind the
legacy dev API. Method: enumerate every `QLineEdit`/`QPlainTextEdit`/
`QSpinBox`/`QDoubleSpinBox` construction site, trace each `.text()`/`.value()`
into its model boundary, then **exercise the actual parse path** (construct the
widget offscreen or the pydantic model directly) for anything that looked
loose. The seven dimensions audited: (1) missing bounds, (2) parse-vs-model
disagreement, (3) NaN/inf entry paths, (4) pasted garbage, (5)
commit-on-focus-loss semantics, (6) API-side gaps behind UI guards, (7)
required fields the UI allows empty.

Context: after sixteen rounds the input surface is broadly hardened —
`allow_inf_nan=False` on API models, `isfinite` validators on authority
models, `Decimal`-based grid math, `read_file_bounded` on every file import,
and a uniform `ValueError → notice/warn_user` convention. This round's job was
finding what the convention *didn't* cover.

## Fixed in this branch

| # | Severity | Location | Finding | Fix |
|---|----------|----------|---------|-----|
| 1 | medium | `measurement_page_workspace.py` `_collect_acquisition_capture` | **Parsed-but-model-rejected input escaped as an uncaught exception — dead click, no notice.** The capture form validates free text with `try: int()/float()` and returns a Japanese error string for parse failures, but values that *parse* then fail the pydantic contract raised `ValidationError` inside `CadMicrophoneCapture`/`CadPlaybackCapture` construction — uncaught there and at the only call site (`_commit_assignment`). Verified: `0`/`-5` in the mic sample-rate field (`Field(gt=0)`) and `nan`/`inf`/`1e999` in the AVR volume field (`valid_playback` isfinite) all raise `ValueError` out of the slot — the click does nothing and the traceback only reaches stderr. | Field-level checks matching the existing style and the model contract: `mic_rate <= 0` → `「マイクのサンプルレートは正の整数で入力してください」`; `not isfinite(volume_db)` → `「AVRボリュームは有限の数値（dB）で入力してください」`. Both now return through the normal `capture_error` channel → `_set_notice`. |
| 2 | medium | `cad_standards.py` `CriterionRule.valid_rule` | **`equals` rules accepted a non-finite float `expected` — silent universal FAIL plus a digest crash.** `ObservedScalar` admits `float`, and `valid_rule` only ran `_decimal` on *finite* numerics, so `expected=nan`/`inf` passed validation. Verified consequences: `_compare` treats a non-finite expected as an opaque scalar (`observed == nan` → `False` for every observation — the criterion fails every room forever with no signal), and `model_dump(mode='json')` keeps the nan so `canonical_json(allow_nan=False)` raises `"Out of range float values are not JSON compliant"` inside `_criterion_digest` — which runs per-criterion during `evaluate_standards_profile` *and* at profile save, surfacing as a cryptic English `warn_user` far from the authoring mistake. | `valid_rule` now raises `ValueError('equals rule expected value must be finite')` for a non-finite float `expected` at construction — fail-closed at the authoring boundary and the API boundary alike (the validator also guards any profile payload reaching the model). Strings, bools, ints and finite floats unchanged. |

## Verified honest (checked, no change needed)

- **Spinbox inventory**: every `QDoubleSpinBox`/`QSpinBox` in `native_editor`,
  `theater_editor`, `wall_editor`, `room_editor`, `room_workspace`,
  `room_geometry_panel`, `room_video_panel`, `field_explorer_panel`,
  `equipment_library`, `geometry_import_dialog`, `room_acoustics_panel`,
  `intervention_planner_panel`, `joint_optimization_panel`,
  `robustness_authoring_panel`, `seat_priority_panel`,
  `room_constraints_panel`, `constraint_editor`, `commissioning_wizard`,
  `capture_receiver_settings`, `system_expansion_widgets`,
  `room_prediction`, `prediction_workspace`, `optimization_*_controller`,
  `workflow_settings` carries an explicit `setRange` — no unranged spin was
  found. Qt clamps typed text into the range on commit; `KeyboardTracking(False)`
  prevents half-typed commits.
- **Commit-on-focus-loss**: `MetricSpinBox`/`_PendingTextSpinBox`
  (`room_workspace.py:2374`) deliberately preserves pending text across
  hide/show and keeps SI authority in `_exact_m`; cancel semantics on dialogs
  discard drafts (`reject` everywhere, no partial commits observed).
- **Cross-field ordering**: band low/high (`InterventionFinding`), DSP
  min/max/step (`JointDspVariable`), angle axes (`PlacementAngleAxis`), zone
  bounds (`system_expansion_workflow`), search axes (`GridAxis`) are all
  model-side `isfinite` + ordering validated; the UI failures route to a
  notice, not a crash.
- **Free-text numeric parsers**: `parse_footprint_vertices` (non-finite
  rejected by `FootprintVertex.finite` → wrapped `InspectorValidationError`),
  REW/mesh parsers (`SpecificImpedancePoint`/`GeometricAcousticBand` finite +
  ranged validators → `except ValueError → warn_user`), material impedance/
  band CSV (`except (ValueError, IndexError)`), EULUMDAT/IES parsers
  (`PhotometricArtifact._check` finite on every candela/angle) — all
  fail-closed with Japanese notices.
- **Required-vs-optional**: empty-name guards on `MaterialDialog`,
  `TreatmentDefinitionDialog`, environment profiles, project creation,
  equipment `user_label`/`source_name`, pairing `confirm_code` compare —
  all warn before construct.
- **API payloads**: `models.py` request models are `allow_inf_nan=False`
  with `gt`/`le`/`min_length` where the UI enforces them; endpoints map
  `KeyError`→404/`ValueError`→422. No UI-guard-only gap found — the round-2
  addition of the `CriterionRule` fix also closes the one payload path that
  could have carried a non-finite `expected` past the UI.
- **Standards editor** (`standards_profile_editor._apply_criterion`/`_save`):
  `float()` parse failure keeps the raw string as `expected` (legitimate —
  `ObservedScalar` admits `str`), and `warn_user` catches `ValueError` on
  both apply and save. Post-fix, a non-finite float now lands in that same
  localized warning path instead of a digest crash.

## Regression tests

`backend/tests/test_review_round17_input_validation.py` — instantiates the
real `MeasurementPageWorkspace` offscreen and drives
`_collect_acquisition_capture` with `-5`/`0`/`-1`, `nan`/`inf`/`-inf`/`1e999`,
`abc`/`db`, and a valid `48000`/`-12.5` pair; constructs `CriterionRule`
equals-rules with nan/±inf (rejected) and with finite float/int/str expected
(still accepted and compared correctly). 13 tests.

## Deferred

- `CadMicrophoneCapture.sample_rate_hz` is `gt=0` with no upper bound — a
  `999999999999`-style entry is accepted by design (provenance field, never
  drives computation). No bound invented: the authority records what the
  operator claims.
- `RoomSnapshot` dims and several free-text metadata fields are deliberately
  unbounded upward (real venues are large); same rationale.
