from __future__ import annotations

from pathlib import Path
import sqlite3
from zipfile import ZipFile

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    read_native_schema_version,
)
from htdt.cad_scene import make_f1_scene
from htdt.native_backup import DATABASE_NAME
from htdt.native_upgrade import (
    IncompatibleNewerSchemaError,
    NativeUpgradeError,
    NativeUpgradeQuarantineError,
    NativeUpgradeVerificationError,
    UpgradeStateRecord,
    execute_native_upgrade,
    list_upgrade_events,
    newer_schema_guidance,
    plan_native_upgrade,
    read_upgrade_state,
    upgrade_snapshot_dir,
    write_upgrade_state,
)
from test_cad_schema import _LEGACY_DDL


def _database(data_dir: Path) -> Path:
    return data_dir / DATABASE_NAME


def _create_current_database(data_dir: Path) -> None:
    repository = SceneRepository(_database(data_dir))
    repository.save(make_f1_scene(), parent_revision_id=None)


def _stamp_schema_version(data_dir: Path, version: int) -> None:
    """Fake an older-version database for lifecycle tests."""

    with sqlite3.connect(_database(data_dir)) as connection, connection:
        connection.execute(
            'UPDATE native_schema_metadata SET schema_version = ?',
            (version,),
        )
        connection.execute(
            'DELETE FROM native_schema_migrations WHERE schema_version >= ?',
            (version + 1,),
        )


def _create_newer_database(data_dir: Path, version: int) -> None:
    _create_current_database(data_dir)
    _stamp_schema_version(data_dir, version)


def _create_legacy_database(data_dir: Path) -> None:
    """A genuine pre-versioning database (no metadata tables, legacy shape)."""

    # Build a real scene payload on a scratch modern DB so the legacy row
    # survives the authority audit the recovery snapshot replays.
    scratch_dir = data_dir.parent / f'{data_dir.name}-scratch'
    repository = SceneRepository(scratch_dir / DATABASE_NAME)
    saved = repository.save(make_f1_scene(), parent_revision_id=None)
    with sqlite3.connect(scratch_dir / DATABASE_NAME) as connection:
        row = connection.execute(
            'SELECT revision_id, document_id, created_at_utc, content_hash, '
            'payload_json FROM scene_revisions WHERE revision_id = ?',
            (saved.revision.revision_id,),
        ).fetchone()

    data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_database(data_dir)) as connection, connection:
        for statement in _LEGACY_DDL:
            connection.execute(statement)
        connection.execute(
            'INSERT INTO scene_revisions('
            'revision_id, document_id, created_at_utc, content_hash, payload_json'
            ') VALUES (?, ?, ?, ?, ?)',
            row,
        )


def test_plan_on_empty_data_dir_is_fresh_install(tmp_path: Path) -> None:
    plan = plan_native_upgrade(tmp_path)
    assert plan.compatibility == 'fresh_install'
    assert not plan.requires_data_update
    assert not plan.recovery_snapshot_required
    assert plan.migration_steps == ()


def test_plan_on_current_schema_is_silent(tmp_path: Path) -> None:
    _create_current_database(tmp_path)
    plan = plan_native_upgrade(tmp_path)
    assert plan.compatibility == 'current'
    assert not plan.requires_data_update
    assert plan.migration_steps == ()
    assert list_upgrade_events(tmp_path) == ()


def test_plan_on_older_schema_requires_recovery_snapshot(tmp_path: Path) -> None:
    _create_current_database(tmp_path)
    _stamp_schema_version(tmp_path, NATIVE_SCHEMA_VERSION - 1)
    plan = plan_native_upgrade(tmp_path)
    assert plan.compatibility == 'migration_required'
    assert plan.requires_data_update
    assert plan.recovery_snapshot_required
    assert plan.migration_steps == (NATIVE_SCHEMA_VERSION,)
    assert plan.estimated_snapshot_bytes is not None
    assert plan.estimated_snapshot_bytes > 0


def test_current_schema_opening_is_a_no_op(tmp_path: Path) -> None:
    _create_current_database(tmp_path)
    event = execute_native_upgrade(tmp_path)
    assert event.outcome == 'no_upgrade'
    assert event.verification_state == 'not_required'
    # A normal same-schema startup must not grow an upgrade journal or
    # recovery generations.
    assert list_upgrade_events(tmp_path) == ()
    assert not upgrade_snapshot_dir(tmp_path).exists()


def test_migration_creates_verified_recovery_snapshot_then_migrates(
    tmp_path: Path,
) -> None:
    _create_current_database(tmp_path)
    _stamp_schema_version(tmp_path, NATIVE_SCHEMA_VERSION - 1)

    event = execute_native_upgrade(tmp_path)

    assert event.outcome == 'completed'
    assert event.from_schema == NATIVE_SCHEMA_VERSION - 1
    assert event.to_schema == NATIVE_SCHEMA_VERSION
    assert event.verification_state == 'verified'
    assert event.recovery_snapshot_ref is not None
    snapshot = Path(event.recovery_snapshot_ref)
    assert snapshot.is_file()
    assert snapshot.parent == upgrade_snapshot_dir(tmp_path)
    # The recovery copy is a fully validated .htdt-backup of the OLD
    # generation — restorable through the normal restore workflow.
    with ZipFile(snapshot) as archive:
        assert 'manifest.json' in archive.namelist()
        assert DATABASE_NAME in archive.namelist()
    assert read_native_schema_version(_database(tmp_path)) == NATIVE_SCHEMA_VERSION

    events = list_upgrade_events(tmp_path)
    assert len(events) == 1
    assert events[0].outcome == 'completed'
    assert events[0].recovery_snapshot_ref == str(snapshot)


def test_legacy_unversioned_database_is_upgraded_with_snapshot(
    tmp_path: Path,
) -> None:
    _create_legacy_database(tmp_path)
    plan = plan_native_upgrade(tmp_path)
    assert plan.compatibility == 'legacy_unversioned'
    assert plan.recovery_snapshot_required

    event = execute_native_upgrade(tmp_path)

    assert event.outcome == 'completed'
    assert event.from_schema == 0
    assert event.verification_state == 'verified'
    assert read_native_schema_version(_database(tmp_path)) == NATIVE_SCHEMA_VERSION
    assert Path(event.recovery_snapshot_ref).is_file()


def test_newer_schema_fails_closed_with_actionable_guidance(
    tmp_path: Path,
) -> None:
    _create_newer_database(tmp_path, NATIVE_SCHEMA_VERSION + 2)

    with pytest.raises(IncompatibleNewerSchemaError) as caught:
        execute_native_upgrade(tmp_path)

    message = str(caught.value)
    assert f'schema v{NATIVE_SCHEMA_VERSION + 2}' in message
    assert f'v{NATIVE_SCHEMA_VERSION}' in message
    assert 'will not modify' in message
    # The database stays untouched.
    assert read_native_schema_version(_database(tmp_path)) == (
        NATIVE_SCHEMA_VERSION + 2
    )
    # No journal entry and no snapshot for a fail-closed plan.
    assert list_upgrade_events(tmp_path) == ()
    assert not upgrade_snapshot_dir(tmp_path).exists()


def test_newer_schema_guidance_names_both_formats() -> None:
    text = newer_schema_guidance(9, 7)
    assert 'v9' in text
    assert 'v7' in text
    assert 'newer HTDT build' in text or 'install the HTDT build' in text


def test_failed_migration_is_journaled_and_snapshot_kept(tmp_path: Path) -> None:
    _create_current_database(tmp_path)
    _stamp_schema_version(tmp_path, NATIVE_SCHEMA_VERSION - 1)
    # Break the database so the post-migration openability check fails:
    # deleting a core table makes SceneRepository._initialize recreate it —
    # instead corrupt the file bytes after the snapshot step by monkeypatching
    # the verifier. Simpler: drop a required column so adoption fails.
    with sqlite3.connect(_database(tmp_path)) as connection, connection:
        connection.execute('DROP TABLE scene_revisions')
        connection.execute(
            'CREATE TABLE scene_revisions (bogus INTEGER PRIMARY KEY)'
        )

    with pytest.raises(NativeUpgradeError):
        execute_native_upgrade(tmp_path)

    events = list_upgrade_events(tmp_path)
    assert len(events) == 1
    assert events[0].outcome in ('failed', 'blocked')
    assert events[0].failure_summary is not None


def test_snapshot_retention_is_bounded(tmp_path: Path) -> None:
    _create_current_database(tmp_path)
    _stamp_schema_version(tmp_path, NATIVE_SCHEMA_VERSION - 1)
    snapshot_dir = upgrade_snapshot_dir(tmp_path)
    snapshot_dir.mkdir(parents=True)
    for index in range(5):
        (snapshot_dir / f'pre-upgrade-stale-{index}.htdt-backup').write_bytes(b'x')

    execute_native_upgrade(tmp_path, keep_snapshots=2)

    remaining = sorted(snapshot_dir.glob('pre-upgrade-*.htdt-backup'))
    # keep=2 verified generations: the stale ones are pruned.
    assert len(remaining) == 2


def test_fresh_install_runs_migrations_without_snapshot(tmp_path: Path) -> None:
    event = execute_native_upgrade(tmp_path)
    assert event.outcome == 'fresh_install'
    assert event.recovery_snapshot_ref is None
    assert read_native_schema_version(_database(tmp_path)) == NATIVE_SCHEMA_VERSION
    assert not upgrade_snapshot_dir(tmp_path).exists()
    events = list_upgrade_events(tmp_path)
    assert len(events) == 1


def test_second_open_after_upgrade_is_silent(tmp_path: Path) -> None:
    _create_legacy_database(tmp_path)
    execute_native_upgrade(tmp_path)
    event = execute_native_upgrade(tmp_path)
    assert event.outcome == 'no_upgrade'
    # The first upgrade journaled exactly one event; no new entries appear.
    assert len(list_upgrade_events(tmp_path)) == 1


def _prepare_upgrade_candidate(data_dir: Path) -> None:
    """A live database one schema generation behind the build."""

    _create_current_database(data_dir)
    _stamp_schema_version(data_dir, NATIVE_SCHEMA_VERSION - 1)


def test_post_commit_verification_failure_quarantines_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_upgrade_candidate(tmp_path)

    def _fail_verify(database_path: Path, expected_version: int) -> None:
        raise NativeUpgradeVerificationError(
            'semantic_audit', 'injected verification failure'
        )

    monkeypatch.setattr(
        'htdt.native_upgrade._verify_upgraded_database', _fail_verify
    )

    with pytest.raises(NativeUpgradeQuarantineError) as caught:
        execute_native_upgrade(tmp_path)

    # The migration transaction committed — the live DB IS at vN now.
    assert read_native_schema_version(_database(tmp_path)) == (
        NATIVE_SCHEMA_VERSION
    )
    # The durable unresolved marker survives for the next startup.
    marker = read_upgrade_state(tmp_path)
    assert marker is not None
    assert marker.state == 'failed_after_commit'
    assert marker.failure_stage == 'semantic_audit'
    assert marker.recovery_snapshot_ref is not None
    # Recovery copy reference + explicit choices travel on the error.
    assert caught.value.recovery_snapshot_ref == (
        marker.recovery_snapshot_ref
    )
    assert caught.value.recovery_choices == (
        'retry_verification',
        'restore_recovery_copy',
        'open_diagnostics',
    )
    # Honest copy: never claims the live database was unmodified.
    assert 'committed, but verification failed' in str(caught.value)
    assert 'did not modify' not in str(caught.value)

    events = list_upgrade_events(tmp_path)
    assert len(events) == 1
    event = events[0]
    assert event.outcome == 'failed'
    assert event.migration_commit_state == 'committed'
    assert event.live_generation_state == 'migrated_unverified'
    assert event.verification_failure_stage == 'semantic_audit'
    assert event.recovery_snapshot_ref is not None


def test_restart_after_failed_verification_reverifies_instead_of_no_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_upgrade_candidate(tmp_path)

    def _fail_verify(database_path: Path, expected_version: int) -> None:
        raise NativeUpgradeVerificationError(
            'repository_open', 'injected verification failure'
        )

    monkeypatch.setattr(
        'htdt.native_upgrade._verify_upgraded_database', _fail_verify
    )
    with pytest.raises(NativeUpgradeQuarantineError):
        execute_native_upgrade(tmp_path)

    # Simulated restart: the same failing verifier keeps the generation
    # quarantined — never an ordinary no_upgrade.
    with pytest.raises(NativeUpgradeQuarantineError) as caught:
        execute_native_upgrade(tmp_path)
    assert 'will not open this generation' in str(caught.value)
    assert read_upgrade_state(tmp_path).state == 'failed_after_commit'

    # With the verifier restored, retry-verification reruns the real
    # bounded checks on the committed generation and clears quarantine
    # without repeating the migration.
    monkeypatch.undo()
    event = execute_native_upgrade(tmp_path)
    assert event.outcome == 'completed'
    assert event.verification_state == 'verified'
    assert event.migration_commit_state == 'committed'
    assert event.live_generation_state == 'migrated_verified'
    assert read_upgrade_state(tmp_path) is None

    # The next open is the ordinary silent no-op again.
    assert execute_native_upgrade(tmp_path).outcome == 'no_upgrade'


def test_crash_after_commit_before_state_persistence_is_quarantined(
    tmp_path: Path,
) -> None:
    # Crash window: migration committed vN but the process died before the
    # pending-verification marker refresh — the marker is still 'migrating'.
    _create_current_database(tmp_path)
    write_upgrade_state(
        tmp_path,
        UpgradeStateRecord(
            state='committed_pending_verification',
            upgrade_id='crash-sim',
            from_schema=NATIVE_SCHEMA_VERSION - 1,
            to_schema=NATIVE_SCHEMA_VERSION,
            started_at_utc='2026-01-01T00:00:00+00:00',
            updated_at_utc='2026-01-01T00:00:00+00:00',
        ),
    )

    event = execute_native_upgrade(tmp_path)

    # Not a silent no_upgrade: the unresolved generation is re-verified.
    assert event.outcome == 'completed'
    assert event.verification_state == 'verified'
    assert event.live_generation_state == 'migrated_verified'
    assert event.upgrade_id == 'crash-sim'
    assert read_upgrade_state(tmp_path) is None


def test_migration_failure_marks_before_commit_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_upgrade_candidate(tmp_path)

    def _boom(database_path: Path) -> None:
        raise RuntimeError('injected migration failure')

    monkeypatch.setattr(
        'htdt.native_upgrade.ensure_native_schema', _boom
    )

    with pytest.raises(NativeUpgradeError) as caught:
        execute_native_upgrade(tmp_path)

    # Before-commit failure is not a quarantine and says so honestly.
    assert not isinstance(caught.value, NativeUpgradeQuarantineError)
    assert 'did not modify the live database' in str(caught.value)

    marker = read_upgrade_state(tmp_path)
    assert marker is not None
    assert marker.state == 'failed_before_commit'
    assert marker.failure_stage == 'migration'

    event = list_upgrade_events(tmp_path)[0]
    assert event.outcome == 'failed'
    assert event.migration_commit_state == 'rolled_back'
    assert event.live_generation_state == 'unchanged'
    assert event.verification_failure_stage == 'migration'

    # The live database is still the old generation: the next launch
    # retries the whole upgrade rather than quarantining.
    assert read_native_schema_version(_database(tmp_path)) == (
        NATIVE_SCHEMA_VERSION - 1
    )
    monkeypatch.undo()
    assert execute_native_upgrade(tmp_path).outcome == 'completed'
    assert read_upgrade_state(tmp_path) is None


def test_clean_current_schema_has_no_marker_and_stays_silent(
    tmp_path: Path,
) -> None:
    _create_current_database(tmp_path)
    assert read_upgrade_state(tmp_path) is None
    event = execute_native_upgrade(tmp_path)
    assert event.outcome == 'no_upgrade'
    assert event.live_generation_state == 'unchanged'
    assert list_upgrade_events(tmp_path) == ()


def test_upgrade_copy_ja_is_japanese(tmp_path: Path) -> None:
    """The property literally named ``upgrade_copy_ja`` feeds the JP
    「HTDT データ更新」 dialog — it must not return English."""
    _create_current_database(tmp_path)
    _stamp_schema_version(tmp_path, NATIVE_SCHEMA_VERSION - 1)
    plan = plan_native_upgrade(tmp_path)
    copy = plan.upgrade_copy_ja
    assert any('ぁ' <= ch <= 'ヿ' or '一' <= ch <= '鿿' for ch in copy)
    assert str(plan.current_schema_version) in copy
    assert str(plan.target_schema_version) in copy


def test_newer_schema_error_carries_versions_for_dialog(tmp_path: Path) -> None:
    """The launch-failure dialog composes JP copy from the versions the
    exception carries — never by parsing the English diagnostic text."""
    _create_newer_database(tmp_path, NATIVE_SCHEMA_VERSION + 2)

    with pytest.raises(IncompatibleNewerSchemaError) as caught:
        execute_native_upgrade(tmp_path)

    assert caught.value.stored_schema_version == NATIVE_SCHEMA_VERSION + 2
    assert caught.value.supported_schema_version == NATIVE_SCHEMA_VERSION


def test_upgrade_failure_recovery_hint_is_japanese(tmp_path: Path) -> None:
    from htdt.native_upgrade import (
        NativeUpgradeQuarantineError,
        upgrade_failure_recovery_ja,
    )

    quarantine = upgrade_failure_recovery_ja(
        NativeUpgradeQuarantineError('simulated')
    )
    generic = upgrade_failure_recovery_ja(NativeUpgradeError('simulated'))

    for text in (quarantine, generic):
        assert any('ぁ' <= ch <= 'ヿ' or '一' <= ch <= '鿿' for ch in text)
    # The quarantined generation's honest path forward: restart re-verifies
    # and the pre-upgrade recovery copy is restorable — surfaced, not just
    # machine-readable on the exception.
    assert '再起動' in quarantine
    assert '復旧用コピー' in quarantine
    assert '変更されていません' in generic
