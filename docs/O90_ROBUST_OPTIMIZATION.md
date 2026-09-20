# O90 — Robust / Tolerance-aware Optimization

> Status: **O90A–O90D software implemented / O90E planned** — tracking: [Issue #140](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/issues/140) / O90B: [Issue #146](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/issues/146)
>
> This document is the detailed authority for O90. Implementation order remains `docs/IMPLEMENTATION_ROADMAP.md`.
>
> O90 extends O10–O80; it does not replace SearchSpec, ObjectiveVector, Pareto, MeasurementPlan, O60 validation, O70 uncertainty, or O80 extended search.

## 1. Purpose

O90 answers a different question from nominal optimization.

Nominal optimization asks:

> Which feasible candidate performs best at the exact modeled geometry and parameters?

O90 additionally asks:

> Which candidates remain acceptable when realistic installation, placement, source, material, and environment uncertainty is applied?

Example:

- Candidate A has the best nominal FR objective.
- Candidate B is slightly worse nominally.
- A degrades sharply when speaker position changes by ±20 mm.
- B stays within a narrower performance range under the same tolerance.

HTDT must preserve that trade-off explicitly. It must not collapse the result into one opaque “robustness score” or silently choose B.

## 2. Scope

Initial O90 capability is **placement-tolerance robustness** over already supported O10/O80 variables.

First-line uncertainty axes:

- speaker X/Y/Z position;
- listener/seat X/Y/Z position;
- acoustic aim yaw/pitch where the current source model supports it;
- physical cabinet yaw where O80 orientation-aware constraints support it.

Capability-gated later axes:

- material/boundary parameters after R110 material authority exists;
- directivity/source sensitivity after R110 source authority exists;
- environment parameters after R110 environment authority exists;
- DSP parameters only after a separate signal-path optimization authority is approved.

O90 does not invent missing physics. Unsupported uncertainty axes remain unavailable.

## 3. Non-goals

O90 does not:

- replace O30 objective vectors with a single scalar;
- claim mathematical global worst-case from a finite sample set;
- infer probability distributions from simple ± tolerances;
- treat infeasible perturbations as zero response or ignore them;
- use synthetic evidence to open production recommendation gates;
- bypass O60/R180 validation;
- depend on R175 ROM/adjoint research for correctness;
- require every candidate to receive an expensive full-wave Monte Carlo evaluation.

## 4. Authority objects

### 4.1 RobustnessSpec

A `RobustnessSpec` is immutable and binds to:

- exact SearchSpec / ExtendedSearchSpec identity;
- exact candidate-set identity;
- model / configuration / prediction provider identity;
- objective evaluation specification;
- tolerance/uncertainty definitions;
- sampling strategy and seed where applicable;
- fidelity schedule;
- resource/sample budget;
- robust metrics requested;
- software/algorithm version.

Changing any of these creates a new spec.

### 4.2 UncertaintyAxis

Each uncertainty axis stores at least:

- stable axis ID;
- target entity / parameter reference;
- unit;
- nominal value;
- uncertainty model;
- parameters of that model;
- allowed physical bounds;
- correlation/group metadata;
- capability/provenance state.

Supported uncertainty models:

1. **bounded_interval**
   - example: speaker X ±20 mm;
   - does not imply any probability distribution;
   - supports local sensitivity and sampled-envelope reporting.

2. **distribution**
   - explicit distribution supplied by the user/model;
   - examples: normal/truncated normal/uniform only when explicitly selected;
   - percentile/probability metrics are only valid in this mode.

3. **empirical**
   - discrete samples or observed installation/measuring variability;
   - weights must be explicit if non-uniform.

4. **discrete**
   - finite alternatives such as mounting positions or repeatable installation states.

An interval such as ±20 mm must never be silently interpreted as “95%”.

### 4.3 Correlation model

O90 must not silently assume correlated parameters are independent.

For multiple axes the spec stores one of:

- explicit independent assumption;
- linked/correlated group rule;
- covariance/correlation definition where supported;
- deterministic linked perturbation relation.

Examples:

- FL/FR move together in X;
- left/right aim errors are mirrored;
- seat row translates as one rigid group.

### 4.4 PerturbationSample

Every evaluated perturbation stores:

- robustness spec hash;
- candidate ID;
- sample ID;
- exact parameter deltas;
- resulting SceneRevision/configuration identity;
- feasibility result and violated hard constraints;
- prediction/result reference;
- fidelity/backend;
- O30-compatible objective vector;
- failure/unsupported reason;
- cache/reuse provenance.

Samples are append-only evidence. Re-running O90 does not mutate prior samples.

### 4.5 RobustnessEvaluation

Per candidate and per objective, O90 may derive:

- nominal value;
- degradation relative to nominal;
- local finite-difference sensitivity;
- normalized sensitivity where units permit;
- sampled range;
- sampled adverse value;
- median/mean only when semantically valid;
- percentile / exceedance probability only when an explicit probabilistic model exists;
- feasible-sample fraction;
- constraint-violation rate;
- confidence/estimation uncertainty where calculated;
- unsupported / insufficient-sample state.

The name **worst-case** is reserved for a method that actually establishes the requested bounded worst-case. A finite sample minimum/maximum is labeled **sampled worst**.

## 5. Objective semantics

O90 reuses O30 objective definitions, including objective direction.

For a minimization objective, adverse movement is upward.
For a maximization objective, adverse movement is downward.

Robust metrics stay independent.

Example candidate vector:

```
nominal_FR_error
FR_error_adverse_p95
FR_error_sampled_worst
seat_variation_nominal
seat_variation_adverse_p95
feasible_fraction
position_sensitivity
```

The user may choose a subset as Pareto objectives. O90 must not manufacture an overall weighted score unless the user explicitly requests a display-only ordering, and such ordering is never “the true optimum”.

## 6. Sampling strategy

O90 uses a staged approach so robustness does not multiply solver cost unnecessarily.

### Stage A — local sensitivity stencil

For selected continuous axes:

- nominal;
- +delta;
- -delta.

Use one-at-a-time or explicitly defined linked perturbations.

Outputs:

- local slope / curvature indication;
- sensitive axes;
- feasibility boundary proximity.

This is the default cheap diagnostic.

### Stage B — bounded or probabilistic sampling

For candidates that remain relevant:

- deterministic low-discrepancy or stratified design for multidimensional bounded spaces;
- seeded randomized sampling only when the exact seed is stored;
- explicit empirical/discrete sample enumeration when supplied.

The exact sampling algorithm is versioned and must be reproducible.

### Stage C — adaptive refinement

Increase sample density when:

- a candidate is near the robust Pareto frontier;
- adverse quantiles are uncertain;
- constraint-feasibility changes rapidly;
- ranking changes under small perturbations;
- local sensitivity indicates strong nonlinearity.

### Stage D — final common-fidelity reevaluation

Final compared candidates must use:

- the same validated solver capability;
- the same objective/band/reference definition;
- compatible fidelity;
- an explicitly comparable sampling/uncertainty spec.

If the budget prevents this, results remain preliminary.

## 7. Multi-fidelity policy

O90 should normally run:

```
nominal Pareto candidates
  -> local sensitivity screen
  -> coarse robustness sampling
  -> robust/Pareto shortlist
  -> medium/high-fidelity perturbation refinement
  -> measured validation candidates
```

It is not acceptable to run high-resolution wave solves across every candidate × every perturbation by default.

Reuse R140 execution planning and cache/resume when available.

R175 ROM/reduced-basis/adjoint methods may accelerate O90 later, but O90 correctness and acceptance do not depend on R175.

## 8. Feasibility under perturbation

Every perturbed state must re-run the applicable hard constraints.

This includes:

- room/allowed/exclusion containment;
- wall clearance;
- pair clearance;
- cabinet envelope;
- O80 physical yaw/orientation-aware footprint;
- height/ceiling constraints;
- linked placement constraints.

An infeasible perturbation is evidence.

O90 must not silently drop it. Depending on the selected robust metric it contributes to:

- feasible fraction;
- violation probability when a probability model exists;
- sampled failure envelope;
- a dedicated robustness objective.

This lets HTDT distinguish:

- acoustically sensitive but physically feasible;
- acoustically stable but frequently infeasible;
- robust on both axes.

## 9. Applicability and validation

### 9.1 Development lane

A `development_synthetic` lane may validate:

- deterministic sampling;
- persistence;
- statistics;
- constraint handling;
- Pareto integration;
- cancel/resume/stale protection;
- UI integration.

Synthetic evidence never opens a production recommendation gate.

### 9.2 Production lane

A `production_owned_room` robustness recommendation requires:

1. the underlying prediction model/observable is eligible under the existing O60/R180 authority;
2. the perturbation axes stay inside that validated applicability domain;
3. the O60 campaign contains relevant sensitivity evidence where required;
4. exact model/config/result hashes remain bound;
5. measurement capability is sufficient for the robust objective being claimed.

A nominal model validation does not automatically validate every perturbation dimension.

If only model-conditioned robustness is justified, the UI must say so.

### 9.3 Sensitivity evidence reuse

O90 should reuse O60 preregistered sensitivity-pair evidence when the same parameter/domain/observable applies.

It must not duplicate O60 semantics or create an independent “validated” flag.

## 10. Interaction with O70 adaptive uncertainty

O70 uncertainty is primarily **model/residual uncertainty used for adaptive measurement selection**.

O90 robustness is **performance variation caused by explicitly modeled input uncertainty/tolerances**.

They must remain distinct.

Where both are available, the result may show:

- input/tolerance variation;
- model/prediction uncertainty;
- measurement uncertainty.

They are not summed into one undocumented error bar.

A future combined decision rule must preserve all three provenance components.

## 11. 3D / Optimize UI

O90 appears inside the new UX140 **最適化** workspace; it is not a new global destination.

Recommended Japanese UI:

- sub-context: **ばらつき耐性**
- section: **設置誤差**
- section: **感度**
- section: **性能分布**
- section: **許容範囲**

Candidate comparison should show nominal and robust metrics side-by-side.

Example:

| 指標 | 案A | 案B |
|---|---:|---:|
| nominal FR偏差 | 2.1 dB | 2.6 dB |
| adverse p95 FR偏差（明示distribution時） | 5.4 dB | 3.1 dB |
| X位置感度 | 高 | 低 |
| feasible fraction | 100% | 100% |

The UI must not add a hidden “robustness score”.

### 11.1 3D tolerance overlay

When available, Room/Optimize preview may display:

- speaker position tolerance envelope;
- aim tolerance cone;
- seat tolerance region;
- axes with high sensitivity;
- regions where hard constraints start failing.

This is a visualization of evaluated evidence, not a promise that every point inside is equally safe.

For a fitted local envelope, the UI labels the approximation and source sample density.

## 12. Persistence / identity

Semantic identity includes:

- base candidate / exact SceneRevision;
- uncertainty axes and models;
- correlation assumptions;
- sampling strategy/version/seed;
- solver/model/backend/fidelity;
- objective spec;
- robust metric spec;
- sample budget;
- software version.

Changing camera/view state does not invalidate O90.
Changing physical/acoustic inputs does.

The exact G10/O80 constraint workspace frozen into the originating SearchSpec is part of the evaluation authority. Local, bounded, distribution, empirical, and discrete evaluation paths must compare the supplied constraint snapshot/hash with that frozen SearchSpec before reading reusable samples or writing new sample/evaluation evidence. A changed allowed region, exclusion region, wall clearance, or other constraint therefore requires a new SearchSpec/O90 authority; stale evidence remains immutable and is not attached to the changed workspace.

O90 SQLite persistence uses the central native-schema authority before repository DDL/DML. Future, malformed, or unrelated databases are rejected before O90 tables are changed; supported legacy adoption remains owned by the central native migration path. Repository connections are transaction-scoped and deterministically closed after commit or rollback rather than relying on garbage collection.

## 13. Compute, cancel, cache and resume

O90 follows existing job semantics:

- never block the GUI thread;
- cancellation is safe;
- completed perturbation samples are reusable when semantic identity matches;
- stale results never attach to a changed current document;
- resume does not recompute completed samples;
- failed samples retain their reason;
- CPU/GPU/backend differences remain in provenance.

The scheduler should batch nearby perturbations only when the underlying solver authority permits operator/BVH/grid/matrix reuse.

## 14. Acceptance

O90 is complete only when all applicable gates below pass.

### O90-A01 — deterministic spec/sample identity

Given the same candidate, RobustnessSpec, algorithm version and seed, generate the same perturbation sample identities and order.

### O90-A02 — local ± tolerance

For a one-dimensional synthetic function and ±20 mm tolerance, evaluate nominal/+/- samples and recover the expected local sensitivity within declared numeric tolerance.

### O90-A03 — multidimensional statistics

For a synthetic function with a known distribution, recover expected percentile/mean statistics within declared sampling tolerance.

### O90-A04 — no fake probability

For `bounded_interval` without a distribution, percentile/probability outputs are unavailable rather than fabricated.

### O90-A05 — sampled worst wording

Finite sampling reports `sampled_worst`; it does not expose an unproven `worst_case` claim.

### O90-A06 — hard constraints

Perturb a feasible O80 candidate until cabinet/room/clearance constraints fail. The sample remains stored as infeasible and changes feasible-fraction metrics; it is not silently discarded.

### O90-A07 — exact provenance

Every perturbation can be traced to its exact base candidate, parameter delta, SceneRevision/config, prediction result and objective vector.

### O90-A08 — Pareto integration

Nominal and selected robust objectives can coexist in O40-compatible Pareto analysis without scalar collapse.

### O90-A09 — multi-fidelity consistency

Candidates promoted from coarse screening are re-evaluated under a common final fidelity before a final robust comparison is claimed.

### O90-A10 — cancel/resume/stale

Cancel and resume reuse completed perturbations, and document changes prevent stale results from becoming current.

### O90-A11 — capability gate

Material/directivity/environment uncertainty axes are unavailable until the corresponding R110+ authority exists and is applicable.

### O90-A12 — production evidence gate

`production_owned_room` robust recommendation stays fail-closed without eligible O60/R180 evidence for the model/observable and relevant perturbation domain.

### O90-A13 — UI semantics

The UX140 comparison shows nominal vs robust metrics, sample/percentile semantics and infeasible fraction in Japanese without an opaque overall score.

### O90-A14 — 3D tolerance visualization

A selected candidate can show position/aim tolerance evidence in 3D with clear distinction between sampled evidence and approximated envelope.

## 15. Recommended implementation slices

### O90A — authority and deterministic local sensitivity

- RobustnessSpec;
- UncertaintyAxis;
- PerturbationSample;
- RobustnessEvaluation;
- local +/- stencil;
- persistence/hash;
- G10/O80 constraint reevaluation;
- O30 objective reuse.

### O90B — multidimensional sampling and robust Pareto

- explicit independent/correlated assumptions;
- bounded/distribution/empirical/discrete sampling;
- percentile/sample-range metrics;
- feasible fraction;
- O40 robust Pareto integration;
- cancel/cache/resume.

PR #149 provides the bounded multidimensional foundation. The Issue #146 completion adds explicit distribution/empirical/discrete uncertainty semantics, probability-gated mean/percentile/constraint-violation outputs, and exact cancel/cache/resume/stale safeguards. Probability is never inferred from bounded intervals or unweighted empirical/discrete states. O90B is complete at this authority boundary. O90C is implemented through PR #228 (auditable staged-fidelity authority), PR #236 (R140 exact execution/cache and capacity-bounded scheduling), and PR #238 (typed common-fidelity O90 robust-Pareto finalization). O90D/O90E remain separate later slices. The #183/#186/#187 repository hardening requires the exact frozen constraint workspace before any cache access, deterministic SQLite close on every transaction path, and the central native-schema gate before O90 repository DDL/DML; these are authority/persistence safeguards and do not change O90A/O90B sampling or probability semantics.

### O90C — multi-fidelity / adaptive robustness

**Implemented via PR #228 / #236 / #238.**

- exact staged-fidelity plan over candidate authorities;
- hard-gate pruning separated from validated approximate screening;
- budget defer remains PRELIMINARY instead of being mislabeled as pruning;
- stage-to-stage exact survivor identity;
- R140 execution task identity binds exact evaluator/backend/config/resource-estimate/device authority;
- deterministic CPU/GPU/memory/scratch capacity-bounded batching;
- exact cache/resume only for identical execution input; stale result/evaluator fails closed;
- common-fidelity final comparison reuses existing O90 nominal/sampled-worst robust Pareto;
- cross-candidate comparison requires compatible model/provider/fidelity/objective spec/sampling budget/relative tolerance domain/dependence model;
- screening survivor id/hash set must exactly equal final robust-Pareto candidate set;
- optional O70 acquisition coordination remains future scheduling policy and does not change uncertainty meanings.

### O90D — UX and 3D tolerance overlays

- UX140 `ばらつき耐性` page;
- nominal/robust side-by-side comparison;
- sensitivity chart;
- tolerance/aim envelope;
- infeasible-region feedback;
- Advanced provenance drawer.

### O90E — owned-room robust validation

- preregister perturbation validation cases;
- reuse O60 sensitivity evidence;
- targeted measured perturbations around selected candidates;
- fail-closed production gate;
- record applicability limits.

## 16. Example decision semantics

Suppose:

- A nominal FR error = 2.1 dB;
- B nominal FR error = 2.6 dB;
- under an explicit position-error distribution bounded to ±20 mm:
  - A adverse p95 = 5.4 dB;
  - B adverse p95 = 3.1 dB.

HTDT reports:

- A has better nominal FR performance;
- B has better tolerance robustness for the declared ±20 mm uncertainty model.

It does **not** report that B is universally “better”.

That decision remains visible as a Pareto trade-off.

## O90D workflow-first robustness UI — 2026-09-20

Optimize now exposes a first-class 「ばらつき耐性」 context backed by existing O90A-O90C authority. Nominal, local sensitivity, axis sensitivity chart, finite sampled performance distribution, finite sampled adverse value, feasibility, completeness and probability-capable metrics remain separate; no hidden robustness score or automatic recommendation is introduced. Bounded/unweighted uncertainty never displays fake p95/probability. Exact Scene/Search/constraint/Objective/model/fidelity mismatch is shown as blocked/stale evidence. A dedicated 3D viewport renders declared position/aim/body-yaw tolerance primitives and persisted infeasible position-sample markers without mutating Scene authority.

O90D software integration is distinct from UX160 owned-Windows visual acceptance. See [ISSUE_140_O90D_ROBUSTNESS_UI_2026-09-20.md](ISSUE_140_O90D_ROBUSTNESS_UI_2026-09-20.md).
