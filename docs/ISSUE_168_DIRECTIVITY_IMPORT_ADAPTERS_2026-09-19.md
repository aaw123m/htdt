# Issue #168 — bounded directivity asset import adapters

Date: 2026-09-19

## Scope

This slice adds a bounded import boundary in front of the existing
`DirectivityDataset` authority. It does not add a second dataset truth and it
does not modify R110, coverage, interpolation, or the existing
`CadDirectivityRepository`.

The import path is:

`raw source bytes -> exact adapter/parser -> build_directivity_dataset() -> DirectivityDataset`

The raw byte sequence is SHA-256 hashed before any UTF-8 decoding, newline
handling, CSV/TSV parsing, or normalization. The original asset identity is
therefore the exact submitted bytes, including whitespace and line endings.

## Versioned registry and service

`backend/src/htdt/cad_directivity_import.py` adds:

- immutable `DirectivityAdapterDescriptor`
- immutable/versioned `DirectivityAdapterRegistryAuthority`
- exact supported/deferred adapter records
- immutable `DirectivityImportDiagnostic`
- immutable `DirectivityImportResult`
- `import_directivity_asset(...)` as the bounded service entry point

Adapter selection requires all of the following explicitly:

- source format
- declared schema
- adapter id
- adapter version

No filename or extension input exists in the service, so extension-based format
guessing is impossible. Column inspection is also not used to choose an
adapter.

The existing `htdt.normalized-directivity.v1` JSON adapter is registered
without changing its semantics.

## Supported format: HTDT polar_table v1

The new supported native import format is:

- source format: `polar_table`
- schema: `htdt.polar-table.v1`
- adapter: `htdt.polar-table-adapter@1`
- parser: `htdt.polar-table-parser@1`
- encoding: strict UTF-8
- container: explicitly declared CSV or TSV

The file starts with exact `# key=value` metadata. Required metadata fixes:

- schema and delimiter
- dataset id/version
- capability: `magnitude_only` or `complex`
- coordinate semantics
- horizontal wrap
- acoustic reference axis
- positive azimuth/elevation directions
- frequency unit `Hz`
- angle unit `degree`
- magnitude unit `db` or `linear`
- normalization semantics
- interpolation method/implementation/version
- evidence kind and source provenance strings
- exact phase reference for complex data

The table columns are exact, not inferred.

For `horizontal_vertical`:

`frequency_hz,horizontal_angle_deg,vertical_angle_deg,magnitude,magnitude_unit`

For `spherical_azimuth_elevation`:

`frequency_hz,azimuth_deg,elevation_deg,magnitude,magnitude_unit`

Complex tables append `phase_deg`. Magnitude-only tables are forbidden from
carrying a phase column or phase reference.

Every row repeats `magnitude_unit`, and it must exactly match the declared
metadata value.

## Coordinate and unit contract

`horizontal_vertical` requires `horizontal_wrap=none`.

`spherical_azimuth_elevation` accepts only explicitly declared `none` or
`signed_180`. Signed-180 coordinates use `[-180, 180)`. A table containing
both -180 and +180 is rejected as ambiguous.

Vertical/elevation values are bounded to `[-90, 90]`. Horizontal/azimuth
values are bounded to `[-180, 180]`.

Linear magnitude is pressure-amplitude magnitude and must be strictly positive.
Conversion uses the existing `pressure-amplitude-db20-v1` semantics through
`20*log10(value)`. No phase is synthesized.

`on_axis_per_frequency` means the table already declares normalized values;
the importer does not silently renormalize the source. The existing
`DirectivityDataset` validator therefore requires the declared on-axis sample
to be 0 dB at every frequency. `explicit_reference_level` requires an exact
`reference_level_db`.

## Validation and authority binding

The polar-table adapter rejects:

- malformed or unknown schema metadata
- unknown or missing metadata fields
- non-UTF-8 input
- delimiter mismatch
- wrong/extra/reordered columns
- invalid frequency or angle values
- duplicate coordinates
- incomplete rectangular grids
- ambiguous -180/+180 directions
- unsupported magnitude units
- non-positive linear magnitude
- phase in magnitude-only data
- missing phase or phase reference in complex data

Dataset construction always calls the existing `build_directivity_dataset()`.
Its existing binding checks remain authoritative for:

- exact EquipmentDefinition id/version/hash
- exact raw source asset SHA-256
- exact declared directivity domain
- exact source format
- exact capability tier
- exact interpolation method/implementation/version
- exact coherent phase reference

The adapter does not create a parallel dataset schema.

## Import diagnostics

Successful imports report:

- raw source SHA-256
- explicit source format/schema
- adapter/parser identity and version
- sample count
- frequency/angular domain
- magnitude-only vs complex capability
- warnings
- normalized `DirectivityDataset.semantic_sha256`

Rejected/unsupported imports carry an explicit rejection/deferred reason and do
not carry a dataset hash.

## Deferred formats

The registry records the following as explicitly deferred rather than guessed:

- CLF
- CF2
- SOFA/AES69
- CTA-2034/spinorama summary

CLF and CF2 do not have a native parser in this slice. No fixture-only parser is
presented as format support, and unknown fields/versions are not heuristically
decoded.

SOFA/AES69 remains outside this slice. No HDF5/SOFA production dependency is
added.

CTA-2034/spinorama summary data is not promoted to a full arbitrary-angle
`DirectivityDataset`; it remains a future `polar_summary`-appropriate input
class.

## Persistence and downstream integration

Persistence continues to use `CadDirectivityRepository` unchanged.

Focused integration fixtures confirm that an imported polar-table dataset can
be consumed directly by:

- the existing R110 source compiler
- the existing O100D coverage evaluator

No R110 or coverage implementation changes are made.

## Focused fixtures

`backend/tests/test_cad_directivity_import.py` covers:

1. valid magnitude-only polar table
2. valid spherical TSV table
3. linear magnitude conversion
4. duplicate row rejection
5. missing grid cell rejection
6. malformed schema rejection
7. EquipmentDefinition domain mismatch
8. exact source SHA mismatch
9. phase column in magnitude-only data rejection
10. complex schema with exact phase reference
11. -180/+180 ambiguity rejection
12. import twice -> identical Dataset hash
13. save/reopen through existing repository
14. imported Dataset -> R110 compile
15. imported Dataset -> coverage evaluation
16. CLF deferred format -> explicit UNSUPPORTED

## Non-goals preserved

This slice does not add:

- commercial speaker catalogue ingestion
- web scraping
- guessed CLF/CF2 parsing
- SOFA/HDF5 dependency adoption
- interpolation algorithm changes
- coverage algorithm changes
- R110 compiler changes
- UI
- common roadmap/status edits

RDC used: 0.
