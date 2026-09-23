import math

from htdt.comparison import FrequencyResponse, compare_frequency_responses


def test_constant_three_db_difference() -> None:
    a = FrequencyResponse((20.0, 40.0, 80.0, 160.0, 320.0), (73.0, 73.0, 73.0, 73.0, 73.0))
    b = FrequencyResponse((20.0, 40.0, 80.0, 160.0, 320.0), (70.0, 70.0, 70.0, 70.0, 70.0))
    result = compare_frequency_responses(a, b, 30, 200, reference_band_hz=(40, 160))
    assert result.valid_points > 2
    assert math.isclose(result.mean_difference_db or 0, 3.0, abs_tol=1e-12)
    assert math.isclose(result.rms_difference_db or 0, 3.0, abs_tol=1e-12)
    assert math.isclose(result.level_offset_db or 0, 3.0, abs_tol=1e-12)
    assert math.isclose(result.shape_rms_db or 0, 0.0, abs_tol=1e-12)


def test_excluded_band_reduces_valid_points() -> None:
    response = FrequencyResponse((20.0, 40.0, 80.0, 160.0, 320.0), (70.0, 71.0, 72.0, 73.0, 74.0))
    full = compare_frequency_responses(response, response, 20, 320)
    excluded = compare_frequency_responses(response, response, 20, 320, excluded_bands=((70, 90),))
    assert excluded.valid_points < full.valid_points
    assert excluded.total_grid_points == full.total_grid_points
