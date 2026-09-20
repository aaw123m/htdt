from __future__ import annotations

from types import SimpleNamespace

class _Vector:
    def __init__(self, value: float) -> None:
        self.value = value

    def metric(self, _objective_id: str):
        return SimpleNamespace(value=self.value)


from htdt.optimization_robustness_presenter import (
    build_robustness_candidate_presentation,
    robustness_comparison_eligibility,
)


def _spec(**updates):
    values = dict(
        candidate_id='candidate-a',
        robustness_spec_id='rob-a',
        robustness_spec_sha256='1' * 64,
        scene_revision_id='rev-1',
        scene_content_hash='2' * 64,
        search_spec_id='search-1',
        search_spec_sha256='3' * 64,
        constraint_workspace_hash='7' * 64,
        nominal_objective_evaluation_id='objective-a',
        nominal_objective_evaluation_sha256='4' * 64,
        objective_evaluation_spec_sha256='5' * 64,
        model_id='model',
        model_version='1',
        prediction_provider_id='solver',
        fidelity='final',
        sampling_strategy='deterministic_multidimensional_bounded',
        sample_count=5,
        axes=(SimpleNamespace(axis_id='speaker-x', parameter='speaker_x_m'),),
    )
    values.update(updates)
    return SimpleNamespace(**values)


def _evaluation(**updates):
    values = dict(
        robustness_spec_id='rob-a',
        robustness_spec_sha256='1' * 64,
        candidate_id='candidate-a',
        objective_id='response.shape_rms_db',
        objective_unit='dB',
        direction='minimize',
        nominal_value=1.0,
        sampled_worst_value=2.0,
        local_sensitivities=(
            SimpleNamespace(
                axis_id='speaker-x',
                input_unit='m',
                objective_unit='dB',
                central_slope_per_unit=3.0,
                minus_slope_per_unit=None,
                plus_slope_per_unit=None,
                state='available',
            ),
        ),
        percentile_semantics='not_available_bounded_interval',
        probability_semantics=None,
        sampled_envelope=SimpleNamespace(percentile_values=None),
        mean_value=None,
        constraint_violation_probability=None,
        feasible_fraction=0.8,
    )
    values.update(updates)
    return SimpleNamespace(**values)


def _sample(index: int, *, feasible=True, failure_reason=None, **updates):
    values = dict(
        sample_id=f'sample-{index}',
        robustness_spec_id='rob-a',
        robustness_spec_sha256='1' * 64,
        candidate_id='candidate-a',
        model_id='model',
        model_version='1',
        prediction_provider_id='solver',
        fidelity='final',
        objective_evaluation_spec_sha256='5' * 64,
        feasible=feasible,
        failure_reason=failure_reason,
        objective_vector=(
            _Vector(1.0 + index * 0.1)
            if feasible and failure_reason is None
            else None
        ),
        probability_weight=None,
    )
    values.update(updates)
    return SimpleNamespace(**values)


def _current_authorities(**updates):
    revision = SimpleNamespace(revision_id='rev-1', content_hash='2' * 64)
    search = SimpleNamespace(
        search_spec_id='search-1',
        search_spec_sha256='3' * 64,
        constraint_workspace_hash='7' * 64,
    )
    objective = SimpleNamespace(
        evaluation_id='objective-a',
        evaluation_sha256='4' * 64,
        evaluation_spec_sha256='5' * 64,
    )
    for key, value in updates.items():
        if key.startswith('revision_'):
            setattr(revision, key.removeprefix('revision_'), value)
        elif key.startswith('search_'):
            setattr(search, key.removeprefix('search_'), value)
        else:
            setattr(objective, key, value)
    return revision, search, objective


def _presentation(spec=None, evaluation=None, samples=None, authorities=None):
    revision, search, objective = authorities or _current_authorities()
    return build_robustness_candidate_presentation(
        spec=spec or _spec(),
        evaluations=(evaluation or _evaluation(),),
        samples=tuple(samples or (_sample(0), _sample(1), _sample(2), _sample(3), _sample(4, feasible=False))),
        current_revision=revision,
        current_search_spec=search,
        nominal_objective=objective,
        current_constraint_workspace_hash='7' * 64,
    )


def test_bounded_uncertainty_never_exposes_fake_percentile_or_probability() -> None:
    view = _presentation()
    metric = view.objectives[0]

    assert metric.percentile_p95 is None
    assert metric.percentile_supported is False
    assert metric.probability_supported is False
    assert '確率分布が設定されていない' in metric.percentile_reason


def test_explicit_probability_model_exposes_p95_and_violation_probability() -> None:
    evaluation = _evaluation(
        percentile_semantics='explicit_probability_model',
        probability_semantics='explicit_distribution',
        sampled_envelope=SimpleNamespace(
            percentile_values={'p05': 0.8, 'p50': 1.1, 'p95': 1.8}
        ),
        mean_value=1.2,
        constraint_violation_probability=0.1,
    )
    metric = _presentation(evaluation=evaluation).objectives[0]

    assert metric.percentile_supported is True
    assert metric.percentile_p95 == 1.8
    assert metric.probability_supported is True
    assert metric.violation_probability == 0.1


def test_objective_direction_is_preserved_for_minimize_and_maximize() -> None:
    minimize = _presentation().objectives[0]
    maximize = _presentation(
        evaluation=_evaluation(
            direction='maximize',
            nominal_value=0.9,
            sampled_worst_value=0.6,
        )
    ).objectives[0]

    assert minimize.direction_label == '小さいほど有利'
    assert minimize.sampled_adverse_value == 2.0
    assert maximize.direction_label == '大きいほど有利'
    assert maximize.sampled_adverse_value == 0.6


def test_sampled_worst_label_is_explicitly_finite_sample_semantics() -> None:
    metric = _presentation().objectives[0]
    assert metric.sampled_adverse_label == '評価サンプル内の不利側最大値'


def test_completeness_counts_feasible_infeasible_failed_and_unsupported() -> None:
    samples = (
        _sample(0),
        _sample(1, feasible=False, failure_reason='hard_constraint_violation'),
        _sample(2, failure_reason='prediction failed'),
        _sample(3, failure_reason='unsupported backend capability'),
    )
    view = _presentation(samples=samples)
    completeness = view.completeness

    assert completeness.sample_count == 4
    assert completeness.feasible_count == 1
    assert completeness.infeasible_count == 1
    assert completeness.failed_prediction_count == 1
    assert completeness.unsupported_count == 1
    assert completeness.state == 'preliminary'


def test_stale_scene_search_or_objective_authority_is_visible_and_blocks_comparison() -> None:
    stale = _presentation(
        authorities=_current_authorities(revision_revision_id='rev-new')
    )
    current_b = _presentation(
        spec=_spec(
            candidate_id='candidate-b',
            robustness_spec_id='rob-b',
            robustness_spec_sha256='6' * 64,
        ),
        evaluation=_evaluation(
            candidate_id='candidate-b',
            robustness_spec_id='rob-b',
            robustness_spec_sha256='6' * 64,
        ),
        samples=(
            _sample(0, candidate_id='candidate-b', robustness_spec_id='rob-b', robustness_spec_sha256='6' * 64),
            _sample(1, candidate_id='candidate-b', robustness_spec_id='rob-b', robustness_spec_sha256='6' * 64),
            _sample(2, candidate_id='candidate-b', robustness_spec_id='rob-b', robustness_spec_sha256='6' * 64),
            _sample(3, candidate_id='candidate-b', robustness_spec_id='rob-b', robustness_spec_sha256='6' * 64),
            _sample(4, candidate_id='candidate-b', robustness_spec_id='rob-b', robustness_spec_sha256='6' * 64),
        ),
    )

    assert stale.current is False
    assert any('再評価' in reason for reason in stale.stale_reasons)
    eligibility = robustness_comparison_eligibility((stale, current_b))
    assert eligibility.eligible is False
    assert any('現在の保存条件' in reason for reason in eligibility.reasons)


def test_comparison_rejects_model_fidelity_or_objective_mismatch_without_score() -> None:
    first = _presentation()
    second = _presentation(
        spec=_spec(
            candidate_id='candidate-b',
            robustness_spec_id='rob-b',
            robustness_spec_sha256='6' * 64,
            fidelity='preview',
        ),
        evaluation=_evaluation(
            candidate_id='candidate-b',
            robustness_spec_id='rob-b',
            robustness_spec_sha256='6' * 64,
        ),
        samples=tuple(
            _sample(
                index,
                candidate_id='candidate-b',
                robustness_spec_id='rob-b',
                robustness_spec_sha256='6' * 64,
                fidelity='preview',
            )
            for index in range(5)
        ),
    )

    eligibility = robustness_comparison_eligibility((first, second))
    assert eligibility.eligible is False
    assert any('計算精度' in reason for reason in eligibility.reasons)
    assert not hasattr(first, 'score')


def test_constraint_authority_change_is_explicitly_stale() -> None:
    revision, search, objective = _current_authorities()
    view = build_robustness_candidate_presentation(
        spec=_spec(),
        evaluations=(_evaluation(),),
        samples=tuple(_sample(index) for index in range(5)),
        current_revision=revision,
        current_search_spec=search,
        nominal_objective=objective,
        current_constraint_workspace_hash='8' * 64,
    )

    assert view.current is False
    assert '制約条件が変更されたため再評価が必要です' in view.stale_reasons


def test_standard_objective_label_is_japanese_first() -> None:
    metric = _presentation().objectives[0]
    assert metric.objective_label == '応答形状 RMS'


def test_presenter_exposes_axis_sensitivity_and_finite_sample_values_for_ui() -> None:
    metric = _presentation().objectives[0]

    assert metric.axis_sensitivities[0].axis_id == 'speaker-x'
    assert metric.axis_sensitivities[0].label == 'スピーカー X位置'
    assert metric.axis_sensitivities[0].magnitude_per_unit == 3.0
    assert metric.sampled_values == (1.0, 1.1, 1.2, 1.3)


def test_presenter_preserves_explicit_probability_weights_for_conditional_distribution() -> None:
    samples = (
        _sample(0, probability_weight=0.25),
        _sample(1, probability_weight=0.75),
    )
    metric = _presentation(
        evaluation=_evaluation(
            percentile_semantics='explicit_probability_model',
            probability_semantics='explicit_discrete_weights',
            sampled_envelope=SimpleNamespace(
                percentile_values={'p05': 1.0, 'p50': 1.1, 'p95': 1.1}
            ),
            mean_value=1.075,
            constraint_violation_probability=0.0,
        ),
        samples=samples,
    ).objectives[0]

    assert metric.sampled_values == (1.0, 1.1)
    assert metric.sampled_weights == (0.25, 0.75)
