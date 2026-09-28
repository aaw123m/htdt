# Round 10 — convergence: deferred items implemented

Scope: implement the deferred items recorded by rounds 6–9 (parity #8–11,
data-safety #8, report #7 / journey #1 severed legs) plus any concrete safe
sketches (polygon winding, cad_units). Branch `devin/rev10-dataj`. No GitHub
Actions — verified locally with pytest.

## Implemented

### Parity #9 — datasets now verify read-time integrity (the real fix)

The round-9 sketch predicted a cheap honest version of the native
hash-on-read contract; that is what shipped. The web store's datasets table
gains a `dataset_sha256` column through the **existing migration pattern**
(`SCHEMA_VERSION` 5 → 6, conditional `ALTER TABLE` + backfill loop, same
shape as the `spec_sha256` migration that landed for constraint_sets):

- `database.dataset_row_sha256(kind, frequency_blob, level_blob,
  phase_blob, metadata_json)` — `canonical_sha256` over the row's
  authoritative fields. `metadata_json` hashes the stored TEXT verbatim so
  the hash survives serializer drift.
- `import_measurement` / `import_rew_api_snapshot` compute and store the
  hash at write time; the migration backfills existing rows.
- `get_dataset_descriptor` / `list_measurements` recompute on every read
  and expose `integrity_valid`; `get_frequency_response` raises the new
  `DatasetIntegrityError` (subclass of `AssetIntegrityError`, so umbrella
  corruption handling still applies) on mismatch or a missing hash —
  `feature_candidates` and `create_comparison` map it to **409**, matching
  the constraint_sets contract. `integrity_problems()` reports
  `dataset_missing_hash:` / `dataset_hash_mismatch:` per row.

Why this shape rather than pinned-importer replay: replay is a store
migration (the round-9 note agrees); the row-hash closes the silent-tamper
gap using the pattern the store already commits to.

### Parity #8 — compare eligibility contract on the web

The doc's "needs a schema change to a frozen store" constraint **no longer
holds**: verdict fields are per-comparison data and persist inside
`comparisons.result_json`, which is the comparison row's own payload — no
DDL required. What changed:

- `ComparisonCreate.force: bool = False`.
- `create_comparison` now computes per-side eligibility with the same rule
  `feature_candidates` already uses (`evidence_type == 'measured'` and
  `quality_status != 'invalid'`), plus the new integrity check. An
  ineligible side without `force` → **422** naming the side and reason and
  telling the caller to resubmit with `force=true`. With `force` the
  comparison proceeds and is recorded as such.
- Persisted `result` gains `level_compatibility`, `label_a`/`label_b`
  (`channel_role · evidence_type · R<revision>`), `eligibility` (per-side
  reason or `'eligible'`), and `forced`.
- `level_compatibility` mirrors the native `LevelCompatibility` taxonomy
  (`cad_comparison_semantics.py`): `normalized_shape_comparable` when the
  datasets share level-reference semantics or an explicit reference band
  was supplied; `diagnostic_only` when forced or the level references are
  incomparable. `absolute_level_comparable` is **never** emitted — the web
  store has no calibration authority (`level_reference` parses as
  `'unknown'` for REW text), and claiming absolute comparability without it
  would be dishonest. The persisted renderer version key is unchanged.
- SPA: a "無効・非実測の測定も診断目的として比較する（結果は
  diagnostic_onlyと記録）" checkbox drives `force`; the verdict line and
  `levelCompatibilityLabel` (labels.ts) render the persisted verdict in JP;
  comparison report links offer JA + EN HTML.

### Parity #10 — `lang` field on the report payload

`REPORT_RENDERER_VERSION` stays `'comparison-report-1'` — the payload is
rebuilt per request, so nothing persisted is reinterpreted.

- `build_report_payload(project, comparison, *, lang='en')` validates
  `lang ∈ REPORT_LANGUAGES` (`'en'`, `'ja'`), adds `'lang'` to the payload
  and a per-language `interpretation_notice`.
- `render_report_html` selects the `_REPORT_STRINGS[lang]` table; every
  formerly-inline EN literal is now a table entry (EN table is
  byte-identical to the old template, so existing reports render
  unchanged). JA table covers headings, metric names, table headers, empty
  states, and SVG aria/axis text; `_JA_TOKEN_LABELS` mirrors
  `frontend/src/labels.ts` so enum tokens (`front_left` → フロント左 etc.)
  translate in JA reports.
- A `verdict` section renders only when the persisted result carries
  `level_compatibility` — comparisons saved before this round simply omit
  it; persisted results are never re-versioned.
- Endpoints: `report.json`/`report.html` accept `?lang=en|ja` (invalid →
  422 via the `Literal` query type). SPA links now offer both.

### Parity #11 — session naming

Cheaper honest fix chosen: the web feature's display name is renamed
**測定グループ** everywhere it appears (MeasurementSessions panel + nav),
and the panel hint now states the distinction outright — "API上の
session_id。native側の測定セッションauthorityとは別系列です". Wire keys
(`session_id`, `/sessions` routes, identifiers) are unchanged; only
user-visible strings moved.

### Data-safety #8 — project lifecycle UI on the library page

`ProjectLibraryPage` was read-only over the registry; the lifecycle
authority (`archive_project`/`unarchive_project`/`plan_project_deletion`/
`delete_project`) existed but was reachable only from tests (the shell
menu's archive dialog covered archive/unarchive only). Now:

- `ProjectEntry.archived`; `list_projects` returns archived rows so they
  stay restorable (the row marks them 「アーカイブ済み」).
- `ProjectLibraryService` exposes `set_archived`, `plan_project_deletion`,
  and `delete_project(project_id, expected_plan=…)` over a lazily-built
  `project_lifecycle.ProjectLibrary` on the same store file.
- The page gains an アーカイブ / アーカイブ解除 / 削除… button row. The
  currently-open document refuses all three (the shell is standing on it);
  archived rows refuse open and re-archive.
- Delete follows the existing confirm contract (the same shape as
  capture-retention's dry-run purge): a read-only plan preview renders
  every consequence — per-authority row counts (`_LIFECYCLE_TABLE_LABELS`
  for the common tables, raw names verbatim otherwise), estimated bytes,
  shared-vs-local asset counts, pending missions/inbox — then confirms,
  archives first when needed, re-plans (so `delete_project`'s fingerprint
  check validates the post-archive world), and deletes atomically.
  `project_not_archived` is handled as a step, not a blocker; every other
  hard blocker is surfaced by `kind`/`detail` verbatim and refuses to show
  a confirm. `StaleError` re-plans with an honest retry message; the
  tombstone's removed-row count is reported on success.

### Report #7 / journey #1 — biquad leg wired; auralization leg documented

**Biquad settings export — wired.** The upstream evidence the deferral
waited on exists in production: `run_joint_execution` persists
`CadCalibrationPlan`s via `calibration_repository.save_plan`, and
`CadCalibrationWorkflowService.export_settings` already produces the
deterministic `{export, json_text, csv_text}` bundle plus the `exported`
lifecycle fact. The leg needed reachability, not new machinery:

- `calibration.export_settings` command (`校正設定（汎用バイクアッド）を
  書き出す`) registered and bound to
  `WorkflowApplication._export_calibration_settings`: pick a SUPPORTED
  plan → `export_settings` → directory pick → atomic `calibration-N`
  `_settings.json`/`_settings.csv` via `claim_export_stem` +
  `write_export_files` (the round-9 export_io pattern), reporting the
  export id + settings sha256. UNSUPPORTED-only selections refuse with the
  plan's own `unsupported_reasons`, and the info box states that export ≠
  applied (the `mark_user_applied` boundary is unchanged).
- Deliverables catalog gains `calibration.biquad_settings`
  (commissioning_verification): `available` pinned to
  `calibration_plan:<id>` when a SUPPORTED plan exists; `blocked` with an
  honest reason when only UNSUPPORTED plans exist; `not_applicable` with a
  deep-link to Optimization → 測定・検証 when no plan exists at all.

**Auralization WAV leg — documented as still severed, precisely.** The
round-8 prerequisite is *not* met by export-verdict metadata. What the
writer chain (`build_auralization_render_spec` → `render_auralization` →
`encode_wav_pcm_s16le` → `build_auralization_artifact`) actually needs:

1. An `ImpulseAuthorityRef` — exact pinned identity of a decoded IR
   (`artifact_id`, `artifact_sha256`, `decoded_pcm_sha256`, sample rate,
   `absolute_amplitude_authority`). No production code resolves a
   persisted predicted/measured IR artifact into this shape; IR artifacts
   land in stores with no resolver mapping them to the authority ref.
2. A `DryProgramAssetRef` — a registered mono program asset; nothing in
   production registers one.
3. An `AuralizationArtifact` repository + a spec-building service pinning
   `source_scenario_id`, receiver, scene revision — none exist.

A deliverables row pointing at this would promise a button that cannot
reach real artifacts — the catalog deliberately stays honest by omitting
it. The three items above are the exact build-out that would unblock it.

## Still deferred (sketches require product/schema decisions)

- **Polygon winding normalization** (round6-spec D1): normalization would
  change stored polygon identities/content hashes — a persisted-data
  semantics decision, not a patch; no safe implementable sketch.
- **`cad_units` 'db' family conflation** (round6-spec D2): schema split of
  a persisted unit family — same standing deferral as round 9.
- **Auralization WAV deliverable**: see above — three named prerequisites
  missing; not a UI wiring problem.

## Verified locally

- `backend`: full suite — `pytest tests -q -n 4` (results in PR body);
  new `tests/test_round10_dataj.py` covers the eligibility gate (422 +
  forced diagnostic_only + persisted verdict fields), read-time integrity
  (tamper → 409 on feature-candidates/compare + `dataset_hash_mismatch`
  on `/api/integrity`), `lang` en/ja/invalid on both report endpoints, and
  the library-page lifecycle wiring (archive gating, アーカイブ済み
  rendering, blocked-plan refusal, confirm→archive→delete flow).
- `frontend`: `tsc` + `vite` build clean.
