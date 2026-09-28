# Round 9 — native Qt app ↔ React SPA (legacy web store) parity & consistency

Scope: which user-facing capabilities exist in one surface but not the other,
and whether each asymmetry is documented/intentional or an unfinished-product
gap. Journeys mapped: room CAD, measurement import/quality/comparison,
optimization, reports, settings, calibration, capture — vs the SPA's panels and
`/api/*` routes. Store invariants, terminology, and duplicated flows
(measurement list / compare / report) checked for matching values, formats,
and verdicts. Prior `docs/reviews/round*.md` were read first; their findings
are not re-reported. Branch `devin/rev9-parity`.

## Framing: the asymmetry is policy, the disclosure was missing

`docs/IMPLEMENTATION_STATUS.md` freezes the browser UI for new CAD features
("browser UI | 新CAD機能は凍結…二重実装しない"), the native release build
excludes the frontend, and the legacy API itself is dev-gated
(`HTDT_LEGACY_API=1`, loopback boundary, docs opt-in, `/api/backup` +
`/api/restore` → 410). So the product ships **two surfaces over two different
stores with deliberately divergent capability sets**:

- **Native (PySide6)** — `cad-scenes.sqlite3`; authoritative reads re-verify
  content hash + transformation seal + pinned-importer replay on every read;
  comparisons replay-validated on save and list.
- **Legacy web SPA** — `htdt.sqlite3` + `assets/`; trust-on-read for
  measurements/datasets/comparisons (constraint_sets and search_specs *do*
  verify `spec_sha256` on read and 409 on mismatch — the verification is
  partial, not absent).

The audit therefore asks: is each gap *declared*, and do the flows that
**do** exist on both sides agree?

## Capability matrix

| Capability | Native Qt | React SPA | Verdict |
|---|---|---|---|
| Room CAD (author/edit geometry, objects, placement) | Yes (Room workspace, revisions) | Read-only context form → new immutable revision (narrow: dims, footprint, MLP, mic, speakers) | INTENTIONAL gap (frozen UI) |
| Measurement import (.frd/.txt/.csv) | Yes, pinned importer + replay-verify | Yes, same `parse_rew_frequency_response` parser; no replay on read | Parity of import, asymmetric verification (known: r4/r7/r8) |
| REW REST (status/preflight/FR preview/snapshot import) | Via `RewApiClient` inside runner/import flows | Dedicated read-only panel (`RewReadonly`) | Overlapping, different presentation — not a gap |
| Measurement quality / dispositions | Full (quality report, dispositions, eligibility gating) | Caller-supplied `quality_status` flag only | INTENTIONAL gap; compare gate deferred below |
| A/B comparison | `compare_frequency_responses`, eligibility-gated, persisted verdict semantics | Same algorithm + role/confounder classification, no eligibility gate | Same core math; gating differs (deferred #8) |
| Comparison plots | A/B overlay + dedicated A−B difference plot | A/B overlay only | FIXED (SPA now renders DifferencePlot) |
| Reports | Analysis export bundle (native) | Self-contained `report.html`/`report.json` per comparison | Both exist; formats differ by design; report.html EN-only (r8 #10) |
| Optimization (search/constraints/candidates/interventions/robustness/validation) | Full Optimization workspace | `PlacementConstraints` + `SearchSpace` panels over `/api/constraint-sets`, `/api/search-space` | Partial web coverage; native-only stages INTENTIONAL |
| Settings / preferences | Preferences store + dialogs | None | INTENTIONAL gap |
| Calibration lifecycle | Yes | None | INTENTIONAL gap |
| Capture (REW capture orchestration) | Yes | Read-only REW status only | INTENTIONAL gap |
| Sessions | Runner plans/campaigns (measurement runs) | `MeasurementSession` = named grouping container | Same word, different concept — deferred #11 |
| Inbox / Activity / Library / Support destinations | Yes | None | INTENTIONAL gap |
| Deep links | `htdt://workspace/…` URIs | `#anchor` fragments | Different mechanism by design; anchor coverage was incomplete — FIXED |
| Backup/restore | Native store backup | 410 retired | BY DESIGN (documented 410) |

No unfinished bridges found in the SPA: all 49 `api()` call sites map to
registered routes, the 410s are intentional retirements, empty states have
honest copy, no TODO/"coming soon" UI. `--legacy-ui` init-order crash is
pre-existing and already recorded.

## Where the surfaces disagreed on the same data (all fixed)

The SPA duplicated native measurement-list/compare/report flows but showed
stored enum tokens verbatim (`front_left`, `usable`, `measured`, `verified`,
`valid`, `repeatability`, `microphone_calibration`) where the native app maps
the identical values to Japanese labels — same store rows, different display
names. Similarly: EN headings on a JP-first surface, EN plot axes, 2-decimal
compare metrics vs the native 3-decimal JP metrics, and no difference plot.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | SPA rendered raw enum tokens (channel_role / quality_status / evidence_type / routing_evidence / phase_status / comparison_role / attachment kind) everywhere the native app shows JP labels for the same values | MED consistency | FIXED — new `frontend/src/labels.ts` mirrors the native label maps; applied in App, MeasurementSessions, ComparisonReports, PlacementOverview, FeatureCandidates, RewReadonly |
| 2 | SPA never disclosed its own scope: nothing told the user this is the dev-only legacy web surface over `htdt.sqlite3`, separate from the shipped native app's `cad-scenes.sqlite3` — an undocumented asymmetry that reads as the product | MED UX/disclosure | FIXED — hero hint states the store split + frozen-UI policy |
| 3 | Deep-link/navigation coverage gap: `#assets` had no nav entry; the four supplemental panels (`sessions`, `reports`, `history`, `rew`) had no `id` at all — unreachable except by scroll | MED usability | FIXED — ids + nav entries + icons + JP nav labels |
| 4 | Compare metrics diverged from native: EN names (`Mean A−B`, `RMS diff`, `Level offset`, `Shape RMS`) at 2 decimals vs native JP names at 3 decimals | LOW consistency | FIXED — 平均差/RMS差/レベル差/形状RMS + `toFixed(3)` |
| 5 | Native shows a dedicated A−B difference plot; SPA plotted only A and B (the saved-snapshot `report.html` also A/B only — left unchanged, see deferred) | LOW parity | FIXED — `DifferencePlot` (zero-referenced, log-f) in the compare block |
| 6 | Plot + 3D room axes/legends English (`Frequency (Hz)`, `X right (m)`, `Room boundary`, raw `speaker.role` in markers) on a JP surface | LOW consistency | FIXED — 周波数/レベル/レベル差, X 右/Z 上/Y 後方, 部屋境界/参照ボックス/スピーカー |
| 7 | Mixed-language headings & options: `Measurement Sessions`, `Saved Comparisons`, `Layout History`, `REW read-only browser`, `Features`, `Search Space`, `Placement Constraints`, `Untitled session`, `REFERENCE`, `CHANGED`/`same`, `enabled`/`disabled`, `offline`/`connected · read-only`, EN select options | LOW consistency | FIXED — JP throughout |
| 8 | Comparison eligibility asymmetry: native `compare_datasets` restricts to `is_normally_eligible` datasets and persists `label_a`/`label_b`/`level_compatibility`/`semantics_json`; web `create_comparison` compares any two datasets (warnings only) and persists `comparison_role`/`context_differences`/`confounders` instead | MED correctness-risk | DEFERRED — needs a disposition model in the web store; sketch below |
| 9 | Store-invariant asymmetry: native verifies hash + pinned-importer replay on every read; web serves measurement/dataset/comparison blobs trust-on-read (while constraint/search specs do verify `spec_sha256` → 409) | MED integrity | DEFERRED — known since r4/r7/r8; sketch below |
| 10 | `render_report_html` is English-only (metrics, section titles, interpretation notice) while the SPA and native UI are JP | LOW consistency | DEFERRED — same as round8-journey #10; bumping `comparison-report-1` renderer is a format decision |
| 11 | "Session" means different things in each surface: web `MeasurementSession` is a named grouping (purpose/started_at/notes); a native runner session is a measurement run plan | LOW terminology | DEFERRED — rename web concept (e.g. 測定グループ) or document; product call |
| 12 | Web `Context` (`R{n}`) vs native `SceneRevision` (リビジョン): same concept, two names | INFO | ACCEPTABLE — both render as `R<revision_number>`; naming noted |

## Deferred sketches

**8 — compare eligibility on web.** Give `create_comparison` the same contract
the native compare has: refuse (or require an explicit `force` flag) when a
dataset's quality/disposition marks it ineligible, and persist the verdict
fields (`level_compatibility`, label_a/b) so the report can state *what kind*
of comparison it is. Needs a disposition/eligibility column set on the legacy
`datasets` table — a schema change to a frozen store, hence deferred.

**9 — read-time verification for measurement blobs.** Cheapest honest version:
extend the existing `integrity_problems()`/`integrity_valid` pattern already
used for constraint_sets/search_specs to datasets — verify stored
`sha256`/row-hash on read and 409 on mismatch, mirroring what the web store
already does elsewhere. Full pinned-importer replay belongs to a store
migration, not a patch.

**10 — bilingual report renderer.** `render_report_html` emits a fixed EN
template under `REPORT_RENDERER_VERSION='comparison-report-1'`. Localizing
means either a `lang` spec field or a `-jp` renderer version bump; both are
format-contract decisions. Until then the SPA panel is JP and the downloaded
report is EN — disclosed by this note.

**11 — session naming.** Rename the web feature's display name to 測定グループ
(grouping container) to stop colliding with the native runner-session concept,
or keep the name and document the distinction in the panel hint (currently the
hint already says "同じ測定作業のまとまり" — close, but shares the word
Session).

## Store invariants — reference table

| Invariant | Native `cad-scenes.sqlite3` | Web `htdt.sqlite3` |
|---|---|---|
| Read verification | hash + seal + pinned importer replay, every read | none on measurement/dataset/comparison; `spec_sha256` verify on constraint_sets/search_specs only |
| Measurement parser | `parse_rew_frequency_response` | same function |
| Comparison algorithm | `compare_frequency_responses` | same function |
| Mutability | append-only revisions + dispositions | caller-settable quality on import; no delete either side (by design, r8 #7) |
| Backup | native store backup | retired (410) |

## Verified locally

- `frontend`: `npm ci` + `npm run build` (tsc + vite) — clean.
- `backend` (no backend changes; regression subset): `test_api.py`,
  `test_comparison.py`, `test_comparison_context_strip.py`,
  `test_report_api.py`, `test_built_frontend.py`,
  `test_frontend_containment.py`, `test_browser_backup_retired.py`
  — 44 passed.
