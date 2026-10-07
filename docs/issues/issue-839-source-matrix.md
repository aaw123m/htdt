# Issue #839 — Authoritative theater standards source matrix

REV64 #839 seals the research synthesis into a **source-matrix authority**:
a sealed, versioned record enumerating every external standards document
HTDT may cite, with exact edition identity, source status, the #839
`StandardsProfile` taxonomy class, the HTDT role, definability, and the
explicit rights/access/redistribution boundary.

The matrix is HTDT's statement of *which documents exist and what each may
be cited for*. A source that cannot lawfully or normatively produce an
evaluated criterion is recorded as such — never promoted into truth.

## Source matrix (`cad_standards_source_matrix.py`)

`StandardsSourceMatrix` is a sealed, content-addressed record
(`matrix_id`/`matrix_sha256` derive from the canonical semantic payload)
persisted to the append-only `cad_standards_source_matrices` table
(schema v94) and reachable via
`CadStandardsRepository.save_source_matrix` / `get_source_matrix` /
`get_source_matrix_version` / `list_source_matrices`.

Each `SourceMatrixEntry` carries:

- exact document identity — `name`, `publisher`, `document_version`,
  `checked_at` (ISO-8601 review date), and at least one `source_uris`;
- `precedence` per #839 §10: `project_selected_profile` >
  `first_party_standard` > `first_party_vendor_guidance` >
  `first_party_support_page` > `official_secondary` >
  `third_party_summary`;
- `profile_class` per #839 §11: `private_theater_recommended_practice`,
  `format_vendor_home_guidance`, `production_reference_layout`,
  `subjective_test_reference_room`, `av_system_measurement_standard`,
  `project_defined_profile`, `research_only_profile`;
- `htdt_role` — the matrix's role column (primary private-theater profile,
  format layout recommendation, vendor capability layout statement,
  conventional/immersive production reference, critical-listening
  reference, measurement/commissioning procedure, or supplementary);
- `source_status` per #839 §13.1: `executable`, `metadata_only`,
  `licensed_source_required`, `unsupported`, `source_conflict`,
  `no_authoritative_numeric_criteria`;
- `definability` — can research define this now: `definable`,
  `definable_bounded_to_source`, `definable_for_unknown`,
  `partially_licensed`, `not_definable`;
- `conformance_scope` — `private_home_conformance`,
  `vendor_home_guidance_only`, `non_home_reference`, or
  `not_a_conformance_source`;
- rights boundary — `normative_access`, `rights_class`,
  `redistribution_rights`; `public_webpage` access additionally pins a
  `page_identity` because an unversioned page is not a document revision;
- optional `conflicting_observations`, `related_standard_ids`, and
  `intended_profile_ids` cross-links.

`validate_source_matrix(matrix, profiles=..., gap_standard_ids=...)`
audits every cross-reference: intended profile ids must exist in the
emitted profiles and related standard ids in the gap matrix — a dangling
reference is a recorded error, never silently dropped.

## The built-in matrix (#839 §12 rows)

| Source | Role | Status | Definability |
|---|---|---|---|
| CEDIA/CTA RP22 v1.2 (Sept 2023) | primary private-theater profile | executable | definable |
| Dolby Atmos Home R3.1 (2018-12-13) | format layout recommendation | executable | definable_bounded_to_source |
| AURO-3D Home Rev.12 (2024-05-16) | format layout recommendation | executable | definable_bounded_to_source |
| DTS:X flexible-layout statement | vendor capability layout statement | no_authoritative_numeric_criteria | definable_for_unknown |
| ITU-R BS.775-4 (12/2022) | conventional multichannel reference | executable | definable_bounded_to_source |
| ITU-R BS.2051-3 (05/2022) | advanced immersive production reference | executable | definable_bounded_to_source |
| ITU-R BS.1116-3 (02/2015) | critical-listening test reference | executable | definable_bounded_to_source |
| AVIXA A102 | measurement/commissioning procedure | licensed_source_required | partially_licensed |
| AVIXA A103 | measurement/commissioning procedure | **source_conflict** | partially_licensed |
| AVIXA A104 | measurement/commissioning procedure | licensed_source_required | partially_licensed |
| AVIXA V202 | measurement/commissioning procedure | licensed_source_required | partially_licensed |
| AVIXA D402 | measurement/commissioning procedure | licensed_source_required | partially_licensed |

Decisions the matrix pins:

- **RP22 stays primary.** The only entry allowed the
  `primary_private_theater_profile` role; the matrix itself enforces at
  most one.
- **AURO Rev.12 is the default AURO source** for new mappings.
- **Dolby R3.1** is recorded with exact identity and scope — vendor home
  guidance, not a recommended practice.
- **DTS:X stays UNKNOWN.** The first-party statement is a flexible-layout
  vendor capability page; fixed numeric home-layout criteria remain
  `no_authoritative_numeric_criteria`, never inferred from third-party
  diagrams.
- **ITU sources are non-home references.** BS.775/BS.2051/BS.1116 are
  free and in force, but their conformance scope is `non_home_reference`
  — they inform production/test practice, never private-home conformance.
- **AVIXA metadata is not normative availability.** A102/A104/V202/D402
  are `licensed_source_required`: catalog metadata exists, the normative
  text requires a licensed copy — HTDT never evaluates against criteria
  it cannot cite.
- **Source conflicts are preserved, never silently resolved.** AVIXA A103
  is `source_conflict`: the detail page shows A103.01:2022 while the
  catalog shows A103.01:2023. The entry keeps both
  `SourceConflictObservation`s and its `document_version` is forbidden
  from matching either claim.

## Fail-closed guarantees

- An entry cannot exist without at least one pinned source URI, a checked
  date, and a coherent role ↔ taxonomy class ↔ conformance scope triple —
  a production reference can never claim private-home conformance.
- `executable` requires an identified access path and a rights class that
  lawfully covers derived criteria; `metadata_only` requires
  `public_metadata_only` rights; `licensed_source_required` and
  `no_authoritative_numeric_criteria` still require an identified
  first-party surface.
- `third_party_summary` precedence can never be executable — third-party
  summaries aid discovery but never stand as normative truth.
- `source_status` and `definability` are bound mechanically: an
  `unsupported` source cannot claim `definable`, and a
  `no_authoritative_numeric_criteria` source carries only
  `definable_for_unknown`.
- `source_conflict` requires at least two distinct recorded claims, and
  `document_version` may not equal any conflicting claim — the matrix
  cannot silently pick a revision.
- Byte-level tampering with the stored payload fails seal verification
  on read; the version string is immutable once saved.

## User-facing surface

`_SOURCE_MATRIX_STATUS_LABELS` / `_SOURCE_MATRIX_ROLE_LABELS` /
`_SOURCE_MATRIX_CLASS_LABELS` and `source_entry_line()` in
`measurement_evidence_display.py` give JA labels for every state — e.g.
`source_conflict` renders as 「出所間の不一致（版を仮定しない）」.

## Verification

`backend/tests/test_issue_839_source_matrix.py` pins the built-in matrix
row-for-row, every fail-closed validator, determinism of the sealed
identity, the repository round-trip + byte tamper, and the JA label
surface.
