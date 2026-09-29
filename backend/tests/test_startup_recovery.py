"""Tests for #739 Startup Recovery / Safe Mode launch-state logic."""

from __future__ import annotations

from pathlib import Path

from htdt.startup_recovery import (
    RESET_SCOPES,
    RecoveryMetadata,
    SafeModePolicy,
    classify_startup_failure,
    complete_launch,
    decide_launch,
    load_recovery_metadata,
    record_launch,
)


def test_unclean_session_offers_recovery_choices() -> None:
    decision = decide_launch(
        unclean_previous_session=True,
        metadata=RecoveryMetadata(),
        build_id='b1',
    )
    assert decision.mode == 'recovery_offered'
    assert 'open_normal' in decision.choices
    assert 'open_safe_mode' in decision.choices
    assert 'open_diagnostics' in decision.choices
    assert 'choose_another_project' in decision.choices
    # No data-relevant failure class -> restore is not recommended.
    assert not decision.restore_recommended
    assert 'verify_data' not in decision.choices


def test_clean_history_launches_normally() -> None:
    decision = decide_launch(
        unclean_previous_session=False,
        metadata=RecoveryMetadata(),
        build_id='b1',
    )
    assert decision.mode == 'normal'
    assert decision.choices == ()
    assert decision.safe_mode_policy is None


def test_repeated_failures_trigger_recovery_offer(tmp_path: Path) -> None:
    for _ in range(2):
        record = record_launch(
            tmp_path,
            build_id='b1',
            launch_mode='normal',
            started_at_utc='2026-01-01T00:00:00+00:00',
        )
        complete_launch(tmp_path, record.launch_id, clean=False)

    decision = decide_launch(
        unclean_previous_session=False,
        metadata=load_recovery_metadata(tmp_path),
        build_id='b1',
    )
    assert decision.mode == 'recovery_offered'
    assert any('起動失敗' in r for r in decision.reasons)


def test_clean_exit_resets_failure_streak(tmp_path: Path) -> None:
    bad = record_launch(
        tmp_path,
        build_id='b1',
        launch_mode='normal',
        started_at_utc='2026-01-01T00:00:00+00:00',
    )
    complete_launch(tmp_path, bad.launch_id, clean=False)
    good = record_launch(
        tmp_path,
        build_id='b1',
        launch_mode='normal',
        started_at_utc='2026-01-01T00:01:00+00:00',
    )
    complete_launch(tmp_path, good.launch_id, clean=True)

    decision = decide_launch(
        unclean_previous_session=False,
        metadata=load_recovery_metadata(tmp_path),
        build_id='b1',
    )
    assert decision.mode == 'normal'


def test_explicit_safe_mode_returns_policy() -> None:
    decision = decide_launch(
        unclean_previous_session=False,
        metadata=RecoveryMetadata(),
        build_id='b1',
        explicit_safe_mode=True,
        renderer_failure_detected=True,
    )
    assert decision.mode == 'safe_mode'
    policy = decision.safe_mode_policy
    assert isinstance(policy, SafeModePolicy)
    assert policy.auto_open_last_project is False
    assert policy.auto_run_prediction_search_import_jobs is False
    assert policy.live_integrations is False
    assert policy.restore_saved_layout is False
    assert policy.project_authority == 'read_only'
    assert policy.keep_recovery_drafts is True
    assert policy.renderer == 'skip_3d'


def test_restore_only_recommended_for_data_relevant_failures() -> None:
    assert classify_startup_failure(
        'vtkOpenGLRenderWindow failed to initialize'
    ) == 'renderer_initialization'
    assert classify_startup_failure(
        'NativeUpgradeQuarantineError: verification failed'
    ) == 'migration_failure'
    assert classify_startup_failure('totally unexpected boom') == 'unknown'

    renderer = decide_launch(
        unclean_previous_session=True,
        metadata=RecoveryMetadata(),
        build_id='b1',
        failure_class='renderer_initialization',
    )
    assert not renderer.restore_recommended
    assert 'verify_data' not in renderer.choices

    data = decide_launch(
        unclean_previous_session=True,
        metadata=RecoveryMetadata(),
        build_id='b1',
        failure_class='migration_failure',
    )
    assert data.restore_recommended
    assert 'verify_data' in data.choices


def test_restore_backup_choice_requires_backups_and_data_failure() -> None:
    """Round9 #11: 'restore_backup' is offered only when it can succeed."""

    # Data-relevant failure + existing generations -> offered.
    decision = decide_launch(
        unclean_previous_session=True,
        metadata=RecoveryMetadata(),
        build_id='b1',
        failure_class='project_data',
        backup_restore_available=True,
    )
    assert 'restore_backup' in decision.choices
    assert 'verify_data' in decision.choices
    assert decision.backup_restore_available

    # Same failure class but nothing to restore -> never offered.
    no_backups = decide_launch(
        unclean_previous_session=True,
        metadata=RecoveryMetadata(),
        build_id='b1',
        failure_class='project_data',
        backup_restore_available=False,
    )
    assert 'restore_backup' not in no_backups.choices
    assert 'verify_data' in no_backups.choices

    # Backups exist but the crash was renderer/config class -> restore is
    # not the relevant first step.
    renderer = decide_launch(
        unclean_previous_session=True,
        metadata=RecoveryMetadata(),
        build_id='b1',
        failure_class='renderer_initialization',
        backup_restore_available=True,
    )
    assert 'restore_backup' not in renderer.choices
    assert 'verify_data' not in renderer.choices

    # Normal launch: no dialog at all regardless of available backups.
    normal = decide_launch(
        unclean_previous_session=False,
        metadata=RecoveryMetadata(),
        build_id='b1',
        backup_restore_available=True,
    )
    assert normal.choices == ()


def test_renderer_failure_detected_also_offers_recovery() -> None:
    decision = decide_launch(
        unclean_previous_session=False,
        metadata=RecoveryMetadata(),
        build_id='b1',
        renderer_failure_detected=True,
        failure_class='renderer_initialization',
    )
    assert decision.mode == 'recovery_offered'
    assert decision.safe_mode_policy.renderer == 'skip_3d'


def test_reset_scopes_never_touch_project_authority() -> None:
    scopes = {scope.scope for scope in RESET_SCOPES}
    assert scopes == {
        'window_layout',
        'application_preferences',
        'optional_integrations',
    }
    for scope in RESET_SCOPES:
        assert scope.touches_project_data is False
        assert scope.reversible


def test_metadata_is_bounded_and_privacy_safe(tmp_path: Path) -> None:
    for index in range(15):
        record = record_launch(
            tmp_path,
            build_id='b1',
            launch_mode='normal',
            started_at_utc=f'2026-01-01T00:{index:02d}:00+00:00',
            project_ref='doc-7',
            workspace='measure',
            crash_correlation_id='8F2A',
        )
        complete_launch(tmp_path, record.launch_id, clean=True)
    metadata = load_recovery_metadata(tmp_path)
    # Rolling window stays bounded.
    assert len(metadata.records) == 10
    # And every record is operational metadata, never project payload.
    for record in metadata.records:
        assert record.launch_id
        assert record.clean_exit is True
        assert record.last_project_ref == 'doc-7'


def test_last_failure_class_comes_from_records(tmp_path: Path) -> None:
    record = record_launch(
        tmp_path,
        build_id='b1',
        launch_mode='normal',
        started_at_utc='2026-01-01T00:00:00+00:00',
    )
    complete_launch(
        tmp_path,
        record.launch_id,
        clean=False,
        failure_class='integration_initialization',
    )
    metadata = load_recovery_metadata(tmp_path)
    assert metadata.last_failure_class('b1') == (
        'integration_initialization'
    )
    decision = decide_launch(
        unclean_previous_session=False,
        metadata=metadata,
        build_id='b1',
    )
    assert decision.failure_class == 'integration_initialization'
    assert not decision.restore_recommended


def test_annotate_launch_records_project_ref(tmp_path: Path) -> None:
    """annotate_launch back-fills the resolved project on a live record."""
    from htdt.startup_recovery import annotate_launch

    record = record_launch(
        tmp_path,
        build_id='b1',
        launch_mode='normal',
        started_at_utc='2026-01-01T00:00:00+00:00',
    )
    annotate_launch(
        tmp_path, record.launch_id, project_ref='project-9'
    )
    metadata = load_recovery_metadata(tmp_path)
    annotated = next(
        r for r in metadata.records if r.launch_id == record.launch_id
    )
    assert annotated.last_project_ref == 'project-9'


def test_last_failure_class_is_scoped_to_build(tmp_path: Path) -> None:
    """A different build's failure class never steers this launch."""

    record = record_launch(
        tmp_path,
        build_id='build-A',
        launch_mode='normal',
        started_at_utc='2026-01-01T00:00:00+00:00',
    )
    complete_launch(
        tmp_path,
        record.launch_id,
        clean=False,
        failure_class='project_data',
    )
    # A newer build crashes without a classified failure.
    record_launch(
        tmp_path,
        build_id='build-B',
        launch_mode='normal',
        started_at_utc='2026-01-01T00:01:00+00:00',
    )
    metadata = load_recovery_metadata(tmp_path)
    assert metadata.last_failure_class('build-B') is None
    decision = decide_launch(
        unclean_previous_session=True,
        metadata=metadata,
        build_id='build-B',
    )
    # Restore guidance requires evidence from THIS build's own streak.
    assert not decision.restore_recommended


def test_annotate_launch_unknown_launch_id_is_a_noop(tmp_path: Path) -> None:
    from htdt.startup_recovery import annotate_launch

    record = record_launch(
        tmp_path,
        build_id='b1',
        launch_mode='normal',
        started_at_utc='2026-01-01T00:00:00+00:00',
    )
    annotate_launch(tmp_path, 'no-such-launch', project_ref='p')
    metadata = load_recovery_metadata(tmp_path)
    annotated = next(
        r for r in metadata.records if r.launch_id == record.launch_id
    )
    assert annotated.last_project_ref is None
