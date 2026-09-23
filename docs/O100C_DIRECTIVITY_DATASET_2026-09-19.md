# O100C DirectivityDataset authority — 2026-09-19

Issue: #168  
Predecessor: PR #198 EquipmentDefinition authority

## Scope and authority boundary

This slice adds the numerical directivity authority used after an
EquipmentDefinition has declared that directivity capability exists.

The existing EquipmentDefinition remains the immutable equipment/capability
authority. DirectivityDataset does not replace or copy cabinet, sensitivity,
SPL/headroom, SystemVariant, or SceneRevision truth. Each dataset binds an exact
EquipmentDefinition by definition id, version, and semantic SHA-256, and also binds
the exact source asset SHA-256 already declared by that EquipmentDefinition.

A dataset is rejected when the EquipmentDefinition identity/hash, declared
directivity tier, source asset hash, source format, valid domain, interpolation
authority, or complex phase reference does not match.

The source asset is never rewritten. The built-in adapter parses bytes into an
immutable normalized authority in memory; the original bytes and their SHA-256
remain the provenance anchor.

## Dataset identity and provenance

DirectivityDataset v1 records:

- dataset id/version and deterministic semantic SHA-256;
- exact EquipmentDefinition id/version/semantic SHA-256;
- exact source asset SHA-256;
- source data format plus normalized container format;
- parser id/version and adapter id/version;
- measured/manufacturer/inferred/analytic/user-defined evidence kind;
- exact source provenance;
- magnitude-only or complex dataset kind;
- explicit coordinate convention and reference axis;
- frequency, horizontal/azimuth, and vertical/elevation grids;
- complete rectangular sample-grid identity and sample identity;
- normalization/reference semantics;
- exact valid frequency/angular domain;
- interpolation method, implementation, version, and provenance;
- explicit phase reference for complex data only.

The persisted repository is append-only by dataset id/version. Reusing an
id/version with different semantics is rejected.

## Coordinate convention

v1 keeps the request API names horizontal_angle_deg and vertical_angle_deg so the
#169 caller has one stable interface.

The dataset declares how those coordinates are interpreted:

- horizontal_vertical: horizontal and vertical off-axis angles. Azimuth wrap is
  forbidden.
- spherical_azimuth_elevation: horizontal_angle_deg means azimuth and
  vertical_angle_deg means elevation.

The reference axis is equipment_acoustic_reference_axis. Positive azimuth is left,
positive elevation is up, and angles are degrees.

For spherical data, horizontal_wrap can be signed_180. In that case source grid
coordinates must use [-180, 180); +180 and -180 cannot both be supplied as
different samples. Evaluation records both the exact requested angle and the
wrapped angle actually evaluated.

## Normalization and dB/linear conversion

Normalized samples are stored as pressure-amplitude magnitude in dB.

The only v1 conversion convention is pressure-amplitude-db20-v1:

    magnitude_db = 20 * log10(linear_pressure_ratio)

Linear source magnitudes must therefore be positive. Zero/negative values are
rejected rather than mapped to an arbitrary floor.

For on_axis_per_frequency normalization, the source must contain the 0 degree
horizontal/azimuth and 0 degree vertical/elevation sample at every frequency, and
that sample must be exactly 0 dB after normalization. The adapter does not silently
renormalize source data.

Magnitude-only data cannot carry phase. Complex data requires phase for every
sample and an explicit phase reference matching EquipmentDefinition.

## Evaluation and interpolation semantics

evaluate_directivity() is a deterministic, fail-closed derived authority.

Inputs are the exact dataset plus requested frequency, horizontal/azimuth angle,
vertical/elevation angle, and requested result type (magnitude or complex).

Semantics:

- exact grid request: return the exact stored sample;
- none: off-grid request is UNSUPPORTED;
- nearest: nearest grid coordinate on each axis, with deterministic lower-value
  tie breaking;
- linear: trilinear interpolation on frequency and angle coordinates;
- log_frequency_linear_angle: log-frequency interpolation and linear angle
  interpolation;
- any request outside the declared domain: UNSUPPORTED;
- no extrapolation is performed;
- a complex request against magnitude-only data: UNSUPPORTED;
- a missing required sample: UNSUPPORTED/fail-closed;
- unsupported interpolation methods remain representable but are not guessed by
  this evaluator.

Magnitude-only interpolation is performed in dB. Complex interpolation converts
each sample to its Cartesian complex pressure ratio using the fixed dB20
conversion, interpolates real and imaginary components, then derives output
magnitude/phase. Phase degrees are never directly linearly interpolated.

Every evaluation records dataset identity, EquipmentDefinition identity, exact
request coordinates, wrapped evaluated azimuth when applicable, interpolation
method/implementation/version, conversion version, supporting sample hashes,
result values or explicit UNSUPPORTED reason, and its own deterministic semantic
SHA-256.

Evaluation results are pure derived authority. Persisting a second mutable result
truth is intentionally avoided: reopening the exact persisted dataset and
re-evaluating the same request must produce the same evaluation semantic identity.

## Import support

Implemented:

- htdt.normalized-directivity.v1 JSON;
- strict UTF-8/JSON parsing;
- extra/ambiguous fields rejected by the normalized source schema;
- complete rectangular grid validation;
- magnitude-only and complex datasets;
- source magnitudes declared in dB or linear pressure ratio;
- horizontal/vertical and spherical azimuth/elevation coordinate semantics.

The normalized JSON adapter is deliberately bounded. It is an explicit stable
exchange format for already normalized data, not a heuristic universal parser.

Deferred:

- native CLF parsing;
- native CF2 parsing;
- SOFA/AES69 library integration;
- CTA-2034/polar-summary expansion;
- arbitrary spherical interpolation algorithms;
- solver-ready R110 source compilation.

CLF/CF2/SOFA/AES69 data must not be guessed through the normalized adapter. A
future native adapter must have its own id/version, preserve the exact original
asset SHA-256, and produce the same DirectivityDataset authority contract.

polar_summary remains a capability summary and is not silently promoted into an
arbitrary-angle DirectivityDataset.

## Persistence

CadDirectivityRepository stores datasets in the same project SQLite database as
the scene/equipment authorities. Save and reopen both revalidate:

- dataset semantic/grid/sample hashes;
- exact persisted EquipmentDefinition existence;
- exact EquipmentDefinition binding;
- exact source/directivity/interpolation/phase contracts.

No source bytes are stored or rewritten by this repository.

## API for Issue #169

Issue #169 should use this path:

1. Resolve the source entity's exact EquipmentDefinition through
   CadEquipmentRepository/SystemVariant binding.
2. Select an explicit DirectivityDataset id/version for that exact
   EquipmentDefinition. Do not auto-select a newest or best dataset.
3. Call evaluate_directivity() with the seat-derived source-relative frequency and
   off-axis coordinates.
4. Consume only SUPPORTED results.
5. Use magnitude_db as the directivity relative level/loss input while retaining
   the evaluation semantic SHA-256 as provenance.
6. Treat UNSUPPORTED as comparison-ineligible/missing according to #169 objective
   authority. Do not substitute 0 dB.
7. Request complex output only when the downstream calculation actually needs
   coherent complex directivity and the bound dataset supports it.

This slice does not implement #169 coverage/worst-seat/SPL/headroom objectives,
Pareto/O90 behavior, or R110 compilation.

## Focused verification

backend/tests/test_cad_directivity.py covers the requested minimum fixtures:

1. magnitude-only dataset;
2. complex dataset;
3. exact on-grid evaluation;
4. supported interpolation evaluation;
5. frequency-domain outside -> UNSUPPORTED;
6. angle-domain outside -> UNSUPPORTED;
7. complex request against magnitude-only -> UNSUPPORTED;
8. save/reopen with identical dataset and evaluation identity;
9. EquipmentDefinition semantic-hash mismatch rejection;
10. incomplete/ambiguous source data fail-closed.

The tests also cover fixed linear-to-dB conversion and exact complex phase
capability.

RDC is not required for this backend authority slice.
