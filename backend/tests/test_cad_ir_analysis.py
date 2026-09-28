from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import numpy as np
import pytest

from htdt.cad_ir_analysis import (
    IR_ANALYSIS_ALGORITHM_SHA256,
    CadIRAnalysisRepository,
    IRAnalysisSpec,
    build_ir_analysis_spec,
    energy_metric_observation,
    replay_ir_analysis,
    run_ir_analysis,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene


FS = 48000.0


def _ir(tau_s: float = 0.08, length_s: float = 0.8, noise_level: float = 0.0, seed: int = 7):
    rng = np.random.default_rng(seed)
    n = int(length_s * FS)
    t = np.arange(n) / FS
    h = rng.standard_normal(n) * np.exp(-t / tau_s)
    h[0] = 1.0
    if noise_level:
        h += rng.standard_normal(n) * noise_level
    return tuple(float(v) for v in h)


def _spec(measurement_id='meas-1', **overrides):
    kwargs: dict = {
        'measurement_id': measurement_id,
        'dataset_id': f'ir-{measurement_id}',
        'dataset_sha256': sha256(b'ir-bytes').hexdigest(),
        'sample_rate_hz': FS,
        'tf_window_s': 0.05,
        'tf_overlap': 0.5,
    }
    kwargs.update(overrides)
    return build_ir_analysis_spec(**kwargs)


def test_spec_identity_rejects_tampering():
    spec = _spec()
    payload = spec.model_dump(mode='python')
    payload['tf_window_s'] = 0.1
    with pytest.raises(Exception, match='spec hash mismatch'):
        IRAnalysisSpec(**payload)


def test_etc_and_markers_are_derived():
    ir = _ir(tau_s=0.03, length_s=0.4)
    result = run_ir_analysis(
        _spec(), ir, created_at='2026-09-23T00:00:00+00:00'
    )
    assert len(result.etc_db) == len(result.etc_time_s) == len(ir)
    assert max(result.etc_db) == pytest.approx(0.0)
    # Markers carry relative delay only — never absolute arrival claims.
    for marker in result.markers:
        assert marker.delay_s > 0
    assert result.effective_alignment == 'none'


def test_markers_delay_anchored_to_time_zero():
    # Direct arrival sits at the declared time_zero_sample, not at the
    # window start: a reflection 3 ms after t0 must report delay_s ~= 3 ms
    # (never inflated by the pre-t0 offset), and pre-t0 energy is never
    # reported as a reflection marker.
    t0 = int(0.010 * FS)
    refl = t0 + int(0.003 * FS)
    n = int(0.2 * FS)
    h = np.zeros(n)
    h[100] = 0.8   # pre-t0 transient: inside the window, before the direct
    h[t0] = 1.0    # direct arrival at the declared time zero
    h[refl] = 0.5  # reflection 3 ms after the direct arrival
    spec = _spec(time_zero_sample=t0)
    result = run_ir_analysis(
        spec, tuple(float(v) for v in h), created_at='2026-09-23T00:00:00+00:00'
    )
    assert result.markers
    assert all(m.delay_s > 0.0 for m in result.markers)
    # No marker precedes the direct arrival at the declared time-zero.
    assert all(m.time_s * FS > t0 for m in result.markers)
    # The 0.5-amplitude peak 3 ms after t0 reads ~3 ms — never ~13 ms.
    reflection = min(result.markers, key=lambda m: abs(m.delay_s - 0.003))
    assert reflection.delay_s == pytest.approx(0.003, abs=1.5 / FS)
    assert reflection.time_s == pytest.approx(refl / FS, abs=1.5 / FS)
    assert reflection.level_db == pytest.approx(-6.0, abs=2.0)


def test_decay_metrics_estimate_on_clean_ir():
    ir = _ir(tau_s=0.05, length_s=1.0)
    result = run_ir_analysis(_spec(), ir, created_at='2026-09-23T00:00:00+00:00')
    metrics = {m.metric: m for m in result.metrics}
    assert metrics['edt'].status == 'estimated'
    # Amplitude decay e^{-t/tau} gives a -8.686/tau dB/s energy slope.
    assert metrics['edt'].value_s == pytest.approx(60.0 * 0.05 / 8.686, rel=0.1)
    assert metrics['t20'].status in ('estimated', 'unknown', 'blocked')
    assert metrics['edt'].fit_interval_db == (0.0, -10.0)


def test_noisy_tail_blocks_deep_metrics_not_edt():
    # High noise floor limits usable range below the T30 interval.
    ir = _ir(tau_s=0.05, length_s=0.8, noise_level=0.02)
    result = run_ir_analysis(_spec(), ir, created_at='2026-09-23T00:00:00+00:00')
    metrics = {m.metric: m for m in result.metrics}
    assert metrics['t30'].status in ('blocked', 'unknown')
    assert metrics['t30'].value_s is None
    assert metrics['t30'].usable_dynamic_range_db is not None


def test_blocked_capability_keeps_metrics_blocked():
    ir = _ir(tau_s=0.05, length_s=1.0)
    result = run_ir_analysis(
        _spec(),
        ir,
        capabilities={'decay': 'BLOCKED'},
        created_at='2026-09-23T00:00:00+00:00',
    )
    assert all(m.status == 'blocked' for m in result.metrics)


def test_absolute_common_time_requires_capability():
    ir = _ir(length_s=0.3)
    spec = _spec(alignment='absolute_common_time')
    result = run_ir_analysis(
        spec, ir, created_at='2026-09-23T00:00:00+00:00'
    )
    assert result.effective_alignment == 'relative_time'
    assert any('common-timing' in w for w in result.warnings)
    allowed = run_ir_analysis(
        spec,
        ir,
        capabilities={'common_timing': 'ALLOWED'},
        created_at='2026-09-23T00:00:00+00:00',
    )
    assert allowed.effective_alignment == 'absolute_common_time'


def test_truncation_is_recorded():
    ir = _ir(tau_s=0.2, length_s=1.0)
    spec = _spec(window_start_s=0.0, window_end_s=0.05)
    result = run_ir_analysis(spec, ir, created_at='2026-09-23T00:00:00+00:00')
    assert result.truncated is True
    assert any('truncat' in w for w in result.warnings)


def test_band_filtered_analysis():
    ir = _ir(tau_s=0.1, length_s=0.8)
    spec = _spec(band_center_hz=500.0, band_fraction='octave')
    result = run_ir_analysis(spec, ir, created_at='2026-09-23T00:00:00+00:00')
    assert result.decay_db and len(result.decay_db) == len(result.decay_time_s)


def test_empty_analysis_window_fails_closed():
    # window bounds that pass the spec validator can still round to an
    # empty sample range at the given rate — that must fail with a clear
    # ValueError, not a raw numpy FFT error on a zero-length frame.
    spec = _spec(window_start_s=1.5 / FS, window_end_s=2.4 / FS)
    with pytest.raises(ValueError, match='no IR evidence'):
        run_ir_analysis(spec, _ir(), created_at='2026-09-23T00:00:00+00:00')


def test_silent_ir_fails_closed():
    # Zero-energy evidence makes the ETC/Schroeder normalizations divide
    # by a zero peak/total; the sealed result must be finite with unknown
    # metrics, not crash canonical JSON on NaN.
    spec = _spec()
    ir = tuple(0.0 for _ in range(4800))
    result = run_ir_analysis(spec, ir, created_at='2026-09-23T00:00:00+00:00')
    assert all(v == -120.0 for v in result.etc_db)
    assert all(v == -140.0 for v in result.decay_db)
    assert result.usable_dynamic_range_db is None
    assert all(m.status == 'unknown' for m in result.metrics)
    assert all(m.status == 'unknown' for m in result.energy_metrics)
    assert 'silent' in ' '.join(result.warnings).lower()


def test_nonfinite_ir_fails_closed():
    spec = _spec()
    ir = (0.5, -0.5, float('inf'), 0.25, 0.0, 0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match='finite'):
        run_ir_analysis(spec, ir, created_at='2026-09-23T00:00:00+00:00')


def test_spectrogram_records_transform_settings():
    ir = _ir(length_s=0.4)
    spec = _spec(tf_window_s=0.02, tf_overlap=0.5)
    result = run_ir_analysis(spec, ir, created_at='2026-09-23T00:00:00+00:00')
    assert result.spec_times_s
    assert result.spec_freqs_hz
    assert all(len(row) == len(result.spec_freqs_hz) for row in result.spec_levels_db)


def test_spectrogram_levels_are_amplitude_db():
    # A tone at 1/100 the reference amplitude must read 20*log10(0.01) =
    # -40 dB, not the -20 dB a power-style 10*log10 produces.
    n = int(0.4 * FS)
    t = np.arange(n) / FS
    # tf_window_s=0.05 -> window_n=2400 -> 20 Hz bins; 200/600 Hz land on bins.
    ir = np.sin(2 * np.pi * 200.0 * t) + 0.01 * np.sin(2 * np.pi * 600.0 * t)
    spec = _spec(tf_window_s=0.05, tf_overlap=0.5)
    result = run_ir_analysis(
        spec, tuple(float(v) for v in ir), created_at='2026-09-23T00:00:00+00:00'
    )
    bin200 = int(round(200.0 / (FS / 2400)))
    bin600 = int(round(600.0 / (FS / 2400)))
    assert result.spec_freqs_hz[bin200] == pytest.approx(200.0)
    assert result.spec_freqs_hz[bin600] == pytest.approx(600.0)
    for row in result.spec_levels_db:
        assert row[bin200] == pytest.approx(0.0, abs=0.5)
        assert row[bin600] == pytest.approx(-40.0, abs=0.5)


def test_replay_and_repository(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_f1_scene(), parent_revision_id=None)
    repository = CadIRAnalysisRepository(scene_repository)
    spec = _spec()
    repository.save_spec(spec)
    ir = _ir(tau_s=0.05, length_s=0.8)
    result = run_ir_analysis(spec, ir, created_at='2026-09-23T00:00:00+00:00')
    repository.save_result(result)
    assert repository.get_spec(spec.spec_id) == spec
    assert repository.get_result(result.result_id) == result
    replay_ir_analysis(result, spec, ir)
    with pytest.raises(ValueError, match='different spec'):
        replay_ir_analysis(result, _spec(measurement_id='other'), ir)


def test_algorithm_identity_is_versioned():
    assert len(IR_ANALYSIS_ALGORITHM_SHA256) == 64


def test_clarity_metrics_anchored_to_time_zero():
    # Synthetic IR: a strong direct arrival at t0 then exponentially
    # decaying energy — early/late ratios are deterministic.
    n = int(0.5 * FS)
    t = np.arange(n) / FS
    h = np.exp(-t / 0.05)
    t0 = int(0.02 * FS)
    h[:t0] = 0.0
    spec = _spec(time_zero_sample=t0)
    result = run_ir_analysis(
        spec, tuple(float(v) for v in h), created_at='2026-09-23T00:00:00+00:00'
    )
    energy = {m.metric: m for m in result.energy_metrics}
    assert energy['c50'].status == 'estimated'
    assert energy['c80'].status == 'estimated'
    assert energy['d50'].status == 'estimated'
    assert energy['early_energy'].status == 'estimated'
    assert energy['c50'].split_time_ms == 50.0
    # More early energy than late energy -> positive clarity.
    assert energy['c50'].value > 0.0
    assert 0.0 < energy['d50'].value < 1.0
    assert energy['early_energy'].early_energy > 0.0
    assert energy['early_energy'].late_energy > 0.0


def test_clarity_metrics_fail_closed_on_truncation():
    ir = _ir(tau_s=0.2, length_s=1.0)
    spec = _spec(window_start_s=0.0, window_end_s=0.05)
    result = run_ir_analysis(spec, ir, created_at='2026-09-23T00:00:00+00:00')
    assert result.truncated is True
    assert result.energy_metrics
    assert all(m.status == 'blocked' for m in result.energy_metrics)
    assert all('truncat' in m.reason for m in result.energy_metrics)


def test_clarity_metrics_blocked_capability():
    ir = _ir(tau_s=0.05, length_s=1.0)
    result = run_ir_analysis(
        _spec(),
        ir,
        capabilities={'clarity': 'BLOCKED'},
        created_at='2026-09-23T00:00:00+00:00',
    )
    assert result.energy_metrics
    assert all(m.status == 'blocked' for m in result.energy_metrics)


def test_clarity_metrics_fail_closed_on_time_zero_outside_window():
    ir = _ir(tau_s=0.05, length_s=1.0)
    spec = _spec(time_zero_sample=int(0.9 * FS), window_end_s=0.5)
    result = run_ir_analysis(spec, ir, created_at='2026-09-23T00:00:00+00:00')
    assert all(m.status == 'blocked' for m in result.energy_metrics)
    assert all('time-zero' in m.reason for m in result.energy_metrics)


def test_energy_metric_observation_adapter():
    n = int(0.5 * FS)
    t = np.arange(n) / FS
    h = np.exp(-t / 0.05)
    spec = _spec()
    result = run_ir_analysis(
        spec, tuple(float(v) for v in h), created_at='2026-09-23T00:00:00+00:00'
    )
    observation = energy_metric_observation(
        result,
        'c80',
        criterion_id='c80-main',
        band_id='125-250',
        entity_ids=('seat-1',),
    )
    assert observation.evidence_basis == 'measured'
    assert observation.provided_capability == 'measured_clarity_c80'
    assert observation.unit == 'dB'
    assert observation.entity_ids == ('seat-1',)
    assert any(
        ref.kind == 'ir_analysis_result'
        and ref.evidence_sha256 == result.analysis_sha256
        for ref in observation.evidence_refs
    )
    # A non-estimated metric never adapts — it stays UNKNOWN upstream.
    blocked = run_ir_analysis(
        _spec(window_end_s=0.05),
        _ir(tau_s=0.2, length_s=1.0),
        created_at='2026-09-23T00:00:00+00:00',
    )
    with pytest.raises(ValueError, match='not estimated'):
        energy_metric_observation(
            blocked, 'c80', criterion_id='c80-main', band_id='125-250'
        )
    with pytest.raises(ValueError, match='unknown IR energy metric kind'):
        energy_metric_observation(
            result, 'xyz', criterion_id='c80-main', band_id='125-250'
        )
