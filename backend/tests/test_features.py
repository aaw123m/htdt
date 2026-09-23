import math

import pytest

from htdt.comparison import FrequencyResponse
from htdt.features import detect_frequency_features, match_geometry_candidates


def _synthetic_response() -> FrequencyResponse:
    frequencies = tuple(2 ** (k / 96) for k in range(math.ceil(96 * math.log2(20)), math.floor(96 * math.log2(200)) + 1))
    levels = [70.0 for _ in frequencies]
    dip_index = min(range(len(frequencies)), key=lambda i: abs(math.log2(frequencies[i] / 80.0)))
    peak_index = min(range(len(frequencies)), key=lambda i: abs(math.log2(frequencies[i] / 125.0)))
    levels[dip_index] = 61.0
    levels[peak_index] = 77.0
    return FrequencyResponse(frequency_hz=frequencies, level_db=tuple(levels))


def test_detects_narrow_peak_and_dip_on_96_ppo_grid() -> None:
    result = detect_frequency_features(_synthetic_response(), low_hz=30, high_hz=180, prominence_db=3.0)
    assert result['classification'] == 'measured_feature_detection'
    assert result['parameters']['ppo'] == 96
    features = result['features']
    dip = next(item for item in features if item['kind'] == 'dip')
    peak = next(item for item in features if item['kind'] == 'peak')
    assert dip['frequency_hz'] == pytest.approx(80.0, rel=0.01)
    assert peak['frequency_hz'] == pytest.approx(125.0, rel=0.01)
    assert dip['prominence_db'] >= 3.0
    assert peak['prominence_db'] >= 3.0


def test_geometry_matches_are_candidates_not_diagnoses() -> None:
    features = [
        {'kind': 'dip', 'frequency_hz': 80.0},
        {'kind': 'peak', 'frequency_hz': 125.0},
    ]
    geometry = {
        'room_modes': [
            {'n_x': 1, 'n_y': 0, 'n_z': 0, 'frequency_hz': 80.5, 'mode_class': 'axial'},
            {'n_x': 0, 'n_y': 1, 'n_z': 0, 'frequency_hz': 124.5, 'mode_class': 'axial'},
        ],
        'first_order_reflections': [
            {'speaker_id': 'FL', 'surface': 'front_y0', 'first_destructive_hz': 79.5},
            {'speaker_id': 'FR', 'surface': 'right_xW', 'first_destructive_hz': 125.0},
        ],
    }
    matches = match_geometry_candidates(features, geometry, tolerance_octaves=1 / 12)
    assert any(match['feature_index'] == 0 and match['candidate_kind'] == 'room_mode' for match in matches)
    assert any(match['feature_index'] == 0 and match['candidate_kind'] == 'first_order_reflection' for match in matches)
    assert any(match['feature_index'] == 1 and match['candidate_kind'] == 'room_mode' for match in matches)
    assert not any(match['feature_index'] == 1 and match['candidate_kind'] == 'first_order_reflection' for match in matches)
