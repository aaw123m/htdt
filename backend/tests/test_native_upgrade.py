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
    execute_native_upgrade,
    list_upgrade_events,
    newer_schema_guidance,
    plan_native_upgrade,
    upgrade_snapshot_dir,
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
