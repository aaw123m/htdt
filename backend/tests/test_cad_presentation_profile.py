"""Video presentation profile tests (#565)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_presentation_profile import (
    MaskingState,
    ProjectorOpticalPresetBinding,
    VideoPresentationProfile,
    build_presentation_mode_confirmation,
    build_video_presentation_profile,
    evaluate_presentation_profile,
    resolve_presentation_aperture,
)


SCREEN_W = 2.67  # 120in 2.39:1 scope screen, meters
SCREEN_H = 1.12


def _provenance():
    return (
        EquipmentDataProvenance(
            evidence_kind='user_defined',
            source_name='installer',
            source_version='1',
            source_reference='commissioning notes',
            source_sha256='c' * 64,
        ),
    )


def _cih_169() -> VideoPresentationProfile:
    return build_video_presentation_profile(
        profile_id='mode-169-cih',
        version='1',
        label='16:9 inside scope screen',
        sizing='constant_image_height',
        target_aspect_ratio=16.0 / 9.0,
        masking=MaskingState(kind='side'),
        optical_preset=ProjectorOpticalPresetBinding(
            preset_ref='lens-memory-2',
            zoom_ratio=1.0,
            anamorphic_enabled=False,
        ),
        provenance=_provenance(),
    )


def _scope_239() -> VideoPresentationProfile:
    return build_video_presentation_profile(
        profile_id='mode-239',
        version='1',
        label='scope, full screen',
        sizing='explicit',
        active_width_m=SCREEN_W,
        active_height_m=SCREEN_H,
        masking=MaskingState(kind='none'),
        optical_preset=ProjectorOpticalPresetBinding(
            preset_ref='lens-memory-1',
            zoom_ratio=1.33,
            anamorphic_enabled=True,
        ),
        provenance=_provenance(),
    )


def test_cih_aperture_resolution():
    aperture = resolve_presentation_aperture(
        _cih_169(),
        screen_visible_width_m=SCREEN_W,
        screen_visible_height_m=SCREEN_H,
    )
    assert aperture.status == 'PASS'
    assert aperture.height_m == pytest.approx(SCREEN_H)
    assert aperture.width_m == pytest.approx(SCREEN_H * 16.0 / 9.0)
    assert aperture.width_m < SCREEN_W


def test_ciw_aperture_resolution():
    profile = build_video_presentation_profile(
        profile_id='mode-ciw',
        version='1',
        sizing='constant_image_width',
        target_aspect_ratio=2.39,
        masking=MaskingState(kind='top_bottom'),
        provenance=_provenance(),
    )
    aperture = resolve_presentation_aperture(
        profile,
        screen_visible_width_m=SCREEN_W,
        screen_visible_height_m=SCREEN_H * 3,
    )
    assert aperture.status == 'PASS'
    assert aperture.width_m == pytest.approx(SCREEN_W)
    assert aperture.height_m == pytest.approx(SCREEN_W / 2.39)


def test_unknown_sizing_yields_unknown_aperture():
    profile = build_video_presentation_profile(
        profile_id='mode-unknown',
        version='1',
        sizing='unknown',
        masking=MaskingState(kind='unknown'),
        provenance=_provenance(),
    )
    aperture = resolve_presentation_aperture(
        profile,
        screen_visible_width_m=SCREEN_W,
        screen_visible_height_m=SCREEN_H,
    )
    assert aperture.status == 'UNKNOWN'
    assert aperture.width_m is None


def test_aperture_beyond_screen_fails():
    profile = build_video_presentation_profile(
        profile_id='too-wide',
        version='1',
        sizing='explicit',
        active_width_m=SCREEN_W + 0.5,
        active_height_m=SCREEN_H,
        provenance=_provenance(),
    )
    aperture = resolve_presentation_aperture(
        profile,
        screen_visible_width_m=SCREEN_W,
        screen_visible_height_m=SCREEN_H,
    )
    assert aperture.status == 'FAIL'


def test_masking_consistency_flags_mismatch():
    # side masking declared but a 2.39 image fills the whole scope screen —
    # there are no side borders to mask.
    profile = build_video_presentation_profile(
        profile_id='bad-masking',
        version='1',
        sizing='explicit',
        active_width_m=SCREEN_W,
        active_height_m=SCREEN_H,
        masking=MaskingState(kind='side'),
        provenance=_provenance(),
    )
    evaluation = evaluate_presentation_profile(
        screen_entity_id='screen-main',
        screen_visible_width_m=SCREEN_W,
        screen_visible_height_m=SCREEN_H,
        profile=profile,
    )
    assert evaluation.masking_status == 'FAIL'
    assert evaluation.aperture_status == 'PASS'


def test_profile_hash_changes_with_optical_state():
    a = _scope_239()
    b = build_video_presentation_profile(
        profile_id='mode-239',
        version='1',
        label='scope, full screen',
        sizing='explicit',
        active_width_m=SCREEN_W,
        active_height_m=SCREEN_H,
        masking=MaskingState(kind='none'),
        optical_preset=ProjectorOpticalPresetBinding(
            preset_ref='lens-memory-4',  # different slot
            zoom_ratio=1.33,
            anamorphic_enabled=True,
        ),
        provenance=_provenance(),
    )
    assert a.profile_sha256 != b.profile_sha256


def test_hash_integrity():
    payload = _cih_169().model_dump(mode='python')
    payload['masking'] = {'kind': 'none'}
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        VideoPresentationProfile(**payload)


def test_confirmation_binds_exact_profile_hash():
    profile = _cih_169()
    confirmation = build_presentation_mode_confirmation(
        confirmation_id='conf-1',
        profile=profile,
        confirmed_at_utc='2026-09-23T00:00:00+00:00',
        method='visual_inspection',
        confirmed_by='installer',
    )
    evaluation = evaluate_presentation_profile(
        screen_entity_id='screen-main',
        screen_visible_width_m=SCREEN_W,
        screen_visible_height_m=SCREEN_H,
        profile=profile,
        confirmation=confirmation,
    )
    assert evaluation.confirmation_status == 'PASS'
    assert evaluation.masking_status == 'PASS'
    assert evaluation.profile_status == 'PASS'
    assert evaluation.evaluation_id.startswith('ppe-')

    other = _scope_239()
    mismatched = evaluate_presentation_profile(
        screen_entity_id='screen-main',
        screen_visible_width_m=SCREEN_W,
        screen_visible_height_m=SCREEN_H,
        profile=other,
        confirmation=confirmation,
    )
    assert mismatched.confirmation_status == 'FAIL'


def test_unconfirmed_profile_reports_unknown():
    evaluation = evaluate_presentation_profile(
        screen_entity_id='screen-main',
        screen_visible_width_m=SCREEN_W,
        screen_visible_height_m=SCREEN_H,
        profile=_cih_169(),
    )
    assert evaluation.confirmation_status == 'UNKNOWN'
    assert evaluation.profile_status == 'UNKNOWN'


def test_cih_ciw_require_aspect():
    with pytest.raises(ValueError, match='target_aspect_ratio'):
        build_video_presentation_profile(
            profile_id='no-aspect',
            version='1',
            sizing='constant_image_height',
            provenance=_provenance(),
        )
    with pytest.raises(ValueError, match='active_width_m'):
        build_video_presentation_profile(
            profile_id='no-dims',
            version='1',
            sizing='explicit',
            provenance=_provenance(),
        )
