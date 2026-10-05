from __future__ import annotations

import os
from pathlib import Path
import time
from zipfile import ZipFile

import pytest

from htdt.automatic_backup import (
    AutomaticBackupPolicy,
    AutomaticBackupScheduler,
    backups_dir,
    managed_data_fingerprint,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.native_backup import DATABASE_NAME


def _scheduler(tmp_path: Path, **policy_kwargs) -> AutomaticBackupScheduler:
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        interval_hours=24.0,
        keep_generations=5,
        keep_daily_generations=14,
    )
    kwargs.update(policy_kwargs)
    policy = AutomaticBackupPolicy(**kwargs)
    return AutomaticBackupScheduler(data_dir, policy)


def _seed_data(tmp_path: Path, document_id: str = 'doc-1') -> SceneRepository:
    data_dir = tmp_path / 'data'
    repository = SceneRepository(data_dir / DATABASE_NAME)
    repository.save(make_empty_scene(document_id), parent_revision_id=None)
    return repository


def test_fingerprint_changes_when_managed_data_changes(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True)
    empty = managed_data_fingerprint(data_dir)
    _seed_data(tmp_path)
    seeded = managed_data_fingerprint(data_dir)
    assert empty != seeded

    repository = SceneRepository(data_dir / DATABASE_NAME)
    repository.save(make_empty_scene('doc-2'), parent_revision_id=None)
    assert managed_data_fingerprint(data_dir) != seeded


def test_generations_live_outside_the_data_dir(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path)
    destination_dir = backups_dir(scheduler.data_dir, scheduler.policy)
    assert destination_dir == tmp_path / 'data-backups'

    result = scheduler.run_due('periodic')
    assert result is not None
    path, manifest = result
    assert path.is_file()
    assert path.parent == destination_dir
    assert not str(path).startswith(str(scheduler.data_dir) + os.sep)
    # The generation is a real, validated archive.
    with ZipFile(path) as archive:
        assert DATABASE_NAME in archive.namelist()
    assert manifest.schema_version == 1


def test_no_backup_when_nothing_changed(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path)
    assert scheduler.run_due('periodic') is not None
    # Immediate second periodic run: unchanged data → skip.
    assert scheduler.run_due('periodic') is None
    assert len(scheduler.list_generations()) == 1


def test_clean_close_backs_up_only_changed_data(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path)
    # No prior backup → changed → due.
    assert scheduler.run_due('clean_close') is not None
    # Unchanged → skipped.
    assert scheduler.run_due('clean_close') is None


def test_pre_destructive_always_backs_up(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path)
    first = scheduler.run_due('pre_destructive')
    assert first is not None
    # Even unchanged data gets a pre-destructive safety generation.
    second = scheduler.run_due('pre_destructive')
    assert second is not None
    names = [p.name for p in scheduler.list_generations()]
    assert all('pre_destructive' in name for name in names)


def test_clean_close_respects_interval(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path, interval_hours=24.0)
    assert scheduler.run_due('clean_close') is not None
    # Changed data but inside the interval: clean-close never forces an
    # extra generation (#755).
    _seed_data(tmp_path, document_id='doc-2')
    assert scheduler.run_due('clean_close') is None


def test_record_clean_close_defers_due_backup(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path)
    scheduler.record_clean_close()
    state = scheduler._load_state()
    assert state['clean_close_pending'] is True
    assert 'last_clean_close_at_utc' in state
    # The next eligible periodic tick performs the deferred generation.
    result = scheduler.run_due('periodic')
    assert result is not None
    assert 'automatic_periodic' in result[0].name
    assert scheduler._load_state()['clean_close_pending'] is False


def test_rotation_never_deletes_safety_generations(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path, keep_generations=2)
    backup_dir = backups_dir(scheduler.data_dir, scheduler.policy)
    backup_dir.mkdir(parents=True, exist_ok=True)
    safety_names = [
        'htdt-backup-pre_restore-20260101T000000Z-aa.htdt-backup',
        'htdt-backup-manual-20260101T010000Z-bb.htdt-backup',
        'htdt-backup-pre_upgrade-20260101T020000Z-cc.htdt-backup',
        'htdt-backup-pre_destructive-20260101T030000Z-dd.htdt-backup',
        # Unknown-classification archive: unrecognised = protected.
        'htdt-backup-unknownkind-20260101T040000Z-ee.htdt-backup',
    ]
    for name in safety_names:
        (backup_dir / name).write_bytes(b'x')
    # Six same-day automatic generations: only the newest 2 survive.
    for index in range(6):
        (
            backup_dir
            / f'htdt-backup-automatic_periodic-20260115T0{index}0000Z-{index}.htdt-backup'
        ).write_bytes(b'x')
    scheduler.prune_generations()
    remaining = {p.name for p in scheduler.list_generations()}
    assert remaining == set(safety_names) | {
        'htdt-backup-automatic_periodic-20260115T050000Z-5.htdt-backup',
        'htdt-backup-automatic_periodic-20260115T040000Z-4.htdt-backup',
    }


def test_safety_generation_does_not_reset_interval(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path, interval_hours=24.0)
    scheduler.run_due('pre_destructive')
    _seed_data(tmp_path, document_id='doc-2')
    # The safety archive covered the old fingerprint but did not advance
    # the routine automatic clock — a never-automatic dir is still due.
    should, reason = scheduler.evaluate('periodic')
    assert should is True
    assert 'no automatic backup has ever run' in reason
    result = scheduler.run_due('periodic')
    assert result is not None
    # Data unchanged since that generation: pre-destructive still runs.
    assert scheduler.run_due('pre_destructive') is not None


def test_generation_records_are_chronological(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path)
    backup_dir = backups_dir(scheduler.data_dir, scheduler.policy)
    backup_dir.mkdir(parents=True, exist_ok=True)
    # uuid suffixes would invert plain name ordering; timestamps must win.
    (backup_dir / 'htdt-backup-automatic_periodic-20260102T000000Z-zz.htdt-backup').write_bytes(b'x')
    (backup_dir / 'htdt-backup-automatic_periodic-20260103T000000Z-aa.htdt-backup').write_bytes(b'x')
    records = scheduler.generation_records()
    assert [r.path.name for r in records] == [
        'htdt-backup-automatic_periodic-20260103T000000Z-aa.htdt-backup',
        'htdt-backup-automatic_periodic-20260102T000000Z-zz.htdt-backup',
    ]
    assert all(r.classification == 'automatic_periodic' for r in records)


def test_classification_in_generation_names(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path)
    periodic = scheduler.run_due('periodic')
    assert 'automatic_periodic' in periodic[0].name
    manual = scheduler.run_due('periodic', classification='manual', force=True)
    assert 'manual' in manual[0].name


def test_rotation_keeps_bounded_generations(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path, keep_generations=3)
    backup_dir = backups_dir(scheduler.data_dir, scheduler.policy)
    backup_dir.mkdir(parents=True, exist_ok=True)
    for index in range(8):
        (
            backup_dir / f'htdt-backup-automatic_periodic-2026010{index}T000000Z-{index}.htdt-backup'
        ).write_bytes(b'x')
    scheduler.prune_generations()
    remaining = scheduler.list_generations()
    # 3 newest always kept; each of the older days keeps one representative
    # (distinct days) up to keep_daily_generations.
    assert len(remaining) <= 3 + scheduler.policy.keep_daily_generations
    names = [p.name for p in remaining]
    assert 'htdt-backup-automatic_periodic-20260107T000000Z-7.htdt-backup' in names
    assert 'htdt-backup-automatic_periodic-20260106T000000Z-6.htdt-backup' in names
    assert 'htdt-backup-automatic_periodic-20260105T000000Z-5.htdt-backup' in names


def test_disabled_policy_skips_all_triggers(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path, enabled=False)
    assert scheduler.run_due('periodic') is None
    assert scheduler.run_due('clean_close') is None
    assert scheduler.run_due('pre_destructive') is None


def test_empty_data_dir_produces_no_backup(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    assert scheduler.run_due('periodic') is None
    assert scheduler.run_due('pre_destructive') is None
    assert scheduler.list_generations() == ()


def test_policy_roundtrip(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True)
    scheduler = AutomaticBackupScheduler(data_dir)
    policy = AutomaticBackupPolicy(
        interval_hours=6.0,
        keep_generations=7,
        keep_daily_generations=30,
    )
    scheduler.save_policy(policy)
    reloaded = AutomaticBackupScheduler(data_dir)
    assert reloaded.policy == policy


def test_list_restorable_backups_merges_generations_and_upgrade_snapshots(
    tmp_path: Path,
) -> None:
    """Round9 #11: the recovery surface and the data-management page list
    automatic generations AND pre-upgrade recovery copies together,
    newest encoded stamp first."""

    from htdt.automatic_backup import list_restorable_backups
    from htdt.native_upgrade import UPGRADE_RECOVERY_DIRNAME

    _seed_data(tmp_path)
    data_dir = tmp_path / 'data'
    scheduler = _scheduler(tmp_path)
    scheduler.run_due('periodic')
    (generation,) = scheduler.list_generations()

    snapshot_dir = data_dir / UPGRADE_RECOVERY_DIRNAME
    snapshot_dir.mkdir(parents=True)
    # A newer stamp than any generation: it must sort first.
    snapshot = snapshot_dir / (
        'pre-upgrade-v2-to-v3-20990101T000000Z-abcdef12.htdt-backup'
    )
    snapshot.write_bytes(b'snapshot')

    listed = list_restorable_backups(data_dir)
    assert listed[0] == snapshot
    assert generation in listed

    # Missing directories / empty dirs simply yield fewer candidates.
    for other in ('data-empty',):
        empty_dir = tmp_path / other
        empty_dir.mkdir()
        assert list_restorable_backups(empty_dir) == ()


def test_fingerprint_changes_when_auxiliary_component_changes(
    tmp_path: Path,
) -> None:
    """Auxiliary authority files (e.g. commissioning-plans.json) ride
    inside every backup archive — an aux-only edit is real managed state
    and must invalidate the fingerprint on its own, otherwise periodic
    evaluation never fires on aux-only changes (REV52)."""
    data_dir = tmp_path / 'data'
    _seed_data(tmp_path)
    before = managed_data_fingerprint(data_dir)

    aux = data_dir / 'commissioning-plans.json'
    aux.write_text('{"plans": [{"id": "p-1"}]}', encoding='utf-8')
    assert managed_data_fingerprint(data_dir) != before

    after = managed_data_fingerprint(data_dir)
    aux.write_text('{"plans": [{"id": "p-2"}, {"id": "p-3"}]}', encoding='utf-8')
    assert managed_data_fingerprint(data_dir) != after
