# StandardsProfile authority

> Issue #170 / authority version `standards-profile-1` + `standards-evaluation-1`
>
> This document defines HTDT's standards/layout criterion authority. It does not turn a
> standards document into physical truth, an acoustic prediction model, or an optimization
> score.

## Authority boundary

`StandardsProfile` is immutable and versioned. Each criterion carries its own source
publisher, document title/version, reference, quantity, unit, applicable domain, required
inputs/capabilities, evidence requirement, comparison operator, boundary inclusivity, and
numeric angle semantics.

## Source provenance (Issue #418)

Citation text alone is not treated as proof that a published criterion's
threshold/rule came from the cited document. Published criteria are bound to
exact, retained source authorities:

- `StandardsSourceAuthority` is a content-addressed record of one source
  document: publisher, document title/version, optional verified
  `document_sha256`, optional `source_uri`, the extraction/normalization
  version, and a set of `CriterionSourceExtraction` records. Its
  `authority_id` (`standards-source-authority:<sha256>`) and
  `semantic_hash_sha256` are derived from the full semantic payload, so any
  change to the document identity or to a recorded extraction is a different
  authority.
- Each `CriterionSourceExtraction` binds an `extraction_id`, the exact
  section/table `reference`, a `content_kind` (`normative`, `guidance`, or
  `policy_transform`), and the normalized structured data — `quantity`,
  `unit`, and the exact `CriterionRule` — that a published criterion must
  reproduce. `excerpt` optionally retains verbatim source text where licensing
  permits; built-in profiles ship normalized data only.
- `CriterionSource.authority_ref` is a typed `ExactExternalAuthorityRef`
  (authority id + version + semantic hash) and must be declared together with
  `extraction_id`. Both are part of the criterion's semantic identity and
  therefore of `profile_semantic_hash`.

`CadStandardsRepository.save_profile()` resolves every declared authority ref
at the persistence boundary — against the injected `source_authority_resolver`
first, then the retained `cad_standards_source_authorities` store — and
verifies that the resolved authority exactly reproduces the criterion's
citation identity, extraction id, reference, content kind, quantity, unit, and
rule. A resolved authority is pinned into the retained store so historical
published profile versions remain auditable after external resolvers change.
Fabricated thresholds, dangling refs/extraction ids, and citations of another
document/version are rejected. `user_defined` profiles remain functional
without authority bindings; when they do declare a ref, it is held to the same
exact-match contract.

Criterion observations are held to the same exact-authority contract (#410):
every `CriterionEvidenceRef` is a typed reference whose `kind` selects a
registered resolver and whose `evidence_id`/`evidence_sha256` pin one exact
authority record. `CadStandardsRepository` re-resolves every ref at save and on
every authoritative read (`get_evaluation`,
`list_evaluations_for_scene`): the resolved evidence must prove it belongs to
the exact SceneRevision/SystemVariant/entities of the evaluation target, and
the claimed observed value, unit, evidence basis, provided inputs, and
capabilities must match the canonical projection the evidence attests. Only
then is `evaluate_standards_profile` replayed and required to reproduce the
persisted record exactly; any divergence — fabricated refs, foreign-scope
evidence, inflated inputs/capabilities, or coherently rehashed result edits —
fails closed. The built-in `standards_manual_observation` kind resolves against
`StandardsObservationAuthority` records retained via
`save_observation_authority`; richer evidence sources register resolvers
through `CadStandardsRepository(evidence_resolvers=...)`. A kind without a
registered resolver is never evidence.

`cad_standards_layout_observation.py` is the declared scene-layout derivation
provider: for each criterion whose quantity is honestly computable from the
exact evaluation target's geometry — seat `acoustic_reference_position`,
speaker channel roles, room footprint — it derives the value, retains it as a
`StandardsObservationAuthority` (`predicted` basis, `scene-layout-derivation-v1`
method, exact SceneRevision/SystemVariant/entity binding), and feeds the
evaluator an explicit `CriterionObservation`. It currently derives the RP22
P1 listener-boundary distance, the four Dolby 5.1.2 role azimuths, the P5
adjacent-surround horizontal angle (azimuth-consecutive pairs), and the P9
same-side adjacent upper vertical angle. Quantities without an honest layout
source — recommended-zone membership, upfiring rendering mode, wide/AURO
layer membership, and every SPL/headroom input — are never derived and stay
`UNKNOWN`.

The built-in authorities live in `backend/src/htdt/cad_standards_authorities.py`;
the built-in profiles in `cad_standards_profiles.py` derive their citation text,
quantity, unit, and rule directly from the retained extraction records so a
profile cannot drift from the claimed authority.


`StandardsEvaluation` binds the exact profile semantic hash to one exact
`SceneRevision` and, when applicable, one exact `SystemVariant` semantic hash and an
explicit set of target entity IDs. Each `CriterionObservation` may additionally bind the
exact entity subset that produced that criterion value; those entity IDs must be a subset
of the evaluation target. The repository re-validates those bindings before persistence. A newer profile creates a new evaluation linked by `reevaluation_of_id`; it
never rewrites the historical profile or evaluation.

The evaluator consumes explicit observations. It does **not** derive a missing physical
quantity, infer a missing capability, or invent a tolerance. Missing required
input/capability/evidence is `UNKNOWN`. A domain that does not apply is
`NOT_APPLICABLE`. Those states are not aliases for `FAIL`.

There is intentionally no aggregate compliance score. Criterion results are independent
records:

- `PASS`: the supplied observation satisfies the exact encoded rule.
- `FAIL`: the supplied observation does not satisfy the exact encoded rule.
- `UNKNOWN`: required input, capability, evidence, unit, or observed value is not
  sufficient to make the comparison.
- `NOT_APPLICABLE`: the criterion's declared domain does not apply, or the caller
  explicitly marks the criterion not applicable.

`predicted` and `measured` evidence remain distinct in the result. A predicted
`PASS` is not renamed or promoted to a measurement-verified `PASS`.

## Source matrix (Issue #839)

`cad_standards_source_matrix.py` seals the research-level question of *which*
external documents may be cited at all. The `StandardsSourceMatrix` record
(schema v94) enumerates every source with exact edition identity, a #839
`StandardsProfile` taxonomy class, the HTDT role it may play, its
`source_status` (`executable` / `metadata_only` /
`licensed_source_required` / `unsupported` / `source_conflict` /
`no_authoritative_numeric_criteria`), definability, and an explicit
rights/access/redistribution boundary. See
[docs/issues/issue-839-source-matrix.md](issues/issue-839-source-matrix.md).

The pinned rows keep CEDIA/CTA-RP22 v1.2 as the single primary
private-theater profile, AURO Rev.12 as the default AURO source, Dolby R3.1
as vendor home guidance, DTS:X as `no_authoritative_numeric_criteria`
(flexible-layout vendor statement — never fixed numeric criteria), the three
ITU-R references as `non_home_reference`, the AVIXA documents as
`licensed_source_required` metadata, and AVIXA A103 as a preserved
`source_conflict`. Third-party summaries can never be `executable`.

## Explicit hard-constraint interface

Compliance is advisory by default. A `FAIL` does not remove a candidate or mutate an
O30/O40/O90/O100 objective. Hard-constraint selection is not stored in, and does not
change, the `StandardsEvaluation` identity. Only criterion IDs explicitly supplied as
`selected_criterion_ids` to `explicit_hard_constraint_gate()` participate in the
downstream gate.

For a selected hard criterion, `FAIL` and `UNKNOWN` block downstream use. An
unselected `FAIL` never blocks. `NOT_APPLICABLE` does not block. This is a separate
gate; it is not a hidden Pareto objective or combined score.

## Deterministic numeric semantics

- JSON identity is canonicalized with sorted keys, UTF-8, compact separators, and
  non-finite JSON numbers forbidden.
- Profile semantic identity covers the exact criterion definitions and source provenance.
- Evaluation identity covers the profile ID/version/hash, exact scene/variant/entity
  binding, observations/evidence, and optional re-evaluation lineage. Downstream
  hard-constraint selection is deliberately excluded from evaluation identity.
- Floating-point comparison uses the decimal string representation of the supplied finite
  number. No epsilon is introduced.
- Every lower/upper boundary records inclusive/exclusive semantics.
- Angle rules explicitly select no wrap, signed `[-180, 180)`, or unsigned
  `[0, 360)` normalization. Absolute-angle comparison is explicit.
- `created_at_utc` is record metadata, not evaluation semantics; repeating the exact
  profile + target + evidence yields the same deterministic evaluation ID/hash.

## Built-in public-source profiles

The built-in data live in `backend/src/htdt/cad_standards_profiles.py`; the evaluator is
separate in `cad_standards.py`. Built-ins are deliberately incomplete where a public
source does not provide an explicit pass/fail boundary that HTDT can encode without
inventing one.

### CEDIA/CTA-RP22 v1.2 — spatial/layout subset

Source:

- CEDIA / Consumer Technology Association, **CEDIA/CTA-RP22 Recommended Practice for
  Immersive Audio Design**, v1.2, September 2023.
- Public source:
  <https://cedia.org/site/assets/files/6057/cedia-cta_rp22_v1_2_sept_2023.pdf>

HTDT provides separate profile identities for Levels 1–4:
`cedia-cta-rp22-spatial-level-{1..4}`, version `1.2-2023-09-prov1` (the `-prov1`
suffix marks the Issue #418 source-authority provenance release; earlier
published versions remain immutable history).

Encoded criteria:

| Criterion ID | RP22 reference | Encoded rule |
|---|---|---|
| `rp22.p01.listener-boundary-distance` | Appendix A Parameter 1; §4.1.4 | listener-to-boundary distance strictly > 0.5 / 0.8 / 1.2 / 1.5 m for Levels 1/2/3/4 |
| `rp22.p03.screen-speakers-outside-zone-count` | Appendix A Parameter 3; §5.5.4 | 0 screen-wall speakers outside recommended zones |
| `rp22.p05.max-adjacent-surround-horizontal-angle` | Appendix A Parameter 5; §5.6.2.1 | max 80° / 60° / 50° for Levels 2/3/4; omitted for Level 1 where Appendix A is N/A |
| `rp22.p07.wide-horizontal-median-deviation` | Appendix A Parameter 7; §5.7 | max absolute deviation 10° / 7° / 5° / 2° for Levels 1/2/3/4 |
| `rp22.p08.upfiring-elevation-speakers-prohibited` | Appendix A Parameter 8; §5.8.2 | Levels 3/4 require `uses_upfiring_elevation_speakers == false`; Levels 1/2 are omitted because “allowed” is not a requirement to use them |
| `rp22.p09.max-adjacent-upper-vertical-angle` | Appendix A Parameter 9; §5.8.2 | max 80° / 60° / 50° for Levels 2/3/4; omitted for Level 1 where Appendix A is N/A |
| `rp22.p11.surround-wide-upper-outside-zone-count` | Appendix A Parameter 11; §5.9.3 | 0 speakers outside recommended zones for Levels 2/3/4; omitted for Level 1 where Appendix A is N/A |

This is explicitly a **spatial/layout subset**, not an RP22 room certification. RP22
contains additional criteria including acoustic performance/SPL-related requirements.
Issue #170 does not implement O100D coverage/SPL/headroom objectives, and this profile
must not imply those unimplemented criteria passed. Criteria that depend on recommended
zones also require the explicit `rp22-recommended-zone-evaluation-v1` capability; without
it they evaluate to `UNKNOWN`.

The following RP22 Appendix A parameters are deliberately **not encoded** in this initial
profile:

| Parameter | Reason not encoded in Issue #170 |
|---|---|
| P2 decoder/renderer capability + discrete speaker count | Levels 3/4 have an explicit format-dependent 15/13 rule; HTDT does not collapse that conditional renderer/format authority into one invented threshold |
| P4 screen-speaker SPL difference | acoustic prediction/result authority; not a layout-only criterion |
| P6 surround-speaker SPL difference | acoustic prediction/result authority; not a layout-only criterion |
| P10 upper-speaker SPL difference | acoustic prediction/result authority; not a layout-only criterion |
| P12–P14 SPL capability/headroom | explicitly deferred with O100D SPL/headroom scope |
| P15 background noise floor | requires measured/acquisition-quality evidence authority rather than layout inference |
| P16–P17 seat-to-seat frequency-response variance | requires acoustic prediction or measurement evidence authority |
| P18–P20 bass extension / low-frequency response / seat variance | requires acoustic prediction or measurement evidence authority |
| P21 early-reflection level | requires acoustic prediction or measurement evidence authority |

Those omissions remain `UNKNOWN`/unsupported at the profile-data level; they are not
silently treated as passing criteria and are not replaced by an aggregate compliance score.

### Dolby Atmos Home Theater 5.1.2 layout guidance

Source:

- Dolby Laboratories, **Dolby Atmos Home Theater Installation Guidelines**, R3.1,
  13 December 2018.
- Public source:
  <https://www.dolby.com/siteassets/technologies/dolby-atmos/atmos-installation-guidelines-121318_r3.1.pdf>
- Encoded reference: Figure 12, page 28, 5.1.2 speaker placement.

Profile identity: `dolby-atmos-home-5.1.2-layout`, version
`r3.1-2018-12-13-prov1`.

Encoded source ranges are 22°–30° for front left/right and 90°–110° for surround
left/right. HTDT maps them into its explicit signed azimuth convention: 0° points toward
the screen/front, positive angles point toward +X/right, negative angles toward -X/left,
and values normalize to [-180°, 180°). Therefore FL is -30°..-22°, FR is +22°..+30°,
SL is -110°..-90°, and SR is +90°..+110°. Endpoints are inclusive because the published
figure presents those endpoints as the placement range. This is a coordinate mapping, not
an added tolerance.

The profile intentionally does not infer top-speaker, elevation, room, or performance
criteria not encoded by this profile.

### AURO-3D Home Theater Setup Rev.12

Source:

- NEWAURO BV, **AURO-3D Home Theater Setup — Installation Guidelines**, Rev. 12,
  16 May 2024.
- Public source:
  <https://www.auro-3d.com/wp-content/uploads/2024/05/Auro-3D-Home-Theater-Setup-Guidelines-v12-20240516.pdf>
- Encoded references: §3.3.1.1 (pages 23–24) and §3.3.2 Table 3 “Normative Speaker Positions” (page 26).

Profile identity: `auro3d-home-layout`, version `rev12-2024-05-16-prov1`.

Encoded criteria are limited to unambiguous public min/max statements:

| Criterion ID | Source rule encoded |
|---|---|
| `auro.v12.lower-layer-max-elevation` | lower-layer elevation maximum 10°; no unstated lower bound |
| `auro.v12.height-layer-elevation` | Height-layer elevation 25°–40° |
| `auro.v12.top-speaker-elevation` | Top speaker elevation 65°–100° |
| `auro.v12.surround-height-opening-angle` | Surround-to-Height opening angle at least 25° |
| `auro.v12.screen-height-opening-angle` | Height screen-channel opening angle at least 22° |

The Rev.12 table also publishes horizontal azimuth rows. HTDT does not encode those rows
in this initial profile because the published Height Right row contains an apparent sign
inconsistency in its maximum azimuth entry. The source is not silently corrected.

### DTS:X

No built-in DTS:X criterion is included in Issue #170 because an official public source
with a sufficiently explicit criterion boundary/provenance was not established for this
slice.

Users can represent an independently sourced criterion through
`build_user_standards_profile()`; every such criterion still requires explicit source
metadata, version, reference, unit, rule, evidence requirements, and capability
prerequisites.

## Persistence and re-evaluation

`CadStandardsRepository` stores profiles and evaluations append-only in the native CAD
SQLite database, after the existing native schema compatibility gate. It reuses
`SceneRepository` and `CadSystemVariantRepository`; it does not create a second
scene/layout truth.

Persistence checks include:

1. the exact profile ID/version/hash exists;
2. the evaluation re-generates identically through the current declared evaluator
   authority;
3. the exact SceneRevision ID/document/content hash exists;
4. an optional SystemVariant ID/hash belongs to that exact baseline SceneRevision;
5. evaluation target entity IDs exist in the exact scene or materialized variant;
6. each criterion observation's entity IDs are an exact subset of that target binding;
7. an explicit re-evaluation points to a persisted historical evaluation with the exact
   same target.

Reopening reads the serialized immutable payload and re-runs its model hash validation.
A duplicate semantic evaluation may carry a later attempted timestamp, but the repository
returns the original stored record because timestamp metadata is not part of the
deterministic criterion result identity.

## S130 workflow-first software integration

S130 exposes the existing authority through the native workflow without creating a second
compliance model.

- **Room > スピーカー・座席**: a StandardsProfile panel shares the existing right-side
  placement context with O100. The user selects an exact profile/version and exact current
  SceneRevision or SystemVariant target. Criterion rows show Japanese status text, observed
  value, encoded requirement, predicted/measured evidence basis, and missing
  input/capability reason.
- **Optimize > 比較**: the same persisted evaluations are shown as a criterion-by-criterion
  matrix across the current room and SystemVariants. There is no aggregate score, winner,
  automatic recommendation, or implicit Pareto objective.
- **Explicit hard constraint**: only checked criterion IDs are passed to
  explicit_hard_constraint_gate(). A selected FAIL blocks, a selected UNKNOWN
  blocks fail-closed, an unselected FAIL remains advisory evidence, and
  NOT_APPLICABLE does not block.
- **History**: profile versions remain selectable independently. Re-evaluation under a newer
  version creates a new immutable evaluation linked through reevaluation_of_id; the
  earlier evaluation is preserved.
- **Advanced provenance**: exact SceneRevision, optional SystemVariant, profile
  identity/version/hash, evaluation identity/timestamp/version, criterion source/reference,
  criterion hash, and evidence identity are available through progressive disclosure rather
  than normal-view hash dumps.
- Built-in profiles are registered idempotently through CadStandardsRepository; persisted
  user-defined profiles are listed by the same selector.

S130 software acceptance is headless/offscreen Qt plus backend authority tests. Windows
DPI/font/mouse/3D screenshot and first-use visual acceptance remain **UX160 pending**.
## Deferred / out of scope

Issue #170 does not implement:

- overall RP22 certification or a hidden total compliance score;
- O100D coverage, SPL, headroom, worst-seat, or other acoustic objectives;
- automatic conversion of criterion results into Pareto objectives;
- automatic candidate deletion based on advisory `FAIL`;
- Windows owned-PC visual/first-use acceptance (UX160);
- private/commercial document content that is not available in the cited public source;
- inferred DTS:X tolerances or silent repair of ambiguous/inconsistent source data;
- physical geometry/measurement derivation engines beyond the scene-layout
  lane above. Providers must declare their own input/evidence capability
  before a criterion can move from `UNKNOWN`.
