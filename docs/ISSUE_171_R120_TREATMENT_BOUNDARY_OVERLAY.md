# Issue #171 × #101 — R120 treatment boundary overlay slice

## Scope

This slice connects the existing immutable `AcousticTreatmentDefinition`,
`AcousticTreatmentPlacement`, and `TreatmentSurfaceBindingEvaluation` authorities to the
solver-neutral R120 boundary contract without changing the existing Treatment or R120 compiler
modules.

The implementation is intentionally limited to authority composition. It does **not** add a
production wave/geometric solver, porous/membrane/perforated equations, scalar-absorption to
impedance conversion, automatic surface subdivision, treatment optimization, or before/after
prediction.

## Authority split

The implementation keeps three distinct authorities:

1. **Base surface construction/material** — the pre-existing
   `SurfaceBoundaryAuthorityBinding.material_authority` and
   `boundary_physics_authority`.
2. **Attached treatment** — immutable `TreatmentBoundaryOverlay`, bound to one exact
   SceneRevision, SemanticAcousticGeometry, R120CompiledGeometry, SemanticSurface, treatment
   definition, placement, and surface-binding evaluation.
3. **Solver-facing composition request** — immutable
   `TreatmentBoundaryCompositionRequest`, which references the base authorities and treatment
   overlay separately.

The R120 adapter retains the original base `material_authority`. Its
`boundary_physics_authority` points to the exact composition authority, which in turn retains
the original base boundary-physics ref. Treatment therefore never becomes a destructive
replacement for base construction.

## Capability policy

Wave capability is emitted only when the existing `TreatmentAcousticModel.material.wave_model`
is concrete (`rigid` or `specific_impedance_table`). A geometric/scalar absorption model is
never converted to impedance.

Geometric capability is emitted only for the existing explicit `banded` geometric material.
Absorption and scattering remain the two distinct source fields already carried by
`GeometricAcousticBand`; they are never summed or collapsed.

Transmission is not represented by the existing `AcousticMaterial` authority. The overlay and
composition therefore retain `transmission_capability_state = UNKNOWN`; no opaque assumption is
introduced.

## Fail-closed states

`compile_treatment_boundary_overlays` explicitly reports:

- `AVAILABLE`
- `BLOCKED_NO_ACOUSTIC_MODEL`
- `BLOCKED_WAVE_MODEL_UNAVAILABLE`
- `BLOCKED_GEOMETRIC_MODEL_UNAVAILABLE`
- `BLOCKED_PARTIAL_COVERAGE`
- `BLOCKED_OVERLAP`
- `BLOCKED_STALE_SURFACE`
- `BLOCKED_STALE_R120_COMPILED_GEOMETRY`

Partial coverage is accepted only when `host_surface_fraction == 1.0`. Anything else remains
blocked until explicit surface subdivision exists.

Multiple treatments on one host surface are fail-closed in this slice because explicit
non-overlap/stacking semantics are not yet an authority. Input order and save order therefore
cannot select a winner.

## Lifecycle semantics

`proposed` and `installed` remain distinct. The exact lifecycle selected by the caller is
stored in both the overlay and final composition request. Installed state is not treated as an
automatic prediction truth.

## Persistence

`TreatmentBoundaryOverlayRepository` adds append-only overlay and composition tables. On
save and on authoritative reads it re-resolves and verifies:

- exact SceneRevision/content hash
- exact SemanticAcousticGeometry
- exact R120CompiledGeometry/hash
- exact TreatmentDefinition/version/hash
- exact TreatmentPlacement/version/hash
- recomputed TreatmentSurfaceBindingEvaluation/hash
- exact host SemanticSurface presence

The repository then replays the canonical compiler helpers
(`compile_treatment_boundary_overlay` / `compile_treatment_boundary_composition`)
from those exact resolved authorities — including the exact R120 host-surface
base binding — and requires exact semantic equality with the persisted record.
Stale R120 geometry, stale surface bindings, or any coherently rehashed derived
overlay/composition field are rejected.

## Focused fixtures

`backend/tests/test_treatment_boundary_overlay.py` covers:

1. full-surface geometric Treatment
2. wave-capable Treatment
3. no acoustic model
4. geometric-only Treatment
5. partial coverage
6. stale surface
7. stale R120CompiledGeometry
8. multiple same-host treatments / overlap fail-closed
9. proposed lifecycle
10. installed lifecycle
11. base material/boundary preservation
12. deterministic overlay/composition hashing
13. save/reopen with authority re-resolution
14. canonical-compiler replay on save/read rejecting coherently rehashed
    derived overlay fields (thickness, air gap, coverage, model metadata,
    capability states, material candidate refs)
15. canonical-compiler replay rejecting rehashed compositions (base
    material/boundary refs moved off the exact R120 host binding, substituted
    treatment material authorities, lifecycle flips, forged overlay refs,
    multi-overlay attachments)
16. fail-closed reads when persisted rows or referenced authorities
    disappear or are tampered in place

## Files

- `backend/src/htdt/treatment_boundary_overlay.py`
- `backend/src/htdt/treatment_boundary_overlay_repository.py`
- `backend/tests/test_treatment_boundary_overlay.py`
- `docs/ISSUE_171_R120_TREATMENT_BOUNDARY_OVERLAY.md`

No common roadmap/status document and no `report.py` file is changed. RDC usage: 0.
