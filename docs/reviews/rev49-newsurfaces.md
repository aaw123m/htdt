# REV49 — deep review of recently merged feature surfaces

Critical re-review of implementations merged for PR #543 (solver authority),
#544 (MIMO / active LF), #547 (presentation sessions PM10–PM40), #548
(auralization review, schema v17), #549/#550 (acceptance wizard), #551
(video commissioning) and commit 7157ca2b (`_is_affine4` transform
validation). Per the REV41/REV46 pattern, the first deep pass over a newly
merged surface produces real defects — this one produced **24 fixed
defects** plus a set of judgment notes that stay documented rather than
changed.

Note: `C:\Users\Administrator\prompts\rev49\common.md` was not reachable
from this session (it lived on the parent session's VM). The review
conventions below are substituted from prior REV41/REV46 reports and
session memory: fail-closed honesty (UNKNOWN/unsupported never fudged),
spec-consistency/usability/accuracy/compute-load priority over rare corner
cases, defect fix + targeted test + scoped pytest green + merge-test
self-merge convention.

## Fixed defects

### 受入検証ウィザード (acceptance_checks.py, acceptance_page.py)

| # | Defect | Fix |
|---|--------|-----|
| A1 | A transient `unavailable` verdict (REW offline, tool missing) permanently `blocked` the step — a temporary environment gap bricked the whole run. | `unavailable`/`deferred` merge to `status='pending'` carrying the detail + JA note; only real `fail` results block. |
| A2 | `check_persistence_probe` wrote a marker and then accepted it on the *next call in the same process* — "restart verified" was provable without a restart. | Markers bind `_PROCESS_BOOT_ID` (or `CheckContext.boot_id`): same-boot marker → deferred; pre-boot-binding markers → re-issued; foreign boot → `verified_after_restart: true`. |
| A3 | A slow auto-check worker could land its result on a *newer* run after the user restarted the wizard — stale result committed to the wrong run. | The completion lambda captures `run_id`; `_on_check_result` early-returns on mismatch and commits onto `repository.latest(run_id)`, skipping already-settled steps. |
| A4 | `input_edit`/`attest_edit` text leaked across step selections — an attestation typed for step N could be saved onto step M. | `_update_step_panel` tracks `_detail_step_id` and clears both edits whenever the shown step changes. |
| A5 | Closing the page mid-check orphaned a running `QThread` → `RuntimeError` on signal delivery after `deleteLater`. | `closeEvent` detaches the worker: signals disconnected, `finished → deleteLater` self-disposal, parent cleared. |
| A6 | `_environment_snapshot` never recorded the code SHA even though the run-header display path reads `env['code_sha']` — dead display field. | Snapshot now probes `git -C <repo_root> rev-parse HEAD` (15 s timeout, silent on failure). |

### 映像調整 (video commissioning, measure import, workspace)

| # | Defect | Fix |
|---|--------|-----|
| V1 | `_session_target` silently substituted the *currently selected* target profile when the session's pinned `target_sha256` couldn't resolve — sessions evaluated against an authority they never bound. | Fail closed: return `get_target_profile_by_hash` or `None`. |
| V2 | `_compatibility_reasons` compared encoding + bit depth only; patch size, APL, pattern generator and signal path differences produced a misleading "comparable" delta. | All four `StimulusDefinition` axes now emit JA incompatibility reasons. |
| V3 | `_metric_row` marked any luminance *increase* `regressed` — a brighter peak white was reported as a regression. | `_metric_row` gains a `better` polarity (`'lower'`/`'higher'`/`'none'`); `peak_white_cd_m2` reports `inconclusive` on change (its goodness is target-relative, which the deviation rows already carry); `black_floor` stays lower-is-better. |
| V4 | `_max_luminance`/`_min_luminance` ranged over **all** non-floor samples — a blue primary (Y=6) was reported as 黒床輝度 and a green primary as ピーク白. | Extrema restricted to achromatic groups (`white_point`+`grayscale` via `_sample_group`); empty → `unknown` row. |
| V5 | `latest_readiness_report` ordered by `created_at_utc DESC, report_id DESC` — a report recorded with an older timestamp (clock skew, manual import) shadowed the actually-latest report. | `ORDER BY rowid DESC` — insertion order is authoritative (this table uses a composite PK, no `seq`). |
| V6 | `save_proposal` never validated that its `diagnosis_id` exists — proposals could reference air while every sibling save pins its session. | Raises `VideoCommissioningIntegrityError` when the referenced diagnosis is not persisted. |
| V7 | `import_video_measurements` ran `build_video_color_measurement_set` and the JSON-path `model_validate` calls **after** its try/except — a parseable file that can't assemble a set (bad `meter_correction`, partial provenance) raised instead of returning `malformed`, violating "never raises for bad input". | Whole materialization block moved inside the guarded region → `malformed` with the same JA reason shape. |
| V8 | A truncated HCFR level row silently fell back to ordinal level inference with no warning (unlike the missing-row case). | Warns `レベル行…の列数が不足しています — 刺激レベルは順序から推定しました`; unparseable level cells warn too. |
| V9 | `_selected_action_kinds` used `proposals[-1]` — the *newest* proposal — while the UI adjusts the *selected* one. | Reads the `proposals_list` current item (`UserRole`), falling back to latest. |
| V10 | `_refresh_session_page` repopulated the surface/target combos and clobbered the user's selections on every refresh. | Preserves `currentData` across repopulation. |
| V11 | `_create_session` rebound whenever `_current_session` existed — switching surfaces silently inherited another surface's session lineage. | Rebind only when the new session targets the same `surface_entity_id`. |
| V13 | `bit_depth` spin values 1–7 silently coerced to `None` (stimulus "unknown") with no user feedback. | Guarded: values 1–7 block creation with a JA notice; 0 keeps "不明"; ≥8 passes through. |

### プレゼン (presentation workspace, review package)

| # | Defect | Fix |
|---|--------|-----|
| P1 | `_export_session` indexed `sessions[index]` positionally — a list mutation between refresh and export could render a different session's package. | `export_session_combo.currentData()` → `get_session`. |
| P2 | Step replay applied **camera only** — `hidden_ids`, `section`, `focus_entity_id` recorded on the viewpoint were dropped, so steps lied about what was presented. | `_show_step` now replays the pinned session document with `set_aux_render_state(section=…)`, `render_document(hidden_ids=…)` and `focus_entity(…)`; `_show_session` stores `_session_document` for replay. |
| P3 | `add_review_note(...)` ran outside the try/except — a malformed note crashed instead of surfacing the failure dialog. | Moved inside the guarded block. |
| P4 | `status_combo` offered `as_built` while a variant pin was selected — a proposal pinned to a variant recorded as built work. | Save-time guard: variant pin + `as_built` → JA warning, no save. |
| P5 | `_build_review` rebuilt the session model with `yaw_step_deg=30` whenever the user checked yaw on a session that never declared it — the package manifest then pinned a `session_sha256` **no persisted session has**. | `build_review_package` takes an explicit `yaw_steps_deg` override (`None` → derive from session, `()` → pinned frames only, tuple → explicit); workspace passes `()` unchecked / `None` derive / `derived_yaw_steps(30)` checked. Public `derived_yaw_steps()` helper exported. |

### Auralization (cad_auralization_review.py)

| # | Defect | Fix |
|---|--------|-----|
| Au1 | `verify_review_package` validated capability/routing payloads *as models* but never cross-checked them against comparison entries — a manifest could pin capabilities/routings it doesn't carry and still verify. | Verifier now builds capability/routing maps and requires, per entry: capability present, `capability_semantic_sha256` match, `artifact_id`/`artifact_semantic_sha256`/`spec_id`/`spec_semantic_sha256`/`routing_sha256` coherence, routing payload embedded; duplicate `capability_id`s rejected. |

### MIMO / アクティブLF (cad_active_lf_design.py)

| # | Defect | Fix |
|---|--------|-----|
| L1 | `latency_budget` `_range_check` returned `PASS` ("plan declares no latency demands") when `plan.total_latency_s` was `None` *and the envelope declared a budget* — a check that never ran certified compatibility. | `_range_check` gains `demand_expected`; latency with a bounded envelope and silent plan → `UNKNOWN` (verdict no longer `compatible`). |
| L2 | `lf_control_evaluation_spec` embedded `[]` as seat ids when callers passed none, while the metrics actually used `seat-N` fallbacks — the persisted evaluation spec did not name the seats it covered. | Seat ids resolved once (`ids or seat-N`) and bound into the spec hash; the per-seat loop reuses the same list. |

### ソルバー権威 (cad_acoustic_geometry_derivation.py)

| # | Defect | Fix |
|---|--------|-----|
| G1 | `build_acoustic_geometry_derivation` checked "authority supplied but compiled geometry has no ref" but not the mirror: a compiled geometry *binding* a portal/boundary-termination authority could be passed `portal_authority=None` → derivation recorded zero declarations for a lineage that has them. | Both refs now require their authority be supplied; asymmetric supply raises `ValueError`. |

### transform 検証 (capture_entity_promotion.py)

| # | Defect | Fix |
|---|--------|-----|
| E1 | `_world_to_scene` returned **one** authority matrix for all annotations — annotations in a *different* coordinate space promoted through a sibling space's transform. | Renamed `_world_to_scene_by_space`; returns a `{space_id: matrix}` map; each annotation promotes through its own `coordinate_space_id`, identity when its space has no recorded authority. |
| E2 | `scale = _uniform_scale(world_to_scene)` dropped the annotation transform's own scale — entity size/body geometry used only the authority scale. | `scale = _uniform_scale(scene_transform)` — the composed transform. |

### `eps = 1e-6` orthogonality verdict (7157ca2b)

**Adequate — kept.** float32-derived column norms deviate ~1e-7 after
normalization and dot products, so 1e-6 carries ~3–10× margin over real
input noise; 1e-4 would admit ~0.006° of shear (silently approximated
away); 1e-9 would reject legitimate float32 authority data. The tighter
constraint is the non-uniform-scale ratio check (`> 1.0 + 1e-6`), which
is the one actually doing "similarity" enforcement.

## Verified clean surfaces

- `cad_presentation_session.py` — hash-pinned model, thorough validators,
  `ordered_viewpoints`, section/annotation records.
- `cad_presentation_repository.py` — `session_document` resolution,
  `session_stale` detection, `_assert_resolves` fail-closed reads.
- `cad_review_package.py` manifest/build coherence — pinned scene hash,
  honest `unavailable` rows, deterministic zip.
- `cad_auralization_review.py` build path — full coherence enforcement at
  build time (artifact/spec/capability/routing/WAV digest).
- `cad_active_lf_control_repository.py` — lifecycle journal and append-only
  transitions.
- `cad_solver_capability_manifest.py` — capability declaration structure.
- `cad_video_commissioning_repository.py` save/load paths beyond the noted
  gaps — `_require_session`, `_row_model` payload↔row integrity, status
  journal.

## Judgment notes (documented, not changed)

- **V12** — `evaluate_photometric_state` needs an
  `ExpectedLuminanceEstimate` + `LuminanceMeasurement`, and nothing in the
  video workspace produces them yet; the photometric axis of
  `diagnose_video_measurement` is therefore unreachable from the UI. That
  is a wiring gap to close when a producer lands, not something to fake.
- **Journey state** counts only session-linked batches — batches imported
  without a `session_id` never appear in the journey strip; intentional
  scoping, flagged for future UX.
- **L3** — `record_transition` does not validate that the latest recorded
  state is still current (a stale read can append a bogus transition);
  append-only journal keeps it auditable, so left as noted risk.
- **L4** — `content_feed` is classified as `cross_term` in DSP counting;
  defensible (a feed is a routed term) but worth revisiting when the
  envelope model gains a dedicated feed class.
- **Au2** — min-length truncation on the package comparisons list is not
  recorded in the manifest; build-time validation already refuses empty.
- **P6** — `_capture_viewpoint` on a stale document captures against the
  live head, not the pinned revision; pinned-session replay (P2 fix) uses
  the resolved document, so the capture path's staleness is bounded to
  authoring, left documented.
- **P7** — bare `except Exception: pass` around combo population can
  swallow real repository errors; acceptable while refresh paths degrade
  to empty lists, flagged.

## Tests

`backend/tests/test_rev49_news_surfaces.py` (17 tests) pins every fix
above; `test_cad_acceptance.py::test_persistence_probe_two_phase` updated
for boot binding. Scoped suite:

```
pytest tests/test_rev49_news_surfaces.py tests/test_cad_acceptance.py \
  tests/test_cad_video_commissioning.py tests/test_cad_video_measure_import.py \
  tests/test_cad_auralization_review.py tests/test_issue_973_active_lf_control.py \
  tests/test_issue_533_active_lf_slices.py tests/test_acoustic_geometry_derivation.py \
  tests/test_capture_entity_promotion.py tests/test_rev47_presentation.py \
  tests/test_rev47_iss2_acceptance.py
→ 163 passed
```
