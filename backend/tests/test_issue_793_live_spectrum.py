"""Issue #793 regression tests — single-channel live-observation
authority: real-time spectrum/RTA, spectrograph, SPL/Leq history, peak
hold, event annotations and the live->capture promotion boundary.

Fixture coverage follows the issue's suggested set: LIVE10 calibrated
RTA, LIVE20 HVAC state change, LIVE30 momentary vs qualified noise,
LIVE40 overload, LIVE50 event gap, LIVE60 RTA vs transfer function,
LIVE70 multi-input, LIVE80 long Leq logging.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_live_spectrum import (
    CapturedLiveTrace,
    FractionalOctaveBinding,
    LiveCaptureSettings,
    LiveEventAnnotation,
    LiveSpectrumObservation,
    RealtimeMeasurementSession,
    SpectrumEstimatorBinding,
    SplQuantityBinding,
    SPLTimeHistory,
    SpectrographBinding,
    TimeHistoryPoint,
    banding_eligibility,
    compare_captured_traces,
    evaluate_capture_promotion,
    evaluate_instrument_binding,
    evaluate_live_observation,
    evaluate_noise_evidence_eligibility,
    evaluate_state_correlation,
    evaluate_time_history_integrity,
    spl_quantity_state,
)
from htdt.cad_live_spectrum_repository import (
    CadLiveSpectrumRepository,
    LiveSpectrumConflictError,
    LiveSpectrumIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256


DOC = 'doc-live793'
_SHA = canonical_sha256({'fixture': 'sha'})
_SHA2 = canonical_sha256({'fixture': 'sha2'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _spectrum(**kw) -> SpectrumEstimatorBinding:
    payload = dict(
        estimator_ref=_ref('spectral_estimator_profile', 'sep-1'),
        fft_size=8192,
        window_kind='hann',
        record_length_s=0.171,
        overlap_fraction=0.5,
        averaging='exponential',
        detector='rms',
        power_scaling='power',
        frequency_resolution_hz=5.86,
        smoothing_fraction_oct=None,
        peak_hold_enabled=False,
        update_rate_hz=4.0,
        estimator_version='htdt-spectral-1.0',
    )
    payload.update(kw)
    return SpectrumEstimatorBinding(**payload)


def _banding(**kw) -> FractionalOctaveBinding:
    payload = dict(
        banding_eligibility='project_defined_banding',
        band_fraction='1/3',
        filter_implementation='fft_bin_aggregation',
    )
    payload.update(kw)
    return FractionalOctaveBinding(**payload)


def _spl(**kw) -> SplQuantityBinding:
    payload = dict(
        frequency_weighting='a',
        time_weighting='fast',
        integration_interval_s=0.125,
        leq_interval_s=None,
        peak_detector_semantics='iec_61672_true_peak',
        max_min_reset_state='running_unreset',
        reference_pressure='20 uPa per IEC 61672-1',
    )
    payload.update(kw)
    return SplQuantityBinding(**payload)


def _spectrograph(**kw) -> SpectrographBinding:
    payload = dict(
        method='fft',
        window_kind='hann',
        time_step_s=0.05,
        frequency_grid='log 20 Hz - 20 kHz, 1/24 oct',
        dynamic_range_db=70.0,
        weighting='z',
        normalization='density',
        display_palette='magma',
    )
    payload.update(kw)
    return SpectrographBinding(**payload)


def _session(**kw) -> RealtimeMeasurementSession:
    payload = dict(
        document_id=DOC,
        input_channel='mic-in-1',
        input_domain='acoustic_pressure',
        instrument_ref=_ref('microphone', 'mic-earthworks-m23'),
        calibration_ref=_ref('instrument_calibration', 'cal-611-1'),
        interface_path_ref=_ref('interface_path', 'iface-699-1'),
        linearity_ref=_ref('measurement_chain', 'lin-695-1'),
        timebase_ref=_ref('timebase', 'tb-609-1'),
        orientation_ref=_ref('orientation', 'ori-732-1'),
        receiver_location_ref=_ref('receiver_location', 'seat-1'),
        gain_range_state='+20 dB range',
        sample_rate_hz=48000.0,
        active_modes=('fractional_octave_rta', 'spl_time_weighted'),
        system_state_ref=_ref('system_state', 'st-573-1'),
        operator='op-1',
        started_at_utc='2026-10-07T01:00:00Z',
    )
    payload.update(kw)
    return RealtimeMeasurementSession.create(**payload)


def _observation(**kw) -> LiveSpectrumObservation:
    s = _session()
    payload = dict(
        document_id=DOC,
        session_ref=_ref('rt_session', s.session_id, s.session_sha256),
        mode='realtime_spectrum',
        capture_state='live_view_only',
        spectrum=_spectrum(),
        observed_at_utc='2026-10-07T01:00:01Z',
    )
    payload.update(kw)
    return LiveSpectrumObservation.create(**payload)


def _rta_observation(**kw) -> LiveSpectrumObservation:
    kw.setdefault('mode', 'fractional_octave_rta')
    kw.setdefault('banding', _banding())
    return _observation(**kw)


def _spl_observation(**kw) -> LiveSpectrumObservation:
    kw.setdefault('mode', 'spl_time_weighted')
    kw.setdefault('spl', _spl())
    kw.setdefault('quantity_value_db', 72.4)
    return _observation(**kw)


def _annotation(**kw) -> LiveEventAnnotation:
    s = _session()
    payload = dict(
        document_id=DOC,
        session_ref=_ref('rt_session', s.session_id, s.session_sha256),
        text='HVAC changed to high',
        operator='op-1',
        source='operator',
        claim_kind='contextual_observation',
        observed_at_utc='2026-10-07T01:02:00Z',
        onset_s=120.0,
    )
    payload.update(kw)
    return LiveEventAnnotation.create(**payload)


def _history(**kw) -> SPLTimeHistory:
    s = _session()
    payload = dict(
        document_id=DOC,
        session_ref=_ref('rt_session', s.session_id, s.session_sha256),
        quantity='spl_time_weighted',
        spl=_spl(),
        interval_s=1.0,
        points=(
            TimeHistoryPoint(
                offset_s=0.0, kind='measured', value_db=68.2),
            TimeHistoryPoint(
                offset_s=1.0, kind='measured', value_db=68.4),
            TimeHistoryPoint(
                offset_s=2.0, kind='measured', value_db=69.0),
        ),
        timebase_ref=_ref('timebase', 'tb-609-1'),
        started_at_utc='2026-10-07T01:00:00Z',
    )
    payload.update(kw)
    return SPLTimeHistory.create(**payload)


def _trace(**kw) -> CapturedLiveTrace:
    s = _session()
    o = _rta_observation(capture_state='captured_trace',
                         payload_ref=_ref('spectrum_payload', 'sp-1'),
                         payload_kind='canonical_series')
    a = _annotation()
    payload = dict(
        document_id=DOC,
        session_ref=_ref('rt_session', s.session_id, s.session_sha256),
        capture_kind='captured_trace',
        source_observation_refs=(
            _ref('live_observation', o.observation_id,
                 o.observation_sha256),),
        canonical_payload_ref=_ref('spectrum_payload', 'sp-1'),
        raw_stream_ref=_ref('raw_stream', 'raw-1'),
        settings_snapshot=LiveCaptureSettings(
            active_modes=('fractional_octave_rta',),
            spectrum=_spectrum(),
            banding=_banding(),
        ),
        annotation_refs=(
            _ref('live_event_annotation', a.annotation_id,
                 a.annotation_sha256),),
        state_snapshot_ref=_ref('system_state', 'st-573-1'),
        capture_event='manual',
        operator='op-1',
        software_version='htdt-0.2.0',
        captured_at_utc='2026-10-07T01:05:00Z',
    )
    payload.update(kw)
    return CapturedLiveTrace.create(**payload)


# ---------------------------------------------------------------------------
# sealed identity + model validators
# ---------------------------------------------------------------------------

class TestSealedIdentity:
    def test_sealed_create_prefixes(self) -> None:
        assert _session().session_id.startswith('rms-')
        assert _observation().observation_id.startswith('lso-')
        assert _history().history_id.startswith('sth-')
        assert _trace().trace_id.startswith('clt-')
        assert _annotation().annotation_id.startswith('lea-')

    def test_seal_covers_payload(self) -> None:
        record = _session()
        digest = canonical_sha256(record.identity_payload())
        assert record.session_sha256 == digest
        assert record.session_id == f'rms-{digest[:24]}'

    def test_forged_sha_rejected_on_save(self, tmp_path: Path) -> None:
        """Model construction trusts the sha field; the repository's
        _assert_sealed re-verification is what rejects a forged sha."""
        repo = _repo(tmp_path)
        forged = _session().model_construct(
            **_session().model_dump(mode='python') | {
                'session_sha256': _SHA2})
        with pytest.raises(LiveSpectrumIntegrityError):
            repo.save_session(forged)

    def test_forged_id_rejected_on_save(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        forged = _session().model_construct(
            **_session().model_dump(mode='python') | {
                'session_id': 'rms-' + '0' * 24})
        with pytest.raises(LiveSpectrumIntegrityError):
            repo.save_session(forged)

    def test_session_requires_input_channel(self) -> None:
        with pytest.raises(ValidationError):
            _session(input_channel='')

    def test_session_is_single_channel_only(self) -> None:
        """LIVE60 — #663 owns dual-channel TF; this authority can never
        claim it (SPECTRUM/RTA != TRANSFER FUNCTION)."""
        with pytest.raises(ValidationError):
            _session(channel_semantics='dual_channel_tf')

    def test_session_refs_must_pin_sha(self) -> None:
        with pytest.raises(ValidationError):
            _session(calibration_ref=AuthorityRef(
                kind='instrument_calibration', ref_id='cal-1'))


class TestObservationValidation:
    def test_spectrum_mode_requires_estimator(self) -> None:
        with pytest.raises(ValidationError):
            _observation(spectrum=None)
        with pytest.raises(ValidationError):
            _rta_observation(spectrum=None)

    def test_rta_requires_banding(self) -> None:
        with pytest.raises(ValidationError):
            _rta_observation(banding=None)

    def test_spectrograph_requires_binding(self) -> None:
        with pytest.raises(ValidationError):
            _observation(mode='spectrograph')

    def test_spl_modes_require_quantity(self) -> None:
        for mode in (
                'spl_instantaneous', 'spl_time_weighted',
                'leq_time_averaged', 'peak_max_min_hold'):
            with pytest.raises(ValidationError):
                _observation(mode=mode)

    def test_leq_requires_interval(self) -> None:
        with pytest.raises(ValidationError):
            _observation(mode='leq_time_averaged', spl=_spl())
        ok = _observation(
            mode='leq_time_averaged', spl=_spl(leq_interval_s=60.0))
        assert ok.mode == 'leq_time_averaged'

    def test_bare_db_rejected(self) -> None:
        """Issue rule: never expose '92 dB' without quantity semantics."""
        with pytest.raises(ValidationError):
            _observation(mode='other_profiled_live_observable',
                         quantity_value_db=92.0)
        with pytest.raises(ValidationError):
            _spl_observation(spl=_spl(frequency_weighting='unknown'))

    def test_capture_requires_canonical_payload(self) -> None:
        with pytest.raises(ValidationError):
            _observation(capture_state='captured_trace')
        with pytest.raises(ValidationError):
            _observation(capture_state='captured_trace',
                         payload_ref=_ref('img', 'shot-1'),
                         payload_kind='raster_image')

    def test_derived_summary_needs_sources(self) -> None:
        with pytest.raises(ValidationError):
            _observation(capture_state='derived_summary',
                         payload_ref=_ref('summary', 'sum-1'),
                         payload_kind='external_reference')

    def test_spatial_average_needs_semantics(self) -> None:
        """LIVE70 — a multi-input display never silently averages."""
        with pytest.raises(ValidationError):
            _observation(multi_input_combination='spatial_average')
        ok = _observation(
            multi_input_combination='spatial_average',
            combination_semantics_ref=_ref('spatial_semantics', 's-1'))
        assert ok.multi_input_combination == 'spatial_average'

    def test_safety_alert_needs_threshold_profile(self) -> None:
        with pytest.raises(ValidationError):
            _observation(safety_critical_alert=True)
        ok = _observation(
            safety_critical_alert=True,
            alert_profile_refs=(_ref('alert_profile', 'ap-1'),))
        assert ok.safety_critical_alert

    def test_iec_class_needs_standard_and_evidence(self) -> None:
        with pytest.raises(ValidationError):
            _banding(banding_eligibility='iec_61260_class_1_eligible')
        with pytest.raises(ValidationError):
            _banding(
                banding_eligibility='iec_61260_class_1_eligible',
                standard_reference='IEC 61260-1@2014')
        ok = _banding(
            banding_eligibility='iec_61260_class_1_eligible',
            standard_reference='IEC 61260-1@2014',
            class_evidence_refs=(_ref('class_test_report', 'rep-1'),))
        assert ok.banding_eligibility == 'iec_61260_class_1_eligible'


class TestHistoryValidation:
    def test_offsets_must_increase(self) -> None:
        with pytest.raises(ValidationError):
            _history(points=(
                TimeHistoryPoint(
                    offset_s=1.0, kind='measured', value_db=70.0),
                TimeHistoryPoint(
                    offset_s=0.5, kind='measured', value_db=71.0),
            ))

    def test_missing_point_cannot_carry_value(self) -> None:
        with pytest.raises(ValidationError):
            TimeHistoryPoint(
                offset_s=5.0, kind='missing', value_db=70.0,
                marker='dropout')
        with pytest.raises(ValidationError):
            TimeHistoryPoint(offset_s=5.0, kind='missing')

    def test_leq_history_requires_interval(self) -> None:
        with pytest.raises(ValidationError):
            _history(quantity='leq')
        ok = _history(quantity='leq', spl=_spl(leq_interval_s=600.0))
        assert ok.quantity == 'leq'


class TestTraceValidation:
    def test_trace_needs_sources_and_payload(self) -> None:
        with pytest.raises(ValidationError):
            _trace(source_observation_refs=())
        with pytest.raises(ValidationError):
            _trace(canonical_payload_ref=AuthorityRef(
                kind='spectrum_payload', ref_id='sp-1'))

    def test_annotations_never_causal(self) -> None:
        """#9 — annotations stay hypotheses/context; claim_kind has no
        causal value to smuggle in."""
        with pytest.raises(ValidationError):
            _annotation(claim_kind='caused_by_state_change')
        with pytest.raises(ValidationError):
            _annotation(operator='')
        with pytest.raises(ValidationError):
            _annotation(observed_at_utc='')


# ---------------------------------------------------------------------------
# evaluators — every path fails closed
# ---------------------------------------------------------------------------

class TestInstrumentBinding:
    def test_no_session(self) -> None:
        verdict, _ = evaluate_instrument_binding(None)
        assert verdict == 'insufficient_evidence'

    def test_no_instrument_is_diagnostic(self) -> None:
        verdict, _ = evaluate_instrument_binding(
            _session(instrument_ref=None))
        assert verdict == 'diagnostic_only'

    def test_uncalibrated(self) -> None:
        verdict, _ = evaluate_instrument_binding(
            _session(calibration_ref=None))
        assert verdict == 'uncalibrated'

    def test_timebase_unbound(self) -> None:
        verdict, _ = evaluate_instrument_binding(
            _session(timebase_ref=None, sample_rate_hz=None))
        assert verdict == 'timebase_unbound'

    def test_bound(self) -> None:
        verdict, _ = evaluate_instrument_binding(_session())
        assert verdict == 'instrument_bound'


class TestLiveObservationVerdicts:
    def test_no_observation(self) -> None:
        verdict, _ = evaluate_live_observation(_session(), None)
        assert verdict == 'insufficient_evidence'

    def test_no_session(self) -> None:
        verdict, _ = evaluate_live_observation(None, _observation())
        assert verdict == 'diagnostic_only'

    def test_instrumentless_observation_is_diagnostic(self) -> None:
        verdict, _ = evaluate_live_observation(
            _session(instrument_ref=None), _observation())
        assert verdict == 'diagnostic_only'

    def test_overload_limits_claims(self) -> None:
        """LIVE40 — the #695 overload flag invalidates/limits the
        quantitative claim."""
        verdict, _ = evaluate_live_observation(
            _session(), _spl_observation(overload_detected=True))
        assert verdict == 'overload_limited'

    def test_safety_alert_denied_uncalibrated(self) -> None:
        """#14 — no safety-critical alarm from an uncalibrated input."""
        verdict, _ = evaluate_live_observation(
            _session(calibration_ref=None),
            _observation(
                safety_critical_alert=True,
                alert_profile_refs=(_ref('alert_profile', 'ap-1'),)))
        assert verdict == 'alert_denied_uncalibrated'

    def test_estimator_unqualified(self) -> None:
        verdict, _ = evaluate_live_observation(
            _session(),
            _observation(spectrum=SpectrumEstimatorBinding()))
        assert verdict == 'estimator_unqualified'
        verdict, _ = evaluate_live_observation(
            _session(),
            _observation(spectrum=_spectrum(window_kind='unknown')))
        assert verdict == 'estimator_unqualified'

    def test_rta_display_only_banding(self) -> None:
        """An FFT binned into ~1/3-octave bars is not an IEC 61260
        analyzer."""
        verdict, _ = evaluate_live_observation(
            _session(), _rta_observation())
        assert verdict == 'banding_display_only'
        verdict, _ = evaluate_live_observation(
            _session(),
            _rta_observation(
                banding=_banding(banding_eligibility='ppo_display_only')))
        assert verdict == 'banding_display_only'

    def test_uncalibrated_spl(self) -> None:
        verdict, _ = evaluate_live_observation(
            _session(calibration_ref=None), _spl_observation())
        assert verdict == 'uncalibrated'

    def test_unweighted_quantity(self) -> None:
        obs = _spl_observation(
            quantity_value_db=None,
            spl=_spl(time_weighting='unknown'))
        verdict, _ = evaluate_live_observation(_session(), obs)
        assert verdict == 'unweighted_quantity'

    def test_live_evidence_bound(self) -> None:
        verdict, _ = evaluate_live_observation(
            _session(), _observation())
        assert verdict == 'live_evidence_bound'
        verdict, _ = evaluate_live_observation(
            _session(), _spl_observation())
        assert verdict == 'live_evidence_bound'

    def test_iec_eligible_rta_is_bound(self) -> None:
        verdict, _ = evaluate_live_observation(
            _session(),
            _rta_observation(banding=_banding(
                banding_eligibility='iec_61260_class_2_eligible',
                standard_reference='IEC 61260-1@2014',
                class_evidence_refs=(
                    _ref('class_test_report', 'rep-1'),))))
        assert verdict == 'live_evidence_bound'


class TestBandingEligibility:
    def test_none(self) -> None:
        eligibility, _ = banding_eligibility(None)
        assert eligibility == 'unknown'

    def test_valid_iec_claim_survives(self) -> None:
        claim = _banding(
            banding_eligibility='iec_61260_class_1_eligible',
            standard_reference='IEC 61260-1@2014',
            class_evidence_refs=(
                _ref('class_test_report', 'rep-1'),))
        eligibility, _ = banding_eligibility(claim)
        assert eligibility == 'iec_61260_class_1_eligible'
        eligibility, _ = banding_eligibility(_banding())
        assert eligibility == 'project_defined_banding'

    def test_forged_iec_claim_degrades(self) -> None:
        """Fail-closed second line: a class claim smuggled past model
        validation still never survives the evaluator without pinned
        standard revision and bound class evidence."""
        forged = FractionalOctaveBinding.model_construct(
            banding_eligibility='iec_61260_class_1_eligible',
            band_fraction='1/3',
            filter_implementation='fft_bin_aggregation',
            standard_reference=None,
            class_evidence_refs=(),
        )
        eligibility, reason = banding_eligibility(forged)
        assert eligibility == 'project_defined_banding'
        assert 'downgraded' in reason
        forged2 = FractionalOctaveBinding.model_construct(
            banding_eligibility='iec_61260_class_2_eligible',
            band_fraction='1/3',
            filter_implementation='fft_bin_aggregation',
            standard_reference='IEC 61260-1@2014',
            class_evidence_refs=(),
        )
        eligibility, _ = banding_eligibility(forged2)
        assert eligibility == 'project_defined_banding'


class TestHistoryIntegrity:
    def test_no_history(self) -> None:
        verdict, _ = evaluate_time_history_integrity(None)
        assert verdict == 'insufficient_evidence'

    def test_empty_history(self) -> None:
        verdict, _ = evaluate_time_history_integrity(
            _history(points=()))
        assert verdict == 'insufficient_evidence'

    def test_complete(self) -> None:
        verdict, _ = evaluate_time_history_integrity(_history())
        assert verdict == 'history_complete'

    def test_declared_gap(self) -> None:
        """LIVE50 — a 20 s disconnect marked explicitly stays honest."""
        verdict, reason = evaluate_time_history_integrity(_history(points=(
            TimeHistoryPoint(offset_s=0.0, kind='measured',
                             value_db=68.0),
            TimeHistoryPoint(offset_s=1.0, kind='missing',
                             marker='interface disconnect'),
            TimeHistoryPoint(offset_s=21.0, kind='missing',
                             marker='interface disconnect'),
            TimeHistoryPoint(offset_s=22.0, kind='measured',
                             value_db=70.1),
        )))
        assert verdict == 'history_with_declared_gaps'
        assert '2' in reason

    def test_unmarked_gap_fails_closed(self) -> None:
        """A drawn-through 20 s gap with no marker is detected."""
        verdict, reason = evaluate_time_history_integrity(_history(points=(
            TimeHistoryPoint(offset_s=0.0, kind='measured',
                             value_db=68.0),
            TimeHistoryPoint(offset_s=1.0, kind='measured',
                             value_db=68.2),
            TimeHistoryPoint(offset_s=22.0, kind='measured',
                             value_db=70.1),
        )))
        assert verdict == 'gap_unmarked'
        assert '22.0' in reason


class TestCapturePromotion:
    def test_nothing_is_live_view(self) -> None:
        verdict, _ = evaluate_capture_promotion(_session(), None)
        assert verdict == 'live_view_only'

    def test_live_view_is_not_evidence(self) -> None:
        """#8 — a technician seeing a peak briefly is not a capture."""
        verdict, _ = evaluate_capture_promotion(
            _session(), _observation())
        assert verdict == 'live_view_only'

    def test_captured_observation(self) -> None:
        obs = _observation(capture_state='captured_trace',
                         payload_ref=_ref('spectrum_payload', 'sp-1'),
                         payload_kind='canonical_series')
        verdict, _ = evaluate_capture_promotion(_session(), obs)
        assert verdict == 'captured_evidence'

    def test_qualified_capture(self) -> None:
        """LIVE10 — captured trace preserves estimator/profile/state."""
        verdict, _ = evaluate_capture_promotion(
            _session(), _rta_observation(), _trace())
        assert verdict == 'qualified_capture'

    def test_capture_without_settings_is_evidence_only(self) -> None:
        verdict, _ = evaluate_capture_promotion(
            _session(), _rta_observation(),
            _trace(settings_snapshot=None))
        assert verdict == 'captured_evidence'

    def test_capture_uncalibrated(self) -> None:
        verdict, _ = evaluate_capture_promotion(
            _session(calibration_ref=None), _rta_observation(), _trace())
        assert verdict == 'uncalibrated_claim'

    def test_capture_without_instrument_is_diagnostic(self) -> None:
        verdict, _ = evaluate_capture_promotion(
            _session(instrument_ref=None), _rta_observation(), _trace())
        assert verdict == 'diagnostic_only'
        verdict, _ = evaluate_capture_promotion(
            None, _rta_observation(), _trace())
        assert verdict == 'diagnostic_only'


class TestStateCorrelation:
    def test_nothing_to_correlate(self) -> None:
        verdict, _ = evaluate_state_correlation(None, (), ())
        assert verdict == 'no_correlation'

    def test_hvac_change_correlation(self) -> None:
        """LIVE20 — before/high/after history + annotation aligned; the
        verdict stays contextual, never causal."""
        annotation = _annotation(
            related_state_ref=_ref('system_state', 'st-573-hvac-high'))
        verdict, reason = evaluate_state_correlation(
            _ref('state_change', 'hvac-high'),
            (annotation,),
            (_ref('live_observation', 'lso-x', _SHA),))
        assert verdict == 'contextual_correlation'
        assert 'causal' in reason or 'not' in reason

    def test_annotation_only(self) -> None:
        verdict, _ = evaluate_state_correlation(None, (_annotation(),), ())
        assert verdict == 'annotation_only'


class TestNoiseBoundary:
    def test_nothing_observed(self) -> None:
        verdict, _ = evaluate_noise_evidence_eligibility(None)
        assert verdict == 'preview_only'

    def test_momentary_rta_is_preview_only(self) -> None:
        """LIVE30 — a short live RTA snapshot can never become qualified
        background-noise evidence here; #580 owns qualification."""
        verdict, _ = evaluate_noise_evidence_eligibility(
            _rta_observation())
        assert verdict == 'preview_only'
        verdict, _ = evaluate_noise_evidence_eligibility(
            _rta_observation(capture_state='captured_trace',
                             payload_ref=_ref('payload', 'p-1'),
                             payload_kind='canonical_series',
                             duration_s=2.0))
        assert verdict == 'captured_noise_candidate'

    def test_trace_is_candidate(self) -> None:
        verdict, _ = evaluate_noise_evidence_eligibility(
            _rta_observation(), _trace())
        assert verdict == 'captured_noise_candidate'


class TestComparison:
    def test_identical_settings_comparable(self) -> None:
        verdict, _ = compare_captured_traces(_trace(), _trace())
        assert verdict == 'comparable'

    def test_different_settings_not_comparable(self) -> None:
        other = _trace(settings_snapshot=LiveCaptureSettings(
            active_modes=('fractional_octave_rta',),
            spectrum=_spectrum(fft_size=4096),
            banding=_banding(),
        ))
        verdict, _ = compare_captured_traces(_trace(), other)
        assert verdict == 'settings_differ'

    def test_missing_snapshot_or_trace(self) -> None:
        verdict, _ = compare_captured_traces(
            _trace(), _trace(settings_snapshot=None))
        assert verdict == 'insufficient_evidence'
        verdict, _ = compare_captured_traces(None, _trace())
        assert verdict == 'insufficient_evidence'


class TestMultiInput:
    def test_two_calibrated_mics_stay_independent(self) -> None:
        """LIVE70 — each input keeps its own calibration/timebase; no
        implicit spatial average."""
        mic_a = _session(input_channel='mic-in-1',
                         instrument_ref=_ref('microphone', 'mic-a'))
        mic_b = _session(input_channel='mic-in-2',
                         instrument_ref=_ref('microphone', 'mic-b'),
                         calibration_ref=_ref(
                             'instrument_calibration', 'cal-611-2'))
        assert mic_a.session_id != mic_b.session_id
        verdict_a, _ = evaluate_live_observation(mic_a, _observation())
        verdict_b, _ = evaluate_live_observation(mic_b, _observation())
        assert verdict_a == verdict_b == 'live_evidence_bound'


class TestLongLeqLogging:
    def test_leq_history_retains_semantics(self) -> None:
        """LIVE80 — exact weighting/interval pinned; the room log never
        pretends to be personal exposure (#602)."""
        history = _history(
            quantity='leq',
            spl=_spl(frequency_weighting='a', time_weighting='unknown',
                     leq_interval_s=600.0),
            interval_s=600.0,
            points=tuple(
                TimeHistoryPoint(
                    offset_s=600.0 * i, kind='aggregate',
                    value_db=55.0 + i * 0.1)
                for i in range(6)),
            exposure_interpretation='delegated_to_issue_602')
        verdict, _ = evaluate_time_history_integrity(history)
        assert verdict == 'history_complete'
        state, _ = spl_quantity_state(history.spl, 'leq')
        assert state == 'quantity_semantics_bound'
        assert history.exposure_interpretation \
            == 'delegated_to_issue_602'


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def _repo(tmp_path: Path) -> CadLiveSpectrumRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadLiveSpectrumRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    records = (
        ('rms', _session(), repo.save_session, repo.get_session,
         'session_id'),
        ('lso', _observation(), repo.save_observation,
         repo.get_observation, 'observation_id'),
        ('sth', _history(), repo.save_history, repo.get_history,
         'history_id'),
        ('clt', _trace(), repo.save_trace, repo.get_trace, 'trace_id'),
        ('lea', _annotation(), repo.save_annotation,
         repo.get_annotation, 'annotation_id'),
    )
    for _name, record, save, get, id_field in records:
        save(record)
        rid = getattr(record, id_field)
        assert get(rid) == record


def test_repository_lists_by_document(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session = _session()
    repo.save_session(session)
    other_doc = _session(document_id='doc-other')
    repo.save_session(other_doc)
    listed = repo.list_sessions(DOC)
    assert [s.session_id for s in listed] == [session.session_id]
    assert len(repo.list_sessions()) == 2


def test_repository_append_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    record = _session()
    repo.save_session(record)
    repo.save_session(record)  # idempotent re-save
    forged = _session(operator='op-2')
    assert forged.session_id != record.session_id
    repo.save_session(forged)
    assert repo.get_session(forged.session_id) == forged


def test_repository_rejects_unsealed_record(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    forged = _session().model_copy(update={'operator': 'op-x'})
    with pytest.raises(LiveSpectrumIntegrityError):
        repo.save_session(forged)


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    record = _observation()
    repo.save_observation(record)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_live_spectrum_observations '
            "SET capture_state='captured_trace' "
            'WHERE observation_id=?',
            (record.observation_id,),
        )
        connection.commit()
    with pytest.raises(LiveSpectrumIntegrityError):
        repo.get_observation(record.observation_id)


def test_live_tables_exist_after_fresh_migrate(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with connect_sqlite(db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    expected = {
        'cad_realtime_measurement_sessions',
        'cad_live_spectrum_observations',
        'cad_spl_time_histories',
        'cad_captured_live_traces',
        'cad_live_event_annotations',
    }
    assert expected <= tables
