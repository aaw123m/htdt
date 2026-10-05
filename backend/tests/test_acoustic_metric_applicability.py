"""Regression tests for the small-room metric applicability gate (#571)."""

from __future__ import annotations

import math

import pytest

from htdt.acoustic_metric_applicability import (
    PositionDecaySample,
    RoomMetricContext,
    build_decay_metric_profile,
    build_modal_decay_record,
    build_spatial_decay_record,
    evaluate_metric_applicability,
    evaluate_metric_comparison,
    evaluate_optimization_metric,
    modal_bandwidth_hz,
    modal_density_per_hz,
    modal_overlap_index,
    room_metric_context,
    schroeder_frequency_hz,
)


# ---------------------------------------------------------------------------
# Physics helpers — Schroeder / modal overlap (literature-derived)
# ---------------------------------------------------------------------------


class TestSchroederPhysics:
    def test_schroeder_frequency_representative_values(self):
        # V=72 m³, T60=0.4 s → fs ≈ 149 Hz (home-theater class).
        fs = schroeder_frequency_hz(0.4, 72.0)
        assert fs == pytest.approx(2000.0 * math.sqrt(0.4 / 72.0))
        assert 140.0 < fs < 160.0
        # V=30 m³, T60=0.3 s → fs ≈ 200 Hz — typical small-room range.
        assert 190.0 < schroeder_frequency_hz(0.3, 30.0) < 210.0
        # V=100 m³, T60=0.5 s → fs ≈ 141 Hz.
        assert 135.0 < schroeder_frequency_hz(0.5, 100.0) < 150.0

    def test_modal_bandwidth(self):
        # Δf ≈ 2.2/T60
        assert modal_bandwidth_hz(0.5) == pytest.approx(4.4)

    def test_overlap_index_reproduces_schroeder(self):
        # At f_s the modal overlap sits in the ~3-order transition zone —
        # the representative 2000 constant folds in approximation (literature
        # reports the exact coefficient varies with the convention used).
        volume, surface, t60 = 72.0, 108.0, 0.4
        fs = schroeder_frequency_hz(t60, volume)
        overlap = modal_overlap_index(fs, t60, volume, surface)
        assert 2.5 <= overlap <= 4.5

    def test_modal_density_grows_quadratically(self):
        d1 = modal_density_per_hz(100.0, 72.0, 108.0)
        d2 = modal_density_per_hz(200.0, 72.0, 108.0)
        assert d2 > d1 * 3.0


# ---------------------------------------------------------------------------
# Applicability decisions — domain classification + fail-closed
# ---------------------------------------------------------------------------


class TestApplicability:
    def test_diffuse_band_metric_applicable(self):
        ctx = room_metric_context(6.0, 4.0, 3.0, 0.4)
        decision = evaluate_metric_applicability(
            't20_band', ctx, band_hz=1000.0
        )
        assert decision.applicable
        assert decision.domain_class == 'diffuse_field_band_metric'
        assert decision.schroeder_hz == pytest.approx(149.07, abs=1.0)

    def test_below_schroeder_requires_modal_authority(self):
        ctx = room_metric_context(6.0, 4.0, 3.0, 0.4)
        decision = evaluate_metric_applicability(
            't20_band', ctx, band_hz=63.0
        )
        assert not decision.applicable
        assert decision.domain_class == 'modal_non_diffuse'
        assert 'BELOW_SCHROEDER_FREQUENCY' in decision.reasons
        assert 'MODAL_AUTHORITY_REQUIRED' in decision.reasons
        assert decision.required_evidence == 'modal_decay_record'

    def test_transition_region_is_limited(self):
        ctx = room_metric_context(6.0, 4.0, 3.0, 0.4)
        fs = schroeder_frequency_hz(0.4, 72.0)
        decision = evaluate_metric_applicability(
            't20_band', ctx, band_hz=fs * 0.75
        )
        assert decision.domain_class == 'transition_region'
        assert decision.limited
        assert 'TRANSITION_REGION_CAUTION' in decision.reasons

    def test_undeclared_context_is_not_applicable(self):
        ctx = RoomMetricContext()  # nothing declared
        decision = evaluate_metric_applicability('t20_band', ctx, band_hz=500.0)
        assert not decision.applicable
        assert 'VOLUME_UNDECLARED' in decision.reasons

    def test_band_required_for_band_metrics(self):
        ctx = room_metric_context(6.0, 4.0, 3.0, 0.4)
        decision = evaluate_metric_applicability('t20_band', ctx)
        assert not decision.applicable
        assert 'BAND_UNDECLARED' in decision.reasons

    def test_insufficient_decay_range_via_profile(self):
        ctx = room_metric_context(6.0, 4.0, 3.0, 0.4)
        profile = build_decay_metric_profile(
            profile_id='p-short',
            metric='t20',
            standard_revision='ISO 3382-2:2008',
            band_hz=1000.0,
            position_ids=('pos-1',),
            method='integrated_impulse',
            dynamic_range_db=20.0,  # T20 needs ≥ 30 dB
        )
        decision = evaluate_metric_applicability(
            't20_band', ctx, band_hz=1000.0, profile=profile
        )
        assert not decision.applicable
        assert decision.domain_class == 'insufficient_decay_range'
        assert 'DECAY_RANGE_INSUFFICIENT' in decision.reasons

    def test_insufficient_snr_via_profile(self):
        ctx = room_metric_context(6.0, 4.0, 3.0, 0.4)
        profile = build_decay_metric_profile(
            profile_id='p-noisy',
            metric='t30',
            standard_revision='ISO 3382-2:2008',
            band_hz=1000.0,
            position_ids=('pos-1',),
            method='integrated_impulse',
            dynamic_range_db=50.0,
            snr_db=4.0,
        )
        decision = evaluate_metric_applicability(
            't30_band', ctx, band_hz=1000.0, profile=profile
        )
        assert not decision.applicable
        assert decision.domain_class == 'insufficient_snr'

    def test_spatial_variance_blocks_scalar(self):
        ctx = room_metric_context(6.0, 4.0, 3.0, 0.4)
        profile = build_decay_metric_profile(
            profile_id='p1',
            metric='t20',
            standard_revision='ISO 3382-2:2008',
            band_hz=1000.0,
            position_ids=('a', 'b', 'c'),
            method='integrated_impulse',
            dynamic_range_db=45.0,
        )
        spatial = build_spatial_decay_record(
            record_id='sr1',
            band_hz=1000.0,
            metric='t20',
            samples=(
                PositionDecaySample(
                    position_id='a', band_hz=1000.0, metric='t20',
                    value_s=0.30,
                ),
                PositionDecaySample(
                    position_id='b', band_hz=1000.0, metric='t20',
                    value_s=0.50,
                ),
                PositionDecaySample(
                    position_id='c', band_hz=1000.0, metric='t20',
                    value_s=0.45,
                ),
            ),
        )
        decision = evaluate_metric_applicability(
            't20_band', ctx, band_hz=1000.0,
            profile=profile, spatial=spatial,
        )
        assert decision.limited
        assert 'SPATIAL_VARIANCE_HIGH' in decision.reasons
        assert decision.required_evidence == 'per_position'


# ---------------------------------------------------------------------------
# Decay metric profile — ISO 3382-2 binding (issue §3)
# ---------------------------------------------------------------------------


class TestDecayProfile:
    def test_t20_interval_enforced(self):
        with pytest.raises(ValueError, match='fit interval'):
            build_decay_metric_profile(
                profile_id='p-bad',
                metric='t20',
                standard_revision='ISO 3382-2:2008',
                band_hz=500.0,
                position_ids=('a',),
                method='integrated_impulse',
                dynamic_range_db=40.0,
                fit_interval_db=(0.0, -10.0),  # EDT interval on a T20
            )

    def test_profile_is_sealed(self):
        profile = build_decay_metric_profile(
            profile_id='p1',
            metric='t30',
            standard_revision='ISO 3382-2:2008',
            band_hz=500.0,
            position_ids=('a',),
            method='interrupted_noise',
            dynamic_range_db=50.0,
        )
        assert profile.profile_sha256


# ---------------------------------------------------------------------------
# Modal decay authority — never relabeled as RT60 (issue §5)
# ---------------------------------------------------------------------------


class TestModalDecay:
    def test_modal_decay_record_sealed(self):
        record = build_modal_decay_record(
            record_id='md-1',
            frequency_hz=57.2,
            decay_time_s=0.9,
            extraction_method='mode-fit',
            mode_indices=(1, 0, 0),
        )
        assert record.record_sha256
        # Never an RT60: no rt/t60-named field exists.
        assert not hasattr(record, 'rt60_s')
        assert not hasattr(record, 't60_s')

    def test_modal_decay_applicable_below_schroeder(self):
        ctx = room_metric_context(6.0, 4.0, 3.0, 0.4)
        decision = evaluate_metric_applicability(
            'modal_decay_per_mode', ctx, band_hz=63.0
        )
        assert decision.applicable
        assert decision.domain_class == 'modal_non_diffuse'


# ---------------------------------------------------------------------------
# Comparison eligibility (issue §6)
# ---------------------------------------------------------------------------


def _profile(metric='t20', band=1000.0, positions=('a',), **kw):
    base = dict(
        profile_id=f'p-{metric}-{band}',
        metric=metric,
        standard_revision='ISO 3382-2:2008',
        band_hz=band,
        position_ids=positions,
        method='integrated_impulse',
        dynamic_range_db=45.0,
    )
    base.update(kw)
    return build_decay_metric_profile(**base)


class TestComparison:
    def test_identical_semantics_comparable(self):
        c = evaluate_metric_comparison(
            _profile(positions=('a', 'b')),
            _profile(positions=('b', 'a')),
        )
        assert c.eligibility == 'COMPARABLE'

    def test_metric_mismatch_incomparable(self):
        c = evaluate_metric_comparison(_profile(metric='t20'), _profile(metric='t30'))
        assert c.eligibility == 'INCOMPARABLE'
        assert 'METRIC_MISMATCH' in c.reasons

    def test_method_mismatch_limited(self):
        c = evaluate_metric_comparison(
            _profile(method='integrated_impulse'),
            _profile(method='interrupted_noise'),
        )
        assert c.eligibility == 'COMPARABLE_WITH_LIMITATIONS'
        assert 'METHOD_MISMATCH' in c.reasons

    def test_domain_mismatch_incomparable(self):
        c = evaluate_metric_comparison(
            _profile(),
            _profile(),
            prediction_domain='modal_non_diffuse',
            measurement_domain='diffuse_field_band_metric',
        )
        assert c.eligibility == 'INCOMPARABLE'
        assert 'DOMAIN_MISMATCH' in c.reasons


# ---------------------------------------------------------------------------
# Optimization guard (issue §7)
# ---------------------------------------------------------------------------


class TestOptimizationGuard:
    def test_applicable_metric_is_eligible(self):
        ctx = room_metric_context(6.0, 4.0, 3.0, 0.4)
        decision = evaluate_metric_applicability(
            't20_band', ctx, band_hz=1000.0
        )
        opt = evaluate_optimization_metric(decision)
        assert opt.eligible

    def test_modal_region_substitutes_modal_objective(self):
        ctx = room_metric_context(6.0, 4.0, 3.0, 0.4)
        decision = evaluate_metric_applicability(
            't20_band', ctx, band_hz=63.0
        )
        opt = evaluate_optimization_metric(decision)
        assert not opt.eligible
        assert opt.substitute_objective == 'modal_decay_per_mode'
        assert 'MODAL_REGION_NEEDS_MODAL_OBJECTIVE' in opt.reasons
