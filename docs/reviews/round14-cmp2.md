# Round 14 — Comparison Verdict Pipeline Depth (CMP2)

Dimension: do the compare lanes (measurement A/B, saved variant sets,
acoustic targets, pareto fronts, treatment plans, provider versions,
evidence reconciliation) produce verdicts that are the honest result of
their stated criterion — identical in what is computed, what the UI shows,
what persists, and what exports carry?

Method: for every verdict producer, construct inputs whose correct verdict
is known a priori, run the real producer, and assert the verdict matches.
All constructed checks live in
`backend/tests/test_round14_cmp2_verdicts.py` (17 tests).

## Verdict producers enumerated

| Producer | Verdict type | Verdict |
|---|---|---|
| `compare_frequency_responses` (`comparison.py`) | numeric diff + mean/rms/offset/shape metrics | **pass** |
| `derive_comparison_semantics` (`cad_comparison_semantics.py`) | level compatibility: absolute / normalized shape / diagnostic only | **pass** |
| `_compare` + `evaluate_standards_profile` (`cad_standards.py`) | PASS / FAIL / UNKNOWN / NOT_APPLICABLE per criterion | **pass** |
| `evaluate_acoustic_targets` (`cad_acoustic_target.py`) | member → band → criterion MET / UNMET / NOT_EVALUATED, evaluability AVAILABLE / UNKNOWN / UNSUPPORTED / BLOCKED | **pass** |
| `pareto_front` (`pareto.py`) | non-dominated set + dominated_by map | **pass** |
| `evaluate_topology_comparison` (`cad_topology_comparison.py`) | ELIGIBLE / INELIGIBLE gate before any pareto axis | **pass** |
| `build_treatment_comparison_outcome` (`cad_acoustic_treatment_comparison.py`) | compatible / partial / incompatible | **pass** |
| `build_intervention_alternative` (`cad_intervention_study.py`) | comparability + evaluation coverage (available / represented / absent) | **pass** |
| `compare_provider_versions` (`cad_validation_dashboard.py`) | newly_passing / newly_failing / unchanged / not_rerun / new_case / removed_case | **FAIL → fixed (REV14-CMP2-01)** |
| `build_multidimensional_evaluations_from_provenance` (`optimization_robustness_multidimensional.py`) | sampled worst + envelope + feasible fraction | **pass** |
| `reconcile_subject` (`cad_evidence_reconciliation.py`) | consistent / conflict / unknown / not_comparable / insufficient | **pass** |
| `scrub_source` (`cad_reflection_guidance.py`) | improves / worsens / unchanged at ±1e-6 | **pass** |
| `reconcile_mission_return` (`mission_reconciliation.py`) | applicable / needs_reconciliation / historical_target_removed / unaffected | **pass** |
| `diff_alternatives` (`cad_design_comparison.py`) | added / removed / changed / unchanged | **pass** |

## Threshold honesty

- `_compare` runs in `Decimal` against `Decimal(str(bound))`; inclusive/
  exclusive bounds are exact — verified at the bound for min/max/range
  and for `equals` (exact, no implicit tolerance: `0.1+0.2` vs `0.3`
  honestly fails rather than rounding into equality). `angle_wrap` and
  `absolute_value` normalize the observed value before comparing.
- Acoustic-target member verdicts use the same `_compare`, so an
  exactly-at-bound member is MET iff `upper_inclusive`/`lower_inclusive`
  declares it — no off-by-one bucket.
- `reconcile_subject` bound is `tolerance + unc_a + unc_b` applied
  symmetrically (`delta <= bound` → consistent at exactly the bound).
- `scrub_source` uses a symmetric ±1e-6 epsilon for improves/worsens and
  documents that "improves" only means lengthened excess delay — never an
  audibility claim.
- `build_coverage_cells` thresholds (_STRONG_AT=5, _LIMITED_AT=2) apply
  to the *eligible* denominator with holdout counted separately; not
  applicable cases are excluded from both numerator and denominator.

## Missing / invalid data

- `pareto_front` raises `ParetoError` when a candidate's selected metric
  is `missing`/`unsupported` — the missing leg is never silently skipped
  nor silently excluded from the axes.
- `_evaluate_band_basis` marks a declared member with no observation
  `missing`, one with conflicting observations `ambiguous`, and either
  makes the whole band `NOT_EVALUATED` (no silent subset mean).
- `compare_frequency_responses` returns `None` metrics below 2 valid
  grid points — never zero-filled numbers.
- `evaluate_standards_profile` fails closed: missing observation /
  unit mismatch / missing input or capability / missing evidence →
  UNKNOWN with a reason code, never a guess at PASS/FAIL.
- `build_multidimensional_evaluations_from_provenance` requires a scored
  nominal (first sorted sample must be `step='nominal'`), counts feasible
  samples with no vector in `failed_sample_ids`, and refuses sample sets
  that do not match the deterministic design's expected ids.
- `build_treatment_comparison_outcome` counts candidates with no outcome
  as not evaluated (`partial`/`incompatible`) and names the missing
  labels in `compatibility_reasons`.
- Intervention alternatives: `evaluation_coverage` derives
  available/represented/absent per declared cell — 'absent' is reported,
  never dropped; `quantitatively_comparable` requires comparable fidelity
  *and* complete coverage, and the panel shows `比較不能` with the reason.

## Direction per metric

- `_dominates_metrics` honors each metric's declared direction
  (minimize: strictly lower better; maximize: strictly higher better).
  The comparison authority is `definition_id` — a content hash over the
  whole `ObjectiveDefinition` including `direction`, `unit`,
  `valid_domain`, `comparison_model_id` and version — so two candidates
  cannot silently disagree on direction; mismatch raises `ParetoError`.
- `sampled_worst` picks the adverse tail by direction: minimize →
  sampled max, maximize → sampled min (verified both ways).
- `_rule_worst_score` adversity is direction-correct: min → `minimum−v`,
  max → `v−maximum`, range → worst of both ends; `spatial_worst` selects
  the member maximizing it and stores that member's own verdict.
- No RT60/D50-style flips found: the acoustic-target rule comparison is
  operator-driven (`min`/`max`/`range`/`equals`), not sign-convention
  driven, so a direction cannot be inverted without changing the
  criterion itself (which changes its `criterion_sha256`).

## Aggregation

- Band → criterion verdict: AVAILABLE requires *every* declared band
  decided; MET requires *every* band result MET (per allowed evidence
  basis — predicted and measured never collapse into one record).
- `spatial_mean` verdicts the mean value but still discloses the
  worst member in `limiting_entity_id`; `spatial_worst` verdicts the
  worst member directly; `per_position` is a strict AND;
  `single_listener` requires exactly one evaluated member.
- Reconciliation subject outcome is worst-case: any `not_comparable`
  pair → `not_comparable`, else any `conflict` → `conflict`, else any
  `unknown` → `unknown`, else `consistent`.
- Topology comparison poisons the whole candidate set honestly: if
  evaluator/model/fidelity signatures diverge, *every*
  provisionally-eligible bundle is marked ineligible
  (`_declared_signature_issue_codes`) because a relative comparison
  across different semantics is meaningless; the pareto front is
  computed over the eligible subset only and the model validator pins
  `dominated_by` keys to exactly that subset.

## Persistence / exports / diff display

- Persisted comparisons carry `algorithm_version` + full spec
  (requested band, reference band, excluded bands);
  `replay_comparison_result` re-runs the pinned algorithm and the
  repository requires `recomputed == result` before INSERT and
  re-verifies dataset hashes on every read — the stored criterion
  version travels with the verdict.
- `authority_revalidation._revalidate_comparison` re-derives the
  verdict with the stored algorithm version and marks the row
  `revalidated` or `kept_stale` — never reinterprets old verdicts under
  a new criterion.
- `analysis_export` serializes the same persisted record
  (`comparison_metadata_entries` exports algorithm_version, the four
  metrics, valid/total grid points, level compatibility); the displayed
  series (`comparison_side_series`, `series_from_comparison`) are the
  stored `a_db`/`b_db`/`difference_db` arrays — the displayed diff is
  the stored a−b, not a third path.
- Reconciliation decisions pin `subject_sha256` + per-observation
  semantic hashes (`observation_refs`) so the persisted row can prove
  which bytes it reconciled.
- Acoustic-target evaluations hash the profile's `target_semantic_hash`
  into `evaluation_id`/`evaluation_sha256` — a threshold change creates
  a different profile hash and cannot silently reinterpret old rows.

## Finding REV14-CMP2-01 — `compare_provider_versions` hides new failures
# (fixed)

In `cad_validation_dashboard.py`, the transition `not_applicable → fail`
fell through to `not_rerun` while `not_applicable → pass` surfaced as
`newly_passing`. A provider version that newly evaluates a previously
non-applicable corpus case and *fails* it was therefore invisible as a
regression — directly contradicting the module's own hard rule that the
version comparison shows newly passing **and** newly failing cases and
never labels a version globally better.

Fix (one clause): `old != 'fail' and new == 'fail' → 'newly_failing'`
covers `pass→fail` (previously `pass and new != 'pass'`) and `n/a→fail`;
`n/a→n/a` still falls through to `not_rerun`, and every
`→ not_applicable` transition is still `not_rerun` because it is caught
by the earlier branch.

Verified empirically: before the fix the constructed n/a→fail case
reported `not_rerun`; after the fix the full 9-transition matrix
(pass/fail/not_applicable × pass/fail/not_applicable, plus added/removed
cases) reports the symmetric outcomes asserted in
`test_version_diff_transition_matrix_is_symmetric`.

## Deferred observations (not verdict-correctness bugs)

- `LevelCompatibility` declares `'incompatible'` but
  `derive_comparison_semantics` can never emit it — `diagnostic_only` is
  the floor. The optimization-workspace JP label map
  (`_level_compatibility_label`) has no entry for it and would fall back
  to the raw English string *if* a row were ever stored with it. Not
  reachable today; left as-is (removing the enum member would change a
  persisted-adjacent Literal).
- `build_multidimensional_evaluations_from_provenance` skips a scored
  sample whose vector lacks the objective id without counting it in
  `failed_sample_ids`. Unreachable: upstream `_multidimensional_sample`
  schema-checks every vector against the objective schema and replaces
  any mismatched vector with `None` (which *is* counted).
- `build_coverage_cells` cell `qualification` uses a least-alarming
  precedence (production_qualified only if uniform; any experimental →
  experimental; any candidate → candidate). A mixed
  {candidate, not_qualified} cell reports 'candidate', masking the
  not_qualified member at the label level; per-case drill-down still
  exposes it. Judged acceptable label semantics, noted for visibility.
