# Round 18 — convergence sweep

Scope: different-reviewer's-eyes pass over `main` after rounds 15–17 merged
(5086c6bc). Not another niche dimension — the five-part sweep from the task:
(1) end-to-end first-time-user journey, (2) cross-cutting patterns the
per-dimension rounds each half-saw, (3) deferred-item reclassification audit,
(4) regression read of recent fix diffs, (5) verdict. Verified locally
(Windows, `QT_QPA_PLATFORM=offscreen`, `pytest backend/tests -q -n 4`,
Python 3.12.10). Branch `devin/rev18-sweep`.

**Verdict: NOT YET CONVERGED — three residual defect families found and
fixed.** Each is a *sibling straggler* of a pattern an earlier round closed
at individual sites but never swept codebase-wide. After this diff the
`10.0 **` overflow surface, the `_disposed`/`_closed` launch-guard
convention, and the `ORDER BY created_at_utc` tiebreak convention are each
uniform across the codebase — which is what "converged" requires.

## Sweep ledger

| Sweep | Result |
|-------|--------|
| Golden journey (fresh eyes) | Startup→workspaces→shutdown lifecycle paths re-walked (launch wiring, measure/optimize/batch-commit, automatic backup, capture-receiver pairing, project-template instantiation). No unmentioned rough edges in the happy path; the two genuine catches were lifecycle-adjacent (see `_disposed` stragglers below), not new UX friction |
| Cross-cutting pattern sweep | **`10.0 ** (db/k)` family**: `OverflowError` is NOT a `ValueError` subclass, so every `except ValueError` boundary (`user_facing_error._map_exception`, module raise-contracts) let it escape. r16 fixed one site (`smoothed_level_trace`) with the `min(max(x, -300), 300)` saturation convention; this sweep found **16 sibling sites across 10 modules** on finite-only-validated inputs — the same unhandled-error shape, one systemic fix class. **Fix split**: statistical/data summations saturate (bit-identical on all previously-working inputs); operator-declared gain authorities (biquad `gain_db`, FIR `gain_db`, drive `gain_db`, auralization `rms_target_dbfs`, dBV/dBu converters) refuse with `ValueError` — a saturated gain would persist garbage-but-valid authority, honest refusal matches the module contract |
| Deferred re-check | ~20 deferred items sampled across r13–r17 docs. Correctly deferred (design-level): workers D1/D2/D3/D6, `capture_disposition_transitions` schema migration, import-as-copy remap, `response_model`/pagination, `BackupError` subclass, per-`Device:` bucketing, settings lazification, `scene_document_heads` FK, MAX_PATH, `progress_sink` lifecycle, frontend fetch timeout (retired stack), `localization.format_datetime` dead code, naive-timestamp cosmetics, append-only tables, PARSER_VERSION pin. **Misclassified**: the r15-time "opportunistic" `ORDER BY created_at_utc` tiebreaks were labelled non-urgent but are one-line diffs with deterministic-correctness value — three remained |
| Regression read of r15–r17 diffs | LRU-bounded schema signatures, activity-center eviction fallbacks, cert minting, excepthook wiring — consistent, no regression shapes found (r4-style pattern hunt: none). One **preexisting env-level failure** surfaced: `test_cad_directivity_source_assets::test_persisted_dataset_reopens_exact_source_bytes` fails on clean `main` — `str(Path('measurement-assets') / digest)` emits `\\` on Windows while the stored row/normalization uses `/`. Harness/platform-level, unrelated to this diff — flagged for a windows-platform follow-up rather than fixed here (the stored-path convention needs a deliberate normalization decision) |
| `_disposed`/`_closed` guard sweep (r17 sibling class) | Two `pool.start` launch paths still missing the guard: `MeasurementPageWorkspace._commit_batch_assignment` (bypasses the guarded `_start_job` helper — calls `_job_pool.start` directly) and `AutomaticBackupRunner.start` (checked `_attempted`, not `_closed`). A late signal post-dispose → `RuntimeError('native worker pool is shut down')` out of the slot |

## Fixed this sweep

| File | Change |
|------|--------|
| `cad_phase_time_analysis.py` | `compute_minimum_phase_deg`: exponent clamp `min(max(v/20, ±300))` on measurement levels (finite-only validated; >6160 dB used to OverflowError) |
| `cad_correction_design_policy.py` | Same clamp at all three power sites: `_fractional_octave_smooth` (both branches) and `_aggregate_band` `linear_power_mean` |
| `cad_ambient_noise.py` | `ambient_overall_level_db` band-power sum clamps; `list_profiles` gains `, profile_id` tiebreak |
| `cad_equipment_self_noise.py` | `combine_noise_sources_energy` power sum clamps |
| `cad_directivity.py` | `evaluate_directivity`: all three `magnitude_db → linear` conversions clamp (exact hit, magnitude_only interpolation, complex phasor sum) — imported balloons are finite-only, so a 7000 dB sample crashed every evaluation touching it |
| `cad_calibration.py` | `calculate_biquad_coefficients` peaking `gain_db`: `OverflowError → ValueError` (module's declared contract) |
| `cad_auralization.py` | `render_auralization` `rms_target_dbfs`: `OverflowError → ValueError` |
| `cad_fir_filter.py` | `evaluate_fir_artifact` output `gain_db`: `OverflowError → ValueError` |
| `cad_multi_channel_excitation.py` | `compose_coherent_system_response` drive `gain_db`: `OverflowError → ValueError` |
| `cad_gain_structure.py` | `dbv_to_vrms`/`dbu_to_vrms`: `OverflowError → ValueError` (API-contract completeness; no live in-tree callers) |
| `scientific_plot_style.py` | `PlotCursor._display_value` log-mode `10.0**raw`: saturates — a reference line parked at an extreme coordinate crashed the hover readout |
| `measurement_page_workspace.py` | `_commit_batch_assignment`: `if self._disposed: return` guard at top (r17 convention) |
| `automatic_backup_runner.py` | `start()`: `or self._closed` — post-shutdown kick returns `False` instead of `RuntimeError` |
| `capture_receiver.py` | `list_pairings`: `ORDER BY created_at_utc, pairing_id` |
| `cad_project_template_repository.py` | `instantiation_for_document`: `ORDER BY created_at_utc DESC, instantiation_id DESC` |
| `backend/tests/test_review_round18_convergence.py` | 13 regression tests; all verified red on base, green on branch |

## Still deferred (correctly — design-level, none small)

- **workers D1/D2/D3/D6** — wedged-worker veto, uncancellable data ops,
  flag-dropping jobs, deliberate lingering threads: UI-contract changes.
- **`capture_disposition_transitions`**, **`scene_document_heads` FK**,
  import-as-copy, `response_model`/pagination, `BackupError` subclass,
  per-`Device:` bucketing, settings lazification, MAX_PATH,
  `progress_sink` lifecycle — all need contracts/schema/product calls.
- **Windows path-separator normalization** for stored asset relative paths
  (the preexisting failure above) — needs a deliberate convention choice.
- `localization.format_datetime` misleading dead code, naive-timestamp
  cosmetics, append-only table growth policy, PARSER_VERSION pin —
  documented design state, intentionally unchanged.

## Suite result

Scoped run on every touched module's suite plus the new regression file
(`pytest backend/tests -q -n 4`, offscreen, 27 files, ~310 tests):
**all pass except the one preexisting Windows path-separator failure**
documented above (fails identically on clean `main`). New tests were
verified red against the pre-change code before the diff was applied.
