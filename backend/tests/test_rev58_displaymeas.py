"""REV58-DISPLAYMEAS regression tests — #682 pattern-generator stimulus
fidelity, #680 colorimeter spectral mismatch / probe matching, #686
display additivity / RGB separation / volumetric characterisation, #647
temporal display fidelity, #666 LUT closed-loop calibration.

Every authority is fail-closed: an unverified delivered signal is not a
match, a match never silently applies to another unit, sparse model
families need measured independence, refresh labels never qualify
temporal claims, and a written LUT without readback is not deployment.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

from htdt.cad_pattern_generator_fidelity import (
    DeliveredStimulusObservation,
    GeneratorCapability,
    PatchNumericIdentity,
    PatternGeneratorInstance,
    PrecisionRequirement,
    RequestedVideoPatch,
    SignalTransformDeclaration,
    evaluate_generator_fidelity,
    evaluate_patch_delivery,
)
from htdt.cad_meter_match import (
    DisplayMeterMatchProfile,
    DisplaySpdIdentity,
    InstrumentIdentity,
    PatchReadingPair,
    PreExistingCorrection,
    ProbeMatchObservation,
    ProbeMatchVerification,
    evaluate_correction_applicability,
)
from htdt.cad_display_additivity import (
    AdditivityPoint,
    CharacterisationPlan,
    DisplayAdditivityObservation,
    HoldoutVerification,
    VolumetricCharacterisation,
    evaluate_additivity,
    evaluate_model_eligibility,
)
from htdt.cad_temporal_display import (
    StepResponseDefinition,
    TemporalDisplayState,
    TemporalStepResponseMeasurement,
    evaluate_temporal_qualification,
)
from htdt.cad_lut_closed_loop import (
    DisplayLUTArtifact,
    LUTDeploymentRecord,
    LUTGenerationRecord,
    LUTPostVerification,
    LUTPreflightVerification,
    LutDomainSpec,
    LutGenerationSpec,
    LutTargetSpec,
    evaluate_lut_closed_loop,
)
from htdt.cad_display_metrology_repository import (
    CadDisplayAdditivityRepository,
    CadLutClosedLoopRepository,
    CadMeterMatchRepository,
    CadPatternGeneratorFidelityRepository,
    CadTemporalDisplayRepository,
    DisplayMetrologyConflictError,
    DisplayMetrologyIntegrityError,
)


DOC = 'doc-rev58-displaymeas'
T0 = '2026-10-06T00:00:00+00:00'
T1 = '2026-10-06T01:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64
SHA_D = 'd' * 64
SHA_E = 'e' * 64
SHA_F = 'f' * 64

STIMULUS_REF = AuthorityRef(
    kind='stimulus_asset', ref_id='stim-1', ref_sha256=SHA_A
)
STATE_REF = AuthorityRef(
    kind='direct_view_display_state',
    ref_id='dvstate-1',
    ref_sha256=SHA_B,
)
PATCH_SET_REF = AuthorityRef(
    kind='patch_set', ref_id='pset-1', ref_sha256=SHA_C
)
CHAR_REF = AuthorityRef(
    kind='volumetric_characterisation',
    ref_id='dvol-1',
    ref_sha256=SHA_D,
)
EVIDENCE_REF = AuthorityRef(
    kind='link_capture', ref_id='capture-1', ref_sha256=SHA_E
)


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _tamper(db_path, sql: str, params=()) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #682 — pattern-generator stimulus fidelity (PG fixtures)
# ---------------------------------------------------------------------------


def _pg_generator() -> PatternGeneratorInstance:
    return PatternGeneratorInstance.create(
        document_id=DOC,
        generator_class='hardware_generator',
        manufacturer='ACME',
        model='VideoForge Pro',
        instance_serial='VF-0007',
        firmware='2.1.0',
        capability=GeneratorCapability(
            control_input_bit_depth=10,
            internal_processing_bit_depth=16,
            output_link_bit_depth=10,
            supported_ranges=('full', 'legal_narrow'),
            max_raster_hz=120.0,
        ),
    )


def _pg_numeric(**over) -> PatchNumericIdentity:
    args = dict(
        code_values=(128.0, 128.0, 128.0),
        bit_depth=8,
        signal_range='legal_narrow',
        color_encoding='ycbcr_422',
        colorimetry='bt709',
        eotf='gamma_2_4',
        chroma_sampling='4:2:2',
    )
    args.update(over)
    return PatchNumericIdentity(**args)


def _pg_patch(**over) -> RequestedVideoPatch:
    args = dict(
        document_id=DOC,
        stimulus_ref=STIMULUS_REF,
        numeric=_pg_numeric(),
        window_size_percent=10.0,
    )
    args.update(over)
    return RequestedVideoPatch.create(**args)


def _pg_observation(
    patch: RequestedVideoPatch,
    generator: PatternGeneratorInstance,
    **over,
) -> DeliveredStimulusObservation:
    args = dict(
        document_id=DOC,
        patch_ref=AuthorityRef(
            kind='requested_video_patch',
            ref_id=patch.patch_id,
            ref_sha256=patch.patch_sha256,
        ),
        generator_ref=AuthorityRef(
            kind='pattern_generator_instance',
            ref_id=generator.generator_id,
            ref_sha256=generator.generator_sha256,
        ),
        observation_point='link_negotiated',
        verification='protocol_readback',
        delivered_code_values=(128.0, 128.0, 128.0),
        delivered_bit_depth=8,
        delivered_range='legal_narrow',
        delivered_encoding='ycbcr_422',
        observed_at_utc=T0,
        evidence_ref=EVIDENCE_REF,
    )
    args.update(over)
    return DeliveredStimulusObservation.create(**args)


def _pg_requirement() -> PrecisionRequirement:
    return PrecisionRequirement(
        required_bit_depth=8,
        required_range='legal_narrow',
        required_encoding='ycbcr_422',
    )


def test_pg10_records_seal_with_prefixed_ids():
    generator = _pg_generator()
    patch = _pg_patch()
    observation = _pg_observation(patch, generator)
    assert generator.generator_id.startswith('pginst-')
    assert patch.patch_id.startswith('reqpatch-')
    assert observation.observation_id.startswith('delobs-')
    assert len(generator.generator_sha256) == 64


def test_pg11_patch_requires_stimulus_sha_pin():
    with pytest.raises(ValueError):
        RequestedVideoPatch.create(
            document_id=DOC,
            stimulus_ref=AuthorityRef(
                kind='stimulus_asset', ref_id='stim-x', ref_sha256=None
            ),
            numeric=_pg_numeric(),
        )


def test_pg12_verified_observation_matches():
    patch = _pg_patch()
    generator = _pg_generator()
    qualification = evaluate_patch_delivery(
        patch,
        _pg_observation(patch, generator),
        requirement=_pg_requirement(),
    )
    assert qualification.verdict == 'delivered_verified'
    assert qualification.mismatches == ()


def test_pg13_unverified_observation_is_not_a_pass():
    patch = _pg_patch()
    generator = _pg_generator()
    observation = DeliveredStimulusObservation.create(
        document_id=DOC,
        patch_ref=AuthorityRef(
            kind='requested_video_patch',
            ref_id=patch.patch_id,
            ref_sha256=patch.patch_sha256,
        ),
        generator_ref=AuthorityRef(
            kind='pattern_generator_instance',
            ref_id=generator.generator_id,
            ref_sha256=generator.generator_sha256,
        ),
        observation_point='generator_output',
        verification='unverified',
        observed_at_utc=T0,
    )
    qualification = evaluate_patch_delivery(
        patch, observation, requirement=_pg_requirement()
    )
    assert qualification.verdict == 'delivery_unverified'


def test_pg14_code_value_shift_is_a_mismatch():
    patch = _pg_patch()
    generator = _pg_generator()
    observation = _pg_observation(
        patch, generator, delivered_code_values=(129.0, 128.0, 128.0)
    )
    qualification = evaluate_patch_delivery(
        patch, observation, requirement=_pg_requirement()
    )
    assert qualification.verdict == 'delivered_mismatch'
    assert any(m.kind == 'code_value_shift' for m in qualification.mismatches)


def test_pg15_range_mapping_drift_detected():
    patch = _pg_patch()
    generator = _pg_generator()
    observation = _pg_observation(
        patch, generator, delivered_range='full'
    )
    qualification = evaluate_patch_delivery(
        patch, observation, requirement=_pg_requirement()
    )
    assert qualification.verdict == 'delivered_mismatch'
    assert any(m.kind == 'range_mapping' for m in qualification.mismatches)


def test_pg16_documented_transform_caps_at_limited():
    patch = _pg_patch()
    generator = _pg_generator()
    observation = _pg_observation(
        patch,
        generator,
        declared_transforms=(
            SignalTransformDeclaration(kind='range_rescale'),
        ),
    )
    qualification = evaluate_patch_delivery(
        patch, observation, requirement=_pg_requirement()
    )
    assert qualification.verdict == 'delivered_with_documented_transform'


def test_pg17_generator_fidelity_ladder():
    generator = _pg_generator()
    verified = _pg_patch(sequence_index=0)
    unverified = _pg_patch(sequence_index=1)
    mismatched = _pg_patch(sequence_index=2)
    observations = {
        verified.patch_id: _pg_observation(verified, generator),
        mismatched.patch_id: _pg_observation(
            mismatched, generator,
            delivered_code_values=(64.0, 64.0, 64.0),
        ),
    }

    clean = evaluate_generator_fidelity(
        generator, (verified,), observations,
        requirement=_pg_requirement(), issued_at_utc=T0,
    )
    assert clean.verdict == 'fidelity_qualified'
    assert clean.qualification_id.startswith('genfq-')

    partial = evaluate_generator_fidelity(
        generator, (verified, unverified), observations,
        requirement=_pg_requirement(), issued_at_utc=T0,
    )
    assert partial.verdict == 'fidelity_insufficient_evidence'

    bad = evaluate_generator_fidelity(
        generator, (verified, mismatched), observations,
        requirement=_pg_requirement(), issued_at_utc=T0,
    )
    assert bad.verdict == 'fidelity_unqualified'
    assert 'DELIVERED_MISMATCH' in bad.reasons


# ---------------------------------------------------------------------------
# #680 — colorimeter spectral mismatch / probe matching (MM fixtures)
# ---------------------------------------------------------------------------


def _mm_instrument(**over) -> InstrumentIdentity:
    args = dict(
        kind='tristimulus_colorimeter',
        manufacturer='ACME',
        model='i1D3',
        serial='C-001',
    )
    args.update(over)
    return InstrumentIdentity(**args)


def _mm_display_state(**over) -> DisplaySpdIdentity:
    args = dict(
        display_instance='DISP-1',
        display_model='LG C4',
        picture_mode='filmmaker',
        spd_basis='measured_spd',
        measured_spd_sha256=SHA_F,
    )
    args.update(over)
    return DisplaySpdIdentity(**args)


def _mm_profile(**over) -> DisplayMeterMatchProfile:
    args = dict(
        document_id=DOC,
        target_instrument=_mm_instrument(),
        reference_instrument=_mm_instrument(
            kind='spectroradiometer', model='CS-2000', serial='S-002'
        ),
        display_state=_mm_display_state(),
        pre_existing_correction=PreExistingCorrection(
            state='raw_default_response'
        ),
        patch_set_ref=PATCH_SET_REF,
        algorithm='matrix_3x3',
        correction_artifact_sha256=SHA_E,
        matched_at_utc=T0,
    )
    args.update(over)
    return DisplayMeterMatchProfile.create(**args)


def _mm_verification(match: DisplayMeterMatchProfile) -> ProbeMatchVerification:
    return ProbeMatchVerification.create(
        document_id=DOC,
        match_ref=AuthorityRef(
            kind='display_meter_match_profile',
            ref_id=match.match_id,
            ref_sha256=match.match_sha256,
        ),
        holdout_patch_set_ref=AuthorityRef(
            kind='patch_set', ref_id='pset-holdout', ref_sha256=SHA_D
        ),
        residuals_delta_e=(0.4, 0.7, 0.9),
        pass_threshold_delta_e=1.5,
        verified_at_utc=T1,
        passed=True,
    )


def test_mm10_records_seal_and_validate():
    match = _mm_profile()
    assert match.match_id.startswith('mmprof-')
    with pytest.raises(ValueError):
        _mm_profile(
            reference_instrument=_mm_instrument(
                kind='tristimulus_colorimeter', serial='C-009'
            )
        )
    with pytest.raises(ValueError):
        PreExistingCorrection(
            state='manufacturer_correction_active', correction_ref=None
        )


def test_mm11_verified_match_applies_with_limitations():
    match = _mm_profile()
    applicability = evaluate_correction_applicability(
        match,
        current_instrument=_mm_instrument(),
        current_display_state=_mm_display_state(),
        current_correction_state=PreExistingCorrection(
            state='raw_default_response'
        ),
        verification=_mm_verification(match),
        evaluated_at_utc=T1,
    )
    assert applicability.verdict == 'applicable'
    assert 'VERIFICATION_PASSED' in applicability.reasons
    assert applicability.applicability_id.startswith('mmappl-')


def test_mm12_different_target_unit_is_not_applicable():
    match = _mm_profile()
    applicability = evaluate_correction_applicability(
        match,
        current_instrument=_mm_instrument(serial='C-999'),
        current_display_state=_mm_display_state(),
        current_correction_state=PreExistingCorrection(
            state='raw_default_response'
        ),
        verification=_mm_verification(match),
        evaluated_at_utc=T1,
    )
    assert applicability.verdict == 'not_applicable'
    assert 'DIFFERENT_TARGET_UNIT' in applicability.reasons


def test_mm13_picture_state_change_stales_the_match():
    match = _mm_profile()
    applicability = evaluate_correction_applicability(
        match,
        current_instrument=_mm_instrument(),
        current_display_state=_mm_display_state(picture_mode='vivid'),
        current_correction_state=PreExistingCorrection(
            state='raw_default_response'
        ),
        verification=_mm_verification(match),
        evaluated_at_utc=T1,
    )
    assert applicability.verdict == 'not_applicable'
    assert 'DISPLAY_STATE_CHANGED' in applicability.reasons


def test_mm14_double_application_risk():
    match = _mm_profile()
    applicability = evaluate_correction_applicability(
        match,
        current_instrument=_mm_instrument(),
        current_display_state=_mm_display_state(),
        current_correction_state=PreExistingCorrection(
            state='custom_match_active',
            correction_ref=AuthorityRef(
                kind='correction_artifact',
                ref_id='corr-other',
                ref_sha256=SHA_B,
            ),
        ),
        verification=_mm_verification(match),
        evaluated_at_utc=T1,
    )
    assert applicability.verdict == 'not_applicable'
    assert 'DOUBLE_APPLICATION_RISK' in applicability.reasons


def test_mm15_observation_requires_residuals():
    match = _mm_profile()
    with pytest.raises(ValueError):
        ProbeMatchObservation.create(
            document_id=DOC,
            match_ref=AuthorityRef(
                kind='display_meter_match_profile',
                ref_id=match.match_id,
                ref_sha256=match.match_sha256,
            ),
            readings=(
                PatchReadingPair(patch_ref=PATCH_SET_REF),
            ),
            observed_at_utc=T0,
        )


# ---------------------------------------------------------------------------
# #686 — display additivity / RGB separation / model eligibility
# ---------------------------------------------------------------------------


def _da_observation() -> DisplayAdditivityObservation:
    return DisplayAdditivityObservation.create(
        document_id=DOC,
        display_state_ref=STATE_REF,
        stimulus_refs=(STIMULUS_REF,),
        points=(
            AdditivityPoint(
                level_percent=50.0,
                component_xyz=(
                    (10.0, 10.0, 10.0),
                    (10.0, 10.0, 10.0),
                    (10.0, 10.0, 10.0),
                ),
                combined_xyz=(30.0, 30.0, 30.0),
            ),
        ),
        measured_at_utc=T0,
    )


def _da_holdout(passed: bool = True) -> HoldoutVerification:
    return HoldoutVerification.create(
        document_id=DOC,
        display_state_ref=STATE_REF,
        model_family='matrix_1d',
        holdout_patch_set_ref=AuthorityRef(
            kind='patch_set', ref_id='pset-holdout', ref_sha256=SHA_D
        ),
        residuals_delta_e=(0.5, 0.8) if passed else (2.5, 3.0),
        pass_threshold_delta_e=1.5,
        verified_at_utc=T1,
        passed=passed,
    )


def test_da10_additivity_residual_measured():
    ok, worst = evaluate_additivity(_da_observation(), tolerance=1.0)
    assert ok is True
    assert worst == pytest.approx(0.0)


def test_da11_sparse_model_requires_independence_evidence():
    eligibility = evaluate_model_eligibility(
        document_id=DOC,
        display_state_ref=STATE_REF,
        model_family='matrix_1d',
        additivity=None,
        separation=None,
        volumetric=None,
        holdout=None,
        additivity_tolerance=1.0,
        coupling_tolerance_delta_e=2.0,
        evaluated_at_utc=T1,
    )
    assert eligibility.verdict == 'insufficient_evidence'
    assert eligibility.eligibility_id.startswith('cmelig-')


def test_da12_non_additive_display_rejects_matrix():
    non_additive = DisplayAdditivityObservation.create(
        document_id=DOC,
        display_state_ref=STATE_REF,
        stimulus_refs=(STIMULUS_REF,),
        points=(
            AdditivityPoint(
                level_percent=50.0,
                component_xyz=(
                    (10.0, 10.0, 10.0),
                    (10.0, 10.0, 10.0),
                    (10.0, 10.0, 10.0),
                ),
                combined_xyz=(75.0, 30.0, 30.0),
            ),
        ),
        measured_at_utc=T0,
    )
    eligibility = evaluate_model_eligibility(
        document_id=DOC,
        display_state_ref=STATE_REF,
        model_family='matrix_1d',
        additivity=non_additive,
        separation=None,
        volumetric=None,
        holdout=_da_holdout(),
        additivity_tolerance=0.1,
        coupling_tolerance_delta_e=2.0,
        evaluated_at_utc=T1,
    )
    assert eligibility.verdict in ('insufficient_evidence', 'inappropriate')
    assert 'NON_ADDITIVE_MEASURED' in eligibility.reasons


def test_da13_volumetric_lut_requires_dense_measurement():
    eligibility = evaluate_model_eligibility(
        document_id=DOC,
        display_state_ref=STATE_REF,
        model_family='fixed_grid_volumetric_lut',
        additivity=None,
        separation=None,
        volumetric=None,
        holdout=_da_holdout(),
        additivity_tolerance=1.0,
        coupling_tolerance_delta_e=2.0,
        evaluated_at_utc=T1,
    )
    assert eligibility.verdict == 'insufficient_evidence'
    assert 'EVIDENCE_MISSING' in eligibility.reasons


def test_da14_volumetric_coverage_is_enforced():
    with pytest.raises(ValueError):
        VolumetricCharacterisation.create(
            document_id=DOC,
            display_state_ref=STATE_REF,
            patch_set_ref=PATCH_SET_REF,
            grid_size=9,
            measured_count=100,
            measured_at_utc=T0,
        )


def test_da15_characterisation_plan_seals():
    plan = CharacterisationPlan.create(
        document_id=DOC,
        display_state_ref=STATE_REF,
        required_capability='volumetric_characterisation',
        minimum_grid_size=9,
        basis='measured channel coupling exceeds matrix tolerance',
        planned_at_utc=T0,
    )
    assert plan.plan_id.startswith('charplan-')


# ---------------------------------------------------------------------------
# #647 — temporal display fidelity (TD fixtures)
# ---------------------------------------------------------------------------


def _td_state(**over) -> TemporalDisplayState:
    args = dict(
        document_id=DOC,
        display_state_ref=STATE_REF,
        input_frame_rate_hz=60.0,
        refresh_rate_hz=120.0,
        vrr='off',
        motion_interpolation='off',
        black_frame_insertion='off',
        overdrive_mode='medium',
        low_latency_mode='on',
    )
    args.update(over)
    return TemporalDisplayState.create(**args)


def _td_step(state: TemporalDisplayState, **over) -> TemporalStepResponseMeasurement:
    args = dict(
        document_id=DOC,
        state_ref=AuthorityRef(
            kind='temporal_display_state',
            ref_id=state.state_id,
            ref_sha256=state.state_sha256,
        ),
        stimulus_ref=STIMULUS_REF,
        start_level_percent=0.0,
        end_level_percent=100.0,
        transition_label='black-to-white',
        definitions=StepResponseDefinition(
            rise_threshold_low_percent=10.0,
            rise_threshold_high_percent=90.0,
            settle_tolerance_percent=5.0,
        ),
        waveform_sha256=SHA_F,
        rise_ms=4.0,
        fall_ms=4.5,
        settle_ms=6.0,
        overshoot_percent=2.0,
        sensor_bandwidth_hz=10000.0,
        measured_at_utc=T0,
    )
    args.update(over)
    return TemporalStepResponseMeasurement.create(**args)


def test_td10_claims_need_in_state_measurements():
    state = _td_state()
    qualification = evaluate_temporal_qualification(
        state,
        step_responses=(),
        motion=(),
        flicker=(),
        retention=(),
        claims=('response_time', 'flicker_free'),
        response_time_threshold_ms=16.0,
        flicker_depth_threshold_percent=5.0,
        evaluated_at_utc=T1,
    )
    assert qualification.qualification_id.startswith('tdq-')
    verdicts = {v.kind: v for v in qualification.claim_verdicts}
    assert verdicts['response_time'].verdict == 'insufficient_evidence'
    assert verdicts['response_time'].reason == 'INSUFFICIENT_MEASUREMENTS'
    assert verdicts['flicker_free'].verdict == 'insufficient_evidence'


def test_td11_fast_settling_step_supports_response_claim():
    state = _td_state()
    step = _td_step(state)
    qualification = evaluate_temporal_qualification(
        state,
        step_responses=(step,),
        motion=(),
        flicker=(),
        retention=(),
        claims=('response_time',),
        response_time_threshold_ms=16.0,
        flicker_depth_threshold_percent=5.0,
        evaluated_at_utc=T1,
    )
    verdict = qualification.claim_verdicts[0]
    assert verdict.verdict == 'supported'
    assert verdict.reason == 'TEMPORAL_QUALIFIED'


def test_td12_other_state_measurements_do_not_count():
    state = _td_state()
    other = _td_state(overdrive_mode='high')
    step_elsewhere = _td_step(other)
    qualification = evaluate_temporal_qualification(
        state,
        step_responses=(step_elsewhere,),
        motion=(),
        flicker=(),
        retention=(),
        claims=('response_time',),
        response_time_threshold_ms=16.0,
        flicker_depth_threshold_percent=5.0,
        evaluated_at_utc=T1,
    )
    assert qualification.claim_verdicts[0].verdict == 'insufficient_evidence'


def test_td13_step_response_needs_a_level_change():
    state = _td_state()
    with pytest.raises(ValueError):
        _td_step(state, end_level_percent=0.0)


# ---------------------------------------------------------------------------
# #666 — LUT closed-loop calibration (LUT fixtures)
# ---------------------------------------------------------------------------


def _lut_domains() -> LutDomainSpec:
    return LutDomainSpec(
        input_code_domain='legal_narrow',
        internal_domain='normalized_float',
        output_code_domain='legal_narrow',
        device_expected_scale='legal_narrow',
        signal_path_range='legal_narrow',
    )


def _lut_artifact() -> DisplayLUTArtifact:
    return DisplayLUTArtifact.create(
        document_id=DOC,
        kind='3d_lut',
        grid_size=17,
        precision_bits=16,
        domains=_lut_domains(),
        target=LutTargetSpec(
            primaries='bt709',
            white_point='d65',
            eotf='gamma_2_4',
        ),
        artifact_payload_sha256=SHA_E,
    )


def _lut_generation(artifact: DisplayLUTArtifact) -> LUTGenerationRecord:
    return LUTGenerationRecord.create(
        document_id=DOC,
        artifact_ref=AuthorityRef(
            kind='display_lut_artifact',
            ref_id=artifact.artifact_id,
            ref_sha256=artifact.artifact_sha256,
        ),
        display_state_ref=STATE_REF,
        characterisation_ref=CHAR_REF,
        patch_set_ref=PATCH_SET_REF,
        generation=LutGenerationSpec(
            engine='htdt-cal',
            engine_version='1.2.0',
            interpolation='tetrahedral',
            gamut_mapping='perceptual_clip',
        ),
        generated_at_utc=T0,
    )


def _lut_preflight(artifact: DisplayLUTArtifact) -> LUTPreflightVerification:
    return LUTPreflightVerification.create(
        document_id=DOC,
        artifact_ref=AuthorityRef(
            kind='display_lut_artifact',
            ref_id=artifact.artifact_id,
            ref_sha256=artifact.artifact_sha256,
        ),
        numeric_validation_passed=True,
        simulation_kind='numeric_only',
        domain_scaling_checked=True,
        verified_at_utc=T0,
    )


def _lut_deployment(
    artifact: DisplayLUTArtifact, **over
) -> LUTDeploymentRecord:
    args = dict(
        document_id=DOC,
        artifact_ref=AuthorityRef(
            kind='display_lut_artifact',
            ref_id=artifact.artifact_id,
            ref_sha256=artifact.artifact_sha256,
        ),
        device_instance='VP-1',
        device_model='Lumagen Radiance Pro',
        slot='3dlut-a',
        write_method='vendor_api',
        deployed_at_utc=T0,
        readback_sha256=SHA_E,
    )
    args.update(over)
    return LUTDeploymentRecord.create(**args)


def _lut_post(
    deployment: LUTDeploymentRecord, **over
) -> LUTPostVerification:
    args = dict(
        document_id=DOC,
        deployment_ref=AuthorityRef(
            kind='lut_deployment_record',
            ref_id=deployment.deployment_id,
            ref_sha256=deployment.deployment_sha256,
        ),
        holdout_patch_set_ref=AuthorityRef(
            kind='patch_set', ref_id='pset-holdout', ref_sha256=SHA_D
        ),
        same_physical_path=True,
        residuals_delta_e=(0.4, 0.6, 0.8),
        pass_threshold_delta_e=1.5,
        verified_at_utc=T1,
        passed=True,
    )
    args.update(over)
    return LUTPostVerification.create(**args)


def test_lut10_closed_loop_requires_every_stage():
    artifact = _lut_artifact()
    qualification = evaluate_lut_closed_loop(
        artifact,
        generation=_lut_generation(artifact),
        preflight=_lut_preflight(artifact),
        deployment=_lut_deployment(artifact),
        post_verification=_lut_post(_lut_deployment(artifact)),
        generation_patch_set_sha256=SHA_C,
        evaluated_at_utc=T1,
    )
    assert qualification.verdict == 'loop_closed'
    assert qualification.qualification_id.startswith('lutq-')


def test_lut11_missing_generation_is_insufficient_evidence():
    artifact = _lut_artifact()
    qualification = evaluate_lut_closed_loop(
        artifact,
        generation=None,
        preflight=_lut_preflight(artifact),
        deployment=_lut_deployment(artifact),
        post_verification=None,
        generation_patch_set_sha256=SHA_C,
        evaluated_at_utc=T1,
    )
    assert 'GENERATION_PROVENANCE_MISSING' in qualification.reasons
    assert qualification.verdict in (
        'insufficient_evidence', 'loop_unverified'
    )


def test_lut12_preflight_missing_is_unverified():
    artifact = _lut_artifact()
    qualification = evaluate_lut_closed_loop(
        artifact,
        generation=_lut_generation(artifact),
        preflight=None,
        deployment=_lut_deployment(artifact),
        post_verification=None,
        generation_patch_set_sha256=SHA_C,
        evaluated_at_utc=T1,
    )
    assert qualification.verdict == 'loop_unverified'
    assert 'PREFLIGHT_MISSING' in qualification.reasons


def test_lut13_readback_mismatch_fails_the_loop():
    artifact = _lut_artifact()
    qualification = evaluate_lut_closed_loop(
        artifact,
        generation=_lut_generation(artifact),
        preflight=_lut_preflight(artifact),
        deployment=_lut_deployment(artifact, readback_sha256=SHA_F),
        post_verification=None,
        generation_patch_set_sha256=SHA_C,
        evaluated_at_utc=T1,
    )
    assert qualification.verdict == 'loop_failed'
    assert 'DEPLOYMENT_READBACK_MISMATCH' in qualification.reasons


def test_lut14_holdout_must_be_independent_of_generation_set():
    artifact = _lut_artifact()
    post = _lut_post(
        _lut_deployment(artifact),
        holdout_patch_set_ref=PATCH_SET_REF,
    )
    qualification = evaluate_lut_closed_loop(
        artifact,
        generation=_lut_generation(artifact),
        preflight=_lut_preflight(artifact),
        deployment=_lut_deployment(artifact),
        post_verification=post,
        generation_patch_set_sha256=SHA_C,
        evaluated_at_utc=T1,
    )
    assert qualification.verdict == 'loop_unverified'
    assert 'HOLDOUT_NOT_INDEPENDENT' in qualification.reasons


def test_lut15_observed_side_effects_cap_at_limited():
    artifact = _lut_artifact()
    post = _lut_post(
        _lut_deployment(artifact), side_effects=('gradation_loss',)
    )
    qualification = evaluate_lut_closed_loop(
        artifact,
        generation=_lut_generation(artifact),
        preflight=_lut_preflight(artifact),
        deployment=_lut_deployment(artifact),
        post_verification=post,
        generation_patch_set_sha256=SHA_C,
        evaluated_at_utc=T1,
    )
    assert qualification.verdict == 'loop_limited'
    assert 'SIDE_EFFECT_OBSERVED' in qualification.reasons


def test_lut16_hdr_target_needs_peak_luminance():
    with pytest.raises(ValueError):
        LutTargetSpec(
            primaries='bt2020', white_point='d65', eotf='pq_st2084'
        )


def test_lut17_domains_must_be_declared():
    with pytest.raises(ValueError):
        LutDomainSpec(
            input_code_domain='unknown',
            internal_domain='normalized_float',
            output_code_domain='legal_narrow',
            device_expected_scale='legal_narrow',
            signal_path_range='legal_narrow',
        )


# ---------------------------------------------------------------------------
# Repository round-trip + integrity + schema surface
# ---------------------------------------------------------------------------


def test_pg50_repository_roundtrip_and_integrity(tmp_path):
    scene = _scene_repo(tmp_path)
    repo = CadPatternGeneratorFidelityRepository(scene)
    generator = _pg_generator()
    patch = _pg_patch()
    observation = _pg_observation(patch, generator)
    qualification = evaluate_generator_fidelity(
        generator, (patch,), {patch.patch_id: observation},
        requirement=_pg_requirement(), issued_at_utc=T0,
    )
    repo.save_generator(generator)
    repo.save_patch(patch)
    repo.save_observation(observation)
    repo.save_qualification(qualification)
    assert repo.get_generator(generator.generator_id) == generator
    assert repo.get_patch(patch.patch_id) == patch
    assert repo.get_observation(observation.observation_id) == observation
    assert (
        repo.get_qualification(qualification.qualification_id)
        == qualification
    )
    repo.save_generator(generator)  # idempotent re-save
    other = PatternGeneratorInstance.create(
        document_id=DOC,
        generator_class='software_generator',
        manufacturer='ACME',
        model='DifferentGen',
        instance_serial='VF-9999',
    )
    object.__setattr__(
        other, 'generator_id', generator.generator_id
    )
    with pytest.raises(DisplayMetrologyIntegrityError):
        repo.save_generator(other)
    _tamper(
        tmp_path / 'cad.sqlite3',
        "UPDATE cad_pg_generator_instances SET model='tampered' "
        'WHERE generator_id=?',
        (generator.generator_id,),
    )
    with pytest.raises(DisplayMetrologyIntegrityError):
        repo.get_generator(generator.generator_id)


def test_mm50_repository_roundtrip(tmp_path):
    scene = _scene_repo(tmp_path)
    repo = CadMeterMatchRepository(scene)
    match = _mm_profile()
    verification = _mm_verification(match)
    observation = ProbeMatchObservation.create(
        document_id=DOC,
        match_ref=AuthorityRef(
            kind='display_meter_match_profile',
            ref_id=match.match_id,
            ref_sha256=match.match_sha256,
        ),
        readings=(
            PatchReadingPair(
                patch_ref=PATCH_SET_REF,
                target_xyz=(20.0, 21.0, 22.0),
                reference_xyz=(20.1, 21.0, 21.9),
                residual_delta_e=0.3,
            ),
        ),
        observed_at_utc=T0,
    )
    applicability = evaluate_correction_applicability(
        match,
        current_instrument=_mm_instrument(),
        current_display_state=_mm_display_state(),
        current_correction_state=PreExistingCorrection(
            state='raw_default_response'
        ),
        verification=verification,
        evaluated_at_utc=T1,
    )
    repo.save_profile(match)
    repo.save_observation(observation)
    repo.save_verification(verification)
    repo.save_applicability(applicability)
    assert repo.get_profile(match.match_id) == match
    assert repo.get_observation(observation.observation_id) == observation
    assert repo.get_verification(verification.verification_id) == verification
    assert (
        repo.get_applicability(applicability.applicability_id)
        == applicability
    )


def test_da50_repository_roundtrip(tmp_path):
    scene = _scene_repo(tmp_path)
    repo = CadDisplayAdditivityRepository(scene)
    observation = _da_observation()
    holdout = _da_holdout()
    eligibility = evaluate_model_eligibility(
        document_id=DOC,
        display_state_ref=STATE_REF,
        model_family='matrix_1d',
        additivity=observation,
        separation=None,
        volumetric=None,
        holdout=holdout,
        additivity_tolerance=1.0,
        coupling_tolerance_delta_e=2.0,
        evaluated_at_utc=T1,
    )
    repo.save_observation(observation)
    repo.save_holdout(holdout)
    repo.save_eligibility(eligibility)
    assert repo.get_observation(observation.observation_id) == observation
    assert repo.get_holdout(holdout.verification_id) == holdout
    assert (
        repo.get_eligibility(eligibility.eligibility_id) == eligibility
    )


def test_td50_repository_roundtrip(tmp_path):
    scene = _scene_repo(tmp_path)
    repo = CadTemporalDisplayRepository(scene)
    state = _td_state()
    step = _td_step(state)
    qualification = evaluate_temporal_qualification(
        state,
        step_responses=(step,),
        motion=(),
        flicker=(),
        retention=(),
        claims=('response_time',),
        response_time_threshold_ms=16.0,
        flicker_depth_threshold_percent=5.0,
        evaluated_at_utc=T1,
    )
    repo.save_state(state)
    repo.save_step_response(step)
    repo.save_qualification(qualification)
    assert repo.get_state(state.state_id) == state
    assert repo.get_step_response(step.measurement_id) == step
    assert (
        repo.get_qualification(qualification.qualification_id)
        == qualification
    )


def test_lut50_repository_roundtrip(tmp_path):
    scene = _scene_repo(tmp_path)
    repo = CadLutClosedLoopRepository(scene)
    artifact = _lut_artifact()
    generation = _lut_generation(artifact)
    preflight = _lut_preflight(artifact)
    deployment = _lut_deployment(artifact)
    post = _lut_post(deployment)
    qualification = evaluate_lut_closed_loop(
        artifact,
        generation=generation,
        preflight=preflight,
        deployment=deployment,
        post_verification=post,
        generation_patch_set_sha256=SHA_C,
        evaluated_at_utc=T1,
    )
    repo.save_artifact(artifact)
    repo.save_generation(generation)
    repo.save_preflight(preflight)
    repo.save_deployment(deployment)
    repo.save_post_verification(post)
    repo.save_qualification(qualification)
    assert repo.get_artifact(artifact.artifact_id) == artifact
    assert repo.get_generation(generation.generation_id) == generation
    assert repo.get_preflight(preflight.preflight_id) == preflight
    assert repo.get_deployment(deployment.deployment_id) == deployment
    assert (
        repo.get_post_verification(post.post_verification_id) == post
    )
    assert (
        repo.get_qualification(qualification.qualification_id)
        == qualification
    )
    assert qualification.verdict == 'loop_closed'


def test_displaymeas_tables_exist_after_fresh_migrate(tmp_path):
    scene = _scene_repo(tmp_path)
    conn = sqlite3.connect(tmp_path / 'cad.sqlite3')
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    expected = {
        'cad_pg_generator_instances',
        'cad_pg_requested_patches',
        'cad_pg_delivered_observations',
        'cad_pg_fidelity_qualifications',
        'cad_mm_match_profiles',
        'cad_mm_match_observations',
        'cad_mm_verifications',
        'cad_mm_applicability',
        'cad_da_additivity_observations',
        'cad_da_separation_assessments',
        'cad_da_volumetric_characterisations',
        'cad_da_holdout_verifications',
        'cad_da_model_eligibility',
        'cad_da_characterisation_plans',
        'cad_td_states',
        'cad_td_step_responses',
        'cad_td_motion_measurements',
        'cad_td_flicker_measurements',
        'cad_td_retention_observations',
        'cad_td_qualifications',
        'cad_lut_artifacts',
        'cad_lut_generation_records',
        'cad_lut_preflight_verifications',
        'cad_lut_deployments',
        'cad_lut_post_verifications',
        'cad_lut_qualifications',
    }
    assert expected <= tables
    assert scene is not None
