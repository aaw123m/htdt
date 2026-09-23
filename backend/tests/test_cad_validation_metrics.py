from __future__ import annotations

from htdt.cad_validation_metrics import (
    CadObjectiveValidationSample,
    build_candidate_separation_check,
    build_repeatability_check,
    build_sensitivity_check,
    build_trend_checks,
)
from htdt.comparison import FrequencyResponse


def _fr(offset: float) -> FrequencyResponse:
    return FrequencyResponse(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=(80.0 + offset, 81.0 + offset, 79.0 + offset, 80.0 + offset),
    )


def _sample(candidate_id: str, predicted: float, measured: float) -> CadObjectiveValidationSample:
    return CadObjectiveValidationSample(
        candidate_id=candidate_id,
        split='holdout',
        objective_id='response.shape_rms_db',
        unit='dB',
        predicted_evaluation_id=f'pred:{candidate_id}',
        measured_evaluation_id=f'meas:{candidate_id}',
        predicted_value=predicted,
        measured_value=measured,
    )


def test_holdout_trend_is_per_objective_pairwise_and_detects_discordance():
    passing = build_trend_checks(
        (_sample('a', 1.0, 1.2), _sample('b', 2.0, 2.1), _sample('c', 3.0, 2.9)),
        min_agreement_ratio=0.75,
    )[0]
    assert passing.comparable_pairs == 3
    assert passing.concordant_pairs == 3
    assert passing.gate == 'pass'

    failing = build_trend_checks(
        (_sample('a', 1.0, 3.0), _sample('b', 2.0, 2.0), _sample('c', 3.0, 1.0)),
        min_agreement_ratio=0.75,
    )[0]
    assert failing.discordant_pairs == 3
    assert failing.gate == 'fail'


def test_holdout_trend_ties_do_not_fake_agreement():
    check = build_trend_checks(
        (_sample('a', 1.0, 1.0), _sample('b', 1.02, 2.0)),
        tie_tolerance_by_objective={'response.shape_rms_db': 0.05},
        min_comparable_pairs=1,
    )[0]
    assert check.comparable_pairs == 0
    assert check.ambiguous_pairs == 1
    assert check.agreement_ratio is None
    assert check.gate == 'insufficient'


def test_sensitivity_gate_uses_observed_change_and_prediction_error_per_metre():
    check = build_sensitivity_check(
        objective_id='response.shape_rms_db',
        unit='dB',
        candidate_a_id='a',
        candidate_b_id='b',
        placement_delta_m=0.02,
        predicted_a=1.0,
        predicted_b=1.1,
        measured_a=1.0,
        measured_b=1.8,
        max_observed_sensitivity_per_m=20.0,
        max_model_error_per_m=10.0,
    )
    assert check.observed_sensitivity_per_m > 20.0
    assert check.model_error_per_m > 10.0
    assert check.gate == 'fail'


def test_repeatability_floor_blocks_candidate_difference_at_noise_scale():
    repeatability = build_repeatability_check(
        scene_revision_id='revision-a',
        measurements=(
            ('repeat-1', _fr(0.0)),
            ('repeat-2', _fr(0.5)),
            ('repeat-3', _fr(-0.5)),
        ),
        low_hz=20.0,
        high_hz=160.0,
    )
    assert repeatability.rms_floor_db > 0
    assert len(repeatability.pairs) == 3

    indistinguishable = build_candidate_separation_check(
        candidate_a_id='a',
        candidate_b_id='b',
        measurement_a_id='a-1',
        measurement_b_id='b-1',
        response_a=_fr(0.0),
        response_b=_fr(0.4),
        low_hz=20.0,
        high_hz=160.0,
        repeatability_floor_db=repeatability.rms_floor_db,
        min_repeatability_multiple=1.0,
    )
    assert indistinguishable.gate == 'fail'

    distinguishable = build_candidate_separation_check(
        candidate_a_id='a',
        candidate_b_id='c',
        measurement_a_id='a-1',
        measurement_b_id='c-1',
        response_a=_fr(0.0),
        response_b=_fr(3.0),
        low_hz=20.0,
        high_hz=160.0,
        repeatability_floor_db=repeatability.rms_floor_db,
        min_repeatability_multiple=1.0,
    )
    assert distinguishable.gate == 'pass'
