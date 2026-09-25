"""Domain authority tests (#1039, #1040, #1041, #1043)."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_hdmi_transport import (
    HdmiFeatureEvidence,
    HDMITransportCapability,
    LipLatencyComponent,
    build_hdmi_lip_evidence,
    build_hdmi_transport_capability,
)
from htdt.cad_processing_condition import (
    build_video_processing_condition,
)
from htdt.cad_room_reflection import (
    InSituContrastMeasurement,
    RoomContrastStimulus,
    build_contrast_decomposition,
    build_in_situ_contrast_measurement,
    build_room_optical_surface_profile,
    estimate_diffuse_room_return,
)
from htdt.cad_scene import Direction3, Position3
from htdt.cad_viewing_envelope import (
    ViewingSeatBinding,
    build_viewing_resolution_policy,
    evaluate_viewing_resolution,
)


def _provenance():
    return (
        EquipmentDataProvenance(
            evidence_kind='manufacturer',
            source_name='Example Co.',
            source_version='2026.1',
            source_reference='datasheet',
            source_sha256='d' * 64,
        ),
    )


# ---- #1039 viewing envelope ----------------------------------------------


def _frontal_aperture(width=2.67, height=1.5):
    return dict(
        aperture_center=Position3(x_m=0.0, y_m=1.2, z_m=0.0),
        aperture_width_m=width,
        aperture_height_m=height,
        aperture_normal=Direction3(x=0.0, y=0.0, z=1.0),
        aperture_right=Direction3(x=1.0, y=0.0, z=0.0),
    )


def _seat(distance=3.0, seat_id='seat-1'):
    return ViewingSeatBinding(
        seat_entity_id=seat_id,
        eye_position=Position3(x_m=0.0, y_m=1.2, z_m=distance),
    )


def test_closer_seat_larger_angle_lower_ppd():
    policy = build_viewing_resolution_policy(
        policy_id='pol-4k',
        version='1',
        min_horizontal_pixels_per_degree=30.0,
        provenance=_provenance(),
    )
    near = evaluate_viewing_resolution(
        evaluation_id='',
        seats=(_seat(distance=2.0),),
        raster_width_px=3840,
        raster_height_px=2160,
        raster_kind='display_native',
        raster_semantics_exact=True,
        policy=policy,
        **_frontal_aperture(),
    )
    far = evaluate_viewing_resolution(
        evaluation_id='',
        seats=(_seat(distance=4.0),),
        raster_width_px=3840,
        raster_height_px=2160,
        raster_kind='display_native',
        raster_semantics_exact=True,
        policy=policy,
        **_frontal_aperture(),
    )
    near_r = near.seat_results[0]
    far_r = far.seat_results[0]
    assert near_r.horizontal_angle_deg > far_r.horizontal_angle_deg
    assert (
        near_r.horizontal_pixels_per_degree
        < far_r.horizontal_pixels_per_degree
    )


def test_higher_raster_higher_ppd():
    kwargs = dict(
        seats=(_seat(distance=3.0),),
        raster_kind='display_native',
        raster_semantics_exact=True,
        **_frontal_aperture(),
    )
    hd = evaluate_viewing_resolution(
        evaluation_id='', raster_width_px=1920, raster_height_px=1080,
        **kwargs,
    )
    uhd = evaluate_viewing_resolution(
        evaluation_id='', raster_width_px=3840, raster_height_px=2160,
        **kwargs,
    )
    assert (
        uhd.seat_results[0].horizontal_pixels_per_degree
        > hd.seat_results[0].horizontal_pixels_per_degree
    )


def test_cih_changes_active_width():
    scope = evaluate_viewing_resolution(
        evaluation_id='',
        seats=(_seat(distance=3.0),),
        raster_width_px=3840,
        raster_height_px=2160,
        raster_kind='display_native',
        raster_semantics_exact=True,
        presentation_mode='cih-scope',
        **_frontal_aperture(width=3.30, height=1.375),
    )
    flat = evaluate_viewing_resolution(
        evaluation_id='',
        seats=(_seat(distance=3.0),),
        raster_width_px=3840,
        raster_height_px=2160,
        raster_kind='display_native',
        raster_semantics_exact=True,
        **_frontal_aperture(width=2.44, height=1.375),
    )
    assert (
        scope.seat_results[0].horizontal_angle_deg
        > flat.seat_results[0].horizontal_angle_deg
    )


def test_unknown_scaler_interpretation_unknown():
    policy = build_viewing_resolution_policy(
        policy_id='pol-ppd',
        version='1',
        min_horizontal_pixels_per_degree=40.0,
    )
    evaluation = evaluate_viewing_resolution(
        evaluation_id='',
        seats=(_seat(distance=3.0),),
        raster_width_px=3840,
        raster_height_px=2160,
        raster_kind='source_content',
        raster_semantics_exact=False,
        policy=policy,
        **_frontal_aperture(),
    )
    result = evaluation.seat_results[0]
    # physical geometry is still reported
    assert result.horizontal_angle_deg > 0.0
    assert result.viewing_distance_m == pytest.approx(3.0)
    # ppd interpretation is UNKNOWN, not guessed
    assert result.horizontal_pixels_per_degree is None
    assert any('raster' in r for r in result.unknown_reasons)
    ppd = next(
        c for c in result.criteria
        if c.criterion == 'horizontal_pixels_per_degree'
    )
    assert ppd.status == 'UNKNOWN'
    assert result.status != 'PASS'


def test_viewing_per_seat_independent():
    evaluation = evaluate_viewing_resolution(
        evaluation_id='',
        seats=(
            _seat(distance=2.0, seat_id='seat-front'),
            _seat(distance=4.0, seat_id='seat-back'),
        ),
        raster_width_px=3840,
        raster_height_px=2160,
        raster_kind='display_native',
        raster_semantics_exact=True,
        **_frontal_aperture(),
    )
    assert len(evaluation.seat_results) == 2
    assert (
        evaluation.seat_results[0].seat_entity_id == 'seat-front'
        and evaluation.seat_results[1].seat_entity_id == 'seat-back'
    )
    assert evaluation.evaluation_id.startswith('vre-')


# ---- #1040 processing condition ------------------------------------------


def test_capability_is_not_active_condition():
    # capability flags alone never claim observed metadata presence
    with pytest.raises(ValueError, match='capability-documented'):
        build_video_processing_condition(
            condition_id='vpc-cap',
            version='1',
            supports_dynamic_metadata=True,
            dynamic_metadata_family='dolby_vision_2',
            dynamic_metadata_presence='observed',
            evidence_class='capability_documented',
        )
    # observed presence requires a real family
    with pytest.raises(ValueError, match='real metadata'):
        build_video_processing_condition(
            condition_id='vpc-bad',
            version='1',
            dynamic_metadata_family='unknown',
            dynamic_metadata_presence='observed',
        )


def test_ambient_adaptive_requires_measured_evidence():
    with pytest.raises(ValueError, match='measured ambient'):
        build_video_processing_condition(
            condition_id='vpc-amb',
            version='1',
            ambient_adaptive_state='active',
        )
    ok = build_video_processing_condition(
        condition_id='vpc-amb-ok',
        version='1',
        ambient_adaptive_state='active',
        ambient_observation_id='amb-1',
        provenance=_provenance(),
    )
    assert ok.ambient_adaptive_state == 'active'


def test_processing_condition_binds_triples():
    with pytest.raises(ValueError, match='supplied together'):
        build_video_processing_condition(
            condition_id='vpc-triple',
            version='1',
            display_specification_id='oled-77',
        )
    missing_readback = build_video_processing_condition(
        condition_id='vpc-unknown',
        version='1',
        display_specification_id='oled-77',
        display_specification_version='2026.1',
        display_specification_sha256='a' * 64,
    )
    assert missing_readback.dynamic_metadata_presence == 'unknown'
    assert missing_readback.motion_processing_mode is None


# ---- #1041 HDMI transport + LIP -------------------------------------------


def test_feature_evidence_not_version_label():
    capability = build_hdmi_transport_capability(
        capability_id='hdmi-cap-1',
        version='1',
        device_id='avr-1',
        port_label='hdmi-2',
        features=(
            HdmiFeatureEvidence(
                feature='frl', state='supported',
                max_rate_gbps=48.0, evidence_class='manufacturer',
            ),
            HdmiFeatureEvidence(
                feature='lip', state='supported',
                evidence_class='certified',
            ),
            HdmiFeatureEvidence(
                feature='qft', state='unknown',
            ),
        ),
        cable_class='ultra96',
        cable_run_id='run-1',
        cable_certification='ultra96 cert QR-4421',
        max_link_rate_gbps=96.0,
        max_rate_evidence_class='certified',
        provenance=_provenance(),
    )
    assert capability.rate_is_authoritative
    states = {f.feature: f.state for f in capability.features}
    assert states['lip'] == 'supported'
    assert states['qft'] == 'unknown'


def test_marketing_evidence_cannot_claim_supported():
    with pytest.raises(ValueError, match='marketing'):
        HdmiFeatureEvidence(
            feature='u96_link', state='supported',
            evidence_class='marketing',
        )
    with pytest.raises(ValueError, match='certification'):
        build_hdmi_transport_capability(
            capability_id='hdmi-bad',
            version='1',
            cable_class='ultra96',
        )
    informational = build_hdmi_transport_capability(
        capability_id='hdmi-info',
        version='1',
        max_link_rate_gbps=48.0,
        max_rate_evidence_class='marketing',
    )
    assert not informational.rate_is_authoritative


def test_lip_is_reported_evidence_not_measured_sync():
    lip = build_hdmi_lip_evidence(
        evidence_id='lip-1',
        version='1',
        signal_path_id='path-1',
        signal_path_version='1',
        signal_path_sha256='a' * 64,
        negotiated_mode_label='4k120-hdr10',
        negotiated_mode_evidence_id='neg-1',
        lip_state='active',
        lip_protocol_version='lip-1.0',
        readback_method='vendor_api',
        components=(
            LipLatencyComponent(
                device_id='avr-1',
                component_kind='audio_processing',
                reported_latency_seconds=0.012,
            ),
            LipLatencyComponent(
                device_id='pj-1',
                component_kind='video_pipeline',
                reported_latency_seconds=0.016,
            ),
        ),
        provenance=_provenance(),
    )
    # reported components + negotiated mode are independent quantities
    assert lip.components[1].reported_latency_seconds == 0.016
    assert lip.negotiated_mode_evidence_id == 'neg-1'
    assert lip.lip_state == 'active'

    with pytest.raises(ValueError, match='readback'):
        build_hdmi_lip_evidence(
            evidence_id='lip-bad',
            version='1',
            lip_state='active',
        )


def test_hdmi_capability_hash_integrity():
    capability = build_hdmi_transport_capability(
        capability_id='hdmi-hash', version='1',
    )
    payload = capability.model_dump(mode='python')
    payload['port_label'] = 'hdmi-9'
    with pytest.raises(ValidationError, match='hash mismatch'):
        HDMITransportCapability(**payload)


# ---- #1043 room reflection -----------------------------------------------


def test_visual_color_never_reflectance_truth():
    with pytest.raises(ValueError, match='evidence tier'):
        build_room_optical_surface_profile(
            profile_id='wall-paint',
            version='1',
            diffuse_reflectance_fraction=0.6,
            evidence_tier='unknown',
        )


def test_room_return_estimator_fails_closed():
    guess = build_room_optical_surface_profile(
        profile_id='wall-guess',
        version='1',
        diffuse_reflectance_fraction=0.7,
        evidence_tier='user_declared',
    )
    # user-declared reflectance cannot authorize a prediction
    assert estimate_diffuse_room_return(
        image_mean_luminance_cd_m2=60.0,
        aperture_area_m2=4.0,
        surfaces=((guess, 0.5),),
    ) is None

    measured = build_room_optical_surface_profile(
        profile_id='wall-measured',
        version='1',
        diffuse_reflectance_fraction=0.5,
        evidence_tier='measured',
        measurement_method='spectrophotometer 8/d',
        provenance=_provenance(),
    )
    lift = estimate_diffuse_room_return(
        image_mean_luminance_cd_m2=60.0,
        aperture_area_m2=4.0,
        surfaces=((measured, 0.6), (guess, 0.4)),
    )
    # rho_eff over usable surfaces only: 0.5 * 60 / pi
    assert lift == pytest.approx(0.5 * 60.0 / math.pi)


def test_higher_room_reflectance_worsens_modeled_contrast():
    low = build_room_optical_surface_profile(
        profile_id='dark-room',
        version='1',
        diffuse_reflectance_fraction=0.2,
        evidence_tier='measured',
    )
    high = build_room_optical_surface_profile(
        profile_id='light-room',
        version='1',
        diffuse_reflectance_fraction=0.8,
        evidence_tier='measured',
    )
    dark_lift = estimate_diffuse_room_return(
        image_mean_luminance_cd_m2=60.0,
        aperture_area_m2=4.0,
        surfaces=((low, 1.0),),
    )
    light_lift = estimate_diffuse_room_return(
        image_mean_luminance_cd_m2=60.0,
        aperture_area_m2=4.0,
        surfaces=((high, 1.0),),
    )
    assert light_lift > dark_lift


def test_typed_stimulus_identity():
    with pytest.raises(ValueError, match='apl_fraction'):
        RoomContrastStimulus(
            stimulus_id='s1', stimulus_kind='fixed_apl'
        )
    with pytest.raises(ValueError, match='pattern_descriptor'):
        RoomContrastStimulus(
            stimulus_id='s2', stimulus_kind='custom'
        )


def test_in_situ_contrast_and_decomposition_honesty():
    measurement = build_in_situ_contrast_measurement(
        measurement_id='insitu-1',
        version='1',
        stimulus_id='ansi-50',
        stimulus_kind='ansi_checkerboard',
        white_luminance_cd_m2=120.0,
        black_luminance_cd_m2=0.4,
        contrast_ratio=300.0,
        instrument='klein-k10',
        provenance=_provenance(),
    )
    assert measurement.contrast_ratio == 300.0

    # unidentifiable decomposition cannot carry component values
    with pytest.raises(ValueError, match='never fabricated'):
        build_contrast_decomposition(
            decomposition_id='dec-bad',
            version='1',
            stimulus_id='ansi-50',
            stimulus_kind='ansi_checkerboard',
            contrast_ratio=300.0,
            room_return_lift_cd_m2=0.1,
            components_identifiable=False,
        )
    decomposition = build_contrast_decomposition(
        decomposition_id='dec-ok',
        version='1',
        stimulus_id='ansi-50',
        stimulus_kind='ansi_checkerboard',
        contrast_ratio=300.0,
        components_identifiable=False,
        limitations=('single-point measurement',),
    )
    assert decomposition.room_return_lift_cd_m2 is None

    payload = measurement.model_dump(mode='python')
    payload['contrast_ratio'] = 999.0
    with pytest.raises(ValidationError, match='hash mismatch'):
        InSituContrastMeasurement(**payload)
