from __future__ import annotations

from hashlib import sha256

import pytest

from htdt.cad_calibration import (
    CadTargetCurve,
    CadTargetCurvePoint,
    CadTargetNormalizationCondition,
)
from htdt.cad_playback_level import ReferenceProfileRef
from htdt.cad_playback_level_compensation import (
    LevelCompensationCurvePoint,
    LevelCompensationEntry,
    PlaybackLevelCompensationProfile,
    build_level_compensation_profile,
    derive_level_compensated_target,
    evaluate_level_headroom,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _ref() -> ReferenceProfileRef:
    return ReferenceProfileRef(
        profile_id='dolby-pa42',
        version='1',
        semantic_sha256=_hash('ref-profile'),
    )


def _curve() -> CadTargetCurve:
    return CadTargetCurve(
        points=(
            CadTargetCurvePoint(frequency_hz=20.0, level_db=0.0),
            CadTargetCurvePoint(frequency_hz=100.0, level_db=-2.0),
            CadTargetCurvePoint(frequency_hz=1000.0, level_db=0.0),
        ),
        normalization=CadTargetNormalizationCondition(
            method='absolute_level', reference_level_db=0.0
        ),
    )


def _profile(**overrides) -> PlaybackLevelCompensationProfile:
    kwargs = dict(
        model_kind='user_authored_level_table',
        reference_profile_ref=_ref(),
        target_profile_id='house-curve',
        target_profile_version='3',
        target_profile_sha256=_hash('target'),
        level_input='acoustic_level_db_spl',
        level_domain_db=(-40.0, 0.0),
        clamp_policy='clamp_to_domain',
        entries=(
            LevelCompensationEntry(
                level_db=-40.0,
                tonal_curve=(
                    LevelCompensationCurvePoint(
                        frequency_hz=50.0, gain_db=8.0
                    ),
                    LevelCompensationCurvePoint(
                        frequency_hz=1000.0, gain_db=0.0
                    ),
                ),
            ),
            LevelCompensationEntry(
                level_db=0.0,
                tonal_curve=(
                    LevelCompensationCurvePoint(
                        frequency_hz=50.0, gain_db=0.0
                    ),
                    LevelCompensationCurvePoint(
                        frequency_hz=1000.0, gain_db=0.0
                    ),
                ),
            ),
        ),
        algorithm_id='test-table',
        algorithm_version='1',
    )
    kwargs.update(overrides)
    return build_level_compensation_profile(**kwargs)


def test_profile_identity_is_stable_and_pinned() -> None:
    profile = _profile()
    assert profile.profile_id.startswith('level-compensation:')
    assert _profile() == profile
    dump = profile.model_dump(mode='python')
    dump['entries'] = tuple(
        dict(entry) for entry in dump['entries']
    )
    dump['entries'][0]['tonal_curve'] = tuple(
        dict(point) for point in dump['entries'][0]['tonal_curve']
    )
    dump['entries'][0]['tonal_curve'][0]['gain_db'] = 9.0
    with pytest.raises(ValueError, match='semantic hash mismatch'):
        PlaybackLevelCompensationProfile.model_validate(dump)


def test_psychoacoustic_derivation_requires_pinned_authority() -> None:
    with pytest.raises(ValueError, match='pinned authority'):
        _profile(model_kind='psychoacoustic_model_derived')
    profile = _profile(
        model_kind='psychoacoustic_model_derived',
        psychoacoustic_authority=ExactExternalAuthorityRef(
            authority_id='ISO 226:2003',
            authority_version='2003',
            semantic_hash_sha256=_hash('iso226'),
        ),
        psychoacoustic_scope='pure_tone_loudness',
    )
    assert profile.model_kind == 'psychoacoustic_model_derived'


def test_derivation_interpolates_between_levels() -> None:
    profile = _profile()
    derived = derive_level_compensated_target(profile, -20.0, _curve())
    assert derived.state == 'derived'
    assert derived.level_db_applied == pytest.approx(-20.0)
    compensation = dict(derived.compensation_points)
    assert compensation[50.0] == pytest.approx(4.0)
    assert compensation[1000.0] == pytest.approx(0.0)
    # Active target = static target + compensation at the declared grid.
    active = dict(derived.active_target_points)
    # static level at 50 Hz: linear on the (20..100) leg: 0 -> -2 at 100
    assert active[50.0] == pytest.approx(
        -2.0 * (50.0 - 20.0) / (100.0 - 20.0) + 4.0
    )
    assert derived.target_id.startswith('level-compensated-target:')
    assert derived.profile_sha256 == profile.semantic_sha256


def test_clamp_and_reject_policies() -> None:
    profile = _profile()
    clamped = derive_level_compensated_target(profile, -60.0, _curve())
    assert clamped.state == 'clamped_to_domain'
    assert clamped.level_db_applied == pytest.approx(-40.0)
    assert dict(clamped.compensation_points)[50.0] == pytest.approx(8.0)

    rejecting = _profile(clamp_policy='reject_outside_domain')
    with pytest.raises(ValueError, match='outside declared'):
        derive_level_compensated_target(rejecting, -60.0, _curve())


def test_opaque_vendor_profile_is_preserved_not_evaluated() -> None:
    profile = _profile(model_kind='unknown_opaque')
    derived = derive_level_compensated_target(profile, -20.0, _curve())
    assert derived.state == 'opaque_not_evaluable'
    assert derived.compensation_points == ()
    headroom = evaluate_level_headroom(derived, device_max_boost_db=6.0)
    assert headroom.feasible is False
    assert 'opaque' in headroom.reason


def test_headroom_checked_on_active_curve() -> None:
    profile = _profile()
    derived = derive_level_compensated_target(profile, -40.0, _curve())
    headroom = evaluate_level_headroom(derived, device_max_boost_db=6.0)
    assert headroom.max_compensation_boost_db == pytest.approx(8.0)
    assert headroom.feasible is False
    assert headroom.headroom_margin_db == pytest.approx(-2.0)

    generous = evaluate_level_headroom(derived, device_max_boost_db=12.0)
    assert generous.feasible is True

    unknown_ceiling = evaluate_level_headroom(
        derived, device_max_boost_db=None
    )
    assert unknown_ceiling.feasible is False
    assert 'unknown' in unknown_ceiling.reason
