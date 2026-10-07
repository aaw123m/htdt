# Issue #805 — Standards-aware coverage beyond the spatial/layout subset

REV63 #805 turns `StandardsProfile` into a complete, versioned
design-decision surface: a sealed **gap-matrix authority** enumerates every
criterion HTDT intends to cover for each built-in standard, and built-in
coverage is extended everywhere a public authoritative boundary exists.
Unsupported or unpublished criteria remain UNKNOWN — never inferred truth.

## Gap matrix (`cad_standards_gap_matrix.py`)

`StandardsGapMatrix` is a sealed, content-addressed record
(`matrix_id`/`matrix_sha256` derive from the semantic payload) persisted to
the append-only `cad_standards_gap_matrices` table (schema v92) and
reachable via `CadStandardsRepository.save_gap_matrix` /
`get_gap_matrix` / `get_gap_matrix_version` / `list_gap_matrices`.

Each `GapMatrixCriterion` carries:

- mandatory `source` (publisher / document_title / document_version /
  reference) — an entry without source and revision cannot exist;
- `applicable_domains` and optional `performance_levels` scoping;
- `state`: `implemented`, `evidence_missing`, or `unsupported`;
- `state_reason` and, for `evidence_missing`, the named missing evidence
  authority (`missing_evidence`);
- for `implemented`: `implemented` refs pinning every emitting
  `(profile_id, profile_version, criterion_id, criterion_sha256)`.

`validate_gap_matrix(matrix, profiles=...)` replays every implemented ref
against the emitted profiles and requires every encoded criterion to be
declared — the matrix can neither claim coverage that does not exist nor
silently drop a shipped criterion. `builtin_standards_gap_matrix()`
derives the implemented entries from the emitted revisions and fails
closed if the record does not describe them.

## Coverage by standard

| Standard | implemented | evidence_missing | unsupported | Total |
|---|---|---|---|---|
| CEDIA/CTA RP22 v1.2 | 20 | 1 (P2) | 0 | 21 |
| Dolby Atmos Home R3.1 (5.1.2) | 5 | 2 | 1 | 8 |
| AURO-3D Home Rev.12 | 5 | 0 | 1 | 6 |
| DTS:X | 0 | 0 | 3 | 3 |
| Screen/viewing (THX, SMPTE EG 18) | 0 | 0 | 2 | 2 |

### RP22 v1.2 — all 21 Appendix A parameters enumerated

Two profile families per level now encode the parameters:

- `cedia-cta-rp22-spatial-level-{1..4}` `1.2-2023-09-prov1` — unchanged;
  P1, P3, P7 at all levels, P5/P9/P11 from L2, P8 from L3.
- `cedia-cta-rp22-performance-level-{1..4}` `1.2-2023-09-prov1` — new;
  P4/P6/P10 (SPL difference), P12/P13/P14 (SPL capability at the RSP),
  P15 (background noise, NCB), P16/P17 (seat-to-seat response variance),
  P18 (bass extension), P19 (LF response vs target), P20 (seat-to-seat LF
  variance), P21 (early-reflection level).

Measured-only criteria (P15–P17, P19–P21) declare
`evidence_requirement='measured'`: a predicted-basis observation always
evaluates UNKNOWN (`measurement_evidence_required`). SPL-capability
criteria (P12–P14) consume exact `CriterionEvidenceRef`s — without a
resolved evidence authority they stay UNKNOWN.

**evidence_missing — `rp22.p02.discrete-rendered-speaker-count`**: the
published L3/L4 boundary is format-conditional (15 discrete rendered
speakers, 13 for an Auro-3D design). Selecting the boundary requires a
declared immersive-format design intent HTDT does not retain; the
criterion stays UNKNOWN rather than apply a threshold that changes
meaning with context.

### Dolby Atmos Home R3.1 — `dolby-atmos-home-5.1.2-layout`

- `r3.1-2018-12-13-prov1` — sealed, unchanged (4 azimuth criteria).
- `r3.1-2018-12-13-prov2` — adds
  `dolby.5.1.2.top-middle-overhead-elevation` (65–100°, 80° recommended;
  Figure 11, verified against the published R3.1 PDF). The layout
  observation lane derives it from TML/TMR speaker positions.
- evidence_missing: `dolby.5.1.2.atmos-enabled-speaker-mode` (needs a
  declared rendering-mode authority — the up-firing alternative layout is
  never judged against the overhead-speaker window),
  `dolby.5.1.2.overhead-speaker-height-ratio` (needs a declared
  listener-level reference height).
- unsupported: `dolby.5.1.2.center-channel-azimuth` — the source publishes
  a nominal 0° placement with no tolerance; a zero-tolerance criterion
  would fabricate failures.

### AURO-3D Rev.12 — `auro3d-home-layout`

5 implemented elevation/opening criteria, unchanged. unsupported:
`auro.v12.horizontal-azimuth-bounds` — Table 3 carries an apparent sign
inconsistency for the Height Right azimuth rows; HTDT does not silently
repair source data.

### DTS:X — `dts-x-home`

No profile exists. Three intended criteria (listener-level layout,
upper-layer layout, renderer capability) are `unsupported`: DTS/Xperi
publishes no authoritative public criteria; third-party diagrams are
descriptive, not conformance boundaries. Evaluation remains UNKNOWN by
design.

### Screen/viewing — `theater-viewing-screen`

`viewing.horizontal-viewing-angle` (THX) and
`viewing.screen-size-distance-ratio` (SMPTE EG 18) are `unsupported`: both
authoritative texts are license-gated, so no citable public boundary
exists.

## Versioning rule

New criteria enter only a **new profile revision** — prov2 sits beside
prov1, and both are emitted by `builtin_standards_profiles()` and retained
by `builtin_standards_source_authorities()`. Existing evaluations pin
`(profile_id, profile_version, profile_semantic_hash)`; a prov1 evaluation
is byte-identical after prov2 ships. The same holds for source
authorities: the prov1 Dolby authority record is sealed, so historical
extractions keep resolving.

## User-facing surface

`_GAP_STATE_LABELS` / `gap_state_label()` / `gap_entry_line()` in
`measurement_evidence_display.py` give JA labels and one-line explanations
(state + citation + reason) for matrix entries. Verdict reason codes
(`PASS`/`FAIL`/`UNKNOWN`/`NOT_APPLICABLE` + reason) are unchanged and
already JA-labelled in `standards_workspace`.

## Fail-closed guarantees

- A criterion without source/revision cannot exist (pydantic + validator).
- `implemented` requires pinned revisions; `evidence_missing` requires a
  named missing-evidence authority; `unsupported`/`evidence_missing`
  carry no evaluation semantics.
- The matrix records coverage state only — it is never a score and never
  promotes an unencoded criterion into an evaluated one.
- Byte-level tampering with the stored payload fails seal verification on
  read.
