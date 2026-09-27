# Round 2 — Test Quality & Coverage

Scope: next tranche of untested money-path modules, the untested Qt-UI
surface, a fresh weak-assertion sweep, and the deferred pydantic `schema`
shadowing decision. Round-1 coverage (blob dedup, schema drift, capture
codecs/units/command adapter/DDL, tautological `assert X or True`) was not
re-reported.

## New coverage (12 test files, ~115 tests)

### Money-path modules (previously untested)

| Module | Test file | What the tests pin |
|---|---|---|
| `cad_measure` | `test_cad_measure.py` (18) | Plan/axis distances, azimuth convention (+Y=0°, +X=+90°, −Y=180°, −X=−90°), elevation ±90°/45°, angle right/straight/zero, coincident-point degenerate returns 0 not NaN, degenerate endpoint `ValueError`, 3D-vs-plan angle, endpoint carry/describe, format strings. |
| `cad_scene_history` | `test_cad_scene_history.py` (12) | Identical→`is_empty`, cross-document `ValueError`, added/removed ids, field-accurate change sets incl. `aim_xyz` and room flag, summary lines anchored to the provided document (removed-entity id fallback `pt-1 を削除`), empty diff line `変更なし（同一内容）`, `summarize_revision` counts/hash. |
| `cad_measured_modal_analysis` | `test_cad_measured_modal_analysis.py` (23) | Residue finite/non-negative, mode frequency & T60 consistency (`6.907755278982137 / decay_time_s`), duplicate-residue-id rejection, SNR bounds; spec seal determinism + tampered hash + duplicate measurement ids + 1:1 positions/datasets + unknown algorithm/ir_capability + inverted band/window rejection; shape alignment/bounds; model seal + orphan-shape + duplicate mode ids; `aggregate_common_poles` greedy first-fit clustering with running-mean centers; reconstruct normalize/zero-peak/explicit positions; the `associate_predicted_mode` ladder (not_attempted → unmatched → ambiguous → associated requires `spatial_pattern_agreement=True`). |
| `cad_measurement_disposition` | `test_cad_measurement_disposition.py` (13) | Disposition seal determinism, corrected↔correction_id bidirectional rule, tamper rejection, correction requires ≥1 field, pose-evidence kind requires ref, `channel_routing_only` entity ban, identity payload includes optional fields only when present, wholesale `source_speaker_ids` replace, eligibility set is exactly `{active, corrected}`. |
| `cad_gizmo` | `test_cad_gizmo.py` (14) | Segment distance (interior/clamped/degenerate/on-segment), `_rotation_about` (Z-90° right-hand, X-180°, normalized axis, off-center pivot invariance, 0°=identity, radial distance preserved), `_event_inside_renderer` bounds + None renderer via stubs, `_device_pixel_ratio` defaults/guard, `_grab_mouse`/`_release_mouse` capability guards. |
| `cad_measurement_verify` | `test_cad_measurement_verify.py` (14) | Timestamp paths (missing/blank/unparsed/source_timezone/Z/host_local), canonical declared verify, tampered samples, unregistered importer fail-closed, `source_kind` mismatch, REW text round-trip + forged `importer_version`, non-canonical JSON rejection, extra-fields and bool-array rejection, authority lookup, seal determinism. |

### Qt-UI modules (offscreen `QPA` platform)

| Module | Test file | What the tests pin |
|---|---|---|
| `room_history_panel` | `test_room_history_panel.py` (5) | ●HEAD/◇detached row markers, label column, summary counts, selection → diff/restore/preview signal wiring, label empty-text + no-selection guards, selection preserved across re-sync. |
| `room_objects_panel` | `test_room_objects_panel.py` (4) | Hidden (`—`) / locked (🔒) / primary (`▶`) markers, summary counts, ordered selection emission + primary-is-last, no recursive selection emission during programmatic sync, batch hide/show/lock/unlock/delete buttons emit current selection. |
| `room_constraints_panel` | `test_room_constraints_panel.py` (6) | Summary distinguishes no-constraints / satisfied / violations / evaluate-error, failed results sort first, entity name and measured distance in row text, selection resolves `result_id`/`constraint_id` and emits the result object, action buttons emit command tokens. |
| `comparison_context_strip` | `test_comparison_context_strip.py` (3) | Label fill, em-dash fallbacks, `semanticState='warning'` only for プレビュー/選択済み. |
| `workflow_help` | `test_workflow_help.py` (3) | `HelpDialog.shortcuts` lists exactly the commands carrying shortcuts (shortcut-less commands absent), empty-registry fallback line, palette dialog fixed content. |

### Wire-compat pin

| File | What it pins |
|---|---|
| `test_schema_wire_key.py` (3) | `schema` Literal fields serialize under their own name via `model_dump(mode='json')`/`model_dump_json` (no alias configured), survive a JSON round-trip, and `Model.model_json_schema()` still resolves correctly despite the attribute shadowing. |

## Production fixes (2)

1. **`cad_measurement_disposition.py` — dead-code seal gap (fixed).**
   `MeasurementCorrectionRecord.identity_payload` had an early `return`
   that made the `correction_kind`/`pose_evidence_ref` block unreachable
   (and left `payload` unbound below it). The optional #863 identity fields
   were therefore never covered by `correction_sha256` — two corrections
   differing only in `correction_kind` would share a seal. Fixed by binding
   the dict to `payload` and appending the optional fields when present;
   rows without them keep byte-identical hashes, so existing seals are
   unaffected.

2. **`room_objects_panel.py` — crash on hidden entities (fixed).**
   `sync_document` called `self.palette().disabled().windowText()`;
   `QPalette` has no `disabled()` method in Qt6/PySide6, so syncing a
   document containing any hidden entity raised `AttributeError` and left
   the object browser unpopulated. Replaced with
   `self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)`.

## Deferred pydantic `schema` shadowing — RESOLVED (keep, don't rename)

Round 1 flagged ~30 models declaring `schema: Literal['htdt.*']`, which
shadows `BaseModel.schema()`. The wire-compat question is settled:

- No alias is configured (`Field.alias`/`serialization_alias` are `None`),
  so `model_dump()`/`model_dump(mode='json')` emit the key as literally
  `"schema"`. Only 9 of ~2355 `model_dump` call sites pass `by_alias=True`
  and none rename `schema`.
- Those payloads are the stored format: SQLite JSON columns, capture
  bundles, review notes, site records all persist `{"schema": "htdt.*"}`
  and validators like `verify_imported_dataset`/`_canonical_payload` demand
  exact byte equality — a rename would silently invalidate every stored
  artifact and every integrity seal.
- The shadowing is harmless in practice: instance attribute `model.schema`
  returns the Literal value, classmethod `Model.model_json_schema()`
  returns the JSON schema — pydantic resolves both correctly (only a
  `UserWarning` at class-definition time).

Decision: **keep the field name**, pinned by `test_schema_wire_key.py` so a
future rename cannot slip in accidentally; any deliberate rename must be a
versioned, migration-aware change.

## Weak/tautological assertion sweep — round 2

Sampled the suite beyond round-1's files with AST analysis plus grep:

- Zero `assert X or True` / `assert True` tautologies remain (round-1's five
  are gone; the only `or True` hits are a `list.append(x) or True` idiom in
  `test_palette_search.py` lambdas, which is legitimate — the lambda's
  return value is unused).
- Assert-less test functions: 13 flagged by the sweep, all false positives
  — `np.testing.assert_allclose`, `raise AssertionError` in try/else,
  `with pytest.raises`, or calls into a validator that raises on failure
  (`validate_bakeoff_decision`). No silent-pass tests found.
- No `len(x) >= 0`-style vacuous comparisons; the `len(...) > 0` hits are
  genuine non-emptiness assertions.
- 465 `is not None` assertions exist suite-wide; spot-checked samples pair
  them with follow-on field assertions rather than standing alone.

Verdict: no new weak-assertion debt found; round-1's cleanup held.

## Scope decisions

- `cad_document` undo/redo (CommandHistory) already carries 19 deep tests —
  deprioritized against truly untested modules.
- Remaining Qt-UI modules not covered this round (~17) are predominantly
  large workspace shells / VTK-coupled widgets where offscreen Qt gives
  little signal for the risk carried; the panels and pure-presentation
  widgets were the high-signal subset and are now covered.

## Test run

`cd backend && TMPDIR=/c/t QT_QPA_PLATFORM=offscreen python -m pytest`
over the 12 new files plus the 9 existing suites touching the modified
modules (`test_issue_863_correction_spatial_truth`,
`test_cad_measurement_effective`, `test_cad_measurement_runner`,
`test_issue_972_measured_modal`, `test_room_editing_authority`,
`test_authority_audit_coverage`, `test_cad_model_validation_service`,
`test_cad_operating_preset`, `test_cad_playback_level`):

**213 tests, all passing.** Branch `devin/rev2-tests`.

## Findings ledger

| Severity | Finding | Disposition |
|---|---|---|
| High | `identity_payload` early-return made `correction_kind`/`pose_evidence_ref` dead to the `correction_sha256` seal | Fixed |
| Medium | `room_objects_panel` crashed on any hidden entity (`QPalette.disabled()` doesn't exist) | Fixed |
| Info | `schema` field shadowing: resolved — field name IS the wire key; rename rejected, contract pinned by tests | Resolved (pinned) |
| Info | ~17 Qt workspace-shell modules remain untested; low marginal value offscreen | Deferred |
