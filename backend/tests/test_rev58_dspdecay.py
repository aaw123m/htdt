"""REV58-DSPDECAY regression tests — #679 DSP filter-realization
authority, #676 decay-curve noise/truncation processing authority,
#705 acoustic-impedance physical-realizability gate.

Fixtures follow the issues' suggested sets (DSR10–DSR90, DEC10–DEC90,
ABI10–ABI100): every verdict is derived fail-closed — a nominal PEQ
readback never becomes transfer verification, a fitted scalar never
hides its eligibility state, and a negative resistive part is never
clipped.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_dsp_realization import (
    CadDspFilterBank,
    CadDspParameterGrid,
    build_dsp_parameter_mapping,
    build_dsp_realization_profile,
    build_dsp_stage_record,
    evaluate_dsp_realization,
)
from htdt.cad_dsp_realization_repository import (
    CadDspRealizationRepository,
    DspRealizationConflictError,
    DspRealizationIntegrityError,
)
from htdt.cad_decay_processing import (
    CadDecayBandSpec,
    CadDecayFitWindow,
    build_decay_edc_artifact,
    build_decay_noise_estimate,
    build_decay_processing_profile,
    build_rir_truncation_decision,
    evaluate_decay_fit,
)
from htdt.cad_decay_processing_repository import (
    CadDecayProcessingRepository,
    DecayProcessingConflictError,
    DecayProcessingIntegrityError,
)
from htdt.cad_boundary_realizability import (
    CadBoundaryUncertaintyBreakdown,
    CadRationalPole,
    build_boundary_evidence_record,
    build_boundary_rational_fit,
    build_td_impedance_realization,
    evaluate_boundary_realizability,
)
from htdt.cad_boundary_realizability_repository import (
    BoundaryRealizabilityConflictError,
    BoundaryRealizabilityIntegrityError,
    CadBoundaryRealizabilityRepository,
)


DOC = 'doc-rev58-dspdecay'
T0 = '2026-10-06T00:00:00+00:00'
T1 = '2026-10-06T01:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64
SHA_D = 'd' * 64
SHA_E = 'e' * 64

DESIGN_REF = AuthorityRef(
    kind='filter_design', ref_id='design-1', ref_sha256=SHA_A
)
RIR_REF = AuthorityRef(
    kind='impulse_response', ref_id='rir-1', ref_sha256=SHA_B
)
SNAPSHOT_REF = AuthorityRef(
    kind='device_snapshot', ref_id='snap-1', ref_sha256=SHA_C
)


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


# ---------------------------------------------------------------------------
# #679 — DSP filter realization (DSR fixtures)
# ---------------------------------------------------------------------------


def _exact_peq_bank() -> CadDspFilterBank:
    return CadDspFilterBank(
        bank_label='peq',
        family='peq_biquad',
        max_filter_count=16,
        parameter_convention='bristow_johnson_q',
        parameter_grids=(
            CadDspParameterGrid(
                parameter='frequency_hz', unit='hz',
                minimum=20.0, maximum=20000.0,
                convention='vendor_defined',
                rounding_rule='exact_no_rounding',
            ),
            CadDspParameterGrid(
                parameter='gain_db', unit='db',
                minimum=-24.0, maximum=24.0,
                convention='vendor_defined',
                rounding_rule='exact_no_rounding',
            ),
            CadDspParameterGrid(
                parameter='q_factor', unit='q',
                minimum=0.5, maximum=25.0,
                convention='bristow_johnson_q',
                rounding_rule='exact_no_rounding',
            ),
        ),
        coefficient_exposure='exact_coefficients_exposed',
        coefficient_format='float64',
    )


def _coarse_peq_bank() -> CadDspFilterBank:
    return CadDspFilterBank(
        bank_label='peq',
        family='peq_biquad',
        max_filter_count=10,
        parameter_convention='bristow_johnson_q',
        parameter_grids=(
            CadDspParameterGrid(
                parameter='frequency_hz', unit='hz',
                minimum=20.0, maximum=500.0, step=1.0,
                convention='vendor_defined',
                rounding_rule='nearest_step',
            ),
            CadDspParameterGrid(
                parameter='gain_db', unit='db',
                minimum=-12.0, maximum=6.0, step=0.5,
                convention='vendor_defined',
                rounding_rule='nearest_step',
            ),
            CadDspParameterGrid(
                parameter='q_factor', unit='q',
                minimum=0.5, maximum=10.0, step=0.1,
                convention='bristow_johnson_q',
                rounding_rule='nearest_step',
            ),
        ),
        coefficient_exposure='quantized_coefficients_exposed',
        coefficient_format='float32',
    )


def _opaque_bank() -> CadDspFilterBank:
    return CadDspFilterBank(
        bank_label='avr_peq',
        family='peq_biquad',
        max_filter_count=9,
        parameter_convention='vendor_defined',
        coefficient_exposure='nominal_parameters_only',
        coefficient_format='proprietary_hidden',
    )


def _profile(**overrides):
    kwargs = dict(
        document_id=DOC,
        device_identity='open-dsp-ref',
        firmware_identity='fw-2.1',
        dsp_engine_label='engine-a',
        sample_rates_hz=(48000.0, 96000.0),
        filter_banks=(_exact_peq_bank(),),
        processing_order_state='declared',
        processing_order=('peq', 'channel_trim'),
        internal_resampling='none_declared',
        provider_provenance='open_source_inspection',
        device_snapshot_ref=SNAPSHOT_REF,
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_dsp_realization_profile(**kwargs)


def test_dsp_profile_seals_and_requires_declared_rate() -> None:
    profile = _profile()
    assert profile.profile_id.startswith('dsppro-')
    assert profile.bank_for('peq') is not None
    assert profile.bank_for('nope') is None

    with pytest.raises(ValueError):
        _profile(sample_rates_hz=())
    with pytest.raises(ValueError):
        _profile(filter_banks=())

    # Re-validating a tampered payload fails the seal check.
    from htdt.cad_dsp_realization import CadDspRealizationProfile

    forged = profile.model_copy(update={'device_identity': 'forged'})
    with pytest.raises(ValueError):
        CadDspRealizationProfile(**forged.model_dump())


def test_dsr10_exact_grid_maps_without_residual() -> None:
    """DSR10 — exact-capability device: the deterministic map produces
    zero deltas and the transfer verifies through a predicted stage."""
    profile = _profile()
    mapping = build_dsp_parameter_mapping(
        document_id=DOC,
        profile=profile,
        bank_label='peq',
        requests=(
            ('frequency_hz', 42.37),
            ('gain_db', -4.17),
            ('q_factor', 4.13),
        ),
        requested_sample_rate_hz=48000.0,
        declared_at_utc=T0,
    )
    assert mapping.state == 'exact'
    assert all(e.verdict == 'exact' for e in mapping.entries)
    assert all(e.delta == 0.0 for e in mapping.entries)

    requested = build_dsp_stage_record(
        document_id=DOC,
        stage_kind='requested_device_state',
        bank_label='peq',
        design_ref=DESIGN_REF,
        active_sample_rate_hz=48000.0,
        state_summary_json='{"f":42.37,"g":-4.17,"q":4.13}',
        declared_at_utc=T0,
    )
    predicted = build_dsp_stage_record(
        document_id=DOC,
        stage_kind='predicted_realized_transfer',
        bank_label='peq',
        parent_stage_ref=requested,
        active_sample_rate_hz=48000.0,
        state_summary_json='{"residual_db":0.0}',
        state_content_sha256=SHA_D,
        declared_at_utc=T0,
    )
    verdict = evaluate_dsp_realization(
        document_id=DOC,
        profile=profile,
        design_ref=DESIGN_REF,
        requested_stage=requested,
        predicted_stage=predicted,
        mapping=mapping,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'realized_within_declared_model'
    assert verdict.transfer_verification == (
        'transfer_realization_verified'
    )


def test_dsr20_coarse_grid_declares_deltas() -> None:
    """DSR20 — coarse frequency/Q grid: the mapping records every delta,
    and the qualification reports declared approximation."""
    profile = _profile(filter_banks=(_coarse_peq_bank(),))
    mapping = build_dsp_parameter_mapping(
        document_id=DOC,
        profile=profile,
        bank_label='peq',
        requests=(
            ('frequency_hz', 42.37),
            ('gain_db', -4.17),
            ('q_factor', 4.13),
        ),
        requested_sample_rate_hz=48000.0,
        declared_at_utc=T0,
    )
    assert mapping.state == 'mapped_with_declared_deltas'
    by_param = {e.parameter: e for e in mapping.entries}
    assert by_param['frequency_hz'].deployable_value == 42.0
    assert by_param['gain_db'].deployable_value == -4.0
    assert by_param['q_factor'].deployable_value == pytest.approx(4.1)
    assert by_param['frequency_hz'].delta == pytest.approx(-0.37)

    verdict = evaluate_dsp_realization(
        document_id=DOC,
        profile=profile,
        design_ref=DESIGN_REF,
        mapping=mapping,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'realized_with_declared_approximation'
    components = dict(verdict.error_components)
    assert 'dsp_parameter_rounding' in components


def test_dsr40_unsupported_parameter_rejects_never_clamps() -> None:
    """DSR40 — unsupported gain/Q: the export path rejects for
    reoptimization instead of silently clamping +8 dB to +6 dB."""
    profile = _profile(filter_banks=(_coarse_peq_bank(),))
    mapping = build_dsp_parameter_mapping(
        document_id=DOC,
        profile=profile,
        bank_label='peq',
        requests=(
            ('frequency_hz', 60.0),
            ('gain_db', 8.0),   # device max +6 dB
            ('q_factor', 20.0),  # device max Q 10
        ),
        declared_at_utc=T0,
    )
    # The in-range frequency maps exactly; both out-of-range parameters
    # are rejected — a partial failure, never silent clamps.
    assert mapping.state == 'partially_unsupported'
    by_param = {e.parameter: e for e in mapping.entries}
    assert by_param['frequency_hz'].verdict == 'exact'
    for param in ('gain_db', 'q_factor'):
        assert by_param[param].verdict == 'unsupported_rejected'
        assert by_param[param].deployable_value is None

    verdict = evaluate_dsp_realization(
        document_id=DOC,
        profile=profile,
        design_ref=DESIGN_REF,
        mapping=mapping,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'reoptimization_required'


def test_dsr60_wrong_sample_rate_is_incompatible() -> None:
    """DSR60 — coefficients generated at 96 kHz on a profile that only
    covers 48 kHz: the wrong-rate reuse is incompatible, not an
    approximation."""
    profile = _profile(sample_rates_hz=(48000.0,))
    requested = build_dsp_stage_record(
        document_id=DOC,
        stage_kind='requested_device_state',
        design_ref=DESIGN_REF,
        active_sample_rate_hz=96000.0,
        declared_at_utc=T0,
    )
    verdict = evaluate_dsp_realization(
        document_id=DOC,
        profile=profile,
        design_ref=DESIGN_REF,
        requested_stage=requested,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'incompatible'
    components = dict(verdict.error_components)
    assert 'sample_rate_mapping' in components


def test_dsr70_readback_match_is_not_transfer_proof() -> None:
    """DSR70 — readback-only proprietary target: nominal state verified,
    internal realization stays model-limited until measured."""
    profile = _profile(
        filter_banks=(_opaque_bank(),),
        provider_provenance='unknown',
        device_snapshot_ref=SNAPSHOT_REF,
    )
    summary = '{"f":42,"g":-4.0,"q":4.1}'
    requested = build_dsp_stage_record(
        document_id=DOC,
        stage_kind='requested_device_state',
        design_ref=DESIGN_REF,
        active_sample_rate_hz=48000.0,
        state_summary_json=summary,
        declared_at_utc=T0,
    )
    readback = build_dsp_stage_record(
        document_id=DOC,
        stage_kind='observed_readback_state',
        device_snapshot_ref=SNAPSHOT_REF,
        active_sample_rate_hz=48000.0,
        state_summary_json=summary,
        state_content_sha256=SHA_D,
        declared_at_utc=T0,
    )
    verdict = evaluate_dsp_realization(
        document_id=DOC,
        profile=profile,
        design_ref=DESIGN_REF,
        requested_stage=requested,
        readback_stage=readback,
        evaluated_at_utc=T1,
    )
    assert verdict.readback_status == 'device_readback_match'
    assert verdict.transfer_verification == 'nominal_state_match_only'
    # DEVICE_READBACK_MATCH never promotes to TRANSFER_REALIZATION_VERIFIED.
    assert verdict.transfer_verification != (
        'transfer_realization_verified'
    )
    assert verdict.state in (
        'realization_model_limited',
        'realized_with_declared_approximation',
        'unqualified',
    )


def test_dsr80_measured_realized_transfer_verifies() -> None:
    """DSR80 — electrical loopback: a measured realized-transfer stage
    qualifies the realization independent of the nominal readback."""
    profile = _profile()
    measured = build_dsp_stage_record(
        document_id=DOC,
        stage_kind='measured_realized_transfer',
        active_sample_rate_hz=48000.0,
        state_summary_json='{"loopback_residual_db":0.05}',
        state_content_sha256=SHA_E,
        declared_at_utc=T0,
    )
    verdict = evaluate_dsp_realization(
        document_id=DOC,
        profile=profile,
        design_ref=DESIGN_REF,
        measured_stage=measured,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'realized_within_declared_model'
    assert verdict.transfer_verification == (
        'transfer_realization_verified'
    )


def test_dsp_stage_readback_requires_evidence() -> None:
    with pytest.raises(ValueError):
        build_dsp_stage_record(
            document_id=DOC,
            stage_kind='observed_readback_state',
            declared_at_utc=T0,
        )


def test_dsp_repository_roundtrip_and_integrity(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repo = CadDspRealizationRepository(scene_repository)
    profile = _profile()
    repo.save_profile(profile)
    repo.save_profile(profile)  # idempotent
    assert repo.get_profile(profile.profile_id) == profile
    assert repo.list_profiles(DOC) == (profile,)

    stage = build_dsp_stage_record(
        document_id=DOC,
        stage_kind='ideal_design',
        design_ref=DESIGN_REF,
        state_summary_json='{"f":42.37}',
        declared_at_utc=T0,
    )
    repo.save_stage(stage)
    assert repo.get_stage(stage.stage_id) == stage

    mapping = build_dsp_parameter_mapping(
        document_id=DOC,
        profile=profile,
        bank_label='peq',
        requests=(('frequency_hz', 42.37),),
        declared_at_utc=T0,
    )
    repo.save_mapping(mapping)
    assert repo.get_mapping(mapping.mapping_id) == mapping

    verdict = evaluate_dsp_realization(
        document_id=DOC,
        profile=profile,
        design_ref=DESIGN_REF,
        mapping=mapping,
        evaluated_at_utc=T1,
    )
    repo.save_qualification(verdict)
    assert repo.get_qualification(verdict.qualification_id) == verdict

    forged = profile.model_copy(update={'device_identity': 'forged'})
    with pytest.raises(DspRealizationIntegrityError):
        repo.save_profile(forged)

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_dsp_realization_profiles SET device_identity=? '
            'WHERE profile_id=?',
            ('tampered', profile.profile_id),
        )
    with pytest.raises(DspRealizationIntegrityError):
        repo.get_profile(profile.profile_id)


# ---------------------------------------------------------------------------
# #676 — decay-curve noise / truncation processing (DEC fixtures)
# ---------------------------------------------------------------------------


def _decay_profile(**overrides):
    kwargs = dict(
        document_id=DOC,
        profile_label='t30-oct-500-schroeder-corrected',
        band=CadDecayBandSpec(
            band_kind='octave',
            center_hz=500.0,
            band_low_hz=354.0,
            band_high_hz=707.0,
            filter_class='iir_bandpass',
            filter_order=4,
            zero_phase=True,
            sample_rate_hz=48000.0,
            filter_label='oct-500',
        ),
        backward_integration='schroeder_with_noise_correction',
        noise_floor_method='lundeby_style_intersection',
        tail_compensation='declared_model',
        fit_windows=(
            CadDecayFitWindow(
                metric='edt',
                start_level_db=0.0,
                end_level_db=-10.0,
            ),
            CadDecayFitWindow(
                metric='t20',
                start_level_db=-5.0,
                end_level_db=-25.0,
                min_dynamic_range_db=20.0,
            ),
            CadDecayFitWindow(
                metric='t30',
                start_level_db=-5.0,
                end_level_db=-35.0,
                min_dynamic_range_db=30.0,
            ),
        ),
        noise_stationarity_assumption='stationary_assumed',
        measurement_or_simulated='measured',
        algorithm_version='htdt-edc-1.0',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_decay_processing_profile(**kwargs)


def _noise(**overrides):
    kwargs = dict(
        document_id=DOC,
        profile=_decay_profile(),
        rir_ref=RIR_REF,
        estimator='tail_mean_energy',
        method='lundeby_style_intersection',
        tail_start_s=1.8,
        tail_end_s=2.0,
        stationarity='stationary_assumed',
        level_db=-48.0,
        uncertainty_db=1.5,
        method_version='lundeby-1995',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_decay_noise_estimate(**kwargs)


def _truncation(**overrides):
    kwargs = dict(
        document_id=DOC,
        profile=_decay_profile(),
        rir_ref=RIR_REF,
        noise_estimate=_noise(),
        intersection_time_s=1.6,
        truncation_time_s=1.8,
        available_decay_range_db=42.0,
        capture_length_s=2.0,
        reason='noise_intersection',
        confidence='high',
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_rir_truncation_decision(**kwargs)


def _raw_edc(**overrides):
    kwargs = dict(
        document_id=DOC,
        profile=_decay_profile(),
        rir_ref=RIR_REF,
        edc_kind='raw_backward_integral',
        content_sha256=SHA_D,
        sample_count=96000,
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_decay_edc_artifact(**kwargs)


def test_decay_profile_seals_and_bands_require_edges() -> None:
    profile = _decay_profile()
    assert profile.profile_id.startswith('decpro-')
    assert profile.window_for('t30') is not None
    assert profile.window_for('t20').start_level_db == -5.0

    with pytest.raises(ValueError):
        CadDecayBandSpec(band_kind='octave')  # no center/edges
    with pytest.raises(ValueError):
        _decay_profile(backward_integration='unknown')
    with pytest.raises(ValueError):
        _decay_profile(
            backward_integration='schroeder_with_noise_correction',
            noise_floor_method='none_declared',
        )


def test_dec10_clean_decay_is_eligible() -> None:
    """DEC10 — clean exponential decay: the qualified method recovers
    the reference within tolerance and reports eligible."""
    record = evaluate_decay_fit(
        document_id=DOC,
        profile=_decay_profile(),
        rir_ref=RIR_REF,
        metric='t30',
        regression='least_squares_line',
        value_s=0.401,
        edc_artifact=_raw_edc(),
        noise_estimate=_noise(),
        truncation=_truncation(),
        sample_count=5200,
        dynamic_range_db=34.0,
        noise_margin_db=8.0,
        fit_residual_db=0.12,
        evaluated_at_utc=T1,
    )
    assert record.eligibility == 'eligible'
    assert record.value_s == pytest.approx(0.401)
    assert record.fit_start_level_db == -5.0
    assert record.fit_end_level_db == -35.0


def test_dec30_insufficient_dynamic_range_fails_closed() -> None:
    """DEC30 — fitting code can return a number, eligibility rejects it:
    the usable range below the noise floor is too short for T30."""
    record = evaluate_decay_fit(
        document_id=DOC,
        profile=_decay_profile(),
        rir_ref=RIR_REF,
        metric='t30',
        regression='least_squares_line',
        value_s=0.52,
        edc_artifact=_raw_edc(),
        noise_estimate=_noise(),
        truncation=_truncation(available_decay_range_db=18.0),
        dynamic_range_db=18.0,
        noise_margin_db=3.0,
        evaluated_at_utc=T1,
    )
    assert record.eligibility == 'insufficient_decay_range'
    assert record.value_s == pytest.approx(0.52)


def test_dec40_capture_truncated_is_not_noise_limited() -> None:
    """DEC40 — the capture ended before the noise intersection: the
    record says CAPTURE_TRUNCATED, never a processed noise limit."""
    truncation = build_rir_truncation_decision(
        document_id=DOC,
        profile=_decay_profile(),
        rir_ref=RIR_REF,
        truncation_time_s=1.2,
        reason='capture_end',
        capture_truncated=True,
        capture_length_s=1.2,
        confidence='high',
        declared_at_utc=T0,
    )
    record = evaluate_decay_fit(
        document_id=DOC,
        profile=_decay_profile(),
        rir_ref=RIR_REF,
        metric='t30',
        regression='least_squares_line',
        value_s=0.61,
        truncation=truncation,
        evaluated_at_utc=T1,
    )
    assert record.eligibility == 'capture_truncated'

    with pytest.raises(ValueError):
        build_rir_truncation_decision(
            document_id=DOC,
            profile=_decay_profile(),
            rir_ref=RIR_REF,
            truncation_time_s=1.2,
            reason='noise_intersection',
            capture_truncated=True,
            declared_at_utc=T0,
        )


def test_dec50_nonstationary_noise_blocks_stationary_correction() -> None:
    """DEC50 — intermittent late noise: the stationary-noise correction
    must not be applied blindly."""
    record = evaluate_decay_fit(
        document_id=DOC,
        profile=_decay_profile(),
        rir_ref=RIR_REF,
        metric='t20',
        regression='least_squares_line',
        value_s=0.39,
        noise_estimate=_noise(stationarity='nonstationary_detected'),
        truncation=_truncation(),
        dynamic_range_db=24.0,
        evaluated_at_utc=T1,
    )
    assert record.eligibility == 'non_stationary_noise'


def test_dec60_multi_slope_and_dec70_modal_route() -> None:
    """DEC60/DEC70 — multi-slope coupled decay and modal-region decay
    report their dedicated states instead of a misleading single T30."""
    multi = evaluate_decay_fit(
        document_id=DOC,
        profile=_decay_profile(),
        rir_ref=RIR_REF,
        metric='t30',
        regression='least_squares_line',
        value_s=0.44,
        noise_estimate=_noise(),
        truncation=_truncation(),
        dynamic_range_db=33.0,
        noise_margin_db=6.0,
        multi_slope_detected=True,
        evaluated_at_utc=T1,
    )
    assert multi.eligibility == 'multi_slope_model_mismatch'

    modal = evaluate_decay_fit(
        document_id=DOC,
        profile=_decay_profile(
            band=CadDecayBandSpec(
                band_kind='octave',
                center_hz=63.0,
                band_low_hz=45.0,
                band_high_hz=90.0,
                filter_class='iir_bandpass',
                sample_rate_hz=48000.0,
            ),
        ),
        rir_ref=RIR_REF,
        metric='t30',
        regression='least_squares_line',
        noise_estimate=_noise(),
        truncation=_truncation(),
        dynamic_range_db=33.0,
        noise_margin_db=6.0,
        modal_method_required=True,
        evaluated_at_utc=T1,
    )
    assert modal.eligibility == 'modal_method_required'


def test_dec80_two_valid_methods_keep_distinct_records() -> None:
    """DEC80 — two defensible methods produce small nonzero spread; both
    records are retained as evidence, not averaged away."""
    profile_a = _decay_profile(
        profile_label='lundeby-intersection',
        noise_floor_method='lundeby_style_intersection',
    )
    profile_b = _decay_profile(
        profile_label='nonlinear-model',
        noise_floor_method='nonlinear_decay_plus_noise_model',
        backward_integration='schroeder_with_tail_compensation',
    )
    record_a = evaluate_decay_fit(
        document_id=DOC,
        profile=profile_a,
        rir_ref=RIR_REF,
        metric='t30',
        regression='least_squares_line',
        value_s=0.401,
        noise_estimate=_noise(profile=profile_a),
        truncation=_truncation(profile=profile_a),
        dynamic_range_db=34.0,
        noise_margin_db=8.0,
        evaluated_at_utc=T1,
    )
    record_b = evaluate_decay_fit(
        document_id=DOC,
        profile=profile_b,
        rir_ref=RIR_REF,
        metric='t30',
        regression='nonlinear_decay_model',
        value_s=0.413,
        noise_estimate=_noise(
            profile=profile_b,
            method='nonlinear_decay_plus_noise_model',
        ),
        truncation=_truncation(profile=profile_b),
        dynamic_range_db=36.0,
        noise_margin_db=10.0,
        evaluated_at_utc=T1,
    )
    assert record_a.eligibility == 'eligible'
    assert record_b.eligibility == 'eligible'
    assert record_a.record_id != record_b.record_id
    assert record_a.value_s != record_b.value_s


def test_dec90_simulated_tail_truncation_is_distinct_cause() -> None:
    """DEC90 — a ray-tracer finite tail is solver truncation, not
    measured background noise: the cause classes stay separate."""
    profile = _decay_profile(
        profile_label='ray-sim-t30',
        measurement_or_simulated='simulated',
        solver_truncation_declared='solver_time_or_order_truncation',
        noise_floor_method='none_declared',
        backward_integration='schroeder_reverse_cumulative',
    )
    assert profile.solver_truncation_declared == (
        'solver_time_or_order_truncation'
    )
    record = evaluate_decay_fit(
        document_id=DOC,
        profile=profile,
        rir_ref=AuthorityRef(
            kind='simulated_impulse_response',
            ref_id='sim-ir-1',
            ref_sha256=SHA_E,
        ),
        metric='t30',
        regression='least_squares_line',
        value_s=0.38,
        evaluated_at_utc=T1,
    )
    assert record.eligibility in (
        'eligible', 'eligible_with_limitations'
    )
    assert record.eligibility != 'noise_floor_too_high'


def test_decay_corrected_edc_derives_never_overwrites() -> None:
    """Corrected EDC is a distinct derived artifact hash-linked to the
    raw backward integral (#676 §5)."""
    raw = _raw_edc()
    corrected = build_decay_edc_artifact(
        document_id=DOC,
        profile=_decay_profile(),
        rir_ref=RIR_REF,
        edc_kind='noise_compensated_edc',
        derived_from_ref=raw,
        content_sha256=SHA_E,
        sample_count=96000,
        compensation='stationary_tail_energy_subtraction',
        compensation_energy_db=-49.0,
        declared_at_utc=T0,
    )
    assert corrected.derived_from_ref is not None
    assert corrected.derived_from_ref.ref_id == raw.artifact_id
    assert corrected.artifact_id != raw.artifact_id

    with pytest.raises(ValueError):
        build_decay_edc_artifact(
            document_id=DOC,
            profile=_decay_profile(),
            rir_ref=RIR_REF,
            edc_kind='noise_compensated_edc',
            content_sha256=SHA_D,
            declared_at_utc=T0,
        )


def test_decay_repository_roundtrip_and_integrity(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repo = CadDecayProcessingRepository(scene_repository)
    profile = _decay_profile()
    repo.save_profile(profile)
    repo.save_profile(profile)  # idempotent
    assert repo.get_profile(profile.profile_id) == profile
    assert repo.list_profiles(DOC) == (profile,)

    noise = _noise(profile=profile)
    repo.save_noise_estimate(noise)
    assert repo.get_noise_estimate(noise.estimate_id) == noise

    truncation = _truncation(profile=profile)
    repo.save_truncation_decision(truncation)
    assert (
        repo.get_truncation_decision(truncation.decision_id)
        == truncation
    )

    raw = _raw_edc(profile=profile)
    repo.save_edc_artifact(raw)
    assert repo.get_edc_artifact(raw.artifact_id) == raw

    record = evaluate_decay_fit(
        document_id=DOC,
        profile=profile,
        rir_ref=RIR_REF,
        metric='t30',
        regression='least_squares_line',
        value_s=0.4,
        noise_estimate=noise,
        truncation=truncation,
        dynamic_range_db=34.0,
        noise_margin_db=8.0,
        evaluated_at_utc=T1,
    )
    repo.save_fit_record(record)
    assert repo.get_fit_record(record.record_id) == record
    assert repo.list_fit_records(DOC) == (record,)

    forged = profile.model_copy(update={'profile_label': 'forged'})
    with pytest.raises(DecayProcessingIntegrityError):
        repo.save_profile(forged)

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_decay_fit_records SET eligibility=? '
            'WHERE record_id=?',
            ('noise_floor_too_high', record.record_id),
        )
    with pytest.raises(DecayProcessingIntegrityError):
        repo.get_fit_record(record.record_id)


# ---------------------------------------------------------------------------
# #705 — acoustic impedance physical realizability (ABI fixtures)
# ---------------------------------------------------------------------------


def _evidence(**overrides):
    kwargs = dict(
        document_id=DOC,
        evidence_label='boundary-z-500-band',
        boundary_class='measured_frequency_domain_impedance',
        quantity_convention='impedance_z',
        normal_direction='into_boundary',
        passivity_class='passive_boundary',
        measured_band_low_hz=100.0,
        measured_band_high_hz=4000.0,
        min_resistive_value=0.12,
        max_reflection_magnitude=0.96,
        uncertainty=CadBoundaryUncertaintyBreakdown(
            resistive_uncertainty=0.05,
            reflection_uncertainty=0.02,
        ),
        interpolation='none',
        extrapolation='not_required',
        conjugate_symmetry='satisfied',
        content_sha256=SHA_D,
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_boundary_evidence_record(**kwargs)


def test_abi10_passive_boundary_with_stable_td() -> None:
    """ABI10 — passive resistive boundary + verified TD realization:
    energy decays, no growth → stable numerical realization."""
    evidence = _evidence(
        boundary_class='parametric_physical_model',
        uncertainty=None,
    )
    td = build_td_impedance_realization(
        document_id=DOC,
        evidence=evidence,
        solver_family='fdtd_impedance_boundary',
        timestep_s=1e-5,
        boundary_update_scheme='locally_reacting_impedance_update',
        integration_method='explicit_euler',
        boundary_stability_margin=0.2,
        solver_band_low_hz=100.0,
        solver_band_high_hz=4000.0,
        fd_magnitude_residual_db=0.3,
        residual_tolerance=0.5,
        energy_growth_observed=False,
        energy_growth_ratio=0.998,
        conjugate_symmetry='satisfied',
        declared_at_utc=T0,
    )
    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=evidence,
        td_realization=td,
        solver_band_low_hz=100.0,
        solver_band_high_hz=4000.0,
        evaluated_at_utc=T1,
    )
    assert assessment.state == 'stable_numerical_realization'
    assert assessment.passivity_class == 'passive_boundary'
    assert assessment.solver_band_within_evidence is True


def test_abi30_small_negative_resistance_unresolved_not_clipped() -> None:
    """ABI30 — slight apparent negative resistance within uncertainty:
    unresolved/review, never clipped to PASS."""
    evidence = _evidence(
        min_resistive_value=-0.02,
        uncertainty=CadBoundaryUncertaintyBreakdown(
            resistive_uncertainty=0.05,
        ),
    )
    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=evidence,
        evaluated_at_utc=T1,
    )
    assert assessment.state == 'passivity_unresolved_with_uncertainty'
    # The stored evidence itself is untouched — min_resistive_value is
    # still negative, not clamped.
    assert evidence.min_resistive_value == -0.02


def test_abi40_clearly_active_boundary_rejected_from_passive_path() -> None:
    """ABI40 — a clearly non-passive input fails the passive path; an
    explicitly declared active-control boundary is classified, not
    admitted."""
    nonpassive = _evidence(
        min_resistive_value=-0.8,
        uncertainty=CadBoundaryUncertaintyBreakdown(
            resistive_uncertainty=0.05,
        ),
    )
    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=nonpassive,
        evaluated_at_utc=T1,
    )
    assert assessment.state == 'nonpassive_input'
    assert assessment.passivity_class == 'nonpassive_unexpected'

    active = _evidence(
        evidence_label='active-lf-control',
        boundary_class='active_control_boundary',
        passivity_class='active_boundary_explicit',
        min_resistive_value=-0.8,
    )
    active_assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=active,
        evaluated_at_utc=T1,
    )
    assert active_assessment.state == 'active_boundary_explicit'

    # An active boundary can never enter through a passive declaration.
    with pytest.raises(ValueError):
        _evidence(
            boundary_class='active_control_boundary',
            passivity_class='passive_boundary',
        )


def test_abi50_unstable_fit_fails_despite_good_fd_residual() -> None:
    """ABI50 — a rational fit with a right-half-plane pole is rejected
    even when the FD residual looked good."""
    evidence = _evidence()
    fit = build_boundary_rational_fit(
        document_id=DOC,
        input_evidence=evidence,
        fit_variable='impedance_z',
        fit_band_low_hz=100.0,
        fit_band_high_hz=4000.0,
        pole_count=2,
        poles=(
            CadRationalPole(re=-120.0, im=850.0),
            CadRationalPole(re=15.0, im=-1200.0),
        ),
        algorithm='vector_fit',
        algorithm_version='vf-3.1',
        stable_pole_constraint='post_fit_check',
        max_residual_db=0.2,
        content_sha256=SHA_E,
        declared_at_utc=T0,
    )
    assert fit.pole_stability() == 'unstable_realization'
    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=evidence,
        rational_fit=fit,
        evaluated_at_utc=T1,
    )
    assert assessment.state == 'unstable_fit'
    assert assessment.stability_state == 'unstable_realization'


def test_abi60_constrained_refit_is_derived_and_passes() -> None:
    """ABI60 — passivity-enforced refit: original evidence preserved,
    derived model passes with its repair delta reported."""
    original = _evidence(
        min_resistive_value=-0.02,
        uncertainty=CadBoundaryUncertaintyBreakdown(
            resistive_uncertainty=0.05,
        ),
    )
    repaired = _evidence(
        evidence_label='passivity-enforced-refit',
        boundary_class='rational_frequency_domain_fit',
        min_resistive_value=0.01,
        derived_from_ref=original,
        interpolation='declared_causal_method',
    )
    assert repaired.derived_from_ref is not None
    assert repaired.derived_from_ref.ref_id == original.record_id

    fit = build_boundary_rational_fit(
        document_id=DOC,
        input_evidence=repaired,
        fit_variable='impedance_z',
        fit_band_low_hz=100.0,
        fit_band_high_hz=4000.0,
        pole_count=2,
        poles=(
            CadRationalPole(re=-120.0, im=850.0),
            CadRationalPole(re=-340.0, im=-1200.0),
        ),
        algorithm='vector_fit_passivity_enforced',
        algorithm_version='vf-3.1',
        stable_pole_constraint='enforced_during_fit',
        passivity_enforcement='repaired_post_fit',
        repair_delta=0.03,
        content_sha256=SHA_A,
        declared_at_utc=T0,
    )
    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=repaired,
        rational_fit=fit,
        evaluated_at_utc=T1,
    )
    assert assessment.state == 'passive_causal_validated'
    assert 'passivity_repair_delta' in assessment.error_components
    # The repair never overwrote the noisy original.
    assert original.min_resistive_value == -0.02


def test_abi70_finite_band_never_global_causality_pass() -> None:
    """ABI70 — finite-band measured data cannot claim global causality:
    the verdict stays unresolved in-band."""
    evidence = _evidence(
        causality_check='consistent_within_band',
        causality_check_band_hz=(100.0, 4000.0),
        out_of_band_assumption='bounded_extrapolation',
    )
    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=evidence,
        evaluated_at_utc=T1,
    )
    assert assessment.state == 'causality_unresolved_finite_band'
    assert assessment.causality_state == (
        'causality_unresolved_finite_band'
    )


def test_abi80_interpolation_artifact_rejected() -> None:
    """ABI80 — independent |Z|/phase splines fabricated |R|>1: the
    transformed boundary is non-passive input, not an approximation."""
    evidence = _evidence(
        boundary_class='rational_frequency_domain_fit',
        max_reflection_magnitude=1.12,
        interpolation='independent_component_splines',
    )
    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=evidence,
        evaluated_at_utc=T1,
    )
    assert assessment.state == 'nonpassive_input'


def test_abi90_solver_timestep_instability_is_td_mismatch() -> None:
    """ABI90 — the boundary model is valid but the FDTD boundary update
    violates its own stability condition: rejected as a realization
    mismatch, separate from free-field CFL (#683 composes)."""
    evidence = _evidence(
        boundary_class='parametric_physical_model',
        uncertainty=None,
    )
    td = build_td_impedance_realization(
        document_id=DOC,
        evidence=evidence,
        solver_family='fdtd',
        timestep_s=2e-5,
        boundary_stability_margin=-0.1,
        boundary_stability_criterion='toyoda_2018_boundary_cell',
        energy_growth_observed=True,
        energy_growth_ratio=1.04,
        declared_at_utc=T0,
    )
    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=evidence,
        td_realization=td,
        evaluated_at_utc=T1,
    )
    assert assessment.state == 'time_domain_realization_mismatch'
    assert 'solver_discretization' in assessment.error_components


def test_abi100_fd_td_crosscheck_residual_retained() -> None:
    """ABI100 — the converted TD realization keeps its FD residual
    envelope; exceeding the declared tolerance fails closed."""
    evidence = _evidence(
        boundary_class='parametric_physical_model',
        uncertainty=None,
    )
    td_bad = build_td_impedance_realization(
        document_id=DOC,
        evidence=evidence,
        solver_family='td_dg',
        fd_magnitude_residual_db=1.8,
        residual_tolerance=0.5,
        energy_growth_observed=False,
        declared_at_utc=T0,
    )
    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=evidence,
        td_realization=td_bad,
        evaluated_at_utc=T1,
    )
    assert assessment.state == 'time_domain_realization_mismatch'


def test_boundary_solver_band_beyond_evidence_is_limited() -> None:
    """A solver band wider than the evidence/fit band degrades to a
    declared limitation instead of silent extrapolation (#705 §12)."""
    evidence = _evidence()
    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=evidence,
        solver_band_low_hz=20.0,
        solver_band_high_hz=20000.0,
        evaluated_at_utc=T1,
    )
    assert assessment.solver_band_within_evidence is False
    assert 'out_of_band_assumption' in assessment.error_components


def test_boundary_repository_roundtrip_and_integrity(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repo = CadBoundaryRealizabilityRepository(scene_repository)
    evidence = _evidence()
    repo.save_evidence(evidence)
    repo.save_evidence(evidence)  # idempotent
    assert repo.get_evidence(evidence.record_id) == evidence
    assert repo.list_evidence(DOC) == (evidence,)

    fit = build_boundary_rational_fit(
        document_id=DOC,
        input_evidence=evidence,
        fit_variable='impedance_z',
        fit_band_low_hz=100.0,
        fit_band_high_hz=4000.0,
        pole_count=1,
        poles=(CadRationalPole(re=-200.0, im=500.0),),
        algorithm='vector_fit',
        algorithm_version='vf-3.1',
        stable_pole_constraint='enforced_during_fit',
        content_sha256=SHA_E,
        declared_at_utc=T0,
    )
    repo.save_rational_fit(fit)
    assert repo.get_rational_fit(fit.fit_id) == fit

    td = build_td_impedance_realization(
        document_id=DOC,
        evidence=evidence,
        solver_family='td_dg',
        timestep_s=1e-5,
        declared_at_utc=T0,
    )
    repo.save_td_realization(td)
    assert repo.get_td_realization(td.realization_id) == td

    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=evidence,
        rational_fit=fit,
        td_realization=td,
        evaluated_at_utc=T1,
    )
    repo.save_assessment(assessment)
    assert repo.get_assessment(assessment.assessment_id) == assessment
    assert repo.list_assessments(DOC) == (assessment,)

    forged = evidence.model_copy(
        update={'min_resistive_value': -0.5}
    )
    with pytest.raises(BoundaryRealizabilityIntegrityError):
        repo.save_evidence(forged)

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_boundary_realizability_assessments SET state=? '
            'WHERE assessment_id=?',
            ('passive_causal_validated', assessment.assessment_id),
        )
    with pytest.raises(BoundaryRealizabilityIntegrityError):
        repo.get_assessment(assessment.assessment_id)


def test_append_only_tables_reject_forged_payloads(tmp_path) -> None:
    """A tampered payload fails seal re-verification on save across all
    three repositories — append-only never silently overwrites."""
    scene_repository = _scene_repo(tmp_path)
    dsp_repo = CadDspRealizationRepository(scene_repository)
    decay_repo = CadDecayProcessingRepository(scene_repository)
    boundary_repo = CadBoundaryRealizabilityRepository(scene_repository)

    profile = _profile()
    dsp_repo.save_profile(profile)
    forged = profile.model_copy(update={'firmware_identity': 'fw-9'})
    with pytest.raises(DspRealizationIntegrityError):
        dsp_repo.save_profile(forged)

    decay_profile = _decay_profile()
    decay_repo.save_profile(decay_profile)
    forged_decay = decay_profile.model_copy(
        update={'algorithm_version': 'htdt-edc-9.9'}
    )
    with pytest.raises(DecayProcessingIntegrityError):
        decay_repo.save_profile(forged_decay)

    evidence = _evidence()
    boundary_repo.save_evidence(evidence)
    forged_evidence = evidence.model_copy(
        update={'min_resistive_value': -0.01}
    )
    with pytest.raises(BoundaryRealizabilityIntegrityError):
        boundary_repo.save_evidence(forged_evidence)


def test_display_lines_report_component_states() -> None:
    """The JA display lines keep the layered verdicts visible."""
    from htdt.measurement_evidence_display import (
        boundary_realizability_line,
        decay_fit_line,
        dsp_realization_line,
    )

    verdict = evaluate_dsp_realization(
        document_id=DOC,
        profile=_profile(filter_banks=(_opaque_bank(),)),
        design_ref=DESIGN_REF,
        evaluated_at_utc=T1,
    )
    line = dsp_realization_line(verdict)
    assert 'DSP 実現適格' in line
    assert '読み戻し' in line and '伝達関数' in line

    record = evaluate_decay_fit(
        document_id=DOC,
        profile=_decay_profile(),
        rir_ref=RIR_REF,
        metric='t30',
        regression='least_squares_line',
        value_s=0.4,
        noise_estimate=_noise(),
        truncation=_truncation(),
        dynamic_range_db=34.0,
        noise_margin_db=8.0,
        evaluated_at_utc=T1,
    )
    line = decay_fit_line(record)
    assert '減衰フィット' in line
    assert 'T30' in line

    assessment = evaluate_boundary_realizability(
        document_id=DOC,
        evidence=_evidence(),
        evaluated_at_utc=T1,
    )
    line = boundary_realizability_line(assessment)
    assert '境界実現性' in line
    assert '受動' in line and '因果' in line and '安定' in line
