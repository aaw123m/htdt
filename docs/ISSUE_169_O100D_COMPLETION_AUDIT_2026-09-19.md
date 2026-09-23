# Issue #169 / O100D completion audit — 2026-09-19

## Audit scope

Baseline audited: `main@2f00a407cd896b5ae55562246f7db3bba0cf56ec`, which contains PR #200, PR #205, PR #208, PR #214, and PR #215.

Completion delta in this branch: first-class typed `PlaybackChainEvaluation` integration in `CadTopologyComparisonRepository`, exact playback-chain reopen resolution, canonical amplifier objective evidence validation, and focused topology/amplifier fixtures.

This audit does not introduce new acoustic or amplifier physics. It checks whether the Issue #169 acceptance surface is backed by exact authorities and concrete fixtures.

## Completion matrix

| Acceptance | Implementation file | Test / fixture | Status | Residual gap |
| --- | --- | --- | --- | --- |
| Coverage fraction / map | `backend/src/htdt/cad_coverage.py` — `CoverageEvaluationScenario`, per-seat coverage results, useful coverage fraction | `backend/tests/test_cad_coverage.py::test_on_axis_off_axis_multi_seat_frequency_coverage_and_objectives`; fail-closed population fixture | SATISFIED | None for O100D. No room-assisted coverage inference is added. |
| Worst-seat directivity loss | `backend/src/htdt/cad_coverage.py` — explicit per-seat off-axis loss and worst-seat aggregate | `test_on_axis_off_axis_multi_seat_frequency_coverage_and_objectives`; `test_negative_off_axis_loss_is_preserved_without_clamp` | SATISFIED | None. |
| Seat-to-seat coverage spread | `backend/src/htdt/cad_coverage.py` — directivity spread aggregate/objective | `test_on_axis_off_axis_multi_seat_frequency_coverage_and_objectives` | SATISFIED | None. |
| Source capability gating | `backend/src/htdt/cad_coverage.py`, `backend/src/htdt/cad_direct_level.py`, existing `EquipmentDefinition` / `DirectivityDataset` authorities | `test_domain_missing_seat_and_missing_aim_fail_closed_without_partial_fraction`; `test_capability_missing_variant_is_comparison_ineligible`; `test_frequency_domain_outside_equipment_authority_is_unsupported` | SATISFIED | Missing capability remains missing/unsupported; no synthetic 0 values. |
| Direct SPL | `backend/src/htdt/cad_direct_level.py` — direct/equipment-derived level authority | `backend/tests/test_cad_direct_level.py::test_direct_distance_level_target_margin_and_headroom_are_explicit` | SATISFIED | Explicitly not room-assisted SPL. |
| Target SPL margin | `backend/src/htdt/cad_direct_level.py` | `test_direct_distance_level_target_margin_and_headroom_are_explicit` | SATISFIED | None within direct/equipment-derived model. |
| Continuous acoustic headroom | `backend/src/htdt/cad_direct_level.py` | `test_direct_distance_level_target_margin_and_headroom_are_explicit` | SATISFIED | Kept separate from electrical headroom. |
| Peak acoustic headroom | `backend/src/htdt/cad_direct_level.py` | `test_direct_distance_level_target_margin_and_headroom_are_explicit` | SATISFIED | Kept separate from electrical headroom. |
| Amplifier electrical headroom | `backend/src/htdt/cad_amplifier_headroom.py`, `backend/src/htdt/cad_amplifier_headroom_repository.py` | `backend/tests/test_cad_amplifier_headroom.py::test_voltage_capability_has_explicit_continuous_peak_and_target_margins`; load/channel/duration fixtures | SATISFIED | No nominal-impedance promotion or unsupported V/W conversion. |
| Amplifier objective becomes first-class topology evidence | `backend/src/htdt/cad_topology_comparison.py::amplifier_headroom_evaluation_ref`; `backend/src/htdt/cad_topology_comparison_repository.py::_resolve_bundle_ref` | `backend/tests/test_cad_topology_amplifier_integration.py::test_typed_amplifier_objectives_compare_and_reopen_exactly` | SATISFIED | Separate `fidelity` label remains unset because `PlaybackChainEvaluation` has no independent fidelity authority; exact evaluation hash + comparison-model identity are retained. |
| Explicit playback / excitation conditions | `PlaybackExcitationScenario` in `backend/src/htdt/cad_direct_level.py`; `PlaybackChainScenario` in `backend/src/htdt/cad_amplifier_headroom.py` | `test_mismatched_excitation_reference_conditions_are_not_comparable`; `test_duration_and_channel_count_conditions_are_not_promoted`; integration semantic mismatch fixture | SATISFIED | No coherent multi-channel summation is invented. |
| Objective quantity / unit / direction / domain / model identity | `backend/src/htdt/optimization_objectives.py` and O100D objective builders | `backend/tests/test_optimization_objectives.py::test_pareto_rejects_incompatible_definition_unit_direction_or_model`; `test_objective_definition_identity_is_deterministic_and_domain_is_enforced` | SATISFIED | None. |
| Maximize / minimize Pareto | `backend/src/htdt/pareto.py`, direction-bearing `ObjectiveDefinition` | `test_mixed_minimize_maximize_pareto_uses_physical_direction_without_sign_flip`; `backend/tests/test_cad_coverage.py::test_coverage_maximize_and_loss_minimize_use_direction_aware_pareto` | SATISFIED | No hidden sign flip or composite score. |
| Missing / unsupported handling | `backend/src/htdt/optimization_objectives.py`; coverage/direct-level/amplifier evaluators; `MissingUnsupportedPolicy` in topology comparison | `test_missing_and_unsupported_objectives_have_no_numeric_substitute`; coverage/direct-level fail-closed fixtures; integration required/optional fixture | SATISFIED | No 0 / infinity sentinel. |
| Named current / proposed topology comparison | `backend/src/htdt/cad_topology_comparison.py` — `SystemTopologyComparisonSpec`, `ComparedSystemVariant`, `VariantEvaluationBundle`, `TopologyComparisonEvaluation` | `backend/tests/test_cad_topology_comparison.py::test_named_topology_comparison_exact_authority_pareto_and_reopen`; typed amplifier integration fixture | SATISFIED | Selection remains reference-only; no SystemVariant application. |
| Standards are not mixed into a hidden score | `VariantEvaluationBundle` keeps `standards_evaluation` separate; objective evidence excludes Standards; comparison eligibility does not implicitly remove Standards FAIL | `test_named_topology_comparison_exact_authority_pareto_and_reopen` | SATISFIED | Standards remain criterion-level evidence only. |
| Persistence / reopen | `CadCoverageRepository`, `CadDirectLevelRepository`, `CadAmplifierHeadroomRepository`, `CadTopologyComparisonRepository` | coverage/direct-level/amplifier round-trip tests; `test_typed_amplifier_objectives_compare_and_reopen_exactly` | SATISFIED | None. |
| Stale exact-authority rejection | Exact hash re-resolution in all typed repositories; new `CadAmplifierHeadroomRepository.resolve_evaluation_exact`; topology resolver compares the reconstructed `ExactAuthorityRef` | existing topology stale external-ref fixture plus `test_typed_resolver_rejects_stale_variant_and_baseline_authority` | SATISFIED | None. |
| Wrong SystemVariant / baseline SceneRevision rejection | Playback-chain exact resolver pins evaluation hash + document/revision/hash + variant id/hash; underlying reopen revalidates source equipment, amplifier capability, speaker load and routing | `test_typed_resolver_rejects_stale_variant_and_baseline_authority` | SATISFIED | None. |
| Different electrical quantity / unit / duration / channel-count / load semantics are not conflated | `PlaybackChainScenario.comparison_sha256` and amplifier objective `comparison_model_version` encode these conditions | `test_objectives_are_maximize_and_conditions_gate_comparison`; `test_duration_and_channel_count_conditions_are_not_promoted`; `test_missing_and_incompatible_amplifier_semantics_are_not_coerced` | SATISFIED | None. |
| Objective evidence is exact, not merely labeled | topology repository reconstructs the canonical amplifier objective vector from the reopened `PlaybackChainEvaluation` and rejects a bound metric that differs | `test_typed_amplifier_objectives_compare_and_reopen_exactly` plus typed stale/wrong-authority fixture | SATISFIED | This validation is intentionally limited to amplifier evidence added by this slice; it does not change Coverage/DirectLevel authorities. |

## Issue #169 acceptance conclusion

All acceptance items required by Issue #169 are backed by a concrete implementation authority and a fixture after the typed amplifier/topology integration in this branch.

The following are intentionally outside the completion requirement and are not blockers:

- room-assisted / solver-backed SPL;
- percentile objectives without an explicit percentile population/weighting authority;
- coherent multi-channel acoustic summation;
- automatic SystemVariant application or lifecycle promotion;
- GUI;
- O90 changes;
- a hidden cinema score.

Issue #169 may be closed only after the focused integration fixtures and the repository GitHub Actions CI pass on the pull request head. If either fails, keep #169 open and record the failing acceptance/residual gap in an issue comment.
