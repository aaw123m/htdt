# Issue #171 × #167 — AcousticTreatment semantic-surface exact binding

Status: implementation slice connecting the merged Issue #171 AcousticTreatment foundation to the merged Issue #167 R120 semantic acoustic geometry authority.

## Exact binding semantics

`AcousticTreatmentPlacement` remains an attached-treatment authority. It is not converted into a `SceneEntity`, does not mutate the base material, and does not write material data into `SemanticSurface`.

The exact host chain is:

1. placement `scene_revision_id + scene_content_hash`
2. exact immutable `SceneRevision`
3. that revision's embedded `SceneDocument.r120_semantic_geometry`
4. exact `host_surface_id`
5. exact versioned `host_surface_authority_sha256`

No redundant semantic-geometry id/hash is copied into the placement. The existing exact SceneRevision content hash already commits to the embedded R120 geometry payload. This avoids creating a second geometry-binding truth that could diverge from the SceneRevision.

For R120-backed revisions, `host_surface_authority_sha256` is defined by `r120-semantic-surface-host-1`: a deterministic SHA-256 over the complete immutable `SemanticSurface` payload plus the host-authority algorithm version. The hash intentionally does **not** include the whole semantic geometry snapshot. This permits stable surface continuity to be distinguished from unrelated geometry changes.

When a placement is built against a SceneRevision that already contains R120 semantic geometry:

- the host surface must exist in that exact geometry;
- an omitted host hash is derived from the exact `SemanticSurface`;
- a supplied host hash must match exactly;
- no key/name fallback or automatic re-binding is performed.

The treatment repository repeats this validation on save and when reopening/listing persisted placements whose bound SceneRevision contains R120 geometry. Invalid exact host bindings fail closed.

## Backward compatibility

PR #197 placements used schema/authority version 1 and allowed an opaque host id/hash before R120 semantic geometry was connected.

That persisted format is unchanged. A placement bound to a historical SceneRevision with no R120 semantic geometry remains readable and keeps its original semantic hash. Such a host reference evaluates as `legacy_unverified`; it is not silently promoted to exact R120 authority.

Because SceneRevision is immutable, adding semantic geometry creates a new revision rather than changing the old revision in place. Existing PR #197 data therefore does not acquire new semantics retroactively.

## Stale semantics and surface lifecycle

`TreatmentSurfaceBindingEvaluation` is immutable and has a deterministic evaluation SHA-256.

The evaluator resolves both:

- the placement's original bound SceneRevision; and
- an optionally evaluated newer SceneRevision.

It distinguishes exact binding validity from cross-revision surface continuity.

Important states include:

- `exact`: exact SceneRevision, exact R120 geometry, exact surface id, exact surface authority hash;
- `legacy_unverified`: historical host id/hash with no R120 geometry authority;
- `surface_authority_mismatch`: surface id still exists but the exact SemanticSurface payload changed;
- `surface_removed`: the surface id is absent from the relevant geometry;
- `wrong_scene_revision`: the evaluated revision belongs to another SceneDocument;
- `stale_scene_revision`: the placement is still bound to an older revision even when the semantic geometry snapshot is unchanged;
- `stale_semantic_geometry`: the newer revision resolves a different semantic geometry hash.

Surface lifecycle is reported separately:

- `stable_same_authority`: stable surface id and identical surface authority hash;
- `stable_authority_changed`: stable surface id but changed semantic class/membership/authority;
- `removed`: stable id no longer exists;
- `unavailable`: no comparable R120 surface authority exists.

A stable surface id is therefore useful for explicit lineage/tracking, but it never auto-rebinds an old placement. Rebinding requires creating a new immutable treatment placement version against the new SceneRevision.

## room_boundary / object_surface / unknown policy

This slice does not impose a treatment-type-specific restriction between `room_boundary` and `object_surface`. Both are valid host semantic classes for placement authority.

`unknown` is also permitted as an exact placement host when id/hash/revision authority matches. This is deliberate: placement authority and acoustic prediction capability are different claims.

An exact placement on an `unknown` surface reports:

- valid placement authority;
- unknown surface semantics;
- `solver_prediction_readiness = UNKNOWN`.

No solver capability is inferred from the host classification.

## Prediction boundary

This slice does not:

- compile treatment physics into solver boundaries;
- overwrite the host surface base material;
- generate impedance from scalar absorption;
- infer a material model from treatment type;
- promote `solver_prediction_readiness` to `SUPPORTED`.

The existing `evaluate_treatment_prediction_capability()` behavior is unchanged. Surface binding only establishes where the immutable treatment placement is attached.

## Persistence and fail-closed behavior

`CadAcousticTreatmentRepository` keeps the existing tables and payload schema. No duplicate R120 binding table is added.

For a placement whose bound SceneRevision contains R120 geometry, repository save/reopen validates:

- saved definition authority;
- exact SceneRevision id/content hash;
- exact SemanticSurface existence;
- exact host surface authority hash;
- existing SystemVariant and placement-lineage rules.

Hash mismatch and missing surfaces are rejected. Surface key/name is never used as a fallback.

## Focused fixtures

`backend/tests/test_cad_acoustic_treatment_surface_binding.py` covers:

1. porous treatment on a `room_boundary` surface;
2. placement on an `object_surface`;
3. nonexistent surface rejection;
4. changed semantic geometry hash reported as stale;
5. wrong SceneRevision;
6. stable surface id with mismatched surface authority;
7. exact `unknown` surface placement without prediction promotion;
8. guided repair that changes other geometry while preserving the same stable surface id/authority;
9. guided repair that preserves the stable id but changes that surface's authority;
10. surface removal;
11. deterministic save/reopen validation and evaluation identity.

Existing Issue #171 and Issue #167 regression suites remain the authority for foundation and semantic-geometry behavior.
