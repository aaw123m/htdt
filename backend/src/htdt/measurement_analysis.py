"""Deterministic, non-destructive display-time analysis of frequency responses.

Everything here derives a *view* of a stored ``CadFrequencyResponseDataset`` —
fractional-octave smoothing, phase unwrapping, processing provenance — without
mutating persisted samples. Each transform is a named, versioned algorithm so
the provenance line shown next to a trace states exactly what was applied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import log10

import numpy as np

from .cad_measurement_models import CadFrequencyResponseDataset


# Supported smoothing views: label -> octave fraction denominator. 0 = stored.
DISPLAY_SMOOTHING_FRACTIONS: tuple[tuple[str, int], ...] = (
    ('原データ', 0),
    ('1/24 オクターブ', 24),
    ('1/12 オクターブ', 12),
    ('1/6 オクターブ', 6),
    ('1/3 オクターブ', 3),
)

SMOOTHING_ALGORITHM_ID = 'fractional-octave-power-mean-1'
PHASE_UNWRAP_ALGORITHM_ID = 'phase-unwrap-180deg-1'


@dataclass(frozen=True, slots=True)
class DerivedLevelTrace:
    """A level-vs-frequency trace with its exact derivation provenance."""

    frequency_hz: tuple[float, ...]
    level_db: tuple[float, ...]
    fraction: int
    algorithm: str | None
    source_smoothing: str | None
    source_already_smoothed: bool


@dataclass(frozen=True, slots=True)
class DerivedPhaseTrace:
    """A phase-vs-frequency trace; unwrapping never alters stored samples."""

    frequency_hz: tuple[float, ...]
    phase_deg: tuple[float, ...]
    unwrapped: bool
    algorithm: str | None


def smoothed_level_trace(
    dataset: CadFrequencyResponseDataset,
    fraction: int,
) -> DerivedLevelTrace:
    """Power-mean smoothing on a ±1/(2*fraction) octave window per sample.

    Deterministic: each output point sits at a source frequency and averages
    the power of every source sample inside its window. ``fraction=0`` returns
    the stored samples untouched.
    """

    frequency = tuple(dataset.frequency_hz)
    level = tuple(dataset.level_db)
    source_smoothing = dataset.smoothing
    already = source_smoothing not in (None, 'none', 'raw')
    if fraction <= 0 or len(frequency) < 3:
        return DerivedLevelTrace(
            frequency_hz=frequency,
            level_db=level,
            fraction=0,
            algorithm=None,
            source_smoothing=source_smoothing,
            source_already_smoothed=already,
        )
    half_width = 1.0 / (2.0 * fraction)
    smoothed: list[float] = []
    frequencies = np.asarray(frequency, dtype=np.float64)
    if (
        len(frequency) > 1
        and len(frequency) == len(level)
        and bool(np.all(frequencies[1:] >= frequencies[:-1]))
    ):
        # Sorted axis: each window is the contiguous slice
        # [searchsorted(lo,'left'), searchsorted(hi,'right')), so membership
        # matches the ``lo <= f <= hi`` scan exactly. Slicing a Python list
        # preserves the input order, so ``sum`` folds the window exactly as
        # the scalar loop did — window totals stay bit-identical (NumPy's
        # accumulate is pairwise and could drift an ulp). Window bounds
        # still come from scalar ``**`` so libm cannot shift membership.
        powers = [10.0 ** (v / 10.0) for v in level]
        lows = np.array(
            [c * (2.0 ** (-half_width)) for c in frequency],
            dtype=np.float64,
        )
        highs = np.array(
            [c * (2.0 ** half_width) for c in frequency], dtype=np.float64
        )
        lo_index = np.searchsorted(frequencies, lows, side='left')
        hi_index = np.searchsorted(frequencies, highs, side='right')
        for lo_i, hi_i in zip(lo_index, hi_index):
            window = powers[lo_i:hi_i]
            smoothed.append(10.0 * log10(sum(window) / len(window)))
    else:
        # Unsorted axis: contiguous slices do not apply — keep the scan.
        for center in frequency:
            lo = center * (2.0 ** (-half_width))
            hi = center * (2.0 ** half_width)
            powers = [
                10.0 ** (level[i] / 10.0)
                for i in range(len(frequency))
                if lo <= frequency[i] <= hi
            ]
            smoothed.append(10.0 * log10(sum(powers) / len(powers)))
    return DerivedLevelTrace(
        frequency_hz=frequency,
        level_db=tuple(smoothed),
        fraction=fraction,
        algorithm=SMOOTHING_ALGORITHM_ID,
        source_smoothing=source_smoothing,
        source_already_smoothed=already,
    )


def phase_trace(
    dataset: CadFrequencyResponseDataset,
    *,
    unwrap: bool = False,
) -> DerivedPhaseTrace | None:
    """Stored phase samples vs frequency; optional deterministic unwrap.

    Unwrap walks the samples once and offsets each point by a multiple of
    360° chosen to keep successive deltas within ±180°. The stored wrapped
    samples are never modified.
    """

    if dataset.phase_status == 'absent' or dataset.phase_deg is None:
        return None
    phase = tuple(dataset.phase_deg)
    frequency = tuple(dataset.frequency_hz)
    if not unwrap:
        return DerivedPhaseTrace(
            frequency_hz=frequency,
            phase_deg=phase,
            unwrapped=False,
            algorithm=None,
        )
    unwrapped = [phase[0]]
    for index in range(1, len(phase)):
        previous = unwrapped[-1]
        candidate = phase[index]
        while candidate - previous > 180.0:
            candidate -= 360.0
        while previous - candidate >= 180.0:
            candidate += 360.0
        unwrapped.append(candidate)
    return DerivedPhaseTrace(
        frequency_hz=frequency,
        phase_deg=tuple(unwrapped),
        unwrapped=True,
        algorithm=PHASE_UNWRAP_ALGORITHM_ID,
    )


def processing_summary(dataset: CadFrequencyResponseDataset) -> str:
    """Human-readable statement of the dataset's recorded processing state."""

    smoothing = dataset.smoothing
    smoothing_text = smoothing if smoothing not in (None, 'none', 'raw') else 'なし'
    parts = [f'入力スムージング: {smoothing_text}']
    try:
        processing = json.loads(dataset.processing_json)
    except (TypeError, ValueError):
        processing = None
    if isinstance(processing, dict) and processing:
        keys = ', '.join(sorted(str(key) for key in processing))
        parts.append(f'処理履歴: {keys}')
    else:
        parts.append('処理履歴: UNKNOWN')
    parts.append(f'レベル基準: {dataset.level_reference}')
    return ' · '.join(parts)


def trace_label(dataset: CadFrequencyResponseDataset, fraction: int) -> str:
    """Per-trace provenance suffix used on A/B plots."""

    if fraction <= 0:
        base = '原データ'
    else:
        base = f'1/{fraction}oct {SMOOTHING_ALGORITHM_ID}'
    if dataset.smoothing not in (None, 'none', 'raw'):
        base += f' (入力: {dataset.smoothing})'
    return base


__all__ = [
    'DISPLAY_SMOOTHING_FRACTIONS',
    'DerivedLevelTrace',
    'DerivedPhaseTrace',
    'PHASE_UNWRAP_ALGORITHM_ID',
    'SMOOTHING_ALGORITHM_ID',
    'phase_trace',
    'processing_summary',
    'smoothed_level_trace',
    'trace_label',
]
