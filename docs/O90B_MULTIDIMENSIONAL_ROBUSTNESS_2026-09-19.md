# O90B — Multidimensional robustness / robust Pareto implementation

Date: 2026-09-19  
Tracking: #140, #146  
Foundation: PR #149

## Implemented scope

O90B extends the merged O90A authority without introducing a parallel robustness truth source.

PR #149 established the bounded multidimensional foundation:

- deterministic multidimensional bounded perturbation sampling;
- fixed spec + seed -> reproducible sample identity and ordering;
- bounded multi-axis combinations with explicit negative/positive corner anchors plus deterministic interior design points;
- explicit linked perturbation groups using a shared normalized coordinate and per-axis multipliers;
- complete feasible / infeasible / failed sample retention;
- G10 and O80 hard-constraint reevaluation for every sample;
- finite sampled envelopes and `sampled_worst`;
- feasible fraction;
- nominal + robust O40-compatible Pareto dimensions;
- exact SceneRevision / candidate / O30 / prediction-model provenance;
- SQLite persistence and deterministic replay.

The Issue #146 completion slice adds the remaining canonical O90B authority:

- explicit `distribution`, `empirical`, and `discrete` input-uncertainty models;
- deterministic model-specific sample identity and ordering;
- explicit seed semantics for sampled distributions;
- mean and percentile outputs only under an explicit probability model;
- hard-constraint violation probability only under an explicit probability model;
- completed-sample cache/reuse;
- safe cancellation that retains completed immutable evidence;
- resume without recomputing exact completed samples;
- stale-result rejection after SceneRevision/spec/model/objective authority changes;
- backward-compatible persistence through the existing `CadRobustnessRepository`.

No new aggregate robustness score is introduced.

## Authority boundaries

O90A remains the base authority for:

- `RobustnessSpec`;
- `UncertaintyAxis`;
- `PerturbationSample`;
- `RobustnessEvaluation`;
- exact candidate / SceneRevision / O30 provenance;
- append-only robustness persistence.

O90B extends those models. Existing O90A and PR #149 bounded semantic hashes remain unchanged because new optional uncertainty/probability fields are excluded from identities when absent.

O90B continues to reuse rather than replace:

- O30 objective vectors;
- O40 Pareto dominance;
- G10 geometric hard constraints;
- O80 orientation / physical-yaw hard constraints;
- the prediction provider/model/fidelity provenance already bound by O90A.

O70 residual/model uncertainty remains separate from O90 input/tolerance uncertainty. Synthetic O90 evidence continues to be development evidence and does not enable a production recommendation.

## Uncertainty semantics

### Bounded interval

A `bounded_interval` is only a declared finite range. It is never converted to a probability distribution.

Therefore bounded sampling exposes:

- sampled min/max;
- `sampled_worst`;
- feasible fraction;
- complete infeasible/failed evidence.

It does not expose mean, percentile, or probability outputs.

### Explicit distribution

A distribution model is probabilistic only because the model explicitly declares the distribution.

The current O90B distribution authority supports:

- explicit uniform ranges;
- explicit normal distributions;
- explicitly truncated normal distributions;
- an explicit independent multi-axis assumption;
- deterministic stratified sampling whose exact ordering is bound to the spec, algorithm version, and seed.

The nominal sample is provenance/reference evidence and carries zero probability mass. Distribution perturbation samples carry the finite design probability weights. The same candidate + SceneRevision + uncertainty model + sample count + algorithm version + seed produces the same sample identities and order. Changing the seed changes the robustness spec identity and sample identities.

Objective mean and p05/p50/p95 are calculated only from feasible, successfully scored probability mass. They are therefore conditional on the feasible scored portion of the declared probability model. Physical infeasibility is reported separately as hard-constraint violation probability rather than being assigned a fabricated objective value.

### Empirical

Empirical states are exact supplied observations/states and are enumerated in canonical state-ID order.

Repeated observations or occurrence counts do not become probability weights implicitly. An empirical model without explicit weights remains non-probabilistic and therefore has no mean/percentile/probability outputs.

If every empirical state explicitly carries a probability weight and the weights sum to 1, probability outputs are enabled with `explicit_empirical_weights` provenance.

### Discrete

Discrete states are exact finite alternatives and are enumerated in canonical state-ID order.

A discrete model is non-probabilistic when weights are omitted. Frequencies are not inferred. Probability outputs are enabled only when every state has an explicit probability weight and the weights sum to 1.

Empirical/discrete states are not converted into bounded intervals.

## Feasibility and probability

Every perturbation continues to run the existing G10 and O80 hard-constraint authorities. Infeasible samples remain persisted evidence and are never silently discarded.

Two different outputs are intentionally retained:

- `feasible_fraction`: the existing PR #149 unweighted finite-design fraction (including the nominal reference sample) that satisfies hard constraints;
- `constraint_violation_probability`: probability-weighted infeasible mass, available only when an explicit probability model exists.

These values can differ and must not be substituted for one another.

Finite objective extrema remain `sampled_worst`; no finite O90B design is renamed `worst_case`.

## Cache, cancel, resume, and stale-result authority

A completed perturbation is reusable only when its exact semantic authority matches the requested plan:

- robustness spec ID/hash;
- uncertainty-model hash and item/draw identity;
- candidate ID;
- SceneRevision authority inherited through the robustness spec;
- parameter deltas;
- model ID/version;
- prediction provider;
- fidelity;
- O30 objective-evaluation spec;
- probability weight where applicable.

`CadRobustnessRepository` is the cache authority. Samples are saved immediately after completion. Cancellation stops before the next not-yet-started sample and leaves already completed samples intact.

Resume loads only samples for the exact robustness-spec identity and skips those exact completed samples. Caller-supplied completed evidence is subject to the same validation. Evidence from a changed SceneRevision, changed uncertainty model/spec/seed, changed candidate, prediction model/provider/fidelity, or changed objective authority is rejected as stale rather than attached to the current result.

Creation timestamps are not semantic identity. A regenerated spec with the same semantic hash may reuse the same cached samples even if its creation timestamp differs.

This is O90B sample-level reuse only. O90C scheduler/multi-fidelity batching and R140 execution-planning integration are not implemented here.

## Persistence and provenance

The existing SQLite tables remain authoritative; no parallel store is introduced. New fields are optional JSON payload extensions, so previously persisted O90A and bounded O90B payloads continue to validate with default values and retain their original semantic hashes.

An explicit-uncertainty `RobustnessSpec` binds:

- exact O90A parent spec ID/hash;
- document and exact SceneRevision/content hash;
- SearchSpec and candidate-set identity;
- exact candidate payload/hash;
- nominal O30 evaluation ID/hash and evaluation-spec hash;
- prediction model ID/version/provider/fidelity;
- explicit uncertainty-model payload/hash;
- sampling strategy/version;
- sample count;
- explicit seed for distribution sampling;
- software version.

Each perturbation additionally binds its sample identity/order, uncertainty item/draw identity, parameter deltas, optional probability weight, perturbed scene hash, G10/O80 evidence, prediction result, objective vector, and failure reason.

Each evaluation stores a sampling-provenance hash over the exact authority and ordered sample identities.

## Validation

Focused O90B tests cover:

- deterministic bounded replay and linked-axis behavior from PR #149;
- deterministic explicit-distribution identity/order for identical spec + seed;
- changed seed -> changed spec/sample identities;
- a synthetic known uniform distribution reproducing expected mean and p95 within declared finite-sampling tolerance;
- bounded-only sampling exposes no percentile/probability output;
- unweighted empirical samples expose no probability output and preserve canonical enumeration;
- explicit discrete probabilities produce hard-constraint violation probability;
- feasible fraction remains distinct from weighted violation probability;
- infeasible evidence is retained;
- cancellation retains completed samples;
- identical spec resume reuses completed samples rather than recomputing them;
- changed spec/seed and changed SceneRevision reject stale reuse;
- persistence round-trip through the existing robustness repository;
- O40 nominal + `sampled_worst` Pareto semantics from PR #149 remain unchanged.

## Out of scope / remaining O90 work

Canonical O90B ends at this authority boundary. Remaining work is intentionally later-slice work:

- O90C multi-fidelity screening/refinement and scheduler/R140 integration;
- O90D UX140 integration and 3D tolerance overlays;
- O90E owned-room robustness validation / production evidence gates.

No GUI, UX shell, O100, O90C multi-fidelity implementation, or RDC work is included in Issue #146.
