"""Round 20 (REV20-FIRST): fresh-install journey regressions.

Two verified gaps from exercising launch paths with cleared state:

1. A managed data directory *occupied by a file* (not just missing) crashed
   ``main()`` with a raw ``FileExistsError`` — ``assert_managed_root_available``
   only rejected non-directories for bootstrap-configured roots, so an
   explicit ``--data-dir`` or the default location sailed through until
   ``SingleInstanceGuard.acquire`` blew up mid-mkdir.

2. Quitting while the first-launch automatic backup was mid-copy detached
   the worker past its shutdown budget: the cancellation flag never reached
   ``create_backup``, so the QThread was killed mid-write at interpreter
   teardown leaving a torn ``htdt-backup-*`` staging dir plus Windows
   ``WinError 32`` cleanup noise.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import htdt.native_cad as native_cad
from htdt.automatic_backup import AutomaticBackupScheduler
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.data_relocation import (
    ManagedDataUnavailableError,
    assert_managed_root_available,
)
from htdt.native_backup import (
    DATABASE_NAME,
    BackupCancelledError,
)


# -- gap 1: a file occupying the managed-root path --------------------------


@pytest.mark.parametrize('source', ['explicit', 'bootstrap', 'default'])
def test_managed_root_occupied_by_file_is_rejected(
    tmp_path: Path, source: str
) -> None:
    """Every source fails closed when the resolved root exists as a file —
    directory creation cannot succeed there and the user needs the honest
    managed-data error, not an opaque FileExistsError mid-launch."""
    root = tmp_path / 'occupied'
    root.write_bytes(b'not a directory')

    with pytest.raises(ManagedDataUnavailableError):
        assert_managed_root_available(root, source)


def test_main_reports_failure_when_data_dir_is_a_file(
    tmp_path: Path, monkeypatch
) -> None:
    """GUI launch over a file-occupied root exits 1 through the localized
    launch-failure dialog instead of crashing with a traceback."""
    occupied = tmp_path / 'data'
    occupied.write_bytes(b'not a directory')

    dialogs: list[dict] = []
    monkeypatch.setattr(
        native_cad,
        'report_launch_failure',
        lambda **kwargs: dialogs.append(kwargs),
    )

    rc = native_cad.main(['--data-dir', str(occupied)])

    assert rc == 1
    assert dialogs and dialogs[0]['title'] == 'HTDTのデータディレクトリを開けません'


def test_maintenance_reports_failure_when_data_dir_is_a_file(
    tmp_path: Path, capsys
) -> None:
    """Maintenance mode must not hang on a modal dialog — the failure goes
    to stderr with a nonzero exit."""
    occupied = tmp_path / 'data'
    occupied.write_bytes(b'not a directory')

    rc = native_cad.main(
        ['--data-dir', str(occupied), '--revalidate']
    )

    assert rc == 1
    assert 'データディレクトリが利用不可' in capsys.readouterr().err


# -- gap 2: cooperative cancellation reaches the backup copy loop -----------


def _seeded_scheduler(tmp_path: Path) -> AutomaticBackupScheduler:
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True)
    repository = SceneRepository(data_dir / DATABASE_NAME)
    repository.save(make_empty_scene('doc-1'), parent_revision_id=None)
    return AutomaticBackupScheduler(data_dir)


def test_run_due_aborts_when_cancelled(tmp_path: Path) -> None:
    """A set cancel flag aborts the copy stage before any archive lands —
    no generation, no torn staging residue."""
    scheduler = _seeded_scheduler(tmp_path)
    destination_dir = tmp_path / 'data-backups'

    with pytest.raises(BackupCancelledError):
        scheduler.run_due('periodic', is_cancelled=lambda: True)

    assert list(destination_dir.glob('*.htdt-backup')) == []
    assert list(destination_dir.glob('htdt-backup-*')) == []


def test_run_due_cancel_flipping_mid_run_leaves_no_staging(
    tmp_path: Path,
) -> None:
    """A flag that flips after the first checks still aborts mid-pipeline;
    the TemporaryDirectory staging must unwind cleanly either way."""
    scheduler = _seeded_scheduler(tmp_path)
    checks = {'n': 0}

    def cancel_after_a_few() -> bool:
        checks['n'] += 1
        return checks['n'] > 2

    with pytest.raises(BackupCancelledError):
        scheduler.run_due('periodic', is_cancelled=cancel_after_a_few)

    destination_dir = tmp_path / 'data-backups'
    assert list(destination_dir.glob('*.htdt-backup')) == []
    assert list(destination_dir.glob('htdt-backup-*')) == []


def test_automatic_backup_runner_threads_cancel_flag(
    tmp_path: Path, monkeypatch
) -> None:
    """The runner must hand the worker pool's cancel event to run_due so
    ``pool.shutdown()`` actually reaches the in-flight copy."""
    import htdt.automatic_backup_runner as runner_module
    from htdt.automatic_backup_runner import AutomaticBackupRunner
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    captured: dict[str, object] = {}

    class _CapturingScheduler:
        def __init__(self, _data_dir: Path) -> None:
            pass

        def evaluate(self, _trigger):
            return True, 'due'

        def run_due(self, _trigger, *, is_cancelled=None, **_kwargs):
            captured['is_cancelled'] = is_cancelled
            # Park until the pool cancels: mirrors a long copy stage.
            assert is_cancelled is not None
            while not is_cancelled():
                threading.Event().wait(0.005)
            raise BackupCancelledError('cancelled mid-copy')

    monkeypatch.setattr(
        runner_module, 'AutomaticBackupScheduler', _CapturingScheduler
    )
    runner = AutomaticBackupRunner(tmp_path)
    completed: list = []
    runner.backup_completed.connect(lambda r, e: completed.append((r, e)))
    runner.start()
    # Wait until the job reaches run_due, then close — mirrors quitting
    # while the first-launch backup is mid-copy.
    import time

    deadline = time.monotonic() + 10
    while 'is_cancelled' not in captured and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    runner.shutdown()
    app.processEvents()

    assert captured.get('is_cancelled') is not None
    assert captured['is_cancelled']() is True
    # The cancelled worker's completion is swallowed — nothing surfaces.
    assert completed == []
