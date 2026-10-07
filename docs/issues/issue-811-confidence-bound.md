# Issue #811 — Solver confidence bounded by input authority

A high-fidelity solver with weak boundary/source/geometry inputs must
not inherit a high-confidence result. The module implements the
mechanical input-qualification rule from the issue:

```
result credibility
    <= weakest materially relevant input authority
    <= solver validation domain for that input/model class
```

There is no single universal score: the bound is per-claim/per-observable
and names the weakest input dimension for the operator.

Literature basis carried from the issue: boundary input parameters
dominate simulation-vs-measurement deviation even when solvers agree
with each other (Li 2022); boundary parameters are a major uncertainty
driver (Thydal 2021); absorption measurement reproducibility is bounded
(ISO 12999-2, Scrosati 2020); absorption→impedance conversion is a
modeled, non-unique step (Fratoni 2023); source-directivity choice
produces material differences in simulated results (Wang & Vigeant 2008);
calibration agrees only inside its calibration context (Pilch 2020) — a
fitted value is never measured truth; directivity format compliance is
container conformance, not acoustic quality (AES69); low-frequency
position perturbation must be evaluated against the modal gradient, not
treated as cosmetic tolerance.

## Sealed record types

Six append-only native tables (schema v87):

| Type | Table | id prefix |
| --- | --- | --- |
| `MaterialInputAuthority` | `cad_material_input_authorities` | `mia-` |
| `SourceDirectivityAuthority` | `cad_source_directivity_authorities` | `sda-` |
| `GeometryInputAuthority` | `cad_geometry_input_authorities` | `gia-` |
| `PoseInputAuthority` | `cad_pose_input_authorities` | `pia-` |
| `SolverInputEnvelope` | `cad_solver_input_envelopes` | `sie-` |
| `ClaimBoundRecord` | `cad_claim_bound_records` | `cbr-` |

Every record seals on `canonical_sha256(identity_payload())` (id+sha
excluded); ids are `<prefix>-<sha[:24]>`. Envelope refs pin `ref_id` +
`ref_sha256`; resolution is integrity-checked, never positional.

### `MaterialInputAuthority` (boundary/material evidence class)

`authority_class` is declared provenance — measured, manufacturer,
fitted and derived classes never collapse into each other:

- `measured_complex_boundary` — complex impedance/admittance/reflection
  measured by a declared method (ISO 10534-2, full complex reflection,
  in-situ), with bound evidence refs and a valid frequency band.
- `measured_parameterized_model`, `manufacturer_laboratory_exact`
  (laboratory identity + usable uncertainty required),
  `measured_energy_coefficient` (ISO 354-family energy coefficient).
- `manufacturer_declared`, `nonidentical_construction` (provenance note
  required), `fitted_effective` (calibration context required — a fitted
  value stays an effective calibrated parameter, never measured truth).
- `derived_conversion` — must pin a `BoundaryConversionArtifact` ref
  (model, assumptions, priors, uncertainty are part of the claim).
- `scalar_absorption_only`, `generic_table_lookup`,
  `inferred_construction`, `undeclared`.

`fitted_context` is structurally forbidden on non-fitted classes — a
calibrated value cannot be relabeled measured. Scattering authority is
only `scattering_coefficient_random_incidence` on a measured
energy-coefficient class; directional diffusion is never scattering.

### `SourceDirectivityAuthority` (directivity provenance)

`provenance_class` (`measured_balloon`, `manufacturer_dataset`,
`fitted_source_correction`, `inferred_estimated`, `simplified_model`,
`undeclared`) is kept strictly separate from `format_compliance`
(`sofa_aes69`, `normalized_directivity_json`, `proprietary`) — a valid
container file is never acoustic quality validation. Coverage, angular
and frequency sampling, interpolation, coordinate frame, normalization
and operating state are retained. Simplified models must name their
model kind (`omnidirectional`, `point_source`, `cardioid_family`).

### `GeometryInputAuthority` (scene fidelity)

`fidelity_class`: `exact_scene_revision` / `asbuilt_measured` (scene
revision ref required; as-built additionally needs a deviation bound or
uncertainty ref), `surveyed_partial`, `simplified` (simplification
policy required — what was dropped is part of the claim),
`assumed_nominal`, `undeclared`.

### `PoseInputAuthority` (placement per subject)

`authority_class`: `surveyed_acoustic_center` (source-origin authority +
position bound required), `measured_position`, `intended_placement`,
`assumed`, `undeclared`. `modal_sensitivity` records whether a position
perturbation was evaluated against the local modal gradient — at low
frequency a position error is modal sensitivity, not a cosmetic
tolerance.

### `SolverInputEnvelope` (binder)

Binds one solver request to the sha-pinned refs of every input-authority
record plus `EnvironmentalBinding`s (temperature, humidity,
speed of sound, door/opening state, movable objects, playback state,
time variance). Environmental claims are dependency-aware: an
undeclared aspect stales only the claims that materially depend on it.

### `ClaimBoundRecord` (the verdict)

One row per claim class, exactly once (all ten `CLAIM_CLASSES`),
evaluated by `evaluate_input_envelope`/`evaluate_claim_bound`.

## Verdict vocabulary

| Verdict | Meaning |
| --- | --- |
| `envelope_inherited` | all materially relevant inputs meet the claim rule; the result inherits the solver validation domain (`full_envelope` ceiling) |
| `solver_bounded` | solver validation domain is the weakest evidence (`solver_bound`) |
| `bounded_by_input` | claim permitted at the weakest input's ceiling (`measured_bound` / `documented_bound` / `limited_bound` / `derived_bound` / `assumed_bound`) |
| `claim_denied` | a declared input authority cannot support this claim class at any level (e.g. scalar absorption feeding a phase-coherent claim) |
| `solver_unqualified` | no accuracy envelope covers the claim's observable, or the capability manifest marks its phenomenon UNSUPPORTED |
| `unbounded_input` | a materially relevant input authority is undeclared — `insufficient_authority`, never a mid-trust default |

Gate order: `unbounded_input` → `claim_denied` → `solver_unqualified` →
`bounded_by_input`/`solver_bounded`/`envelope_inherited`. Every non-
inherited verdict names its `weakest_dimensions`
(`material_boundary`/`source_directivity`/`geometry`/`pose`/
`environment`/`solver_domain`).

## Claim classes and requirement rules

Ten claim classes (`CLAIM_CLASSES`): `absolute_level_at_position`,
`local_frequency_response`, `spatial_coverage`,
`early_reflection_structure`, `modal_response`, `decay_time`,
`late_diffuse_field`, `phase_coherent_field`, `speech_intelligibility`,
`hybrid_overlap_consistency`. `_CLAIM_REQUIREMENTS` declares, per claim,
which authority classes support/bound/deny it per dimension and which
environmental aspects it needs — the matrix is inspectable and declared,
not derived.

Notable rules: `phase_coherent_field` requires measured complex boundary
authority; `late_diffuse_field` demotes material support when no ISO
17497-1 scattering evidence exists; modal-sensitive claims
(`local_frequency_response`, `modal_response`, `phase_coherent_field`)
demote `pose` when `modal_sensitivity` is `unevaluated`; derived
conversions and scalar absorption bound at `derived_bound`/`assumed_bound`
where they participate at all.

## Integration points

- `acoustic_validation_envelope.AccuracyEnvelopeRecord` — the solver-side
  ceiling: the claim's observable must have a matching envelope; the
  weakest matching envelope's `validation_state` sets the solver-domain
  rank. A claim with no covering envelope is `solver_unqualified`.
- `cad_solver_capability_manifest.SolverCapabilityManifest` — a second,
  weaker solver authority: `SUPPORTED`/`BOUNDED` contribute
  `documented`/`limited` ranks (capability, not accuracy validation) and
  `UNSUPPORTED` vetoes the claim.
- `cad_material_evidence_compatibility` vocab (`BoundaryPhysicalQuantity`,
  `EvidenceMethodClass`, `EvidencePhase`, `EvidenceIncidence`) and
  `BoundaryConversionArtifact` (pinned by `conversion_ref`) are reused —
  no new provenance vocabulary is invented.
- `cad_directivity_admission.DirectivityCoverage` is reused for the
  directivity coverage axis.
- JA labels: `CONFIDENCE_BOUND_LABELS` covers every verdict, ceiling,
  dimension and authority class; `measurement_evidence_display` exposes
  `confidence_bound_verdict_line`/`confidence_bound_label`; lifecycle
  table labels are registered for all six tables.
- Native audit: `cad_solver_confidence_bound_repository` is reachable
  via `_RepositoryChain` name `solver_confidence`; all six tables have
  `_ReplayProbe`s and `_ROW_BINDINGS`.

## Non-goals

No new material fitting algorithms; no mandatory lab impedance for
every project; lower-confidence predictions are permitted and labeled —
they are never silently upgraded; inferred values are never presented as
measurements; UI surfaces are not rewired — downstream surfaces consume
the sealed `ClaimBoundRecord`.
