# Round 9 — output & report surfaces end-to-end

Scope: everything the user exports, prints, shares, or hands off. Prior
rounds were read first (esp. `round8-journey.md`); their findings are
not re-reported. Branch `devin/rev9-report`.

Method: (1) map every write/export path and its tests; (2) render real
artifacts and inspect bytes (SVG markup, CSV rows, ZIP members, JSON);
(3) fix only defects that break honesty, coherence, or durability of the
artifact; (4) implement the one feature candidate whose sketch was
concrete (round-8 §9 per-comparison export).

## Coverage map — surface → tests → content assertions

| Surface | Writer | Round-9 assertions added |
|---|---|---|
| Web comparison report | `report.render_report_html` → `_svg_chart` | joint (f,a,b) filtering, NaN/non-positives dropped whole-row, graceful fallback |
| Analysis bundle | `render_analysis_{csv,json,html}` + `_export_analysis_bundle` | log-x plot + octave ticks + axis label; NaN never reaches a polyline; stem never collides; group all-or-nothing |
| Per-comparison CSV/PNG | `_export_saved_comparison`, `_export_difference_plot_png` (new) | CSV carries a/b/difference series + verdict metadata; buttons track saved-vs-preview state |
| Mission package | `capture_receiver.export_mission_package` | `write_bytes_atomic` (unit-tested via export_io) |
| Standards profile | `standards_profile_editor._export_profile` | `write_text_atomic`, error still routed to `warn_user` |
| Commissioning registry | `CommissioningPlanRepository._store` | `write_text_atomic` (empty registry can no longer be half-written) |
| Diagnostics ZIP | `DiagnosticPackageBuilder.build` | staged temp + promote; explosion mid-build leaves nothing |
| Deliverables catalog | `deliverables_catalog.catalog` | honest `expected_formats`; member-file notes; `commissioning.report` blocked instead of phantom |
| Handoff package | `installation_handoff.write_handoff_package` | unchanged — already staging+digest-verify+rollback |
| Equipment catalog | `equipment.export_capture_catalog` | unchanged — honest single-format JSON |
| Project bundle/backup | `project_transfer`/`backup` | unchanged — already staged |

Surfaces that do **not** exist (recorded so future rounds don't re-hunt):
SOFA/REW are import-only — no export writers. No print support anywhere —
no `QPrinter`, no `@media print` in the HTML reports; the HTML report is
the print path and prints as rendered. No clipboard-copy export path.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | Web report `_svg_chart` filtered `grid_hz`/`a_db`/`b_db` per-array: a mid-series NaN in one array silently re-paired every later x with the wrong y (wrong chart, same length); a mid-series non-positive frequency crashed `log2` → the whole report endpoint 500s | MED correctness | FIXED — joint row filter; falls back to the honest "no aligned points" paragraph under 2 rows |
| 2 | Analysis `_plot_svg` drew frequency series on a linear Hz axis: 20–100 Hz compressed into ~2.5 px of a 720 px plot — the bass decade unreadable, the exact region HTDT analyses | MED usability | FIXED — log2 x-axis gated to Frequency series spanning ≥2×, octave ticks (20,…,50000), axis label `Frequency (log)`; non-frequency axes keep linear corners |
| 3 | `_export_analysis_bundle` wrote `analysis_export.csv/json/_report.html` in place with plain `write_text`: a crash mid-sequence left a partial bundle and a re-export silently clobbered the previous generation | MED robustness | FIXED — `claim_export_stem` + `write_export_files`: fresh `analysis[-N]` stem per generation, all-or-nothing members |
| 4 | Deliverables catalog promised artifacts that cannot be produced: handoff `('csv','md','json')` though the package is csv/html/json; drawing set `('svg','pdf')` though it ships as `dimension_sheets.csv`; BOM/labels claimed standalone exports; `commissioning.report` showed `available` + `('md','html')` though no generator exists; equipment catalog claimed `csv` alongside JSON | MED honesty | FIXED — formats match reality; non-standalone rows name the exact handoff member file; commissioning report is `blocked` with an honest reason |
| 5 | Single-file writers (`export_mission_package`, `_export_profile`, `CommissioningPlanRepository._store`, diagnostics ZIP) used direct writes — a crash left truncated files at the chosen path; the plan registry would then load as an *empty* registry (`_load` tolerates corrupt JSON) and silently drop every recorded plan | LOW robustness | FIXED — all routed through `export_io` atomic writers / `_staged_zip_archive` |
| 6 | Per-comparison export didn't exist (whole-bundle or screenshot only) — round-8 deferred §9 | LOW completeness | IMPLEMENTED — "この比較をCSVで保存…" writes an analysis CSV with the A, B and A−B series plus verdict metadata; "差分プロットをPNGで保存…" snapshots the difference plot at 1280 px; both only enabled on a saved comparison, never a live preview |
| 7 | Calibration biquad export (`filter_design`/`biquad` path) and auralization WAV render are severed legs — test-only reachable, same shape as round-8's quality-report leg | MED completeness | DEFERRED — needs the same evidence/feature build-out as round-8 #1 |
| 8 | Desktop comparison plots remain linear-frequency while the web report is log — a parity note, not a defect (the desktop plot is interactive/zoomable) | — | OBSERVED |
| 9 | `list_comparisons` all-or-nothing replay means one corrupt row excludes every comparison from an export | — | already deferred round-8 #11 — not re-fixed |

## What was verified (bytes, not code-reading)

- `_svg_chart` output on crafted payloads: `<polyline>` point counts
  match surviving joint rows; no `nan`/`inf` substrings; `<p>` fallback
  for unsuitable grids.
- `render_analysis_html` on a 20–20 kHz series: emitted SVG contains
  `Frequency (log)` and the `>20000<` octave tick; a `Time` series stays
  linear.
- `write_export_files`: pre-existing member → `FileExistsError`, zero
  files created; success path returns all members; no `*.tmp` leftovers.
- `claim_export_stem`: any claimed member bumps the stem
  (`analysis` → `analysis-2` → `analysis-3`).
- `DiagnosticPackageBuilder.build` against a `zipfile.ZipFile` that
  raises mid-construction: chosen path absent, staged `.tmp` removed;
  happy path contains `manifest.json`.
- `_export_saved_comparison` end-to-end through the real workspace
  (offscreen Qt): committed two measurements, persisted a comparison,
  exported — CSV contains `comparison:<id>:a`, `:b`, and the difference
  series plus `comparison.<id>.rms_difference_db` metadata.
- Deliverables catalog on the F1 scene and on an empty document: every
  `expected_formats` tuple equals what its writer actually emits;
  member-file notes appear on `drawing_set`/`bom`/`field.labels`;
  `commissioning.report` flips `not_applicable` → `blocked` exactly when
  plans exist.

## Export-vs-UI parity spot check

| Value | UI shows | Export carries |
|---|---|---|
| Comparison metrics | `RMS差/平均差/形状RMS/レベル差` (3 decimals) | `comparison.<id>.rms_difference_db` etc. — raw floats, same record |
| Side labels | `A: … → B: …` | `label_a`/`label_b` metadata + `A: …` series labels |
| Level compatibility | localized label | raw enum + `semantics_json` verbatim |
| Difference curve | `difference_plot` (linear Hz, zoomable) | `comparison:<id>` series on `grid_hz` |
| Handoff tables | overview sections | `installation_coordinates.csv`, `dimension_sheets.csv` inside zip |

Rounding: the UI formats to 3 decimals; exports write full precision.
This is correct — exports are evidence, not the rendered string.

## Deferred (not re-reported elsewhere)

- **Severed output legs** (#7): biquad filter export and WAV
  auralization render have writers but no production caller. Fix = wire
  them into the deliverables catalog only once their upstream evidence
  capture exists (round-8 #1 is the blocker).
- **Print support**: if the HTML reports are meant to print well, add
  `@media print` rules (hide nav, force light background) — cosmetic,
  no correctness impact.

## Tests

New file `backend/tests/test_round9_output_surfaces.py` — 15 tests.
Full backend suite (`pytest -q -n 4 tests`, Windows, `QT_QPA_PLATFORM=offscreen`):
~5,829 tests, **all pass** (exit 0, no failures/errors/skips triage needed).
