# Round 7 — React SPA Deep Pass (`frontend/src/`)

Scope: the whole Vite/React 19 surface — `App.tsx`, `ConstraintBuilder.tsx`,
`SearchSpace.tsx`, `PlacementConstraints.tsx`, `MeasurementSessions.tsx`,
`ComparisonReports.tsx`, `PlacementOverview.tsx`, `RewReadonly.tsx`,
`FeatureCandidates.tsx`, `plots.tsx`, `ErrorBoundary.tsx`, `api.ts`,
`styles.css`, plus a new `copy.tsx`. Axes: component behavior (silent
validation, stale UI), interaction gaps (no retry, focus loss, silent
discard), design consistency (color tokens vs hardcoded values, focus
visibility, dark-theme coverage), feature affordances, and wire-check for
dead props. Round 3–6 findings are not re-reported.

Headline finding: two independent focus/state bugs made ordinary editing
hostile — typing into a speaker ID remounted the row on every keystroke, and
every loader failure was a dead end with no retry. The other big item:
`index.html` already advertised `color-scheme: dark light`, so dark-mode
users got browser-darkened form controls on top of a light-only stylesheet.
The app is now fully themed for both schemes.

## Fixed this round

| ID | Severity | Area | Finding | Resolution |
|----|----------|------|---------|------------|
| R7-F1 | High | `App.tsx` speaker form | Speaker rows used `key={`${speaker.speaker_id}-${index}`}` — every keystroke in the ID field changed the key, React remounted the row, and focus was dropped mid-typing. Effectively impossible to type a multi-character speaker ID. | `key={index}` (rows are ordered, insertion is append-only). |
| R7-F2 | Medium | `App.tsx`, `ComparisonReports.tsx`, `PlacementOverview.tsx`, `MeasurementSessions.tsx`, `PlacementConstraints.tsx`, `SearchSpace.tsx` | Every project/data load failure rendered an error string and stopped — no retry affordance anywhere. A transient failure meant reloading the page (and losing form state). | Per-panel `loadError` state + inline 再読込 button that re-runs the failed load (`retryLoad`/`reloadConstraintSets`/`reload`). Action errors (`error`) still render separately so a failed save doesn't wipe the retry affordance. |
| R7-F3 | Medium | `App.tsx` | `reloadProjectData('')` early-returned without clearing `contexts`/`measurements`/`comparisons`/`attachments` — deselecting a project left the previous project's lists rendered. | Explicit clear of all four lists on empty id; the `projectId` effect also resets `message`/`error`/`loadError` so a stale success notice doesn't bleed across projects. |
| R7-F4 | Medium | `App.tsx`, `MeasurementSessions.tsx` | Uncontrolled `<input type="file">` elements kept their filename after a successful save (measurement file, attachment, REW session import) — the form looked dirty and a re-save looked armed when `measurementFile` was actually null. | `ref` on each file input; `.value = ''` after successful save. |
| R7-F5 | Medium | `App.tsx`, `SearchSpace.tsx`, `ConstraintBuilder.tsx` | Client validation gaps vs the backend validators — all were 422 round-trips with raw detail text: empty/duplicate `speaker_id` and `speaker_id='MLP'` (collides with the measurement point id), `GridAxis` `step_m <= 0` and `max_m < min_m`, `candidate_limit` non-integer or outside 1–50000 (`SYSTEM_MAX_RAW_CANDIDATES`), negative `EntityProfile` radius/margin, and A/B compare with `datasetA === datasetB`. | Mirror the backend rules client-side with localized messages (speaker_id重複/MLP不可, stepは0より大きい値, minがmaxを超えています, candidate limitは1〜50000の整数, Radius/Marginは0以上, 同じ測定同士は比較できません). |
| R7-F6 | Medium | `styles.css`, `plots.tsx`, `index.html` | `color-scheme: dark light` is declared, so dark-mode users got OS-darkened form controls/scrollbars on a light-only stylesheet — and every surface, map, and token was hardcoded light. | Full `@media (prefers-color-scheme: dark)` pass (~75 rules): CSS vars re-mapped (`--ink`/`--surface`/`--fill`/`--accent`/`--accent-soft`/semantic colors/shadow), plus every hardcoded light surface (panels, cards, chips, sketch/wall/search maps, builder, workflow nav, notices, pills). Plotly plots get transparent paper + light font via `themedLayout()` in `plots.tsx`. |
| R7-F7 | Medium | `styles.css` | Five different "active" blues hardcoded across components (`#edf5ff`, `#e8f2ff`, `#eef6ff`/`#e7f1ff` gradient, `#f4f9ff`) — the same semantic state rendered five different colors; `.danger-text` used `#c53030` vs `--danger` `#c9342f`; exclusion-region red `#d23b3b` was a third danger red. | New `--accent-soft` token used by entity chips, rule kinds, stack symbols, axis badges, link cards, preview pill, candidate rows; `.danger-text` and exclusion strokes now use `--danger`. |
| R7-F8 | Medium | `styles.css` | No `:focus-visible` outline on buttons/links — keyboard users had no focus indicator on any button, nav link, or ghost action (inputs had one; SVG sketch tools got theirs in round 3). | `button:focus-visible, .button-link:focus-visible, a:focus-visible` accent outline. |
| R7-F9 | Medium | `ConstraintBuilder.tsx` | The builder's × button (and any accidental click path) silently discarded all drafted rules + sketched polygon points — real data loss with no confirm. | `closeBuilder()` confirms via `window.confirm` when rules/points exist; `Esc` now closes through the same path. |
| R7-F10 | Low | `ConstraintBuilder.tsx` (round 5 leftover) | Dead wire: `Draft.polygonGroups` + `PolygonSketch`'s `groups`/`onGroupsChange` were threaded through three layers but nothing ever populated them — the "completed polygons" render path was unreachable dead code. | Removed `polygonGroups` from `Draft`, `groups`/`onGroupsChange` props, and the unreachable `completed` render. |
| R7-F11 | Low | `App.tsx`, `MeasurementSessions.tsx`, `PlacementConstraints.tsx`, `FeatureCandidates.tsx`, `RewReadonly.tsx` | Async warnings lists used `key={warning}` — duplicate warning strings produce duplicate React keys; `Measurement.metadata` (`phase_status`, `warnings`) was fetched but never rendered; `EvaluationResult.observations` never rendered. | Index keys for warning lists; measurement cards now show `phase` + `warnings`; constraint result shows `n/m checks passed`. |
| R7-F12 | Low | all panels | Notice roles: error/success banners were plain divs (invisible to screen readers); `.plot` divs had `aria-label` but no `role`; toggle-style buttons (`entity-chip`, `segmented`, `rule-kind`, help `?`) lacked `aria-pressed`; pagination arrows and the Context select had no labels; session measurement list had no empty state; zero-feasible search result rendered an empty browser; 'checking' health label was the last English string. | `role="alert"`/`role="status"` on notices, `role="img"` on plots, `aria-pressed` on toggles, `aria-label` on icon-only controls, empty states for session measurements and zero-candidate results, 「確認中」. |

## Feature additions

- `copy.tsx`: `CopyCode` — click-to-copy chip (clipboard API + textarea
  fallback, "copied" feedback) applied to `dataset_id`, `asset_sha256`,
  comparison/session ids. Cards that showed truncated hex now show the
  truncation only as `display`; full value is one click away.
- `PlacementOverview` cards render the full `dataset_id` (was truncated);
  `.cards code` got `overflow-wrap: anywhere` so the 64-char id wraps
  instead of clipping.

## Flagged, not implemented

| Item | Why deferred |
|------|--------------|
| URL/deep-linkable state (project/context/dataset in hash or query) | Workflow state spans many selects; a half-done URL sync is worse than none. Worth a dedicated round with a routing decision. |
| Plotly theme reacting to live `prefers-color-scheme` changes | `themedLayout()` reads the scheme at render; a media-query listener + `Plotly.react` re-theme is a small follow-up but out of "small diff" scope. |
| Loading skeletons on card grids | Errors + empty states now exist; skeletons are polish on top. |

## Verified clean

- Every mutating path is non-reentrant (round 6 `busy` flags intact).
- All selects render a placeholder option and handle empty lists.
- `ErrorBoundary` per-panel isolation works; `api.ts` error mapping intact.
- SVG sketch/wall tools remain fully keyboard-operable (round 3).
- `usePlot`/`Plotly.react` cleanup (`purge`) present; seq guards on all
  async loads (round 5) intact.

## Build

`npm ci` clean (node 20.19.0, npm run via `npm-cli.js` — the box's `npm`
shim is a broken bash script). `tsc --noEmit` clean, `vite build` clean.
No test runner exists in the frontend (`package.json` has only
dev/build/preview — round 3 item E); verification is type-check + build +
re-inspection.
