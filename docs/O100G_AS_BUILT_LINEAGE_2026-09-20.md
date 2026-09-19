# O100G — descendant proposal lineage and explicit as-built completion

Date: 2026-09-20

## Scope

This slice establishes the backend lifecycle boundary between an applied O100 proposal and physical as-built truth.

It does not implement measured promotion or GUI styling.

The authority chain is:

```text
immutable SystemVariant (proposed)
→ explicit SystemVariantApplication
→ applied SceneRevision
→ optional immutable descendant SceneRevision edits
→ explicit SystemVariantAsBuiltRecord
→ later measurement lifecycle
```

The original proposal is never rewritten.

## Descendant-aware proposal lineage

`CadSystemVariantRepository.proposal_lineage_for_revision()` now walks exact immutable SceneRevision ancestry.

Semantics:

- exact applied revision still resolves to its application/variant;
- a later save/edit descending from that revision retains the same proposal origin;
- if another SystemVariant is explicitly applied later in the lineage, the nearest application wins;
- missing parents, ancestry cycles and cross-document ancestry fail closed.

This supports real installation adjustment workflows where the selected proposal is applied first and then exact as-installed positions are edited before installation completion.

## SystemVariantAsBuiltRecord

An as-built record is explicit installation-completion evidence, not an inference from entity presence.

It binds:

- exact SystemVariantApplication id/hash;
- exact immutable SystemVariant id/hash;
- exact applied SceneRevision id/hash;
- exact as-built target SceneRevision id/hash;
- every revision id/hash along the applied→as-built ancestry path;
- every proposed entity id promoted to `as_built`;
- explicit manual installation-completion confirmation identity/time;
- optional notes.

All proposed entities must still exist in the target revision.

Entity kind must remain unchanged. Proposed speaker role must remain unchanged.

Position/orientation/size may differ in the descendant revision, allowing real installation adjustment to become explicit as-built geometry while preserving the proposal as historical evidence.

## No proposal rewrite

`SystemVariant.entity_lifecycle` remains the immutable proposal authority.

The as-built record uses separate `EntityLifecycleBinding(state='as_built')` values.

It never mutates:

- ProposedEntitySpec;
- proposal evidence refs;
- original proposed geometry;
- baseline SceneRevision;
- SystemVariant hash.

## Persistence

`CadSystemVariantLifecycleRepository` stores one append-only installation-completion record per exact SystemVariantApplication.

Save/reopen re-resolves:

- application id/hash;
- SystemVariant id/hash;
- target SceneRevision id/hash;
- nearest proposal ancestry;
- complete applied→target revision path;
- proposed entity presence/kind/role.

An unrelated revision or a revision governed by a newer SystemVariantApplication cannot be promoted through the older application.

## Focused verification

`backend/tests/test_cad_system_variant.py` adds checks for:

1. proposal lineage survives a descendant SceneRevision installation edit;
2. original proposal geometry remains unchanged;
3. explicit descendant as-built completion records exact revision lineage;
4. all proposed entities become separate `as_built` lifecycle evidence;
5. append-only save/reopen;
6. a descendant missing a proposed entity cannot be promoted.

## Deferred to the next O100G slice

- SystemVariantApplication-specific measurement plan/campaign;
- exact measurement + MeasurementQuality capability binding;
- `measured` lifecycle promotion;
- measurement retake lineage integration;
- UX proposed/as-built/measured badges and 3D ghost styling;
- Windows visual acceptance (#118).

RDC usage: 0.
