"""Regression tests for the #607 content-loudness authority.

Fixture map (issue §fixtures): LDN10 BS.1770 gating math; LDN20
profile/edition pinning incl. research-only draft; LDN30 content
identity; LDN40 normalization observation honesty; LDN50 gain-chain
separation; LDN60 immersive eligibility; LDN70 matching records.
"""

from __future__ import annotations

from math import isfinite
from pathlib import Path

import pytest

from htdt.cad_loudness_authority import (
    ContentIdentity,
    GATING_ABSOLUTE_LUFS,
    PlaybackGainState,
    assert_quantity_separation,
    build_loudness_matching_record,
    build_loudness_measurement,
    build_normalization_observation,
    build_playback_gain_state,
    compute_integrated_loudness_lufs,
    compute_true_peak_dbtp,
    evaluate_loudness_profile_eligibility,
    loudness_channel_weights,
    normalization_audit_diff,
    seed_content_loudness_profiles,
)
from htdt.cad_loudness_repository import (
    CadLoudnessRepository,
    LoudnessConflictError,
    LoudnessIntegrityError,
)
from htdt.cad_repository import SceneRepository


_DOC = 'doc-loudness'


def _profiles():
    return seed_content_loudness_profiles(
        document_id=_DOC, created_at_utc='2026-10-01T00:00:00+00:00'
    )


def _profile(standard_id: str, edition: str | None = None):
    for p in _profiles():
        if p.standard_id == standard_id and (
            edition is None or p.standard_edition == edition
        ):
            return p
    raise AssertionError(f'no {standard_id}@{edition} profile')


def _content(**overrides):
    kwargs = dict(
        title='Reference Feature',
        stream_or_file_ref='file:///media/movie.mkv',
        edition='uhd-bd-2024',
        track_id='audio-1',
        language='en',
        codec='dts-hd-ma',
        container='mkv',
        format_ref='fmt-603-immersive',
        content_sha256='a' * 64,
        service_source_label='local-library',
    )
    kwargs.update(overrides)
    return ContentIdentity(**kwargs)


# ---------------------------------------------------------------------------
# LDN10 — BS.1770 gating math
# ---------------------------------------------------------------------------


def test_integrated_loudness_gating() -> None:
    """Uniform -23 LUFS content: mono blocks at z = 10^((l+0.691)/10)."""
    target = -23.0
    z = 10 ** ((target + 0.691) / 10.0)
    blocks = ((z,),) * 8
    lufs, count = compute_integrated_loudness_lufs(blocks, (1.0,))
    assert lufs == pytest.approx(target, abs=1e-9)
    assert count == 8

    # Absolute gate: silence is honest None, not 0.
    assert compute_integrated_loudness_lufs(
        ((0.0,),) * 4, (1.0,)
    ) == (None, 0)

    # Relative gate: a quiet tail does not pull the integrated value.
    quiet_z = 10 ** ((-60.0 + 0.691) / 10.0)
    mixed = ((z,),) * 8 + ((quiet_z,),) * 8
    mixed_lufs, kept = compute_integrated_loudness_lufs(mixed, (1.0,))
    assert kept == 8
    assert mixed_lufs == pytest.approx(target, abs=1e-9)


def test_channel_weights_and_true_peak() -> None:
    assert loudness_channel_weights('stereo', 2) == (1.0, 1.0)
    assert loudness_channel_weights('multichannel_5_1', 5) == (
        1.0, 1.0, 1.0, 1.41, 1.41
    )
    with pytest.raises(ValueError):
        loudness_channel_weights('multichannel_5_1', 6)  # LFE excluded
    with pytest.raises(ValueError):
        loudness_channel_weights('object_based', 4)
    assert compute_true_peak_dbtp((0.5, 0.707, 0.6)) == pytest.approx(
        20.0 * __import__('math').log10(0.707)
    )
    assert compute_true_peak_dbtp(()) is None


# ---------------------------------------------------------------------------
# LDN20/60 — profile eligibility: draft research-only, stereo meter
# ineligible for immersive content
# ---------------------------------------------------------------------------


def test_profile_eligibility_gates() -> None:
    production = _profile('itu-r-bs1770', '5')
    draft = _profile('itu-r-bs1770', '2026-draft')
    assert draft.eligibility == 'research_only'
    verdict, reasons = evaluate_loudness_profile_eligibility(
        production, content_config='multichannel_5_1'
    )
    assert verdict == 'production_current' and reasons == ()
    verdict, reasons = evaluate_loudness_profile_eligibility(
        draft, content_config='multichannel_5_1'
    )
    assert verdict == 'research_only'
    # Stereo-only EBU profile is ineligible for object-based content.
    ebu = _profile('ebu-r128')
    verdict, reasons = evaluate_loudness_profile_eligibility(
        ebu, content_config='object_based'
    )
    assert 'immersive_content_requires_extended_or_object_profile' in reasons


# ---------------------------------------------------------------------------
# LDN30/50 — measurement + gain chain separation
# ---------------------------------------------------------------------------


def test_measurement_round_trip_and_profile_pin(tmp_path: Path) -> None:
    repository = CadLoudnessRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    profile = _profile('itu-r-bs1770', '5')
    repository.save_profile(profile)
    measurement = build_loudness_measurement(
        document_id=_DOC,
        content=_content(),
        source_class='disc_file',
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        standard_id=profile.standard_id,
        standard_edition=profile.standard_edition,
        algorithm_version=profile.algorithm_version,
        channel_config='multichannel_5_1',
        profile_eligibility='production_current',
        integrated_loudness_lufs=-27.3,
        loudness_range_lu=14.2,
        true_peak_dbtp=-1.1,
        measured_at_utc='2026-10-01T00:00:00+00:00',
    )
    repository.save_measurement(measurement)
    repository.save_measurement(measurement)
    assert repository.get_measurement(
        measurement.measurement_id
    ) == measurement
    assert repository.list_measurements(_DOC) == (measurement,)

    # Unpinned measurements (unknown profile) cannot persist.
    orphan = CadLoudnessRepository(SceneRepository(tmp_path / 'o.sqlite3'))
    with pytest.raises(LoudnessIntegrityError):
        orphan.save_measurement(measurement)


def test_quantity_separation_gate() -> None:
    # Same quantity needs no chain.
    assert_quantity_separation(
        'programme_integrated_loudness', 'programme_integrated_loudness'
    )
    # LUFS -> SPL without a calibrated chain fails closed.
    with pytest.raises(ValueError, match='calibrated'):
        assert_quantity_separation(
            'programme_integrated_loudness', 'measured_in_room_spl'
        )
    assert_quantity_separation(
        'programme_integrated_loudness',
        'measured_in_room_spl',
        calibrated_chain_ref='cal-chain-579',
    )
    # Programme loudness never reads as system capability.
    with pytest.raises(ValueError, match='calibrated'):
        assert_quantity_separation(
            'programme_integrated_loudness',
            'system_max_clean_capability',
        )


# ---------------------------------------------------------------------------
# LDN40 — normalization audit: applied gain vs measured room SPL
# ---------------------------------------------------------------------------


def test_normalization_observation_and_audit() -> None:
    on = build_normalization_observation(
        document_id=_DOC,
        content=_content(),
        source_class='streaming_video',
        mode='on_target_profile',
        target_profile_ref='provider-tv-target',
        target_lufs=-24.0,
        applied_gain_db=+4.0,
        limiter_state='inactive',
        device_app_version='app-9.1',
        user_setting='normalization=on',
        observed_at_utc='2026-10-01T00:00:00+00:00',
    )
    off = build_normalization_observation(
        document_id=_DOC,
        content=_content(),
        source_class='streaming_video',
        mode='off',
        observed_at_utc='2026-10-01T00:05:00+00:00',
    )
    # An "off" observation cannot claim an applied gain.
    with pytest.raises(Exception, match='off-mode'):
        build_normalization_observation(
            document_id=_DOC,
            source_class='streaming_video',
            mode='off',
            applied_gain_db=-2.0,
            observed_at_utc='2026-10-01T00:05:00+00:00',
        )
    state_on = build_playback_gain_state(
        document_id=_DOC,
        content=_content(),
        normalization_observation_id=on.observation_id,
        service_normalization_gain_db=4.0,
        player_gain_db=0.0,
        processor_input_gain_db=0.0,
        master_volume_db=-10.0,
        channel_trims_db={'center': 0.0},
        calibration_reference_state_ref='cal-579-chain',
        measured_in_room_spl_db=78.0,
        measured_position_label='mlp',
        captured_at_utc='2026-10-01T00:00:00+00:00',
    )
    state_off = build_playback_gain_state(
        document_id=_DOC,
        content=_content(),
        normalization_observation_id=off.observation_id,
        service_normalization_gain_db=None,
        player_gain_db=0.0,
        processor_input_gain_db=0.0,
        master_volume_db=-10.0,
        channel_trims_db={'center': 0.0},
        calibration_reference_state_ref='cal-579-chain',
        measured_in_room_spl_db=74.5,
        measured_position_label='mlp',
        captured_at_utc='2026-10-01T00:05:00+00:00',
    )
    diff = normalization_audit_diff(on, off, state_on, state_off)
    assert diff['applied_gain_delta_db'] == pytest.approx(4.0)
    assert diff['measured_spl_delta_db'] == pytest.approx(3.5)
    # Source-side gain sums metadata+normalization+player only.
    assert state_on.total_source_gain_db == pytest.approx(4.0)


def test_gain_state_repository_and_observation_link(tmp_path: Path) -> None:
    repository = CadLoudnessRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    observation = build_normalization_observation(
        document_id=_DOC,
        source_class='streaming_audio',
        mode='replaygain_style',
        applied_gain_db=-3.0,
        observed_at_utc='2026-10-01T00:00:00+00:00',
    )
    repository.save_observation(observation)
    assert (
        repository.get_observation(observation.observation_id)
        == observation
    )

    state = build_playback_gain_state(
        document_id=_DOC,
        normalization_observation_id=observation.observation_id,
        master_volume_db=-12.0,
        captured_at_utc='2026-10-01T00:00:00+00:00',
    )
    repository.save_gain_state(state)
    assert repository.get_gain_state(state.state_id) == state

    # A state referencing an unpersisted observation fails closed.
    dangling = build_playback_gain_state(
        document_id=_DOC,
        normalization_observation_id='norm:' + 'b' * 64,
        captured_at_utc='2026-10-01T00:10:00+00:00',
    )
    with pytest.raises(LoudnessIntegrityError):
        repository.save_gain_state(dangling)


def test_matching_record_round_trip(tmp_path: Path) -> None:
    repository = CadLoudnessRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    record = build_loudness_matching_record(
        document_id=_DOC,
        comparison_label='eq-a-vs-b-dialogue',
        target_quantity='programme_integrated_loudness',
        measurement_window='dialogue-scene-12',
        side_a_content=_content(),
        side_b_content=_content(edition='stream-2025'),
        applied_gain_a_db=0.0,
        applied_gain_b_db=-1.5,
        residual_mismatch_db=0.2,
        method='short_term_window_matched',
        recorded_at_utc='2026-10-01T00:00:00+00:00',
    )
    repository.save_matching_record(record)
    assert repository.get_matching_record(record.record_id) == record
    with pytest.raises(LoudnessIntegrityError):
        repository.save_matching_record(
            record.model_copy(update={'residual_mismatch_db': 9.9})
        )
