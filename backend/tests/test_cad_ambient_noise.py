from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_ambient_noise import (
    AmbientNoiseCriterion,
    CadAmbientNoiseRepository,
    ambient_overall_level_db,
    ambient_snr_db,
    build_ambient_noise_criterion,
    build_ambient_noise_profile,
    build_ambient_operating_condition,
    compare_ambient_profiles,
    evaluate_ambient_criterion,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene


OCTAVE_BANDS = (63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0)
NC_25_LIMITS = (54.0, 44.0, 37.0, 31.0, 27.0, 24.0, 22.0, 21.0)


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    return scene_repository, revision, CadAmbientNoiseRepository(scene_repository)


def _condition(revision, **overrides):
    kwargs: dict = {
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'hvac_state': 'off',
        'projector_state': 'off',
        'created_at': '2026-09-23T00:00:00+00:00',
    }
    kwargs.update(overrides)
    return build_ambient_operating_condition(**kwargs)


def _profile(condition, **overrides):
    kwargs: dict = {
        'microphone_position': Position3(x_m=3.0, y_m=3.0, z_m=1.1),
        'method': 'measured',
        'level_semantics': 'absolute_spl',
        'weighting': 'Z',
        'band_spec': 'octave',
        'band_center_hz': OCTAVE_BANDS,
        'band_level_db': (30.0, 28.0, 25.0, 22.0, 20.0, 18.0, 16.0, 14.0),
        'integration_duration_s': 10.0,
        'captured_at': '2026-09-23T01:00:00+00:00',
    }
    kwargs.update(overrides)
    return build_ambient_noise_profile(condition, **kwargs)


def _criterion():
    return build_ambient_noise_criterion(
        name='NC-25',
        criterion_version='nc-contour-1',
        band_center_hz=OCTAVE_BANDS,
        limit_level_db=NC_25_LIMITS,
    )


def test_condition_separates_equipment_states(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    off = _condition(revision)
    on = _condition(revision, hvac_state='on')
    unknown = _condition(revision, hvac_state='unknown')
    assert len({off.condition_sha256, on.condition_sha256, unknown.condition_sha256}) == 3
    assert unknown.hvac_state == 'unknown'


def test_profile_level_semantics_stay_explicit(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    condition = _condition(revision)
    relative = _profile(condition, level_semantics='relative')
    assert relative.level_semantics == 'relative'


def test_criterion_evaluation_on_absolute_profile(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    profile = _profile(_condition(revision))
    evaluation = evaluate_ambient_criterion(
        profile,
        _criterion(),
        evaluated_at='2026-09-23T02:00:00+00:00',
    )
    assert evaluation.verdict == 'PASS'
    assert set(evaluation.band_verdicts) == {'PASS'}


def test_criterion_evaluation_detects_failing_band(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    profile = _profile(
        _condition(revision),
        band_level_db=(60.0, 28.0, 25.0, 22.0, 20.0, 18.0, 16.0, 14.0),
    )
    evaluation = evaluate_ambient_criterion(
        profile,
        _criterion(),
        evaluated_at='2026-09-23T02:00:00+00:00',
    )
    assert evaluation.verdict == 'FAIL'
    assert evaluation.band_verdicts[0] == 'FAIL'


def test_uncalibrated_profile_cannot_produce_absolute_verdict(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    for semantics in ('relative', 'unknown'):
        profile = _profile(_condition(revision), level_semantics=semantics)
        evaluation = evaluate_ambient_criterion(
            profile,
            _criterion(),
            evaluated_at='2026-09-23T02:00:00+00:00',
        )
        assert evaluation.verdict == 'UNKNOWN'
        assert set(evaluation.band_verdicts) == {'UNKNOWN'}


def test_band_basis_mismatch_is_unknown(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    profile = _profile(
        _condition(revision),
        band_spec='third_octave',
        band_center_hz=(100.0, 125.0, 160.0),
        band_level_db=(20.0, 20.0, 20.0),
    )
    evaluation = evaluate_ambient_criterion(
        profile,
        _criterion(),
        evaluated_at='2026-09-23T02:00:00+00:00',
    )
    assert evaluation.verdict == 'UNKNOWN'


def test_measured_condition_comparison(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    off = _profile(_condition(revision, hvac_state='off'), overall_level_db=32.0)
    on = _profile(
        _condition(revision, hvac_state='on', created_at='2026-09-23T00:10:00+00:00'),
        band_level_db=(40.0, 38.0, 35.0, 32.0, 30.0, 28.0, 26.0, 24.0),
        overall_level_db=42.0,
    )
    comparison = compare_ambient_profiles(
        on,
        off,
        created_at='2026-09-23T03:00:00+00:00',
    )
    assert comparison.difference_db == (10.0,) * 8
    assert comparison.overall_difference_db == pytest.approx(10.0)


def test_snr_evidence_requires_absolute_spl(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    profile = _profile(_condition(revision), overall_level_db=35.0)
    assert ambient_snr_db(profile, 75.0) == pytest.approx(40.0)
    relative = _profile(_condition(revision), level_semantics='relative', overall_level_db=35.0)
    with pytest.raises(ValueError, match='absolute-SPL'):
        ambient_snr_db(relative, 75.0)


def test_overall_level_from_absolute_bands(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    profile = _profile(_condition(revision))
    derived = ambient_overall_level_db(profile)
    assert derived is not None and derived > 30.0
    relative = _profile(_condition(revision), level_semantics='relative')
    assert ambient_overall_level_db(relative) is None


def test_repository_round_trip_and_evaluation_replay(tmp_path: Path):
    scene_repository, revision, repository = _repositories(tmp_path)
    condition = _condition(revision)
    repository.save_condition(condition)
    profile = _profile(condition)
    repository.save_profile(profile)
    criterion = _criterion()
    repository.save_criterion(criterion)
    evaluation = evaluate_ambient_criterion(
        profile,
        criterion,
        evaluated_at='2026-09-23T02:00:00+00:00',
    )
    repository.save_evaluation(evaluation)
    assert repository.get_condition(condition.condition_id) == condition
    assert repository.get_profile(profile.profile_id) == profile
    assert repository.get_criterion(criterion.criterion_id) == criterion
    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
    assert repository.list_profiles(revision.document_id) == (profile,)
    # A mismatched profile binding fails closed.
    other = _profile(_condition(revision, hvac_state='on'), overall_level_db=50.0)
    forged = evaluate_ambient_criterion(
        other,
        criterion,
        evaluated_at='2026-09-23T02:00:00+00:00',
    )
    forged = forged.model_copy(update={'profile_id': profile.profile_id})
    with pytest.raises(ValueError):
        repository.save_evaluation(forged)


def test_comparison_requires_same_band_basis(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    octave = _profile(_condition(revision))
    third = _profile(
        _condition(revision),
        band_spec='third_octave',
        band_center_hz=(100.0, 125.0, 160.0),
        band_level_db=(20.0, 20.0, 20.0),
    )
    with pytest.raises(ValueError, match='same band basis'):
        compare_ambient_profiles(octave, third, created_at='2026-09-23T03:00:00+00:00')


def test_absolute_vs_relative_is_blocked_not_subtracted(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    absolute = _profile(_condition(revision))
    relative = _profile(_condition(revision), level_semantics='relative')
    comparison = compare_ambient_profiles(
        absolute, relative, created_at='2026-09-23T03:00:00+00:00'
    )
    assert comparison.level_compatibility == 'blocked'
    assert comparison.quantitative_delta_db() is None
    statuses = {c.check: c.status for c in comparison.compatibility_checks}
    assert statuses['level_semantics_compatible'] == 'BLOCKED'


def test_weighting_mismatch_is_blocked(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    weighted_a = _profile(_condition(revision), weighting='A')
    weighted_z = _profile(_condition(revision), weighting='Z')
    comparison = compare_ambient_profiles(
        weighted_a, weighted_z, created_at='2026-09-23T03:00:00+00:00'
    )
    assert comparison.level_compatibility == 'blocked'
    statuses = {c.check: c.status for c in comparison.compatibility_checks}
    assert statuses['weighting_compatible'] == 'BLOCKED'


def test_same_position_hvac_ab_is_quantitative(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    off_condition = _condition(revision, hvac_state='off')
    on_condition = _condition(
        revision,
        hvac_state='on',
        created_at='2026-09-23T00:10:00+00:00',
    )
    off = _profile(off_condition, calibration_authority_id='cal-1')
    on = _profile(
        on_condition,
        calibration_authority_id='cal-1',
        band_level_db=(40.0, 38.0, 35.0, 32.0, 30.0, 28.0, 26.0, 24.0),
        overall_level_db=42.0,
    )
    comparison = compare_ambient_profiles(
        on,
        off,
        created_at='2026-09-23T03:00:00+00:00',
        intent='operating_state_ab',
        condition_a=on_condition,
        condition_b=off_condition,
    )
    assert comparison.level_compatibility == 'quantitative_delta_db'
    assert comparison.differing_condition_axes == ('hvac_state',)
    statuses = {c.check: c.status for c in comparison.compatibility_checks}
    assert statuses['difference_attributable'] == 'PASS'
    assert statuses['intent_requirements'] == 'PASS'


def test_moved_microphone_downgrades_attribution(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    condition = _condition(revision)
    at_a = _profile(condition, calibration_authority_id='cal-1')
    at_b = _profile(
        condition,
        microphone_position=Position3(x_m=4.5, y_m=3.0, z_m=1.1),
        calibration_authority_id='cal-1',
    )
    comparison = compare_ambient_profiles(
        at_a,
        at_b,
        created_at='2026-09-23T03:00:00+00:00',
        condition_a=condition,
        condition_b=condition,
    )
    statuses = {c.check: c.status for c in comparison.compatibility_checks}
    assert statuses['position_semantics'] == 'UNKNOWN'
    assert statuses['difference_attributable'] == 'UNKNOWN'


def test_spatial_noise_map_intent_allows_position_difference(tmp_path: Path):
    _, revision, _ = _repositories(tmp_path)
    condition = _condition(revision)
    at_a = _profile(condition)
    at_b = _profile(
        condition,
        microphone_position=Position3(x_m=1.0, y_m=1.0, z_m=1.1),
    )
    comparison = compare_ambient_profiles(
        at_a,
        at_b,
        created_at='2026-09-23T03:00:00+00:00',
        intent='spatial_noise_map',
        condition_a=condition,
        condition_b=condition,
    )
    statuses = {c.check: c.status for c in comparison.compatibility_checks}
    assert statuses['position_semantics'] == 'PASS'
    assert statuses['intent_requirements'] == 'PASS'


def test_comparison_repository_replays_typed_decision(tmp_path: Path):
    scene_repository, revision, repository = _repositories(tmp_path)
    off_condition = _condition(revision, hvac_state='off')
    on_condition = _condition(
        revision,
        hvac_state='on',
        created_at='2026-09-23T00:10:00+00:00',
    )
    repository.save_condition(off_condition)
    repository.save_condition(on_condition)
    off = _profile(off_condition)
    on = _profile(
        on_condition,
        band_level_db=(40.0, 38.0, 35.0, 32.0, 30.0, 28.0, 26.0, 24.0),
    )
    repository.save_profile(off)
    repository.save_profile(on)
    comparison = compare_ambient_profiles(
        on,
        off,
        created_at='2026-09-23T03:00:00+00:00',
        intent='operating_state_ab',
        condition_a=on_condition,
        condition_b=off_condition,
    )
    repository.save_comparison(comparison)
    assert repository.get_comparison(comparison.comparison_id) == comparison
