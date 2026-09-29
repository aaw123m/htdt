# Round 14 — Print / paginated-output fidelity

Scope: the printed/PDF'd report is the deliverable that reaches the
customer. Does the `@media print` output match the screen content —
no clipped columns, no missing JP glyphs, no orphaned headers, honest
charts — for every printable artifact?

## Print surfaces (enumerated)

HTDT has **no Qt print path** (`QPrinter`/`QPrintPreview` do not exist in
the codebase). Every printable artifact is a self-contained HTML report
carrying its own `@media print` stylesheet, printed by whatever HTML
engine the operator uses (browser print dialog → PDF or paper):

| Surface | Source | Print CSS |
|---|---|---|
| Comparison report (`comparison_report.html`) | `report.py::render_report_html` | yes (since round 10) |
| Installation report (`installation_report.html`) | `report.py::render_installation_report_html` | yes |
| Analysis export (`analysis.html`) | `analysis_export.py::render_analysis_html` | yes |
| Handoff CSVs / manifest | `installation_handoff.py` | n/a (not paginated) |
| Drawing sets / field labels (`to_svg()` mm sheets) | `cad_drawing_set.py`, `cad_field_labels.py` | persisted in SQLite only — no file export path to print |
| Commissioning report | — | **not implemented** (`deliverables_catalog.py` marks it 未実装) |

## Method

No GitHub Actions — all verification local on Windows. Real reports were
built through the real builders (comparison en/ja, installation
small/big, analysis, empty variants, plus a fixture seeded with a
200-char unbreakable token and a 64-char sha256-like pin), then printed
to PDF with headless Chrome `--print-to-pdf` (which applies
`@media print` faithfully), then inspected with PyMuPDF: page counts,
embedded font list, text-extraction probes for JP strings and known
numbers, bounding-box overflow scan, and 110-dpi PNG renders reviewed
visually.

## Findings — fixed

1. **Analysis chart y-axis labels clipped off the SVG's left edge.**
   `_plot_svg` rendered tick labels with the `.12g` data formatter
   (`86.9997454097`, 13 chars) right-anchored at `x=pad-6` inside a
   720×360 viewBox — anything past ~7 chars extends beyond `x=0` and
   clips, in print *and* on screen. Verified: the PDF span contained
   only `7454097`. Fix: dedicated `_axis_number` (`.4g`) for tick text
   + pad 48→64. Verified post-fix: `87`/`69`/`0.45`/`0.21` fully inside
   the plot edge.

2. **Comparison spec `<pre>` clipped long JSON lines.** The
   machine-readable spec block had no wrap rule; a long unbroken line
   printed truncated at the page edge. Fix:
   `pre{white-space:pre-wrap;overflow-wrap:anywhere}` (the installation
   report already had this rule — the comparison report did not).
   Verified: the seeded 200-char token now wraps inside the block.

3. **Unbreakable strings overflowed table cells in all three reports.**
   64-char sha256 source columns, long `entity_id`s, and hex tokens had
   no emergency break rule. Fix: `td{overflow-wrap:anywhere}` +
   `code{overflow-wrap:anywhere}` + `p,li{overflow-wrap:break-word}`;
   headers get `th{overflow-wrap:break-word}` so `anywhere` doesn't
   squeeze `th` min-content and mid-word-break headers
   ("Ro le", "Clearan ce basis" — caught and corrected in review).

4. **Printed entity table dropped `name` — preview/CSV parity gap.**
   The installation entity table printed `entity_id` + kind + 8
   numeric/enum columns but no display name, while both the handoff
   preview dialog (`handoff_preview_text`) and the entity CSV include
   `name`. An installer matching printed rows to the preview had no
   shared label. Fix: `name` rendered under the `entity_id` cell
   (muted, same pattern as other secondary lines). Verified: JP names
   (`吸音パネル 09（側壁・後部）`) print and extract.

5. **Page-break hygiene.** `h1/h2` could print orphaned at a page
   bottom and table rows could split mid-row (an entity row's name
   fragment carried to the next page). Fix: `h1,h2{break-after:avoid}`
   + `tr{break-inside:avoid}` in the print block of all three reports.
   Verified: rows now break at boundaries.

## Verified OK

- **JP glyphs**: comparison_ja embeds YuGothic/MS-Gothic in the PDF,
  all probe strings extract; JP content in `lang="en"` reports
  (installation/analysis) resolves via YuGothic/YaHei fallback — no
  tofu anywhere.
- **Charts**: inline SVGs print as vectors with axes, log-x ticks, and
  legends intact; `max-height:16cm` + `break-inside:avoid` keeps a
  chart on one page.
- **Content parity**: printed numbers are the same strings the HTML
  emits (same `_format_number`/`_metric` path — no second formatting
  code path for print, so no rounding drift is possible).
- **Edge cases**: empty analysis bundle prints an honest 1-pager;
  the 30-entity installation report paginates to 11 pages with no
  overflow (bounding-box scan: 0 items outside page bounds in every
  PDF).
- **`details` blocks** are print-hidden by design (interactive-only
  payload viewers) — the semantic payload remains in the page source.

## Observations — deferred

- `_plot_svg` legend labels stack at fixed top-left positions inside
  the plot area and overlap traces when several series share a unit —
  pre-existing cosmetic issue, same on screen and in print.
- `InstallationDimensionPoint` has no `name` field, so the dimension
  table can't show entity names without a semantic-model change —
  out of scope for a print fix.
- Analysis/installation documents are `lang="en"`; JP names rely on
  font fallback rather than a declared CJK stack — renders correctly
  on Windows, but a declared fallback stack would be safer on
  font-poor systems.
- Axis ticks are `.4g` while CSV/JSON keep `.12g` — deliberate
  display-vs-data precision split; the chart is a visual aid, the
  payload is the data.
- The 10-column installation table stays portrait — readable after the
  wrap fix but narrow; a landscape `@page` hint or column pruning is a
  design choice, not a bug fix.

## Files changed

- `backend/src/htdt/report.py` — comparison + installation print CSS
  (pre/code/td/th/p-li wrap rules; `h1,h2{break-after:avoid}`;
  `tr{break-inside:avoid}`); entity table gains `name`.
- `backend/src/htdt/analysis_export.py` — `_axis_number` (`.4g`) for
  tick labels, plot pad 48→64, matching print-CSS wrap/break rules.
