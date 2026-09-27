# Round 8 — measurement→comparison→report data journey

Scope: the second half of the golden path — REW import → quality →
comparison → export/report. Whether imported data flows end-to-end, the
integrity of the journey (units/refs through import, lineage visibility,
orphaned rows, reimport behaviour), output correctness (exported content
vs displayed values, empty states), and missing journey features. Prior
rounds 1–7 were read first (esp. `round6-ux`, `round7-workflow`); their
findings are not re-reported. Branch `devin/rev8-journey`.

Two measurement domains exist side by side and were both walked end to
end:

- **Native CAD domain** (`measurement_workflow.py`,
  `cad_measurement_repository.py`, `measurement_page_workspace.py`) —
  the PySide6 desktop app. Authoritative reads re-verify the persisted
  semantic hash + transformation seal + pinned-importer replay + managed
  raw asset on *every* read (`_dataset_and_asset`), and comparisons are
  replay-validated on *both* save and list (`_validate_current_comparison`).
- **Legacy web store** (`database.py` + `main.py` + React frontend) —
  trust-on-read model: stored blobs served back as-is, caller-supplied
  `quality_status`, no replay. Reported as an integrity asymmetry in
  round 4 (db) / round 7 (workflow); not re-fixed here.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | Quality-report leg is severed: `build_measurement_quality_report` / `save_report` / `save_observation` / `save_dataset_level_reference` have zero production callers — the quality page can never show "最新", campaign cells can never leave `quality_pending`, `absolute_level_comparable` is unreachable, calibration plans pinning `measurement_quality_report_*` can never complete | HIGH completeness | DEFERRED — needs an evidence-capture feature, sketch below |
| 2 | Saved-comparison reload drops identity + verdict: persisted `label_a`/`label_b`/`level_compatibility`/`semantics_json` were never rendered, and the FR overlay was never rebound to the saved pair — selecting a history row showed metrics for the saved run over a preview of whatever pair happened to be selected | MED correctness/UX | FIXED (`_bind_saved_pair` + persisted-verdict rendering) |
| 3 | Native analysis export dropped the comparison verdict (web report shows mean/RMS/offset/shape — the bundle exported only the difference curve) and silently skipped any measurement failing re-verification, writing a possibly-empty file claiming completeness | MED completeness | FIXED (`comparison_metadata_entries` + recorded omissions + empty-export guard) |
| 4 | One corrupt dataset/comparison bricked the whole measurement workspace: `measurement_views` let `dataset_for_measurement` raise per row, and `saved_comparisons` (all-or-nothing replay) propagated through `_refresh_comparison_choices` — `refresh()` died with an unhandled `ValueError` | MED robustness | FIXED (per-row `dataset_error` view field + guarded history) |
| 5 | Single-file import had no duplicate signal: `_classify_duplicate` (exact-content and same-acquisition detection) ran only in the batch lane — re-importing an identical file silently produced a second measurement | MED completeness | FIXED (pending carries the classification; import card + commit confirm) |
| 6 | `_mismatch_label` covered only 6 of the 11 advisory codes (`acquisition_context`, `routing_profile`, `level_reference`, `timing_reference`, `radiation_scope` fell back to raw snake_case in the UI) | LOW polish | FIXED (label map extended) |
| 7 | Per-measurement delete with cascade (comparisons/attachments/reports becoming orphans) — doesn't exist in either store | — | BY DESIGN — append-only + dispositions; see below |
| 8 | Re-import/replace of an existing measurement — doesn't exist | — | BY DESIGN — retake lineage supersedes instead; see below |
| 9 | Per-comparison export / PNG-of-plot export — users must export the whole bundle or screenshot | LOW completeness | DEFERRED — sketch below |
| 10 | Web-tier comparison report is English-only while the desktop is JP | LOW consistency | DEFERRED — sketch below |
| 11 | `list_comparisons` is all-or-nothing — tolerant per-row variant would let exports ship the verifiable subset | LOW robustness | DEFERRED — sketch below |

## What already works (verified by reading both ends)

- **Import→store integrity (native).** `commit_pending` persists
  record+dataset+raw asset atomically; every authoritative read replays
  the pinned importer against the content-addressed raw file and compares
  `dataset_sha256`. Units/references are honest-by-construction: REW text
  exports carry no calibration evidence, so `level_reference` stays
  `unknown` and `absolute_ok` semantics can't be forged.
- **Batch import.** `stage_rew_text_files`/`stage_rew_snapshots` classify
  exact duplicates (sha256 of raw) and same-acquisition re-exports
  (REW `external_source_id`), surface per-item resolution choices, and
  `commit_batch` is idempotent (`already_committed`/`reused` outcomes).
- **Retake lineage.** Append-only lineage records chain
  superseded→selected heads; quality views expose the lineage and
  retake guidance.
- **Comparison persist/replay/history.** `compare_datasets` persists the
  full result + semantics + labels + spec/algorithm hashes; `get`/`list`
  replay-validate every row; the history table renders the persisted
  scalars.
- **Web journey.** `import_measurement`/`import_rew_api_snapshot` →
  `list_measurements` → `get_frequency_response` →
  `save_comparison`/`list_comparisons` → `render_report_html` (metric
  cards, true log-scale SVG, measurement identity table, embedded JSON)
  all work end to end; `duplicate_asset`/`existing_dataset_count` flags
  surface in the React session UI.
- **Export determinism.** `build_analysis_export` sorts series/metadata
  canonically; `series_from_*` requires provenance pins
  (`source_sha256`, operation provenance for derived/transformed).
- **No orphans by construction.** Neither store exposes a delete path,
  so "measurement deleted → dangling comparisons/attachments" cannot
  occur. Dispositions (`excluded_from_normal_use`, `misassigned`,
  `duplicate_import`, `test_only`) are the designed remove-from-use
  mechanism and are honoured by `comparison_candidates`/`measurement_views`.

## 1 — quality-report leg is severed (HIGH, deferred with sketch)

`CadMeasurementQualityEvidence` deliberately requires evidence REW text
exports don't carry (clipping, SNR, timing reference, polarity, IR window,
calibration provenance). The producers exist and are tested —
`build_measurement_quality_report`, `CadMeasurementQualityRepository.save_report`,
`save_observation`, `save_dataset_level_reference` — but **no production
code calls them**. Consequences verified live in code:

- The quality page can only ever show レポートなし (`report_state`
  starts at `'missing'` and nothing ever writes a report).
- `_derived_commit_status` can never leave `quality_pending` → campaign
  cells can never reach `completed` → no campaign can ever complete
  (the existing `test_campaign_cell_commit_and_resume` test documents
  this: the cell lands at 品質確認待ち and stays).
- `level_reference` can never be set to `absolute_spl` →
  `derive_comparison_semantics` can never return
  `absolute_level_comparable` → the comparison page can never claim a
  calibrated A/B level difference.
- Measurement plans pinning `measurement_quality_report_*` authority
  can never satisfy their gate.

**Why deferred, not fixed:** a correct fix is a feature, not a patch —
it needs an evidence-capture path (REW preflight metadata where
available, plus operator-entered acquisition facts) feeding
`CadMeasurementQualityEvidence` → `build_measurement_quality_report` →
`save_report`, and it must never fabricate evidence a file doesn't carry.
Auto-writing all-UNKNOWN reports would add no information and still leave
cells `quality_pending`.

**Sketch:** add an "acquisition evidence" step to the assignment/commit
flow (or a per-measurement quality dialog): capture
`CadMeasurementQualityEvidence{clipping_*, snr_*, timing_*,
polarity_*, ir_window_*, calibration_*}` fields the operator/REW session
can actually attest → `build_measurement_quality_report(measurement_id,
dataset, evidence)` → `quality_repository.save_report(report)` →
`runner_commit_cell` and the quality page pick it up automatically
(`report_state='current'`). REW text imports stay honest UNKNOWN-heavy
by construction; REW API/.mdat sessions can fill more. The plumbing is
already there end-to-end — only the producer UI is missing.

## 2 — saved-comparison reload (fixed)

`_history_selection_changed` set `_last_comparison` and called
`_show_comparison`, which rendered persisted metrics — but the FR overlay,
phase plot and mismatch advisory stayed bound to whatever the selectors
happened to show, and the record's `label_a`/`label_b`/
`level_compatibility`/`semantics_json` were never drawn. The same
comparison looked different depending on the incidental selection.

Fix: `_bind_saved_pair` re-seats both dataset selectors (with preset
fallback to the `any` superset when the pair was compared under another
preset) and re-previews; when a side has since left the eligible set the
live overlay is cleared with a designed message instead of showing an
unrelated pair. `_show_comparison` now prepends the persisted identity
(`A: … → B: …`), the stored level-compat verdict and the persisted
mismatch warnings ("保存時の注意"), labelled from the record — not
recomputed against the live project.

Regression: `test_history_selection_rebinds_saved_pair_and_verdict` —
save A/B, move the selectors to a different pair, select the history row:
selectors return to the persisted pair and the verdict block renders.

## 3 — analysis export carries the verdict (fixed)

The web report's metric cards (mean/RMS/level offset/shape RMS, band
spec, labels) had no counterpart in the native bundle: only the
difference curve was exported as a series. The CSV/JSON/HTML file a user
handed to someone therefore contained the trace but not the comparison's
result. Additionally `_export_analysis_bundle` silently `continue`d past
any measurement whose authoritative read failed, and wrote a file even
when nothing verifiable remained.

Fix: `comparison_metadata_entries(comparison)` emits namespaced
`comparison.<id>.{algorithm_version, requested_band_hz, actual_band_hz,
reference_band_hz, excluded_bands_hz, valid_points, total_grid_points,
mean_difference_db, rms_difference_db, level_offset_db, shape_rms_db,
label_a, label_b, level_compatibility}` into the bundle metadata (Nones
skipped — `AnalysisExportMeta` requires non-empty values). The export
records `omitted.measurement.<id>` / `omitted.comparisons` entries when
authoritative reads fail, refuses to write an empty export, and reports
omissions in the completion dialog's details.

Regression: `test_comparison_metadata_entries_carry_verdict` —
keys/values land in CSV+JSON.

## 4 — one corrupt row must not brick the workspace (fixed)

`measurement_views` called the authoritative `dataset_for_measurement`
unguarded per row — a single tampered/removed raw asset killed the whole
listing (quality page, comparison candidates, campaign tables all consume
it), i.e. one bad row made every healthy measurement unusable. Likewise
`saved_comparisons` validates all rows or raises, and
`_refresh_comparison_choices` let that propagate through `refresh()`.

Fix: `MeasurementView.dataset_error` carries the per-row failure
(dataset stays `None` — downstream consumers remain fail-closed), a
persisted report on an unverifiable dataset reports `stale` rather than
`missing`; the quality table flags the row 検証エラー and the detail
pane shows the reason; `_refresh_comparison_choices` catches the history
failure, renders an empty history and surfaces the error in the notice
bar. This weakens no verification — reads still fail closed, the failure
is just scoped to the row that failed instead of the whole page.

Regression: `test_measurement_views_isolate_unverifiable_dataset` +
`test_workspace_survives_corrupt_dataset_and_comparison_history` (offscreen
Qt: refresh doesn't raise, row flagged, history empty with notice).

## 5 — single-file import duplicate signal (fixed)

Batch imports get `_classify_duplicate` (exact `source_sha256` match, or
`external_source_id` same-acquisition); the single-file lane re-imported
identical bytes silently. `PendingMeasurementImport` now carries
`duplicate_kind`/`duplicate_of_measurement_id` — classified at
`stage_rew_text`/`stage_rew_snapshot`, preserved across
`select_pending_revision` rebinds — the import card surfaces the warning
with the existing measurement's name, and `_commit_assignment` asks for
explicit confirmation before storing a second copy. Advisory only: the
operator can still intentionally import-as-new (the batch lane's
`import_as_new` equivalent), which is the correct behaviour for e.g.
deliberate re-measurement to a different binding.

Regression: `test_single_import_flags_exact_duplicate_and_survives_revision_rebind`,
`test_single_snapshot_import_flags_same_acquisition`,
`test_single_import_commit_confirms_duplicate` (offscreen Qt with
`QMessageBox.question` monkeypatched).

## 6 — label map gap (fixed)

`_mismatch_label` now covers all 11 advisory codes
`derive_comparison_semantics` can emit; previously five axes fell back to
raw English snake_case inside a JP UI.

## 7/8 — delete & replace are deliberately absent (standing)

Neither store has a measurement delete — the domain model is append-only:
dispositions remove evidence from normal use without destroying it, and
retake lineage supersedes instead of replacing. This is *the* integrity
convention of the repo (fail-closed evidence, replay-validated reads) and
any delete-with-cascade feature would cut against it: a deleted
measurement would make every comparison/report that references it
unverifiable forever. If a "remove" UX is ever wanted, it should be a
disposition preset (`excluded_from_normal_use`) with a rename, not a row
delete. No code change; documented here so the gap reads as design, not
omission.

## 9/10/11 — smaller deferred notes

- **Per-comparison/per-plot export.** The bundle export is all-or-nothing;
  a "この比較をCSVで保存" button on the comparison page plus PNG snapshot
  of `comparison_plot` (pyqtgraph ` exporters.ImageExporter`) is ~a page
  of code on top of `series_from_comparison` +
  `comparison_metadata_entries`. Kept out of scope: needs a UX decision
  (where the button lives, whether exports include the advisory text).
- **JP comparison report.** The desktop comparison page is JP, the web
  report template is EN — a `render_report_html(..., locale=` split or a
  JP `render_comparison_report` for the workspace is straightforward but
  doubles a template; worth doing when the report gains the metrics the
  bundle now carries.
- **Tolerant `list_comparisons` variant.** Today the index validates
  all-or-nothing; a `list_comparisons_degraded()` returning
  `(comparisons, failures)` would let exports ship the verifiable subset
  with per-row omission notes instead of one `omitted.comparisons` entry.
  The current handling is honest (recorded, not silent); the refinement
  is only worthwhile if corrupt-comparison encounters become routine.

## Files changed

- `src/htdt/measurement_workflow.py` — `PendingMeasurementImport.duplicate_*`
  fields + classification at both single-file staging paths +
  `select_pending_revision` carries them; `MeasurementView.dataset_error`
  + per-row guarded dataset read + stale-report marking.
- `src/htdt/measurement_page_workspace.py` — duplicate advisory on the
  import card + commit confirmation; `dataset_error` flagged in the
  quality table/detail/plot; guarded comparison-history load;
  `_bind_saved_pair` + persisted identity/verdict rendering;
  `_level_compatibility_label`; `_mismatch_label` completed.
- `src/htdt/analysis_export.py` — `comparison_metadata_entries`.
- `src/htdt/workflow_application.py` — export records omissions, refuses
  empty exports, ships comparison verdict metadata.
- `tests/test_round8_measurement_journey.py` — 7 new tests.
- `docs/reviews/round8-journey.md` — this file.

## Tests

`cd backend && TMPDIR=/c/t C:/devin/python/python.exe -m pytest -q -n 4`
— see commit message / session report for the run result. New tests:
`tests/test_round8_measurement_journey.py` (7 tests, all passing under
`-n 4`).

Environment note: no Python was present on the box; the official
python.org silent installer stalled (known Server-2022 quirk), so Python
3.12.10 was provisioned from the NuGet `python` package into
`C:\devin\python` — matching the pinned 3.12.10 toolchain in
`requirements-n05-windows.lock` — and `pip install -e backend[dev]` ran
under it.
