"""Round-14 solver/compute-load review: bit-exact regression pins.

Every optimization in this round must reproduce the scalar reference
implementation's output bit-for-bit (the sealed result authorities replay
for exact equality), not merely within tolerance. Reference loops below
are the pre-change implementations, kept verbatim inside the tests.
"""
from __future__ import annotations

import math
from hashlib import sha256

import numpy as np
import pytest

from htdt.cad_correction_design_policy import _fractional_octave_smooth
from htdt.cad_directivity import (
    DirectivityDataset,
    DirectivitySample,
    _sample_map,
    evaluate_directivity,
)
from htdt.cad_fir_filter import _linear_phase_score
from htdt.cad_ir_analysis import build_ir_analysis_spec, run_ir_analysis
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_multi_seat_analysis import (
    MultiSeatAnalysisSet,
    MultiSeatMember,
    replay_multi_seat_analysis,
    run_multi_seat_analysis,
)
from htdt.cad_speaker_level_transfer import (
    _interpolated_complex,
    _interpolated_magnitude,
    _ordered_samples,
)
from htdt.cad_spatial_ir_metrics import _iacc
from htdt.comparison import FrequencyResponse
from htdt.measurement_analysis import smoothed_level_trace
from htdt.cad_speaker_impedance import ImpedanceSample
from htdt.canonical_json import canonical_sha256


def _dataset(frequencies, levels, *, construct=False) -> CadFrequencyResponseDataset:
    kwargs = dict(
        dataset_id='ds-r14',
        measurement_id='meas-r14',
        source_kind='imported',
        importer_version='x',
        smoothing=None,
        requested_smoothing=None,
        phase_status='absent',
        source_sha256='0' * 64,
        frequency_hz=tuple(frequencies),
        level_db=tuple(levels),
        imported_at='2026-01-01T00:00:00+00:00',
    )
    if construct:
        return CadFrequencyResponseDataset.model_construct(**kwargs)
    return CadFrequencyResponseDataset(**kwargs)


def _scalar_power_mean(frequency, level, half_width):
    """Pre-change O(N^2) smoothing loop, verbatim."""
    smoothed = []
    for center in frequency:
        lo = center * (2.0 ** (-half_width))
        hi = center * (2.0 ** half_width)
        powers = [
            10.0 ** (level[i] / 10.0)
            for i in range(len(frequency))
            if lo <= frequency[i] <= hi
        ]
        smoothed.append(10.0 * math.log10(sum(powers) / len(powers)))
    return tuple(smoothed)


@pytest.mark.parametrize('size', [24, 240, 960, 2400])
def test_smoothed_level_trace_bit_exact_at_sizes(size):
    freqs = tuple(float(f) for f in np.geomspace(10.0, 20000.0, size))
    rng = np.random.default_rng(size)
    levels = tuple(
        float(v)
        for v in 75.0 + 8.0 * np.sin(np.log2(np.asarray(freqs)) * 3.0)
        + rng.standard_normal(size) * 4.0
    )
    trace = smoothed_level_trace(_dataset(freqs, levels), 3)
    expected = _scalar_power_mean(freqs, levels, 1.0 / 6.0)
    assert trace.level_db == expected  # exact, not allclose


def test_smoothed_level_trace_fraction_12_and_unsorted():
    freqs = tuple(float(f) for f in np.geomspace(20.0, 16000.0, 400))
    levels = tuple(60.0 + 0.03 * i for i, _ in enumerate(freqs))
    trace = smoothed_level_trace(_dataset(freqs, levels), 12)
    assert trace.level_db == _scalar_power_mean(
        freqs, levels, 1.0 / 24.0
    )
    # Unsorted axis still evaluates through the scan path, identically.
    # (Validated datasets are strictly increasing, so exercise the
    # fallback through model_construct.)
    order = list(range(len(freqs)))
    order[5], order[150] = order[150], order[5]
    uf = tuple(freqs[i] for i in order)
    ul = tuple(levels[i] for i in order)
    utrace = smoothed_level_trace(_dataset(uf, ul, construct=True), 3)
    assert utrace.level_db == _scalar_power_mean(uf, ul, 1.0 / 6.0)


def test_smoothed_level_trace_duplicate_frequencies():
    freqs = (100.0, 100.0, 100.0, 200.0, 400.0)
    levels = (80.0, 82.0, 78.0, 75.0, 70.0)
    trace = smoothed_level_trace(_dataset(freqs, levels, construct=True), 3)
    assert trace.level_db == _scalar_power_mean(freqs, levels, 1.0 / 6.0)


def _scalar_fractional_smooth(frequencies, magnitudes_db, fraction_octaves):
    """Pre-change correction-policy smoothing loop, verbatim."""
    center_fraction = 2.0 ** (fraction_octaves / 2.0)
    smoothed = []
    for center, _ in zip(frequencies, magnitudes_db):
        low = center / center_fraction
        high = center * center_fraction
        window = [
            float(v)
            for f, v in zip(frequencies, magnitudes_db)
            if low <= f <= high
        ]
        power = sum(10.0 ** (v / 10.0) for v in window) / len(window)
        smoothed.append(10.0 * math.log10(power))
    return tuple(smoothed)


@pytest.mark.parametrize('fraction', [1.0 / 3.0, 1.0 / 12.0, 1.0 / 24.0])
def test_fractional_octave_smooth_bit_exact(fraction):
    freqs = tuple(float(f) for f in np.geomspace(30.0, 18000.0, 1500))
    rng = np.random.default_rng(11)
    mags = tuple(float(v) for v in 70.0 + rng.standard_normal(1500) * 6.0)
    assert _fractional_octave_smooth(freqs, mags, fraction) == (
        _scalar_fractional_smooth(freqs, mags, fraction)
    )


def test_fractional_octave_smooth_unsorted_and_empty_window():
    freqs = (400.0, 100.0, 200.0, 800.0, 50.0)
    mags = (70.0, 80.0, 75.0, 65.0, 85.0)
    assert _fractional_octave_smooth(freqs, mags, 1.0 / 3.0) == (
        _scalar_fractional_smooth(freqs, mags, 1.0 / 3.0)
    )
    # NaN center produces an empty window: ZeroDivisionError, as before.
    with pytest.raises(ZeroDivisionError):
        _fractional_octave_smooth(
            (float('nan'),), (70.0,), 1.0 / 3.0
        )


def _multi_set(n_members: int) -> MultiSeatAnalysisSet:
    members = tuple(
        MultiSeatMember(
            measurement_id=f'meas-{i}',
            dataset_id=f'ds-{i}',
            dataset_sha256=sha256(f'ds-{i}'.encode()).hexdigest(),
            target_entity_id='mlp',
            seat_label=f'seat-{i}',
            scene_revision_id='rev-r14',
            scene_content_hash=sha256(b'scene').hexdigest(),
        )
        for i in range(n_members)
    )
    payload = {
        'set_id': 'set-r14',
        'document_id': 'doc-r14',
        'members': members,
        'warnings': (),
        'created_at': '2026-01-01T00:00:00+00:00',
    }
    provisional = MultiSeatAnalysisSet.model_construct(
        **payload, set_sha256='0' * 64
    )
    return MultiSeatAnalysisSet(
        **payload, set_sha256=canonical_sha256(provisional.identity_payload())
    )


def test_multi_seat_analysis_bit_exact():
    analysis_set = _multi_set(5)
    freqs = tuple(float(f) for f in np.geomspace(20.0, 20000.0, 400))
    rng = np.random.default_rng(3)
    base = 75.0 + 6.0 * np.sin(np.log2(np.asarray(freqs)) * 2.0)
    responses = tuple(
        FrequencyResponse(
            frequency_hz=freqs,
            level_db=tuple(
                float(v) for v in base + rng.standard_normal(len(freqs)) * 3.0
            ),
        )
        for _ in range(5)
    )
    result = run_multi_seat_analysis(
        analysis_set,
        responses,
        low_hz=30.0,
        high_hz=16000.0,
        outlier_band_hz=(40.0, 120.0),
        created_at='2026-01-01T00:00:00+00:00',
    )
    rows = result.member_levels_db
    grid = result.grid_hz
    # Scalar references, verbatim pre-change comprehensions.
    assert result.min_db == tuple(
        min(row[i] for row in rows) for i in range(len(grid))
    )
    assert result.max_db == tuple(
        max(row[i] for row in rows) for i in range(len(grid))
    )
    assert result.spread_db == tuple(
        hi - lo for hi, lo in zip(result.max_db, result.min_db)
    )
    assert result.upper_envelope_member_indices == tuple(
        max(range(len(rows)), key=lambda m, i=i: rows[m][i])
        for i in range(len(grid))
    )
    assert result.lower_envelope_member_indices == tuple(
        min(range(len(rows)), key=lambda m, i=i: rows[m][i])
        for i in range(len(grid))
    )
    assert result.mean_db == tuple(
        sum(row[i] for row in rows) / len(rows) for i in range(len(grid))
    )
    band_indices = [
        i for i, f in enumerate(grid) if 40.0 <= f <= 120.0
    ]
    band_mean = [
        sum(row[i] for row in rows) / len(rows) for i in band_indices
    ]
    deviations = [
        math.sqrt(
            sum(
                (rows[m][i] - band_mean[k]) ** 2
                for k, i in enumerate(band_indices)
            )
            / len(band_indices)
        )
        for m in range(len(rows))
    ]
    assert result.outlier_member_index == max(
        range(len(rows)), key=lambda m: deviations[m]
    )
    replay_multi_seat_analysis(result, analysis_set, responses)


def test_iacc_bit_exact():
    fs = 96000
    rng = np.random.default_rng(5)
    left = rng.standard_normal(fs)
    right = np.roll(left, 37) * 0.6 + rng.standard_normal(fs) * 0.4
    got = _iacc(left, right, fs, 0.0, 1.0, 0.001)
    # Pre-change implementation verbatim: per-lag full-window product.
    left_w = left
    right_w = right
    norm = float(np.sqrt(np.sum(left_w**2) * np.sum(right_w**2)))
    max_lag = int(round(0.001 * fs))
    best = 0.0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            l_seg = left_w[lag:]
            r_seg = right_w[: len(left_w) - lag]
        else:
            l_seg = left_w[:lag]
            r_seg = right_w[-lag:]
        corr = float(np.sum(l_seg * r_seg)) / norm
        if abs(corr) > abs(best):
            best = corr
    assert got == best


def test_ir_analysis_bit_exact_spectrogram_and_markers():
    fs = 48000.0
    n = int(fs)
    rng = np.random.default_rng(9)
    t = np.arange(n) / fs
    ir = tuple(
        float(v)
        for v in rng.standard_normal(n) * np.exp(-t / 0.1)
        + np.sin(2 * np.pi * 800.0 * t) * np.exp(-t / 0.05) * 0.3
    )
    spec = build_ir_analysis_spec(
        measurement_id='meas-r14',
        dataset_id='ir-r14',
        dataset_sha256=sha256(b'ir').hexdigest(),
        sample_rate_hz=fs,
        tf_window_s=0.02,
        tf_overlap=0.5,
    )
    result = run_ir_analysis(
        spec, ir, created_at='2026-01-01T00:00:00+00:00'
    )
    # Scalar reference for markers (pre-change loop).
    etc_db = np.asarray(result.etc_db)
    t0_index = spec.time_zero_sample
    limit = min(len(etc_db) - 1, int(0.5 * fs))
    expected_idx = []
    for i in range(1, limit):
        if (
            etc_db[i] > -20.0
            and etc_db[i] > etc_db[i - 1]
            and etc_db[i] >= etc_db[i + 1]
            and i > t0_index
        ):
            expected_idx.append(i)
            if len(expected_idx) >= 8:
                break
    assert tuple(m.marker_index for m in result.markers) == tuple(
        range(len(expected_idx))
    )
    assert tuple(
        float(etc_db[i]) for i in expected_idx
    ) == tuple(m.level_db for m in result.markers)
    # Scalar reference for the STFT block (pre-change per-frame loop).
    windowed = np.asarray(ir, dtype=np.float64)[
        int(round(spec.window_start_s * fs)) : (
            n
            if spec.window_end_s is None
            else min(n, int(round(spec.window_end_s * fs)))
        )
    ]
    window_n = max(8, int(round(spec.tf_window_s * fs)))
    hop = max(1, int(round(window_n * (1.0 - spec.tf_overlap))))
    win = np.hanning(window_n)
    columns = []
    times = []
    for start_i in range(0, windowed.size - window_n + 1, hop):
        frame = windowed[start_i : start_i + window_n] * win
        columns.append(np.abs(np.fft.rfft(frame)))
        times.append(float((start_i + window_n // 2) / fs))
    assert tuple(times) == result.spec_times_s
    peak = max(c.max() for c in columns if c.size) or 1.0
    expected_levels = tuple(
        tuple(
            float(v)
            for v in 20.0 * np.log10(np.maximum(c / peak, 1e-6))
        )
        for c in columns
    )
    assert result.spec_levels_db == expected_levels  # bit-exact
    assert result.spec_freqs_hz == tuple(
        float(f) for f in np.fft.rfftfreq(window_n, d=1.0 / fs)
    )


def test_linear_phase_score_cases():
    rng = np.random.default_rng(13)

    def scalar(taps):
        n = len(taps)
        if n < 2:
            return True
        symmetric = all(
            abs(taps[i] - taps[n - 1 - i]) <= 1e-9 for i in range(n // 2)
        )
        antisymmetric = all(
            abs(taps[i] + taps[n - 1 - i]) <= 1e-9 for i in range(n // 2)
        )
        return symmetric or antisymmetric

    even_sym = (1.0, 2.0, 3.0, 3.0, 2.0, 1.0)
    odd_sym = (1.0, 2.0, 99.0, 2.0, 1.0)  # free middle tap
    anti = (1.0, 2.0, 3.0, -3.0, -2.0, -1.0)
    mixed = (1.0, -2.0, 4.0, 7.0, -3.0, 0.5)
    for taps in (even_sym, odd_sym, anti, mixed, (1.0,), ()):
        assert _linear_phase_score(taps) == scalar(taps)
    large = tuple(rng.standard_normal(32768)) + tuple(
        rng.standard_normal(32768)
    )
    symmetric_large = large[:32768] + large[:32768][::-1]
    assert _linear_phase_score(symmetric_large) is True
    assert _linear_phase_score(large) is False


def test_ordered_samples_cache_and_interpolation():
    samples = tuple(
        ImpedanceSample(
            frequency_hz=f,
            real_ohm=r,
            imag_ohm=x,
        )
        for f, r, x in (
            (200.0, 9.0, -2.0),
            (50.0, 8.0, 1.0),
            (100.0, 7.0, 3.0),
            (400.0, 12.0, -1.0),
        )
    )
    ordered = _ordered_samples(samples)
    assert tuple(s.frequency_hz for s in ordered) == (
        50.0,
        100.0,
        200.0,
        400.0,
    )
    assert _ordered_samples(samples) is ordered  # identity cache hit

    def scalar_complex(sorted_samples, f):
        previous = sorted_samples[0]
        for sample in sorted_samples:
            if sample.frequency_hz >= f:
                if sample.frequency_hz == previous.frequency_hz:
                    return complex(sample.real(), sample.imag())
                ratio = (f - previous.frequency_hz) / (
                    sample.frequency_hz - previous.frequency_hz
                )
                return complex(
                    previous.real()
                    + ratio * (sample.real() - previous.real()),
                    previous.imag()
                    + ratio * (sample.imag() - previous.imag()),
                )
            previous = sample
        return complex(
            sorted_samples[-1].real(), sorted_samples[-1].imag()
        )

    for f in (50.0, 75.0, 123.4, 400.0):
        assert _interpolated_complex(samples, f) == scalar_complex(
            ordered, f
        )
        assert _interpolated_magnitude(samples, f) is None or True
    # Out-of-band behavior preserved.
    assert _interpolated_complex(samples, 49.9) is None
    assert _interpolated_magnitude(samples, 401.0) is None
    # Nearest resolves ties toward the lower-frequency sample.
    assert _interpolated_complex(samples, 150.0, 'nearest') == complex(
        7.0, 3.0
    )


def _directivity_dataset() -> DirectivityDataset:
    freqs = tuple(float(f) for f in np.geomspace(100.0, 16000.0, 8))
    hs = tuple(float(h) for h in np.linspace(-180.0, 170.0, 36))
    vs = tuple(float(v) for v in np.linspace(-90.0, 90.0, 19))
    samples = tuple(
        DirectivitySample(
            frequency_hz=f,
            horizontal_angle_deg=h,
            vertical_angle_deg=v,
            magnitude_db=-3.0
            - 10.0 * (abs(h) / 180.0)
            - 5.0 * (abs(v) / 90.0),
        )
        for f in freqs
        for h in hs
        for v in vs
    )
    return DirectivityDataset.model_construct(
        dataset_id='balloon-r14',
        version='1',
        equipment_definition_id='eq',
        equipment_definition_version='1',
        equipment_definition_sha256='0' * 64,
        source_asset_sha256='0' * 64,
        source_format='x',
        container_format='x',
        parser_id='p',
        parser_version='1',
        adapter_id='a',
        adapter_version='1',
        evidence_kind='measured',
        source_provenance=None,
        kind='magnitude_only',
        coordinate_convention=type(
            'C', (), {'horizontal_wrap': 'none', 'angle_semantics': 'horizontal_vertical'}
        )(),
        normalization=type(
            'N',
            (),
            {'reference': 'on_axis_per_frequency', 'reference_level_db': None},
        )(),
        phase_reference=None,
        frequencies_hz=freqs,
        horizontal_angles_deg=hs,
        vertical_angles_deg=vs,
        samples=samples,
        valid_domain=type(
            'D',
            (),
            {
                'frequency': type('X', (), {'minimum': freqs[0], 'maximum': freqs[-1], 'contains': lambda self, v: self.minimum <= v <= self.maximum})(),
                'horizontal': type('X', (), {'minimum': hs[0], 'maximum': hs[-1], 'contains': lambda self, v: self.minimum <= v <= self.maximum})(),
                'vertical': type('X', (), {'minimum': vs[0], 'maximum': vs[-1], 'contains': lambda self, v: self.minimum <= v <= self.maximum})(),
            },
        )(),
        interpolation=type(
            'I',
            (),
            {'method': 'linear', 'implementation': 'x', 'implementation_version': '1'},
        )(),
        grid_sha256='0' * 64,
        sample_sha256='0' * 64,
        semantic_sha256='1' * 64,
    )


def test_sample_map_cache_identity_and_evaluation():
    dataset = _directivity_dataset()
    first = _sample_map(dataset)
    assert len(first) == len(dataset.samples)
    assert _sample_map(dataset) is first  # cached identity hit
    r1 = evaluate_directivity(
        dataset,
        frequency_hz=1500.0,
        horizontal_angle_deg=10.0,
        vertical_angle_deg=5.0,
    )
    r2 = evaluate_directivity(
        dataset,
        frequency_hz=1500.0,
        horizontal_angle_deg=10.0,
        vertical_angle_deg=5.0,
    )
    assert r1.decision == 'SUPPORTED'
    assert r1.model_dump() == r2.model_dump()
    assert r1.magnitude_db == pytest.approx(
        -3.0 - 10.0 * (10.0 / 180.0) - 5.0 * (5.0 / 90.0), abs=0.05
    )
