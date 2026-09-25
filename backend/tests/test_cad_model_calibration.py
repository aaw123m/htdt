from __future__ import annotations

from hashlib import sha256
import math

import pytest

from htdt.cad_equipment import FrequencyDomain
from htdt.cad_model_calibration import (
    CalibrationEvidenceRef,
    CalibrationObjectiveSpec,
    CalibrationOptimizerSpec,
    CalibrationParameterDefinition,
    evaluate_holdout_discipline,
    freeze_calibrated_model,
    run_model_calibration,
    build_model_calibration_spec,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _ref(label: str, version: str = '1') -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'campaign:{label}',
        authority_version=version,
        semantic_hash_sha256=_hash(label),
    )


def _fitted_param(name: str, low: float, high: float) -> CalibrationParameterDefinition:
    return CalibrationParameterDefinition(
        parameter_id=name,
        target_kind='boundary_surface_material',
        target_id='surface-1',
        quantity='absorption',
        unit='1',
        model_family='allpass_alpha',
        role='fitted',
        lower_bound=low,
        upper_bound=high,
    )


def _spec(**overrides):
    kwargs = dict(
        baseline_snapshot_id='snapshot-1',
        baseline_snapshot_sha256=_hash('baseline'),
        solver_id='solver-1',
        solver_version='1',
        calibration_evidence=(
            CalibrationEvidenceRef(
                evidence_kind='campaign',
                evidence_id='campaign:dev',
                evidence_sha256=_hash('dev-campaign'),
            ),
        ),
        parameters=(
            _fitted_param('alpha', 0.0, 1.0),
            CalibrationParameterDefinition(
                parameter_id='temperature',
                target_kind='environment',
                target_id='env-1',
                quantity='temperature_c',
                unit='degC',
                model_family='air',
                role='fixed',
                fixed_value=20.0,
            ),
        ),
        objective=CalibrationObjectiveSpec(
            observable='transfer_magnitude_db',
            frequency_domain=FrequencyDomain(
                minimum_hz=20.0, maximum_hz=200.0
            ),
        ),
        optimizer=CalibrationOptimizerSpec(max_evaluations=64),
        holdout_campaign_ref=_ref('holdout'),
    )
    kwargs.update(overrides)
    return build_model_calibration_spec(**kwargs)


def test_spec_requires_fitted_params_and_holdout_separation() -> None:
    spec = _spec()
    assert spec.spec_id.startswith('model-calibration-spec:')
    with pytest.raises(ValueError, match='at least one'):
        _spec(
            parameters=(
                CalibrationParameterDefinition(
                    parameter_id='alpha',
                    target_kind='boundary_surface_material',
                    target_id='surface-1',
                    quantity='absorption',
                    unit='1',
                    model_family='allpass_alpha',
                    role='fixed',
                    fixed_value=0.5,
                ),
            )
        )
    with pytest.raises(ValueError, match='distinct'):
        _spec(
            calibration_evidence=(
                CalibrationEvidenceRef(
                    evidence_kind='campaign',
                    evidence_id='campaign:holdout',
                    evidence_sha256=_hash('holdout'),
                ),
            ),
            holdout_campaign_ref=_ref('holdout'),
        )


def test_parameter_roles_are_typed_and_bounded() -> None:
    with pytest.raises(ValueError, match='bounds'):
        CalibrationParameterDefinition(
            parameter_id='alpha',
            target_kind='boundary_surface_material',
            target_id='surface-1',
            quantity='absorption',
            unit='1',
            model_family='allpass_alpha',
            role='fitted',
        )
    with pytest.raises(ValueError, match='explicit value'):
        CalibrationParameterDefinition(
            parameter_id='alpha',
            target_kind='boundary_surface_material',
            target_id='surface-1',
            quantity='absorption',
            unit='1',
            model_family='allpass_alpha',
            role='fixed',
        )


def test_grid_search_finds_minimum_and_issues_new_model_identity() -> None:
    spec = _spec(parameters=(
        _fitted_param('alpha', 0.0, 1.0),
    ))

    class QuadraticEvaluator:
        def evaluate(self, values):
            # residual minimized at alpha = 0.75
            return ((values['alpha'] - 0.75) * 10.0, 1.0)

    result = run_model_calibration(spec, QuadraticEvaluator())
    assert result.calibrated_model_id.startswith('calibrated-model:')
    # 64-point grid over [0, 1]: step 1/63, closest to 0.75 is 47/63
    fitted = dict(result.fitted_values)
    assert fitted['alpha'] == pytest.approx(47.0 / 63.0)
    assert result.evaluations_used > 0
    assert result.identifiability_verdict in (
        'identifiable_within_experiment',
        'weakly_identifiable',
        'contains_non_identifiable_group',
    )


def test_correlated_parameters_marked_non_identifiable() -> None:
    spec = _spec(
        parameters=(
            _fitted_param('a', 0.0, 1.0),
            _fitted_param('b', 0.0, 1.0),
        ),
        optimizer=CalibrationOptimizerSpec(max_evaluations=64),
    )

    class CorrelatedEvaluator:
        # residual depends only on a + b: perfectly correlated directions
        def evaluate(self, values):
            return (values['a'] + values['b'] - 1.0, 0.0, 0.0)

    result = run_model_calibration(spec, CorrelatedEvaluator())
    assert result.identifiability_verdict in (
        'contains_non_identifiable_group',
        'weakly_identifiable',
    )
    if result.correlation_groups:
        group_ids = {
            pid for group in result.correlation_groups for pid in group.parameter_ids
        }
        assert {'a', 'b'} <= group_ids
        for item in result.sensitivity:
            if item.parameter_id in group_ids:
                assert item.identifiability == 'correlated_non_identifiable'


def test_weakly_identifiable_when_residual_flat() -> None:
    spec = _spec()

    class FlatEvaluator:
        def evaluate(self, values):
            return (0.0, 0.0, 0.0)

    result = run_model_calibration(spec, FlatEvaluator())
    assert result.identifiability_verdict in (
        'weakly_identifiable',
        'contains_non_identifiable_group',
    )


def test_freeze_and_holdout_discipline() -> None:
    spec = _spec()

    class QuadraticEvaluator:
        def evaluate(self, values):
            return ((values['alpha'] - 0.5),)

    result = run_model_calibration(spec, QuadraticEvaluator())
    freeze = freeze_calibrated_model(
        result, spec, normalization_policy_sha256=_hash('norm')
    )
    assert freeze.freeze_id.startswith('calibrated-model-freeze:')
    assert freeze.calibrated_model_id == result.calibrated_model_id

    record = evaluate_holdout_discipline(
        freeze,
        spec,
        consumed_campaign_ref=_ref('holdout'),
        previously_consumed_campaign_ids=(),
    )
    assert record.verdict == 'independent_holdout'

    reused = evaluate_holdout_discipline(
        freeze,
        spec,
        consumed_campaign_ref=_ref('holdout'),
        previously_consumed_campaign_ids=('campaign:holdout',),
    )
    assert reused.verdict == 'holdout_reused_for_development'
    assert reused.reasons

    wrong = evaluate_holdout_discipline(
        freeze,
        spec,
        consumed_campaign_ref=_ref('dev'),
        previously_consumed_campaign_ids=(),
    )
    assert wrong.verdict == 'holdout_reused_for_development'


def test_result_identity_changes_with_fitted_values() -> None:
    spec = _spec()

    class ShiftEvaluator:
        def evaluate(self, values):
            return ((values['alpha'] - 0.9),)

    first = run_model_calibration(spec, ShiftEvaluator())
    second = run_model_calibration(spec, ShiftEvaluator())
    assert first.calibrated_model_id == second.calibrated_model_id
    assert first.result_id == second.result_id


def _transformed_param(
    name: str, transform: str, low: float, high: float
) -> CalibrationParameterDefinition:
    return CalibrationParameterDefinition(
        parameter_id=name,
        target_kind='boundary_surface_material',
        target_id='surface-1',
        quantity='absorption',
        unit='1',
        model_family='allpass_alpha',
        transform=transform,
        role='fitted',
        lower_bound=low,
        upper_bound=high,
    )


def test_transform_grids_sample_transform_coordinates() -> None:
    from htdt.cad_model_calibration import _grid_points

    identity = _transformed_param('p', 'identity', 0.001, 1.0)
    assert _grid_points(identity, 4) == pytest.approx(
        (0.001, 0.334, 0.667, 1.0)
    )

    logged = _transformed_param('p', 'log', 0.001, 1.0)
    assert _grid_points(logged, 4) == pytest.approx(
        (0.001, 0.01, 0.1, 1.0)
    )

    logit = _transformed_param('p', 'logit01', 0.1, 0.9)
    points = _grid_points(logit, 3)
    assert points == pytest.approx((0.1, 0.5, 0.9))


def test_transform_domain_bounds_rejected_at_spec_validation() -> None:
    with pytest.raises(ValueError, match='positive'):
        _transformed_param('p', 'log', 0.0, 1.0)
    with pytest.raises(ValueError, match='positive'):
        _transformed_param('p', 'log', -0.5, 2.0)
    with pytest.raises(ValueError, match=r'\(0, 1\)'):
        _transformed_param('p', 'logit01', 0.0, 0.5)
    with pytest.raises(ValueError, match=r'\(0, 1\)'):
        _transformed_param('p', 'logit01', 0.2, 1.0)
    # fixed parameters carry no bounds and keep accepting any transform
    CalibrationParameterDefinition(
        parameter_id='p',
        target_kind='environment',
        target_id='env-1',
        quantity='temperature_c',
        unit='degC',
        model_family='air',
        transform='log',
        role='fixed',
        fixed_value=20.0,
    )


def test_log_transform_finds_minimum_and_stays_physical() -> None:
    spec = _spec(
        parameters=(_transformed_param('alpha', 'log', 0.001, 1.0),),
        optimizer=CalibrationOptimizerSpec(max_evaluations=64),
    )
    # 8-point log grid over [1e-3, 1]: 10^(-3 + 3k/7) for k = 0..7.
    # Exact grid point k=2 -> 10^(-2.142857...) ~ 0.007197.
    target = 10.0 ** (-3.0 + 6.0 / 7.0)

    class TargetEvaluator:
        def evaluate(self, values):
            return (math.log(values['alpha'] / target),)

    result = run_model_calibration(spec, TargetEvaluator())
    fitted = dict(result.fitted_values)
    assert fitted['alpha'] == pytest.approx(target)
    assert result.sensitivity_parameterization == 'physical_parameter'


def test_log_grid_replay_is_deterministic() -> None:
    spec = _spec(
        parameters=(_transformed_param('alpha', 'log', 0.001, 1.0),),
        optimizer=CalibrationOptimizerSpec(max_evaluations=64),
    )

    class AnyEvaluator:
        def evaluate(self, values):
            return (values['alpha'],)

    first = run_model_calibration(spec, AnyEvaluator())
    second = run_model_calibration(spec, AnyEvaluator())
    assert first.fitted_values == second.fitted_values
    assert first.result_id == second.result_id
