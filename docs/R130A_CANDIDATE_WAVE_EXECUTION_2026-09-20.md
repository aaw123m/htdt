# R130A candidate wave execution — 2026-09-20

## Scope

Issue #101 の solver-neutral acoustic authority を、一つの bounded candidate wave backend の実数値実行まで縦につなぐ slice。production solver 選定や R130A numerical acceptance 完了ではない。

成立する exact chain:

`AcousticSceneSnapshot -> AcousticPredictionRequest -> READY SolverDispatchBinding -> CandidateWaveExecutionInput -> pinned PFFDTD numerical execution -> immutable complex-pressure artifact -> AcousticSolverResultEnvelope -> save/reopen`

## Candidate backend / fixture

- backend: PFFDTD Python/Numba CPU candidate
- upstream commit: `aa319f6c86517cb95aabfae8656277da62c3ead5`
- fixture: `r130a-candidate-wave-cube-v1`
- geometry: exact 2 m closed cube compiled through existing R120 authority
- boundary: explicit `rigid_zero_normal_velocity`; unknown boundary is rejected
- source: one exact R110 source
- wave excitation: explicit `AcousticWaveExcitationAuthority` complex volume velocity at 40/80 Hz; speaker sensitivity is not converted to source strength
- receiver: one exact receiver
- observable: raw complex pressure, Cartesian real/imaginary Pa, 40/80 Hz
- execution: CPU-only, bounded grid/time/output/wall-time resource configuration

PFFDTD's discrete impulse is used only to obtain the finite-record pressure/volume-velocity transfer. The stored pressure is that transfer multiplied by the exact explicit complex volume-velocity samples. This is candidate execution plumbing, not an independent acoustic reference truth.

## Exact input / output authority

`CandidateWaveExecutionInput` hashes snapshot/request/dispatch, R120 geometry/topology and boundary composition, R110 source, wave-excitation binding/authority, ordered receivers, requested frequency/time sampling, solver implementation/configuration, adapter/compiler version, runtime/backend identity, and resource configuration. UI/camera state is excluded.

Raw numerical result is stored outside `AcousticSolverResultEnvelope` as a content-addressed immutable JSON artifact. The artifact records schema/encoding, complex convention, receiver order, frequency/time axes, units/reference, valid domain, execution identity, exact source lineage, raw PFFDTD asset SHA-256, and real/imaginary pressure arrays. Envelope stores exact artifact/schema refs only.

External authority/artifact reopen verifies exact id/version/hash and payload digest. Missing or modified artifacts fail closed.

## Fail-closed / focused validation

Focused tests cover READY-only execution, BLOCKED dispatch refusal, missing wave excitation, stale dispatch-chain identity, unsupported observable/band, deterministic input identity, artifact/envelope construction, save/reopen, missing/modified artifact, cancellation, and backend failure without fake result persistence.

The dedicated GitHub Actions workflow additionally checks out the exact PFFDTD commit and executes the bounded fixture. Its evidence artifact must explicitly retain:

- `production_solver_selected=false`
- `r100b_completed=false`
- `r130a_numerical_acceptance_completed=false`
- `owned_room_evidence=false`
- `rdc_calls=0`

Final workflow run/result hashes are recorded on PR #243 / Issue #101 after CI convergence.

## Non-claims / remaining gates

This slice does **not** establish production PFFDTD adoption, R100B completion, R130A numerical acceptance, R130B/C boundary validation, R180 owned-room validation, convergence PASS, production recommendation eligibility, or independent-reference truth. Those remain separate gates.

RDC: 0.
