# Issue #171: Treatment boundary composition → AcousticSceneSnapshot

Date: 2026-09-19  
Branch: `issue-171-treatment-acoustic-snapshot`  
Base: latest `main` at task start, `2f00a407cd896b5ae55562246f7db3bba0cf56ec`

## Scope

This slice binds the existing PR #211 treatment boundary overlay/composition authority into the PR #213 solver-neutral `AcousticSceneSnapshot` contract. It does not implement R130/R150, solver-specific impedance, porous absorber physics, material fitting, fake before/after prediction, GUI, InstallationOutput changes, or production solver selection.

## v1 / v2 compatibility

Existing no-treatment snapshots remain snapshot schema/authority/compiler v1. The builder deliberately emits v1 when no treatment boundary result is supplied, and the v1 semantic payload excludes the new v2-only treatment bindings and geometric-boundary readiness field. This preserves the existing persisted v1 semantic identity.

Treatment-bearing snapshots use schema/authority/compiler v2. Persisted v1 JSON that does not contain the new fields is accepted with compatibility defaults and is revalidated against the original v1 semantic payload. No v1 treatment semantics were silently rewritten.

## Exact treatment composition binding

A v2 snapshot stores domain-specific `TreatmentBoundarySnapshotBinding` entries. An AVAILABLE entry keeps:

- exact host `SemanticSurface`
- target domain (`wave` or `geometric`)
- exact `TreatmentBoundaryCompositionRequest` id/version/hash
- exact attached `TreatmentBoundaryOverlay` refs
- selected treatment material authorities
- proposed/installed lifecycle
- exact base material authority
- exact base boundary physics authority
- exact definition/placement/surface-binding lineage copied from the overlay authority

The snapshot does not calculate absorption, impedance, effective coefficients, partial-coverage approximations, or replacement materials.

## Base construction vs attached treatment

`surface_boundary_configuration` continues to represent the exact base R120 surface mapping. Treatment composition is stored separately. AVAILABLE treatment input is accepted only when the composition's base material and base boundary physics equal the host R120 mapping, and when `adapt_treatment_boundary_composition_to_r120()` reproduces the supplied exact R120 treatment binding.

This keeps:

`base construction authority + attached treatment overlay -> TreatmentBoundaryCompositionRequest`

as the sole composition authority. The snapshot only binds and re-resolves that authority.

## Build-time exactness / stale handling

Treatment-bearing snapshot construction validates:

- exact SceneRevision id/content hash
- exact SemanticAcousticGeometry id/hash
- exact R120CompiledGeometry id/hash
- host SemanticSurface existence and stable host-authority hash
- host surface presence in the exact R120 mapping
- composition target domain
- composition id/hash semantics via its model
- exact overlay id/hash semantics via its model
- exact overlay/composition SceneRevision, geometry and R120 lineage
- exact base material/boundary authority
- exact attached overlay refs and selected material authorities
- exact lifecycle
- exact adapted R120 treatment binding

`BLOCKED_STALE_SURFACE` and `BLOCKED_STALE_R120_COMPILED_GEOMETRY` results are rejected rather than serialized as usable snapshot input. Other blocked treatment results may be represented explicitly, but never carry an AVAILABLE composition identity.

## Repository reopen semantics

`CadAcousticSnapshotRepository` accepts an optional typed `TreatmentBoundaryOverlayRepository` dependency.

For treatment-bearing snapshots, that typed repository is mandatory. Reopen does not trust an opaque hash string. It re-resolves the exact composition and every overlay. The treatment repository chain then re-resolves definition, placement and surface-binding evaluation authorities as implemented by PR #211.

No-treatment v1 snapshots do not require a treatment repository and retain the prior reopen path.

## Purpose-specific readiness

No aggregate `solver_ready` field was added.

v2 adds purpose-specific `geometric_boundary_ready` alongside the existing `wave_boundary_ready`. For every treated host surface, a domain is boundary-ready only when an exact AVAILABLE composition for that domain is present. Capability on an overlay alone does not synthesize the other domain's composition.

Consequences:

- geometric-only treatment can keep geometric boundary readiness while wave remains blocked, when the exact geometric composition is bound
- wave-only treatment can keep wave boundary readiness while geometric remains blocked, when the exact wave composition is bound
- UNKNOWN treatment capability never promotes that domain to ready
- a blocked wave result does not block a separately bound AVAILABLE geometric composition
- partial coverage / overlap / no acoustic model remain fail-closed for both domains

Requested observables continue to gate only on the capabilities required by their domain.

## Prediction identity

Treatment-bearing snapshot bindings participate in the v2 snapshot semantic hash. Therefore changes to composition id/hash, selected material authority, lifecycle, or exact overlay identity change the snapshot id/hash.

`AcousticPredictionRequest` already includes the snapshot id/hash in its deterministic input payload. Treatment changes therefore propagate to prediction input identity without changing the prediction request schema.

## Tests

Focused coverage is in:

`backend/tests/test_cad_acoustic_snapshot_treatment.py`

It covers the requested fixtures:

1. no-treatment v1 regression
2. wave-capable treatment
3. geometric-only treatment
4. unknown treatment model
5. proposed lifecycle
6. installed lifecycle
7. base material/boundary preservation
8. overlay change -> snapshot hash change
9. composition change -> prediction input hash change
10. stale overlay rejection
11. stale host surface rejection
12. wrong R120 compiled geometry rejection
13. wrong SceneRevision rejection
14. save/reopen exact typed re-resolution
15. persisted v1 payload compatibility
16. blocked treatment result never promoted to AVAILABLE, while an independently bound geometric domain remains usable

## Validation

PR #219 GitHub Actions CI run #1088 completed successfully on the implementation head:

- `python -m pytest backend\\tests`: **701 passed, 1 skipped, 2 warnings**
- the focused `test_cad_acoustic_snapshot_treatment.py` coverage is included in that backend suite
- all CI authority preflights, launcher checks, acceptance-harness compilation, and PowerShell syntax checks passed
- repository comparison remained based on the task-start latest `main` commit with no other agent branch incorporated
- RDC remained **0**

## Deferred

Deferred deliberately:

- R130 solver adapter
- R150 solver adapter
- effective impedance solver
- porous absorber physics
- material fitting
- automatic partial-coverage effective coefficients
- fake before/after prediction
- GUI
- InstallationOutput changes
- production solver selection

## RDC

RDC calls: **0**.
