# R150 late-field scattering / diffraction / late-energy path authority

Status: **software vertical slice implemented; bounded late-energy path contributions are available as a typed authority for R160 hybrid composition. Crossover/stitching decisions and production solver validation remain intentionally unimplemented.**

Issue: #101
Draft PR: #430
Base main at task start: `3b9ed65f9d5016fb8997e0ee93ad22f6dda09b17`
Authority version: `r150-late-field-energy-1`
RDC usage: **0**
HTDT-Capture changes: **0**

## Scope and authority boundary

Deterministic GA previously produced direct and specular reflected paths only.
This slice adds the bounded energy-side complement as a separate typed artifact —
`LateFieldEnergyArtifact` — that consumes the *exact* `DeterministicGaExecutionInput`
(and its region / Portal / boundary-termination / directivity / material authorities)
and emits `LateEnergyPathContribution` records. Specular deterministic identity is
not blurred: late-energy contributions are typed `surface_scattering` /
`edge_diffraction` / `aperture_diffraction`, never reissued as specular paths, and
the artifact carries its own bounds and provenance for the R160 `late_energy_decay`
observable to consume.

Primary implementation:

- `backend/src/htdt/cad_late_field_energy.py`
- `backend/tests/test_cad_late_field_energy.py`
- Native schema v12 (`cad_late_field_artifacts`) in `cad_schema_ddl.py` / `cad_schema.py`

## Kinds and bounds

| Kind | Resolution | Per-band bound |
| --- | --- | --- |
| `surface_scattering` | compiled triangle patch per region-boundary surface; occluded patch fails closed | `D * patch_area / (4*pi*d1_min^2) * (1-alpha)*s / (4*pi*d2_min^2)` over vertex-minimum distances |
| `edge_diffraction` (`wedge`) | declared edge resolving to exactly 2 incident non-coplanar triangles on 1 surface | `D * kappa / (4*pi*d1^2 * 4*pi*d2^2)`, endpoint-minimum distances, `kappa` = configured `diffraction_energy_bound_factor` |
| `aperture_diffraction` (`aperture_rim`) | declared edge resolving to exactly 2 incident non-coplanar triangles on 2 distinct surfaces (e.g. a floor/wall junction rim) | same bound; separation angle recorded |

Because compiled GA-ready geometry is watertight, a free single-incidence edge
cannot exist; `aperture_rim` is therefore defined over the two-surface junction
case. Diffracting edges are declared vertex pairs resolved against the compiled
mesh — never inferred — and the Fermat shortest-path apex is computed per
source/receiver with endpoint clamping recorded on the contribution.

Every contribution carries `incident_distance_bound_m`, `emergent_distance_bound_m`,
`late_energy_upper_bound_per_m2`, directivity evidence, material (scattering) or
`diffracting_edge` record (diffraction), and `apex_clamped_to_segment_endpoint`.
Identity hashes use the canonical pruned-None semantic-payload convention;
contributions order canonically by source -> receiver -> kind -> interaction
key -> contribution id.

## Fail-closed decisions

- `UNSUPPORTED_GEOMETRY`: Portal / multi-region geometry policies.
- `UNSUPPORTED_BOUNDARY_QUANTITY`: absent or unsupported boundary material model.
- `UNSUPPORTED_DIRECTIVITY`: resolved dataset does not cover the departure direction.
- `BLOCKED_VISIBILITY`: occluded source->patch segment (scattering).
- `UNDIFRACTING_EDGE`: coplanar incident faces, wrong incident-surface count, or a
  vertex pair that is not a compiled mesh edge.
- `EDGE_REQUIRES_HIGHER_ORDER_DIFFRACTION`: source/receiver->apex segment blocked by
  non-incident faces (incident triangles only are ignored).
- Region membership, exact-authority hash binding (execution input refs, compiled
  geometry identity), request/artifact observable binding in
  `build_late_field_result_envelope`, and `CadLateFieldEnergyArtifactRepository`
  persistence revalidation all fail closed.

## Explicit non-goals

Automatic crossover/stitching (R160 sibling slice), production solver validation,
stochastic ray tracing, and higher-order / multi-edge diffraction. Rejected
candidates are recorded with typed reason codes rather than silently degraded.

## Test coverage

`backend/tests/test_cad_late_field_energy.py` — 13 regression tests covering
analytic scattering bounds and ordering, deterministic re-execution identity,
wedge apex resolution with occluded-edge rejection, aperture-rim junction
diffraction, undiffracting declarations, occlusion and material/directivity
capability rejection, portal-policy and exact-authority mismatch fail-closed,
configuration canon, observable manifest/envelope binding, and repository
persistence/stale revalidation. `test_cad_schema.py` extends the migration-row
list to v12 per the established pattern.
