# R110 source-model compiler contract foundation — 2026-09-19

Issues: #168, #101

## Scope

This slice adds a solver-neutral, immutable R110 source interface between the
existing Scene/SystemVariant + O100C equipment/directivity authorities and
future acoustic solver adapters.

No production wave solver is selected or modified. No PFFDTD/MFEM adapter is
connected by this change.

## New authority

`backend/src/htdt/cad_r110_source.py` defines
`R110CompiledSourceModel` and compiler version
`r110-source-model-compiler-1`.

The compiled authority binds exactly to:

- SceneRevision id + content hash
- SystemVariant id + semantic hash
- source speaker entity id + entity semantic hash
- EquipmentDefinition id/version/hash
- optional DirectivityDataset id/version/hash + exact source-asset hash
- exact source entity position/orientation
- EquipmentDefinition acoustic reference point transformed into world
  coordinates by the exact source entity pose
- explicit Scene `aim_xyz` as the acoustic/directivity reference axis
- declared/numerical valid frequency domain and its authority
- directivity capability tier and dataset kind
- coherent-phase availability and exact phase reference
- directivity normalization semantics
- exact interpolation implementation/version/provenance reference
- purpose-specific capability decisions, approximation metadata, unsupported
  reasons, and deterministic semantic hash

The compiler does **not** infer acoustic aim from cabinet orientation. If
`aim_xyz` is absent, directivity use is fail-closed.

The compiler also detects a conflicting legacy
`SceneEntity.acoustic_reference_offset_m`: when present it must agree with
the exact EquipmentDefinition acoustic reference point. The compiled source
uses EquipmentDefinition as the source reference authority instead of copying
equipment source data into Scene.

## Capability separation

The contract keeps these claims independent:

1. geometry/reference point
2. magnitude directivity
3. complex directivity
4. coherent phase
5. electrical sensitivity/reference
6. acoustic wave-excitation normalization

A single `solver_ready` boolean is intentionally not present.

Purpose-specific states are exposed separately:

- `SUPPORTED_FOR_GEOMETRIC_DIRECTIVITY`
- `SUPPORTED_FOR_COMPLEX_DIRECTIVITY`
- `BLOCKED_FOR_WAVE_EXCITATION`
- `UNSUPPORTED`

### Electrical reference is not wave excitation

EquipmentDefinition sensitivity/reference evidence is preserved as an
independent capability only. Compiler v1 has no accepted authority for

`electrical input -> acoustic volume velocity / complex source strength`.

Therefore wave-excitation normalization is always `UNKNOWN` and the wave
excitation use case remains `BLOCKED_FOR_WAVE_EXCITATION`. No solver-native
source amplitude is synthesized.

### Magnitude-only

An exact magnitude-only DirectivityDataset can support geometric/magnitude
source directivity when an explicit source aim axis exists.

It never enables coherent phase, complex directivity, or phase-bearing source
synthesis.

### Complex

Complex directivity is exposed only when all of the following are exact and
consistent:

- complex DirectivityDataset
- exact EquipmentDefinition binding
- exact source asset hash
- EquipmentDefinition coherent-phase claim
- exact matching phase reference
- explicit source aim axis

Even then, wave excitation remains blocked without an independent
electrical-to-acoustic transfer authority.

### polar_summary / analytic / unknown

- `polar_summary` is not expanded into a full angular field.
- `unknown` is unsupported for directivity.
- current EquipmentDefinition analytic metadata is only a named model; because
  no numerical evaluator authority is bound, compiler v1 does not mark it as
  solver-ready directivity.

## Persistence and deterministic reopen

`backend/src/htdt/cad_r110_source_repository.py` adds append-only persistence
for compiled source models.

On save/reopen it re-resolves and revalidates:

1. exact SceneRevision id/hash
2. exact SystemVariant id/hash and baseline binding
3. exact source entity in the materialized variant
4. exact EquipmentBindingRef and EquipmentDefinition id/version/hash
5. optional exact DirectivityDataset id/version/hash/source-asset hash
6. a fresh deterministic recompile equal to the persisted model

A payload that no longer reproduces from those authorities is rejected.

Dataset selection is explicit by semantic hash. The repository does not pick a
"latest" directivity dataset implicitly.

## Required fixtures

`backend/tests/test_cad_r110_source.py` covers:

1. magnitude-only speaker
2. complex directivity speaker
3. unknown directivity
4. polar_summary
5. exact source pose/reference point
6. SceneRevision/SystemVariant mismatch
7. EquipmentDefinition mismatch
8. DirectivityDataset mismatch
9. magnitude-only coherent request rejection
10. complex directivity with no wave-excitation normalization
11. deterministic compile
12. save/reopen with exact authority re-resolution

## Boundaries left for later R110 / solver-specific work

This slice does not establish:

- electrical-to-acoustic transfer authority
- volume velocity
- complex source strength / solver-native forcing
- source impedance
- solver boundary/source discretization
- mesh/grid source injection
- solver-specific directivity interpolation/resampling beyond referencing the
  existing exact DirectivityDataset interpolation authority
- production wave-solver selection
- PFFDTD or MFEM source adapter behavior
- R100B tolerance changes
- analytic-directivity numerical evaluator authority

No changes were made to `cad_equipment.py`, `cad_directivity.py`, production
solver adapters, coverage objective code, or common roadmap documents.

RDC used: **0**.
