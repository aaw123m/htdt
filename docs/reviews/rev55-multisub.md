# REV55-MULTISUB — Conventional multi-subwoofer optimization authority (issue #569)

Scope: the vendor-neutral evidence contract for **conventional**
multi-subwoofer optimization — placement, per-sub gain, delay/polarity and
bounded EQ used to reduce seat-to-seat low-frequency variation across a
listening area. Explicitly separated from the #533 active-control authority
(`cad_active_lf_control.py`): `MULTI_SUB_SUM_OPTIMIZATION` evidence never
carries ART / WaveForming / MIMO-room-control / active-absorption labels.
Sibling tracks #564 (registration, merged), #568 (correction qualification,
in flight) and #566/#570/#571 (merged) are referenced, not rebuilt.

## Literature basis

- **Welti & Devantier, "Low-Frequency Optimization Using Multiple
  Subwoofers", JAES 54(5), May 2006.** The paper's definition of a
  successful multi-sub arrangement is that the seat responses become *as
  similar as possible*, so a single global EQ can flatten the whole
  listening area — a flat response at one measurement position is
  explicitly not a sufficient success criterion. It also quantifies
  seat-to-seat variation as the per-frequency standard deviation across
  seats, which this module computes verbatim (`mean_std_db` per band).
- **Welti, AES 133rd convention (2012) — "How many subwoofers are
  enough?"** Consistency across seats and bass efficiency are separate
  figures of merit; the Harman work uses mean spatial variance (MSV). The
  module reports `msv_db2`, `spread_db`, `worst_seat_deviation_db`,
  pairwise-RMS spread and `MultiSubEffort` (gain/headroom) as independent
  metrics — never one hidden "bass score".
- **Rectangular-room placement results (Welti/Harman).** The evidence
  record carries exact declared/scene positions per sub and an
  `installable` feasibility state, so literature-optimal placements
  (mid-wall 25%-width arrangements etc.) remain *candidates to be proven
  installable*, not defaults assumed reachable.
- **SFM-style objective normalization.** Sum-frequency magnitude is
  bounded by |SFM| ≤ number of sources; the contract stores per-sub
  signal-path state (gain/delay/polarity/crossover/EQ refs) verbatim and
  treats seat-consistency as the primary deliverable.

## Implemented

### `backend/src/htdt/cad_multi_sub_optimization.py` (new)

Authority vocabulary and sealed records:

- `ConventionalMultiSubStrategy` — `single_sub_baseline`,
  `multi_sub_fixed_layout`, `multi_sub_placement_optimized`,
  `multi_sub_gain_delay_polarity_optimized`,
  `multi_sub_with_bounded_eq`. `MultiSubStrategyLabel` additionally names
  the #533 labels (`cross_channel_support_control`,
  `wavefront_active_control`, `external_proprietary_control`) and
  `assert_conventional_strategy` fails closed on them; the candidate
  validator enforces the boundary too.
- `SubChannelBinding` — exact per-sub signal state: physical output,
  output-group ref, position state (`scene_exact` / `declared` /
  `unknown`) + position + orientation, `gain_db`, `delay_s`, polarity,
  crossover ref+sha, per-sub EQ ref+sha, usable-output ref and the
  `feasible|infeasible|unknown` installable flag. `None` means UNKNOWN —
  no silent nominal defaults.
- `BoundedEqBinding` — bounded shared-EQ identity (formulation, filter
  count, boost/cut bounds) with `delegated_qualification_ref` pointing at
  a #568/EQP10-style qualification record so EQ eligibility stays owned
  by the correction-qualification track.
- `MultiSubSeatWeighting` — `uniform_declared`, `explicit_weights` or
  `seat_priority_profile` (id+sha against the #513 authority). Explicit
  weights must cover every optimization seat; a weight change is a new
  candidate identity.
- `MultiSubSeatPartition` — disjoint `optimization` / `holdout` /
  `repeatability` populations by construction (a seat cannot be both
  fitted and held out; repeatability positions are a third population).
- `MultiSubEvaluatorIdentity` — evaluator + model + declared `fidelity`
  token plus evidence-scope flags (`fixture_only`/`synthetic`/
  `production_eligible`) so fixture rows cannot launder into production
  evidence. The common-fidelity rule is mechanical: differing `fidelity`
  strings make a qualification `incomparable_fidelity`.
- `MultiSubCandidate` — sealed record over all of it (topology, routing,
  signal state, partition, weighting, declared objective families,
  optimized-variable list, constraints). Validators enforce stage
  semantics: `single_sub_baseline` has exactly one sub; multi-sub
  strategies have ≥2; `multi_sub_with_bounded_eq` requires the EQ binding
  and earlier stages may not carry one; the optimized-variable list may
  not exceed the stage's allowed variables.
- `evaluate_multi_sub_candidate` + `MultiSubEvaluation` — evaluates one
  declared population over the canonical log2 grid (`comparison._grid`,
  `_interpolate_many`; extrapolation forbidden by construction through
  the common-overlap computation). Produces per-band `BandSeatMetrics`:
  `mean_std_db` (Welti seat-to-seat), `msv_db2`, `spread_db`,
  `worst_seat_deviation_db`, pairwise RMS max/mean, `per_seat_mean_db`
  verbatim, `seat_mean_db`, `weighted_mean_db`. Target-bound runs also
  record per-seat / mean / worst / weighted RMS-vs-target on the same
  grid. Every seat's grid-sampled response is stored verbatim in
  `member_levels_db` — raw per-seat observables are never hidden behind a
  weighted aggregate.
- `MultiSubStageComparison` + `attribute_stage_transitions` — the
  canonical A→D staged comparison (A single-sub, B fixed/placement, C
  +gain/delay/polarity, D +bounded EQ), requiring one population and one
  fidelity across stages. Per-band rows carry seat-consistency deltas;
  response-scope rows carry target-deviation deltas (`scope` field
  separates the two families; negative delta = improvement, matching
  `ImprovementDelta`).
- `MultiSubQualification` + `evaluate_qualification` — deterministic
  verdict derived from the bound evaluations and the declared claim:
  population-role binding, common fidelity (else `incomparable_fidelity`),
  one partition per side, `listening_region` requires holdout evidence on
  both sides (else downgraded to `optimization_seats` with
  `missing_holdout_evidence`), holdout seat-consistency regression beyond
  `holdout_regression_tolerance_db` (default 0.5 dB) is
  `failed_holdout_regression` — the classic "improved at fitted seats,
  worse elsewhere" overfit cannot pass. Improvements are reported per
  family (`ImprovementDelta`, band + response scopes), never a single
  score.
- `MultiSubDeploymentVerification` + `evaluate_deployment` — binds the
  read-back `ObservedSubState` for every declared sub (installed,
  gain/delay/polarity/position exact match where declared), the
  post-deployment remeasurement covering every declared seat
  (optimization + holdout) and optional post-deployment evaluations.
  Verdicts: `verified`, `verification_incomplete`,
  `infeasible_installation` (a physically uninstallable candidate is
  infeasible, not "best theoretical"), `observed_state_mismatch`.

### `backend/src/htdt/cad_multi_sub_optimization_repository.py` (new)

Append-only repository over five tables — `cad_multi_sub_candidates`,
`cad_multi_sub_evaluations`, `cad_multi_sub_qualifications`,
`cad_multi_sub_stage_comparisons`, `cad_multi_sub_deployments`. Reads
re-verify row columns against the payload (sealed hashes recomputed);
`MultiSubConflictError` on same-id/different-content; saves pin the
persisted scene revision and pinned candidate/evaluation/qualification
parents.

### Schema / integrity wiring

- `NATIVE_SCHEMA_VERSION` 20 → 21 with `_migrate_20_to_21` running the
  idempotent baseline DDL (same convention as REGCAL's `_migrate_19_to_20`).
- `cad_schema_ddl.py`: five CREATE TABLE + six CREATE INDEX statements;
  `NATIVE_SCHEMA_TABLES` updated (also registers the previously-missing
  `capture_authoring_provenances` entry the DDL contract test flagged).
- `native_row_integrity.py`: five tables in `_UNBOUND_PAYLOAD_TABLES`
  (repository-side re-verification owns column checks).
- `native_authority_audit.py`: `multi_sub_optimization` repo factory +
  five `_ReplayProbe`s replaying persisted records through the repository.

### `backend/src/htdt/multi_sub_optimization_panel.py` (new)

`MultiSubBaselinePanel` — read-only comparison-surface panel wired into
the optimization workspace comparison page: an evaluation picker
(strategy × population × seat count), per-band seat-to-seat metrics
(mean across-seat std, max–min spread, worst-seat deviation, verbatim
per-seat band means), evidence kinds, and the latest stored
qualification verdict + blocking reasons. It creates no ranking, no
recommendation and no composite score.

### `backend/tests/test_rev55_multisub.py` (new)

MSB10–MSB70 fixture coverage: single-sub baseline, two fixed subs,
gain/delay/polarity-optimized and placement-optimized candidates,
bounded-PEQ binding + delegated EQ qualification ref, overfit
(`failed_holdout_regression`), deployment verification
(verified / incomplete / infeasible / mismatch). Plus fail-closed gates:
active-control labels, partition disjointness, EQ-binding stage rules,
optimized-variable stage rules, weight coverage, claim downgrades,
fidelity incomparability, hash tamper, persistence re-verification and
conflict detection.

## Consistency with the other REV55 tracks

- **#533 active LF control** (`cad_active_lf_control.py`) remains the
  sole owner of cross-channel support / MIMO / wavefront / proprietary
  concepts; this module treats those labels as out-of-vocabulary. #533
  can consume `MULTI_SUB_SUM_OPTIMIZATION` evidence as its conventional
  baseline (issue requirement).
- **#568 correction qualification**: EQ constraints delegate via
  `BoundedEqBinding.delegated_qualification_ref` — the multi-sub record
  references the qualification, it does not re-implement EQP10 checks.
- **#564 registration**: measured seat evidence binds measurement/dataset
  id+sha via `SeatResponseBinding`; comparability is left to the
  registration authority.
- **#513 seat priority**: profile weighting binds by exact id+sha.

## Remaining items

- No optimizer is implemented — this is the evidence contract. Placement
  search (e.g. literature mid-wall/25%-width enumerations), gain/delay
  search and bounded-EQ synthesis are future tracks; the record's
  `algorithm_id`/`algorithm_seed` fields are pinned for them.
- The panel is a read surface only; authoring candidates/evaluations
  through the UI (search spec wiring, solver dispatch) is open.
- Repeatability-position evaluations are accepted by the schema but not
  yet surfaced in the panel or deployment flow beyond the declared-seat
  remeasurement gate.
- Deployment verification binds read-back state but does not drive the
  write path — observed DSP state must come from an integration that can
  actually read it back.
- `MultiSubEffort` is declared-input only; no effort solver feeds it yet.
