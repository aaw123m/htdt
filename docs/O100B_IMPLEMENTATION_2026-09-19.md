# O100B implementation record — Virtual topology / placement search

Date: 2026-09-19  
Tracking: Issue #142 / Issue #147

## Implemented scope

O100B adds deterministic placement search for speakers that exist only as O100A `ProposedEntitySpec` entries.

Implemented authority objects and interfaces:

- `ProposedPlacementSpec`
  - exact proposed entity / role binding;
  - named installation zone;
  - allowed XY region;
  - optional height bounds;
  - deterministic XYZ grid axes;
  - acoustic aim yaw/pitch axes;
  - physical cabinet yaw/toe-in axis.
- `LinkedPlacementRule`
  - explicit master/slave relationship;
  - mirror X;
  - equal X/Y/Z;
  - equal-delta X/Y/Z through existing O10 semantics.
- `TopologyPlacementSearchSpec`
  - exact baseline `SceneRevision`;
  - exact O100A template `SystemVariant`;
  - exact role/zone/placement inputs;
  - exact G10 and O10 authority snapshots;
  - deterministic semantic hash and search identity.
- `TopologyPlacementCandidate`
  - exact XYZ;
  - acoustic aim yaw/pitch;
  - physical cabinet yaw;
  - exact O10 candidate provenance;
  - deterministic content-derived candidate identity and ordering.
- `CadTopologySearchRepository`
  - immutable search-spec persistence;
  - candidate persistence;
  - candidate -> O100A `SystemVariant` lineage;
  - identity-only comparison reference for downstream O30/O40 comparison.

## Baseline SceneRevision remains immutable during search

O100B does **not** save a temporary proposed `SceneRevision`.

The search path is:

1. load the exact baseline `SceneRevision`;
2. materialize the O100A template `SystemVariant` in memory;
3. expose that virtual scene to existing G10/O10 services;
4. generate and filter placement candidates;
5. materialize a candidate only as an in-memory `SceneDocument`;
6. convert a selected candidate to a new immutable O100A `SystemVariant`;
7. persist/apply through existing O100A authority only when explicitly requested.

This keeps current/as-built state unchanged while exploring proposed topology.

## G10 authority reuse

O100B does not introduce a second placement hard-constraint engine.

Existing G10 authority is reused for:

- exact room bounds;
- per-role/per-proposed-entity allowed installation regions;
- inherited exclusion regions;
- inherited wall clearance;
- inherited pair distance;
- proposed height bounds through existing axis-range semantics;
- linked/symmetric placement relations.

O100B translates its role/zone specification into the existing G10 request model and stores the validated G10 semantic hash in `TopologyPlacementSearchSpec`.

## O10 deterministic candidate semantics reuse

O100B calls the existing O10 deterministic grid service directly against the in-memory proposed scene.

Existing O10 semantics are reused for:

- Decimal-backed grid stepping;
- deterministic axis ordering;
- deterministic raw enumeration;
- linked derivation;
- G10 feasibility evaluation;
- rejection accounting;
- duplicate handling;
- base candidate identity.

O100B then deterministically expands each feasible O10 XYZ candidate by explicitly declared orientation/aim axes.

No opaque topology score is introduced.

## Linked / symmetric pair placement

SL/SR-style pair placement is expressed as explicit linked constraints.

For example:

- SL X is searched;
- SR X is derived by `mirror_x` around the declared mirror plane;
- SR Y is derived by `equal_y`;
- SR Z is derived by `equal_z`.

The derived coordinates are still evaluated by G10. Pairing metadata remains in O100A `ChannelRoleBinding`; geometric derivation remains O10/G10 authority.

## O80 orientation-aware semantics reuse

Physical cabinet yaw/toe-in is applied through the existing O80 body-yaw preview semantics.

That preserves the established distinction between:

- `body_yaw_deg`: physical cabinet orientation, including coupled acoustic-axis rotation;
- `aim_yaw_deg`: acoustic aim yaw independent of cabinet pose.

After body yaw is applied, existing O80 `orientation_constraint_rejections()` rechecks exact oriented XY envelopes against:

- room boundary;
- allowed region;
- exclusion region;
- wall clearance;
- envelope pair-distance constraints.

O100B does not duplicate those calculations.

Acoustic aim pitch is an explicit geometric direction axis in O100B. It changes only `aim_xyz`; it does not claim any directivity, SPL, or source-model capability.

## 3.0.2 -> proposed 5.0.2

The targeted acceptance fixture starts from a current 3.0.2-like `SceneRevision`:

- FL / C / FR;
- TFL / TFR;
- no SL / SR.

An O100A template adds SL and SR as `proposed`, with reciprocal paired-role metadata.

O100B then searches SL inside a left-side installation zone and derives SR through mirror/equal pair constraints into a right-side installation zone.

The resulting candidate remains proposal-only until converted to an immutable child `SystemVariant`.

## Candidate -> immutable SystemVariant

`topology_candidate_to_system_variant()`:

- keeps the exact O100A baseline;
- keeps O100A role bindings;
- keeps remove/replace topology operations;
- keeps current/as-built/measured lifecycle overrides;
- preserves existing proposal evidence references;
- replaces proposed speaker payloads with exact candidate pose/aim;
- records exact O100B search/candidate hashes in provenance;
- sets the O100A template as `parent_variant_id`;
- derives a deterministic candidate-variant ID.

The output is still validated by O100A `build_system_variant()` and `materialize_system_variant()`; O100B does not create a parallel variant authority.

## Persistence and comparison interface

`CadTopologySearchRepository` stores:

- `cad_topology_search_specs`;
- `cad_topology_placement_candidates`;
- `cad_topology_candidate_variants`.

Foreign keys bind O100B records to existing O100A variants and existing SceneRevision authority.

The comparison interface returns identities/hashes only. Objective vectors, Pareto dominance, scoring, prediction, and robustness remain owned by their existing authorities.

## Capability and evidence boundary

O100B does **not** implement O100C EquipmentDefinition/source capability.

Therefore O100B:

- does not invent directivity;
- does not invent sensitivity;
- does not invent SPL/headroom;
- does not attach measurement evidence to proposed speakers;
- does not treat geometric aim as evidence that an acoustic model supports directional prediction.

A candidate may carry pose/aim even when source capability is unknown. Any later objective requiring unsupported source capability must remain unavailable until O100C supplies explicit authority.

## Explicit non-scope / remaining work

Not implemented in O100B:

- EquipmentDefinition / source/directivity/SPL capability — O100C;
- topology/objective comparison or new objective semantics;
- O30/O40 Pareto reimplementation;
- O90 robustness reimplementation;
- automatic equipment alternatives;
- opaque combined topology/placement score;
- GUI / workflow-shell integration;
- As-built promotion or measurement workflow.

No O90 or UX files are changed by this slice.

## Targeted verification

`backend/tests/test_cad_topology_search.py` covers:

- current 3.0.2-like baseline -> proposed 5.0.2 with SL/SR;
- reciprocal SL/SR role pairing;
- independent left/right allowed installation zones;
- mirror X + equal Y/Z linked pair placement;
- height hard constraints;
- acoustic aim yaw/pitch;
- deterministic rebuild, candidate ordering, identity, and candidate-set hash;
- no temporary/baseline SceneRevision mutation;
- candidate -> immutable O100A `SystemVariant`;
- proposed lifecycle retains no measurement evidence;
- O100B persistence and comparison-reference integration;
- candidate tamper detection;
- physical cabinet yaw reusing O80 exact oriented allowed-region rejection.

CI results are recorded in the pull request after GitHub Actions completes.
