# Issue #176 follow-up — InstallationOutput v2 projector / standards integration

Date: 2026-09-19  
Tracking: Issue #176  
Depends on: PR #193, PR #194, PR #192

## Scope

This follow-up connects the existing InstallationOutput/report path to the immutable
projector/video-geometry and standards authorities that landed after the original
InstallationOutput foundation.

New InstallationOutput generation uses:

- schema version 2;
- authority version `installation-output-2`;
- the existing `report.py` generation path;
- exact `ProjectorSpecification` + `VideoGeometryEvaluation` references;
- exact `StandardsProfile` + `StandardsEvaluation` references.

No second editable installation truth is introduced.

## Projector source of truth

Projector installation output is generated only when an exact
`ProjectorSpecification` and exact `VideoGeometryEvaluation` are supplied together.

The report snapshot carries the exact specification id/version/SHA-256 and the exact
video-geometry evaluation id/SHA-256. It summarizes authority-owned values including:

- projector SceneEntity id;
- cabinet-local lens-reference offset and evaluated world lens position;
- cabinet-local optical axis;
- throw-ratio range and evaluated throw ratio;
- evaluated zoom position;
- declared horizontal/vertical lens-shift ranges and evaluated required shifts;
- image-plane corners;
- projection-cone directions;
- supported and evaluated aspect-ratio information;
- screen entity reference, visible aperture, and frame clearance;
- projection and aggregate geometry status;
- per-seat sightline/obstruction result;
- pairwise collision/clearance result;
- acoustically-transparent screen acoustic-effect state from the existing video
  authority.

The report does not copy projector specification values into SceneEntity metadata and
does not infer a specification from generic Scene metadata.

The acoustically-transparent screen acoustic effect remains `UNKNOWN` when the video
geometry authority has no acoustic transmission/reflection model.

## Exact target validation

InstallationOutput generation fails closed when projector/video evidence does not bind
to the exact installation target. Validation includes:

- document id;
- SceneRevision id;
- SceneRevision content hash;
- optional SystemVariant id and semantic SHA-256;
- effective materialized Scene content hash;
- projector specification id/version/SHA-256;
- video-evaluation specification SHA-256;
- projector and screen entity kinds.

Stale or mismatched evidence is rejected rather than rendered.

## Standards source of truth

Standards report output is generated only from an exact `StandardsProfile` and exact
`StandardsEvaluation` pair.

The InstallationOutput snapshot records:

- profile id/version/semantic SHA-256;
- evaluation id/SHA-256;
- criterion id;
- criterion-level `PASS`, `FAIL`, `UNKNOWN`, or `NOT_APPLICABLE`;
- observed value and unit;
- criterion target entity ids;
- reason code;
- source publisher/document/version/reference/URI;
- exact evidence references exposed by the StandardsEvaluation result.

The report does not rerun the standards evaluator and does not manufacture a second
compliance truth. Criterion results are copied as a read-only report snapshot after
exact binding validation.

Hard-constraint policy is intentionally not part of InstallationOutput truth. No
aggregate compliance score is computed.

## UNKNOWN semantics

Scene entity presence alone is not evidence that an authority exists.

Therefore:

- a projector SceneEntity without an exact ProjectorSpecification and
  VideoGeometryEvaluation reports projector authority as `UNKNOWN`;
- absence of an exact StandardsProfile/StandardsEvaluation pair reports standards as
  `UNKNOWN`;
- unavailable authority fields remain absent/null under an explicit `UNKNOWN`
  section state rather than being guessed from generic metadata;
- CalibrationPlan remains `UNKNOWN` in this slice;
- AcousticTreatment integration remains `UNKNOWN` in this slice.

The last two integrations are deferred to their owning slices to avoid competing
authority implementations.

## Determinism and compatibility

`exported_at` remains generation metadata and is excluded from
`InstallationOutput.semantic_sha256`.

v2 semantic identity includes the projector and standards report summaries, exact
authority hashes, criterion results, and exact target bindings.

CSV keeps the existing installation-coordinate table and appends deterministic
authority-summary records for v2. HTML uses the existing `report.py` renderer and
adds projector/video and standards sections plus the machine-readable semantic
snapshot.

Serialized v1 InstallationOutput objects remain loadable and validate against their
original v1 identity payload. New calls to `build_installation_output()` generate v2.

## Verification coverage

Focused tests cover:

- exact ProjectorSpecification binding;
- valid VideoGeometryEvaluation;
- sightline obstruction present and absent;
- collision/clearance result;
- StandardsProfile + StandardsEvaluation binding;
- mixed PASS / FAIL / UNKNOWN criteria;
- SceneRevision mismatch rejection;
- SystemVariant mismatch rejection;
- projector specification/evaluation hash-binding mismatch rejection;
- projector authority absent -> UNKNOWN despite projector SceneEntity presence;
- standards authority absent -> UNKNOWN;
- deterministic CSV;
- deterministic semantic HTML payload;
- exported_at changes not affecting semantic identity;
- persistence/reopen/regeneration equality through the existing projector and
  standards repositories;
- v1 serialized InstallationOutput compatibility.

## Explicit non-scope

This slice does not implement:

- AcousticTreatment InstallationOutput integration;
- CalibrationPlan InstallationOutput integration;
- PDF generation;
- UI changes;
- projector color/HDR calibration;
- acoustically-transparent screen acoustic modeling;
- CRM or quotation output.

RDC is not required for this slice.
