"""Round-13 number-truth regression tests.

Each test feeds a known synthetic input through the real pipeline and
asserts the pipeline value equals an independent recomputation done in
the test — the same quantity, computed a second way, not a snapshot of
whatever the code happens to emit today.
"""
from __future__ import annotations

import base64
import math
import struct
from hashlib import sha256

import numpy as np
import pytest

from htdt.cad_acoustic_target import (
    AcousticTargetBand,
    AcousticTargetCriterion,
    AcousticTargetObservation,
    build_acoustic_target_profile,
    evaluate_acoustic_targets,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_ir_analysis import build_ir_analysis_spec, run_ir_analysis
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_multi_seat_analysis import (
    MultiSeatMember,
    build_multi_seat_set,
    run_multi_seat_analysis,
)
from htdt.cad_spatial_ir_metrics import _iacc, _lateral_fraction
from htdt.comparison import (
    FrequencyResponse,
    _grid,
    _valid_grid,
    compare_frequency_responses,
)
from htdt.measurement_analysis import smoothed_level_trace
from htdt.optimization_objectives import (
    ResponseObjectiveSpec,
    movement_objectives,
    seat_pairwise_objectives,
    target_response_objectives,
)
from htdt.cad_standards import CriterionRule
from htdt.rew_api import decode_frequency_response

FS = 48000.0


def _spec(**overrides):
    kwargs = {
        'measurement_id': 'm1',
        'dataset_id': 'd1',
        'dataset_sha256': sha256(b'ir').hexdigest(),
        'sample_rate_hz': FS,
        'tf_window_s': 0.05,
        'tf_overlap': 0.5,
    }
    kwargs.update(overrides)
    return build_ir_analysis_spec(**kwargs)


# ------------------------------------------------------ builder canonical form
def test_spec_builder_accepts_int_for_float_fields():
    # Canonical JSON serializes int and float differently ('48000' vs
    # '48000.0'); the provisional identity hash must seal the validated
    # representation, or the spec can never be built from int literals.
    spec = _spec(sample_rate_hz=48000, window_start_s=0)
    assert spec.sample_rate_hz == 48000.0
    spec2 = _spec(clarity_split_times_ms=(50, 80), tf_window_s=1, tf_overlap=0)
    assert spec2.clarity_split_times_ms == (50.0, 80.0)
    spec3 = _spec(time_zero_sample=100.0)
    assert spec3.time_zero_sample == 100


def test_spec_builder_float_semantics_unchanged():
    a = _spec(sample_rate_hz=48000)
    b = _spec(sample_rate_hz=48000.0)
    # Same physical input must yield the same semantic identity
    # (spec_id differs — identity payload excludes it).
    assert a.identity_payload() == b.identity_payload()


# ---------------------------------------------------------------- IR analysis
def test_ir_decay_metrics_match_exponential_theory():
    # h(t) = exp(-t/tau) -> energy decays exp(-2t/tau) -> Schroeder curve
    # linear in dB, RT60 = 3*ln(10)*tau exactly.
    rt60_true = 0.4
    tau = rt60_true / (3.0 * math.log(10.0))
    n = int(FS * 1.0)
    t = np.arange(n) / FS
    ir = tuple(float(v) for v in np.exp(-t / tau))
    result = run_ir_analysis(_spec(), ir, created_at='2026-01-01T00:00:00')
    by_metric = {m.metric: m for m in result.metrics}
    for name in ('edt', 't20', 't30'):
        metric = by_metric[name]
        assert metric.status == 'estimated'
        assert metric.value_s == pytest.approx(rt60_true, rel=0.01)


def test_ir_energy_metrics_recomputed():
    # Impulses of amplitude 1.0@0ms, 0.5@60ms, 0.25@100ms.
    # energy: 1.0 / 0.25 / 0.0625. C50 splits [0,50ms) vs [50ms,end].
    ir = np.zeros(int(FS))
    ir[0] = 1.0
    ir[int(0.060 * FS)] = 0.5
    ir[int(0.100 * FS)] = 0.25
    result = run_ir_analysis(_spec(), tuple(ir), created_at='2026-01-01T00:00:00')
    em = {(m.metric, m.split_time_ms): m for m in result.energy_metrics}
    total = 1.0 + 0.25 + 0.0625
    assert em[('c50', 50.0)].value == pytest.approx(
        10.0 * math.log10(1.0 / (0.25 + 0.0625)), abs=1e-9)
    assert em[('d50', 50.0)].value == pytest.approx(1.0 / total, abs=1e-9)
    # C80's early window covers the 60 ms impulse too.
    assert em[('c80', 80.0)].value == pytest.approx(
        10.0 * math.log10((1.0 + 0.25) / 0.0625), abs=1e-9)
    assert em[('early_energy', 50.0)].value == pytest.approx(
        1.0 / total, abs=1e-9)
    assert em[('early_energy', 80.0)].value == pytest.approx(
        (1.0 + 0.25) / total, abs=1e-9)


# ------------------------------------------------------------- A/B comparison
def _curve(offset_fn):
    freqs = tuple(20.0 * 2.0 ** (i / 24.0) for i in range(200))
    levels = tuple(70.0 + 2.0 * math.sin(i * 0.3) + offset_fn(i)
                   for i in range(200))
    return FrequencyResponse(freqs, levels)


def test_comparison_metrics_recomputed():
    # a - b difference is LINEAR in log2(f): log2-domain interpolation is
    # exact, so every displayed number is hand-computable from the grid.
    a = _curve(lambda i: 0.0)
    b = _curve(lambda i: -(0.5 * math.log2(a.frequency_hz[i] / 100.0)))
    ref = (200.0, 2000.0)
    result = compare_frequency_responses(
        a, b, 30.0, 8000.0, reference_band_hz=ref)
    diffs = [x - y for x, y in zip(result.a_db, result.b_db)]
    assert result.valid_points == len(diffs)
    # Independent recompute on the same documented grid semantics.
    want_diffs = [0.5 * math.log2(f / 100.0) for f in result.grid_hz]
    assert diffs == pytest.approx(want_diffs, abs=1e-9)
    mean = sum(diffs) / len(diffs)
    rms = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    assert result.mean_difference_db == pytest.approx(mean, abs=1e-12)
    assert result.rms_difference_db == pytest.approx(rms, abs=1e-12)
    # Level offset = mean difference over the reference-band grid;
    # shape RMS is the evaluated-band RMS after removing that offset.
    ref_low = max(ref[0], a.frequency_hz[0], b.frequency_hz[0])
    ref_high = min(ref[1], a.frequency_hz[-1], b.frequency_hz[-1])
    ref_grid = _valid_grid(_grid(ref_low, ref_high), ())
    offset = sum(0.5 * math.log2(f / 100.0) for f in ref_grid) / len(ref_grid)
    assert result.level_offset_db == pytest.approx(offset, abs=1e-12)
    shape = math.sqrt(
        sum((d - offset) ** 2 for d in diffs) / len(diffs))
    assert result.shape_rms_db == pytest.approx(shape, abs=1e-12)


# -------------------------------------------------------------- smoothing
def test_smoothed_level_trace_power_mean():
    ds = CadFrequencyResponseDataset(
        dataset_id='d', measurement_id='m',
        frequency_hz=(100.0, 141.4213562373095, 200.0),
        level_db=(0.0, 10.0, 0.0),
        phase_deg=None, phase_status='absent',
        smoothing=None, level_reference='relative',
        processing_json='{}',
        source_sha256=sha256(b'ds').hexdigest(), importer_version='v1',
    )
    trace = smoothed_level_trace(ds, 12)  # ±1/24-oct window per sample
    # Only the center point falls inside its own window here.
    assert trace.level_db[1] == pytest.approx(10.0, abs=1e-9)
    assert trace.level_db[0] == pytest.approx(0.0, abs=1e-9)
    assert trace.level_db[2] == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------- multi-seat
def _member(i: int) -> MultiSeatMember:
    return MultiSeatMember(
        measurement_id=f'm{i}', dataset_id=f'd{i}',
        dataset_sha256=sha256(f'd{i}'.encode()).hexdigest(),
        target_entity_id=f'e{i}', seat_label=f's{i}',
        scene_revision_id='r1', scene_content_hash='a' * 64,
        is_mlp=(i == 0),
    )


def test_multi_seat_envelope_mean_outlier():
    seat_set = build_multi_seat_set(
        (_member(0), _member(1), _member(2)),
        document_id='doc', created_at='t0',
    )
    base = _curve(lambda i: 0.0)
    curves = (
        base,
        _curve(lambda i: 3.0),
        _curve(lambda i: 9.0),
    )
    result = run_multi_seat_analysis(
        seat_set, curves, low_hz=30.0, high_hz=8000.0,
        central_tendency='arithmetic_mean_in_db',
        outlier_band_hz=(30.0, 8000.0),
        created_at='t0',
    )
    for i in range(len(result.grid_hz)):
        col = [row[i] for row in result.member_levels_db]
        assert result.min_db[i] == pytest.approx(min(col))
        assert result.max_db[i] == pytest.approx(max(col))
        assert result.spread_db[i] == pytest.approx(max(col) - min(col))
        assert result.mean_db[i] == pytest.approx(sum(col) / 3.0)
    # Outlier = member with largest RMS deviation from the band mean
    # (member 2 sits +9 dB, others +0/+3).
    assert result.outlier_member_index == 2


# -------------------------------------------------------------- objectives
def test_seat_pairwise_objectives_recomputed():
    r = [_curve(lambda i: 0.0), _curve(lambda i: 3.0), _curve(lambda i: 9.0)]
    vec = seat_pairwise_objectives('cand', r, ResponseObjectiveSpec(
        low_hz=30.0, high_hz=8000.0))
    mv = {m.objective_id: m.value for m in vec.metrics}
    # Pairwise RMS diffs (independent of grid): 3, 9, 6 dB.
    assert mv['seat.pairwise_rms_difference_max_db'] == pytest.approx(
        9.0, abs=0.05)
    assert mv['seat.pairwise_rms_difference_rms_db'] == pytest.approx(
        math.sqrt((9.0 + 81.0 + 36.0) / 3.0), abs=0.05)


def test_target_response_objectives_recomputed():
    a = _curve(lambda i: 0.0)
    b = _curve(lambda i: -3.0 - 1.5 * math.cos(i * 0.2))
    vec = target_response_objectives(
        'cand', b, a, ResponseObjectiveSpec(low_hz=30.0, high_hz=8000.0))
    mv = {m.objective_id: m.value for m in vec.metrics}
    assert mv['response.peak_excess_db'] == pytest.approx(0.0, abs=1e-9)
    # diff = b - a = -3 - 1.5cos → min = -4.5 → dip_deficit = 4.5
    assert mv['response.dip_deficit_db'] == pytest.approx(4.5, abs=0.05)


def test_movement_objectives_euclidean():
    vec = movement_objectives(
        'cand',
        {'e1': {'x_m': 0.0, 'y_m': 0.0, 'z_m': 0.0}},
        {'e1': {'x_m': 3.0, 'y_m': 4.0, 'z_m': 0.0}},
    )
    mv = {m.objective_id: m.value for m in vec.metrics}
    assert mv['movement.total_m'] == pytest.approx(5.0, abs=1e-12)
    assert mv['movement.max_m'] == pytest.approx(5.0, abs=1e-12)


# --------------------------------------------------------- verdict boundaries
def _profile(rule: CriterionRule, unit: str = 's'):
    criterion = AcousticTargetCriterion(
        criterion_id='c1', name='T30 bound',
        metric='decay_t30', metric_version='m1',
        origin='user_goal',
        bands=(AcousticTargetBand(
            band_id='b1',
            frequency=FrequencyDomain(minimum_hz=100.0, maximum_hz=8000.0),
        ),),
        rule=rule, unit=unit, aggregation='spatial_mean',
        required_capability='measured_t30',
    )
    return build_acoustic_target_profile(
        profile_id='p', profile_version='v1', name='p',
        document_id='doc', criteria=(criterion,), created_at_utc='t0',
    )


def _evaluate(profile, value, unit, entity='seat1'):
    obs = [AcousticTargetObservation(
        criterion_id='c1', band_id='b1', observed_value=value,
        unit=unit, evidence_basis='measured',
        provided_capability='measured_t30', entity_ids=(entity,),
    )]
    ev = evaluate_acoustic_targets(
        profile=profile, observations=obs,
        available_capabilities=('measured_t30',), created_at_utc='t0')
    return ev.results[0]


def test_target_boundary_inclusive_and_exclusive():
    inclusive = _profile(CriterionRule(operator='max', maximum=0.5))
    assert _evaluate(inclusive, 0.5, 's').verdict == 'MET'
    exclusive = _profile(CriterionRule(
        operator='max', maximum=0.5, upper_inclusive=False))
    assert _evaluate(exclusive, 0.5, 's').verdict == 'UNMET'


def test_target_observation_unit_conversion():
    profile = _profile(CriterionRule(operator='max', maximum=0.5))
    result = _evaluate(profile, 500.0, 'ms')
    assert result.verdict == 'MET'
    member = result.band_results[0].member_results[0]
    assert member.observed_value == pytest.approx(0.5)
    assert member.converted is True


# -------------------------------------------------------------- spatial IR
def test_iacc_identical_channels():
    rng = np.random.default_rng(3)
    signal = rng.standard_normal(4096)
    # Identical channels correlate perfectly at lag 0.
    assert _iacc(signal, signal, 48000, 0.0, 0.08, 0.001) == pytest.approx(
        1.0, abs=1e-9)
    # Right = delayed copy -> correlation peak appears inside the lag scan.
    right = np.zeros_like(signal)
    right[24:] = signal[:-24]
    assert abs(_iacc(signal, right, 48000, 0.0, 0.08, 0.001)) == pytest.approx(
        1.0, abs=0.01)


def test_lateral_fraction_ratio():
    lateral = np.zeros(4096)
    omni = np.zeros(4096)
    omni[:100] = 1.0          # omni energy 100 over [0, end)
    lateral[10:30] = 0.5      # figure-8 energy 0.25*20 = 5 inside window
    # J_LF = sum(p_L^2 in window) / sum(p^2 over 0..window_end)
    j = _lateral_fraction(lateral, omni, 48000, 0.0, 0.04)
    assert j == pytest.approx((0.25 * 20.0) / 100.0, abs=1e-9)


# ------------------------------------------------------------------ REW axis
def test_rew_ppo_frequency_axis():
    mag = struct.pack('>4f', 70.0, 71.0, 72.0, 73.0)
    payload = {
        'magnitude': base64.b64encode(mag).decode(),
        'startFreq': 20.0, 'ppo': 2,
    }
    response = decode_frequency_response('m', payload)
    want = tuple(20.0 * 2.0 ** (i / 2.0) for i in range(4))
    assert response.frequency_hz == pytest.approx(want)
