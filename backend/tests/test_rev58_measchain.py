"""REV58-MEASCHAIN regression tests — #695 measurement-chain
linearity/overload authority, #697 swept-sine deconvolution /
harmonic-separation authority, #668 room-acoustic excitation-source
authority."""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_measchain_linearity import (
    CadAcquisitionStage,
    CadDynamicProcessingBlock,
    CadLinearityBand,
    CadTwoLevelCheck,
    CadUpperLevelSpec,
    build_measchain_profile,
    build_overload_observation,
    evaluate_measchain_qualification,
)
from htdt.cad_measchain_linearity_repository import (
    CadMeasChainLinearityRepository,
    MeasChainAuthorityConflictError,
    MeasChainAuthorityIntegrityError,
)
from htdt.cad_sweep_deconvolution import (
    CadExtractionWindow,
    build_deconvolution_spec,
    build_harmonic_component,
    build_recovered_ir,
    evaluate_linear_ir_capability,
)
from htdt.cad_sweep_deconvolution_repository import (
    CadSweepDeconvolutionRepository,
    SweepDeconvAuthorityConflictError,
    SweepDeconvAuthorityIntegrityError,
)
from htdt.cad_excitation_source import (
    CadOmniBandCapability,
    CadSourceLevelCapability,
    CadSourcePose,
    build_excitation_profile,
    build_orientation_capture,
    evaluate_source_qualification,
)
from htdt.cad_excitation_source_repository import (
    CadExcitationSourceRepository,
    ExcitationAuthorityConflictError,
    ExcitationAuthorityIntegrityError,
)


DOC = 'doc-rev58-measchain'
T0 = '2026-10-06T00:00:00+00:00'
T1 = '2026-10-06T01:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64
SHA_D = 'd' * 64

STIMULUS_REF = AuthorityRef(
    kind='stimulus_signal', ref_id='stim-ess-1', ref_sha256=SHA_A
)
CALIBRATION_REF = AuthorityRef(
    kind='calibration_event', ref_id='cal-1', ref_sha256=SHA_B
)
CAPTURE_REF = AuthorityRef(
    kind='measurement_capture', ref_id='cap-1', ref_sha256=SHA_C
)


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


# ---------------------------------------------------------------------------
# #695 — measurement-chain linearity / overload
# ---------------------------------------------------------------------------


def _stages():
    return (
        CadAcquisitionStage(
            stage_kind='microphone_capsule',
            device_identity='mic-1',
            model_or_label='half-inch measurement capsule',
            output_headroom_db=30.0,
            overload_indicator='unavailable',
        ),
        CadAcquisitionStage(
            stage_kind='external_preamp_conditioning',
            device_identity='preamp-1',
            gain_db=20.0,
            overload_indicator='unavailable',
        ),
        CadAcquisitionStage(
            stage_kind='adc',
            device_identity='iface-1',
            overload_indicator='asserted',
        ),
        CadAcquisitionStage(
            stage_kind='driver_acquisition_software',
            device_identity='driver-1',
            overload_indicator='unknown',
        ),
    )


def _upper_level_spec():
    return CadUpperLevelSpec(
        applies_to_stage_index=0,
        level_db=135.0,
        level_unit='db_spl',
        threshold_kind='thd_3pct',
        thd_percent=3.0,
        level_semantics='peak',
        signal_class='sinusoidal',
        band_low_hz=20.0,
        band_high_hz=20000.0,
        evidence_basis='manufacturer_specification',
        evidence_source='mic-1 datasheet 3% THD max SPL',
    )


def _linearity_band():
    return CadLinearityBand(
        band_low_hz=20.0,
        band_high_hz=20000.0,
        max_linear_level_db=135.0,
        level_unit='db_spl',
        evidence_basis='laboratory_measurement',
        evidence_source='lab two-tone check',
        uncertainty_db=0.5,
    )


def _profile(**overrides):
    kwargs = dict(
        document_id=DOC,
        chain_label='mic→preamp→adc',
        stages=_stages(),
        upper_level_specs=(_upper_level_spec(),),
        linearity_bands=(_linearity_band(),),
        calibration_ref=CALIBRATION_REF,
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_measchain_profile(**kwargs)


def test_measchain_profile_seals_and_stages_must_be_ordered() -> None:
    profile = _profile()
    assert profile.profile_id.startswith('mchain-')
    assert len(profile.profile_sha256) == 64

    with pytest.raises(ValueError, match='canonical'):
        _profile(
            stages=(
                CadAcquisitionStage(stage_kind='adc'),
                CadAcquisitionStage(stage_kind='microphone_capsule'),
            )
        )


def test_measchain_stage_index_bounds_checked() -> None:
    with pytest.raises(ValueError, match='points outside the stage graph'):
        _profile(
            upper_level_specs=(
                CadUpperLevelSpec(
                    applies_to_stage_index=99,
                    level_db=100.0,
                    level_unit='db_spl',
                    threshold_kind='clip_point',
                    evidence_basis='manufacturer_specification',
                ),
            )
        )


def test_measchain_calibration_ref_must_carry_sha() -> None:
    with pytest.raises(ValueError, match='sha256'):
        _profile(
            calibration_ref=AuthorityRef(
                kind='calibration_event', ref_id='cal-1'
            )
        )


def test_overload_observation_requires_evidence() -> None:
    profile = _profile()
    with pytest.raises(ValueError, match='requires some evidence'):
        build_overload_observation(
            document_id=DOC,
            chain_ref=profile,
            overload_mechanism='no_overload_observed',
            declared_at_utc=T0,
        )
    with pytest.raises(ValueError, match='localization evidence'):
        build_overload_observation(
            document_id=DOC,
            chain_ref=profile,
            overload_mechanism='analog_front_end_clip',
            declared_at_utc=T0,
        )


def test_confirmed_overload_fails_every_capability() -> None:
    profile = _profile()
    observation = build_overload_observation(
        document_id=DOC,
        chain_ref=profile,
        overload_mechanism='adc_numeric_full_scale',
        overload_stage_index=2,
        sample_peak_dbfs=0.0,
        peak_semantics='peak',
        declared_at_utc=T1,
    )
    verdict = evaluate_measchain_qualification(
        document_id=DOC,
        profile=profile,
        observation=observation,
        requested_class='high_level_spl',
        requested_band_low_hz=200.0,
        requested_band_high_hz=8000.0,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'overload_observed'
    assert verdict.distortion_attribution == 'chain_overload_confirmed'
    states = dict(verdict.capabilities)
    assert states['linear_magnitude_valid'] == 'invalid'
    assert states['dut_thd_valid'] == 'invalid'
    # The adverse flag honestly reports overload cannot be excluded.
    assert states['chain_overload_not_excluded'] == 'valid'


def test_hidden_agc_makes_high_level_claims_ineligible() -> None:
    profile = _profile(
        dynamic_processing=(
            CadDynamicProcessingBlock(
                processing_kind='agc',
                stage_index=3,
                state='enabled',
            ),
        )
    )
    verdict = evaluate_measchain_qualification(
        document_id=DOC,
        profile=profile,
        requested_class='dut_nonlinear',
        requested_band_low_hz=200.0,
        requested_band_high_hz=8000.0,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'nonlinear_measurement_ineligible'
    assert verdict.distortion_attribution == 'chain_distortion_limited'
    assert dict(verdict.capabilities)['dut_compression_valid'] == 'invalid'


def test_unasserted_indicator_is_not_linearity_proof() -> None:
    # Overload indicator merely 'clear' — plus real linearity evidence the
    # chain qualifies; without a band request a high-level claim is
    # unqualified regardless.
    profile = _profile()
    observation = build_overload_observation(
        document_id=DOC,
        chain_ref=profile,
        overload_mechanism='no_overload_observed',
        overload_indicator_state='clear',
        sample_peak_dbfs=-6.0,
        peak_semantics='peak',
        declared_at_utc=T1,
    )
    verdict = evaluate_measchain_qualification(
        document_id=DOC,
        profile=profile,
        observation=observation,
        requested_class='high_level_spl',
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'unqualified_insufficient_evidence'

    verdict2 = evaluate_measchain_qualification(
        document_id=DOC,
        profile=profile,
        observation=observation,
        requested_class='standard_fr_ir',
        requested_band_low_hz=200.0,
        requested_band_high_hz=8000.0,
        evaluated_at_utc=T1,
    )
    assert verdict2.state == 'chain_qualified_within_declared_range'


def test_high_level_claim_needs_evidenced_band_and_level() -> None:
    # Band declared but no evidence covering it.
    thin = _profile(upper_level_specs=(), linearity_bands=())
    verdict = evaluate_measchain_qualification(
        document_id=DOC,
        profile=thin,
        requested_class='high_level_spl',
        requested_band_low_hz=200.0,
        requested_band_high_hz=8000.0,
        requested_level_db=110.0,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'unqualified_insufficient_evidence'

    # Evidenced band but requested level exceeds the envelope.
    verdict2 = evaluate_measchain_qualification(
        document_id=DOC,
        profile=_profile(),
        requested_class='high_level_spl',
        requested_band_low_hz=200.0,
        requested_band_high_hz=8000.0,
        requested_level_db=150.0,
        evaluated_at_utc=T1,
    )
    assert verdict2.state == 'overload_suspected'

    # A 1 kHz-only qualification does not extrapolate to 30 Hz.
    verdict3 = evaluate_measchain_qualification(
        document_id=DOC,
        profile=_profile(
            upper_level_specs=(
                CadUpperLevelSpec(
                    applies_to_stage_index=0,
                    level_db=120.0,
                    level_unit='db_spl',
                    threshold_kind='thd_1pct',
                    band_low_hz=500.0,
                    band_high_hz=2000.0,
                    evidence_basis='manufacturer_specification',
                ),
            ),
            linearity_bands=(),
        ),
        requested_class='high_level_spl',
        requested_band_low_hz=30.0,
        requested_band_high_hz=100.0,
        requested_level_db=110.0,
        evaluated_at_utc=T1,
    )
    assert verdict3.state == 'unqualified_insufficient_evidence'


def test_two_level_linear_verdict_qualifies_chain() -> None:
    profile = _profile()
    observation = build_overload_observation(
        document_id=DOC,
        chain_ref=profile,
        overload_mechanism='no_overload_observed',
        overload_indicator_state='clear',
        two_level_check=CadTwoLevelCheck(
            levels_db=(-20.0, -10.0),
            gain_settings=('0dB', '10dB'),
            normalized_residual_db=0.2,
            verdict='chain_linear_within_tested_range',
        ),
        declared_at_utc=T1,
    )
    verdict = evaluate_measchain_qualification(
        document_id=DOC,
        profile=profile,
        observation=observation,
        requested_class='dut_nonlinear',
        requested_band_low_hz=200.0,
        requested_band_high_hz=8000.0,
        requested_level_db=120.0,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'chain_qualified_within_declared_range'
    assert verdict.distortion_attribution == 'dut_distortion_eligible'


def test_overload_suspected_via_two_level_check() -> None:
    profile = _profile()
    observation = build_overload_observation(
        document_id=DOC,
        chain_ref=profile,
        overload_mechanism='unknown',
        overload_indicator_state='unavailable',
        sample_peak_dbfs=-3.0,
        two_level_check=CadTwoLevelCheck(
            levels_db=(-20.0, -10.0),
            normalized_residual_db=3.0,
            verdict='overload_suspected',
        ),
        declared_at_utc=T1,
    )
    verdict = evaluate_measchain_qualification(
        document_id=DOC,
        profile=profile,
        observation=observation,
        requested_class='high_level_spl',
        requested_band_low_hz=200.0,
        requested_band_high_hz=8000.0,
        requested_level_db=120.0,
        evaluated_at_utc=T1,
    )
    assert verdict.state == 'overload_suspected'
    assert verdict.distortion_attribution == 'chain_overload_suspected'
    assert dict(verdict.capabilities)['dut_thd_valid'] == 'invalid'


def test_measchain_repository_roundtrip_and_integrity(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repo = CadMeasChainLinearityRepository(scene_repository)
    profile = _profile()
    repo.save_profile(profile)
    repo.save_profile(profile)  # idempotent
    assert repo.get_profile(profile.profile_id) == profile
    assert repo.list_profiles(DOC) == (profile,)

    observation = build_overload_observation(
        document_id=DOC,
        chain_ref=profile,
        overload_mechanism='adc_numeric_full_scale',
        overload_stage_index=2,
        sample_peak_dbfs=0.0,
        declared_at_utc=T1,
    )
    repo.save_observation(observation)
    assert repo.get_observation(observation.observation_id) == observation

    verdict = evaluate_measchain_qualification(
        document_id=DOC,
        profile=profile,
        observation=observation,
        requested_class='standard_fr_ir',
        evaluated_at_utc=T1,
    )
    repo.save_qualification(verdict)
    assert repo.get_qualification(verdict.qualification_id) == verdict
    assert repo.list_qualifications(DOC) == (verdict,)

    # Forged sha on an existing id is append-only conflict territory;
    # a forged *payload* fails the seal re-verification on save.
    forged = profile.model_copy(update={'chain_label': 'forged'})
    with pytest.raises(MeasChainAuthorityIntegrityError):
        repo.save_profile(forged)

    # A tampered mirrored column is caught on read.
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_measchain_linearity_profiles SET chain_label=? '
            'WHERE profile_id=?',
            ('tampered', profile.profile_id),
        )
    with pytest.raises(MeasChainAuthorityIntegrityError):
        repo.get_profile(profile.profile_id)


# ---------------------------------------------------------------------------
# #697 — swept-sine deconvolution / harmonic separation
# ---------------------------------------------------------------------------


def _deconv_spec():
    return build_deconvolution_spec(
        document_id=DOC,
        stimulus_ref=STIMULUS_REF,
        sweep_law='exponential',
        sweep_f_start_hz=20.0,
        sweep_f_end_hz=20000.0,
        sweep_duration_s=10.0,
        sweep_sample_rate_hz=48000.0,
        algorithm='farina_inverse_filter',
        implementation_version='htdt-deconv-1.0',
        inverse_construction='time_reversal_with_amplitude_taper',
        normalization='unity_at_linear_peak',
        time_origin_convention='linear_peak_at_t0',
        declared_at_utc=T0,
    )


def test_deconv_spec_requires_sha_pinned_stimulus() -> None:
    with pytest.raises(ValueError, match='sha256'):
        build_deconvolution_spec(
            document_id=DOC,
            stimulus_ref=AuthorityRef(
                kind='stimulus_signal', ref_id='stim-ess-1'
            ),
            sweep_law='exponential',
            algorithm='farina_inverse_filter',
            declared_at_utc=T0,
        )


def test_harmonic_offsets_derive_from_exact_sweep_law() -> None:
    from math import log

    spec = _deconv_spec()
    # Δt_n = −T·ln(n)/ln(f2/f1)
    expected2 = -10.0 * log(2) / log(20000.0 / 20.0)
    assert spec.harmonic_offset_s(2) == pytest.approx(expected2)
    # Order-2 valid fundamental band: f in [20, 10000].
    assert spec.harmonic_valid_band(2) == (20.0, 10000.0)

    incomplete = build_deconvolution_spec(
        document_id=DOC,
        stimulus_ref=STIMULUS_REF,
        sweep_law='exponential',
        sweep_duration_s=10.0,
        algorithm='regularized_division',
        declared_at_utc=T0,
    )
    assert incomplete.harmonic_offset_s(2) is None

    linear = build_deconvolution_spec(
        document_id=DOC,
        stimulus_ref=STIMULUS_REF,
        sweep_law='linear',
        algorithm='time_domain_least_squares',
        declared_at_utc=T0,
    )
    assert linear.harmonic_offset_s(2) is None


def test_recovered_ir_provenance_classes() -> None:
    spec = _deconv_spec()
    component = build_harmonic_component(
        document_id=DOC,
        spec=spec,
        harmonic_order=2,
        extraction_window=CadExtractionWindow(
            window_start_s=-1.5, window_end_s=-0.9,
        ),
        overlap_state='separated',
        component_artifact_sha256=SHA_D,
        declared_at_utc=T1,
    )
    assert component.expected_offset_s == pytest.approx(
        spec.harmonic_offset_s(2)
    )

    ir = build_recovered_ir(
        document_id=DOC,
        provenance_class='derived_full_provenance',
        spec_ref=spec,
        raw_capture_ref=CAPTURE_REF,
        full_response_sha256=SHA_A,
        linear_extract_sha256=SHA_B,
        harmonic_component_refs=(component,),
        declared_at_utc=T1,
    )
    assert ir.ir_id.startswith('recir-')

    # Derived provenance without the raw capture pin is rejected.
    with pytest.raises(ValueError, match='raw capture'):
        build_recovered_ir(
            document_id=DOC,
            provenance_class='derived_full_provenance',
            spec_ref=spec,
            full_response_sha256=SHA_A,
            declared_at_utc=T1,
        )

    # Imported final IR cannot carry a spec or components and must name
    # its source tool.
    with pytest.raises(ValueError, match='source tool'):
        build_recovered_ir(
            document_id=DOC,
            provenance_class='imported_final_only',
            linear_extract_sha256=SHA_B,
            declared_at_utc=T1,
        )
    with pytest.raises(ValueError, match='deconvolution'):
        build_recovered_ir(
            document_id=DOC,
            provenance_class='imported_final_only',
            spec_ref=spec,
            source_tool='rew',
            declared_at_utc=T1,
        )
    imported = build_recovered_ir(
        document_id=DOC,
        provenance_class='imported_final_only',
        linear_extract_sha256=SHA_B,
        source_tool='rew',
        source_tool_version='5.30',
        declared_at_utc=T1,
    )
    verdict = evaluate_linear_ir_capability(
        document_id=DOC,
        ir=imported,
        clock_gate='synchronized',
        chain_gate='qualified',
        evaluated_at_utc=T1,
    )
    assert verdict.contamination_state == 'insufficient_evidence'
    assert dict(verdict.capabilities)['fr_valid'] == 'limited'


def test_linear_ir_capability_contamination_and_gates() -> None:
    spec = _deconv_spec()
    separated = build_harmonic_component(
        document_id=DOC,
        spec=spec,
        harmonic_order=2,
        overlap_state='separated',
        component_artifact_sha256=SHA_D,
        declared_at_utc=T1,
    )
    ir = build_recovered_ir(
        document_id=DOC,
        provenance_class='derived_full_provenance',
        spec_ref=spec,
        raw_capture_ref=CAPTURE_REF,
        full_response_sha256=SHA_A,
        linear_extract_sha256=SHA_B,
        harmonic_component_refs=(separated,),
        declared_at_utc=T1,
    )
    verdict = evaluate_linear_ir_capability(
        document_id=DOC,
        ir=ir,
        spec=spec,
        harmonic_components=(separated,),
        clock_gate='synchronized',
        chain_gate='qualified',
        evaluated_at_utc=T1,
    )
    assert verdict.contamination_state == 'nonlinear_components_separated'
    assert dict(verdict.capabilities)['fr_valid'] == 'valid'
    assert dict(verdict.capabilities)['absolute_phase_valid'] == 'valid'

    # Causal contamination → early reflections/clarity invalid.
    contaminating = build_harmonic_component(
        document_id=DOC,
        spec=spec,
        harmonic_order=3,
        overlap_state='contaminates_causal',
        component_artifact_sha256=SHA_D,
        declared_at_utc=T1,
    )
    ir2 = build_recovered_ir(
        document_id=DOC,
        provenance_class='derived_full_provenance',
        spec_ref=spec,
        raw_capture_ref=CAPTURE_REF,
        full_response_sha256=SHA_A,
        linear_extract_sha256=SHA_B,
        harmonic_component_refs=(contaminating,),
        declared_at_utc=T1,
    )
    verdict2 = evaluate_linear_ir_capability(
        document_id=DOC,
        ir=ir2,
        spec=spec,
        harmonic_components=(contaminating,),
        clock_gate='synchronized',
        chain_gate='qualified',
        evaluated_at_utc=T1,
    )
    assert (
        verdict2.contamination_state
        == 'causal_nonlinear_contamination_risk'
    )
    caps2 = dict(verdict2.capabilities)
    assert caps2['early_reflection_valid'] == 'invalid'
    assert caps2['clarity_valid'] == 'invalid'
    assert caps2['fr_valid'] == 'limited'

    # Declared asynchronous clock → no absolute phase, early reflections
    # degrade.
    verdict3 = evaluate_linear_ir_capability(
        document_id=DOC,
        ir=ir,
        spec=spec,
        harmonic_components=(separated,),
        clock_gate='unsynchronized_declared',
        chain_gate='qualified',
        evaluated_at_utc=T1,
    )
    caps3 = dict(verdict3.capabilities)
    assert caps3['absolute_phase_valid'] == 'invalid'
    assert caps3['early_reflection_valid'] == 'limited'

    # Chain overload observed → 'valid' degrades to 'limited'/'invalid'.
    verdict4 = evaluate_linear_ir_capability(
        document_id=DOC,
        ir=ir,
        spec=spec,
        harmonic_components=(separated,),
        clock_gate='synchronized',
        chain_gate='overload_observed',
        evaluated_at_utc=T1,
    )
    caps4 = dict(verdict4.capabilities)
    assert caps4['fr_valid'] == 'limited'
    assert caps4['early_reflection_valid'] == 'limited'


def test_sweep_repository_roundtrip_and_integrity(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repo = CadSweepDeconvolutionRepository(scene_repository)
    spec = _deconv_spec()
    repo.save_spec(spec)
    repo.save_spec(spec)
    assert repo.get_spec(spec.spec_id) == spec

    component = build_harmonic_component(
        document_id=DOC,
        spec=spec,
        harmonic_order=2,
        overlap_state='separated',
        component_artifact_sha256=SHA_D,
        declared_at_utc=T1,
    )
    repo.save_component(component)
    assert repo.get_component(component.component_id) == component
    assert repo.list_components(DOC) == (component,)

    ir = build_recovered_ir(
        document_id=DOC,
        provenance_class='derived_full_provenance',
        spec_ref=spec,
        raw_capture_ref=CAPTURE_REF,
        full_response_sha256=SHA_A,
        linear_extract_sha256=SHA_B,
        harmonic_component_refs=(component,),
        declared_at_utc=T1,
    )
    repo.save_recovered_ir(ir)
    assert repo.get_recovered_ir(ir.ir_id) == ir

    capability = evaluate_linear_ir_capability(
        document_id=DOC,
        ir=ir,
        spec=spec,
        harmonic_components=(component,),
        evaluated_at_utc=T1,
    )
    repo.save_capability(capability)
    assert repo.get_capability(capability.capability_id) == capability
    assert repo.list_capabilities(DOC) == (capability,)

    forged = spec.model_copy(update={'sweep_duration_s': 99.0})
    with pytest.raises(SweepDeconvAuthorityIntegrityError):
        repo.save_spec(forged)

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_sweep_deconvolution_specs SET algorithm=? '
            'WHERE spec_id=?',
            ('forged_algo', spec.spec_id),
        )
    with pytest.raises(SweepDeconvAuthorityIntegrityError):
        repo.get_spec(spec.spec_id)


# ---------------------------------------------------------------------------
# #668 — room-acoustic excitation source
# ---------------------------------------------------------------------------


def _omni_band(
    state='omni_within_profile_band',
    basis='laboratory_measurement',
):
    return CadOmniBandCapability(
        band_low_hz=100.0,
        band_high_hz=2000.0,
        state=state,
        evidence_basis=basis,
        method_or_standard='ISO 3382-1 free-field verification',
        max_deviation_db=1.5,
        uncertainty_db=0.5,
    )


def _dodeca_profile(**overrides):
    kwargs = dict(
        document_id=DOC,
        source_label='dodeca-1',
        source_type='dodecahedron_omni',
        measurand_class='room_response_approx_omni_source',
        device_model='dodecahedron speaker mk2',
        omni_capabilities=(_omni_band(),),
        pose=CadSourcePose(
            x_m=1.2, y_m=0.8, z_m=1.4, azimuth_deg=0.0,
        ),
        level_capability=CadSourceLevelCapability(
            max_test_level_db=105.0,
            limiter_state='none_declared',
            achieved_decay_range_db=50.0,
        ),
        stimulus_ref=STIMULUS_REF,
        declared_at_utc=T0,
    )
    kwargs.update(overrides)
    return build_excitation_profile(**kwargs)


def test_dodecahedron_shape_is_not_omni_evidence() -> None:
    with pytest.raises(ValueError, match='omnidirectionality'):
        _dodeca_profile(
            omni_capabilities=(
                CadOmniBandCapability(
                    band_low_hz=100.0,
                    band_high_hz=2000.0,
                    state='omni_within_profile_band',
                    evidence_basis='assumed_from_geometry',
                ),
            )
        )


def test_measurand_class_is_source_coherent() -> None:
    with pytest.raises(ValueError, match='standardized omni-source'):
        build_excitation_profile(
            document_id=DOC,
            source_label='main-L',
            source_type='installed_loudspeaker_channel',
            measurand_class='room_response_standardized_omni_source',
            declared_at_utc=T0,
        )
    with pytest.raises(ValueError, match='installed'):
        build_excitation_profile(
            document_id=DOC,
            source_label='dodeca-1',
            source_type='dodecahedron_omni',
            measurand_class='installed_channel_system_response',
            declared_at_utc=T0,
        )


def test_source_qualification_purposes() -> None:
    dodeca = _dodeca_profile()
    verdict = evaluate_source_qualification(
        document_id=DOC,
        profile=dodeca,
        purposes=(
            'standardized_room_characterization',
            'installed_system_diagnostics',
            'strength_g_measurement',
            'simulation_validation_comparison',
        ),
        requested_band_low_hz=200.0,
        requested_band_high_hz=1000.0,
        simulated_source_model='ideal_omni',
        evaluated_at_utc=T1,
    )
    elig = dict(verdict.eligibilities)
    assert elig['standardized_room_characterization'] == 'eligible'
    assert elig['installed_system_diagnostics'] == 'eligible'
    # 'omni_within_profile_band' can carry G only with limitation.
    assert elig['strength_g_measurement'] == (
        'eligible_with_source_limitation'
    )
    assert verdict.strength_g_gate == 'ineligible'
    assert verdict.level_gate == 'sufficient'
    assert verdict.sim_comparison == 'comparable_within_validated_band'

    # Beyond the verified band the same purpose fails closed.
    verdict2 = evaluate_source_qualification(
        document_id=DOC,
        profile=dodeca,
        purposes=('standardized_room_characterization',),
        requested_band_low_hz=4000.0,
        requested_band_high_hz=8000.0,
        evaluated_at_utc=T1,
    )
    assert dict(verdict2.eligibilities)[
        'standardized_room_characterization'
    ] == 'source_state_unknown'


def test_installed_speaker_never_answers_standardized_metrics() -> None:
    installed = build_excitation_profile(
        document_id=DOC,
        source_label='front-L',
        source_type='installed_loudspeaker_channel',
        measurand_class='installed_channel_system_response',
        device_model='bookshelf sp',
        declared_at_utc=T0,
    )
    verdict = evaluate_source_qualification(
        document_id=DOC,
        profile=installed,
        purposes=(
            'standardized_room_characterization',
            'installed_system_diagnostics',
            'strength_g_measurement',
            'simulation_validation_comparison',
        ),
        simulated_source_model='ideal_omni',
        evaluated_at_utc=T1,
    )
    elig = dict(verdict.eligibilities)
    # Wrong source class — never silently answers C80/T30/G.
    assert elig['standardized_room_characterization'] == 'wrong_source_class'
    assert elig['strength_g_measurement'] == 'wrong_source_class'
    # ...but stays first-class installed-system evidence.
    assert elig['installed_system_diagnostics'] == 'eligible'
    # Measured installed channel vs simulated ideal omni is a wrong model.
    assert verdict.sim_comparison == 'wrong_source_model'
    assert verdict.strength_g_gate == 'ineligible'


def test_source_level_gate_and_limiter() -> None:
    limited = _dodeca_profile(
        level_capability=CadSourceLevelCapability(
            limiter_state='engaged',
            achieved_decay_range_db=25.0,
        )
    )
    verdict = evaluate_source_qualification(
        document_id=DOC,
        profile=limited,
        purposes=('standardized_room_characterization',),
        requested_band_low_hz=200.0,
        requested_band_high_hz=1000.0,
        evaluated_at_utc=T1,
    )
    assert verdict.level_gate == 'insufficient'
    assert dict(verdict.eligibilities)[
        'standardized_room_characterization'
    ] == 'insufficient_source_level'

    no_level = _dodeca_profile(level_capability=None)
    verdict2 = evaluate_source_qualification(
        document_id=DOC,
        profile=no_level,
        purposes=('standardized_room_characterization',),
        requested_band_low_hz=200.0,
        requested_band_high_hz=1000.0,
        evaluated_at_utc=T1,
    )
    assert verdict2.level_gate == 'unknown'


def test_orientation_capture_aggregate_requires_transform() -> None:
    profile = _dodeca_profile()
    pose = CadSourcePose(azimuth_deg=45.0)
    with pytest.raises(ValueError, match='transform'):
        build_orientation_capture(
            document_id=DOC,
            source_ref=profile,
            pose=pose,
            aggregation_role='aggregate_result',
            declared_at_utc=T1,
        )
    transform_ref = AuthorityRef(
        kind='measurement_transform', ref_id='xf-1', ref_sha256=SHA_D
    )
    aggregate = build_orientation_capture(
        document_id=DOC,
        source_ref=profile,
        pose=pose,
        aggregation_role='aggregate_result',
        transform_ref=transform_ref,
        aggregate_of=('srcori-a', 'srcori-b'),
        declared_at_utc=T1,
    )
    assert aggregate.aggregation_role == 'aggregate_result'


def test_excitation_repository_roundtrip_and_integrity(tmp_path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repo = CadExcitationSourceRepository(scene_repository)
    profile = _dodeca_profile()
    repo.save_profile(profile)
    repo.save_profile(profile)
    assert repo.get_profile(profile.profile_id) == profile
    assert repo.list_profiles(DOC) == (profile,)

    capture = build_orientation_capture(
        document_id=DOC,
        source_ref=profile,
        pose=CadSourcePose(azimuth_deg=90.0),
        captured_at_utc=T1,
        declared_at_utc=T1,
    )
    repo.save_capture(capture)
    assert repo.get_capture(capture.capture_id) == capture

    qualification = evaluate_source_qualification(
        document_id=DOC,
        profile=profile,
        purposes=('standardized_room_characterization',),
        evaluated_at_utc=T1,
    )
    repo.save_qualification(qualification)
    assert repo.get_qualification(
        qualification.qualification_id
    ) == qualification

    forged = profile.model_copy(update={'source_type': 'subwoofer'})
    with pytest.raises(ExcitationAuthorityIntegrityError):
        repo.save_profile(forged)

    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_excitation_source_profiles SET source_type=? '
            'WHERE profile_id=?',
            ('impulse_source', profile.profile_id),
        )
    with pytest.raises(ExcitationAuthorityIntegrityError):
        repo.get_profile(profile.profile_id)


def test_append_only_tables_coexist_distinct_records(tmp_path) -> None:
    """Append-only + sha-derived ids: different content → different id,
    both coexist; re-saving the same sealed record is a no-op."""
    scene_repository = _scene_repo(tmp_path)
    repo = CadMeasChainLinearityRepository(scene_repository)
    repo_a = CadSweepDeconvolutionRepository(scene_repository)
    repo_b = CadExcitationSourceRepository(scene_repository)

    first = _profile()
    second = _profile(chain_label='mic→preamp→adc (second rig)')
    repo.save_profile(first)
    repo.save_profile(second)
    assert first.profile_id != second.profile_id
    assert {p.profile_id for p in repo.list_profiles(DOC)} == {
        first.profile_id,
        second.profile_id,
    }

    spec_a = _deconv_spec()
    spec_b = build_deconvolution_spec(
        document_id=DOC,
        stimulus_ref=STIMULUS_REF,
        sweep_law='linear',
        sweep_duration_s=5.0,
        algorithm='time_domain_least_squares',
        declared_at_utc=T0,
    )
    repo_a.save_spec(spec_a)
    repo_a.save_spec(spec_b)
    assert len(repo_a.list_specs(DOC)) == 2

    src_a = _dodeca_profile()
    src_b = _dodeca_profile(source_label='dodeca-2')
    repo_b.save_profile(src_a)
    repo_b.save_profile(src_b)
    assert len(repo_b.list_profiles(DOC)) == 2
