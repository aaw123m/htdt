# Round 21 — export completeness & fidelity

Scope: does every export path carry what the user *sees and means* — not
just a valid file? Round 13 verified byte-level truth (atomicity,
digests, BOM); this round audited semantic fidelity against the exporting
view: field parity, value precision, filter scope, interpretive metadata,
selection vs all, encoding, and re-import losslessness. Each gap was
verified by constructing data, running the real export path, and diffing
the artifact field-by-field against the model. Branch
`devin/rev21-export`. Python 3.12.10, pytest,
`QT_QPA_PLATFORM=offscreen`. Verification harness:
`verify_rev21.py` / `verify_rev21b.py` at the repo root.

## Method

1. Enumerated every user-facing export path by auditing
   `getSaveFileName`/`get_existing_directory` call sites and every
   `render_*`/`write_*_package` writer in `backend/src/htdt`.
2. For each surface, enumerated the columns/curves the exporting view
   shows (workspace overlays, review tables, preview text, sibling
   package members) and diffed them against the rendered artifact.
3. Constructed real persisted data (measurement datasets + saved
   comparisons; a SUPPORTED calibration plan with crossover + PEQ +
   routing; a calibration-bound installation handoff; a run prediction
   matrix) and exported through the production renderers.
4. Parsed the artifacts back (csv.reader, json.loads) and compared
   field-by-field against the authority models.

## Export surface → verified table

| Export | Writer | Checked | Result |
|---|---|---|---|
| Analysis bundle (CSV/JSON/HTML) | `workflow_application._export_analysis_bundle` → `render_analysis_*` | Series coverage, metadata, identity, axis labels | **GAPS → FIXED** (F1, F4) |
| Saved-comparison CSV | `measurement_page_workspace._export_saved_comparison` | A/B/difference curves, verdict metadata, utf-8-sig | OK |
| Calibration settings JSON+CSV | `cad_calibration_workflow.export_settings` → `render_generic_biquad_*` | Field parity vs `CalibrationChannelReview`, provenance, sample rate | **GAPS → FIXED** (F2) |
| Installation handoff package | `installation_handoff.write_handoff_package` | settings.csv vs HTML report + coordinates CSV, manifest digests, BOM | **GAPS → FIXED** (F3) |
| Prediction matrix cell transfer | `prediction_matrix_service.cell_transfers` | (frequency, magnitude, phase) contract | **BROKEN → FIXED** (F5) |
| Diagnostics zip | `support_diagnostics.DiagnosticPackageBuilder` | include-project filter honored, redaction, per-member status | OK |
| Project bundle `.htdtproj` | `export_project_bundle` | dependency closure, managed-asset digests, re-import | OK (verified round 13) |
| Equipment catalog JSON | `export_equipment_catalog_snapshot` | deterministic snapshot, sha256 | OK |
| Installation entities/coords CSV | `report.render_installation_csv` | crossover_json/peq_json rows present at schema ≥3 | OK |
| Comparison report JSON/HTML | `report.build_report_payload` | full payload embedded, JP strings | OK |
| Standards profile JSON | `export_profile_json`/`import_profile_json` | model_dump round-trip | OK |

## Findings (fixed)

1. **Analysis bundle dropped comparison side curves (HIGH)** —
   `_export_analysis_bundle` exported only the A−B difference series per
   comparison. The workspace renders three curves (side A levels, side B
   levels, difference); the exported bundle could not reproduce the view,
   while the single-comparison export (`_export_saved_comparison`)
   shipped all three. Verified: two-dataset + saved-comparison fixture →
   bundle series ids contained only `comparison:<id>` — `:a`/`:b` absent,
   and `semantics_json` metadata was also dropped. Fixed via a shared
   `comparison_export_parts` helper now used by both exporters (a+b+diff
   series + verdict metadata + semantics payload).

2. **Calibration settings CSV lost provenance, crossovers, routing and
   the sample rate (HIGH)** — `render_generic_biquad_csv` wrote a flat
   17-column coefficient table. `CadCalibrationExportSnapshot` carries
   `export_id`, `calibration_plan_id`, both semantic hashes,
   `sample_rate_hz` and `quantization_notes`; `CadExportedChannelSettings`
   carries `source_entity_id`, `crossovers`, `routing` — the review
   (`CalibrationChannelReview`) shows all of them. The CSV dropped every
   one. Without `sample_rate_hz` the b0/b1/b2/a1/a2 coefficients cannot
   be interpreted at all (they are computed for a specific Fs). Fixed:
   a `section,key,value` metadata header (same convention as the
   analysis CSV) carries export/plan/adapter/format/sample-rate/
   quantization identity, and per-channel rows now include
   `source_entity_id`, `crossover_json`, `routing_json` (canonical JSON,
   matching the installation CSV convention). The companion `.json`
   remains the full-fidelity machine format; the CSV is the
   human/spreadsheet view and now shows what the review shows.

3. **Handoff `settings.csv` thinner than its own package (HIGH)** —
   within one handoff package, `installation_report.html` shows per
   channel Source/Channel/Output/Gain/Delay/Polarity/**Crossover**/
   **Ordered PEQ** plus plan + ExportSnapshot identity and sample rate,
   and `installation_coordinates.csv` already carries `crossover_json`/
   `peq_json` — while `settings.csv` dropped `role_id`,
   `source_entity_id`, `sample_rate_hz`, crossovers, PEQ and routing.
   Fixed: settings.csv now carries `role_id`, `source_entity_id`,
   `sample_rate_hz`, `crossover_json`, `peq_json`, `routing_json`
   alongside the existing authority pins.

4. **Analysis CSV omitted document identity and axis labels (MEDIUM)** —
   the JSON payload carries `document_id`, `schema_version`,
   `authority_version` and per-series `x_label`/`y_label`; the CSV had
   none of them, so a detached CSV could not say which document/spec it
   belonged to or what its axes meant. Fixed: three extra `export` header
   rows + `x_label`/`y_label` columns in the series table.

5. **`cell_transfers` never returned phase (MEDIUM)** —
   `PredictionMatrixService.cell_transfers` declared
   `(pressure_reference_pa, frequency, magnitude-or-None)` with docstring
   "(frequency, magnitude, phase)" but had a dead conditional — both
   branches returned `magnitude_pa`, and `phase_deg` was never emitted;
   its `except KeyError` was also dead (`transfer()` raises `ValueError`).
   Fixed: returns
   `(pressure_reference_pa, frequency_hz, magnitude_pa, phase_deg | None)`,
   and unknown coordinates return `None` as declared. Verified with a
   real executed matrix: phase `(0, -10, -20)` deg now reaches the caller.

## Verified not issues

- **Encoding**: every CSV writer that reaches the filesystem is emitted
  via `write_export_files(bom_suffixes=('.csv',))` or
  `write_text_atomic(..., encoding='utf-8-sig')` — UTF-8 BOM for
  Excel/Japanese locale; JSON/HTML are `utf-8` with `charset` declared.
- **Filter scope**: the diagnostics package honors
  `diagnostics.include_project_ids` (opt-in), records per-member
  `complete/truncated/skipped` status and redacts credential keys,
  paths and `last_project_ref` unless opted in.
- **Formula injection**: every CSV cell flows through `csv_safe_row`
  (`'=`/`'+`/`'-`/`'@` prefix neutralization) — verified on the new
  columns too.
- **Value fidelity**: exports write `.12g` precision (not the 2dp
  display rounding); units are labeled in column names/headers and axis
  labels now travel in the analysis CSV.
- **Selection vs all**: comparison export covers the shown saved
  comparison; analysis bundle enumerates persisted measurements +
  comparisons for the current document — both scopes explicit in the
  UI flow (pick one vs. bundle all).
- **Re-import**: `read_generic_biquad_json` round-trips the calibration
  JSON exactly; `import_profile_json` validates + re-saves standards
  profiles; project bundle import replays member digests (round 13).

## Preexisting suite repairs (found while running the full suite)

Three reds existed on clean `origin/main` (verified in a pristine
worktree) — all stale test fakes, repaired here:

- `_BusyFakeController` in `test_round18_cancel_paths.py` lacked the
  `operation_cancelled` signal + `request_cancel` added by REV19/D2 —
  `build_data_management_component` raised `AttributeError` at connect.
- `test_review_round3.py`'s `blocked_job(_emit)` still used the
  one-argument job signature; `_OperationWorker.run` calls
  `job(emit, cancel_event, commit_cb)` since REV19, so the job TypeErrored
  inside the worker and `started.wait(5)` hung to timeout. Fake updated
  to the three-argument contract.
- `test_rev18_acoustic_truth.py` imports `scipy.signal` unconditionally
  but `scipy` was never declared — collection `ImportError` on a fresh
  env. Pinned `scipy==1.18.1` in `dev` extras.

## Deferred

- `project_performance` JSONL perf journal — append-mode; torn tail is
  normal journal semantics (carried over from round 13).
- `main.py` HTTP `report.*` endpoints stream in-memory renders — no
  partial-file risk (carried over from round 13).

## Tests

`backend/tests/test_round21_export_fidelity.py` (5 tests):
analysis CSV identity + axis labels; comparison export ships all three
workspace curves + semantics; calibration CSV provenance + crossover/
routing/source entity; handoff settings.csv full channel fields; matrix
cell transfer returns frequency/magnitude/phase + None for unknown cell.
