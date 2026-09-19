# O100 — System Expansion / Virtual Channel Topology Optimization

> Status: **O100A–O100E implemented / O100F–O100G planned** — tracking: [Issue #142](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/issues/142) / implementations: PR #144, #150, #168/#198/#206/#212, #169/#214/#215, #228
>
> O100 extends the existing N40 / G10 / O10–O90 / R-series authority. It does not replace them.
>
> Purpose: allow HTDT to answer questions such as:
>
> > “The current room has no SL/SR. Add virtual SL/SR, define where they may be installed, evaluate feasible placements, compare multiple system layouts, and determine which candidates are worth installing and measuring.”

## 1. Product goal

HTDT must support not only **optimization of objects that already exist**, but also **design-space expansion**.

Examples:

- current 3.0.2 → proposed 5.0.2 by adding SL/SR;
- current 5.0.2 → proposed 7.0.2 by adding SBL/SBR;
- compare one pair of wall-mounted surrounds vs another installation zone;
- compare two explicitly supplied speaker models for a new role;
- add a second subwoofer later and search its allowed region;
- compare “no added speaker” against one or more expansion variants;
- after installation, preserve the original proposal and create an As-built/Measured state.

The system must never pretend that a proposed speaker already exists physically or has been measured.

## 2. Distinguish four questions

O100 separates:

1. **Topology**
   - which roles/entities exist in the candidate system?
   - example: 3.0.2 vs 5.0.2.

2. **Equipment/source definition**
   - what physical/acoustic model represents each proposed speaker?

3. **Placement**
   - where may each proposed entity be installed?
   - height, orientation and aim are part of this search.

4. **Performance**
   - how does each exact topology/equipment/placement variant perform for supported objectives?

A result is not a valid O100 candidate unless all four layers can be traced exactly.

## 3. Relationship to existing authority

### N40

N40 already allows arbitrary speaker roles; 3.0.2 is a template/fixture, not a fixed schema.

O100 reuses this capability but adds proposal lifecycle and variant semantics.

### G10 / O10

Reuse:

- allowed region;
- exclusion region;
- wall clearance;
- cabinet clearance;
- pair distance;
- linked placement;
- axis/height bounds;
- movement/installation constraints.

O100 must not implement a second placement-constraint engine.

### O30 / O40

Reuse independent objective vectors and Pareto comparison.

Topology variants are never reduced to one hidden “system score”.

### O80

O80 remains the authority for supported extended placement parameters such as:

- acoustic aim;
- physical cabinet yaw/toe-in;
- height / multi-entity search when supported.

O100 supplies proposed entities/topologies to O10/O80; it does not duplicate extended-search logic.

### O90

O90 evaluates robustness of an exact O100 candidate against installation/input tolerances.

O100 asks:
> What system/placement variants should exist?

O90 asks:
> How sensitive is this exact candidate to realistic error?

### R-series

R110+ supplies source/directivity/material/environment capability.
R170/R180 determine which acoustic objectives are valid for production decisions.

O100 cannot unlock unsupported physics.

## 4. Lifecycle state

Every physical-system entity participating in a design decision has an explicit lifecycle state.

Initial states:

- `current`
  - entity exists in the currently modeled working system;
- `proposed`
  - hypothetical entity that does not yet exist physically;
- `as_built`
  - installed physical entity with recorded actual pose/configuration;
- `measured`
  - as-built entity/system with bound measurement evidence.

A UI badge may use natural Japanese labels, but persistence uses stable semantic states.

Rules:

- adding an SL/SR proposal does not mutate the current/as-built scene;
- a proposed entity has no physical measurement merely because its model has manufacturer data;
- installing a proposal creates a new As-built revision; it does not rewrite the old proposal;
- measured evidence binds to the exact As-built SceneRevision/AcquisitionContext;
- proposal → installed → measured is an append-only lineage.

## 5. Core authority objects

### 5.1 SystemVariant

Immutable record describing one complete candidate system.

Minimum identity:

- base SceneRevision/content hash;
- variant ID/name;
- parent variant if any;
- active channel-role set;
- proposed/current/as-built entity bindings;
- EquipmentDefinition/source-model bindings;
- signal/routing assumptions where required;
- constraint authority;
- exact placement candidate identity;
- prediction/model/objective identity;
- algorithm/version provenance.

Examples:

- `Current 3.0.2`;
- `5.0.2 / SL-SR side-wall zone A`;
- `5.0.2 / SL-SR rear-biased`;
- `5.0.2 / Speaker Model B`.

### 5.2 ProposedEntitySpec

Defines an entity not present in the baseline.

Stores:

- entity type;
- role;
- lifecycle=`proposed`;
- physical dimensions/envelope;
- acoustic reference definition;
- EquipmentDefinition/source model reference;
- placement authority;
- orientation/aim bounds;
- optional linked-pair/group relation;
- provenance and uncertainty.

A proposed entity is not stored as a fake measured speaker.

### 5.3 ChannelRoleBinding

A role is not inferred only from the entity label.

Store:

- stable role ID;
- user-facing display name;
- layout/profile authority if one is selected;
- source/routing semantics;
- paired/group relationship;
- optional angular/layout criterion reference;
- profile/version provenance.

Examples may include `SL`, `SR`, `SBL`, `SBR`, but the persistence model is not limited to a fixed hard-coded list.

### 5.4 EquipmentDefinition

O100 depends on an explicit equipment/source record rather than a product-name string.

Required/optional capability fields include:

- manufacturer/model/user-defined identity;
- cabinet dimensions;
- acoustic reference;
- directivity data/capability;
- valid frequency band;
- sensitivity/reference level if known;
- SPL/headroom data if known;
- mounting/clearance metadata;
- data source/version/hash;
- measured / manufacturer / inferred / analytic state;
- uncertainty.

Missing data remains missing.

## 6. Source/directivity capability

For a proposed SL/SR, HTDT may have:

1. **complex directional source data**
   - supports the strongest coherent directional claims within validated range;

2. **magnitude-only directivity**
   - may support coverage/magnitude objectives but not fabricated coherent phase;

3. **summary/polar data**
   - limited objectives;

4. **analytic/generic source model**
   - explicitly approximate;

5. **unknown**
   - objectives requiring directivity remain unavailable.

The UI must say what the proposal is based on.

Example:

> 「この候補は汎用90°指向モデルによる概算です。実機の位相・最大SPLは評価していません。」

## 7. LayoutProfile / standards relationship

O100 introduces the concept of a versioned `LayoutProfile` interface.

A profile may provide:

- known channel roles;
- placement-angle regions;
- height/elevation criteria;
- pairing/symmetry rules;
- optional seat/reference-point definitions;
- source/reference citation and version.

Examples may later include Dolby or RP22-derived evaluation profiles where the available source/rights permit.

Important:

- a LayoutProfile is an **evaluation/constraint source**, not physical truth;
- user-defined layout remains possible;
- no profile silently moves speakers;
- compliance is not the same as acoustic optimum;
- failing one profile criterion does not delete the candidate unless the user made it a hard constraint.

## 8. Search-space definition

O100 search starts from one or more proposed roles.

For each proposed speaker/entity:

- allowed region;
- exclusion region;
- minimum/maximum height;
- wall/ceiling/furniture clearance;
- cabinet envelope;
- orientation/yaw limits;
- acoustic aim limits;
- role-specific angular region if selected;
- distance limits;
- linked placement relation;
- installation-zone choice;
- equipment alternative choice if enabled.

Example SL/SR definition:

```text
SL:
  allowed zone: left-side wall region
  height: 1.1–1.6 m
  wall clearance: >= 0.10 m
  aim yaw: -20° … +20° relative to seat target

SR:
  derived by mirror relation from SL

pair:
  same model
  linked height
  optional left/right geometric symmetry
```

This definition is immutable for a search run.

## 9. Topology generation

O100 must not blindly enumerate every conceivable speaker configuration.

A `TopologySearchSpec` explicitly lists allowed topology operations.

Initial operations:

- add role/entity;
- remove proposed role/entity;
- replace equipment binding;
- choose one of explicit installation zones;
- activate/deactivate an explicitly defined optional pair/group.

Examples:

```text
baseline: 3.0.2
allowed variants:
  - baseline unchanged
  - + SL/SR
```

or:

```text
baseline: 5.0.2
allowed variants:
  - current
  - + SBL/SBR
  - replace SL/SR Model A with Model B
```

HTDT does not invent arbitrary channel layouts unless the user/profile explicitly places them in the topology search space.

## 10. Candidate identity

A candidate includes all decision dimensions that affect semantics.

At minimum:

- topology variant;
- role set;
- exact equipment binding;
- exact XYZ;
- physical orientation;
- acoustic aim;
- linked-pair derivation;
- constraint-set identity;
- layout-profile identity;
- solver/prediction authority;
- objective specification.

Changing equipment model at the same coordinates creates a different candidate.

Changing only camera/view does not.

## 11. Objective families

O100 reuses O30 objective semantics and only enables objectives backed by current capability.

### 11.1 Geometric/layout objectives

Possible without full acoustic solver if the required geometry is known:

- role angular placement;
- distance;
- height/elevation;
- symmetry;
- physical collision/clearance;
- installation-zone feasibility;
- cable/install distance later if authority exists.

### 11.2 Coverage objectives

Require adequate directivity authority.

Examples:

- per-seat on-axis/off-axis angle;
- useful coverage fraction;
- worst-seat directivity loss;
- seat-to-seat coverage spread;
- MLP vs secondary-seat trade-off.

### 11.3 SPL / headroom objectives

Require explicit source/equipment capability.

Examples:

- worst-seat target SPL margin;
- continuous/peak headroom where source data supports it;
- level variation across seats.

Unknown max SPL/sensitivity means the corresponding objective is unavailable.

### 11.4 Acoustic-response objectives

Use R-series/O30 authority when supported:

- FR/target deviation;
- peaks/nulls;
- seat variation;
- early reflections;
- decay/other observables only when validated.

### 11.5 Installation/complexity objectives

May include independently:

- number of added speakers;
- displacement/installation distance;
- mounting complexity;
- cable length estimate;
- equipment count/cost only if explicit user data exists.

Do not mix these into acoustic quality.

## 12. Multi-channel semantics

O100 must not compare different topologies using an undefined multi-channel sum.

Separate:

1. **per-channel/source transfer**
   - evaluate each role/source independently where appropriate;

2. **defined excitation scenario**
   - exact routing/gain/delay/filter/content assumption is stored;

3. **program-format/layout criteria**
   - evaluated as geometry/profile compliance, not as an invented acoustic waveform.

For coherent sums, phase/routing/timing must be known.
For unrelated program channels, HTDT must not create an arbitrary coherent sum and call it “system FR”.

## 13. Search strategy

Topology × equipment × placement can explode combinatorially.

Recommended staged pipeline:

```text
explicit topology variants
  -> geometric hard constraints
  -> layout/profile feasibility
  -> cheap coverage/directivity screening
  -> nominal acoustic screening
  -> O30/O40 Pareto shortlist
  -> common-fidelity acoustic reevaluation
  -> O90 robustness on surviving variants
  -> proposed installation / measurement plan
```

Rules:

- never use unsupported cheap models to eliminate a candidate from a stronger physics objective without a validated screening relationship;
- keep audit samples where screening is approximate;
- final compared candidates use compatible objective definitions and fidelity;
- budget-limited results remain preliminary.

## 14. Example: adding SL/SR

Baseline:

```text
Current system:
  FL / C / FR / TFL / TFR
  no SL / SR
```

O100 workflow:

1. create `SystemVariant: 5.0.2 proposal`;
2. add ProposedEntitySpec for SL/SR;
3. bind role definitions;
4. select or create EquipmentDefinition;
5. draw allowed installation zones in Room;
6. set height/clearance/aim/pair rules;
7. generate feasible O10/O80 placement candidates;
8. evaluate capability-gated objectives;
9. inspect Pareto candidates;
10. optionally run O90 robustness;
11. select one proposal;
12. apply it as a new proposed SceneRevision;
13. physically install;
14. record As-built pose/model;
15. create MeasurementPlan;
16. import REW evidence;
17. validate predictions against the exact installed state.

Baseline 3.0.2 remains available throughout.

## 15. SystemVariant comparison

The user must be able to compare:

- existing/current system;
- proposed topology A;
- proposed topology B;
- different equipment binding;
- different placement candidate;
- later, measured as-built system.

Comparison set keeps:

- exact SystemVariant;
- exact SceneRevision;
- source/equipment capability;
- objective vector;
- model/evidence;
- O90 robustness if available;
- validation/measured state.

A/B/C comparison is a decision object, not Undo history.

## 16. Proposed vs As-built promotion

Applying a SystemVariant to the scene does not mean it was installed.

Recommended transitions:

```text
Proposed
  -> selected for installation
  -> As-built
  -> Measured
```

At As-built transition:

- actual equipment identity is recorded;
- actual measured physical pose may differ from proposal;
- installation deviations are stored;
- old proposal remains immutable.

O90 can later compare the actual deviation with the planned tolerance envelope.

## 17. Interaction with O90

After an exact O100 candidate exists, O90 may perturb:

- proposed speaker XYZ;
- height;
- aim;
- physical yaw;
- listener position;
- later supported equipment/material/environment axes.

Example result:

```text
Variant A:
  nominal coverage: best
  ±20 mm placement sensitivity: high

Variant B:
  nominal coverage: slightly lower
  ±20 mm robustness: better
```

HTDT reports both facts.
It does not silently select B.

## 18. UI design

O100 integrates into the workflow-first shell.

### Room > スピーカー・座席

New actions:

- `スピーカーを追加`
- `仮想スピーカーとして追加`
- `役割を設定`
- `機種 / 音源モデルを設定`
- `配置可能範囲を設定`

Proposed entities:

- use ghost/outline treatment distinct from current/as-built;
- display `仮想` or `提案` badge;
- never look identical to measured installed entities.

### Optimize

Recommended sub-contexts:

- `探索設定`
- `構成比較`
- `候補`
- `比較`
- `ばらつき耐性`
- `測定・検証`

`構成比較` answers:

- which roles are added/removed;
- which equipment model is used;
- what placement region applies;
- which objectives are available/unavailable and why.

### Overview

Readiness may show:

- 「SL/SRは仮想構成です」
- 「SL/SRの音源モデルが未設定です」
- 「この機種では指向性データがないため全席coverageを評価できません」
- 「配置候補を計算できます」
- 「この案は未設置です」
- 「設置後に実測してください」

## 19. Command / deep-link additions

Candidate commands:

- `speaker.add_proposed`
- `speaker.assign_role`
- `speaker.assign_equipment`
- `optimization.system_variant.create`
- `optimization.topology_search.run`
- `optimization.variant.compare`
- `optimization.variant.apply`
- `system.mark_as_built`

Exact command IDs may be adjusted to the central command registry conventions, but logic must not be duplicated in widgets.

## 20. Persistence

At minimum persist:

- SystemVariant;
- TopologySearchSpec;
- ProposedEntitySpec;
- ChannelRoleBinding;
- EquipmentDefinition reference;
- per-entity PlacementConstraint binding;
- exact candidate coordinates/orientation/aim;
- variant ObjectiveVector;
- PredictionRun bindings;
- robustness references;
- installation/as-built transition record;
- measurement/validation references.

No result may be detached from the exact topology that produced it.

## 21. Fail-closed conditions

Do not produce a strong recommendation when:

- role/source mapping is ambiguous;
- proposed speaker has no model for the requested objective;
- directivity is required but unknown;
- SPL/headroom data is required but unknown;
- solver applicability does not cover the requested objective/band;
- the topology requires routing/excitation semantics that are undefined;
- comparison candidates were evaluated with incompatible fidelity/objective definitions;
- O90 robustness is requested outside its validated perturbation domain;
- production claim lacks required O60/R180 evidence.

Unavailable objective is shown as unavailable, not zero or a poor score.

## 22. Implementation slices

### O100A — SystemVariant / proposed lifecycle authority — implemented

Implemented in PR #144. The authority is intentionally limited to proposal topology/lifecycle and exact SceneRevision derivation; O100B placement search, O100C equipment/source capability, GUI, and new O90 semantics are not part of this slice.

- immutable SystemVariant;
- immutable ProposedEntitySpec;
- explicit current / proposed / as_built / measured lifecycle binding;
- versionable ChannelRoleBinding without a fixed role enum;
- exact add/remove/replace variant diff;
- baseline SceneRevision remains immutable;
- explicit selected-variant apply creates one new SceneRevision;
- proposal evidence survives apply through append-only variant/application lineage;
- WorkingDocument whole-scene replacement provides a one-Undo integration point;
- stable comparison reference exposes baseline/proposed/applied revision identity without implementing a new comparison score.

### O100B — topology + virtual placement search

- TopologySearchSpec;
- add/remove/replace explicit variant operations;
- per-proposed-entity allowed region;
- G10/O10 reuse;
- linked SL/SR pair;
- O80 aim/toe-in/height integration;
- deterministic candidate identity.

O100B was implemented in PR #150. It reuses O100A variants, O10 deterministic grid generation, G10 hard constraints and O80 orientation-aware body semantics; candidate conversion requires exact deterministic search membership and does not mutate the baseline SceneRevision. Equipment/source capability and performance objectives remain outside this slice.

### O100C — EquipmentDefinition/source capability

- source/equipment binding;
- physical dimensions;
- directivity tiers;
- sensitivity/SPL optional capability;
- data provenance;
- explicit unknown/inferred/measured states.

Implemented through Issue #168 and its merged EquipmentDefinition / DirectivityDataset / R110 source-authority slices. O100 reuses those exact authorities rather than creating a competing acoustic-source model.

### O100D — capability-gated system objectives

- layout/profile evaluation;
- coverage;
- worst-seat/seat spread;
- SPL/headroom;
- acoustic objectives;
- independent install/complexity objectives;
- O40 Pareto integration.

Implemented through Issue #169 and the merged named topology-comparison / coverage / direct-level / amplifier-headroom slices. Objective definition/unit/direction/model/fidelity compatibility remains exact and missing/unsupported values are not replaced by numeric sentinels.

### O100E — multi-fidelity topology search

- geometric/profile screening;
- directivity/coverage screening;
- acoustic refinement;
- common-fidelity final comparison;
- cache/scheduler reuse;
- audit trail for approximate pruning.

Implemented in PR #228. The shared staged-fidelity authority distinguishes hard-gate pruning, validated approximate screening, budget defer and blocked evidence; exact stage-to-stage survivor identity is preserved, and the final O100D eligible set must equal the screening survivor set before COMPLETE can be claimed. The generic stage contract is reusable by O90C, but O90C final robustness binding remains a separate slice.

### O100F — O90 robust system expansion

- exact O100 candidate → O90 RobustnessSpec;
- placement/aim tolerance;
- nominal vs robust variant comparison;
- 3D tolerance overlays for proposed entities.

### O100G — UX / as-built / measurement loop

- proposed-speaker Room workflow;
- system-variant comparison;
- Japanese-first copy;
- proposal ghost visuals;
- selected variant apply;
- As-built transition;
- MeasurementPlan;
- measured/validated comparison.

## 23. Acceptance

### O100-A01 — baseline immutable

Create a 5.0.2 proposed variant from a 3.0.2 baseline.
Baseline SceneRevision remains unchanged.

### O100-A02 — arbitrary role

Add SL/SR without changing the persistent speaker schema to a fixed 5.0.2 schema.

### O100-A03 — proposed state

SL/SR are clearly persisted/rendered as proposed, not measured/current.

### O100-A04 — linked allowed region

Define independent left/right zones or mirrored linked placement and deterministically regenerate the same feasible candidate set.

### O100-A05 — hard constraints

Proposed speaker candidates re-run room/allowed/exclusion/wall/pair/cabinet constraints.

### O100-A06 — source capability gate

Without directivity data, coverage objective is unavailable instead of fabricated.

### O100-A07 — equipment identity

Changing only SL/SR EquipmentDefinition changes candidate/SystemVariant semantic identity.

### O100-A08 — topology comparison

Compare current 3.0.2 and proposed 5.0.2 without treating missing SL/SR response in baseline as numeric zero.

### O100-A09 — multi-channel semantics

Per-channel analysis and explicitly defined excitation scenarios remain distinct; undefined coherent multi-channel sum is rejected.

### O100-A10 — Pareto

Topology/placement variants retain independent objectives and no hidden overall score is required.

### O100-A11 — common-fidelity final comparison

Final variants are re-evaluated under compatible model/objective/fidelity before final comparison is claimed.

### O100-A12 — O90 integration

An exact SL/SR candidate can be passed into O90 without changing O90 uncertainty semantics.

### O100-A13 — apply

Selecting a proposed variant creates one new SceneRevision and preserves the previous system for comparison/Undo/history.

### O100-A14 — As-built transition

Installing the proposal creates As-built state and preserves proposed coordinates/model as historical evidence.

### O100-A15 — measurement binding

REW measurements can bind only after a real AcquisitionContext/SceneRevision exists; proposed SL/SR never gain fake measured evidence.

### O100-A16 — UX

A user can complete:

```text
current system
→ add virtual SL/SR
→ draw install zones
→ choose speaker/source model
→ run placement search
→ compare candidates
→ inspect robust tolerance
→ select proposal
```

without internal schema IDs or manual database editing.

## 24. Product-completion definition

For this class of workflow, HTDT is product-complete only when it can preserve the full chain:

```text
Current physical system
  ↓
Proposed topology
  ↓
Proposed equipment/source
  ↓
Allowed installation space
  ↓
Feasible placement candidates
  ↓
Capability-gated prediction
  ↓
Multi-objective / Pareto comparison
  ↓
O90 tolerance robustness
  ↓
Selected proposal
  ↓
As-built physical state
  ↓
REW measurement
  ↓
Prediction-vs-measurement validation
```

The key distinction is that a proposal is useful before hardware exists, but becomes a strong real-room recommendation only when the relevant model/evidence gates support the claim.
