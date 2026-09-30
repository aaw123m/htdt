# R170B hybrid / extended integration vertical slice

Date: 2026-09-30
Issue: #101 / R170B hybrid integration
Task-start main: `3b9ed65f`

## Scope

This slice binds the already-merged R170B `HybridPredictionProvider`
(plus the R170A `LowBandPredictionProvider`) into the remaining R170B
surfaces — mixed-fidelity batch orchestration, O70 residual/adaptive,
O80 multi-seat / multi-radiator / aim — behind per-cell capability gates,
and closes the R120B containment gap (XY-in-bounds placements that exceed
the ceiling envelope are rejected; entity-entity collision is a declared
hard constraint). Every binding is capability-gated and carries typed
provenance; nothing is promoted to production or to validation evidence.

Implemented ownership is intentionally bounded to:

- `backend/src/htdt/cad_mixed_fidelity_provider_integration.py` (new)
- `backend/src/htdt/cad_prediction_matrix.py`
- `backend/src/htdt/cad_prediction_provider.py`
- `backend/src/htdt/cad_hybrid_prediction_provider.py`
- `backend/src/htdt/placement_constraints.py`
- `backend/src/htdt/cad_constraint_models.py`
- `backend/src/htdt/cad_constraints.py`
- `backend/src/htdt/cad_constraint_authoring.py`
- focused regression tests (`backend/tests/test_r170b_extended_integration.py`)

## Authority version

`r170b-extended-integration-1`

## Multi-fidelity / hybrid batch

`collect_mixed_fidelity_matrix_results` resolves one
`PredictionMatrixSpec` cell by cell:

- a cell resolves to the **hybrid lane** when a `HybridPredictionProvider`
  is bound to its exact `(source_entity_id, receiver_id)` pair — the R160
  provider is single-pair by contract, so the plan records batching
  semantics `mixed_fidelity_per_cell_provider_batch`;
- otherwise it resolves to the column's **low-band lane** (the R170A
  multi-receiver provider batches its receivers per column);
- every admissible lane re-verifies scene/snapshot/solver authority pins,
  the declared magnitude capability, and the observable-contract
  frequency grid; a cell with no admissible lane becomes BLOCKED or
  UNSUPPORTED with an explicit reason — never skipped or fabricated.

Cell transfers carry typed provenance: `provider_ref` is the union of
`PredictionProviderRef | HybridPredictionProviderRef`, the hybrid lane
records its `r160_artifact_ref`, `normalization_authority_ref`, the
declared phasor convention, and `timing_authority='unavailable'` — the
R160 steady-state phasor stack declares no timing authority, so a mixed
result honestly stays `coherent_sum_eligible=False` when a hybrid cell
participates, matching the same-fidelity assessor's rules per
(source, receiver) participant. `cached_result_sha256` reuse follows the
same #986 semantics (hash mismatch is a hard error), and
`execute_mixed_fidelity_prediction_matrix` persists spec/run/result
through the existing repository surface.

## O70 residual / adaptive

`build_mixed_fidelity_measurement_validation` emits exactly one
`CadModelValidationRecord` (model id `htdt.r170b.mixed_fidelity_matrix`)
over a mixed result set — one sample per READY/CACHED cell whose
`(matrix_source_id, matrix_receiver_id)` has an explicitly bound
measurement pinned to the spec's SceneRevision. The predicted side is
the cell transfer's own exact authority projected into level units at
its pinned pressure reference; the measured side is re-checked against
the spec's document/revision/content hash. Cells without a bound
measurement are omitted; when no cell is admissible the builder fails
closed instead of emitting an empty record.
`bind_mixed_fidelity_validation_providers` routes every referenced
provider into the existing O70 adaptive binding records — a provider the
record's pairs never reference cannot bind.

## O80 multi-seat / multi-radiator / aim

- `build_provider_multi_seat_member` projects a provider receiver into a
  `MultiSeatMember` with `evidence_type='predicted'`; the member dataset
  identity is the exact hash of the provider's per-receiver response —
  no fabricated measurement id.
- `build_provider_multi_seat_binding` seals per-member provider refs and
  per-provider `O80_MULTI_SEAT` consumer bindings into one immutable
  `ProviderMultiSeatBinding` (`r170b-multi-seat-binding:<hash>`);
  tampered dataset hashes or unbound providers fail closed.
- `resolve_provider_seat_responses` re-derives each predicted member's
  typed `FrequencyResponse` through the same band-checked read path N70
  uses; measured members keep their own repository provenance.
- `bind_provider_to_radiator_model` gates an `O80_MULTI_RADIATOR`
  binding on the provider's source binding pinning the model's exact
  equipment definition (id/version/sha256); `required_observables`
  includes `frequency_response_phase` only when the provider's phase
  capability is READY — `provider_radiator_transfer_evidence` reports
  `complex` / `magnitude_only` / `none` accordingly.
- `bind_provider_to_aim_evaluation` requires
  `spatial_pressure_field` — every current provider reports it
  UNSUPPORTED, so the binding fails closed rather than inventing
  directivity.

## R120B containment

- `EntityProfile` gains an optional declared `z_extent_m`; the built-in
  room-boundary check now computes the entity's `[z_bottom, z_top]`
  interval when an extent is declared: the **ceiling** side uses the
  full declared envelope (z_top must not exceed room height — an
  XY-in-bounds placement whose cabinet crosses the ceiling is rejected),
  while the **floor** side keeps the legacy origin-point check
  (z_origin >= 0) so fixtures whose envelopes legitimately extend below
  the origin (seat cushions) are not newly rejected.
- Entity-entity collision is a **declared** constraint kind
  `entity_collision` (a hard constraint, not a universal built-in) —
  coarse bounding envelopes may intentionally overlap in vertical
  placement solutions, so overlap only rejects where the constraint is
  explicitly declared. Both entities must declare XY envelopes and Z
  extents at validation time; overlap requires a positive-area XY
  intersection AND a positive-length Z-interval intersection.
- `CadEntityCollisionConstraint` maps the same semantics onto the CAD
  constraint set and the G10 evaluation payload, with the result
  surface reporting `entity_collision.overlap` /
  「物体同士が3D範囲で干渉しています」.

## Fail-closed list

- no provider run bound for a matrix cell → cell `BLOCKED`
- provider capability not READY → cell `UNSUPPORTED`
- authority pins (scene/snapshot/solver) mismatch → cell `BLOCKED`
- response/output grid ≠ observable contract axis → cell `BLOCKED`
- hybrid provider bound under a mismatched key → `BLOCKED`
- cached result hash mismatch → `ValueError` (no silent reuse)
- no READY/CACHED cell with a bound measurement → no residual record
- provider not referenced by the record's pairs → no O70 binding
- predicted member dataset hash mismatch / unbound → `ValueError`
- radiator equipment identity mismatch → `ValueError`
- `spatial_pressure_field` unsupported → `ValueError` (aim)
- entity_collision without declared envelopes → constraint-set
  validation error, `ValueError`
- any binding keeps `stale_state='CURRENT'` semantics and typed
  provenance — no production claim is made anywhere in the slice

## Test coverage

`backend/tests/test_r170b_extended_integration.py` — 18 tests covering
lane assignment, per-lane READY/provenance, every fail-closed gate,
cached-cell reuse semantics, execution terminal state, the per-cell
coherent-compatibility assessor, O70 record+binding round-trip over the
real R130/R160 fixture bundle, multi-seat member/binding/typed
resolution, radiator evidence and equipment gating, aim fail-closed,
and the R120B ceiling-envelope / collision constraint semantics.
