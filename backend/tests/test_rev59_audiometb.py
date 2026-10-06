"""REV59-AUDIOMET-B regression tests — #661 adaptive identification,
#663 live dual-channel TF, #658 microphone arrays, #662 impedance/T-S.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_adaptive_identification import (
    AdaptiveIdentificationProfile,
    AdaptiveTransferEstimate,
    ArbitraryStimulusMeasurement,
    ResidualEvidence,
    evaluate_adaptive_claim,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_field_metrology_repository import (
    CadFieldMetrologyRepository,
    FieldMetrologyIntegrityError,
)
from htdt.cad_impedance_measurement import (
    ImpedanceCalibrationState,
    ImpedanceMeasurementProfile,
    MeasuredLoadEvidence,
    ThieleSmallDerivation,
    evaluate_load_claim,
)
from htdt.cad_live_transfer_function import (
    CoherenceObservation,
    DualChannelTFObservation,
    LiveTransferFunctionSession,
    ReferenceDelayTrack,
    evaluate_capture_claim,
)
from htdt.cad_microphone_array import (
    BeamformingTransform,
    MicrophoneArrayGeometry,
    SpatialSamplingCapability,
    evaluate_spatial_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema


def _ref(rid: str = 'doc-1') -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='a' * 64)


def _repo(tmp_path: Path) -> CadFieldMetrologyRepository:
    db = tmp_path / 'scene.htdtscene'
    ensure_native_schema(db)
    return CadFieldMetrologyRepository(SceneRepository(db))


def _aam(**kw) -> AdaptiveIdentificationProfile:
    payload = dict(
        document_id='doc-1',
        method='subband_adaptive_identifier',
        method_reference='fsaf-reference-impl-x',
        license_review_status='reviewed_permitted',
        model_length_samples=8192,
    )
    payload.update(kw)
    return AdaptiveIdentificationProfile.create(payload)


def _asm(**kw) -> ArbitraryStimulusMeasurement:
    payload = dict(
        document_id='doc-1',
        stimulus_kind='file_segment',
        defect_state='stimulus_validated',
        asset_sha256='b' * 64,
        segment_start_s=0.0,
        segment_end_s=30.0,
        crest_factor_db=12.0,
    )
    payload.update(kw)
    return ArbitraryStimulusMeasurement.create(payload)


def _ate(**kw) -> AdaptiveTransferEstimate:
    payload = dict(
        document_id='doc-1',
        profile_ref=_ref('aam-1'),
        stimulus_ref=_ref('asm-1'),
        clock_qualification='common_clock',
        convergence_state='converged',
        excitation_support=(
            dict(band_hz=(80.0, 8000.0), state='identified'),
        ),
    )
    payload.update(kw)
    return AdaptiveTransferEstimate.create(payload)


def _are(**kw) -> ResidualEvidence:
    payload = dict(
        document_id='doc-1',
        estimate_ref=_ref('ate-1'),
        declared_quantity='adaptive_residual_tdn_like',
        components=(
            dict(kind='noise_contribution', magnitude_db=-60.0),
            dict(kind='nonlinear_contribution', magnitude_db=-45.0),
        ),
    )
    payload.update(kw)
    return ResidualEvidence.create(payload)


def _lts(**kw) -> LiveTransferFunctionSession:
    payload = dict(
        document_id='doc-1',
        reference_kind='electrical_loopback',
        reference_channel='in-2',
        measurement_channels=('in-1',),
        sample_rate_hz=48000.0,
        clock_topology='common_clock',
    )
    payload.update(kw)
    return LiveTransferFunctionSession.create(payload)


def _dto(**kw) -> DualChannelTFObservation:
    payload = dict(
        document_id='doc-1',
        session_ref=_ref('lts-1'),
        estimator='h1_gxy_gxx',
        mtw_method='single_fft',
        averaging='finite_fifo',
        capture_state='captured_snapshot',
        tf_payload_ref=_ref('tf-1'),
    )
    payload.update(kw)
    return DualChannelTFObservation.create(payload)


def _coh(**kw) -> CoherenceObservation:
    payload = dict(
        document_id='doc-1',
        observation_ref=_ref('dto-1'),
        raw_trace_ref=_ref('coh-raw-1'),
        possible_causes=('low_measurement_snr',),
    )
    payload.update(kw)
    return CoherenceObservation.create(payload)


def _rdt(**kw) -> ReferenceDelayTrack:
    payload = dict(
        document_id='doc-1',
        session_ref=_ref('lts-1'),
        delay_ms=3.4,
        method='auto_found',
    )
    payload.update(kw)
    return ReferenceDelayTrack.create(payload)


def _mag(**kw) -> MicrophoneArrayGeometry:
    payload = dict(
        document_id='doc-1',
        topology='linear',
        member_refs=(_ref('mic-1'), _ref('mic-2')),
        member_positions_m=((0.0, 0.0, 1.2), (0.1, 0.0, 1.2)),
        min_spacing_m=0.1,
        max_spacing_m=0.1,
        aperture_m=0.1,
    )
    payload.update(kw)
    return MicrophoneArrayGeometry.create(payload)


def _ssc(**kw) -> SpatialSamplingCapability:
    payload = dict(
        document_id='doc-1',
        array_ref=_ref('mag-1'),
        sync_capability='coherent_beamforming_eligible',
        propagation_model='far_field_plane_wave',
        band_capabilities=(
            dict(band_hz=(500.0, 3400.0), state='well_sampled'),
        ),
    )
    payload.update(kw)
    return SpatialSamplingCapability.create(payload)


def _bft(**kw) -> BeamformingTransform:
    payload = dict(
        document_id='doc-1',
        array_ref=_ref('mag-1'),
        capability_ref=_ref('ssc-1'),
        algorithm='delay_and_sum',
        steering_model='far_field_plane_wave',
        output_state='direction_supported',
        psf_evidence_ref=_ref('psf-1'),
        result_ref=_ref('map-1'),
    )
    payload.update(kw)
    return BeamformingTransform.create(payload)


def _zmp(**kw) -> ImpedanceMeasurementProfile:
    payload = dict(
        document_id='doc-1',
        target_kind='individual_driver',
        method='calibrated_dual_channel_divider',
        terminal_identity='driver-terminals',
        sample_rate_hz=48000.0,
    )
    payload.update(kw)
    return ImpedanceMeasurementProfile.create(payload)


def _zcs(**kw) -> ImpedanceCalibrationState:
    payload = dict(
        document_id='doc-1',
        profile_ref=_ref('zmp-1'),
        steps=('open_circuit_cal', 'short_circuit_cal',
               'reference_load_cal'),
        reference_load_ohms=100.0,
        reference_load_uncertainty_ohms=0.1,
        lead_identity='lead-A',
    )
    payload.update(kw)
    return ImpedanceCalibrationState.create(payload)


def _zle(**kw) -> MeasuredLoadEvidence:
    payload = dict(
        document_id='doc-1',
        profile_ref=_ref('zmp-1'),
        calibration_ref=_ref('zcs-1'),
        evidence_class='measured_complex_load',
        trace_ref=_ref('z-trace-1'),
        phase_trace_ref=_ref('z-phase-1'),
        frequency_range_hz=(10.0, 20000.0),
        signal_domain='small_signal',
    )
    payload.update(kw)
    return MeasuredLoadEvidence.create(payload)


def _tsd(**kw) -> ThieleSmallDerivation:
    payload = dict(
        document_id='doc-1',
        load_evidence_ref=_ref('zle-1'),
        method='added_mass',
        model_fit_state='model_fit_acceptable',
        added_mass_g=10.0,
        added_mass_uncertainty_g=0.2,
        rdc_ohms=5.6,
        fit_residual=0.02,
    )
    payload.update(kw)
    return ThieleSmallDerivation.create(payload)


# -- validation --------------------------------------------------------


def test_profile_requires_declared_method_and_license():
    with pytest.raises(ValueError):
        _aam(method='unknown')
    with pytest.raises(ValueError, match='license'):
        _aam(license_review_status='review_pending')
    with pytest.raises(ValueError, match='external_import_only'):
        _aam(method='subband_adaptive_identifier',
             license_review_status='external_import_only')
    with pytest.raises(ValueError, match='reference'):
        _aam(method_reference='')


def test_stimulus_requires_exact_identity():
    with pytest.raises(ValueError):
        _asm(stimulus_kind='unknown')
    with pytest.raises(ValueError, match='hash'):
        _asm(asset_sha256=None)
    with pytest.raises(ValueError, match='segment'):
        _asm(segment_end_s=None)
    with pytest.raises(ValueError, match='seed'):
        _asm(stimulus_kind='generated_noise', asset_sha256=None,
             segment_start_s=None, segment_end_s=None,
             realization_seed=None)


def test_estimate_requires_profile_stimulus_and_support():
    with pytest.raises(ValueError):
        _ate(profile_ref=None)
    with pytest.raises(ValueError):
        _ate(stimulus_ref=None)
    with pytest.raises(ValueError, match='excitation'):
        _ate(excitation_support=())
    with pytest.raises(ValueError, match='state evidence'):
        _ate(convergence_state='system_changed_during_adaptation')


def test_residual_requires_decomposition_and_not_thd():
    with pytest.raises(ValueError):
        _are(estimate_ref=None)
    with pytest.raises(ValueError, match='decomposed'):
        _are(components=())
    with pytest.raises(ValueError, match='THD'):
        _are(comparable_to_sweep_thd=True)


def test_session_requires_reference_and_channels():
    with pytest.raises(ValueError):
        _lts(reference_kind='unknown')
    with pytest.raises(ValueError):
        _lts(measurement_channels=())
    with pytest.raises(ValueError):
        _lts(reference_channel='')


def test_observation_requires_estimator_semantics():
    with pytest.raises(ValueError):
        _dto(session_ref=None)
    with pytest.raises(ValueError):
        _dto(estimator='unknown')
    with pytest.raises(ValueError):
        _dto(mtw_method='unknown')
    with pytest.raises(ValueError):
        _dto(averaging='unknown')


def test_qualified_capture_requires_pinned_state():
    with pytest.raises(ValueError, match='delay_ref'):
        _dto(capture_state='qualified_capture')
    obs = _dto(
        capture_state='qualified_capture',
        delay_ref=_ref('d-1'),
        coherence_ref=_ref('c-1'),
        timebase_ref=_ref('t-1'),
        stimulus_state_ref=_ref('s-1'),
    )
    assert obs.capture_state == 'qualified_capture'


def test_coherence_preserves_raw_trace():
    with pytest.raises(ValueError):
        _coh(observation_ref=None)
    with pytest.raises(ValueError):
        _coh(raw_trace_ref=None)


def test_delay_track_requires_method():
    with pytest.raises(ValueError):
        _rdt(session_ref=None)
    with pytest.raises(ValueError):
        _rdt(method='unknown')


def test_array_geometry_requires_members_and_topology():
    with pytest.raises(ValueError):
        _mag(topology='unknown')
    with pytest.raises(ValueError):
        _mag(member_refs=(_ref('mic-1'),),
             member_positions_m=((0.0, 0.0, 1.2),))
    with pytest.raises(ValueError):
        _mag(member_positions_m=((0.0, 0.0, 1.2),))


def test_sampling_capability_requires_band_states():
    with pytest.raises(ValueError):
        _ssc(array_ref=None)
    with pytest.raises(ValueError):
        _ssc(sync_capability='unknown')
    with pytest.raises(ValueError):
        _ssc(propagation_model='unknown')
    with pytest.raises(ValueError):
        _ssc(band_capabilities=())


def test_beamforming_transform_requires_method_and_psf():
    with pytest.raises(ValueError):
        _bft(array_ref=None)
    with pytest.raises(ValueError):
        _bft(capability_ref=None)
    with pytest.raises(ValueError):
        _bft(algorithm='unknown')
    with pytest.raises(ValueError):
        _bft(steering_model='unknown')
    with pytest.raises(ValueError, match='point-spread'):
        _bft(psf_evidence_ref=None)


def test_impedance_profile_target_and_method():
    with pytest.raises(ValueError):
        _zmp(target_kind='unknown')
    with pytest.raises(ValueError, match='different domain'):
        _zmp(target_kind='active_device_input')
    with pytest.raises(ValueError):
        _zmp(method='unknown')


def test_calibration_reference_load_needs_measured_value():
    with pytest.raises(ValueError):
        _zcs(profile_ref=None)
    with pytest.raises(ValueError):
        _zcs(steps=())
    with pytest.raises(ValueError, match='uncertainty'):
        _zcs(reference_load_uncertainty_ohms=None)


def test_load_evidence_requires_trace_and_calibration():
    with pytest.raises(ValueError):
        _zle(profile_ref=None)
    with pytest.raises(ValueError):
        _zle(trace_ref=None)
    with pytest.raises(ValueError, match='phase'):
        _zle(phase_trace_ref=None)
    with pytest.raises(ValueError, match='calibration'):
        _zle(calibration_ref=None)
    with pytest.raises(ValueError):
        _zle(signal_domain='unknown')


def test_ts_derivation_requires_method_inputs():
    with pytest.raises(ValueError):
        _tsd(load_evidence_ref=None)
    with pytest.raises(ValueError):
        _tsd(method='unknown')
    with pytest.raises(ValueError, match='mass'):
        _tsd(added_mass_uncertainty_g=None)
    with pytest.raises(ValueError, match='volume'):
        _tsd(method='sealed_box', added_mass_g=None,
             added_mass_uncertainty_g=None, sealed_volume_l=None)


# -- evaluators --------------------------------------------------------


def test_adaptive_claim_ladder():
    p, s, e = _aam(), _asm(), _ate()
    assert evaluate_adaptive_claim(p, s, None)[0] == 'no_estimate'
    assert evaluate_adaptive_claim(None, s, e)[0] == (
        'unqualified_method'
    )
    e_chg = _ate(convergence_state='system_changed_during_adaptation',
                 stationarity_ref=_ref('st-1'))
    assert evaluate_adaptive_claim(p, s, e_chg)[0] == (
        'stationarity_violated'
    )
    e_nc = _ate(convergence_state='not_converged')
    assert evaluate_adaptive_claim(p, s, e_nc)[0] == 'not_converged'
    e_clk = _ate(clock_qualification='async_uncorrected')
    assert evaluate_adaptive_claim(p, s, e_clk)[0] == 'clock_limited'
    s_clip = _asm(defect_state='stimulus_clipped')
    assert evaluate_adaptive_claim(p, s_clip, e)[0] == (
        'stimulus_defect_limited'
    )
    e_band = _ate(excitation_support=(
        dict(band_hz=(80.0, 8000.0), state='identified'),
        dict(band_hz=(20.0, 80.0), state='insufficient_excitation'),
    ))
    assert evaluate_adaptive_claim(p, s, e_band)[0] == (
        'band_limited_estimate'
    )
    assert evaluate_adaptive_claim(p, s, e)[0] == (
        'qualified_adaptive_estimate'
    )


def test_capture_claim_ladder():
    sess, obs, d = _lts(), _dto(), _rdt()
    assert evaluate_capture_claim(sess, None, d)[0] == 'ephemeral_only'
    assert evaluate_capture_claim(None, obs, d)[0] == 'ephemeral_only'
    o_prov = _dto(estimator='provider_defined')
    assert evaluate_capture_claim(sess, o_prov, d)[0] == (
        'estimator_unqualified'
    )
    s_async = _lts(clock_topology='async_uncorrected')
    assert evaluate_capture_claim(s_async, obs, d)[0] == (
        'clock_limited'
    )
    d_trk = _rdt(method='auto_tracked',
                 masked_change='device_latency_change')
    assert evaluate_capture_claim(sess, obs, d_trk)[0] == (
        'tracker_masked_change'
    )
    o_live = _dto(capture_state='live_ephemeral')
    assert evaluate_capture_claim(sess, o_live, d)[0] == (
        'ephemeral_only'
    )
    assert evaluate_capture_claim(sess, obs, None)[0] == (
        'delay_unqualified'
    )
    o_qual = _dto(capture_state='qualified_capture',
                  delay_ref=_ref('d-1'), coherence_ref=_ref('c-1'),
                  timebase_ref=_ref('t-1'),
                  stimulus_state_ref=_ref('s-1'))
    assert evaluate_capture_claim(sess, o_qual, d)[0] == (
        'qualified_capture'
    )
    assert evaluate_capture_claim(sess, obs, d)[0] == (
        'captured_evidence'
    )


def test_spatial_claim_ladder():
    g, c, t = _mag(), _ssc(), _bft()
    assert evaluate_spatial_claim(g, c, None)[0] == 'no_transform'
    assert evaluate_spatial_claim(None, c, t)[0] == (
        'unsupported_output'
    )
    c_mag = _ssc(sync_capability='magnitude_only_array')
    assert evaluate_spatial_claim(g, c_mag, t)[0] == (
        'sync_insufficient'
    )
    c_nf = _ssc(propagation_model='near_field_point_source')
    assert evaluate_spatial_claim(g, c_nf, t)[0] == 'model_mismatch'
    c_alias = _ssc(band_capabilities=(
        dict(band_hz=(500.0, 3400.0), state='well_sampled'),
        dict(band_hz=(3400.0, 8000.0), state='spatial_aliasing_risk'),
    ))
    assert evaluate_spatial_claim(g, c_alias, t)[0] == (
        'spatial_alias_ambiguous'
    )
    c_low = _ssc(band_capabilities=(
        dict(band_hz=(40.0, 200.0),
             state='low_frequency_resolution_limited'),
    ))
    assert evaluate_spatial_claim(g, c_low, t)[0] == 'aperture_limited'
    t_multi = _bft(output_state='multiple_paths_unresolved')
    assert evaluate_spatial_claim(g, c, t_multi)[0] == (
        'multiple_paths_unresolved'
    )
    assert evaluate_spatial_claim(g, c, t)[0] == 'direction_supported'


def test_load_claim_ladder():
    p, c, e, d = _zmp(), _zcs(), _zle(), _tsd()
    assert evaluate_load_claim(p, c, None)[0] == 'no_evidence'
    assert evaluate_load_claim(None, c, e)[0] == 'unqualified_profile'
    assert evaluate_load_claim(p, None, e)[0] == 'calibration_missing'
    p_full = _zmp(target_kind='complete_passive_loudspeaker_terminals')
    assert evaluate_load_claim(p_full, c, e, d)[0] == (
        'derivation_ineligible_target'
    )
    d_lf = _tsd(model_fit_state='insufficient_low_frequency_data')
    assert evaluate_load_claim(p, c, e, d_lf)[0] == (
        'insufficient_low_frequency_data'
    )
    d_lim = _tsd(model_fit_state='model_fit_limited')
    assert evaluate_load_claim(p, c, e, d_lim)[0] == 'model_fit_limited'
    e_ls = _zle(signal_domain='large_signal')
    assert evaluate_load_claim(p, c, e_ls)[0] == 'state_limited'
    assert evaluate_load_claim(p, c, e)[0] == 'qualified_load_evidence'
    assert evaluate_load_claim(p, c, e, d)[0] == (
        'qualified_load_evidence'
    )


# -- repository --------------------------------------------------------


def test_roundtrip_all_fifteen(tmp_path):
    repo = _repo(tmp_path)
    recs = (_aam(), _asm(), _ate(), _are(), _lts(), _dto(), _coh(),
            _rdt(), _mag(), _ssc(), _bft(), _zmp(), _zcs(), _zle(),
            _tsd())
    repo.save_identification_profile(recs[0])
    repo.save_stimulus_measurement(recs[1])
    repo.save_transfer_estimate(recs[2])
    repo.save_residual_evidence(recs[3])
    repo.save_live_session(recs[4])
    repo.save_tf_observation(recs[5])
    repo.save_coherence_observation(recs[6])
    repo.save_delay_track(recs[7])
    repo.save_array_geometry(recs[8])
    repo.save_sampling_capability(recs[9])
    repo.save_beamforming_transform(recs[10])
    repo.save_impedance_profile(recs[11])
    repo.save_impedance_calibration(recs[12])
    repo.save_load_evidence(recs[13])
    repo.save_ts_derivation(recs[14])
    assert repo.get_identification_profile(
        recs[0].profile_id) == recs[0]
    assert repo.get_stimulus_measurement(
        recs[1].stimulus_id) == recs[1]
    assert repo.get_transfer_estimate(recs[2].estimate_id) == recs[2]
    assert repo.get_residual_evidence(recs[3].residual_id) == recs[3]
    assert repo.get_live_session(recs[4].session_id) == recs[4]
    assert repo.get_tf_observation(recs[5].observation_id) == recs[5]
    assert repo.get_coherence_observation(
        recs[6].coherence_id) == recs[6]
    assert repo.get_delay_track(recs[7].track_id) == recs[7]
    assert repo.get_array_geometry(recs[8].geometry_id) == recs[8]
    assert repo.get_sampling_capability(
        recs[9].capability_id) == recs[9]
    assert repo.get_beamforming_transform(
        recs[10].transform_id) == recs[10]
    assert repo.get_impedance_profile(recs[11].profile_id) == recs[11]
    assert repo.get_impedance_calibration(
        recs[12].calibration_id) == recs[12]
    assert repo.get_load_evidence(recs[13].evidence_id) == recs[13]
    assert repo.get_ts_derivation(recs[14].derivation_id) == recs[14]


def test_tamper_detected(tmp_path):
    repo = _repo(tmp_path)
    e = _zle()
    repo.save_load_evidence(e)
    import sqlite3
    with sqlite3.connect(repo.path) as c:
        c.execute(
            'UPDATE cad_measured_load_evidence SET evidence_class=? '
            'WHERE evidence_id=?',
            ('measured_magnitude_only_load', e.evidence_id)
        )
    with pytest.raises(FieldMetrologyIntegrityError):
        repo.get_load_evidence(e.evidence_id)


def test_fresh_migrate(tmp_path):
    db = tmp_path / 'fresh.htdtscene'
    version = ensure_native_schema(db)
    assert version == 70
    repo = CadFieldMetrologyRepository(SceneRepository(db))
    p = _aam()
    repo.save_identification_profile(p)
    assert repo.get_identification_profile(p.profile_id) == p
