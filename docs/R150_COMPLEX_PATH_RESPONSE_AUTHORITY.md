# R150 frequency-dependent complex path response authority

Status: **software vertical slice implemented; per-path physically normalized complex response authority is available. R160 numerical composition remains intentionally unimplemented.**

Issue: #101  
Draft PR: #276  
Base main at task start: `49901a6b46ad889bd06b34e4482a5b6121545d1d`  
Response authority: `r150-complex-path-response-1`  
RDC usage: **0**  
HTDT-Capture changes: **0**

## Scope and authority boundary

The existing `DeterministicPathArtifact` remains the geometry-only R150 evidence and continues to declare
`coherent_phase_authority=UNAVAILABLE_NOT_SYNTHESIZED`. This slice does **not** mutate that contract or infer
phase from path length alone.

A separate immutable `DeterministicPathFrequencyResponseArtifact` now binds one exact deterministic path and its
exact `DeterministicGaExecutionInput` to explicit source, receiver, environment, material/boundary,
Portal-transfer, frequency-grid, and response configuration authorities. It is deliberately **per-path only**. No path summation or R160 wave+GA stitching is
performed here.

Primary implementation:

- `backend/src/htdt/cad_geometric_acoustics_response.py`
- `backend/tests/test_cad_geometric_acoustics_response.py`
- `.github/workflows/r150-complex-path-response.yml`

The existing R150 path geometry kernel and adapter are unchanged.

## Physical normalization

The coherent transfer contract is:

| Field | Authority |
| --- | --- |
| quantity | `complex_acoustic_pressure_per_volume_velocity` |
| unit | `Pa/(m3/s)` |
| source normalization | unit volume velocity, `Q=1 m3/s` |
| phasor convention | `exp(+i*omega*t)` |
| angular frequency | `omega=2*pi*f` |
| outgoing Green function | `G(r)=exp(-i*k*r)/(4*pi*r)` |
| wavenumber | `k=omega/c` |
| time origin | `source_t0` |
| geometric spreading | absolute spherical `1/(4*pi*r)`, no arbitrary reference distance |
| point-monopole transfer | `p/Q = i*omega*rho*G(r)` |
| path aggregation | `per_path_only_no_sum` |

Density `rho`, sound speed `c`, environmental validity, and the exact frequency grid are hashed dependencies.
A persisted path delay must reproduce `path_length/c` under the selected environment. A different environment
does not silently reuse the old coherent response.

The analytic monopole is **not** a default speaker model. It exists only through an explicit
`PointSourceNormalizationAuthority` and an explicit supported analytic directivity capability.

## Source capability matrix

| Source evidence | Per-path result |
| --- | --- |
| exact complex `DirectivityDataset`, `on_axis_per_frequency` normalization, aligned volume-velocity phase reference + explicit point-source normalization | complex supported |
| explicit analytic `point_volume_velocity_monopole_omnidirectional_v1` with coherent phase reference + point-source normalization | complex supported |
| exact magnitude-only directivity with `on_axis_per_frequency` normalization | magnitude-only; phase/real/imag absent |
| polar summary / unknown directivity | unsupported |
| declared imported directivity with missing dataset | unsupported |
| dataset / EquipmentDefinition identity or tier mismatch | rejected |
| imported directivity using `explicit_reference_level` | unsupported for this point-source ratio contract |
| missing absolute point-source normalization | unsupported for `Pa/(m3/s)` |

For imported complex directivity, the existing R110/O100C evaluator is reused with
`request='complex'`. Its interpolation/provenance rules remain authoritative. This response layer requires
`on_axis_per_frequency` normalization before treating directivity magnitude as a dimensionless multiplicative
point-source directional ratio; `explicit_reference_level` is not assumed compatible with unit volume velocity.
Magnitude-only data is never
promoted by setting phase to zero, and unknown directivity is never replaced by an omnidirectional response.

## Reflection transfer authority

Each reflected path requires one exact `SurfaceReflectionTransferAuthority` per ordered surface interaction.

| Boundary evidence | Capability / rule |
| --- | --- |
| mathematically exact rigid pressure boundary | complex `R=+1` |
| exact specific-impedance sample | complex local-reaction `R=(Z*cos(theta)-rho*c)/(Z*cos(theta)+rho*c)` |
| explicit complex reflection transfer | complex, exact supplied samples |
| geometric absorption/scattering only | magnitude-only `sqrt((1-alpha)*(1-s))`; phase unavailable |
| missing / stale / out-of-band authority | unsupported |

Specific impedance requires an explicit incidence cosine and an exact impedance sample at every requested
response frequency. No complex coefficient is inferred from scalar absorption. First- and second-order response
uses the deterministic path's **ordered** surface sequence and multiplies coefficients in that order.

Every reflection authority binds the exact R120 compiled geometry reference, material authority, and optional
boundary-physics authority. An old response cannot be current after its surface geometry/material authority
changes.

## Portal acoustic transfer authority

`PortalAcousticTransferAuthority` is solver-neutral and immutable. It records:

- exact Portal id;
- explicit from-region / to-region direction;
- exact Portal geometry authority ref;
- physical quantity `complex_pressure_transmission_ratio`;
- dimensionless complex transmission samples;
- exact valid frequency band;
- provenance text;
- provenance state: measured / manufacturer / analytic / assumed;
- uncertainty statement;
- schema/version/hash;
- analytic approximation version/tolerance when applicable.

An open Portal in R120 geometry **does not imply** transmission `1+0i`. If a free-field-like open-aperture unity
approximation is desired, it must be constructed explicitly with
`build_open_aperture_unity_transfer_authority`, including approximation version, tolerance/validity statement,
and provenance.

For a multi-Portal direct path, the exact ordered Portal interactions from the geometry artifact select directed
transfer authorities and their complex coefficients are multiplied in traversal order. Missing, stale,
direction-mismatched, or out-of-band Portal transfer evidence makes coherent response unsupported; it is never
represented as zero.

## Per-path output artifact

`DeterministicPathFrequencyResponseArtifact` carries at least:

- exact deterministic path artifact id/hash;
- exact deterministic path id/hash;
- source / receiver refs;
- ordered reflection-surface identities;
- ordered Portal identities;
- exact geometric path length;
- physical quantity / unit / source normalization;
- phasor convention / time origin;
- density / sound speed;
- exact frequency grid and valid band;
- per-frequency propagation phase and geometric spreading;
- per-frequency magnitude;
- complex phase / real / imaginary values only when coherent capability is supported;
- capability state: `COMPLEX_SUPPORTED`, `MAGNITUDE_ONLY`, or `UNSUPPORTED`;
- explicit unsupported reasons;
- canonically sorted exact dependency refs.

`UNSUPPORTED` artifacts carry **no fabricated response samples**. Unsupported is not equivalent to a zero
transfer function.

## Fail-closed conditions

The builder rejects or downgrades without fabricating coherent phase for:

- unknown source directivity;
- magnitude-only source directivity requested as coherent;
- scalar absorption/scattering without phase authority;
- missing reflection authority;
- missing Portal transfer authority;
- response/material/source/environment valid-band mismatch;
- source identity or exact R110 compiled-source hash mismatch;
- receiver identity or world-position mismatch;
- stale/mismatched deterministic GA execution input;
- execution-input R120/Portal authority mismatch;
- execution-input environment/path-delay mismatch;
- stale R120 geometry binding;
- stale reflection/surface authority;
- stale Portal-transfer geometry authority;
- missing absolute source normalization;
- zero or near-singular positive path length below the declared minimum;
- any dependency that no longer re-resolves to its exact id/version/hash.

The accepted input type remains the deterministic R150 artifact; stochastic ray artifacts are not accepted.
Current deterministic interactions are strictly reflection or Portal crossing, so unsupported
scattering/diffraction interactions cannot be silently interpreted by this layer.

## Persistence and stale rejection

`CadPathFrequencyResponseRepository` is append-only. Save/reopen re-resolves the exact response dependency graph
and regenerates the artifact before accepting it as current.

The dependency set includes, as applicable:

- deterministic path artifact and exact path;
- exact `DeterministicGaExecutionInput` used to generate that path;
- R120 compiled geometry;
- R110 source authority;
- EquipmentDefinition;
- DirectivityDataset;
- explicit point-source normalization;
- receiver authority;
- environment;
- exact frequency grid;
- response configuration;
- surface reflection authorities;
- material authorities;
- boundary-physics authorities;
- Portal geometry authority;
- Portal transfer authorities.

If any dependency is missing or resolves to a different exact id/version/hash, the persisted response is rejected
rather than replayed as current.

## Numerical references and verification fixtures

Focused numerical/authority tests cover:

1. **Free-field direct** — independent evaluation of
   `i*omega*rho*exp(-i*k*r)/(4*pi*r)` agrees in real, imaginary, and magnitude components.
2. **Rigid-plane first reflection** — image-source path length with exact `R=+1` agrees in magnitude and phase.
3. **Specific complex impedance** — local-reaction reflection agrees with an independently evaluated complex
   impedance coefficient.
4. **Second order** — two explicitly complex surface coefficients reproduce the deterministic ordered product
   and exact path phase.
5. **Magnitude-only source** — magnitude is retained while phase/complex components remain absent.
6. **Scalar absorption-only surface** — magnitude-only response is produced; no reflection phase is synthesized.
7. **Out-of-band / unknown source capability** — coherent response is unsupported.
8. **Missing Portal transfer** — open/multi-Portal geometry remains valid but coherent response is unsupported.
9. **Explicit multi-Portal transfer** — ordered complex transmission product is deterministic; changing one
   transfer authority changes response identity.
10. **Environment / near-singularity checks** — delay mismatch and near-zero path length fail closed.
11. **Persistence** — exact save/reopen succeeds; removal of a dependency causes stale rejection.
12. **Identity mismatch** — source, receiver, and stale R120 surface binding fail closed.
13. **Stale Portal authority** — transfer bound to a different Portal authority fails closed.
14. **Coherent self-consistency** — complex phase must agree with real/imag and the response grid must remain inside the exact path-artifact band.
15. **Source phase origin** — a coherent directivity phase reference not aligned to `source_volume_velocity_t0` fails closed.
16. **Directivity normalization** — `explicit_reference_level` is not silently treated as a point-source directional ratio.
17. **Complex directional transfer** — an exact on-axis-normalized complex DirectivityDataset contributes its explicit phase to the point-source transfer.
18. **Execution-input binding** — stale execution-input identity, R110 source hash mismatch, receiver position mismatch, execution-environment mismatch, and Portal-authority mismatch fail closed.

The dedicated workflow also runs the complete existing
`backend/tests/test_cad_geometric_acoustics_adapter.py` suite, covering existing direct, first-order,
second-order, single-Portal, and arbitrary region-graph / multi-Portal geometry regressions.

The focused response suite contains 18 numerical/authority tests. The exact final-head workflow result, together
with the existing R150 geometry regression result, is recorded in Draft PR #276.

## Valid-band semantics

Frequency grids are exact sorted authorities. Explicit reflection and Portal transfer samples are looked up at
the requested exact frequencies; the response layer does not invent interpolation for those authorities.
Specific impedance similarly requires exact samples. Directivity interpolation, when allowed, remains delegated
to the existing directivity evaluator and its recorded interpolation provenance.

This keeps each complex sample traceable to an explicit physical authority and avoids hidden extrapolation.

## R160 handoff

The next R160 slice may consume these per-path response artifacts together with exact R130 wave pressure
artifacts, but must separately define and validate:

- compatible `Pa/(m3/s)` source excitation/reference between R130 and R150;
- common phasor/time-origin semantics;
- compatible environment/frequency grids;
- coherent path summation policy;
- hybrid wave/GA overlap and crossover policy;
- numerical reference fixtures for the combined response.

This PR intentionally adds **no** path summation and **no** wave+GA numerical stitching.

## Non-claims

Not implemented or claimed here:

- stochastic ray tracing;
- late reverberation / RT60 / EDT;
- scattering;
- diffraction;
- automatic material inference;
- automatic Portal transmission inference;
- broadband room-response summation;
- R160 wave+GA numerical composition;
- production solver adoption;
- owned-room validation.
