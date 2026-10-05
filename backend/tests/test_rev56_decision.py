"""REV56 (#577/#604): evidence-aware decision rule + uncertainty
propagation/robust design — verdict taxonomy, guard bands, manifest
anti-double-counting, deterministic propagation, rank reversal under
shared states, sealed-record integrity, fail-closed persistence."""

from __future__ import annotations

from math import isclose, sqrt

import pytest
from pydantic import ValidationError

from htdt.cad_decision_rule import (
    DecisionSubject,
    DominanceAxisEvidence,
    EvidenceRequest,
    GuardBand,
    PracticalEquivalence,
    RiskPolicy,
    UncertaintyCompositionManifest,
    UncertaintySourceRef,
    build_decision_rule_spec,
    evaluate_limit_conformity,
    evaluate_pareto_dominance,
    evaluate_pairwise_preference,
)
from htdt.cad_decision_rule_repository import (
    CadDecisionRuleRepository,
    DecisionRuleConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_robust_design_repository import (
    CadRobustDesignRepository,
    RobustDesignConflictError,
)
from htdt.cad_scene import make_empty_scene
from htdt.cad_uncertainty_propagation import (
    ExplicitInputState,
    PropagatedState,
    UncertainInput,
    build_propagation_plan,
    build_propagation_spec,
    build_robust_design_assessment,
    build_uncertain_input_set,
    compute_sensitivity,
    execute_propagation,
    summarize_observable,
)

NOW = '2026-10-05T00:00:00+00:00'
_H = 'a' * 64


def _source(source_id='u1', **overrides):
    kwargs = dict(
        source_id=source_id,
        source_class='measurement_uncertainty',
        representation='standard_uncertainty',
        value=0.3,
        unit='dB',
        scope='independent',
    )
    kwargs.update(overrides)
    return UncertaintySourceRef(**kwargs)


def _manifest(sources=(), method='independent_rss', **overrides):
    kwargs = dict(
        sources=tuple(sources),
        combination_method=method,
        combination_justification='test manifest justification',
    )
    kwargs.update(overrides)
    return UncertaintyCompositionManifest(**kwargs)


def _pairwise_rule(manifest=None, **overrides):
    kwargs = dict(
        document_id='doc-1',
        rule_version_label='test-rule-v1',
        decision_type='pairwise_candidate_preference',
        criterion_id='response.rms_difference_db',
        criterion_unit='dB',
        criterion_direction='minimize',
        uncertainty_manifest=manifest
        or _manifest((_source('u1'), _source('u2'))),
        risk_policy=RiskPolicy(policy_id='balanced_design_exploration'),
        created_at_utc=NOW,
    )
    kwargs.update(overrides)
    return build_decision_rule_spec(**kwargs)


def _subject(candidate_id='cand-a', evaluation_id='ev-a'):
    return DecisionSubject(
        candidate_id=candidate_id,
        evaluation_id=evaluation_id,
        evaluation_sha256='b' * 64,
    )


# ---------------------------------------------------------------------------
# #577 — manifest composition
# ---------------------------------------------------------------------------


def test_dr70_manifest_rejects_double_counted_overlap():
    covered = _source('envelope', representation='sampled_envelope', value=1.0)
    with pytest.raises(ValidationError, match='double counting'):
        _manifest((_source('u1', overlaps=('envelope',)), covered))


def test_dr70_excluded_overlap_resolves_without_double_counting():
    covered = _source(
        'envelope', representation='standard_uncertainty', value=1.0
    )
    manifest = _manifest(
        (
            _source(
                'u1',
                inclusion='excluded',
                reason='covered by envelope',
                representation='declared',
                value=None,
            ),
            covered,
        )
    )
    assert isclose(manifest.pairwise_resolution(), 1.0)


def test_dr70_common_mode_cancels_pairwise():
    manifest = _manifest(
        (
            _source('shared', scope='paired_common_mode', value=5.0),
            _source('u1'),
            _source('u2'),
        )
    )
    assert isclose(manifest.pairwise_resolution(), sqrt(0.3**2 + 0.3**2))


def test_dr70_declared_correlated_sums_inside_group():
    manifest = _manifest(
        (
            _source(
                'g1',
                scope='declared_correlated',
                correlation_group_id='mesh',
                value=0.4,
            ),
            _source(
                'g2',
                scope='declared_correlated',
                correlation_group_id='mesh',
                value=0.4,
            ),
            _source('independent', value=0.0),
        )
    )
    # (0.4 + 0.4) linear inside the group, quadrature across groups
    assert isclose(manifest.pairwise_resolution(), 0.8)


def test_manifest_rss_requires_standard_uncertainties():
    with pytest.raises(ValidationError, match='standard/expanded'):
        _manifest((_source('u1', representation='bounded_interval'),))


def test_manifest_linear_sum_requires_intervals():
    with pytest.raises(
        ValidationError, match='bounded_linear_sum requires'
    ):
        _manifest((_source('u1'),), method='bounded_linear_sum')


def test_manifest_declared_resolution_needs_value():
    with pytest.raises(ValidationError, match='declared resolution value'):
        _manifest(
            (),
            method='declared_resolution',
            declared_resolution=None,
        )


def test_manifest_empty_sources_resolve_to_none():
    manifest = _manifest((), method='bounded_linear_sum')
    assert manifest.pairwise_resolution() is None


# ---------------------------------------------------------------------------
# #577 — pairwise preference verdicts
# ---------------------------------------------------------------------------


def test_dr10_below_combined_resolution_is_indeterminate():
    rule = _pairwise_rule()
    verdict = evaluate_pairwise_preference(
        rule,
        candidate_a=_subject('a', 'ev-a'),
        candidate_b=_subject('b', 'ev-b'),
        value_a=1.0,
        value_b=1.05,  # |Δ|=0.05 << resolution ~0.42
        created_at_utc=NOW,
    )
    assert verdict.verdict == 'evidentially_indeterminate'
    assert verdict.leading_candidate_id is None
    assert 'separation_below_evidence_resolution' in verdict.reason_codes
    assert verdict.rule_sha256 == rule.semantic_sha256


def test_dr10_nominal_order_display_only_when_policy_allows():
    rule = _pairwise_rule(
        risk_policy=RiskPolicy(
            policy_id='experimental_research',
            allow_nominal_rank_display=True,
        )
    )
    verdict = evaluate_pairwise_preference(
        rule,
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        value_a=1.0,
        value_b=1.05,
        created_at_utc=NOW,
    )
    assert verdict.verdict == 'evidentially_indeterminate'
    assert verdict.leading_candidate_id is None
    assert 'nominal_leader_display_only' in verdict.reason_codes


def test_dr20_clearly_superior_within_declared_evidence():
    rule = _pairwise_rule()
    verdict = evaluate_pairwise_preference(
        rule,
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        value_a=0.5,  # Δ=1.0 > resolution ~0.42, minimize → A better
        value_b=1.5,
        created_at_utc=NOW,
    )
    assert verdict.verdict == 'clearly_superior_within_declared_evidence'
    assert verdict.leading_candidate_id == 'a'


def test_dr30_resolved_but_below_practical_threshold_is_equivalent():
    rule = _pairwise_rule(
        practical_equivalence=PracticalEquivalence(
            threshold=2.0,
            basis='project_tolerance',
            basis_detail='perceptual difference below 2 dB not meaningful '
            'for this project',
        )
    )
    verdict = evaluate_pairwise_preference(
        rule,
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        value_a=0.5,
        value_b=1.5,  # resolved (Δ=1.0>0.42) but < 2.0 practical threshold
        created_at_utc=NOW,
    )
    assert verdict.verdict == 'practically_equivalent_within_tolerance'
    assert verdict.leading_candidate_id is None


def test_dr40_no_declared_uncertainty_is_insufficient_evidence():
    rule = _pairwise_rule(
        uncertainty_manifest=_manifest((), method='bounded_linear_sum')
    )
    verdict = evaluate_pairwise_preference(
        rule,
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        value_a=0.5,
        value_b=1.5,
        created_at_utc=NOW,
    )
    assert verdict.verdict == 'insufficient_evidence'
    assert 'no_declared_uncertainty_resolution' in verdict.reason_codes
    assert verdict.leading_candidate_id is None


def test_dr50_smaller_objective_does_not_auto_imply_stronger_verdict():
    rule = _pairwise_rule(
        uncertainty_manifest=_manifest(
            (_source('big', value=10.0), _source('big2', value=10.0))
        )
    )
    verdict = evaluate_pairwise_preference(
        rule,
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        value_a=0.5,
        value_b=5.0,  # huge nominal gap, still under huge resolution
        created_at_utc=NOW,
    )
    assert verdict.verdict == 'evidentially_indeterminate'


def test_pairwise_indeterminate_carries_value_of_information_request():
    request = EvidenceRequest(
        request_kind='higher_fidelity_solve',
        target='response.rms_difference_db',
        rationale='current model error dominates the difference',
    )
    rule = _pairwise_rule()
    verdict = evaluate_pairwise_preference(
        rule,
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        value_a=1.0,
        value_b=1.05,
        evidence_requests=(request,),
        created_at_utc=NOW,
    )
    assert verdict.evidence_requests == (request,)


# ---------------------------------------------------------------------------
# #577 — guard band conformity
# ---------------------------------------------------------------------------


def _conformity_rule(limit=80.0, acceptance=79.0, direction='upper_limit'):
    return build_decision_rule_spec(
        document_id='doc-1',
        rule_version_label='limit-rule-v1',
        decision_type='hard_constraint_conformity',
        criterion_id='spl_max_db',
        criterion_unit='dB SPL',
        criterion_direction=direction,
        guard_band=GuardBand(
            specification_limit=limit,
            direction='upper' if direction == 'upper_limit' else 'lower',
            acceptance_limit=acceptance,
            uncertainty_basis='U=1dB measurement uncertainty at 95%',
            risk_policy='false_accept_guarded',
        ),
        uncertainty_manifest=_manifest(
            (_source('u1', value=0.5),)
        ),
        risk_policy=RiskPolicy(policy_id='conservative_production'),
        created_at_utc=NOW,
    )


def test_dr50_pass_with_guard_band():
    verdict = evaluate_limit_conformity(
        _conformity_rule(),
        subject=_subject(),
        point_estimate=78.0,
        created_at_utc=NOW,
    )
    assert verdict.verdict == 'constraint_pass_with_guard_band'


def test_dr50_straddling_acceptance_is_not_yet_conforming():
    verdict = evaluate_limit_conformity(
        _conformity_rule(),
        subject=_subject(),
        point_estimate=79.5,  # > acceptance 79.0, <= spec 80.0
        created_at_utc=NOW,
    )
    assert verdict.verdict == 'constraint_not_yet_conforming'


def test_dr50_beyond_spec_is_fail():
    verdict = evaluate_limit_conformity(
        _conformity_rule(),
        subject=_subject(),
        point_estimate=81.0,
        created_at_utc=NOW,
    )
    assert verdict.verdict == 'constraint_fail'


def test_guard_band_rejects_inverted_direction():
    with pytest.raises(ValidationError, match='guard band'):
        GuardBand(
            specification_limit=80.0,
            direction='upper',
            acceptance_limit=81.0,  # looser than spec — not a guard band
            uncertainty_basis='none',
            risk_policy='balanced',
        )


def test_conformity_rule_requires_guard_band():
    with pytest.raises(ValidationError, match='guard band'):
        build_decision_rule_spec(
            document_id='doc-1',
            rule_version_label='v1',
            decision_type='hard_constraint_conformity',
            criterion_id='spl',
            criterion_direction='upper_limit',
            uncertainty_manifest=_manifest((_source(),)),
            risk_policy=RiskPolicy(policy_id='balanced_design_exploration'),
            created_at_utc=NOW,
        )


# ---------------------------------------------------------------------------
# #577 — Pareto dominance under uncertainty
# ---------------------------------------------------------------------------


def _dominance_rule(**overrides):
    kwargs = dict(
        document_id='doc-1',
        rule_version_label='dom-rule-v1',
        decision_type='pareto_dominance',
        criterion_id='multiobjective',
        criterion_direction='minimize',
        uncertainty_manifest=_manifest(
            (_source('u1'),), method='independent_rss'
        ),
        risk_policy=RiskPolicy(policy_id='balanced_design_exploration'),
        created_at_utc=NOW,
    )
    kwargs.update(overrides)
    return build_decision_rule_spec(**kwargs)


def _axis(
    objective_id,
    a=(1.0, 2.0),
    b=(3.0, 4.0),
    nominal_a=1.5,
    nominal_b=3.5,
    **overrides,
):
    kwargs = dict(
        objective_id=objective_id,
        direction='minimize',
        nominal_a=nominal_a,
        nominal_b=nominal_b,
        interval_a=a,
        interval_b=b,
    )
    kwargs.update(overrides)
    return DominanceAxisEvidence(**kwargs)


def test_dr60_robust_dominance_when_all_intervals_separate():
    verdict = evaluate_pareto_dominance(
        _dominance_rule(),
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        axes=(
            _axis('o1', a=(0.0, 1.0), b=(2.0, 3.0)),
            _axis('o2', a=(0.0, 0.5), b=(0.6, 1.0), nominal_a=0.2, nominal_b=0.8),
        ),
        created_at_utc=NOW,
    )
    assert verdict.dominance_verdict == 'robustly_dominates_within_tested_domain'
    assert verdict.verdict == 'clearly_superior_within_declared_evidence'
    assert verdict.leading_candidate_id == 'a'


def test_dr60_overlapping_intervals_downgrade_to_dominance_uncertain():
    verdict = evaluate_pareto_dominance(
        _dominance_rule(),
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        axes=(
            _axis('o1', a=(0.0, 3.0), b=(2.0, 4.0)),  # overlap
            _axis('o2', a=(0.0, 0.5), b=(0.6, 1.0), nominal_a=0.2, nominal_b=0.8),
        ),
        created_at_utc=NOW,
    )
    assert verdict.dominance_verdict == 'dominance_uncertain'
    assert verdict.verdict == 'evidentially_indeterminate'
    assert verdict.leading_candidate_id is None


def test_dr60_resolved_conflict_is_tradeoff():
    verdict = evaluate_pareto_dominance(
        _dominance_rule(),
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        axes=(
            _axis('o1', a=(0.0, 1.0), b=(2.0, 3.0)),  # A robustly better
            _axis('o2', a=(2.0, 3.0), b=(0.0, 1.0), nominal_a=2.5, nominal_b=0.5),
        ),
        created_at_utc=NOW,
    )
    assert verdict.dominance_verdict == 'tradeoff'
    assert verdict.verdict == 'conflicting_objectives'


def test_dr60_resolution_only_axes_give_nominal_dominance():
    verdict = evaluate_pareto_dominance(
        _dominance_rule(),
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        axes=(
            _axis('o1', a=None, b=None, resolution=0.1),
            _axis('o2', a=None, b=None, resolution=0.1),
        ),
        created_at_utc=NOW,
    )
    assert verdict.dominance_verdict == 'nominally_dominates'
    assert verdict.verdict == 'superior_with_limitations'
    # nominal-only dominance names the leader but records the reversal risk
    assert verdict.leading_candidate_id == 'a'
    assert 'nominal_dominance_only_not_robust' in verdict.reason_codes


def test_dr60_no_resolution_axis_is_insufficient_evidence():
    verdict = evaluate_pareto_dominance(
        _dominance_rule(),
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        axes=(_axis('o1', a=None, b=None, resolution=None),),
        created_at_utc=NOW,
    )
    assert verdict.dominance_verdict == 'dominance_insufficient_evidence'
    assert verdict.verdict == 'insufficient_evidence'


# ---------------------------------------------------------------------------
# #577 — sealing + repository round-trip
# ---------------------------------------------------------------------------


def test_rule_is_sealed_and_tamper_rejected():
    rule = _pairwise_rule()
    assert rule.rule_id == f'decision-rule:{rule.semantic_sha256}'
    payload = rule.model_dump(mode='json')
    payload['criterion_id'] = 'other'
    with pytest.raises(ValidationError, match='hash mismatch'):
        type(rule).model_validate(payload)


def test_verdict_is_sealed_and_tamper_rejected():
    verdict = evaluate_pairwise_preference(
        _pairwise_rule(),
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        value_a=0.5,
        value_b=1.5,
        created_at_utc=NOW,
    )
    assert verdict.verdict_id == f'decision-verdict:{verdict.semantic_sha256}'
    payload = verdict.model_dump(mode='json')
    payload['document_id'] = 'doc-9'
    with pytest.raises(ValidationError, match='hash mismatch'):
        type(verdict).model_validate(payload)


def test_superiority_verdict_requires_named_leader():
    from htdt.canonical_json import canonical_sha256

    verdict = evaluate_pairwise_preference(
        _pairwise_rule(),
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        value_a=0.5,
        value_b=1.5,
        created_at_utc=NOW,
    )
    payload = verdict.model_dump(mode='json')
    payload['verdict'] = 'clearly_superior_within_declared_evidence'
    payload['leading_candidate_id'] = None
    digest = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key not in ('verdict_id', 'semantic_sha256')
        }
    )
    payload['semantic_sha256'] = digest
    payload['verdict_id'] = f'decision-verdict:{digest}'
    with pytest.raises(ValidationError, match='leading candidate'):
        type(verdict).model_validate(payload)


def test_dr80_user_override_policy_requires_note():
    with pytest.raises(ValidationError, match='override_note'):
        RiskPolicy(policy_id='user_explicit_override')


def test_repository_round_trip_rules_and_verdicts(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene('doc-1'), parent_revision_id=None)
    repository = CadDecisionRuleRepository(scene_repository)

    rule = _pairwise_rule()
    repository.save_rule(rule)
    repository.save_rule(rule)  # idempotent
    assert repository.get_rule(rule.rule_id) == rule

    verdict = evaluate_pairwise_preference(
        rule,
        candidate_a=_subject('a'),
        candidate_b=_subject('b'),
        value_a=0.5,
        value_b=1.5,
        created_at_utc=NOW,
    )
    repository.save_verdict(verdict)
    repository.save_verdict(verdict)  # idempotent
    assert repository.get_verdict(verdict.verdict_id) == verdict
    assert repository.list_verdicts_for_rule(rule.rule_id) == (verdict,)
    assert repository.list_verdicts_for_document('doc-1') == (verdict,)


def test_repository_conflict_on_different_payload(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene('doc-1'), parent_revision_id=None)
    repository = CadDecisionRuleRepository(scene_repository)
    rule = _pairwise_rule()
    repository.save_rule(rule)
    tampered = rule.model_construct(
        **{**rule.model_dump(), 'criterion_id': 'different'}
    )
    with pytest.raises(DecisionRuleConflictError):
        repository.save_rule(tampered)


# ---------------------------------------------------------------------------
# #604 — uncertain inputs + propagation plan
# ---------------------------------------------------------------------------


def _input(input_id='mat', **overrides):
    kwargs = dict(
        input_id=input_id,
        kind='material_absorption',
        uncertainty_class='epistemic',
        representation='bounded_interval',
        target_kind='material',
        target_ref='wall-fabric',
        bound_low=-0.05,
        bound_high=0.05,
    )
    kwargs.update(overrides)
    return UncertainInput(**kwargs)


def _input_set(inputs, **overrides):
    kwargs = dict(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_H,
        inputs=inputs,
        created_at_utc=NOW,
    )
    kwargs.update(overrides)
    return build_uncertain_input_set(**kwargs)


def test_uq_input_requires_representation_consistent_fields():
    with pytest.raises(ValidationError, match='bound_low'):
        _input(representation='bounded_interval', bound_low=None)
    with pytest.raises(ValidationError, match='empirical_samples'):
        _input(
            representation='empirical_samples',
            bound_low=None,
            bound_high=None,
            empirical_samples=None,
        )
    with pytest.raises(ValidationError, match='coverage_probability'):
        _input(
            representation='credible_interval',
            coverage_probability=None,
        )
    with pytest.raises(ValidationError, match='correlation_group_id'):
        _input(
            representation='correlated_samples',
            bound_low=None,
            bound_high=None,
            empirical_samples=(1.0, 2.0),
        )


def test_uq_input_no_silent_tolerance_to_gaussian():
    # a bounded interval never becomes a distribution implicitly
    item = _input()
    assert item.representation == 'bounded_interval'
    assert item.distribution is None


def test_uq_input_set_sealed():
    input_set = _input_set((_input(),))
    assert input_set.input_set_id.startswith('uncertain-input-set:')
    payload = input_set.model_dump(mode='json')
    payload['inputs'][0]['bound_high'] = 9.0
    with pytest.raises(ValidationError, match='hash mismatch'):
        type(input_set).model_validate(payload)


def test_uq_spec_rejects_wrong_input_set():
    input_set = _input_set((_input(),))
    other = _input_set((_input(input_id='other', bound_high=0.5),))
    spec = build_propagation_spec(
        input_set_id=other.input_set_id,
        input_set_sha256=other.semantic_sha256,
        method='deterministic_corner_pairs',
        sample_count=3,
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='pin this input set'):
        build_propagation_plan(input_set, spec)


def test_uq_corner_plan_nominal_plus_per_input_corners():
    input_set = _input_set(
        (
            _input('a', bound_low=-0.1, bound_high=0.1),
            _input('b', bound_low=-0.2, bound_high=0.2),
        )
    )
    spec = build_propagation_spec(
        input_set_id=input_set.input_set_id,
        input_set_sha256=input_set.semantic_sha256,
        method='deterministic_corner_pairs',
        sample_count=5,
        created_at_utc=NOW,
    )
    plan = build_propagation_plan(input_set, spec)
    assert plan[0].step == 'nominal'
    assert len(plan) == 5  # 1 + 2 inputs * 2 corners
    assert {state.focus_input_id for state in plan[1:]} == {'a', 'b'}


def test_uq_plan_is_deterministic():
    input_set = _input_set((_input(), _input('b')))
    spec = build_propagation_spec(
        input_set_id=input_set.input_set_id,
        input_set_sha256=input_set.semantic_sha256,
        method='deterministic_low_discrepancy',
        sampling_seed=42,
        sample_count=8,
        created_at_utc=NOW,
    )
    first = build_propagation_plan(input_set, spec)
    second = build_propagation_plan(input_set, spec)
    assert first == second


def test_uq50_correlated_samples_share_joint_row():
    input_set = _input_set(
        (
            _input(
                'gx',
                kind='geometry_dimension',
                representation='correlated_samples',
                bound_low=None,
                bound_high=None,
                empirical_samples=(0.0, 0.1, 0.2, 0.3),
                correlation_group_id='geom',
            ),
            _input(
                'gy',
                kind='geometry_dimension',
                representation='correlated_samples',
                bound_low=None,
                bound_high=None,
                empirical_samples=(10.0, 20.0, 30.0, 40.0),
                correlation_group_id='geom',
            ),
        )
    )
    spec = build_propagation_spec(
        input_set_id=input_set.input_set_id,
        input_set_sha256=input_set.semantic_sha256,
        method='deterministic_low_discrepancy',
        sampling_seed=7,
        sample_count=10,
        created_at_utc=NOW,
    )
    plan = build_propagation_plan(input_set, spec)
    # every sampled state draws a shared row: gy == gx_index * 100
    rows = {(0.0, 10.0), (0.1, 20.0), (0.2, 30.0), (0.3, 40.0)}
    for state in plan[1:]:
        pair = (state.state_values['gx'], state.state_values['gy'])
        assert pair in rows


def test_uq_explicit_states_carry_weights():
    input_set = _input_set((_input(),))
    spec = build_propagation_spec(
        input_set_id=input_set.input_set_id,
        input_set_sha256=input_set.semantic_sha256,
        method='explicit_states',
        sample_count=3,
        explicit_states=(
            ExplicitInputState(
                state_id='s1',
                state_values={'mat': -0.03},
                probability_weight=0.25,
            ),
            ExplicitInputState(
                state_id='s2',
                state_values={'mat': 0.04},
                probability_weight=0.75,
            ),
        ),
        created_at_utc=NOW,
    )
    plan = build_propagation_plan(input_set, spec)
    assert plan[1].probability_weight == 0.25
    assert plan[2].probability_weight == 0.75
    with pytest.raises(ValidationError, match='sum to 1'):
        build_propagation_spec(
            input_set_id=input_set.input_set_id,
            input_set_sha256=input_set.semantic_sha256,
            method='explicit_states',
            sample_count=2,
            explicit_states=(
                ExplicitInputState(
                    state_id='s1',
                    state_values={'mat': 0.0},
                    probability_weight=0.5,
                ),
            ),
            created_at_utc=NOW,
        )


# ---------------------------------------------------------------------------
# #604 — propagation + summaries + pairwise reversal
# ---------------------------------------------------------------------------


def _uq_environment(inputs, candidates=('cand-a', 'cand-b')):
    input_set = _input_set(inputs)
    spec = build_propagation_spec(
        input_set_id=input_set.input_set_id,
        input_set_sha256=input_set.semantic_sha256,
        method='deterministic_low_discrepancy',
        sampling_seed=13,
        sample_count=12,
        created_at_utc=NOW,
    )
    return input_set, spec, candidates


def _linear_evaluator(coefficients, nominal=0.0):
    """observable = nominal + Σ c_i * state_value_i (additive deltas)."""

    def evaluate(candidate_id, state: PropagatedState):
        value = nominal
        for input_id, coefficient in coefficients.items():
            value += coefficient * state.state_values.get(input_id, 0.0)
        return {'obs': value}

    return evaluate


def test_uq10_sensitivity_shows_dominant_input():
    input_set = _input_set(
        (
            _input('mat', bound_low=-0.5, bound_high=0.5),
            _input(
                'pos',
                kind='source_position',
                bound_low=-0.01,
                bound_high=0.01,
            ),
        )
    )
    spec = build_propagation_spec(
        input_set_id=input_set.input_set_id,
        input_set_sha256=input_set.semantic_sha256,
        method='deterministic_corner_pairs',
        sample_count=5,
        created_at_utc=NOW,
    )
    result = execute_propagation(
        input_set=input_set,
        spec=spec,
        evaluator=_linear_evaluator({'mat': 1.0, 'pos': 1.0}),
        candidate_ids=('cand-a',),
    )
    study = compute_sensitivity(
        input_set=input_set,
        spec=spec,
        samples=result.samples,
        candidate_id='cand-a',
    )
    assert study is not None
    by_input = {item.input_id: item for item in study.contributions}
    assert by_input['mat'].value > 10 * by_input['pos'].value
    assert by_input['mat'].share == pytest.approx(
        by_input['mat'].value / (by_input['mat'].value + by_input['pos'].value)
    )


def test_uq20_envelope_reflects_position_sensitive_optimum():
    # sharp optimum at x=0: f = 10*x^2 — symmetric interval still moves the
    # sampled max well above nominal (position sensitivity is exposed).
    input_set = _input_set(
        (_input('x', kind='receiver_position', bound_low=-0.3, bound_high=0.3),)
    )
    spec = build_propagation_spec(
        input_set_id=input_set.input_set_id,
        input_set_sha256=input_set.semantic_sha256,
        method='deterministic_low_discrepancy',
        sampling_seed=3,
        sample_count=10,
        created_at_utc=NOW,
    )
    result = execute_propagation(
        input_set=input_set,
        spec=spec,
        evaluator=lambda cid, state: {
            'obs': 10.0 * state.state_values.get('x', 0.0) ** 2
        },
        candidate_ids=('cand-a',),
    )
    summary = summarize_observable(observable_id='obs', samples=result.samples)
    assert summary is not None
    assert summary.nominal == 0.0
    assert summary.sampled_max > 0.1


def test_uq40_paired_rank_reversal_detected_under_shared_states():
    # cand-a nominal better; cand-b better whenever delta > 0.5 — a real
    # reversal the paired comparison must expose.
    input_set, spec, candidates = _uq_environment(
        (_input('d', bound_low=0.0, bound_high=1.0),)
    )

    def evaluator(candidate_id, state):
        delta = state.state_values.get('d', 0.0)
        if candidate_id == 'cand-a':
            return {'obs': 1.0}
        return {'obs': 1.2 - delta}  # nominal 1.2 (worse), better when d>0.2

    result = execute_propagation(
        input_set=input_set,
        spec=spec,
        evaluator=evaluator,
        candidate_ids=candidates,
    )
    assessment = build_robust_design_assessment(
        input_set=input_set,
        spec=spec,
        samples=result.samples,
        candidate_ids=candidates,
        objective_ids=('obs',),
        created_at_utc=NOW,
    )
    pair = assessment.pairwise[0]
    assert pair.nominal_difference == pytest.approx(-0.2)
    assert pair.paired_reversal_fraction is not None
    assert pair.paired_reversal_fraction > 0.0


def test_uq30_robust_alternative_profile_shows_tighter_envelope():
    input_set, spec, candidates = _uq_environment(
        (_input('d', bound_low=0.0, bound_high=1.0),)
    )

    def evaluator(candidate_id, state):
        delta = state.state_values.get('d', 0.0)
        if candidate_id == 'cand-a':
            return {'obs': 0.5 + 2.0 * delta}   # nominal best, wide spread
        return {'obs': 1.0 + 0.01 * delta}      # nominal worse, robust

    result = execute_propagation(
        input_set=input_set,
        spec=spec,
        evaluator=evaluator,
        candidate_ids=candidates,
    )
    assessment = build_robust_design_assessment(
        input_set=input_set,
        spec=spec,
        samples=result.samples,
        candidate_ids=candidates,
        objective_ids=('obs',),
        created_at_utc=NOW,
    )
    profiles = {item.candidate_id: item for item in assessment.candidates}
    a = profiles['cand-a'].summaries[0]
    b = profiles['cand-b'].summaries[0]
    assert a.nominal < b.nominal
    assert (a.sampled_max - a.sampled_min) > 50 * (
        b.sampled_max - b.sampled_min
    )


def test_uq60_two_inputs_each_contribute():
    input_set = _input_set(
        (
            _input('c1', kind='model_calibration_parameter', bound_low=-1.0, bound_high=1.0),
            _input('c2', kind='model_calibration_parameter', bound_low=-1.0, bound_high=1.0),
        )
    )
    spec = build_propagation_spec(
        input_set_id=input_set.input_set_id,
        input_set_sha256=input_set.semantic_sha256,
        method='deterministic_corner_pairs',
        sample_count=5,
        created_at_utc=NOW,
    )
    # opposing effects — neither dominates; both contribute
    result = execute_propagation(
        input_set=input_set,
        spec=spec,
        evaluator=_linear_evaluator({'c1': 1.0, 'c2': -1.0}),
        candidate_ids=('cand-a',),
    )
    study = compute_sensitivity(
        input_set=input_set,
        spec=spec,
        samples=result.samples,
        candidate_id='cand-a',
    )
    assert study is not None
    shares = {item.input_id: item.share for item in study.contributions}
    assert isclose(shares['c1'], 0.5, rel_tol=1e-9)
    assert isclose(shares['c2'], 0.5, rel_tol=1e-9)


def test_uq70_evidence_requests_and_limitations_persist_in_assessment():
    input_set, spec, candidates = _uq_environment((_input(),), ('solo',))

    def evaluator(candidate_id, state):
        return {'obs': state.state_values.get('mat', 0.0)}

    result = execute_propagation(
        input_set=input_set,
        spec=spec,
        evaluator=evaluator,
        candidate_ids=candidates,
    )
    assessment = build_robust_design_assessment(
        input_set=input_set,
        spec=spec,
        samples=result.samples,
        candidate_ids=candidates,
        objective_ids=('obs',),
        evidence_requests=(
            'measure wall material absorption in situ to narrow the epistemic bound',
        ),
        limitations=(
            'exploratory 12-state sweep — no convergence claim',
            'surrogate-class inputs not covered by this assessment',
        ),
        created_at_utc=NOW,
    )
    assert assessment.evidence_requests[0].startswith('measure wall')
    assert 'no convergence claim' in assessment.limitations[0]
    assert assessment.assessment_id.startswith('robust-design:')


def test_uq_failure_samples_are_evidence_not_silent():
    input_set, spec, candidates = _uq_environment((_input(),), ('solo',))

    def evaluator(candidate_id, state):
        if state.step == 'sampled':
            raise RuntimeError('solver diverged')
        return {'obs': 1.0}

    result = execute_propagation(
        input_set=input_set,
        spec=spec,
        evaluator=evaluator,
        candidate_ids=candidates,
    )
    failed = [s for s in result.samples if s.failure_reason]
    assert len(failed) > 0
    assert all(s.failure_reason.startswith('evaluation_failed:') for s in failed)


def test_uq_propagation_cancellation():
    input_set, spec, candidates = _uq_environment((_input(),), ('solo',))
    calls = {'count': 0}

    def evaluator(candidate_id, state):
        calls['count'] += 1
        return {'obs': 1.0}

    result = execute_propagation(
        input_set=input_set,
        spec=spec,
        evaluator=evaluator,
        candidate_ids=candidates,
        cancel_requested=lambda: calls['count'] >= 3,
    )
    assert result.status == 'cancelled'
    assert result.computed_count == 3


def test_uq_weighted_explicit_states_produce_weighted_expected():
    input_set = _input_set((_input(),))
    spec = build_propagation_spec(
        input_set_id=input_set.input_set_id,
        input_set_sha256=input_set.semantic_sha256,
        method='explicit_states',
        sample_count=3,
        explicit_states=(
            ExplicitInputState(
                state_id='s1', state_values={'mat': 0.0},
                probability_weight=0.1,
            ),
            ExplicitInputState(
                state_id='s2', state_values={'mat': 10.0},
                probability_weight=0.9,
            ),
        ),
        created_at_utc=NOW,
    )
    result = execute_propagation(
        input_set=input_set,
        spec=spec,
        evaluator=_linear_evaluator({'mat': 1.0}, nominal=0.0),
        candidate_ids=('solo',),
    )
    summary = summarize_observable(observable_id='obs', samples=result.samples)
    assert summary is not None
    # 0.0*1 + 0.1*0 + 0.9*10 = 9.0 over feasible samples
    assert summary.expected == pytest.approx(9.0)
    assert summary.expected_semantics == 'weighted_mean'


def test_uq_assessment_repository_round_trip(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_empty_scene('doc-1'), parent_revision_id=None
    ).revision
    repository = CadRobustDesignRepository(scene_repository)

    input_set = _input_set(
        (_input(),),
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
    )
    repository.save_input_set(input_set)
    repository.save_input_set(input_set)
    assert repository.get_input_set(input_set.input_set_id) == input_set

    spec = build_propagation_spec(
        input_set_id=input_set.input_set_id,
        input_set_sha256=input_set.semantic_sha256,
        method='deterministic_corner_pairs',
        sample_count=3,
        created_at_utc=NOW,
    )
    result = execute_propagation(
        input_set=input_set,
        spec=spec,
        evaluator=_linear_evaluator({'mat': 1.0}),
        candidate_ids=('solo',),
    )
    assessment = build_robust_design_assessment(
        input_set=input_set,
        spec=spec,
        samples=result.samples,
        candidate_ids=('solo',),
        objective_ids=('obs',),
        created_at_utc=NOW,
    )
    repository.save_assessment(assessment)
    assert repository.get_assessment(assessment.assessment_id) == assessment
    assert repository.list_assessments_for_input_set(
        input_set.input_set_id
    ) == (assessment,)

    tampered = assessment.model_construct(
        **{**assessment.model_dump(), 'document_id': 'doc-2'}
    )
    with pytest.raises(RobustDesignConflictError):
        repository.save_assessment(tampered)
