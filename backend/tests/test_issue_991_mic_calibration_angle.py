"""Issue #991: mic calibration-angle applicability — 0°/90° profile
semantics vs actual acquisition orientation; fail closed to unknown
orientation / profile semantics, never force a correction."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_mic_response_calibration import (
    CalibrationApplicabilityVerdict,
    MicOrientationEvidence,
    MicrophoneResponseCalibrationProfile,
    build_response_calibration_profile,
    evaluate_calibration_angle_applicability,
)
from htdt.cad_scene import Direction3, Position3

_H = '2' * 64


def _profile(**overrides):
    kwargs = dict(
        profile_id='cal-1',
        schema_version='miccal_v1',
        instrument_model='miniDSP UMIK-1',
        incidence_kind='on_axis_0deg',
        reference_axis_semantics='capsule_axis_toward_source',
        angle_tolerance_deg=10.0,
        valid_frequency_hz=(10.0, 20000.0),
        correction_asset_sha256=_H,
    )
    kwargs.update(overrides)
    return build_response_calibration_profile(**kwargs)


def _evidence(**overrides):
    kwargs = dict(
        observed_direction=Direction3(x=0.0, y=0.0, z=-1.0),
        microphone_position=Position3(x_m=1.0, y_m=1.0, z_m=1.2),
        target_source_position=Position3(x_m=1.0, y_m=1.0, z_m=0.0),
        observed_pose_provenance='captured_annotated',
    )
    kwargs.update(overrides)
    return MicOrientationEvidence(**kwargs)


def _evaluate(profile=None, evidence=None, **overrides):
    kwargs = dict(
        profile=profile or _profile(),
        evidence=evidence if evidence is not None else _evidence(),
        verdict_id='v-1',
        measurement_id='meas-1',
    )
    kwargs.update(overrides)
    return evaluate_calibration_angle_applicability(**kwargs)


def test_profile_sealed():
    profile = _profile()
    assert len(profile.profile_sha256) == 64
    MicrophoneResponseCalibrationProfile.model_validate(
        profile.model_dump(mode='json')
    )
    payload = profile.model_dump(mode='json')
    payload['incidence_kind'] = 'perpendicular_90deg'
    with pytest.raises(ValidationError, match='hash mismatch'):
        MicrophoneResponseCalibrationProfile.model_validate(payload)


def test_directional_profile_requires_axis_semantics():
    with pytest.raises(ValidationError, match='reference_axis_semantics'):
        _profile(reference_axis_semantics=None)


def test_no_orientation_evidence_is_unknown_not_assumed():
    verdict = _evaluate(evidence=MicOrientationEvidence())
    assert verdict.state == 'unknown_orientation'
    assert verdict.angular_mismatch_deg is None


def test_unknown_profile_semantics_fails_closed():
    profile = _profile(
        incidence_kind='unknown', reference_axis_semantics=None
    )
    verdict = _evaluate(profile=profile)
    assert verdict.state == 'unknown_profile_semantics'


def test_zero_deg_profile_pointed_at_source_verified():
    # capsule axis aimed straight down at the source → on-axis, in tolerance
    verdict = _evaluate()
    assert verdict.state == 'verified_compatible'


def test_zero_deg_profile_rotated_90_incompatible():
    # capsule axis horizontal → 90° off the source direction
    verdict = _evaluate(
        evidence=_evidence(observed_direction=Direction3(x=1.0, y=0.0, z=0.0))
    )
    assert verdict.state == 'incompatible'
    assert verdict.angular_mismatch_deg == pytest.approx(90.0, abs=1e-6)


def test_planned_only_evidence_assumes_not_verifies():
    verdict = _evaluate(
        evidence=MicOrientationEvidence(
            planned_direction=Direction3(x=0.0, y=0.0, z=-1.0),
            microphone_position=Position3(x_m=1.0, y_m=1.0, z_m=1.2),
            target_source_position=Position3(x_m=1.0, y_m=1.0, z_m=0.0),
        )
    )
    assert verdict.state == 'assumed_compatible'
    assert verdict.orientation_evidence == 'planned'


def test_verified_requires_observed_evidence():
    with pytest.raises(ValidationError, match='observed'):
        CalibrationApplicabilityVerdict(
            verdict_id='v',
            profile_id='cal-1',
            profile_sha256=_H,
            state='verified_compatible',
            orientation_evidence='planned',
        )


def test_incompatible_requires_reported_mismatch():
    with pytest.raises(ValidationError, match='angular mismatch'):
        CalibrationApplicabilityVerdict(
            verdict_id='v',
            profile_id='cal-1',
            profile_sha256=_H,
            state='incompatible',
            angular_mismatch_deg=None,
        )


def test_no_tolerance_reports_mismatch_and_assumes():
    profile = _profile(angle_tolerance_deg=None)
    verdict = _evaluate(
        profile=profile,
        evidence=_evidence(observed_direction=Direction3(x=0.0, y=-1.0, z=0.0)),
    )
    assert verdict.state == 'assumed_compatible'
    assert verdict.angular_mismatch_deg == pytest.approx(90.0, abs=1e-6)


def test_source_position_requires_mic_position():
    with pytest.raises(ValidationError, match='microphone_position'):
        MicOrientationEvidence(
            target_source_position=Position3(x_m=0.0, y_m=0.0, z_m=0.0)
        )
