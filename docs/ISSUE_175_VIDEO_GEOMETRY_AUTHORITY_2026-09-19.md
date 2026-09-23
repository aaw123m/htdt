# Issue #175 backend authority slice — projector / screen / sightline geometry

Date: 2026-09-19  
Tracking: Issue #175

## Implemented scope

This slice adds a deterministic geometry authority on top of the existing immutable
`SceneRevision` / `SystemVariant` path. It does not change the acoustic solver and does
not add UI visual-acceptance requirements.

Implemented domain authority:

- explicit `projector` and `riser` physical SceneEntity kinds;
- immutable/versioned `ProjectorSpecification`;
- manufacturer or user-defined projector-spec provenance;
- explicit cabinet-local lens reference and optical axis;
- throw-ratio range and deterministic required zoom fraction;
- optional manufacturer/user lens-shift ranges in one documented canonical convention;
- projector supported aspect-ratio authority;
- screen visible image aperture, frame dimensions from SceneEntity, and frame clearance;
- projection image plane and four-corner cone geometry;
- per-seat eye reference and spherical head-obstruction reference;
- horizontal/vertical subtended viewing angle and screen-center elevation angle;
- explicit user/profile angle thresholds without inventing a cinema-standard default;
- row/head sightline evaluation against explicit screen sample points;
- riser support/top-height interaction through an exact `riser` SceneEntity;
- OBB-based speaker/screen/projector collision and clearance evaluation;
- deterministic evaluation identity bound to exact SceneRevision and optional SystemVariant;
- append-only projector-specification/evaluation persistence and deterministic reopen.

## SceneRevision / SystemVariant authority

`SceneRevision` remains the physical scene truth. The new evaluator never saves a
temporary scene.

For a SystemVariant-bound evaluation:

1. the exact baseline SceneRevision id/hash is retained;
2. the exact SystemVariant id/hash is retained;
3. the variant is materialized in memory through existing
   `materialize_system_variant()`;
4. the materialized scene content hash is included in the evaluation target;
5. the baseline SceneRevision is not mutated or saved.

Issue #175 needs seat/screen/projector variants, while O100A originally restricted
`ProposedEntitySpec` to speakers. The existing authority was therefore extended
backward-compatibly:

- v1 persisted SystemVariant payloads remain accepted;
- new builder output uses `o100a-system-variant-2`;
- proposed physical entities may be speaker, seat, screen, projector, riser, furniture, or
  AV equipment;
- speaker proposals still require an exact `ChannelRoleBinding`;
- non-speaker proposals cannot smuggle speaker-role metadata;
- replacement preserves physical entity kind;
- measurement points remain outside proposed physical-entity semantics;
- proposal/current/as-built/measured lifecycle evidence rules remain unchanged.

## Projector specification semantics

`ProjectorSpecification` is not stored in SceneEntity name/metadata.

Identity includes:

- specification id/version;
- manufacturer/model where applicable;
- source kind: `manufacturer` or `user_defined`;
- publisher/document/version/reference/source URI/source hash provenance;
- cabinet-local lens reference;
- cabinet-local optical axis;
- throw-ratio min/max;
- optional optical zoom ratio metadata;
- optional horizontal/vertical lens shift;
- supported aspect ratios.

Lens-shift values use a canonical full-image-dimension fraction from the image center.
Source-specific percentages must be explicitly converted into that convention; the
evaluator does not guess a manufacturer's percentage convention.

## Projection geometry

The screen SceneEntity is the physical frame/envelope. `ScreenGeometryBinding` stores
the visible image aperture and image-center offset. In screen-local axes:

- +X = image right;
- +Y = viewing/projector side normal;
- +Z = image up.

The evaluator derives:

- exact world lens point;
- exact image-plane center and corners;
- optical-axis intersection with the image plane;
- perpendicular lens-to-plane throw distance;
- throw ratio;
- normalized required zoom position inside the declared throw-ratio interval;
- required horizontal/vertical lens shift;
- optical-axis deviation from the screen normal;
- visible-aperture fit inside the physical screen frame;
- projector aspect-ratio support.

No keystone, warp, or image-processing calibration is implemented. An optical-axis
deviation threshold is explicit evaluation policy, not an inferred standard.

## Seat viewing / sightline

Each seat binding stores exact entity id, row id, local eye point, local head-sphere
center/radius, and optional riser entity id.

The evaluator computes:

- horizontal screen subtended angle;
- vertical screen subtended angle;
- vertical angle from eye to screen center;
- deterministic line-segment/head-sphere clearance for every explicit sightline sample;
- blocking seat ids and blocking row ids;
- minimum head-ray clearance.

No RP22/SMPTE/Dolby viewing-angle threshold is silently introduced. Angle limits and
sightline sample points are explicit request/policy data and therefore participate in
the request/evaluation hash.

## Riser interaction

A riser is an explicit physical SceneEntity, not a metadata string.

For a seat bound to a riser, evaluation records:

- seat base Z from the oriented seat envelope;
- riser top Z from the oriented riser envelope;
- support gap;
- horizontal support of the seat center by the riser footprint;
- PASS/FAIL against an explicit support tolerance.

Sightline geometry uses the seat's exact world pose. Therefore a variant that raises a
rear-row seat together with its exact riser changes the sightline result without mutating
the baseline revision.

## Collision authority

The backend evaluates speaker/screen/projector physical envelopes with 3D oriented-box
SAT. The screen frame clearance and explicit global collision clearance are applied
geometrically. Collision evidence is pair-specific and retained in the deterministic
result; it is not collapsed into an optimization score.

## Acoustic-screen boundary

`ScreenGeometryBinding.acoustically_transparent` is metadata about screen construction
only.

This slice has no acoustic transmission/reflection authority for acoustically transparent
screens. Therefore:

- `acoustically_transparent=True` -> acoustic effect is `UNKNOWN`;
- unknown transparency -> acoustic effect is `UNKNOWN`;
- explicitly non-transparent -> acoustic-effect check is `NOT_APPLICABLE`.

The acoustic status is kept separate from `geometry_status`; it does not fabricate an
acoustic pass/fail and it does not alter the acoustic solver.

## Persistence / reproducibility

`CadVideoGeometryRepository` stores:

- `cad_projector_specifications`;
- `cad_video_geometry_evaluations`.

Before repository DDL it reuses the native schema compatibility gate.

On evaluation save it verifies:

1. the exact projector specification/hash is already persisted;
2. the exact SceneRevision id/document/hash exists;
3. an optional SystemVariant exists with the exact id/hash and exact baseline binding;
4. the evaluator reproduces the complete immutable evaluation byte-for-byte in model
   semantics;
5. only then is the deterministic evaluation persisted.

Reopen re-validates projector/evaluation semantic hashes through the Pydantic models.

## Explicit non-scope / remaining work

Not implemented in this slice:

- acoustic solver changes;
- acoustically-transparent screen transmission/reflection/diffraction modeling;
- SDR/HDR/color calibration;
- projector CMS, tone mapping, keystone/warp calibration;
- UI visual acceptance;
- native workflow/palette/Inspector UX;
- standards-profile thresholds from PR #192;
- automatic candidate pruning or optimization scoring from the new geometry status;
- roadmap/status-document edits that would conflict with PR #192.

The future standards/profile layer may consume these exact geometry observations after
PR #192 lands, but this slice does not create a second standards authority.

## Focused verification

`backend/tests/test_cad_video_geometry.py` covers:

- deterministic manufacturer specification identity and provenance;
- user-defined projector specification support without fabricated manufacturer/model;
- exact lens reference, throw ratio, zoom fraction, lens shift, aspect ratio, image plane;
- seat horizontal/vertical/center viewing angles;
- row/head obstruction on a baseline SceneRevision;
- rear-row riser variant clearing the obstruction;
- projector replacement + seat replacement + riser addition in one SystemVariant;
- baseline SceneRevision immutability during variant evaluation;
- speaker/screen collision failure;
- acoustically-transparent screen acoustic effect remaining `UNKNOWN`;
- projector spec/evaluation save and deterministic reopen;
- rejection of non-physical ProposedEntitySpec payloads.

RDC was not used.
