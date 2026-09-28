"""Issue #971: phase/time diagnostics — group delay, minimum/excess phase,
timing-removal semantics, crossover-pair compatibility gates."""

from __future__ import annotations

from math import isclose, pi

import pytest
from pydantic import ValidationError

from htdt.cad_phase_time_analysis import (
    CrossoverAlignmentDiagnostic,
    HTDT_GROUP_DELAY_ALGORITHM,
    analyze_group_delay,
    analyze_minimum_excess_phase,
    build_phase_time_spec,
    compute_group_delay_s,
    compute_minimum_phase_deg,
    unwrap_phase_deg,
)

_H = 'c' * 64


def _spec(**overrides):
    kwargs = dict(
        spec_id='pts-1',
        schema_version='phase_time_v1',
        document_id='doc-1',
        measurement_id='meas-1',
        dataset_id='ds-1',
        dataset_sha256=_H,
        phase_capability='valid',
        absolute_timing_state='retained',
        delay_removal_state='not_applied',
        min_phase_provider='htdt',
        min_phase_algorithm_version='htdt_min_phase_v1',
        band_state='full',
        producer='htdt',
        producer_version='0.1.0',
        created_at_utc='2026-09-25T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_phase_time_spec(**kwargs)


def test_unwrap_corrects_discontinuities():
    raw = (170.0, 175.0, 185.0, -175.0, -170.0)
    unwrapped = unwrap_phase_deg(raw)
    assert unwrapped[2:] == (185.0, 185.0, 190.0)


def test_group_delay_recovers_constant_delay():
    # H(f) = exp(-j·2π·f·τ) → phase slope -360·τ deg/Hz, GD = τ.
    tau_s = 0.003
    freqs = tuple(10.0 + 10.0 * i for i in range(20))
    phase = tuple(-360.0 * tau_s * f for f in freqs)
    gd = compute_group_delay_s(freqs, phase)
    for value in gd:
        assert isclose(value, tau_s, rel_tol=1e-6)


def test_group_delay_requires_samples():
    with pytest.raises(ValueError, match='three samples'):
        compute_group_delay_s((10.0, 20.0), (0.0, -1.0))


def test_min_phase_matches_min_phase_system():
    # Discrete-time minimum-phase reference: H(z) = (1 + a z^-1)(1 - b z^-1)
    # has both zeros inside the unit circle, so its analytic phase is the
    # exact minimum phase for the sampled magnitude. The mirrored even
    # extension used by the cepstral lifter is then exact (cepstral tail
    # truncation scales like a^(2N) << machine precision for |a| < 1 and
    # N ~ 257), unlike a continuous-time fixture where a still-rolling-off
    # band edge carries inherent flat-extension truncation bias.
    import math

    a, b = 0.8, 0.55
    fnyq = 24000.0
    n = 257
    freqs = tuple(i * fnyq / (n - 1) for i in range(n))

    def mag_db(f: float) -> float:
        w = math.pi * f / fnyq
        m1 = 1.0 + 2.0 * a * math.cos(w) + a * a
        m2 = 1.0 - 2.0 * b * math.cos(w) + b * b
        return 10.0 * math.log10(m1) + 10.0 * math.log10(m2)

    def analytic_phase_deg(f: float) -> float:
        w = math.pi * f / fnyq
        return math.degrees(
            -math.atan2(a * math.sin(w), 1.0 + a * math.cos(w))
            + math.atan2(b * math.sin(w), 1.0 - b * math.cos(w))
        )

    derived = compute_minimum_phase_deg(freqs, tuple(mag_db(f) for f in freqs))
    worst = max(
        abs(derived[i] - analytic_phase_deg(freqs[i])) for i in range(n)
    )
    assert worst < 0.05


def test_min_phase_flat_response_is_zero():
    # A flat magnitude must return ~0 deg everywhere — the round-9 atan2
    # bug reported +/-180 deg for this input.
    freqs = tuple(float(f) for f in range(0, 1001, 10))
    derived = compute_minimum_phase_deg(freqs, (-3.0,) * len(freqs))
    assert max(abs(v) for v in derived) < 0.05


def test_min_phase_rejects_nonuniform_grid():
    freqs = (0.0, 20.0, 50.0, 90.0, 140.0)
    with pytest.raises(ValueError, match='uniform frequency grid'):
        compute_minimum_phase_deg(freqs, (0.0,) * 5)


def test_min_phase_rejects_subband_axis():
    # a truncated measurement band cannot reconstruct true minimum phase
    freqs = tuple(float(f) for f in range(10, 10010, 10))
    with pytest.raises(ValueError, match='DC-to-Nyquist'):
        compute_minimum_phase_deg(freqs, (-3.0,) * len(freqs))


def test_delay_removal_cannot_retain_absolute_timing():
    with pytest.raises(ValidationError, match='absolute-timing'):
        _spec(delay_removal_state='applied', absolute_timing_state='retained')


def test_insufficient_band_cannot_min_phase():
    with pytest.raises(ValidationError, match='insufficient'):
        _spec(band_state='insufficient', min_phase_provider='htdt')


def test_group_delay_result_is_sealed():
    spec = _spec()
    freqs = tuple(10.0 + 10.0 * i for i in range(10))
    phase = tuple(-360.0 * 0.002 * f for f in freqs)
    result = analyze_group_delay(
        spec,
        result_id='pta-1',
        dataset_id='ds-1',
        dataset_sha256=_H,
        frequency_hz=freqs,
        phase_deg=phase,
        created_at_utc='2026-09-25T00:01:00+00:00',
    )
    trace = result.trace('group_delay_s')
    assert trace is not None
    assert trace.algorithm_version == HTDT_GROUP_DELAY_ALGORITHM
    assert trace.provider == 'htdt'
    assert isclose(trace.values[0], 0.002, rel_tol=1e-6)


def test_min_excess_result_has_both_traces():
    spec = _spec()
    freqs = tuple(float(f) for f in range(0, 1001, 10))
    level = tuple(-3.0 for _ in freqs)
    phase = tuple(-0.5 * f / 10.0 for f in freqs)
    result = analyze_minimum_excess_phase(
        spec,
        result_id='pta-2',
        dataset_id='ds-1',
        dataset_sha256=_H,
        frequency_hz=freqs,
        level_db=level,
        measured_phase_deg=phase,
        created_at_utc='2026-09-25T00:02:00+00:00',
    )
    assert result.trace('minimum_phase_deg') is not None
    assert result.trace('excess_phase_deg') is not None


def test_min_excess_rejects_subband_axis():
    spec = _spec()
    with pytest.raises(ValueError, match='DC-to-Nyquist'):
        analyze_minimum_excess_phase(
            spec,
            result_id='pta-2b',
            dataset_id='ds-1',
            dataset_sha256=_H,
            frequency_hz=(20.0, 30.0, 40.0, 50.0, 60.0),
            level_db=(-3.0,) * 5,
            measured_phase_deg=(0.0,) * 5,
            created_at_utc='2026-09-25T00:02:30+00:00',
        )


def test_min_excess_rejects_nonflat_hf_extension():
    # htdt_min_phase_v1 implements a flat tail extension; a spec that
    # declares linear_slope or producer tails must fail closed rather
    # than silently ship flat-extension semantics.
    freqs = tuple(float(f) for f in range(0, 1001, 10))
    for hf_extension in ('linear_slope', 'producer'):
        spec = _spec(hf_extension=hf_extension)
        with pytest.raises(ValueError, match='flat tail extension'):
            analyze_minimum_excess_phase(
                spec,
                result_id=f'pta-hf-{hf_extension}',
                dataset_id='ds-1',
                dataset_sha256=_H,
                frequency_hz=freqs,
                level_db=(-3.0,) * len(freqs),
                measured_phase_deg=(0.0,) * len(freqs),
                created_at_utc='2026-09-25T00:02:40+00:00',
            )


def test_producer_identity_not_relabeled():
    spec = _spec(
        min_phase_provider='rew_producer',
        min_phase_algorithm_version='rew_540',
    )
    with pytest.raises(ValueError, match='provider'):
        analyze_minimum_excess_phase(
            spec,
            result_id='pta-3',
            dataset_id='ds-1',
            dataset_sha256=_H,
            frequency_hz=(10.0,) * 5,
            level_db=(0.0,) * 5,
            measured_phase_deg=(0.0,) * 5,
            created_at_utc='2026-09-25T00:03:00+00:00',
        )


def test_crossover_pair_requires_shared_timing():
    with pytest.raises(ValidationError, match='timing'):
        CrossoverAlignmentDiagnostic(
            diagnostic_id='cx-1',
            document_id='doc-1',
            pair_a_measurement_id='m1',
            pair_b_measurement_id='m2',
            timing_compatibility='unshared',
            state='computable',
            delay_difference_s=0.001,
        )
    ok = CrossoverAlignmentDiagnostic(
        diagnostic_id='cx-2',
        document_id='doc-1',
        pair_a_measurement_id='m1',
        pair_b_measurement_id='m2',
        timing_compatibility='shared_reference',
        state='computable',
        delay_difference_s=0.001,
        crossover_band_hz=(60.0, 120.0),
    )
    assert ok.state == 'computable'
