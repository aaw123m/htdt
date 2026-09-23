# Issue #171 — AcousticTreatment foundation

Status: implementation slice for Issue #171.

## Scope implemented

This slice introduces a first-class, immutable treatment authority without changing the acoustic solver.

- `AcousticTreatmentDefinition` is immutable/versioned and carries deterministic `definition_sha256`.
- supported treatment taxonomy:
  - porous absorber
  - absorber with explicit air gap
  - membrane/panel absorber
  - perforated/slotted absorber
  - bass trap
  - diffuser/scattering element
  - hybrid
- exact provenance identifies source kind, source id, source version, optional exact source SHA-256, and reference.
- manufacturing/cut-list dimensions are explicit `width_m / height_m / thickness_m`; air gap is separate from panel thickness.
- physical layer stack keeps thickness, density, airflow resistivity, and surface density explicit.
- treatment-specific physical parameters include bulk density, airflow resistivity, membrane surface density, perforation/slot open-area ratio, and cavity depth.
- treatment-level acoustic capability uses the existing `AcousticMaterial` authority. Wave complex-impedance capability and geometric absorption/scattering capability remain independent.
- acoustic model provenance distinguishes measured / inferred / modelled evidence, valid frequency band, and uncertainty.
- unsupported treatment physics is represented by absence of a treatment acoustic model and evaluates to `UNKNOWN`.
- `AcousticTreatmentPlacement` is an immutable/versioned attached-treatment instance with exact position, orientation, coverage, optional semantic host-surface id/hash, SceneRevision binding, optional SystemVariant binding, and append-only proposed -> installed lineage.
- definitions and placements persist deterministically in the native SQLite database and reopen through `CadAcousticTreatmentRepository`.

## Authority boundaries

Treatment is deliberately **not** represented as a `SceneEntity` and is not folded into the room base-construction material.

The following authorities remain distinct:

1. SceneRevision owns physical room/entity truth.
2. SystemVariant owns proposed system/entity changes.
3. R110/R100 `AcousticMaterial` owns explicit wave-vs-geometric material capability semantics.
4. AcousticTreatmentDefinition owns the attached treatment assembly and its physical/acoustic source authority.
5. AcousticTreatmentPlacement owns placement/lifecycle lineage and exact bindings to the existing scene/variant authorities.
6. Furniture-equivalent absorption is not represented by AcousticTreatmentDefinition.
7. The no-treatment baseline is an exact SceneRevision/SystemVariant with no selected treatment placements. No synthetic zero-absorption treatment is created.

A placement may carry a semantic `host_surface_id` plus exact surface-authority hash, but this slice does not invent a replacement room-surface truth. Stronger validation against Issue #167 semantic acoustic geometry can be added once that authority is merged.

## Capability semantics

This slice does not compile treatment into solver boundaries.

`evaluate_treatment_prediction_capability()` therefore separates:

- wave material capability: `SUPPORTED` only when the reused `AcousticMaterial.wave_model` explicitly supports it;
- geometric material capability: `SUPPORTED` only when the reused `AcousticMaterial.geometric_model` explicitly supports it;
- solver prediction readiness: always `UNKNOWN` in this foundation slice.

A banded scalar absorption/scattering model does not create a complex impedance table. A proposed or installed placement does not, by itself, establish prediction capability.

The focused porous-panel fixture intentionally has measured banded geometric data with `wave_model='unsupported'`. Its geometric material capability is known while wave capability and solver readiness remain unknown.

The focused membrane fixture intentionally has physical geometry/parameters but no qualified acoustic model. Both material capabilities and solver readiness remain unknown.

## Lifecycle and variant semantics

Treatment placement history is append-only.

- version 1 must be `proposed`;
- a proposed placement may be revised as another proposed version;
- a proposed placement may transition to `installed`;
- installed placement versions are terminal;
- every later placement version binds to the exact previous placement semantic hash.

SystemVariant A/B can reference different treatment placements while materializing to the same baseline SceneRevision. This is intentional: treatment is an attached design authority, not a hidden Scene mutation. Tests verify that variant materialization and treatment persistence do not alter the baseline SceneRevision.

## Persistence

`CadAcousticTreatmentRepository` adds:

- `cad_acoustic_treatment_definitions`
- `cad_acoustic_treatment_placements`

Both are append-only semantic payload stores. Definition id/version and instance/version are immutable. Placement writes validate exact saved definition hash, SceneRevision id/content hash, SystemVariant id/hash when present, and prior placement lineage.

Native schema compatibility is checked before repository DDL/DML access, matching the current fail-closed repository pattern.

## Focused fixtures/tests

`backend/tests/test_cad_acoustic_treatment.py` covers:

- deterministic definition identity;
- measured porous panel with explicit dimensions, density, air gap, banded geometric capability, and no fake impedance;
- unsupported membrane acoustic physics remaining UNKNOWN;
- no-treatment baseline;
- SystemVariant A/B treatment alternatives;
- deterministic save/reopen;
- baseline SceneRevision immutability under variant materialization;
- proposed -> installed append-only lineage.

## Deferred prediction/output work

Explicitly deferred from this slice:

- acoustic solver/compiler changes;
- deriving or fitting complex impedance from scalar absorption;
- porous / membrane / perforated / slotted / diffuser numerical models not already backed by explicit authority;
- before/after prediction values;
- automatic prediction-capability promotion after placement;
- treatment optimization;
- validation against the Issue #167 semantic acoustic-geometry surface registry after that authority lands;
- InstallationOutput treatment quantity/cut-list rendering.

The last item should consume these definition dimensions and placement authorities directly when implemented. It must not create a second editable treatment truth.
