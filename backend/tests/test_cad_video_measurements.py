"""Video measurement-authority tests (#1005, #1010, #1012, #1017, #1030)."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_laser_speckle import (
    LaserProjectionSpeckleMeasurement,
    build_laser_speckle_condition,
    build_laser_speckle_measurement,
)
from htdt.cad_screen_moire import (
    MoireObservation,
    build_moire_condition,
    build_moire_observation,
    build_screen_microstructure,
    derive_projected_pixel_pitch_mm,
)
from htdt.cad_spatial_fidelity import (
    CameraChainProfile,
    SpatialQualityResult,
    build_projection_optical_condition,
    build_spatial_image_quality_measurement,
)
from htdt.cad_temporal_emission import (
    TemporalMetricResult,
    build_display_temporal_condition,
    build_temporal_emission_measurement,
    build_temporal_light_waveform,
    derive_dominant_frequency_hz,
    derive_modulation_depth,
)
from htdt.cad_video_latency import (
    LatencyObservation,
    VideoLatencyMeasurement,
    build_video_latency_condition,
    build_video_latency_measurement,
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


# ---- #1005 video latency ------------------------------------------------


def _latency_condition(**overrides):
    kwargs = dict(
        condition_id='lat-game-4k120',
        version='1',
        signal_path_id='path-1',
        signal_path_version='1',
        signal_path_sha256='a' * 64,
        resolution_label='3840x2160',
        refresh_rate_hz=120.0,
        refresh_kind='fixed',
        bit_depth=10,
        hdr_format_family='hdr10',
        picture_mode='game',
        allm_state='negotiated',
        provenance=None,
    )
    kwargs.pop('provenance')
    kwargs.update(overrides)
    return build_video_latency_condition(**kwargs)


def test_latency_condition_vrr_requires_range():
    with pytest.raises(ValueError, match='vrr_min_hz'):
        _latency_condition(refresh_kind='vrr')


def test_latency_measurement_derives_frames_and_stats():
    condition = _latency_condition()
    measurement = build_video_latency_measurement(
        measurement_id='lat-m1',
        version='1',
        condition=condition,
        metric_kind='input_to_photon',
        method='dedicated_lag_tester',
        trigger_definition='flash rectangle',
        response_detection='photodiode at center',
        scan_position='center',
        instrument='lmg-e',
        refresh_rate_hz_effective=120.0,
        observations=(
            LatencyObservation(
                observed_at_utc='2026-09-24T00:00:00+00:00',
                latency_seconds=0.016,
            ),
            LatencyObservation(
                observed_at_utc='2026-09-24T00:00:01+00:00',
                latency_seconds=0.018,
            ),
        ),
        provenance=_provenance(),
    )
    assert measurement.mean_latency_seconds == pytest.approx(0.017)
    assert measurement.min_latency_seconds == pytest.approx(0.016)
    # frames only derived against a declared stable/effective rate
    assert measurement.latency_frames == pytest.approx(0.017 * 120.0)
    assert measurement.measurement_id.startswith('')


def test_device_reported_latency_requires_source():
    with pytest.raises(ValueError, match='reported_source'):
        build_video_latency_measurement(
            measurement_id='lat-lip',
            version='1',
            condition=_latency_condition(),
            metric_kind='device_reported',
            method='device_reported',
        )


def test_latency_measurement_hash_integrity():
    measurement = build_video_latency_measurement(
        measurement_id='lat-m2',
        version='1',
        condition=_latency_condition(),
        metric_kind='input_to_photon',
        method='high_speed_camera',
        scan_position='top',
    )
    payload = measurement.model_dump(mode='python')
    payload['scan_position'] = 'bottom'
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        VideoLatencyMeasurement(**payload)


# ---- #1010 spatial fidelity ---------------------------------------------


def _optical_condition(**overrides):
    kwargs = dict(
        condition_id='opt-center',
        version='1',
        projector_instance_id='pj-1',
        throw_distance_m=3.4,
        zoom_state=1.0,
        keystone_correction='none',
        native_resolution_w=3840,
        native_resolution_h=2160,
        pixel_shift_mode='off',
        image_width_m=2.67,
        image_height_m=1.5,
        warmup_minutes=30.0,
    )
    kwargs.update(overrides)
    return build_projection_optical_condition(**kwargs)


def test_mtf_result_requires_camera_method_and_algorithm():
    with pytest.raises(ValueError, match='algorithm_version'):
        build_spatial_image_quality_measurement(
            measurement_id='siq-1',
            version='1',
            condition=_optical_condition(),
            method='camera_image',
            camera_profile=CameraChainProfile(camera_model='a7iii'),
            results=(
                SpatialQualityResult(
                    grid_point='center',
                    metric_kind='mtf50',
                    value=3.2,
                    unit='cyc/px',
                ),
            ),
        )
    with pytest.raises(ValueError, match='not MTF'):
        build_spatial_image_quality_measurement(
            measurement_id='siq-2',
            version='1',
            condition=_optical_condition(),
            method='visual_inspection',
            results=(
                SpatialQualityResult(
                    grid_point='center',
                    metric_kind='mtf50',
                    value=3.2,
                    unit='cyc/px',
                    algorithm_version='slanted-edge-1',
                ),
            ),
        )


def test_spatial_measurement_accepts_camera_mtf_with_chain():
    measurement = build_spatial_image_quality_measurement(
        measurement_id='siq-3',
        version='1',
        condition=_optical_condition(),
        method='slanted_edge_camera',
        camera_profile=CameraChainProfile(
            camera_model='a7iii', sensor_mtf_note='sensor Nyquist limited'
        ),
        grid=('center', 'corner_tl'),
        results=(
            SpatialQualityResult(
                grid_point='center',
                metric_kind='mtf50',
                value=3.2,
                unit='cyc/px',
                algorithm_version='slanted-edge-1',
                instrument_limited=True,
            ),
        ),
        provenance=_provenance(),
    )
    assert measurement.results[0].instrument_limited


# ---- #1012 laser speckle ------------------------------------------------


def _speckle_condition(**overrides):
    kwargs = dict(
        condition_id='spk-ust-rgb',
        version='1',
        projector_instance_id='pj-laser-1',
        light_engine_mode='rgb_laser',
        speckle_reduction_state='on',
        screen_optical_profile_id='screen-ust',
        screen_optical_profile_version='1',
        screen_optical_profile_sha256='b' * 64,
        throw_class='ust',
        throw_distance_m=0.4,
        image_width_m=2.2,
        image_height_m=1.24,
        viewing_distance_m=2.5,
        viewing_angle_deg=0.0,
        test_wavelength='white',
    )
    kwargs.update(overrides)
    return build_laser_speckle_condition(**kwargs)


def test_speckle_monochromatic_vs_visual_kinds_separate():
    condition = _speckle_condition()
    mono = build_laser_speckle_measurement(
        measurement_id='spk-m1',
        version='1',
        condition=condition,
        metric_kind='monochromatic_contrast',
        method='iec_62906_5_6',
        method_version='iec62906-5-6',
        value_fraction=0.08,
        test_wavelength=None,
        instrument='speckle-meter',
        provenance=_provenance(),
    )
    assert mono.metric_kind == 'monochromatic_contrast'

    visual = build_laser_speckle_measurement(
        measurement_id='spk-v1',
        version='1',
        condition=condition,
        metric_kind='visual_observation',
        method='visual',
        visual_observation='visible',
    )
    assert visual.visual_observation == 'visible'

    # ordinal evidence cannot masquerade as an instrumented metric
    with pytest.raises(ValueError, match='visual_observation'):
        build_laser_speckle_measurement(
            measurement_id='spk-bad',
            version='1',
            condition=condition,
            metric_kind='monochromatic_contrast',
            method='iec_62906_5_6',
            value_fraction=0.08,
            visual_observation='visible',
        )
    # instrumented kinds need a measured value
    with pytest.raises(ValueError, match='value_fraction'):
        build_laser_speckle_measurement(
            measurement_id='spk-bad2',
            version='1',
            condition=condition,
            metric_kind='colour_speckle',
            method='cie_colour_speckle',
        )


def test_speckle_camera_method_requires_chain():
    with pytest.raises(ValueError, match='camera_profile'):
        build_laser_speckle_measurement(
            measurement_id='spk-cam',
            version='1',
            condition=_speckle_condition(),
            metric_kind='colour_speckle',
            method='camera_derived',
            value_fraction=0.06,
        )


def test_speckle_measurement_hash_integrity():
    m = build_laser_speckle_measurement(
        measurement_id='spk-hash',
        version='1',
        condition=_speckle_condition(),
        metric_kind='monochromatic_contrast',
        method='iec_62906_5_6',
        value_fraction=0.08,
    )
    payload = m.model_dump(mode='python')
    payload['value_fraction'] = 0.09
    with pytest.raises(ValidationError, match='hash mismatch'):
        LaserProjectionSpeckleMeasurement(**payload)


# ---- #1017 moiré ---------------------------------------------------------


def _moire_condition(**overrides):
    kwargs = dict(
        condition_id='moire-1',
        version='1',
        projector_instance_id='pj-1',
        native_resolution_w=3840,
        native_resolution_h=2160,
        pixel_shift_mode='off',
        raster_semantics_exact=True,
        screen_optical_profile_id='screen-at',
        screen_optical_profile_version='1',
        screen_optical_profile_sha256='c' * 64,
        screen_rotation_deg=0.0,
        image_width_m=2.67,
        image_height_m=1.5,
        viewing_distance_m=3.0,
        test_pattern='pixel grid',
    )
    kwargs.update(overrides)
    return build_moire_condition(**kwargs)


def test_microstructure_unknown_stays_unknown():
    unknown = build_screen_microstructure(
        microstructure_id='ms-unknown',
        version='1',
        structure_kind='perforated',
    )
    assert unknown.perforation_pitch_mm is None
    # structural figures require a declared evidence kind
    with pytest.raises(ValueError, match='evidence_kind'):
        build_screen_microstructure(
            microstructure_id='ms-bad',
            version='1',
            structure_kind='perforated',
            perforation_pitch_mm=0.5,
            evidence_kind='unknown',
        )


def test_projected_pixel_pitch_requires_exact_raster():
    condition = _moire_condition()
    pitch = derive_projected_pixel_pitch_mm(condition)
    assert pitch == pytest.approx(2.67 / 3840 * 1000.0)
    # pixel-shift or unknown raster semantics defeat the derivation
    shifted = _moire_condition(pixel_shift_mode='4x')
    assert derive_projected_pixel_pitch_mm(shifted) is None
    inexact = _moire_condition(raster_semantics_exact=False)
    assert derive_projected_pixel_pitch_mm(inexact) is None


def test_moire_camera_observation_requires_profile():
    condition = _moire_condition()
    with pytest.raises(ValueError, match='camera_profile'):
        build_moire_observation(
            observation_id='mo-1',
            version='1',
            condition=condition,
            method='camera_analysis',
            observed_artifact='beat_pattern',
        )
    visual = build_moire_observation(
        observation_id='mo-2',
        version='1',
        condition=condition,
        method='visual',
        observed_artifact='none',
        severity='not observed',
        provenance=_provenance(),
    )
    assert visual.observation_id == 'mo-2'
    payload = visual.model_dump(mode='python')
    payload['observed_artifact'] = 'beat_pattern'
    with pytest.raises(ValidationError, match='hash mismatch'):
        MoireObservation(**payload)


# ---- #1030 temporal emission --------------------------------------------


def _temporal_condition(**overrides):
    kwargs = dict(
        condition_id='temp-vrr',
        version='1',
        display_instance_id='disp-1',
        display_specification_id='oled-77',
        display_specification_version='2026.1',
        display_specification_sha256='e' * 64,
        firmware='fw-3.4',
        input_resolution_w=3840,
        input_resolution_h=2160,
        refresh_hz=120.0,
        refresh_kind='vrr',
        vrr_min_hz=40.0,
        vrr_max_hz=120.0,
        hdr_format_family='hdr10',
        picture_mode='game',
        bfi_state='off',
        local_dimming_state='n/a',
        motion_interpolation_mode='off',
        allm_state='negotiated',
        warmup_minutes=20.0,
    )
    kwargs.update(overrides)
    return build_display_temporal_condition(**kwargs)


def test_temporal_condition_vrr_requires_range():
    with pytest.raises(ValueError, match='vrr_min_hz'):
        _temporal_condition(vrr_min_hz=None, vrr_max_hz=None)


def test_waveform_requires_replayable_evidence():
    condition = _temporal_condition()
    with pytest.raises(ValueError, match='samples or an asset_sha256'):
        build_temporal_light_waveform(
            waveform_id='wf-empty',
            version='1',
            condition=condition,
        )
    with pytest.raises(ValueError, match='sample_rate_hz'):
        build_temporal_light_waveform(
            waveform_id='wf-norate',
            version='1',
            condition=condition,
            samples=(1.0, 2.0, 1.0),
        )


def test_temporal_derived_metrics_bound_to_waveform():
    condition = _temporal_condition()
    # 20 Hz sine + offset at 200 Hz sampling → clear dominant frequency
    rate = 200.0
    samples = tuple(
        100.0 + 20.0 * math.sin(2 * math.pi * 20.0 * i / rate)
        for i in range(256)
    )
    waveform = build_temporal_light_waveform(
        waveform_id='wf-sine',
        version='1',
        condition=condition,
        detector='photodiode-x',
        detector_bandwidth_hz=1000.0,
        sample_rate_hz=rate,
        samples=samples,
        stimulus='10% window',
        channel='luminance',
        provenance=_provenance(),
    )
    depth = derive_modulation_depth(waveform)
    assert depth is not None
    # (max-min)/(max+min) ≈ (120-80)/(120+80) = 0.2 (sampled peaks)
    assert depth.value == pytest.approx(0.2, abs=0.02)
    freq = derive_dominant_frequency_hz(waveform)
    assert freq is not None
    assert freq.value == pytest.approx(20.0, abs=1.0)

    measurement = build_temporal_emission_measurement(
        measurement_id='tem-1',
        version='1',
        condition=condition,
        instrument='photodiode-x',
        waveforms=(waveform,),
        metrics=(depth, freq),
        provenance=_provenance(),
    )
    assert len(measurement.metrics) == 2

    # a metric referencing an external waveform is rejected
    orphan = TemporalMetricResult(
        metric_kind='modulation_depth',
        metric_version='modulation-depth-v1',
        waveform_id='wf-other',
        value=0.1,
        unit='fraction',
    )
    with pytest.raises(ValidationError, match='not in this measurement'):
        build_temporal_emission_measurement(
            measurement_id='tem-2',
            version='1',
            condition=condition,
            waveforms=(waveform,),
            metrics=(orphan,),
        )
