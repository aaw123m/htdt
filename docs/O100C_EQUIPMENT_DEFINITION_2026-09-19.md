# O100C EquipmentDefinition authority — 2026-09-19

Issue: #168

## Implemented authority

This slice introduces an immutable/versioned `EquipmentDefinition` authority for
speaker/source equipment. Product identity and source capability no longer need to
be encoded in `SceneEntity.name`, generic metadata, or an untyped point-source
assumption.

The authority records:

- manufacturer or user-defined identity;
- exact source/version/reference/SHA-256 provenance;
- cabinet envelope and entity-local acoustic reference point;
- mounting, port, and explicit clearance metadata;
- optional sensitivity/reference-level conditions;
- optional evidenced continuous/peak SPL and declared headroom conditions;
- directivity capability tiers `complex`, `magnitude_only`,
  `polar_summary`, `analytic`, and `unknown`;
- explicit valid frequency/horizontal/vertical domains;
- explicit interpolation method, implementation version, and provenance for every
  known directivity capability;
- measured/manufacturer/inferred/analytic/user-defined provenance distinctions;
- explicit uncertainty records;
- deterministic semantic SHA-256 identity.

`CadEquipmentRepository` persists definitions append-only by
`(definition_id, version)`. Reusing an id/version with different semantics is
rejected. Save/reopen validates the semantic hash.

## SystemVariant binding

`SystemVariant` now supports `EquipmentBindingRef`, containing only the exact
source entity id plus EquipmentDefinition id/version/semantic hash. Equipment
properties are not duplicated into the variant.

To preserve O100A/O100B identity semantics, variants without equipment bindings
continue to use `o100a-system-variant-2`. A variant with equipment bindings uses
`o100a-system-variant-3`, and the binding references participate in the variant
semantic hash.

`CadSystemVariantRepository.save_variant()` rejects dangling or identity-mismatched
EquipmentDefinition references. `CadEquipmentRepository.resolve_variant_bindings()`
re-materializes the exact variant from its baseline SceneRevision, verifies the
bound entity is a speaker source, and resolves the exact persisted definition.

## Downstream capability gate

`evaluate_equipment_capability()` is fail-closed. It exposes independent claims
for cabinet geometry, sensitivity/reference level, continuous SPL, peak SPL,
directivity summary, magnitude directivity, complex directivity, and coherent
phase.

Important semantics:

- `unknown` directivity supports no directivity claim;
- `polar_summary` is not silently upgraded to an arbitrary-angle magnitude field;
- `magnitude_only` cannot support complex/coherent-phase claims;
- `complex` requires an explicit coherent phase reference;
- analytic phase is available only when explicitly declared with a phase reference;
- requested frequency/angles outside the declared valid domain are unsupported;
- missing sensitivity/SPL data remains unsupported rather than receiving defaults.

## Authority boundaries

- SceneRevision remains the physical scene authority.
- SystemVariant remains the proposed topology/lifecycle authority.
- EquipmentDefinition owns equipment/source physical and acoustic capability data.
- The SystemVariant stores references only, not a second copy of equipment truth.
- This slice does not create an R110 solver source model. The explicit capability
  tiers, acoustic reference point, asset hashes, domain, interpolation provenance,
  and coherent-phase gate are intended to be consumed by the future R110 source
  interface.
- No coherent phase is generated from magnitude-only data.
- No missing manufacturer values are inferred into authoritative values.
- No commercial product catalogue is bundled.

## Deferred

The following are intentionally outside this slice:

- #169 coverage, SPL/headroom, and worst-seat objective evaluation;
- R110 solver/source compilation and acoustic transfer prediction;
- binary CLF/CF2/SOFA/AES69 decoding/import adapters. This slice can identify those
  source formats and bind exact asset hashes/capability semantics, but parsing the
  binary/text source into solver-ready R110 data remains deferred to the R110 import
  boundary;
- CTA-2034/polar-summary objective interpretation beyond the explicit
  `polar_summary` capability tier;
- amplifier/device interaction;
- UI/catalogue workflows.

## Focused verification

`backend/tests/test_cad_equipment.py` covers:

- representative manufacturer-defined and user-defined fixtures;
- unknown, magnitude-only, and complex directivity capability tiers;
- rejection of fabricated coherent phase for magnitude-only data;
- deterministic save/reopen semantic identity;
- immutable id/version persistence;
- exact EquipmentDefinition binding to a SystemVariant source entity;
- equipment choice participating in equipped SystemVariant identity;
- rejection of unpersisted equipment references;
- fail-closed unsupported/domain-outside downstream capability checks.

No RDC or Windows visual acceptance is required for this backend authority slice.
