from __future__ import annotations

from hashlib import sha256
import math

import pytest

from htdt.cad_calibration import (
    CadTargetCurve,
    CadTargetCurvePoint,
    CadTargetNormalizationCondition,
)
from htdt.cad_correction_design_policy import (
    CorrectionDesignPolicy,
    CorrectionRegularization,
    CorrectionValidationPolicy,
    MeasurementPopulationRef,
    PositionMagnitudeSample,
    SpatialRobustnessRule,
    aggregate_position_magnitudes,
    build_correction_design_policy,
)
from htdt.cad_equipment import FrequencyDomain


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _target() -> CadTargetCurve:
    return CadTargetCurve(
        points=(
            CadTargetCurvePoint(frequency_hz=20.0, level_db=0.0),
            CadTargetCurvePoint(frequency_hz=200.0, level_db=0.0),
        ),
        normalization=CadTargetNormalizationCondition(
            method='absolute_level', reference_level_db=0.0
        ),
    )


def _policy(**overrides) -> CorrectionDesignPolicy:
    kwargs = dict(
        measurement_population=MeasurementPopulationRef(
            population_id='mmt-1',
            population_version='1',
            semantic_sha256=_hash('population'),
            sample_count=3,
        ),
        frequency_domain=FrequencyDomain(
            minimum_hz=20.0, maximum_hz=200.0
        ),
        correction_bands=((20.0, 60.0), (60.0, 200.0)),
        aggregation='arithmetic_db_mean',
        smoothing='none',
        seat_ids=('seat-a', 'seat-b', 'seat-c'),
        min_positions=2,
        max_boost_db=6.0,
        max_cut_db=12.0,
        regularization=CorrectionRegularization(
            formulation='peq',
            strength_db=1.0,
            max_filters=8,
            max_q=8.0,
        ),
        spatial_robustness=SpatialRobustnessRule(
            persistence_fraction=0.66,
            error_significance_db=1.0,
            sign_consistency_required=True,
            local_null_max_boost_db=2.0,
            movement_sensitivity_threshold_db=4.0,
        ),
        validation=CorrectionValidationPolicy(
            rule='none', validation_algorithm_version='1'
        ),
        device_max_boost_db=9.0,
        algorithm_id='eqp-test',
        algorithm_version='1',
    )
    kwargs.update(overrides)
    return build_correction_design_policy(**kwargs)


def _sample(position_id: str, low: float, high: float) -> PositionMagnitudeSample:
    return PositionMagnitudeSample(
        position_id=position_id,
        frequencies_hz=(30.0, 50.0, 100.0, 150.0),
        magnitudes_db=(low, low, high, high),
    )


def test_policy_identity_and_rule_gating() -> None:
    policy = _policy()
    assert policy.policy_id.startswith('correction-design-policy:')
    assert _policy() == policy

    with pytest.raises(ValueError, match='overlap'):
        _policy(correction_bands=((20.0, 100.0), (80.0, 200.0)))
    with pytest.raises(ValueError, match='declared frequency domain'):
        _policy(correction_bands=((10.0, 100.0),))
    with pytest.raises(ValueError, match='fir-only'):
        _policy(
            regularization=CorrectionRegularization(
                formulation='peq', strength_db=1.0, max_taps=512
            )
        )
    with pytest.raises(ValueError, match='mlp_weighted'):
        _policy(aggregation='mlp_weighted')


def test_persistent_peak_gets_cut_not_boost() -> None:
    policy = _policy()
    measurements = (
        _sample('seat-a', low=6.0, high=0.5),
        _sample('seat-b', low=7.0, high=0.4),
        _sample('seat-c', low=5.5, high=0.6),
    )
    evidence = aggregate_position_magnitudes(
        measurements, _target(), policy
    )
    assert evidence.evidence_id.startswith('spatial-correction-evidence:')
    first, second = evidence.bands
    # Band 1: every seat shows a consistent +6 dB peak.
    assert first.classification == 'spatially_persistent_error'
    assert first.sign_consistent is True
    assert first.aggregated_error_db == pytest.approx(6.1666, rel=1e-3)
    # A positive persistent error is a peak — boost is forbidden outright.
    assert first.max_allowed_boost_db == 0.0
    assert first.max_allowed_cut_db == pytest.approx(12.0)
    assert first.headroom_feasible is True
    assert second.classification == 'insufficient_evidence'


def test_local_null_is_never_boosted_past_the_guard() -> None:
    policy = _policy()
    measurements = (
        _sample('seat-a', low=-9.0, high=0.3),
        _sample('seat-b', low=-8.0, high=0.2),
        _sample('seat-c', low=5.0, high=0.5),
    )
    evidence = aggregate_position_magnitudes(
        measurements, _target(), policy
    )
    first = evidence.bands[0]
    # Null at two seats but positive at the third -> not sign-consistent.
    assert first.classification in ('local_null', 'spatially_variable')
    assert first.max_allowed_boost_db <= 2.0
    assert first.recommend_geometry_review is True
    # Headroom remains provable against the capped boost.
    assert first.headroom_feasible is True


def test_spatial_holdout_excludes_positions_from_evidence() -> None:
    policy = _policy(
        validation=CorrectionValidationPolicy(
            rule='spatial_positions',
            holdout_position_ids=('seat-c',),
            validation_algorithm_version='1',
        )
    )
    measurements = (
        _sample('seat-a', low=6.0, high=0.2),
        _sample('seat-b', low=6.0, high=0.2),
        _sample('seat-c', low=-20.0, high=-20.0),
    )
    evidence = aggregate_position_magnitudes(
        measurements, _target(), policy
    )
    assert evidence.position_ids_used == ('seat-a', 'seat-b')
    assert evidence.holdout_position_ids == ('seat-c',)
    assert evidence.bands[0].classification == 'spatially_persistent_error'


def test_spatially_variable_band_hands_off_to_geometry() -> None:
    policy = _policy()
    measurements = (
        _sample('seat-a', low=8.0, high=0.2),
        _sample('seat-b', low=-8.0, high=0.2),
        _sample('seat-c', low=0.0, high=0.2),
    )
    evidence = aggregate_position_magnitudes(
        measurements, _target(), policy
    )
    band = evidence.bands[0]
    assert band.classification == 'spatially_variable'
    assert band.position_spread_db > 4.0
    assert band.recommend_geometry_review is True
    assert band.max_allowed_boost_db == pytest.approx(2.0)


def test_target_interpolation_is_log_frequency() -> None:
    # Target curves interpolate linearly in log2 frequency (the canonical
    # comparison authority's convention). Target: (20, 0) -> (160, -8);
    # at 80 Hz log2 gives -8 * 2/3 = -5.333, linear-in-Hz gives -3.429.
    target = CadTargetCurve(
        points=(
            CadTargetCurvePoint(frequency_hz=20.0, level_db=0.0),
            CadTargetCurvePoint(frequency_hz=160.0, level_db=-8.0),
        ),
        normalization=CadTargetNormalizationCondition(
            method='absolute_level', reference_level_db=0.0
        ),
    )
    policy = _policy(
        correction_bands=((20.0, 200.0),),
        seat_ids=('seat-a',),
        min_positions=1,
    )
    measurements = (
        PositionMagnitudeSample(
            position_id='seat-a',
            frequencies_hz=(80.0,),
            magnitudes_db=(0.0,),
        ),
    )
    evidence = aggregate_position_magnitudes(measurements, target, policy)
    # residual = 0 - (-8 * 2/3) = +5.333
    assert evidence.bands[0].aggregated_error_db == pytest.approx(
        16.0 / 3.0, rel=1e-6
    )


def test_fractional_octave_smoothing_is_power_mean() -> None:
    # 1/3-octave smoothing must combine levels as power (the
    # fractional-octave-power-mean-1 convention): a -20 dB dip inside a
    # window dominated by two 0 dB samples yields -1.74 dB, not the
    # -6.67 dB an arithmetic dB mean would give.
    third_oct = 1.0 / 3.0
    policy = _policy(
        correction_bands=((20.0, 200.0),),
        seat_ids=('seat-a',),
        min_positions=1,
        smoothing='fractional_octave',
        smoothing_fraction_octaves=third_oct,
    )
    measurements = (
        PositionMagnitudeSample(
            position_id='seat-a',
            frequencies_hz=(89.1, 100.0, 112.2),
            magnitudes_db=(0.0, -20.0, 0.0),
        ),
    )
    evidence = aggregate_position_magnitudes(
        measurements, _target(), policy
    )
    center = 10.0 * math.log10((1.0 + 0.01 + 1.0) / 3.0)
    edge = 10.0 * math.log10((1.0 + 0.01) / 2.0)
    expected = (edge + center + edge) / 3.0
    assert evidence.bands[0].aggregated_error_db == pytest.approx(
        expected, rel=1e-6
    )
