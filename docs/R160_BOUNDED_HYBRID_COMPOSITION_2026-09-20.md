# R160 Bounded Hybrid Composition — 2026-09-20

Issue: #101

Base main: `b4843cc3b7b22d0d66032deb912f58f2ae30af85`

Status: implementation complete on feature branch; GitHub Actions acceptance is recorded in the PR.

## Purpose

Extend the merged PR #254 solver-neutral typed hybrid foundation into an
auditable bounded composition authority using exact current R130 wave and R150
deterministic GA artifacts. This slice deliberately does not numerically add the
two backends into a broadband frequency response.

## Authority chain

`AcousticSceneSnapshot / SceneRevision`
→ exact `AcousticSolverResultEnvelope` carrying R130 complex pressure
→ exact R150 `DeterministicPathArtifact`
→ PR #254 `HybridAcousticResult`
→ content-addressed `HybridCompositionSpec`
→ observable-specific eligibility and double-count gates
→ immutable `HybridCompositionDecision`
→ content-addressed composed `HybridAcousticResult`.

The spec binds exact source artifact identities, scene/source/receiver/environment
compatibility, requested observable/domain, backend valid bands, exact overlap
(or no-overlap), explicit crossover policy, native reference conventions,
double-count policy, capability requirements and semantic hash.

## Actual supported observable

The current R130/R150 pair can produce a bounded composed authority for
`deterministic_path_identity`. The output does not numerically alter R130 or
R150 payloads. Reflection order is read from the R150 ordered interaction
sequence and therefore includes second-order paths when present.

## Intentionally unsupported observables

- magnitude/energy numerical composition: R130 Pa complex pressure and R150
  relative energy transport lack one exact normalization/reference; wave
  `full_field` may also already contain the direct/early components represented
  by GA;
- coherent phase: R150 explicitly lacks coherent phase authority; geometric path
  length is not promoted into phase;
- arrival timing: no shared explicit wave/GA time origin is available;
- late decay: bounded early specular paths are not RT60/EDT or late-tail
  authority.

## Overlap and gap fixture

Overlap policy is explicit and has no default:
`preserve_overlap_no_blend`, `explicit_partition_no_blend`, or
`preserve_gap_no_fill`. The explicit partition mode requires a caller-provided
crossover inside the exact overlap. Blend width is fixed to zero.

When bands are disjoint, the internal gap is retained as typed metadata. No
interpolation/extrapolation fills the gap.

## Double-count strategy

Wave input is conservatively classified as `full_field`. R150 input is
classified from actual deterministic paths as direct and/or deterministic early
reflection. Numeric composition under a disjoint-component requirement is
rejected because exact subtraction/decomposition authority is absent. The valid
path-identity composition preserves component identities and performs no numeric
sum.

## Persistence and stale behavior

The existing append-only R160 repository stores the composed typed result.
Reopen re-resolves exact snapshot, request, solver-result, external wave payload,
R150 path artifact and PR #254 stitching policy, regenerates the foundation,
then regenerates the composition from its embedded content-addressed spec.

Wave/GA/source/receiver changes therefore fail exact re-resolution. Crossover,
overlap and double-count policy changes produce a different spec/result identity;
the repository can reject an older persisted result when an expected new spec is
provided.

## Focused acceptance

Focused tests cover compatible exact inputs, incompatible scene/path identity,
no-overlap gap preservation, missing coherent phase, observable-specific
capability, double-count ambiguity, deterministic identity, save/reopen,
wave-artifact stale failure, expected-policy stale failure, PR #254 legacy
identity compatibility, and R150 second-order path-order handling.

GitHub Actions are the only execution acceptance for this slice. RDC usage: 0.

## Non-claims

No production broadband hybrid solver, numerical wave/GA pressure stitch,
automatic best crossover, coherent broadband IR, late-reverberation solver,
stochastic ray tracer, diffraction, arbitrary scattering, R170 GUI integration,
R180 validation or owned-room crossover tuning is claimed.
