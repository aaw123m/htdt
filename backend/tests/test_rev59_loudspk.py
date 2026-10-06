"""REV59-LOUDSPK regression tests — large-signal mechanics (#754), source
normalization (#734), thermal compression (#731), microphone incidence
(#732), same-channel arrays (#737), grille transfer (#735)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_grille_transfer import (
    FrontLayerApplicability,
    GrilleTransferEvidence,
    LoudspeakerFrontLayer,
    evaluate_front_layer,
)
from htdt.cad_large_signal import (
    ExcursionCapability,
    LargeSignalTransducerModel,
    VentFlowCapability,
    compare_excursion_datums,
    evaluate_output_limit,
)
from htdt.cad_loudspeaker_evidence_repository import (
    CadLoudspeakerEvidenceRepository,
    LoudspeakerEvidenceIntegrityError,
)
from htdt.cad_microphone_incidence import (
    MeasurementMicrophoneDirectionalProfile,
    MicrophoneIncidenceApplicability,
    ReceiverOrientationState,
    evaluate_incidence,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_source_normalization import (
    AbsoluteAcousticOutputAnchor,
    LoudspeakerSourceNormalization,
    ReferenceDriveCondition,
    convert_reference_drive,
    evaluate_absolute_prediction,
)
from htdt.cad_surround_array import (
    ArrayAcousticQualification,
    ArrayMember,
    ArrayReproductionMode,
    SameChannelSpeakerArray,
    assert_member_adjustment,
    evaluate_array_qualification,
)
from htdt.cad_thermal_compression import (
    CompressionSample,
    RecoveryProfile,
    SustainedOutputTest,
    ThermalCompressionObservation,
    claim_output_capability,
)
from htdt.canonical_json import canonical_sha256


DOC = 'doc-loudspk'
_SHA = canonical_sha256({'fixture': 'sha'})
_OTHER_SHA = canonical_sha256({'fixture': 'other'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _sealed(record: object, sha_field: str) -> bool:
    """Recompute the canonical seal from the identity payload."""
    return getattr(record, sha_field) == canonical_sha256(
        record.identity_payload()
    )


# ---------------------------------------------------------------------------
# #754 large-signal mechanics
# ---------------------------------------------------------------------------


def _ls_model(**overrides) -> LargeSignalTransducerModel:
    kwargs = dict(
        document_id=DOC,
        model_id='m1',
        model_sha256=_SHA,
        transducer_ref=_ref('speaker', 'sp1'),
        evidence_class='iec_62458_parameter_measurement',
        enclosure_alignment='bass_reflex',
        nonlinear_parameter_kinds=('bl_x', 'kms_x'),
        level_applicability='small_signal_only',
    )
    kwargs.update(overrides)
    return LargeSignalTransducerModel(**kwargs)


def test_large_signal_seal() -> None:
    rec = LargeSignalTransducerModel.create(
        document_id=DOC,
        transducer_ref=_ref('speaker', 'sp1'),
        evidence_class='iec_62458_parameter_measurement',
        enclosure_alignment='bass_reflex',
        nonlinear_parameter_kinds=('bl_x', 'kms_x'),
        level_applicability='small_signal_only',
    )
    assert rec.model_id.startswith('lstm-')
    assert _sealed(rec, 'model_sha256')
    tampered = rec.model_copy(update={'enclosure_alignment': 'sealed'})
    assert not _sealed(tampered, 'model_sha256')


def test_large_signal_validated_level_requires_measured() -> None:
    with pytest.raises(ValidationError):
        _ls_model(
            level_applicability='large_signal_validated_to_level',
            evidence_class='manufacturer_declared_xmax',
            validation_ref=_ref('measurement', 'v1'),
        )
    ok = _ls_model(
        level_applicability='large_signal_validated_to_level',
        evidence_class='iec_60268_22_large_signal_measurement',
        validation_ref=_ref('measurement', 'v1'),
    )
    assert ok.level_applicability == 'large_signal_validated_to_level'


def test_large_signal_unknown_params_rejected() -> None:
    with pytest.raises(ValidationError):
        _ls_model(nonlinear_parameter_kinds=('bl_x', 'unknown'))


def test_output_limit_verdicts() -> None:
    verdict, _ = evaluate_output_limit(_ls_model(), 'driver_excursion')
    assert verdict == 'small_signal_only'
    verdict, _ = evaluate_output_limit(
        _ls_model(level_applicability='output_compression_model_available'),
        'thermal_limiter',
    )
    assert verdict == 'protection_limited'
    verdict, _ = evaluate_output_limit(
        _ls_model(
            evidence_class='iec_60268_22_large_signal_measurement',
            level_applicability='output_compression_model_available',
        ),
        'driver_excursion',
    )
    assert verdict == 'measured_bounded'
    verdict, _ = evaluate_output_limit(
        _ls_model(
            evidence_class='manufacturer_declared_xmax',
            level_applicability='output_compression_model_available',
        ),
        'driver_excursion',
    )
    assert verdict == 'model_bounded'
    verdict, _ = evaluate_output_limit(
        _ls_model(level_applicability='unknown_level_applicability'),
        'unknown',
    )
    assert verdict == 'insufficient_evidence'
    verdict, _ = evaluate_output_limit(
        _ls_model(
            evidence_class='user_entered',
            nonlinear_parameter_kinds=(),
            level_applicability='output_compression_model_available',
        ),
        'driver_excursion',
    )
    assert verdict == 'insufficient_evidence'


def test_excursion_datum_comparison() -> None:
    a = ExcursionCapability(
        document_id=DOC, capability_id='e1', capability_sha256=_SHA,
        transducer_ref=_ref('speaker', 'sp1'),
        evidence_class='iec_60268_22_large_signal_measurement',
        definition_basis='performance_based',
        convention='one_way_peak',
        distortion_criterion='thd_10',
        value_mm=6.0,
        standard_ref=_ref('standard', 'iec60268-22'),
    )
    b = a.model_copy(
        update={'capability_id': 'e2', 'capability_sha256': _OTHER_SHA}
    )
    assert compare_excursion_datums(a, b) == ('equivalent', ())
    mismatched = b.model_copy(update={'convention': 'peak_to_peak'})
    assert compare_excursion_datums(a, mismatched)[0] == (
        'convention_mismatch'
    )
    unknown = b.model_copy(update={'convention': 'unknown'})
    assert compare_excursion_datums(a, unknown)[0] == 'incomparable'


def test_excursion_standard_classes_require_standard_ref() -> None:
    with pytest.raises(ValidationError):
        ExcursionCapability(
            document_id=DOC, capability_id='e1', capability_sha256=_SHA,
            transducer_ref=_ref('speaker', 'sp1'),
            evidence_class='standard_profiled_xmax',
            definition_basis='performance_based',
            convention='one_way_peak',
            value_mm=6.0,
        )


def test_vent_flow_requires_vented_alignment() -> None:
    with pytest.raises(ValidationError):
        VentFlowCapability(
            document_id=DOC, capability_id='v1', capability_sha256=_SHA,
            system_ref=_ref('system', 's1'),
            mechanism='port_flow_turbulence_limit',
            enclosure_alignment='sealed',
        )
    ok = VentFlowCapability(
        document_id=DOC, capability_id='v1', capability_sha256=_SHA,
        system_ref=_ref('system', 's1'),
        mechanism='port_flow_turbulence_limit',
        enclosure_alignment='bass_reflex',
    )
    assert ok.mechanism == 'port_flow_turbulence_limit'


# ---------------------------------------------------------------------------
# #734 source normalization / absolute output
# ---------------------------------------------------------------------------


def _snrm(**overrides) -> LoudspeakerSourceNormalization:
    kwargs = dict(
        document_id=DOC,
        normalization_id='n1',
        normalization_sha256=_SHA,
        dataset_ref=_ref('dataset', 'sp1-spin'),
        capability='absolute_spl_at_reference_drive',
        normalization_method='absolute_spl_per_direction',
    )
    kwargs.update(overrides)
    return LoudspeakerSourceNormalization(**kwargs)


def _anchor(**overrides) -> AbsoluteAcousticOutputAnchor:
    kwargs = dict(
        document_id=DOC,
        anchor_id='a1',
        anchor_sha256=_SHA,
        normalization_ref=_ref('snrm', 'n1'),
        drive_ref=_ref('rdrv', 'd1'),
        reference_distance_m=1.0,
        acoustic_origin_ref=_ref('origin', 'o1'),
        linear_scaling='linear_scaling_eligible',
        verdict='absolute_spl_prediction_eligible',
    )
    kwargs.update(overrides)
    return AbsoluteAcousticOutputAnchor(**kwargs)


def test_source_normalization_absolute_rejects_per_freq_norm() -> None:
    with pytest.raises(ValidationError):
        _snrm(
            capability='absolute_spl_at_reference_drive',
            normalization_method='normalized_per_frequency',
        )


def test_absolute_prediction_verdicts() -> None:
    relative = _snrm(
        capability='relative_shape_only',
        normalization_method='normalized_to_on_axis',
    )
    assert evaluate_absolute_prediction(relative, _anchor()) == (
        'relative_response_only',
        ('dataset_is_relative',),
    )
    abs_rec = _snrm(capability='maximum_output_capability')
    verdict, _ = evaluate_absolute_prediction(abs_rec, _anchor())
    assert verdict == 'absolute_spl_prediction_eligible'
    verdict, _ = evaluate_absolute_prediction(abs_rec, None)
    assert verdict == 'insufficient_source_level_evidence'
    limited_anchor = _anchor(linear_scaling='linear_scaling_with_limits')
    verdict, _ = evaluate_absolute_prediction(abs_rec, limited_anchor)
    assert verdict == 'absolute_with_limitations'
    verdict, _ = evaluate_absolute_prediction(
        _snrm(capability='unknown'), _anchor()
    )
    assert verdict == 'insufficient_source_level_evidence'


def test_drive_conversion_needs_impedance() -> None:
    src = ReferenceDriveCondition(
        document_id=DOC, condition_id='c1', condition_sha256=_SHA,
        quantity_kind='analogue_voltage_rms', value=2.83,
    )
    assert convert_reference_drive(src, 'analogue_power', None)[0] == (
        'equivalence_blocked'
    )
    assert convert_reference_drive(
        src, 'analogue_power', _ref('impedance', 'z1')
    )[0] == 'conversion_permitted'
    assert convert_reference_drive(src, 'analogue_voltage_rms', None)[0] == (
        'conversion_permitted'
    )
    assert convert_reference_drive(src, 'digital_dbfs', None)[0] == (
        'incomparable'
    )


def test_typed_drive_quantity_requires_value() -> None:
    with pytest.raises(ValidationError):
        ReferenceDriveCondition(
            document_id=DOC, condition_id='c1', condition_sha256=_SHA,
            quantity_kind='analogue_voltage_rms', value=None,
        )


# ---------------------------------------------------------------------------
# #731 sustained output / thermal compression
# ---------------------------------------------------------------------------


def _sout(**overrides) -> SustainedOutputTest:
    kwargs = dict(
        document_id=DOC,
        test_id='t1',
        test_sha256=_SHA,
        device_ref=_ref('speaker', 'sp1'),
        capability_class='sustained_output',
        initial_thermal_state='normal_warmed_operation',
        duration_s=300.0,
    )
    kwargs.update(overrides)
    return SustainedOutputTest(**kwargs)


def _obs(**overrides) -> ThermalCompressionObservation:
    kwargs = dict(
        document_id=DOC,
        observation_id='o1',
        observation_sha256=_SHA,
        test_ref=_ref('sout', 't1'),
        suspected_cause='voice_coil_thermal',
        samples=(CompressionSample(elapsed_s=10.0, loss_db=0.5),),
    )
    kwargs.update(overrides)
    return ThermalCompressionObservation(**kwargs)


def test_sustained_requires_duration() -> None:
    with pytest.raises(ValidationError):
        _sout(capability_class='sustained_output', duration_s=None)
    burst = _sout(capability_class='short_burst_peak', duration_s=None)
    assert burst.capability_class == 'short_burst_peak'


def test_compression_chain_overload_forbids_dut_cause() -> None:
    with pytest.raises(ValidationError):
        _obs(
            suspected_cause='voice_coil_thermal',
            measurement_chain_overload=True,
        )


def test_claim_output_capability_fail_closed() -> None:
    burst = _sout(capability_class='short_burst_peak', duration_s=None)
    assert claim_output_capability(burst, _obs(), 'sustained_output')[0] == (
        'burst_only_evidence'
    )
    assert claim_output_capability(
        burst, _obs(), 'short_burst_peak'
    )[0] == 'sustained_verified'
    protection = _obs(suspected_cause='dsp_limiter')
    assert claim_output_capability(
        burst, protection, 'short_burst_peak'
    )[0] == 'protection_limited'
    overload = _obs(
        suspected_cause='unknown',
        measurement_chain_overload=True,
    )
    assert claim_output_capability(
        burst, overload, 'short_burst_peak'
    )[0] == 'measurement_chain_limited'
    assert claim_output_capability(None, _obs(), 'sustained_output')[0] == (
        'insufficient_evidence'
    )
    sustained = _sout()
    assert claim_output_capability(
        sustained, _obs(), 'sustained_output'
    )[0] == 'sustained_verified'
    assert claim_output_capability(
        sustained, None, 'sustained_output'
    )[0] == 'insufficient_evidence'


def test_recovery_full_requires_baseline() -> None:
    with pytest.raises(ValidationError):
        RecoveryProfile(
            document_id=DOC, profile_id='r1', profile_sha256=_SHA,
            test_ref=_ref('sout', 't1'),
            recovery_state='fully_recovered',
            baseline_within_db=None,
        )


# ---------------------------------------------------------------------------
# #732 microphone incidence
# ---------------------------------------------------------------------------


def _mdpf(**overrides) -> MeasurementMicrophoneDirectionalProfile:
    kwargs = dict(
        document_id=DOC,
        profile_id='p1',
        profile_sha256=_SHA,
        microphone_ref=_ref('mic', 'm1'),
        calibration_field_kind='free_field_sensitivity',
    )
    kwargs.update(overrides)
    return MeasurementMicrophoneDirectionalProfile(**kwargs)


def _rors(**overrides) -> ReceiverOrientationState:
    kwargs = dict(
        document_id=DOC,
        state_id='s1',
        state_sha256=_SHA,
        orientation_frame='world_fixed',
        yaw_deg=0.0,
    )
    kwargs.update(overrides)
    return ReceiverOrientationState(**kwargs)


def test_directional_profile_dataset_requires_grids() -> None:
    with pytest.raises(ValidationError):
        _mdpf(calibration_field_kind='angular_response_dataset')


def test_world_fixed_orientation_requires_yaw() -> None:
    with pytest.raises(ValidationError):
        _rors(yaw_deg=None)


def test_incidence_verdict_ladder() -> None:
    profile = _mdpf()
    orientation = _rors()
    # 0 deg incidence without a declared frequency gate applies directly.
    verdict, scope, _ = evaluate_incidence(
        profile, orientation, 'direct_source', 0.0, None
    )
    assert verdict == 'directly_applicable'
    # With frequency declared and no angular evidence the profile stays
    # honest about the unknown off-axis response even on-axis.
    verdict, _, reasons = evaluate_incidence(
        profile, orientation, 'direct_source', 0.0, 500.0
    )
    assert verdict == 'limited'
    assert 'angle_sensitive_band' in reasons
    # Off-axis without angular evidence degrades further.
    verdict, _, _ = evaluate_incidence(
        profile, orientation, 'direct_source', 30.0, 500.0
    )
    assert verdict == 'limited'
    verdict, _, _ = evaluate_incidence(
        profile, orientation, 'direct_source', 60.0, 500.0
    )
    assert verdict == 'incompatible'
    # Pressure calibration is angle-independent: honest where angular
    # effects are negligible, limited once they are not.
    pressure = _mdpf(
        calibration_field_kind='pressure_sensitivity',
        angle_sensitive_above_hz=250.0,
    )
    verdict, _, reasons = evaluate_incidence(
        pressure, orientation, 'direct_source', 0.0, 500.0,
    )
    assert verdict == 'limited'
    assert 'pressure_calibration_not_free_field' in reasons
    verdict, _, _ = evaluate_incidence(
        pressure, orientation, 'direct_source', 0.0, 100.0,
    )
    assert verdict == 'angular_effect_negligible_within_evidence'
    # A diffuse-field quantity cannot claim a direct-path measurand.
    verdict, _, _ = evaluate_incidence(
        _mdpf(calibration_field_kind='diffuse_field_sensitivity'),
        orientation, 'direct_source', 0.0, 500.0,
    )
    assert verdict == 'limited'
    # Diffuse measurand on diffuse calibration applies directly.
    verdict, _, _ = evaluate_incidence(
        _mdpf(calibration_field_kind='diffuse_field_sensitivity'),
        orientation, 'room_response_full', 45.0, 500.0,
    )
    assert verdict == 'directly_applicable'
    # Unknown calibration kind is always fail-closed.
    verdict, _, _ = evaluate_incidence(
        _mdpf(calibration_field_kind='unknown'),
        orientation, 'direct_source', 0.0, None,
    )
    assert verdict == 'unknown'


def test_incidence_measured_grid_correction() -> None:
    profile = _mdpf(
        calibration_field_kind='angular_response_dataset',
        angular_dataset_ref=_ref('dataset', 'd1'),
        angular_grid_deg=(0.0, 30.0, 60.0, 90.0),
        frequency_grid_hz=(250.0, 500.0, 1000.0),
        angle_sensitive_above_hz=1000.0,
    )
    orientation = _rors(yaw_deg=30.0)
    verdict, scope, _ = evaluate_incidence(
        profile, orientation, 'direct_source', 30.0, 1000.0
    )
    assert verdict == 'correction_available'
    assert scope == 'measured_grid'
    # Outside the frequency grid the correction is extrapolated, not
    # measured.
    verdict, scope, _ = evaluate_incidence(
        profile, orientation, 'direct_source', 30.0, 4000.0
    )
    assert verdict == 'limited'
    assert scope == 'derived_extrapolated'
    # Below the declared sensitivity threshold with no stated incidence
    # the angle does not matter.
    verdict, _, _ = evaluate_incidence(
        profile, None, 'direct_source', None, 500.0
    )
    assert verdict == 'angular_effect_negligible_within_evidence'


def test_incidence_applicability_correction_ref() -> None:
    with pytest.raises(ValidationError):
        MicrophoneIncidenceApplicability(
            document_id=DOC, applicability_id='i1', applicability_sha256=_SHA,
            profile_ref=_ref('mdpf', 'p1'),
            measurand='direct_source',
            verdict='correction_available',
            correction_scope='measured_grid',
            correction_ref=None,
        )


# ---------------------------------------------------------------------------
# #737 same-channel surround arrays
# ---------------------------------------------------------------------------


def _array(**overrides) -> SameChannelSpeakerArray:
    members = (
        ArrayMember(speaker_ref=_ref('speaker', 'a')),
        ArrayMember(speaker_ref=_ref('speaker', 'b')),
    )
    kwargs = dict(
        document_id=DOC,
        array_id='ar1',
        array_sha256=_SHA,
        logical_channel_ref=_ref('channel', 'Ls'),
        topology='independent_output_fixed_array',
        members=members,
    )
    kwargs.update(overrides)
    return SameChannelSpeakerArray(**kwargs)


def _qual(**overrides) -> ArrayAcousticQualification:
    kwargs = dict(
        document_id=DOC,
        qualification_id='q1',
        qualification_sha256=_SHA,
        array_ref=_ref('scar', 'ar1'),
        mode_ref=_ref('armd', 'm1'),
        measured_seat_refs=(_ref('seat', 's1'), _ref('seat', 's2')),
        holdout_seat_refs=(_ref('seat', 's3'),),
        summed_evidence_ref=_ref('measurement', 'm9'),
        level_spread_db=2.0,
        spectral_spread_db=3.0,
        localization_impact='none_observed',
        verdict='coverage_uniform',
    )
    kwargs.update(overrides)
    return ArrayAcousticQualification(**kwargs)


def test_array_requires_two_members() -> None:
    with pytest.raises(ValidationError):
        _array(members=(ArrayMember(speaker_ref=_ref('speaker', 'a')),))


def test_shared_output_rejects_independent_member() -> None:
    members = (
        ArrayMember(speaker_ref=_ref('speaker', 'a')),
        ArrayMember(
            speaker_ref=_ref('speaker', 'b'), independent_adjustment=True
        ),
    )
    with pytest.raises(ValidationError):
        _array(
            topology='physical_shared_output_array', members=members
        )


def test_array_qualification_verdicts() -> None:
    array = _array()
    mode = ArrayReproductionMode(
        document_id=DOC, mode_id='m1', mode_sha256=_SHA,
        array_ref=_ref('scar', 'ar1'),
        render_mode='channel_7_1',
        member_behavior='arrayed_members',
    )
    verdict, _ = evaluate_array_qualification(array, mode, _qual())
    assert verdict == 'coverage_uniform'
    verdict, _ = evaluate_array_qualification(
        array, mode, _qual(cancellation_seats=1)
    )
    assert verdict == 'seat_anomaly'
    verdict, _ = evaluate_array_qualification(
        array, mode, _qual(measured_seat_refs=(_ref('seat', 's1'),))
    )
    assert verdict == 'unqualified'
    verdict, _ = evaluate_array_qualification(
        array, mode, _qual(summed_evidence_ref=None)
    )
    assert verdict == 'unqualified'
    object_mode = ArrayReproductionMode(
        document_id=DOC, mode_id='m2', mode_sha256=_OTHER_SHA,
        array_ref=_ref('scar', 'ar1'),
        render_mode='atmos_object',
        member_behavior='arrayed_members',
    )
    verdict, _ = evaluate_array_qualification(array, object_mode, _qual())
    assert verdict == 'localization_tradeoff'


def test_member_adjustment_guard() -> None:
    shared_members = (
        ArrayMember(
            speaker_ref=_ref('speaker', 'a'), independent_adjustment=False
        ),
        ArrayMember(
            speaker_ref=_ref('speaker', 'b'), independent_adjustment=False
        ),
    )
    shared = _array(
        topology='physical_shared_output_array', members=shared_members
    )
    with pytest.raises(ValueError):
        assert_member_adjustment(shared, 1)
    independent = _array()
    assert_member_adjustment(independent, 1)


# ---------------------------------------------------------------------------
# #735 front layer / grille transfer
# ---------------------------------------------------------------------------


def _layer(**overrides) -> LoudspeakerFrontLayer:
    kwargs = dict(
        document_id=DOC,
        layer_id='l1',
        layer_sha256=_SHA,
        loudspeaker_ref=_ref('speaker', 'sp1'),
        kind='perforated_metal',
        frame_present=True,
    )
    kwargs.update(overrides)
    return LoudspeakerFrontLayer(**kwargs)


def _gtrf(**overrides) -> GrilleTransferEvidence:
    kwargs = dict(
        document_id=DOC,
        evidence_id='g1',
        evidence_sha256=_SHA,
        layer_ref=_ref('lfly', 'l1'),
        evidence_class='independent_measured',
        transfer_kind='complex_transfer',
        measured_angles_deg=(0.0, 15.0, 30.0),
        spacing_min_mm=10.0,
        spacing_max_mm=60.0,
    )
    kwargs.update(overrides)
    return GrilleTransferEvidence(**kwargs)


def test_frame_kinds_require_frame_flag() -> None:
    with pytest.raises(ValidationError):
        _layer(kind='frame_plus_cloth', frame_present=False)


def test_user_measured_requires_both_captures() -> None:
    with pytest.raises(ValidationError):
        _gtrf(
            evidence_class='htdt_user_measured',
            layer_on_capture_ref=_ref('capture', 'on1'),
            layer_off_capture_ref=None,
        )


def test_front_layer_fail_closed() -> None:
    layer = _layer()
    # No layer at all: base data applies directly.
    assert evaluate_front_layer(None, None, None) == (
        'base_directivity_directly_applicable',
        (),
    )
    # Unknown or marketing evidence: never apply the bare response.
    unknown = _gtrf(evidence_class='unknown', transfer_kind='unknown')
    assert evaluate_front_layer(layer, unknown, None)[0] == 'unknown'
    assert evaluate_front_layer(layer, None, None)[0] == 'unknown'
    marketing = _gtrf(evidence_class='marketing_declared')
    assert evaluate_front_layer(layer, marketing, None)[0] == 'unknown'
    # Measured complex transfer with angular coverage, inside the
    # spacing window.
    measured = _gtrf()
    assert evaluate_front_layer(layer, measured, 30.0)[0] == (
        'installed_front_layer_directivity_available'
    )
    assert evaluate_front_layer(layer, measured, 200.0)[0] == (
        'directivity_limited'
    )
    magnitude = _gtrf(
        evidence_id='g3',
        evidence_sha256=_OTHER_SHA,
        transfer_kind='magnitude_only',
    )
    assert evaluate_front_layer(layer, magnitude, 30.0)[0] == (
        'base_directivity_plus_measured_transfer'
    )
    axis_only = _gtrf(
        evidence_id='g4',
        evidence_sha256=canonical_sha256({'x': 4}),
        transfer_kind='magnitude_only',
        measured_angles_deg=(),
    )
    assert evaluate_front_layer(layer, axis_only, 30.0)[0] == (
        'on_axis_only_correction'
    )


def test_front_layer_applicability_double_count_rejected() -> None:
    with pytest.raises(ValidationError):
        FrontLayerApplicability(
            document_id=DOC, applicability_id='f1', applicability_sha256=_SHA,
            layer_ref=_ref('lfly', 'l1'),
            base_dataset_ref=_ref('dataset', 'b1'),
            verdict='base_directivity_plus_measured_transfer',
            base_dataset_grille_state='grille_on',
        )
    ok = FrontLayerApplicability(
        document_id=DOC, applicability_id='f1', applicability_sha256=_SHA,
        layer_ref=_ref('lfly', 'l1'),
        base_dataset_ref=_ref('dataset', 'b1'),
        verdict='base_directivity_plus_measured_transfer',
        base_dataset_grille_state='grille_off',
    )
    assert ok.verdict == 'base_directivity_plus_measured_transfer'


# ---------------------------------------------------------------------------
# Repository roundtrip / idempotent / tamper
# ---------------------------------------------------------------------------


_LOUDSPK_TABLES = (
    'cad_large_signal_models',
    'cad_excursion_capabilities',
    'cad_vent_flow_capabilities',
    'cad_mechanical_output_limits',
    'cad_source_normalizations',
    'cad_reference_drive_conditions',
    'cad_absolute_output_anchors',
    'cad_sustained_output_tests',
    'cad_thermal_compression_observations',
    'cad_recovery_profiles',
    'cad_microphone_directional_profiles',
    'cad_receiver_orientation_states',
    'cad_microphone_incidence_applicability',
    'cad_same_channel_arrays',
    'cad_array_reproduction_modes',
    'cad_array_qualifications',
    'cad_loudspeaker_front_layers',
    'cad_grille_transfer_evidence',
    'cad_front_layer_applicability',
)


def _repo(tmp_path: Path) -> CadLoudspeakerEvidenceRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadLoudspeakerEvidenceRepository(scene)


def test_new_tables_exist(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with connect_sqlite(db) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    for table in _LOUDSPK_TABLES:
        assert table in names


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    model = LargeSignalTransducerModel.create(
        document_id=DOC,
        transducer_ref=_ref('speaker', 'sp1'),
        evidence_class='iec_62458_parameter_measurement',
        enclosure_alignment='bass_reflex',
        nonlinear_parameter_kinds=('bl_x', 'kms_x'),
        level_applicability='small_signal_only',
    )
    repo.save_large_signal_model(model)
    assert repo.get_large_signal_model(model.model_id) == model
    # Idempotent re-save of the same sealed record.
    repo.save_large_signal_model(model)

    norm = LoudspeakerSourceNormalization.create(
        document_id=DOC,
        dataset_ref=_ref('dataset', 'sp1-spin'),
        capability='absolute_spl_at_reference_drive',
        normalization_method='absolute_spl_per_direction',
    )
    repo.save_source_normalization(norm)
    assert repo.get_source_normalization(norm.normalization_id) == norm

    drive = ReferenceDriveCondition.create(
        document_id=DOC,
        quantity_kind='analogue_voltage_rms',
        value=2.83,
    )
    repo.save_drive_condition(drive)
    assert repo.get_drive_condition(drive.condition_id) == drive

    anchor = AbsoluteAcousticOutputAnchor.create(
        document_id=DOC,
        normalization_ref=_ref(
            'snrm', norm.normalization_id, norm.normalization_sha256
        ),
        drive_ref=_ref('rdrv', drive.condition_id, drive.condition_sha256),
        reference_distance_m=1.0,
        acoustic_origin_ref=_ref('origin', 'o1'),
        linear_scaling='linear_scaling_eligible',
        verdict='absolute_spl_prediction_eligible',
    )
    repo.save_output_anchor(anchor)
    assert repo.get_output_anchor(anchor.anchor_id) == anchor

    test = SustainedOutputTest.create(
        document_id=DOC,
        device_ref=_ref('speaker', 'sp1'),
        capability_class='sustained_output',
        initial_thermal_state='normal_warmed_operation',
        duration_s=300.0,
    )
    repo.save_sustained_test(test)
    assert repo.get_sustained_test(test.test_id) == test

    obs = ThermalCompressionObservation.create(
        document_id=DOC,
        test_ref=_ref('sout', test.test_id, test.test_sha256),
        suspected_cause='voice_coil_thermal',
        samples=(CompressionSample(elapsed_s=10.0, loss_db=0.5),),
    )
    repo.save_compression_observation(obs)
    assert repo.get_compression_observation(obs.observation_id) == obs

    recovery = RecoveryProfile.create(
        document_id=DOC,
        test_ref=_ref('sout', test.test_id, test.test_sha256),
        recovery_state='fully_recovered',
        baseline_within_db=0.2,
    )
    repo.save_recovery_profile(recovery)
    assert repo.get_recovery_profile(recovery.profile_id) == recovery

    mic = MeasurementMicrophoneDirectionalProfile.create(
        document_id=DOC,
        microphone_ref=_ref('mic', 'm1'),
        calibration_field_kind='free_field_sensitivity',
    )
    repo.save_mic_profile(mic)
    assert repo.get_mic_profile(mic.profile_id) == mic

    orientation = ReceiverOrientationState.create(
        document_id=DOC,
        orientation_frame='world_fixed',
        yaw_deg=0.0,
    )
    repo.save_orientation_state(orientation)
    assert repo.get_orientation_state(orientation.state_id) == orientation

    applic = MicrophoneIncidenceApplicability.create(
        document_id=DOC,
        profile_ref=_ref('mdpf', mic.profile_id, mic.profile_sha256),
        orientation_ref=_ref(
            'rors', orientation.state_id, orientation.state_sha256
        ),
        measurand='direct_source',
        verdict='directly_applicable',
    )
    repo.save_incidence_applicability(applic)
    assert repo.get_incidence_applicability(
        applic.applicability_id
    ) == applic

    array = SameChannelSpeakerArray.create(
        document_id=DOC,
        logical_channel_ref=_ref('channel', 'Ls'),
        topology='independent_output_fixed_array',
        members=(
            ArrayMember(speaker_ref=_ref('speaker', 'a')),
            ArrayMember(speaker_ref=_ref('speaker', 'b')),
        ),
    )
    repo.save_channel_array(array)
    assert repo.get_channel_array(array.array_id) == array

    mode = ArrayReproductionMode.create(
        document_id=DOC,
        array_ref=_ref('scar', array.array_id, array.array_sha256),
        render_mode='channel_7_1',
        member_behavior='arrayed_members',
    )
    repo.save_reproduction_mode(mode)
    assert repo.get_reproduction_mode(mode.mode_id) == mode

    qual = ArrayAcousticQualification.create(
        document_id=DOC,
        array_ref=_ref('scar', array.array_id, array.array_sha256),
        mode_ref=_ref('armd', mode.mode_id, mode.mode_sha256),
        measured_seat_refs=(_ref('seat', 's1'), _ref('seat', 's2')),
        holdout_seat_refs=(_ref('seat', 's3'),),
        summed_evidence_ref=_ref('measurement', 'm9'),
        level_spread_db=2.0,
        spectral_spread_db=3.0,
        localization_impact='none_observed',
        verdict='coverage_uniform',
    )
    repo.save_array_qualification(qual)
    assert repo.get_array_qualification(qual.qualification_id) == qual

    layer = LoudspeakerFrontLayer.create(
        document_id=DOC,
        loudspeaker_ref=_ref('speaker', 'sp1'),
        kind='perforated_metal',
        frame_present=True,
    )
    repo.save_front_layer(layer)
    assert repo.get_front_layer(layer.layer_id) == layer

    transfer = GrilleTransferEvidence.create(
        document_id=DOC,
        layer_ref=_ref('lfly', layer.layer_id, layer.layer_sha256),
        evidence_class='independent_measured',
        transfer_kind='complex_transfer',
        measured_angles_deg=(0.0, 15.0, 30.0),
        spacing_min_mm=10.0,
        spacing_max_mm=60.0,
    )
    repo.save_grille_transfer(transfer)
    assert repo.get_grille_transfer(transfer.evidence_id) == transfer

    flap = FrontLayerApplicability.create(
        document_id=DOC,
        layer_ref=None,
        base_dataset_ref=_ref('dataset', 'b1'),
        verdict='base_directivity_directly_applicable',
        base_dataset_grille_state='grille_off',
    )
    repo.save_front_layer_applicability(flap)
    assert repo.get_front_layer_applicability(
        flap.applicability_id
    ) == flap


def test_repository_tamper_detected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    model = LargeSignalTransducerModel.create(
        document_id=DOC,
        transducer_ref=_ref('speaker', 'sp1'),
        evidence_class='iec_62458_parameter_measurement',
        enclosure_alignment='bass_reflex',
        nonlinear_parameter_kinds=('bl_x', 'kms_x'),
        level_applicability='small_signal_only',
    )
    repo.save_large_signal_model(model)
    with connect_sqlite(repo.path) as conn:
        conn.execute(
            "UPDATE cad_large_signal_models SET evidence_class='unknown' "
            'WHERE model_id=?',
            (model.model_id,),
        )
        conn.commit()
    with pytest.raises(LoudspeakerEvidenceIntegrityError):
        repo.get_large_signal_model(model.model_id)
