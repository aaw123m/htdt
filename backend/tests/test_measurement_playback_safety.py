"""Tests for #734 measurement playback safety preflight."""

from __future__ import annotations

from htdt.measurement_playback_safety import (
    ObservedOutputState,
    PlaybackPreflightRequest,
    PlaybackSafetyPolicy,
    PlaybackStimulusSummary,
    build_abort_report,
    decide_preflight,
    run_playback_preflight,
)


def _stimulus(**overrides) -> PlaybackStimulusSummary:
    payload = {
        'kind': 'sweep',
        'digital_level_dbfs': -20.0,
        'band_hz': (20.0, 20000.0),
        'duration_s': 5.0,
        'repetitions': 1,
        'channel_count': 2,
        'coherent_channels': False,
        'producer': 'rew',
    }
    payload.update(overrides)
    return PlaybackStimulusSummary(**payload)


def _observed(**overrides) -> ObservedOutputState:
    payload = {
        'observed_at_utc': '2026-01-01T00:00:00+00:00',
        'source': 'adapter_read_back',
        'master_volume': -40.0,
        'master_volume_unit': 'avr_display_units',
        'muted': False,
        'routing_signature': 'route-a',
    }
    payload.update(overrides)
    return ObservedOutputState(**payload)


def _request(**overrides) -> PlaybackPreflightRequest:
    payload = {
        'request_id': 'req-1',
        'session_step_id': 'cell-7',
        'action': 'start_sweep',
        'intended_source_id': 'rew-usb',
        'intended_output_target': 'avr-hdmi-1',
        'expected_routing_signature': 'route-a',
        'stimulus': _stimulus(),
        'observed': _observed(),
    }
    payload.update(overrides)
    return PlaybackPreflightRequest(**payload)


def test_fully_verified_evidence_is_ready() -> None:
    result = run_playback_preflight(
        _request(), evaluated_at_utc='2026-01-01T00:00:00+00:00'
    )
    assert result.outcome == 'ready'
    assert result.automated_start_permitted
    assert not result.requires_confirmation
    view = result.resolved_view
    assert view['observed_master_volume'] == -40.0
    assert view['observed_muted'] is False
    assert view['observed_routing_signature'] == 'route-a'
    assert view['stimulus']['kind'] == 'sweep'
    assert view['unknowns'] == []


def test_missing_read_back_requires_manual_control() -> None:
    result = run_playback_preflight(
        _request(observed=None),
        evaluated_at_utc='2026-01-01T00:00:00+00:00',
    )
    assert result.outcome == 'unknown_manual_control_required'
    codes = {r.code for r in result.reasons}
    assert 'missing_read_back' in codes
    assert set(result.resolved_view['unknowns']) == {
        'master_volume', 'mute_state', 'routing_state'
    }
    # Manual-only workflow stays usable: decision under manual control
    # permits; without it, playback is refused.
    assert decide_preflight(
        result,
        decision='manual_control_accepted',
        decided_at_utc='2026-01-01T00:00:01+00:00',
    ).playback_permitted
    assert not decide_preflight(
        result,
        decision='confirmed',
        decided_at_utc='2026-01-01T00:00:01+00:00',
    ).playback_permitted


def test_unknown_volume_and_mute_are_explicit_unknowns() -> None:
    result = run_playback_preflight(
        _request(
            observed=_observed(master_volume=None, muted=None),
        ),
        evaluated_at_utc='2026-01-01T00:00:00+00:00',
    )
    assert result.outcome == 'unknown_manual_control_required'
    codes = {r.code for r in result.reasons}
    assert 'unknown_volume' in codes
    assert 'unknown_mute_state' in codes


def test_level_over_policy_limit_blocks() -> None:
    result = run_playback_preflight(
        _request(
            stimulus=_stimulus(digital_level_dbfs=-5.0),
            policy=PlaybackSafetyPolicy(max_digital_level_dbfs=-12.0),
        ),
        evaluated_at_utc='2026-01-01T00:00:00+00:00',
    )
    assert result.outcome == 'blocked'
    assert result.reasons[0].code == 'configured_limit_exceeded'
    # Even an explicit confirmation cannot start a blocked action.
    assert not decide_preflight(
        result,
        decision='confirmed',
        decided_at_utc='2026-01-01T00:00:01+00:00',
    ).playback_permitted


def test_routing_mismatch_blocks() -> None:
    result = run_playback_preflight(
        _request(observed=_observed(routing_signature='route-b')),
        evaluated_at_utc='2026-01-01T00:00:00+00:00',
    )
    assert result.outcome == 'blocked'
    assert any(r.code == 'routing_mismatch' for r in result.reasons)


def test_unverifiable_routing_is_unknown_not_ready() -> None:
    result = run_playback_preflight(
        _request(observed=_observed(routing_signature=None)),
        evaluated_at_utc='2026-01-01T00:00:00+00:00',
    )
    assert result.outcome == 'unknown_manual_control_required'
    assert 'routing_state' in result.resolved_view['unknowns']


def test_changed_inputs_invalidate_to_blocked() -> None:
    result = run_playback_preflight(
        _request(changed_inputs=('master_volume',)),
        evaluated_at_utc='2026-01-01T00:00:00+00:00',
    )
    assert result.outcome == 'blocked'
    assert result.reasons[0].code == 'stale_evidence'


def test_newly_bound_routing_gets_low_level_confirmation_step() -> None:
    result = run_playback_preflight(
        _request(
            routing_newly_bound=True,
            supports_low_level_probe=True,
        ),
        evaluated_at_utc='2026-01-01T00:00:00+00:00',
    )
    assert result.outcome == 'ready_with_confirmation'
    assert result.low_level_confirmation_step is not None
    assert 'low-level' in result.low_level_confirmation_step


def test_device_muted_requires_confirmation() -> None:
    result = run_playback_preflight(
        _request(observed=_observed(muted=True)),
        evaluated_at_utc='2026-01-01T00:00:00+00:00',
    )
    assert result.outcome == 'ready_with_confirmation'
    assert any(r.code == 'device_muted' for r in result.reasons)
    assert decide_preflight(
        result,
        decision='confirmed',
        decided_at_utc='2026-01-01T00:00:01+00:00',
    ).playback_permitted


def test_manual_control_disallowed_turns_unknown_into_blocked() -> None:
    result = run_playback_preflight(
        _request(
            observed=None,
            policy=PlaybackSafetyPolicy(manual_control_permitted=False),
        ),
        evaluated_at_utc='2026-01-01T00:00:00+00:00',
    )
    assert result.outcome == 'blocked'


def test_result_binds_to_session_and_invalidates_on_change() -> None:
    request = _request()
    result = run_playback_preflight(
        request, evaluated_at_utc='2026-01-01T00:00:00+00:00'
    )
    assert result.session_step_id == 'cell-7'
    assert result.matches(request)
    changed = _request(observed=_observed(master_volume=-10.0))
    assert not result.matches(changed)
    changed_stimulus = _request(stimulus=_stimulus(repetitions=2))
    assert not result.matches(changed_stimulus)


def test_abort_report_never_claims_guaranteed_stop() -> None:
    report = build_abort_report(
        aborted_at_utc='2026-01-01T00:00:05+00:00',
        preflight_id='pf-1',
        stop_sweep_outcome='acknowledged',
        mute_outcome='attempted_unknown',
    )
    assert report.workflow_halted is True
    assert report.software_stop_is_guaranteed is False
    assert report.acknowledged_actions == ('stop_sweep', 'halt_campaign_advancement')
    assert report.unresolved_actions == ('request_mute',)


def test_no_universal_safe_level_is_assumed() -> None:
    # A loud stimulus with no configured limit is startable only because
    # the *evidence* verifies, not because of a hidden threshold.
    result = run_playback_preflight(
        _request(stimulus=_stimulus(digital_level_dbfs=-1.0)),
        evaluated_at_utc='2026-01-01T00:00:00+00:00',
    )
    assert result.outcome == 'ready'
    assert not any(
        r.code == 'configured_limit_exceeded' for r in result.reasons
    )
    # And it stays honest about what it does not prove.
    assert any('SPL' in item for item in result.limitations)
