from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
import math
from typing import Any
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash


ALGORITHM_VERSION = 'fr-compare-1'
PPO = 96


class ComparisonError(ValueError):
    pass






COMPARISON_ALGORITHM_IDENTITY = {
    'algorithm_version': ALGORITHM_VERSION,
    'points_per_octave': PPO,
    'grid': 'log2_spaced_octave_aligned_grid_over_overlap_band',
    'interpolation': 'linear_in_log2_frequency',
    'extrapolation': 'forbidden',
    'difference': 'a_minus_b_pointwise',
    'excluded_bands': 'inclusive_frequency_ranges_removed_from_valid_grid',
    'reference_band': 'level_offset_is_mean_difference_over_valid_reference_grid',
    'metrics': [
        'mean_difference_db',
        'rms_difference_db',
        'level_offset_db',
        'shape_rms_db',
    ],
    'insufficient_valid_points': 'difference_metrics_are_none_below_two_points',
}
COMPARISON_ALGORITHM_SHA256 = _hash(COMPARISON_ALGORITHM_IDENTITY)


@dataclass(frozen=True)
class FrequencyResponse:
    frequency_hz: tuple[float, ...]
    level_db: tuple[float, ...]


@dataclass(frozen=True)
class ComparisonResult:
    """Canonical A/B output plus the exact spec that produced it.

    ``requested_band_hz``, ``reference_band_hz`` and ``excluded_bands`` record
    the full comparison spec so a persistence authority can replay the pinned
    algorithm and require exact equality instead of trusting opaque arrays.
    """

    requested_band_hz: tuple[float, float]
    actual_band_hz: tuple[float, float]
    grid_hz: tuple[float, ...]
    a_db: tuple[float, ...]
    b_db: tuple[float, ...]
    difference_db: tuple[float, ...]
    mean_difference_db: float | None
    rms_difference_db: float | None
    level_offset_db: float | None
    shape_rms_db: float | None
    valid_points: int
    total_grid_points: int
    algorithm_version: str = ALGORITHM_VERSION
    reference_band_hz: tuple[float, float] | None = None
    excluded_bands: tuple[tuple[float, float], ...] = ()


def _grid(low_hz: float, high_hz: float) -> tuple[float, ...]:
    k_min = math.ceil(PPO * math.log2(low_hz))
    k_max = math.floor(PPO * math.log2(high_hz))
    if k_max < k_min:
        return ()
    return tuple(2 ** (k / PPO) for k in range(k_min, k_max + 1))


def _interpolate(response: FrequencyResponse, frequency_hz: float) -> float:
    frequencies = response.frequency_hz
    if frequency_hz < frequencies[0] or frequency_hz > frequencies[-1]:
        raise ComparisonError('Interpolation would require extrapolation')
    index = bisect_right(frequencies, frequency_hz)
    if index == 0:
        return response.level_db[0]
    if index == len(frequencies):
        return response.level_db[-1]
    left_f = frequencies[index - 1]
    right_f = frequencies[index]
    if frequency_hz == left_f:
        return response.level_db[index - 1]
    left_x = math.log2(left_f)
    right_x = math.log2(right_f)
    x = math.log2(frequency_hz)
    ratio = (x - left_x) / (right_x - left_x)
    return response.level_db[index - 1] + ratio * (response.level_db[index] - response.level_db[index - 1])


def _excluded(frequency_hz: float, excluded_bands: tuple[tuple[float, float], ...]) -> bool:
    return any(low <= frequency_hz <= high for low, high in excluded_bands)


def _mean(values: tuple[float, ...]) -> float:
    return sum(values) / len(values)


def compare_frequency_responses(
    a: FrequencyResponse,
    b: FrequencyResponse,
    low_hz: float,
    high_hz: float,
    reference_band_hz: tuple[float, float] | None = None,
    excluded_bands: tuple[tuple[float, float], ...] = (),
) -> ComparisonResult:
    if high_hz <= low_hz:
        raise ComparisonError('Invalid comparison band')
    overlap_low = max(low_hz, a.frequency_hz[0], b.frequency_hz[0])
    overlap_high = min(high_hz, a.frequency_hz[-1], b.frequency_hz[-1])
    if overlap_high <= overlap_low:
        raise ComparisonError('The two datasets do not overlap in the requested band')

    complete_grid = _grid(overlap_low, overlap_high)
    valid_grid = tuple(f for f in complete_grid if not _excluded(f, excluded_bands))
    a_values = tuple(_interpolate(a, f) for f in valid_grid)
    b_values = tuple(_interpolate(b, f) for f in valid_grid)
    differences = tuple(a_value - b_value for a_value, b_value in zip(a_values, b_values, strict=True))

    mean_difference = None
    rms_difference = None
    if len(differences) >= 2:
        mean_difference = _mean(differences)
        rms_difference = math.sqrt(_mean(tuple(value * value for value in differences)))

    offset = None
    shape_rms = None
    if reference_band_hz is not None:
        ref_low = max(reference_band_hz[0], a.frequency_hz[0], b.frequency_hz[0])
        ref_high = min(reference_band_hz[1], a.frequency_hz[-1], b.frequency_hz[-1])
        if ref_high > ref_low:
            ref_grid = tuple(f for f in _grid(ref_low, ref_high) if not _excluded(f, excluded_bands))
            if len(ref_grid) >= 2:
                ref_diff = tuple(_interpolate(a, f) - _interpolate(b, f) for f in ref_grid)
                offset = _mean(ref_diff)
                if len(differences) >= 2:
                    shape_rms = math.sqrt(_mean(tuple((value - offset) ** 2 for value in differences)))

    return ComparisonResult(
        requested_band_hz=(low_hz, high_hz),
        actual_band_hz=(overlap_low, overlap_high),
        grid_hz=valid_grid,
        a_db=a_values,
        b_db=b_values,
        difference_db=differences,
        mean_difference_db=mean_difference,
        rms_difference_db=rms_difference,
        level_offset_db=offset,
        shape_rms_db=shape_rms,
        valid_points=len(valid_grid),
        total_grid_points=len(complete_grid),
        reference_band_hz=reference_band_hz,
        excluded_bands=excluded_bands,
    )


# Explicit versioned replay support: every algorithm version that can produce
# persisted comparisons keeps its pinned identity hash and builder here so
# historical results stay replayable. A result pinned to an identity that is
# not registered fails closed instead of silently trusting supplied arrays.
ComparisonResultReplay = Callable[..., ComparisonResult]

_COMPARISON_RESULT_REPLAY: dict[str, tuple[str, ComparisonResultReplay]] = {
    ALGORITHM_VERSION: (
        COMPARISON_ALGORITHM_SHA256,
        compare_frequency_responses,
    ),
}


def comparison_algorithm_sha256(algorithm_version: str) -> str:
    """Pinned identity hash for a replayable comparison algorithm version."""
    replay = _COMPARISON_RESULT_REPLAY.get(algorithm_version)
    if replay is None:
        raise ValueError(
            'comparison algorithm version is not replayable: '
            f'{algorithm_version}'
        )
    return replay[0]


def replay_comparison_result(
    result: ComparisonResult,
    a: FrequencyResponse,
    b: FrequencyResponse,
) -> ComparisonResult:
    """Rerun the pinned algorithm for ``result.algorithm_version``.

    The result carries its full comparison spec (requested band, reference
    band, exclusions) so replay recomputes the canonical output from the two
    frequency responses and can be required to equal the proposed result.
    """
    replay = _COMPARISON_RESULT_REPLAY.get(result.algorithm_version)
    if replay is None:
        raise ValueError(
            'comparison algorithm version is not replayable: '
            f'{result.algorithm_version}'
        )
    _algorithm_sha256, builder = replay
    return builder(
        a,
        b,
        result.requested_band_hz[0],
        result.requested_band_hz[1],
        reference_band_hz=result.reference_band_hz,
        excluded_bands=result.excluded_bands,
    )
