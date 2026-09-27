import math
import random

import pytest

from htdt.comparison import (
    ComparisonError,
    FrequencyResponse,
    _grid,
    _interpolate,
    _interpolate_many,
    compare_frequency_responses,
    replay_comparison_result,
)


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


def test_interpolate_many_matches_scalar_bitwise() -> None:
    # Persisted comparisons replay for exact equality, so the vectorized path
    # must reproduce the scalar loop bit-for-bit, including exact-hit early
    # returns and edge grid points.
    rng = random.Random(20260927)
    for _ in range(200):
        count = rng.randint(1, 60)
        frequencies = []
        value = math.ldexp(1.0, rng.randint(-5, 4))
        for _ in range(count):
            frequencies.append(value)
            value *= rng.uniform(1.001, 1.4)
        response = FrequencyResponse(
            tuple(frequencies),
            tuple(rng.uniform(-80.0, 40.0) for _ in frequencies),
        )
        grid = _grid(response.frequency_hz[0], response.frequency_hz[-1])
        assert _interpolate_many(response, grid) == tuple(
            _interpolate(response, f) for f in grid
        )


def test_interpolate_many_degenerate_frequency_axis() -> None:
    # A non-strictly-increasing axis keeps the scalar loop's semantics
    # (duplicates change where bisect lands), so each grid point is
    # computed by the scalar fallback.
    response = FrequencyResponse((10.0, 20.0, 20.0, 40.0), (0.0, 5.0, 7.0, 9.0))
    for f in (10.0, 15.0, 20.0, 35.0, 40.0):
        assert _interpolate_many(response, (f,)) == (_interpolate(response, f),)


def test_interpolate_many_extrapolation_raises() -> None:
    response = FrequencyResponse((10.0, 20.0, 40.0), (0.0, 5.0, 9.0))
    with pytest.raises(ComparisonError):
        _interpolate_many(response, (30.0, 50.0))
    assert _interpolate_many(response, ()) == ()


def test_vectorized_compare_replays_identically() -> None:
    rng = random.Random(7)
    a = FrequencyResponse(
        tuple(math.exp(i * 0.13) * 20.0 for i in range(40)),
        tuple(rng.uniform(-90.0, 20.0) for _ in range(40)),
    )
    b = FrequencyResponse(
        tuple(math.exp(i * 0.11) * 21.0 for i in range(45)),
        tuple(rng.uniform(-90.0, 20.0) for _ in range(45)),
    )
    result = compare_frequency_responses(
        a, b, 25.0, 500.0,
        reference_band_hz=(60.0, 120.0),
        excluded_bands=((80.0, 95.0), (300.0, 350.0)),
    )
    assert replay_comparison_result(result, a, b) == result
