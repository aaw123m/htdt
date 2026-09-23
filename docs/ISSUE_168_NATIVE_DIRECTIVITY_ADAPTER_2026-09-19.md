# Issue #168 — Native directivity adapter investigation (2026-09-19)

## Outcome

**BLOCKED — no native CLF or CF2 adapter is promoted to SUPPORTED in this slice.**

The requested gate was intentionally fail-closed: one real native format may be supported only when both its exact format/version semantics and a legally reproducible CI fixture can be established without inference. The investigation established useful CLF/CF2 facts, but not an authoritative, lossless mapping from the native balloon coordinate/order semantics into the current HTDT `DirectivityDataset` authority. Implementing a parser anyway would require assuming coordinate/arc semantics, performing unapproved resampling or symmetry expansion, or undocumented binary decoding.

RDC usage: **0**.

## Selected format

None.

CLF text was investigated first as the stronger candidate because authoritative vendor/tool documentation exposes the authoring text form, while CF2 is the secure binary distribution form. The available material is sufficient to distinguish the formats, versions, resolution, and phase history, but not sufficient to authorize a lossless HTDT normalization under the existing dataset coordinate/grid contract.

## Selected version

None.

Relevant version facts established during research:

- CLF Group describes CLF1 as 10 degree / 1-octave and CLF2 as 5 degree / 1/3-octave authoring data, with the reader producing secure CF1/CF2 binary distribution files.
- CLF Group and CATT document that **CLF2 v2**, released 2011-09-08, introduced optional phase, filters, and multipart files. Therefore phase must not be inferred for earlier CLF2 material.
- Audiomatica's CLIO documentation demonstrates a CLF2 text export with `<CLF2>`, `<VERSION> 1`, `<RADIATION> <fullsphere>`, `<BALLOON-SYMMETRY> <none>`, `<BALLOON-ARC-ORDER>`, `<BALLOON-REF> <relative>`, and `<BAND>` blocks.

## Authoritative format sources

Primary / authoritative sources used:

1. CLF Group, *SAC Newsletter / CLF format overview*:
   https://www.clfgroup.org/clf_SAC_Newsetter_fall_2004.pdf
   - CLF authoring data is TAB-delimited text.
   - CLF1 and CLF2 use different angular/frequency resolutions.
   - The reader writes secure binary CF1/CF2 distribution files.

2. CLF Group, *CLF Downloads*:
   https://www.clfgroup.org/clfdocuments.htm
   - Official download surface for CLF tooling/documentation.
   - Search-indexed CLF Group material identifies CLF2 v2 support for optional phase, filters, and multipart files.
   - The site was not reliably retrievable from the CI/research environment, so an exact complete authoring grammar could not be admitted from this source.

3. CLF Group, *FAQ*:
   https://www.clfgroup.org/faq.htm
   - Search-indexed official material states CLF2 v2 supports optional phase and related extensions.

4. CATT-Acoustic, *What's new?*:
   https://www.catt.se/whats_new.htm
   - Records CLF2 v2 release on 2011-09-08 and its introduction of phase, optional filters, and multipart descriptions.

5. Audiomatica, *CLIO Directivity / CLF export application note*:
   https://www.audiomatica.com/wp/wp-content/uploads/appnote_002.pdf
   - Vendor documentation showing a real CLF2 version-1 text export.
   - Documents its measurement coordinate system as polar angle from the loudspeaker front axis plus azimuth around that axis.
   - Documents regular 5 degree full-sphere measurement grids and the CLF authoring/export workflow.

6. ODEON, *Odeon 15 Features — Import of source directivity files in the CLF text format*:
   https://odeon.dk/product/whatsnew/previous-versions/odeon-15-features/
   - Documents import of the TAB-separated CLF authoring text used to generate CF1/CF2 distribution files.

7. ODEON, *Directivity files*:
   https://odeon.dk/downloads/directivity-files/
   - Describes CLF directivity as a spherical polar-coordinate grid and the 10 degree CF1 / 5 degree CF2 discretization.

A reverse-engineered third-party CLF grammar was found during research but was deliberately **not** treated as specification authority.

## Why CLF text is blocked

### Native coordinate semantics do not match the current HTDT dataset authority

Authoritative vendor documentation does not give this slice one unambiguous, version-specific mapping from every CLF text balloon ordering convention into HTDT coordinates. Audiomatica documents its CLF export workflow from measurements expressed as a **front-axis polar angle** plus **azimuth around the front axis**, while ODEON describes the CLF balloon as a spherical polar-coordinate grid. The available official CLF material was not retrievable with the complete arc-order/axis grammar needed to prove the exact transform.

Current HTDT `DirectivityDataset` stores and downstream evaluates a rectangular product grid of:

- source-relative horizontal / azimuth angle,
- source-relative vertical / elevation angle,
- with an acoustic reference axis at horizontal=0, vertical=0,
- and requires every frequency × horizontal × vertical cell exactly once.

Without the authoritative CLF axis, arc-order, symmetry, and pole rules, accepting a native balloon as the HTDT grid would be an assumption. Depending on the native convention and symmetry mode, normalization could require one or more of:

- angular reprojection/resampling,
- interpolation onto a new grid,
- symmetry expansion,
- direction deduplication at spherical poles,
- or a new native spherical-coordinate authority consumed by downstream evaluators.

All of those are outside this slice, and several are explicitly forbidden to infer. In particular, this slice does not relabel or rotate native angles without an exact versioned authority.

### Exact grammar / required-sample authority is incomplete in the available official material

The available primary/authoritative documents demonstrate real fields and high-level resolution/version behavior, but the complete official authoring grammar was not reliably retrievable. In particular, this slice does not have sufficient primary authority to make fail-closed decisions for every required/optional field, ordering rule, symmetry expansion rule, and required sample count for each radiation/symmetry combination.

Therefore the implementation does not:

- guess unknown tags,
- accept unknown versions as compatible,
- infer missing directions,
- expand symmetry,
- invent rear-hemisphere samples,
- invent normalization,
- or invent interpolation.

## Why CF2 is blocked

CF2 is the secure/traceable binary distribution form produced from CLF authoring data. The investigation did not establish a public exact binary decoder specification with versioned byte-level semantics suitable for an independent deterministic parser and legally reproducible fixture.

No extension sniffing, reverse-engineered binary layout, or heuristic decoding is admitted.

CF2 also inherits the same downstream coordinate-normalization problem described above once its directional samples are decoded.

## Implemented subset

No native-format parser subset was implemented.

The only code change in this slice is a focused regression strengthening the existing PR #212 boundary:

- CLF registry state must remain `DEFERRED`.
- CF2 registry state must remain `DEFERRED`.
- Neither deferred descriptor may advertise a supported capability.
- Import attempts for either format remain `UNSUPPORTED`.
- The diagnostic still records SHA-256 of the exact submitted raw bytes before any parsing.

The existing normalized JSON and `htdt.polar-table.v1` adapters remain unchanged.

## Deliberately unsupported fields / semantics

Because no native adapter is authorized, all native CLF/CF2 payload fields remain unsupported by HTDT import in this slice.

Specifically, HTDT does not interpret or synthesize:

- CLF balloon symmetry,
- balloon arc order,
- radiation hemisphere expansion,
- native polar-to-horizontal/elevation reprojection,
- unknown CLF version compatibility,
- CF2 binary records,
- filters or multipart CLF2 v2 structures,
- phase where it is absent,
- missing angular samples,
- missing frequency bands,
- normalization references not exactly represented by the existing authority,
- or interpolation not explicitly bound by `EquipmentDefinition`.

## Phase semantics

No phase capability is opened.

The research confirms that CLF2 v2 added optional phase. That fact is not a license to generate phase for CLF2 v1 or for any magnitude-only source. Existing HTDT magnitude-only/complex gates remain unchanged.

## Normalization semantics

No CLF normalization mapping is implemented.

Real CLF text examples include `<BALLOON-REF>` values such as `<relative>`, but this slice does not have enough exact authority to map every native normalization/reference case onto either HTDT `on_axis_per_frequency` or `explicit_reference_level` without assumptions.

## Fixture provenance

No native CLF/CF2 fixture is committed.

A synthetic fixture would be legally straightforward only after the accepted native grammar/version and required sample topology are exact. Creating one now would encode the same unverified assumptions that the bounded-adapter rule is intended to prevent.

Existing HTDT-owned polar-table fixtures remain unrelated and unchanged.

## Required authority to unblock

A future native adapter can proceed when all of the following are available:

1. An official or equivalently authoritative, version-specific CLF text grammar **or** CF2 binary specification covering required/optional fields and record ordering.
2. Exact sample-count/grid rules for the chosen radiation and symmetry mode.
3. Exact coordinate convention including front/up/right axes, angular direction/sign, pole handling, and arc order.
4. Exact magnitude and normalization/reference semantics for the selected version/subset.
5. For phase-capable versions, exact phase units/reference/coherency semantics.
6. A lossless mapping contract from the selected native coordinate grid into existing `DirectivityDataset`, or an explicitly approved extension to the existing authority that R110 and O100D can consume without hidden resampling.
7. A license-safe synthetic or redistributable fixture constructed from those exact semantics.

## Deferred formats

The following registry states remain unchanged:

- CLF — **DEFERRED**
- CF2 — **DEFERRED**
- SOFA/AES69 — **DEFERRED**
- CTA-2034 summary — **DEFERRED**

No `DEFERRED` entry is promoted to `SUPPORTED`.

## Validation result

Focused regression added in `backend/tests/test_cad_directivity_import.py` verifies both CLF and CF2 remain explicitly deferred, advertise no capability, return `UNSUPPORTED`, and preserve the raw submitted-byte SHA-256 in the diagnostic.

Validated on code/test head `49fa4e726a1581837eacb7d730b03db47387b47a`:

- GitHub Actions **CI #1083** — **PASS**
  - backend: **686 passed, 1 skipped, 2 warnings**
  - R100B / R100A-3 and Windows launcher/script/hardware-gate preflights: **PASS**
- GitHub Actions **Windows Release Artifact #474** — **PASS**
  - locked native package build: **PASS**
  - packaged maintenance smoke: **PASS**
  - old-PC -> new-PC migration validation: **PASS**
  - pinned Inno Setup / per-user installer build: **PASS**
  - install/uninstall user-data retention smoke: **PASS**
  - package and installer uploads: **PASS**

This final validation-record update changes documentation only and uses `[skip ci]` so the already-successful code-identical workflows are not rerun without cause.

RDC = **0**.
