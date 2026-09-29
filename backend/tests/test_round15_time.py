"""Round-15 time/clock regression coverage.

Shifted-clock probes over the automatic-backup interval evaluation and
rotation (a moved-backward clock or a foreign state file must never stall
backups or delete the generation just written), plus the display truth of
backup timestamps and the scene-history time column.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta, timezone

from PySide6.QtWidgets import QApplication

import htdt.automatic_backup as automatic_backup_module
from htdt.automatic_backup import (
    AutomaticBackupPolicy,
    AutomaticBackupScheduler,
    backups_dir,
)
from htdt.cad_repository import SceneRevision, SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    make_empty_scene,
    scene_content_hash,
)
from htdt.data_management_ui import _default_backup_name, _format_created_at
from htdt.native_backup import DATABASE_NAME
from htdt.room_history_panel import RoomHistoryPanel


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


def _write_state(scheduler: AutomaticBackupScheduler, last_run: str) -> None:
    state = scheduler._load_state()
    state.update(
        {
            'schema_version': 1,
            'fingerprint': 'stale-fingerprint',
            'last_automatic_at_utc': last_run,
            'classification': 'automatic_periodic',
            'clean_close_pending': False,
        }
    )
    scheduler._save_state(state)


def test_future_last_run_stays_due_with_skew_reason(tmp_path: Path) -> None:
    """A clock moved backward (or a foreign state file) must not stall
    automatic backups for the duration of the skew."""
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    _write_state(scheduler, future)

    should, reason = scheduler.evaluate('periodic')
    assert should is True
    assert 'future' in reason


def test_naive_last_run_is_due_not_a_crash(tmp_path: Path) -> None:
    """Offset-naive state text parses fine but used to raise TypeError on
    the aware subtraction, escaping the 'unreadable' guard entirely."""
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path)
    _write_state(scheduler, '2026-09-01T10:00:00')

    should, reason = scheduler.evaluate('periodic')
    assert should is True
    assert 'unreadable' in reason


def test_run_due_never_prunes_the_generation_it_just_wrote(
    tmp_path: Path, monkeypatch
) -> None:
    """Under a skewed clock the new generation's filename stamp sorts older
    than existing ones — rotation must not delete the archive it made."""
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path, keep_generations=2, keep_daily_generations=0)
    backup_dir = backups_dir(scheduler.data_dir, scheduler.policy)
    backup_dir.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (
            backup_dir
            / f'htdt-backup-automatic_periodic-20260929T0{index}0000Z-{index}.htdt-backup'
        ).write_bytes(b'x')

    class _SkewedDatetime(datetime):
        """Wall clock two days behind the real instant."""

        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(days=2)

    monkeypatch.setattr(automatic_backup_module, 'datetime', _SkewedDatetime)
    result = scheduler.run_due('periodic')
    assert result is not None
    destination, _manifest = result
    # The skewed-stamp archive survived its own rotation; the oldest
    # pre-existing generation was the one pruned.
    assert destination.is_file()
    assert not (
        backup_dir / 'htdt-backup-automatic_periodic-20260929T000000Z-0.htdt-backup'
    ).exists()
    assert len(scheduler.list_generations()) == 3


def test_prune_generations_respects_explicit_protection(tmp_path: Path) -> None:
    _seed_data(tmp_path)
    scheduler = _scheduler(tmp_path, keep_generations=2, keep_daily_generations=0)
    backup_dir = backups_dir(scheduler.data_dir, scheduler.policy)
    backup_dir.mkdir(parents=True, exist_ok=True)
    names = [
        'htdt-backup-automatic_periodic-20260929T100000Z-a1.htdt-backup',
        'htdt-backup-automatic_periodic-20260929T110000Z-b2.htdt-backup',
        'htdt-backup-automatic_periodic-20260920T090000Z-c3.htdt-backup',
    ]
    for name in names:
        (backup_dir / name).write_bytes(b'x')
    protected = backup_dir / names[2]

    removed = scheduler.prune_generations(protected=(protected,))
    assert protected.exists()
    assert protected not in removed
    assert set(removed) == set()


def test_default_backup_name_uses_utc_stamp_with_marker() -> None:
    name = _default_backup_name(
        datetime(2026, 9, 29, 13, 5, tzinfo=timezone.utc)
    )
    assert name == 'HTDT-backup-2026-09-29-1305Z.htdt-backup'


def test_format_created_at_labels_utc_not_local() -> None:
    assert _format_created_at('2026-09-18T15:00:00+00:00') == '2026/09/18 15:00 UTC'
    assert _format_created_at('2026-09-18T15:00:00Z') == '2026/09/18 15:00 UTC'
    # An offset-bearing value converts to the same UTC instant.
    assert _format_created_at('2026-09-18T23:00:00+09:00') == '2026/09/18 14:00 UTC'
    assert _format_created_at('not-a-date') == 'not-a-date'


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_history_timestamp_cell_keeps_the_zone_marker() -> None:
    _app()
    panel = RoomHistoryPanel()
    document = SceneDocument(
        document_id='doc-1',
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='sp-1',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=0.75, z_m=1.05),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
            ),
        ),
    )
    revision = SceneRevision(
        revision_id='rev-1',
        document_id='doc-1',
        parent_revision_id=None,
        created_at_utc='2026-09-26T10:00:00.123456+00:00',
        content_hash=scene_content_hash(document),
        document=document,
        detached=False,
    )
    panel.sync_revisions((revision,), head_revision_id='rev-1', labels={})

    cell = panel.tree.topLevelItem(0).text(0)
    assert '2026-09-26 10:00:00' in cell
    assert '+00:00' in cell
    assert '.123456' not in cell
