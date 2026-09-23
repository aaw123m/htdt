"""Photometric/HDR commissioning tests (#557)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_photometric import (
    AmbientLightObservation,
    AngularGainSample,
    LightOutputReading,
    LuminanceMeasurement,
    ProjectorImagePerformanceProfile,
    ToneMappingState,
    build_projector_image_performance_profile,
    build_screen_optical_profile,
    estimate_direct_view_luminance,
    estimate_projection_luminance,
    evaluate_photometric_state,
)


def _provenance():
    return (
        EquipmentDataProvenance(
            evidence_kind='manufacturer',
            source_name='Example Projection Co.',
            source_version='2026.1',
            source_reference='Model P datasheet',
            source_sha256='d' * 64,
        ),
    )


def _projector_profile(**overrides):
    kwargs = dict(
        profile_id='pj-eco',
        version='1',
        operating_mode='eco',
        projector_specification_id='example-projector-p',
        projector_specification_version='2026.1',
        projector_specification_sha256='e' * 64,
        light_output=(
            LightOutputReading(
                evidence_class='rated', lumens=2200.0
            ),
            LightOutputReading(
                evidence_class='measured',
                lumens=1800.0,
                measured_at_utc='2026-09-20T00:00:00+00:00',
                instrument='lumagen-probe',
            ),
        ),
        lens_transmission_fraction=0.92,
        lens_transmission_evidence_class='measured',
        on_off_contrast_ratio=40000.0,
        on_off_contrast_evidence_class='rated',
        hdr_format_families=('hdr10',),
        tone_mapping=ToneMappingState(
            mode='frame-adapt', dynamic=True, target_peak_cd_m2=120.0
        ),
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return build_projector_image_performance_profile(**kwargs)


def _screen_profile():
    return build_screen_optical_profile(
        profile_id='screen-mat',
        version='1',
        screen_material='matte white',
        nominal_gain=1.0,
        angular_gain=(AngularGainSample(angle_deg=15.0, gain=0.93),),
        acoustically_transparent=True,
        provenance=_provenance(),
    )


def test_profile_requires_exact_spec_triple():
    with pytest.raises(ValueError, match='supplied together'):
        _projector_profile(
            projector_specification_version=None,
            projector_specification_sha256=None,
        )


def test_lens_loss_requires_evidence_class():
    with pytest.raises(ValueError, match='evidence class'):
        _projector_profile(lens_transmission_evidence_class=None)


def test_profile_hash_integrity():
    payload = _projector_profile().model_dump(mode='python')
    payload['operating_mode'] = 'high'
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        ProjectorImagePerformanceProfile(**payload)


def test_estimate_unknown_without_lens_profile():
    estimate = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(
            lens_transmission_fraction=None,
            lens_transmission_evidence_class=None,
        ),
        screen_profile=_screen_profile(),
    )
    assert estimate.status == 'UNKNOWN'
    assert estimate.predicted_peak_white_cd_m2 is None
    assert 'lens/throw' in estimate.status_reason
    assert estimate.estimate_id.startswith('ele-')


def test_estimate_pass_with_exact_inputs():
    estimate = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(),
        screen_profile=_screen_profile(),
        reference_evidence_class='measured',
    )
    assert estimate.status == 'PASS'
    # 1800 lm * 0.92 / (2.67*1.12) / pi * gain 1.0
    expected = 1800.0 * 0.92 / (2.67 * 1.12) / 3.141592653589793
    assert estimate.predicted_peak_white_cd_m2 == pytest.approx(expected)
    assert estimate.projector_image_profile_sha256 == (
        _projector_profile().profile_sha256
    )
    assert estimate.predicted_on_off_contrast == 40000.0


def test_ambient_lux_folds_into_black_floor_not_white():
    ambient = AmbientLightObservation(
        observation_id='amb-1',
        location_label='screen wall',
        declared_lux=5.0,
        measured_lux=12.0,
        measured_at_utc='2026-09-20T00:00:00+00:00',
        instrument='lux-meter-1',
    )
    without = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(),
        screen_profile=_screen_profile(),
    )
    with_ambient = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(),
        screen_profile=_screen_profile(),
        ambient_observation=ambient,
    )
    assert with_ambient.predicted_black_floor_cd_m2 > (
        without.predicted_black_floor_cd_m2
    )
    assert with_ambient.predicted_peak_white_cd_m2 == (
        without.predicted_peak_white_cd_m2
    )


def test_direct_view_estimate_from_display_spec():
    estimate = estimate_direct_view_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        peak_luminance_cd_m2=800.0,
        black_level_cd_m2=0.0005,
        display_specification_sha256='a' * 64,
    )
    assert estimate.status == 'PASS'
    assert estimate.surface_kind == 'direct_view'
    assert estimate.predicted_on_off_contrast == pytest.approx(800.0 / 0.0005)


def test_evaluation_compares_per_criterion():
    estimate = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(),
        screen_profile=_screen_profile(),
    )
    measurement = LuminanceMeasurement(
        measurement_id='m-1',
        measured_at_utc='2026-09-21T00:00:00+00:00',
        instrument='klein-k10',
        method='contact at screen center',
        surface_kind='projection',
        surface_entity_id='screen-main',
        peak_white_cd_m2=estimate.predicted_peak_white_cd_m2 * 1.05,
        on_off_contrast_ratio=38000.0,
        provenance=_provenance(),
    )
    evaluation = evaluate_photometric_state(
        estimate=estimate, measurement=measurement
    )
    statuses = {c.criterion: c.status for c in evaluation.criteria}
    assert statuses['peak_white_luminance'] == 'PASS'
    assert statuses['black_floor'] == 'UNKNOWN'  # never measured
    assert statuses['on_off_contrast'] == 'PASS'
    assert evaluation.evaluation_id.startswith('phe-')


def test_evaluation_measurement_only():
    measurement = LuminanceMeasurement(
        measurement_id='m-2',
        measured_at_utc='2026-09-21T00:00:00+00:00',
        surface_kind='direct_view',
        surface_entity_id='display-main',
        peak_white_cd_m2=750.0,
    )
    evaluation = evaluate_photometric_state(
        estimate=None, measurement=measurement
    )
    assert all(c.status == 'UNKNOWN' for c in evaluation.criteria)
