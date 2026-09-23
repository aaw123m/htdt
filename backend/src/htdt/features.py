from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
import math
from typing import Any

from .comparison import FrequencyResponse, PPO


FEATURE_ALGORITHM_VERSION = 'fr-feature-candidates-1'


class FeatureDetectionError(ValueError):
    pass


@dataclass(frozen=True)
class FrequencyFeature:
    kind: str
    frequency_hz: float
    level_db: float
    baseline_db: float
    signed_deviation_db: float
    prominence_db: float


@dataclass(frozen=True)
class CandidateMatch:
    feature_index: int
    feature_kind: str
    feature_frequency_hz: float
    candidate_kind: str
    candidate_frequency_hz: float
    distance_octaves: float
    details: dict[str, Any]


def _grid(low_hz: float, high_hz: float) -> tuple[float, ...]:
    if low_hz <= 0 or high_hz <= low_hz:
        raise FeatureDetectionError('Invalid feature detection band')
    k_min = math.ceil(PPO * math.log2(low_hz))
    k_max = math.floor(PPO * math.log2(high_hz))
    if k_max < k_min:
        return ()
    return tuple(2 ** (k / PPO) for k in range(k_min, k_max + 1))


def _interpolate(response: FrequencyResponse, frequency_hz: float) -> float:
    frequencies = response.frequency_hz
    levels = response.level_db
    if len(frequencies) != len(levels) or len(frequencies) < 2:
        raise FeatureDetectionError('Frequency response requires at least two aligned points')
    if frequency_hz < frequencies[0] or frequency_hz > frequencies[-1]:
        raise FeatureDetectionError('Interpolation would require extrapolation')
    index = bisect_right(frequencies, frequency_hz)
    if index == 0:
        return levels[0]
    if index == len(frequencies):
        return levels[-1]
    left_f, right_f = frequencies[index - 1], frequencies[index]
    if frequency_hz == left_f:
        return levels[index - 1]
    ratio = (math.log2(frequency_hz) - math.log2(left_f)) / (math.log2(right_f) - math.log2(left_f))
    return levels[index - 1] + ratio * (levels[index] - levels[index - 1])


def _moving_baseline(values: tuple[float, ...], radius_points: int) -> tuple[float, ...]:
    if radius_points < 1:
        raise FeatureDetectionError('Baseline window is too small')
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
    baseline: list[float] = []
    for index in range(len(values)):
        start = max(0, index - radius_points)
        stop = min(len(values), index + radius_points + 1)
        baseline.append((prefix[stop] - prefix[start]) / (stop - start))
    return tuple(baseline)


def detect_frequency_features(
    response: FrequencyResponse,
    *,
    low_hz: float,
    high_hz: float,
    prominence_db: float = 3.0,
    baseline_window_octaves: float = 1 / 3,
    min_spacing_octaves: float = 1 / 12,
) -> dict[str, Any]:
    if prominence_db <= 0:
        raise FeatureDetectionError('prominence_db must be positive')
    if baseline_window_octaves <= 0 or min_spacing_octaves < 0:
        raise FeatureDetectionError('Feature window parameters must be positive')
    if any(not math.isfinite(value) or value <= 0 for value in response.frequency_hz):
        raise FeatureDetectionError('Frequency response has invalid frequency values')
    if any(not math.isfinite(value) for value in response.level_db):
        raise FeatureDetectionError('Frequency response has invalid level values')
    if any(right <= left for left, right in zip(response.frequency_hz, response.frequency_hz[1:])):
        raise FeatureDetectionError('Frequency response frequencies must be strictly increasing')

    overlap_low = max(low_hz, response.frequency_hz[0])
    overlap_high = min(high_hz, response.frequency_hz[-1])
    if overlap_high <= overlap_low:
        raise FeatureDetectionError('Dataset does not overlap the requested feature band')
    grid = _grid(overlap_low, overlap_high)
    if len(grid) < 3:
        raise FeatureDetectionError('Feature band has too few 96 PPO points')
    levels = tuple(_interpolate(response, frequency) for frequency in grid)

    radius_points = max(1, round((baseline_window_octaves * PPO) / 2.0))
    baseline = _moving_baseline(levels, radius_points)
    residual = tuple(level - base for level, base in zip(levels, baseline, strict=True))

    raw_candidates: list[FrequencyFeature] = []
    for index in range(1, len(grid) - 1):
        value = residual[index]
        if value >= prominence_db and value >= residual[index - 1] and value > residual[index + 1]:
            raw_candidates.append(FrequencyFeature('peak', grid[index], levels[index], baseline[index], value, abs(value)))
        elif value <= -prominence_db and value <= residual[index - 1] and value < residual[index + 1]:
            raw_candidates.append(FrequencyFeature('dip', grid[index], levels[index], baseline[index], value, abs(value)))

    selected: list[FrequencyFeature] = []
    for candidate in sorted(raw_candidates, key=lambda item: (-item.prominence_db, item.frequency_hz)):
        if any(abs(math.log2(candidate.frequency_hz / existing.frequency_hz)) < min_spacing_octaves for existing in selected):
            continue
        selected.append(candidate)
    selected.sort(key=lambda item: item.frequency_hz)

    return {
        'classification': 'measured_feature_detection',
        'algorithm_version': FEATURE_ALGORITHM_VERSION,
        'parameters': {
            'ppo': PPO,
            'requested_low_hz': low_hz,
            'requested_high_hz': high_hz,
            'actual_low_hz': overlap_low,
            'actual_high_hz': overlap_high,
            'prominence_db': prominence_db,
            'baseline_window_octaves': baseline_window_octaves,
            'baseline_radius_points': radius_points,
            'min_spacing_octaves': min_spacing_octaves,
        },
        'features': [asdict(feature) for feature in selected],
        'grid_points': len(grid),
    }


def match_geometry_candidates(
    features: list[dict[str, Any]],
    geometry: dict[str, Any],
    *,
    tolerance_octaves: float = 1 / 12,
    max_matches_per_feature: int = 8,
) -> list[dict[str, Any]]:
    if tolerance_octaves <= 0:
        raise FeatureDetectionError('tolerance_octaves must be positive')
    if max_matches_per_feature < 1:
        raise FeatureDetectionError('max_matches_per_feature must be positive')

    candidate_rows: list[tuple[str, float, dict[str, Any], set[str]]] = []
    for mode in geometry.get('room_modes', []):
        frequency = mode.get('frequency_hz')
        if isinstance(frequency, (int, float)) and frequency > 0:
            candidate_rows.append(('room_mode', float(frequency), dict(mode), {'peak', 'dip'}))
    for reflection in geometry.get('first_order_reflections', []):
        frequency = reflection.get('first_destructive_hz')
        if isinstance(frequency, (int, float)) and frequency > 0:
            candidate_rows.append(('first_order_reflection', float(frequency), dict(reflection), {'dip'}))

    matches: list[CandidateMatch] = []
    for feature_index, feature in enumerate(features):
        feature_frequency = float(feature['frequency_hz'])
        feature_kind = str(feature['kind'])
        local: list[CandidateMatch] = []
        for candidate_kind, candidate_frequency, details, allowed_kinds in candidate_rows:
            if feature_kind not in allowed_kinds:
                continue
            distance = abs(math.log2(feature_frequency / candidate_frequency))
            if distance <= tolerance_octaves:
                local.append(CandidateMatch(
                    feature_index=feature_index,
                    feature_kind=feature_kind,
                    feature_frequency_hz=feature_frequency,
                    candidate_kind=candidate_kind,
                    candidate_frequency_hz=candidate_frequency,
                    distance_octaves=distance,
                    details=details,
                ))
        local.sort(key=lambda item: (item.distance_octaves, item.candidate_kind, item.candidate_frequency_hz))
        matches.extend(local[:max_matches_per_feature])
    return [asdict(match) for match in matches]
