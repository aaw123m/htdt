from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Any, NamedTuple
from .canonical_json import canonical_sha256 as _hash


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
    return _grid_cached(k_min, k_max)


@lru_cache(maxsize=128)
def _grid_cached(k_min: int, k_max: int) -> tuple[float, ...]:
    """Grid tuple for an octave-aligned index range.

    ``2 ** (k / PPO)`` must keep CPython's pow rounding exactly (NumPy's
    power can differ by 1 ulp), so only the (k_min, k_max) range — which
    recurs for every comparison over the same band — is memoized rather
    than vectorized.
    """
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


_np: Any = None


def _numpy() -> Any:
    global _np
    if _np is None:
        import numpy

        _np = numpy
    return _np


class _PreparedResponse(NamedTuple):
    frequencies: Any  # np.ndarray
    log_frequencies: Any  # np.ndarray
    levels: Any  # np.ndarray
    strictly_increasing: bool
    response: FrequencyResponse


def _prepare_response(response: FrequencyResponse) -> '_PreparedResponse':
    np = _numpy()
    frequencies = np.asarray(response.frequency_hz)
    strictly_increasing = bool(
        len(frequencies) < 2 or np.all(np.diff(frequencies) > 0)
    )
    return _PreparedResponse(
        frequencies=frequencies,
        log_frequencies=np.array(
            [math.log2(f) for f in response.frequency_hz]
        ),
        levels=np.asarray(response.level_db),
        strictly_increasing=strictly_increasing,
        response=response,
    )


@lru_cache(maxsize=64)
def _log2_many(grid_hz: tuple[float, ...]) -> Any:
    """``math.log2`` per grid point, memoized on the grid tuple.

    Bands recur across comparisons; each entry is one small float array.
    """
    return _numpy().array([math.log2(f) for f in grid_hz])


def _interpolate_prepared(
    prepared: _PreparedResponse,
    grid_hz: tuple[float, ...],
    log_grid: Any,
) -> tuple[float, ...]:
    """Vectorized form of per-point ``_interpolate``, bit-identical output.

    ``COMPARISON_ALGORITHM_SHA256`` pins ``linear_in_log2_frequency`` and
    persisted results replay for exact equality, so every operation here
    reproduces the scalar loop's IEEE ordering: ``searchsorted`` equals
    ``bisect_right`` on a strictly increasing axis, the log2 inputs still
    come from ``math.log2`` (``numpy.log2`` may differ by 1 ulp across
    libm builds), and each element computes
    ``l_left + ratio * (l_right - l_left)`` in the same order. Endpoint
    and exact-hit early returns are reinstated as masks. A frequency axis
    that is not strictly increasing falls back to the scalar loop so
    degenerate inputs evaluate identically.
    """
    np = _numpy()
    frequencies = prepared.frequencies
    levels = prepared.levels
    if not grid_hz:
        return ()
    grid = np.asarray(grid_hz)
    if not prepared.strictly_increasing:
        return tuple(_interpolate(prepared.response, f) for f in grid_hz)
    if bool(np.any(grid < frequencies[0])) or bool(np.any(grid > frequencies[-1])):
        raise ComparisonError('Interpolation would require extrapolation')
    index = np.searchsorted(frequencies, grid, side='right')
    # Clamp only for array indexing; the true ``index`` decides which
    # early-return mask applies, matching the scalar branch order.
    clipped = np.clip(index, 1, len(frequencies) - 1)
    left = clipped - 1
    log_frequencies = prepared.log_frequencies
    with np.errstate(divide='ignore', invalid='ignore'):
        ratio = (log_grid - log_frequencies[left]) / (
            log_frequencies[clipped] - log_frequencies[left]
        )
        interpolated = levels[left] + ratio * (levels[clipped] - levels[left])
    result = np.where(grid == frequencies[left], levels[left], interpolated)
    result = np.where(index == 0, levels[0], result)
    result = np.where(index == len(frequencies), levels[-1], result)
    return tuple(result.tolist())


def _interpolate_many(
    response: FrequencyResponse,
    grid_hz: tuple[float, ...],
) -> tuple[float, ...]:
    return _interpolate_prepared(
        _prepare_response(response),
        grid_hz,
        _log2_many(grid_hz),
    )


@lru_cache(maxsize=64)
def _valid_grid(
    complete_grid: tuple[float, ...],
    excluded_bands: tuple[tuple[float, float], ...],
) -> tuple[float, ...]:
    if not excluded_bands or not complete_grid:
        return complete_grid
    np = _numpy()
    grid = np.asarray(complete_grid)
    excluded = np.zeros(grid.shape, dtype=bool)
    for low, high in excluded_bands:
        excluded |= (grid >= low) & (grid <= high)
    return tuple(f for f, drop in zip(complete_grid, excluded) if not drop)


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
    valid_grid = _valid_grid(complete_grid, excluded_bands)
    log_valid_grid = _log2_many(valid_grid)
    a_prepared = _prepare_response(a)
    b_prepared = _prepare_response(b)
    a_values = _interpolate_prepared(a_prepared, valid_grid, log_valid_grid)
    b_values = _interpolate_prepared(b_prepared, valid_grid, log_valid_grid)
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
            ref_grid = _valid_grid(_grid(ref_low, ref_high), excluded_bands)
            if len(ref_grid) >= 2:
                log_ref_grid = _log2_many(ref_grid)
                ref_a = _interpolate_prepared(a_prepared, ref_grid, log_ref_grid)
                ref_b = _interpolate_prepared(b_prepared, ref_grid, log_ref_grid)
                ref_diff = tuple(
                    a_value - b_value
                    for a_value, b_value in zip(ref_a, ref_b, strict=True)
                )
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
