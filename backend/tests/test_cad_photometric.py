"""Photometric/HDR commissioning tests (#557)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_photometric import (
    AmbientLightObservation,
    AmbientReflectanceProfile,
    AngularGainSample,
    LightOutputReading,
    LuminanceMeasurement,
    ProjectorImagePerformanceProfile,
    ToneMappingState,
    build_ambient_reflectance_profile,
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
        evidence_policy='require_measured',
    )
    assert estimate.status == 'PASS'
    # 1800 lm * 0.92 / (2.67*1.12) / pi * gain 1.0
    expected = 1800.0 * 0.92 / (2.67 * 1.12) / 3.141592653589793
    assert estimate.predicted_peak_white_cd_m2 == pytest.approx(expected)
    assert estimate.projector_image_profile_sha256 == (
        _projector_profile().profile_sha256
    )
    assert estimate.predicted_on_off_contrast == 40000.0
    assert estimate.light_output_evidence_class == 'measured'


def test_evidence_policy_fails_closed_on_downgrade():
    # #1016: requesting measured evidence when only rated/user-measured
    # exists must UNKNOWN the estimate, not silently downgrade.
    estimate = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(
            light_output=(
                LightOutputReading(evidence_class='rated', lumens=2200.0),
            ),
        ),
        screen_profile=_screen_profile(),
        evidence_policy='require_measured',
    )
    assert estimate.status == 'UNKNOWN'
    assert estimate.light_output_evidence_class is None
    assert 'require_measured' in estimate.status_reason

    estimated = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(
            light_output=(
                LightOutputReading(evidence_class='rated', lumens=2200.0),
                LightOutputReading(
                    evidence_class='user_measured',
                    lumens=1900.0,
                    measured_at_utc='2026-09-20T00:00:00+00:00',
                ),
            ),
        ),
        screen_profile=_screen_profile(),
        evidence_policy='allow_user_measured_or_better',
    )
    assert estimated.status == 'PASS'
    assert estimated.light_output_evidence_class == 'user_measured'

    diagnostic = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(
            light_output=(
                LightOutputReading(evidence_class='rated', lumens=2200.0),
            ),
        ),
        screen_profile=_screen_profile(),
        evidence_policy='best_available_diagnostic',
    )
    assert diagnostic.status == 'PASS'
    # the actual class is recorded machine-readably, not implied
    assert diagnostic.light_output_evidence_class == 'rated'
    assert 'rated' in diagnostic.status_reason


def test_ambient_lux_folds_into_black_floor_and_white():
    # #1015/#1050: ambient lifts the effective black floor AND the effective
    # white via an explicit reflectance model; native figures stay separate.
    ambient = AmbientLightObservation(
        observation_id='amb-1',
        location_label='screen wall',
        declared_lux=5.0,
        measured_lux=12.0,
        quantity_kind='screen_plane_illuminance',
        measured_at_utc='2026-09-20T00:00:00+00:00',
        instrument='lux-meter-1',
    )
    reflectance = build_ambient_reflectance_profile(
        profile_id='matte-refl',
        version='1',
        surface_kind='projection',
        screen_optical_profile_id='screen-mat',
        screen_optical_profile_version='1',
        screen_optical_profile_sha256=_screen_profile().profile_sha256,
        diffuse_reflectance_fraction=0.25,
        evidence_kind='manufacturer_reflectance',
        provenance=_provenance(),
    )
    without = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(),
        screen_profile=_screen_profile(),
        ambient_reflectance=reflectance,
    )
    with_ambient = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(),
        screen_profile=_screen_profile(),
        ambient_observation=ambient,
        ambient_reflectance=reflectance,
    )
    # effective-on-off-v1: rho * E / pi = 0.25 * 12 / pi lift on both fields
    lift = 0.25 * 12.0 / 3.141592653589793
    assert with_ambient.ambient_black_lift_cd_m2 == pytest.approx(lift)
    assert with_ambient.ambient_model == 'diffuse-reflectance-v1'
    assert with_ambient.predicted_black_floor_cd_m2 == pytest.approx(
        without.predicted_black_floor_cd_m2 + lift
    )
    assert with_ambient.predicted_peak_white_cd_m2 == pytest.approx(
        without.predicted_peak_white_cd_m2 + lift
    )
    # native numbers are untouched by ambient (#1015)
    assert with_ambient.predicted_native_peak_white_cd_m2 == (
        without.predicted_peak_white_cd_m2
    )
    assert with_ambient.predicted_native_on_off_contrast == 40000.0
    assert with_ambient.predicted_on_off_contrast < (
        with_ambient.predicted_native_on_off_contrast
    )


def test_ambient_observation_without_reflectance_is_unmodeled():
    # #1050: nominal gain is never ambient reflectance — lux without a
    # reflectance profile leaves the observation visibly unmodeled.
    ambient = AmbientLightObservation(
        observation_id='amb-2',
        location_label='screen wall',
        measured_lux=12.0,
        quantity_kind='screen_plane_illuminance',
        measured_at_utc='2026-09-20T00:00:00+00:00',
    )
    estimate = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(),
        screen_profile=_screen_profile(),
        ambient_observation=ambient,
    )
    assert estimate.ambient_black_lift_cd_m2 is None
    assert estimate.ambient_model is None
    assert any(
        'ambient contribution UNKNOWN' in n for n in estimate.input_notes
    )
    assert any(
        'is not an ambient-reflection' in n for n in estimate.input_notes
    )
    # effective fields still mirror native when no ambient model applied
    assert estimate.predicted_peak_white_cd_m2 == (
        estimate.predicted_native_peak_white_cd_m2
    )


def test_reflected_luminance_observation_lifts_black_directly():
    ambient = AmbientLightObservation(
        observation_id='amb-3',
        location_label='screen center',
        measured_luminance_cd_m2=0.5,
        quantity_kind='reflected_luminance',
        measured_at_utc='2026-09-20T00:00:00+00:00',
    )
    estimate = estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(),
        screen_profile=_screen_profile(),
        ambient_observation=ambient,
    )
    assert estimate.ambient_black_lift_cd_m2 == 0.5
    assert estimate.ambient_model == 'reflected-luminance-v1'


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


def _bound_projection_estimate(**kwargs):
    """Estimate bound to every compatibility axis the evaluation needs."""
    return estimate_projection_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        projector_profile=_projector_profile(),
        screen_profile=_screen_profile(),
        surface_entity_id='screen-main',
        **kwargs,
    )


def _bound_measurement(estimate, **overrides):
    kwargs = dict(
        measurement_id='m-1',
        measured_at_utc='2026-09-21T00:00:00+00:00',
        instrument='klein-k10',
        method='contact at screen center',
        surface_kind='projection',
        surface_entity_id='screen-main',
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        projector_image_profile_id='pj-eco',
        projector_image_profile_version='1',
        projector_image_profile_sha256=(
            estimate.projector_image_profile_sha256
        ),
        screen_optical_profile_id='screen-mat',
        screen_optical_profile_version='1',
        screen_optical_profile_sha256=_screen_profile().profile_sha256,
        operating_mode='eco',
        aperture_width_m=2.67,
        aperture_height_m=1.12,
        peak_white_cd_m2=estimate.predicted_peak_white_cd_m2 * 1.05,
        on_off_contrast_ratio=38000.0,
        provenance=_provenance(),
    )
    kwargs.update(overrides)
    return LuminanceMeasurement(**kwargs)


def test_evaluation_compares_per_criterion():
    estimate = _bound_projection_estimate()
    measurement = _bound_measurement(estimate)
    evaluation = evaluate_photometric_state(
        estimate=estimate, measurement=measurement
    )
    statuses = {c.criterion: c.status for c in evaluation.criteria}
    assert statuses['peak_white_luminance'] == 'PASS'
    assert statuses['black_floor'] == 'UNKNOWN'  # never measured
    assert statuses['on_off_contrast'] == 'PASS'
    assert evaluation.evaluation_id.startswith('phe-')
    assert evaluation.compatibility is not None
    assert evaluation.compatibility.same_surface == 'compatible'
    assert evaluation.compatibility.same_scene_state == 'compatible'
    assert evaluation.compatibility.compatible_method == 'compatible'


def test_evaluation_unknown_when_axes_unbound():
    # #1014: a bare measurement cannot be compared to the prediction —
    # unbound axes gate every criterion to UNKNOWN, never PASS.
    estimate = _bound_projection_estimate()
    measurement = LuminanceMeasurement(
        measurement_id='m-unbound',
        measured_at_utc='2026-09-21T00:00:00+00:00',
        surface_kind='projection',
        surface_entity_id='screen-main',
        peak_white_cd_m2=estimate.predicted_peak_white_cd_m2,
    )
    evaluation = evaluate_photometric_state(
        estimate=estimate, measurement=measurement
    )
    assert all(c.status == 'UNKNOWN' for c in evaluation.criteria)
    assert evaluation.compatibility.same_display_profile == 'unbound'
    assert evaluation.compatibility.same_scene_state == 'unbound'
    assert evaluation.compatibility.compatible_method == 'unbound'
    peak = next(
        c for c in evaluation.criteria
        if c.criterion == 'peak_white_luminance'
    )
    assert 'same_scene_state:unbound' in peak.blocking_axes


def test_evaluation_incompatible_scene_blocks_pass():
    estimate = _bound_projection_estimate()
    measurement = _bound_measurement(
        estimate, scene_revision_id='rev-2'
    )
    evaluation = evaluate_photometric_state(
        estimate=estimate, measurement=measurement
    )
    statuses = {c.criterion: c.status for c in evaluation.criteria}
    assert statuses['peak_white_luminance'] == 'UNKNOWN'
    assert evaluation.compatibility.same_scene_state == 'incompatible'


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
    assert evaluation.compatibility is None


def test_reflectance_profile_requires_evidence_kind():
    # #1050: a diffuse reflectance figure needs typed evidence — marketing
    # copy is not a reflectance source.
    with pytest.raises(ValueError, match='evidence kind'):
        build_ambient_reflectance_profile(
            profile_id='bad-refl',
            version='1',
            surface_kind='projection',
            screen_optical_profile_id='screen-mat',
            screen_optical_profile_version='1',
            screen_optical_profile_sha256=_screen_profile().profile_sha256,
            diffuse_reflectance_fraction=0.3,
            evidence_kind=None,
            provenance=_provenance(),
        )


def test_direct_view_ambient_unmodeled_without_reflectance():
    # #1050: direct-view ambient that cannot be modeled is recorded, not
    # silently dropped.
    ambient = AmbientLightObservation(
        observation_id='amb-dv',
        location_label='panel',
        measured_lux=300.0,
        quantity_kind='room_illuminance',
        measured_at_utc='2026-09-20T00:00:00+00:00',
    )
    estimate = estimate_direct_view_luminance(
        scene_revision_id='rev-1',
        scene_content_sha256='f' * 64,
        peak_luminance_cd_m2=800.0,
        black_level_cd_m2=0.0005,
        display_specification_sha256='a' * 64,
        ambient_observation=ambient,
    )
    assert estimate.status == 'PASS'
    assert estimate.ambient_black_lift_cd_m2 is None
    assert any(
        'ambient contribution UNKNOWN' in n for n in estimate.input_notes
    )
