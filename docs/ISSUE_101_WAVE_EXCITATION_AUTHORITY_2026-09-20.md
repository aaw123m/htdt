# Issue #101 — explicit acoustic wave-excitation authority

Date: 2026-09-20

## Scope

This slice resolves the R110/R130 source-strength gap without deriving acoustic source strength from electrical sensitivity.

The authority chain is:

```text
EquipmentDefinition
+ explicit measured/modelled complex volume-velocity spectrum
→ AcousticWaveExcitationAuthority
R110CompiledSourceModel
+ AcousticWaveExcitationAuthority
→ WaveSourceExcitationBinding
→ AcousticSceneSnapshot v3
→ AcousticPredictionRequest
→ AcousticSolverDispatchBinding
```

No wave solver is executed.

## Explicit acoustic authority

`AcousticWaveExcitationAuthority` is immutable/versioned and represents:

- exact EquipmentDefinition id/version/hash;
- equivalent monopole located at the equipment acoustic reference point;
- absolute complex volume velocity in m³/s;
- exact frequency samples;
- `exp(-i*omega*t)` phasor convention;
- exact interpolation implementation/version/provenance;
- exact provenance;
- declared approximation note;
- valid frequency domain equal to the sample bounds.

At least two frequency samples are required so the valid frequency interval is explicit.

This authority is acoustic evidence. It is not a sensitivity-to-volume-velocity conversion.

## No electrical inference

Existing R110 v1 remains unchanged and continues to report:

`BLOCKED_FOR_WAVE_EXCITATION`

when considered alone.

A sensitivity reference such as 2.83 V / 1 m SPL is never converted into volume velocity, source impedance, monopole strength, or solver forcing.

Wave readiness can be resolved only by an exact external `WaveSourceExcitationBinding`.

## Exact source binding

`WaveSourceExcitationBinding` binds:

- exact R110 compiled-source SHA-256;
- exact source entity;
- exact EquipmentDefinition identity;
- exact AcousticWaveExcitationAuthority id/version/hash;
- exact valid frequency domain;
- explicit equivalent-source model.

The binding is deterministic and rejects an excitation authority for another equipment definition.

## Persistence

`CadWaveExcitationRepository` stores excitation authorities and source bindings append-only in the native CAD database.

Save/reopen re-resolves:

- EquipmentDefinition;
- R110CompiledSourceModel;
- AcousticWaveExcitationAuthority;
- deterministic source/excitation recomposition.

No opaque hash is accepted as sufficient authority.

## AcousticSceneSnapshot v3

Snapshot v1 and v2 identities remain compatible:

- v1: no treatment, no explicit wave excitation;
- v2: treatment boundary composition, no explicit wave excitation;
- v3: one or more explicit wave-source excitation bindings.

The v3 snapshot includes exact `WaveSourceExcitationBinding` identities.

A source whose R110 v1 state is BLOCKED becomes wave-source-ready only when:

1. an exact binding exists for that R110 source;
2. the binding matches the source entity and EquipmentDefinition;
3. the requested frequency interval is inside the excitation authority valid domain.

The snapshot still preserves the underlying R110 blocked state. The external acoustic authority resolves the missing capability rather than rewriting R110 history.

A binding outside the requested frequency range does not promote readiness and records:

`wave_source_excitation_frequency_domain_unsupported`.

## Solver dispatch

The focused fixture connects the v3 snapshot to the merged PR #222 adapter contract.

When geometry, boundary, environment, receiver and explicit wave excitation are all ready, a wave `complex_pressure` request can reach dispatch `READY`.

This means only that the exact request is compatible with the exact adapter contract. No numerical result, convergence, or production solver approval is implied.

## Focused verification

`backend/tests/test_cad_wave_excitation.py` covers:

1. R110 alone remains BLOCKED;
2. explicit complex volume-velocity authority is exact and deterministic;
3. wrong EquipmentDefinition binding is rejected;
4. exact binding promotes v3 snapshot wave readiness;
5. wave solver dispatch becomes READY for the exact supported contract;
6. snapshot save/reopen re-resolves the exact excitation binding.

## Deferred

- electrical-input → acoustic volume-velocity transfer model;
- source impedance;
- non-monopole wave-source injection;
- grid/mesh source discretization;
- actual R130 solve;
- production solver selection;
- GPU/CPU execution;
- calibration/fitting of volume velocity.

RDC usage: 0.
