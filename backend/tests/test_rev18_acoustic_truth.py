"""REV18-ACOUSTIC: independent recompute of numeric/acoustic claims.

Every assertion in this file recomputes the quantity under test from a
reference implementation written here — textbook formulas, closed-form
solutions, or scipy references — rather than trusting the production code
path. Tolerances are annotated per check: exact recompute paths get
ulp-level tolerances; noisy/statistical checks get justified looser ones.

Dimensions covered (per REV18 charter):
 1. Decay metrics (EDT/T20/T30) vs Schroeder integration on synthetic
    exponentially-decaying IRs of known T60.
 2. Room-mode frequencies vs the closed-form rigid-box solution;
    first-order reflections vs the image-source method.
 3. Spectrogram values vs scipy.signal.stft on identical inputs.
 4. Fractional-octave smoothing vs a brute-force power-mean reference.
 5. IACC / lateral-fraction / late-lateral-level vs textbook formulas on
    constructed signals.
 6. SPL summation and dB conversions (10*log10 energy vs 20*log10
    amplitude, 20 uPa reference).
 7. Unit conversions (Hz<->rad/s, samples<->seconds, nepers<->T60)
    inside solver plumbing.
"""

from __future__ import annotations

import math
from cmath import exp as cexp
from hashlib import sha256
from math import atan2, degrees, isfinite, log10, pi, sqrt

import numpy as np
import pytest
import scipy.signal

from htdt.acoustic_pffdtd_adapter import finite_record_pressure_transfer
from htdt.acoustic_pffdtd_causal_boundary import (
    CausalAdmittanceBranch,
    build_causal_frequency_dependent_boundary_authority,
    evaluate_normalized_admittance,
)
from htdt.acoustic_spatial_decomposition import (
    ComplexPressureValue,
    ExactDecompositionAuthorityRef,
    SpatialDecompositionConditioningPolicy,
    SpatialPressureSamplePoint,
    TwoPointFrequencyPressure,
    build_spatial_field_decomposition_spec,
    build_two_point_complex_pressure_evidence,
    decompose_planar_normal_incidence_two_point,
)
from htdt.acoustics import first_order_reflections, rectangular_room_modes
from htdt.cad_calibration import (
    build_biquad_filter,
    calculate_biquad_coefficients,
    evaluate_biquad_db,
)
from htdt.cad_correction_design_policy import _fractional_octave_smooth
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_equipment_self_noise import (
    combine_noise_sources_energy,
    predict_free_field_listener_level,
)
from htdt.cad_fir_filter import build_fir_filter_artifact, evaluate_fir_artifact
from htdt.cad_hybrid_numerical_composition import _weights
from htdt.cad_hybrid_prediction_provider import (
    SPL_REFERENCE_PA,
    HybridAbsolutePressureSample,
)
from htdt.cad_ir_analysis import (
    _fft_bandpass,
    _hilbert_envelope_db,
    _schroeder_db,
    build_ir_analysis_spec,
    run_ir_analysis,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measured_modal_analysis import MeasuredMode
from htdt.cad_multi_seat_analysis import (
    MultiSeatMember,
    build_multi_seat_set,
    run_multi_seat_analysis,
)
from htdt.cad_scene import Direction3, Position3
from htdt.cad_spatial_ir_metrics import (
    DirectionalIrChannelRef,
    ImpulseEarEvidenceRef,
    build_spatial_ir_metric_spec,
    evaluate_spatial_ir_metric,
    _iacc,
    _late_lateral_level,
    _lateral_fraction,
    _lateral_fraction_cosine,
)
from htdt.comparison import (
    FrequencyResponse,
    _grid,
    _interpolate,
    compare_frequency_responses,
)
from htdt.measurement_analysis import smoothed_level_trace


FS = 48000.0
C_M_S = 343.0


def _hash(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _pcm_hash(samples) -> str:
    return sha256(np.asarray(samples, dtype="<f8").tobytes()).hexdigest()


def _ir_spec(**overrides):
    kwargs = dict(
        measurement_id="meas-1",
        dataset_id="ir-1",
        dataset_sha256=sha256(b"ir-bytes").hexdigest(),
        sample_rate_hz=FS,
        tf_window_s=0.05,
        tf_overlap=0.5,
    )
    kwargs.update(overrides)
    return build_ir_analysis_spec(**kwargs)


def _make_dataset(frequency_hz, level_db, **overrides):
    kwargs = dict(
        dataset_id="ds-1",
        measurement_id="meas-1",
        frequency_hz=tuple(float(f) for f in frequency_hz),
        level_db=tuple(float(v) for v in level_db),
        phase_deg=None,
        phase_status="absent",
        source_sha256="0" * 64,
        importer_version="test-1",
    )
    kwargs.update(overrides)
    return CadFrequencyResponseDataset(**kwargs)


# ---------------------------------------------------------------------------
# Dimension 1 — decay metrics vs Schroeder truth on synthetic enclosures.
# ---------------------------------------------------------------------------


def _schroeder_reference_db(samples: np.ndarray) -> np.ndarray:
    """Textbook Schroeder curve: 10*log10 of the reverse cumulative energy."""
    energy = np.asarray(samples, dtype=np.float64) ** 2
    cumulative = np.cumsum(energy[::-1])[::-1]
    return 10.0 * np.log10(cumulative / cumulative[0])


def _fit_rt60_reference(times, curve_db, lo_db, hi_db):
    mask = (curve_db >= lo_db) & (curve_db <= hi_db)
    slope = np.polyfit(times[mask], curve_db[mask], 1)[0]
    return -60.0 / slope


def test_schroeder_curve_matches_reverse_cumulative_reference():
    # Direct recompute of the reverse-cumsum definition; identical float64
    # operations, so only a clamp-floor difference is allowed.
    rng = np.random.default_rng(11)
    n = 2048
    h = rng.standard_normal(n) * np.exp(-np.arange(n) / (0.05 * FS))
    ref = _schroeder_reference_db(h)
    got = _schroeder_db(h)
    np.testing.assert_allclose(got, np.maximum(ref, -140.0), atol=1e-12)


def test_decay_metrics_recover_known_rt60_from_exponential_ir():
    # Construct an IR whose *energy envelope* decays exactly 60 dB in T60:
    # h(t) = exp(-alpha t) with alpha = 3*ln(10)/T60 (amplitude nepers/s),
    # so h^2 decays at 20*log10(e)*2*alpha... i.e. -60 dB at t = T60.
    t60_true = 0.4
    alpha = 3.0 * math.log(10.0) / t60_true
    n = int(1.2 * FS)
    t = np.arange(n) / FS
    h = np.exp(-alpha * t)
    spec = _ir_spec(noise_floor_method="none")
    result = run_ir_analysis(
        spec, tuple(float(v) for v in h), created_at="2026-09-29T00:00:00+00:00"
    )
    metrics = {m.metric: m for m in result.metrics}
    for name in ("edt", "t20", "t30"):
        metric = metrics[name]
        assert metric.status == "estimated", (name, metric.reason)
        # A perfectly linear Schroeder curve has one exact slope; any
        # deviation from T60 is a regression-boundary or scaling bug.
        assert metric.value_s == pytest.approx(t60_true, rel=0.02)


def test_decay_metrics_match_independent_slope_fit():
    # Noise-modulated exponential: recompute the Schroeder curve and the
    # regression in-test, and require the reported estimate to equal the
    # recompute far below numerical noise (same definition, float64).
    rng = np.random.default_rng(3)
    n = int(1.0 * FS)
    t = np.arange(n) / FS
    h = rng.standard_normal(n) * np.exp(-3.0 * math.log(10.0) * t / 0.35)
    spec = _ir_spec(noise_floor_method="none")
    result = run_ir_analysis(
        spec, tuple(float(v) for v in h), created_at="2026-09-29T00:00:00+00:00"
    )
    curve = np.maximum(_schroeder_reference_db(h), -140.0)
    for metric, (hi, lo) in (
        ("edt", (0.0, -10.0)),
        ("t20", (-5.0, -25.0)),
        ("t30", (-5.0, -35.0)),
    ):
        got = {m.metric: m for m in result.metrics}[metric]
        assert got.status == "estimated"
        ref = _fit_rt60_reference(t, curve, lo, hi)
        assert got.value_s == pytest.approx(ref, rel=1e-9)
        # And the slope sign/factor convention: -60/slope gives seconds.
        assert got.slope_db_per_s == pytest.approx(-60.0 / ref, rel=1e-9)


def test_hilbert_envelope_matches_scipy_reference():
    rng = np.random.default_rng(5)
    n = 3000
    h = rng.standard_normal(n) * np.exp(-np.arange(n) / (0.04 * FS))
    analytic = scipy.signal.hilbert(h)
    ref = 10.0 * np.log10(np.abs(analytic) ** 2 / np.max(np.abs(analytic) ** 2))
    got = _hilbert_envelope_db(h)
    np.testing.assert_allclose(got, ref, atol=1e-10)


def test_brickwall_band_edges_are_exact_octave_ratios():
    # Octave band edges are fc * 2^(+-1/2); third-octave fc * 2^(+-1/6).
    # Recompute the whole brickwall mask + irfft directly.
    n = 8192
    rng = np.random.default_rng(19)
    signal = rng.standard_normal(n)
    for fraction, half_ratio in (
        ("octave", 2.0 ** 0.5),
        ("third_octave", 2.0 ** (1.0 / 6.0)),
    ):
        fc = 400.0
        spectrum = np.fft.rfft(signal)
        freqs = np.fft.rfftfreq(n, d=1.0 / FS)
        mask = (freqs >= fc / half_ratio) & (freqs <= fc * half_ratio)
        ref = np.fft.irfft(spectrum * mask, n=n)
        got = _fft_bandpass(signal, FS, fc, fraction)
        np.testing.assert_allclose(got, ref, atol=1e-12)
        # Sanity: a tone at band center survives; a tone outside dies.
        tone_in = np.sin(2 * pi * np.arange(n) * fc / FS)
        tone_out = np.sin(2 * pi * np.arange(n) * fc * 2.2 / FS)
        for tone, expect_energy in ((tone_in, True), (tone_out, False)):
            filtered = _fft_bandpass(tone, FS, fc, fraction)
            ratio = float(np.sum(filtered**2) / np.sum(tone**2))
            if expect_energy:
                assert ratio > 0.9
            else:
                assert ratio < 0.01


def test_clarity_metrics_match_direct_energy_integrals():
    # Direct at t0, one reflection 30 ms later, exponential tail.
    t0 = int(0.02 * FS)
    split50 = int(round(0.050 * FS))
    n = int(0.6 * FS)
    rng = np.random.default_rng(13)
    h = np.zeros(n)
    h[t0] = 1.0
    h[t0 + int(0.03 * FS)] = 0.4
    tail_start = t0 + int(0.05 * FS)
    tail_n = n - tail_start
    h[tail_start:] += (
        rng.standard_normal(tail_n)
        * np.exp(-np.arange(tail_n) / (0.06 * FS))
        * 0.05
    )
    spec = _ir_spec(time_zero_sample=t0, noise_floor_method="none")
    result = run_ir_analysis(
        spec, tuple(float(v) for v in h), created_at="2026-09-29T00:00:00+00:00"
    )
    energy = np.asarray(h, dtype=np.float64) ** 2
    early = float(energy[t0 : t0 + split50].sum())
    late = float(energy[t0 + split50 :].sum())
    metrics = {
        (m.metric, m.split_time_ms): m for m in result.energy_metrics
    }
    c50 = metrics[("c50", 50.0)]
    assert c50.status == "estimated"
    assert c50.value == pytest.approx(10.0 * log10(early / late), abs=1e-9)
    d50 = metrics[("d50", 50.0)]
    assert d50.value == pytest.approx(early / (early + late), abs=1e-12)


def test_spectrogram_matches_scipy_stft_on_identical_inputs():
    rng = np.random.default_rng(17)
    n = int(0.9 * FS)
    t = np.arange(n) / FS
    h = rng.standard_normal(n) * np.exp(-t / 0.05)
    spec = _ir_spec(tf_window_s=0.05, tf_overlap=0.5, noise_floor_method="none")
    result = run_ir_analysis(
        spec, tuple(float(v) for v in h), created_at="2026-09-29T00:00:00+00:00"
    )
    window_n = max(8, int(round(0.05 * FS)))
    hop = max(1, int(round(window_n * 0.5)))
    win = np.hanning(window_n)
    freqs, times, zxx = scipy.signal.stft(
        h,
        fs=FS,
        window=win,
        nperseg=window_n,
        noverlap=window_n - hop,
        nfft=None,
        detrend=False,
        return_onesided=True,
        boundary=None,
        padded=False,
    )
    ref_mag = np.abs(zxx).T  # frames x bins
    ref_db = 20.0 * np.log10(np.maximum(ref_mag / ref_mag.max(), 1e-6))
    got = np.asarray(result.spec_levels_db)
    assert got.shape == ref_db.shape
    # Same window/frames/FFT: only libfft rounding separates them.
    np.testing.assert_allclose(got, ref_db, atol=1e-6)
    # Frequency axis and frame-center times follow the declared conventions.
    np.testing.assert_allclose(
        np.asarray(result.spec_freqs_hz),
        np.fft.rfftfreq(window_n, d=1.0 / FS),
        atol=0.0,
    )
    np.testing.assert_allclose(
        np.asarray(result.spec_times_s), times, atol=1e-12
    )


# ---------------------------------------------------------------------------
# Dimension 2 — room modes and image-source paths vs closed form.
# ---------------------------------------------------------------------------


def test_room_modes_match_closed_form_standing_wave_solution():
    w, d, h = 4.6, 3.8, 2.7
    max_hz = 220.0
    modes = rectangular_room_modes(w, d, h, max_hz=max_hz, sound_speed_m_s=C_M_S)
    # Independent closed-form recompute: f = c/2 * sqrt(sum((n_i/L_i)^2)).
    expected = []
    for nx in range(int(2 * max_hz * w / C_M_S) + 2):
        for ny in range(int(2 * max_hz * d / C_M_S) + 2):
            for nz in range(int(2 * max_hz * h / C_M_S) + 2):
                if nx == ny == nz == 0:
                    continue
                f = (C_M_S / 2.0) * math.sqrt(
                    (nx / w) ** 2 + (ny / d) ** 2 + (nz / h) ** 2
                )
                if f <= max_hz:
                    nonzero = sum(v > 0 for v in (nx, ny, nz))
                    cls = {1: "axial", 2: "tangential", 3: "oblique"}[nonzero]
                    expected.append((nx, ny, nz, f, cls))
    expected.sort(key=lambda item: (item[3], item[0], item[1], item[2]))
    got = [(m.n_x, m.n_y, m.n_z, m.frequency_hz, m.mode_class) for m in modes]
    assert len(got) == len(expected)
    for g, e in zip(got, expected):
        assert g[:3] == e[:3] and g[4] == e[4]
        assert g[3] == pytest.approx(e[3], rel=1e-12)
    # Axial fundamental along the longest dimension is exactly c/(2L).
    axial_x = next(m for m in modes if (m.n_x, m.n_y, m.n_z) == (1, 0, 0))
    assert axial_x.frequency_hz == pytest.approx(C_M_S / (2.0 * w))


def test_first_order_reflections_match_image_source_geometry():
    w, d, h = 4.0, 5.0, 2.6
    source = (1.0, 1.2, 1.1)
    receiver = (3.1, 3.7, 1.4)
    out = first_order_reflections(
        w,
        d,
        h,
        source,
        receiver,
        speaker_id="FL",
        speaker_role="front_left",
        sound_speed_m_s=C_M_S,
    )
    direct = math.dist(source, receiver)
    surfaces = {c.surface: c for c in out}
    assert len(surfaces) == 6
    # Recompute each reflection independently via plane mirroring.
    for label, axis, plane in (
        ("left_x0", 0, 0.0),
        ("right_xW", 0, w),
        ("front_y0", 1, 0.0),
        ("rear_yD", 1, d),
        ("floor_z0", 2, 0.0),
        ("ceiling_zH", 2, h),
    ):
        cand = surfaces[label]
        mirrored = list(source)
        mirrored[axis] = 2.0 * plane - source[axis]
        refl_len = math.dist(tuple(mirrored), receiver)
        assert cand.reflected_length_m == pytest.approx(refl_len, abs=1e-12)
        excess = refl_len - direct
        assert cand.excess_length_m == pytest.approx(excess, abs=1e-12)
        assert cand.excess_delay_ms == pytest.approx(
            excess / C_M_S * 1000.0, abs=1e-9
        )
        assert cand.first_destructive_hz == pytest.approx(
            C_M_S / (2.0 * excess), rel=1e-9
        )
        # Reflection point lies on the surface plane and on the
        # mirrored-source -> receiver segment.
        point = cand.reflection_point_m
        assert point[axis] == pytest.approx(plane, abs=1e-9)
        t = (plane - mirrored[axis]) / (receiver[axis] - mirrored[axis])
        expected_point = tuple(
            mirrored[i] + t * (receiver[i] - mirrored[i]) for i in range(3)
        )
        for got_v, exp_v in zip(point, expected_point):
            assert got_v == pytest.approx(exp_v, abs=1e-9)


# ---------------------------------------------------------------------------
# Dimension 4 — fractional-octave smoothing vs brute-force power mean.
# ---------------------------------------------------------------------------


def _power_mean_reference(frequencies, levels_db, fraction):
    """Literal +-1/(2N) octave window membership scan, power averaging."""
    half = 1.0 / (2.0 * fraction)
    out = []
    for center in frequencies:
        lo = center * 2.0 ** (-half)
        hi = center * 2.0 ** half
        window = [
            10.0 ** (v / 10.0)
            for f, v in zip(frequencies, levels_db)
            if lo <= f <= hi
        ]
        out.append(10.0 * log10(sum(window) / len(window)))
    return out


def test_smoothed_level_trace_matches_power_mean_reference():
    # Dense log-spaced grid with a peaky response.
    freqs = tuple(2.0 ** (k / 24.0) for k in range(24 * 3, 24 * 11))
    levels = tuple(
        -20.0 * abs(math.log2(f / 630.0)) for f in freqs
    )
    dataset = _make_dataset(freqs, levels)
    for fraction in (24, 12, 6, 3):
        got = smoothed_level_trace(dataset, fraction)
        ref = _power_mean_reference(freqs, levels, fraction)
        assert len(got.level_db) == len(ref)
        for g, r in zip(got.level_db, ref):
            assert g == pytest.approx(r, abs=1e-9)


def test_correction_policy_smoothing_matches_same_power_mean():
    rng = np.random.default_rng(23)
    freqs = tuple(2.0 ** (k / 48.0) for k in range(48 * 5, 48 * 8))
    levels = tuple(float(v) for v in rng.normal(-70.0, 4.0, len(freqs)))
    got = _fractional_octave_smooth(freqs, levels, 1.0 / 3.0)
    ref = _power_mean_reference(freqs, levels, 3.0)
    for g, r in zip(got, ref):
        assert g == pytest.approx(r, abs=1e-9)


def test_fractional_octave_power_mean_exceeds_flat_and_geometric():
    # Power mean is convex in the dB domain: for a linear-in-log2f ramp the
    # smoothed value at a window center is >= the ramp value (Jensen on
    # 10^(L/10)), with equality only for a flat trace.
    freqs = tuple(2.0 ** (k / 48.0) for k in range(48 * 5, 48 * 8))
    ramp = tuple(-60.0 + 6.0 * math.log2(f / 500.0) for f in freqs)
    dataset = _make_dataset(freqs, ramp)
    smoothed = smoothed_level_trace(dataset, 3)
    mid = len(freqs) // 2
    assert smoothed.level_db[mid] > ramp[mid]
    flat = _make_dataset(freqs, tuple(-75.0 for _ in freqs))
    flat_out = smoothed_level_trace(flat, 3)
    for v in flat_out.level_db:
        assert v == pytest.approx(-75.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Dimension 5 — IACC and lateral metrics vs textbook formulas.
# ---------------------------------------------------------------------------


def _iacc_reference(left, right, sample_rate, start_s, end_s, max_lag_s):
    """IACC per the textbook definition via scipy.signal.correlate.

    IACF(tau) = sum_t l(t+tau) r(t) / sqrt(E_l E_r), bounded to
    |tau| <= max_lag; IACC is max |IACF|. ``correlate(l, r, 'full')``
    computes exactly sum_t l(t) r(t-tau) = IACF(tau) with zero padding,
    which reduces to the production code's shifted inner products.
    """
    i0 = int(round(start_s * sample_rate))
    i1 = min(len(left), int(round(end_s * sample_rate)))
    l = np.asarray(left[i0:i1], dtype=np.float64)
    r = np.asarray(right[i0:i1], dtype=np.float64)
    norm = math.sqrt(float(np.sum(l**2) * np.sum(r**2)))
    max_lag = int(round(max_lag_s * sample_rate))
    corr = scipy.signal.correlate(l, r, mode="full")
    lags = scipy.signal.correlation_lags(l.size, r.size, mode="full")
    inside = np.abs(corr[np.abs(lags) <= max_lag] / norm)
    return float(np.max(inside))


def _spatial_spec(metric, *, ears=None, channels=None, **overrides):
    kwargs = dict(
        acoustic_scene_snapshot_id="snapshot-1",
        acoustic_scene_snapshot_sha256=_hash("snapshot"),
        source_scenario_id="scenario-1",
        source_scenario_sha256=_hash("scenario"),
        metric=metric,
        time_window_start_s=0.0,
        time_window_end_s=0.08,
        window_semantics="early",
        head_orientation_deg=0.0,
    )
    kwargs.update(overrides)
    if ears:
        kwargs["left_ear"], kwargs["right_ear"] = ears
    if channels:
        kwargs.update(channels)
    return build_spatial_ir_metric_spec(**kwargs)


def _ear_ref(ear, samples):
    return ImpulseEarEvidenceRef(
        kind="measured",
        artifact_id=f"ir:{ear}",
        artifact_sha256=_hash(f"ir-{ear}"),
        decoded_pcm_sha256=_pcm_hash(samples),
        sample_rate_hz=FS,
        sample_count=len(samples),
        ear=ear,
        receiver_id="seat-1",
        receiver_entity_id="entity-1",
        absolute_amplitude_authority=True,
    )


def _channel_ref(role, samples, dataset="ds-spatial"):
    return DirectionalIrChannelRef(
        kind="measured",
        artifact_id=f"ir:{role}",
        artifact_sha256=_hash(f"ir-{role}"),
        decoded_pcm_sha256=_pcm_hash(samples),
        sample_rate_hz=FS,
        sample_count=len(samples),
        dataset_id=dataset,
        dataset_sha256=_hash(dataset),
        channel_role=role,
        receiver_id="seat-1",
        receiver_entity_id="entity-1",
        absolute_amplitude_authority=True,
    )


def test_iacc_matches_textbook_normalized_correlation():
    rng = np.random.default_rng(29)
    n = 4800
    left = rng.standard_normal(n)
    right = np.roll(left, 7) * 0.9 + rng.standard_normal(n) * 0.15
    spec = _spatial_spec(
        "iacc",
        ears=(_ear_ref("left", left), _ear_ref("right", right)),
        time_window_start_s=0.0,
        time_window_end_s=0.08,
    )
    result = evaluate_spatial_ir_metric(
        spec, left_ir=tuple(left), right_ir=tuple(right)
    )
    assert result.state == "computed"
    ref = _iacc_reference(
        left, right, int(FS), 0.0, 0.08, spec.iacc_max_lag_s
    )
    assert result.value == pytest.approx(ref, abs=1e-12)
    # The 7-sample shift is inside the +-1 ms search: peak is large.
    assert abs(result.value) > 0.8


def test_iacc_respects_bounded_lag_window():
    # A pure delay OUTSIDE +-1 ms must not be recovered as full correlation.
    rng = np.random.default_rng(31)
    n = 4800
    left = rng.standard_normal(n)
    shift = int(0.003 * FS)  # 3 ms > 1 ms bound
    right = np.roll(left, shift)
    spec = _spatial_spec(
        "iacc",
        ears=(_ear_ref("left", left), _ear_ref("right", right)),
        time_window_start_s=0.0,
        time_window_end_s=0.08,
    )
    result = evaluate_spatial_ir_metric(
        spec, left_ir=tuple(left), right_ir=tuple(right)
    )
    assert result.state == "computed"
    # Overlap of a shifted random signal over 3.84k samples is ~sqrt-free
    # random; bound it well below the true-lag value.
    assert abs(result.value) < 0.6


def test_lateral_metrics_match_iso3382_definitions():
    rng = np.random.default_rng(37)
    n = int(0.2 * FS)
    omni = rng.standard_normal(n)
    lateral = rng.standard_normal(n)
    lat = _channel_ref("figure8_lateral", lateral)
    om = _channel_ref("omnidirectional", omni)
    start_s, end_s = 0.02, 0.08
    spec = _spatial_spec(
        "lateral_fraction",
        channels={"lateral_channel": lat, "omni_channel": om},
        time_window_start_s=start_s,
        time_window_end_s=end_s,
    )
    result = evaluate_spatial_ir_metric(
        spec, lateral_ir=tuple(lateral), omni_ir=tuple(omni)
    )
    i0 = int(round(start_s * FS))
    i1 = int(round(end_s * FS))
    ref = float(np.sum(lateral[i0:i1] ** 2)) / float(np.sum(omni[:i1] ** 2))
    assert result.state == "computed"
    assert result.value == pytest.approx(ref, rel=1e-12)

    spec_c = _spatial_spec(
        "lateral_fraction_cosine",
        channels={"lateral_channel": lat, "omni_channel": om},
        time_window_start_s=start_s,
        time_window_end_s=end_s,
    )
    result_c = evaluate_spatial_ir_metric(
        spec_c, lateral_ir=tuple(lateral), omni_ir=tuple(omni)
    )
    ref_c = float(np.sum(np.abs(lateral[i0:i1] * omni[i0:i1]))) / float(
        np.sum(omni[:i1] ** 2)
    )
    assert result_c.state == "computed"
    assert result_c.value == pytest.approx(ref_c, rel=1e-12)


def test_late_lateral_level_is_energy_db_vs_full_reference():
    rng = np.random.default_rng(41)
    n = int(0.3 * FS)
    lateral = rng.standard_normal(n) * 0.2
    reference = rng.standard_normal(n)
    lat = _channel_ref("figure8_lateral", lateral)
    ref_ch = _channel_ref(
        "free_field_omni_reference", reference, dataset="ds-ref"
    )
    start_s, end_s = 0.1, 0.25
    spec = _spatial_spec(
        "listener_envelopment",
        channels={"lateral_channel": lat, "reference_channel": ref_ch},
        time_window_start_s=start_s,
        time_window_end_s=end_s,
    )
    result = evaluate_spatial_ir_metric(
        spec, lateral_ir=tuple(lateral), reference_ir=tuple(reference)
    )
    i0 = int(round(start_s * FS))
    i1 = int(round(end_s * FS))
    ref_db = 10.0 * log10(
        float(np.sum(lateral[i0:i1] ** 2)) / float(np.sum(reference**2))
    )
    assert result.state == "computed"
    assert result.value_unit == "decibel"
    assert result.value == pytest.approx(ref_db, abs=1e-9)


# ---------------------------------------------------------------------------
# Dimension 6 — SPL sums and the 10-vs-20 log10 conventions.
# ---------------------------------------------------------------------------


def test_energy_sums_and_free_field_distance_law():
    # Two equal 80 dB sources sum to 83.0103 dB (energy sum, +3.01 dB).
    total = combine_noise_sources_energy((80.0, 80.0))
    assert total == pytest.approx(80.0 + 10.0 * log10(2.0), abs=1e-9)
    # A -10 dB-down second source contributes 0.414 dB.
    total2 = combine_noise_sources_energy((80.0, 70.0))
    assert total2 == pytest.approx(
        10.0 * log10(10.0**8.0 + 10.0**7.0), abs=1e-9
    )
    # Free-field distance law: doubling distance drops 6.02 dB (pressure).
    assert predict_free_field_listener_level(
        source_level_db=90.0,
        reference_distance_m=1.0,
        listener_distance_m=2.0,
    ) == pytest.approx(90.0 - 20.0 * log10(2.0), abs=1e-9)


def test_spl_reference_is_20uPa_amplitude_convention():
    # 1 Pa -> 94 dB SPL; 2 Pa -> 100 dB SPL (amplitude: 20*log10).
    sample = HybridAbsolutePressureSample(
        frequency_hz=1000.0,
        complex_real_pa=2.0,
        complex_imag_pa=0.0,
        magnitude_pa=2.0,
        magnitude_db_spl=100.0,
        phase_deg=0.0,
    )
    assert sample.magnitude_db_spl == pytest.approx(
        20.0 * log10(2.0 / SPL_REFERENCE_PA), abs=1e-9
    )
    with pytest.raises(ValueError, match="dB SPL conversion mismatch"):
        HybridAbsolutePressureSample(
            frequency_hz=1000.0,
            complex_real_pa=2.0,
            complex_imag_pa=0.0,
            magnitude_pa=2.0,
            magnitude_db_spl=10.0 * log10(2.0 / SPL_REFERENCE_PA),
            phase_deg=0.0,
        )


def test_clarity_metrics_use_energy_ratio_not_amplitude():
    # early = 4x late energy -> C50 = +6.02 dB (10*log10), not +12.04.
    t0 = 0
    split = int(round(0.05 * FS))
    n = int(0.3 * FS)
    h = np.zeros(n)
    h[t0] = 1.0
    # Four equal-energy early spikes vs one equal-energy late spike.
    h[t0 + 10] += 0.5
    h[t0 + 20] += 0.5
    h[t0 + 30] += 0.5
    # early energy = 1 + 3*0.25 = 1.75; add one late spike of the same
    # total: late spike amplitude sqrt(1.75/4) x ... simpler: build exact.
    h = np.zeros(n)
    h[t0] = 1.0
    h[t0 + 100] = 1.0
    h[t0 + 200] = 1.0
    h[t0 + 300] = 1.0  # early energy = 4
    h[t0 + split + 10] = 1.0  # late energy = 1 -> ratio 4 -> +6.02 dB
    spec = _ir_spec(time_zero_sample=t0, noise_floor_method="none")
    result = run_ir_analysis(
        spec, tuple(float(v) for v in h), created_at="2026-09-29T00:00:00+00:00"
    )
    c50 = next(m for m in result.energy_metrics if m.metric == "c50")
    assert c50.status == "estimated"
    assert c50.value == pytest.approx(10.0 * log10(4.0), abs=1e-9)


# ---------------------------------------------------------------------------
# Dimension 7 — unit conversions inside solver plumbing.
# ---------------------------------------------------------------------------


def test_fir_transfer_matches_direct_dft_and_group_delay_seconds():
    taps = (0.1, 0.2, 0.5, 0.2, 0.1)
    artifact = build_fir_filter_artifact(
        filter_class="linear_phase",
        sample_rate_hz=48000.0,
        taps=taps,
        tap_format="float64",
        channel_id="FL",
        time_reference_sample=2,
        latency_s=0.0,
        source_producer="test",
        source_version="1",
    )
    # Sparse grid spanning > pi of phase between points — the regression
    # case for grid finite-difference group delay: the analytic
    # -Im(H'/H) derivative is exact at every evaluation frequency.
    freqs = (100.0, 431.5, 1000.0, 24000.0)
    diag = evaluate_fir_artifact(artifact, freqs)
    # Independent DFT: H(f) = sum_k taps[k] * exp(-j 2pi f k / fs).
    for i, f in enumerate(freqs):
        k = np.arange(len(taps))
        expected = np.sum(
            np.asarray(taps) * np.exp(-2j * pi * f * k / 48000.0)
        )
        got_db = diag.magnitude_db[i]
        assert got_db == pytest.approx(20.0 * log10(abs(expected)), rel=1e-9)
        # Symmetric FIR: group delay is exactly (N-1)/2 samples in
        # seconds at every frequency, including Nyquist.
        assert diag.group_delay_s[i] == pytest.approx(
            (len(taps) - 1) / 2.0 / 48000.0, rel=1e-6
        )


def test_finite_record_transfer_matches_dtft_kernel_definition():
    rng = np.random.default_rng(43)
    n = 512
    dt = 1.0 / 48000.0
    h = rng.standard_normal(n) * np.exp(-np.arange(n) / 100.0)
    source = np.zeros(n)
    source[0] = 1.0  # unit volume-velocity impulse at sample 0
    freqs = np.array([100.0, 400.0, 1000.0])
    transfer = finite_record_pressure_transfer(
        h, source, time_step_s=dt, frequency_hz=freqs
    )
    # Reference: P/Q with the declared exp(+i 2pi f n dt) analysis kernel,
    # dt-weighted on both sides.
    times = np.arange(n) * dt
    kernel = np.exp(+2j * np.pi * freqs[:, None] * times[None, :])
    ref = (dt * (kernel @ h)) / (dt * (kernel @ source))
    np.testing.assert_allclose(transfer, ref, rtol=1e-12, atol=1e-18)
    # And the value is the conjugate of the ordinary DFT of h on this grid.
    std = np.array(
        [np.sum(h * np.exp(-2j * np.pi * f * np.arange(n) * dt)) for f in freqs]
    )
    np.testing.assert_allclose(transfer, np.conj(std), rtol=1e-12)


def test_biquad_peaking_gain_and_butterworth_corner():
    # RBJ peaking EQ: gain at f0 equals the declared gain_db.
    peaking = build_biquad_filter(
        filter_id="pk",
        filter_type="peaking",
        frequency_hz=1000.0,
        q=2.0,
        gain_db=6.0,
        sample_rate_hz=48000,
    )
    assert evaluate_biquad_db(peaking, 1000.0) == pytest.approx(6.0, abs=1e-9)
    # RBJ low-pass with Q = 1/sqrt(2) (Butterworth): -3.01 dB exactly at fc.
    lp = build_biquad_filter(
        filter_id="lp",
        filter_type="low_pass",
        frequency_hz=200.0,
        q=1.0 / math.sqrt(2.0),
        gain_db=0.0,
        sample_rate_hz=48000,
    )
    assert evaluate_biquad_db(lp, 200.0) == pytest.approx(
        -3.010299956639812, abs=1e-6
    )
    # And DC gain is unity (0 dB) for the LP — catches any b0/(1+a0) slip.
    assert evaluate_biquad_db(lp, 1.0) == pytest.approx(0.0, abs=1e-3)


def test_causal_boundary_admittance_matches_parallel_series_rlc():
    authority = build_causal_frequency_dependent_boundary_authority(
        source_scene_revision_id="scene-rev",
        source_scene_content_hash="a" * 64,
        source_surface_id="semantic-surface:" + "b" * 64,
        material_id="m",
        material_version="1",
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=40.0, maximum_hz=80.0
        ),
        branches=(
            CausalAdmittanceBranch(
                d_seconds=8.0e-4, e_dimensionless=1.5, f_per_second=120.0
            ),
            CausalAdmittanceBranch(
                d_seconds=1.0e-5, e_dimensionless=0.7, f_per_second=0.0
            ),
        ),
        evidence_state="analytic",
        provenance={"basis": "closed_form", "fixture": "rev18"},
        uncertainty=None,
    )
    freqs = (40.0, 55.5, 80.0)
    got = evaluate_normalized_admittance(authority, freqs)
    for f, value in zip(freqs, got):
        jw = 1j * 2.0 * pi * f
        ref = 0j
        for branch in authority.branches:
            z = (
                jw * float(branch.d_seconds)
                + float(branch.e_dimensionless)
                + float(branch.f_per_second) / jw
            )
            ref += 1.0 / z
        assert value == pytest.approx(ref, rel=1e-12)


def test_decay_rate_neper_t60_convention_round_trip():
    # T60 = 0.5 s -> decay rate ln(10^3)/0.5 nepers/s (amplitude).
    mode = MeasuredMode(
        mode_id="m1",
        center_frequency_hz=80.0,
        decay_rate_nepers_per_s=6.907755278982137 / 0.5,
        decay_time_s=0.5,
    )
    assert mode.decay_rate_nepers_per_s == pytest.approx(13.815510557964274)
    # A rate consistent with a DIFFERENT convention (ln(10^6): energy) must
    # be rejected — the declared convention is amplitude-decay T60.
    with pytest.raises(ValueError, match="T60 convention"):
        MeasuredMode(
            mode_id="m2",
            center_frequency_hz=80.0,
            decay_rate_nepers_per_s=13.815510557964274 / 0.5,
            decay_time_s=0.5,
        )


def test_decomposition_recovers_known_incident_reflected_phasors():
    frequencies = (100.0, 250.0, 500.0)
    d1, d2 = 0.1, 0.3
    incident = tuple(1.7 * cexp(0.31j * i) for i in range(3))
    reflected = tuple(0.4 * cexp(-0.7j * i) for i in range(3))
    samples = []
    for f, inc, refl in zip(frequencies, incident, reflected, strict=True):
        k = 2.0 * pi * f / C_M_S
        p1 = inc * cexp(-1j * k * d1) + refl * cexp(+1j * k * d1)
        p2 = inc * cexp(-1j * k * d2) + refl * cexp(+1j * k * d2)
        samples.append(
            TwoPointFrequencyPressure(
                frequency_hz=f,
                sample_1_pressure=ComplexPressureValue(
                    real=p1.real, imag=p1.imag
                ),
                sample_2_pressure=ComplexPressureValue(
                    real=p2.real, imag=p2.imag
                ),
            )
        )
    evidence = build_two_point_complex_pressure_evidence(
        sample_ids=("p-near", "p-far"),
        frequencies=tuple(samples),
    )
    raw_ref = ExactDecompositionAuthorityRef(
        authority_kind="raw_complex_pressure_evidence",
        authority_id=evidence.evidence_id,
        authority_version=evidence.authority_version,
        semantic_sha256=evidence.semantic_sha256,
    )

    def _ref(kind, identity):
        digest = sha256(f"{kind}:{identity}".encode("utf-8")).hexdigest()
        return ExactDecompositionAuthorityRef(
            authority_kind=kind,
            authority_id=f"{kind}:{identity}",
            authority_version="1",
            semantic_sha256=digest,
        )

    spec = build_spatial_field_decomposition_spec(
        fixture_authority_ref=_ref("analytic_fixture", "two-point-v1"),
        prediction_authority_ref=None,
        raw_complex_pressure_evidence_ref=raw_ref,
        timing_fourier_authority_ref=_ref(
            "timing_fourier", "exp(-i*omega*t)/exp(+i*omega*t)"
        ),
        normalization_authority_ref=_ref(
            "pressure_normalization", "analytic-pa-v1"
        ),
        sound_speed_authority_ref=_ref("sound_speed", "343mps-v1"),
        pressure_evidence_source="analytic_synthetic",
        solver_backend_provenance_ref=None,
        boundary_plane_point=Position3(x_m=0.0, y_m=0.0, z_m=0.0),
        surface_outward_normal=Direction3(x=-1.0, y=0.0, z=0.0),
        sample_points=(
            SpatialPressureSamplePoint(
                sample_id="p-near",
                position=Position3(x_m=d1, y_m=0.0, z_m=0.0),
                inward_distance_m=d1,
            ),
            SpatialPressureSamplePoint(
                sample_id="p-far",
                position=Position3(x_m=d2, y_m=0.0, z_m=0.0),
                inward_distance_m=d2,
            ),
        ),
        frequency_grid_hz=frequencies,
        sound_speed_m_s=C_M_S,
        pressure_evidence_phasor_convention="exp(-i*omega*t)",
        pressure_analysis_kernel="exp(+i*omega*t)",
        complex_pressure_normalization="Pa, analytic synthetic amplitude",
    )
    result = decompose_planar_normal_incidence_two_point(
        spec=spec, pressure_evidence=evidence
    )
    assert result.overall_status == "AVAILABLE"
    for item, inc, refl in zip(
        result.frequency_results, incident, reflected, strict=True
    ):
        got_i = item.incident_pressure.as_complex()
        got_r = item.reflected_pressure.as_complex()
        assert abs(got_i - inc) < 1e-9
        assert abs(got_r - refl) < 1e-9
        # Condition number vs numpy.linalg.cond (2-norm).
        k = 2.0 * pi * item.frequency_hz / C_M_S
        matrix = np.array(
            [
                [cexp(-1j * k * d1), cexp(+1j * k * d1)],
                [cexp(-1j * k * d2), cexp(+1j * k * d2)],
            ]
        )
        ref_cond = float(np.linalg.cond(matrix))
        assert item.conditioning.condition_number_2 == pytest.approx(
            ref_cond, rel=1e-9
        )


# ---------------------------------------------------------------------------
# Comparison grid / hybrid blend / multi-seat statistics.
# ---------------------------------------------------------------------------


def test_comparison_grid_is_octave_aligned_and_interpolation_log2():
    grid = _grid(20.0, 20000.0)
    # Grid is f_k = 2^(k/96); powers of two inside [20, 20000] must appear.
    for power in (32.0, 64.0, 128.0, 256.0, 512.0, 1024.0, 2048.0,
                  4096.0, 8192.0, 16384.0):
        assert power in grid
    # Linear-in-log2 interpolation: at the geometric mean of two points the
    # interpolated level is the arithmetic mean of the endpoint levels.
    response = FrequencyResponse(
        frequency_hz=(100.0, 200.0, 400.0),
        level_db=(0.0, 6.0, 12.0),
    )
    midpoint = math.sqrt(100.0 * 200.0)
    assert _interpolate(response, midpoint) == pytest.approx(3.0, abs=1e-12)


def test_compare_responses_linear_in_log2_difference_metrics():
    # Responses linear in log2(f): interpolation is exact, so all
    # differences equal the constant offset, RMS is |offset|, shape RMS 0.
    freqs = tuple(2.0 ** (k / 24.0) for k in range(24 * 4, 24 * 9))
    a = FrequencyResponse(freqs, tuple(-60.0 + 5.0 * math.log2(f) for f in freqs))
    b = FrequencyResponse(freqs, tuple(-62.5 + 5.0 * math.log2(f) for f in freqs))
    result = compare_frequency_responses(
        a, b, 100.0, 8000.0, reference_band_hz=(200.0, 4000.0)
    )
    assert result.mean_difference_db == pytest.approx(2.5, abs=1e-9)
    assert result.rms_difference_db == pytest.approx(2.5, abs=1e-9)
    assert result.level_offset_db == pytest.approx(2.5, abs=1e-9)
    assert result.shape_rms_db == pytest.approx(0.0, abs=1e-9)


def test_hybrid_weights_are_complementary_and_linear_in_hz():
    # Declared law 'linear_frequency_complementary_v1': linear in Hz.
    lo, hi = _weights(120.0, start_hz=100.0, end_hz=200.0)
    assert lo == pytest.approx(0.8, abs=1e-12)
    assert hi == pytest.approx(0.2, abs=1e-12)
    assert lo + hi == pytest.approx(1.0)
    assert _weights(50.0, start_hz=100.0, end_hz=200.0) == (1.0, 0.0)
    assert _weights(250.0, start_hz=100.0, end_hz=200.0) == (0.0, 1.0)


def test_multi_seat_statistics_match_column_recompute():
    grid_freqs = tuple(2.0 ** (k / 12.0) for k in range(12 * 4, 12 * 7))
    member_levels = [
        tuple(-70.0 + i + 2.0 * math.log2(f) for f in grid_freqs)
        for i in range(3)
    ]
    members = tuple(
        MultiSeatMember(
            measurement_id=f"m{i}",
            dataset_id=f"d{i}",
            dataset_sha256=_hash(f"ds{i}"),
            target_entity_id=f"t{i}",
            seat_label=f"seat{i}",
            scene_revision_id="rev-1",
            scene_content_hash=_hash("scene"),
        )
        for i in range(3)
    )
    analysis_set = build_multi_seat_set(
        members,
        document_id="doc-1",
        created_at="2026-09-29T00:00:00+00:00",
    )
    responses = tuple(
        FrequencyResponse(grid_freqs, member_levels[i]) for i in range(3)
    )
    result = run_multi_seat_analysis(
        analysis_set,
        responses,
        low_hz=100.0,
        high_hz=4000.0,
        central_tendency="arithmetic_mean_in_db",
        created_at="2026-09-29T00:00:00+00:00",
    )
    # Members are interpolated onto the result's 96-PPO grid; recompute
    # that interpolation independently (linear in log2(f)) before the
    # column statistics.
    def log2_interp(x_src, y_src, x_dst):
        xs = np.log2(np.asarray(x_src, dtype=np.float64))
        xd = np.log2(np.asarray(x_dst, dtype=np.float64))
        return np.interp(xd, xs, np.asarray(y_src, dtype=np.float64))

    rows = np.asarray(
        [
            log2_interp(grid_freqs, member_levels[i], result.grid_hz)
            for i in range(3)
        ]
    )
    np.testing.assert_allclose(
        np.asarray(result.min_db), np.min(rows, axis=0), atol=1e-12
    )
    np.testing.assert_allclose(
        np.asarray(result.max_db), np.max(rows, axis=0), atol=1e-12
    )
    np.testing.assert_allclose(
        np.asarray(result.mean_db), np.mean(rows, axis=0), atol=1e-12
    )
    np.testing.assert_allclose(
        np.asarray(result.spread_db),
        np.max(rows, axis=0) - np.min(rows, axis=0),
        atol=1e-12,
    )
