"""Round 8: the in-app periodic automatic-backup tick (#755)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import htdt.automatic_backup_runner as runner_module
from htdt.automatic_backup_runner import AutomaticBackupRunner
from htdt.cad_repository import SceneRepository


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _drain(app: QApplication, done: list, *, limit: int = 2000) -> None:
    import time

    deadline = time.monotonic() + 30
    while not done and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


class _NotDueScheduler:
    def __init__(self, _data_dir: Path) -> None:
        self.ran = False

    def evaluate(self, _trigger):
        return False, 'within interval'

    def run_due(self, _trigger, **_kwargs):
        raise AssertionError('run_due must not run when not due')


class _DueScheduler:
    def __init__(self, _data_dir: Path) -> None:
        pass

    def evaluate(self, _trigger):
        return True, 'no automatic backup has ever run'

    def run_due(self, _trigger, **_kwargs):
        return Path('C:/backups/gen.htdt-backup'), SimpleNamespace()


class _FailingScheduler(_DueScheduler):
    def run_due(self, _trigger, **_kwargs):
        raise RuntimeError('disk full')


def test_runner_reports_completion_when_due(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    monkeypatch.setattr(
        runner_module, 'AutomaticBackupScheduler', _DueScheduler
    )
    runner = AutomaticBackupRunner(tmp_path)
    done: list = []
    runner.backup_completed.connect(lambda r, e: done.append((r, e)))
    assert runner.start() is True
    _drain(app, done)
    assert done and done[0][0] is not None and done[0][1] is None
    # One shot per runner — a second start is refused.
    assert runner.start() is False
    runner.shutdown()


def test_runner_silent_when_not_due(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    monkeypatch.setattr(
        runner_module, 'AutomaticBackupScheduler', _NotDueScheduler
    )
    runner = AutomaticBackupRunner(tmp_path)
    started: list = []
    done: list = []
    runner.backup_started.connect(lambda: started.append(1))
    runner.backup_completed.connect(lambda r, e: done.append((r, e)))
    runner.start()
    _drain(app, done)
    assert started == []
    assert done and done[0] == (None, None)
    runner.shutdown()


def test_runner_surfaces_failure(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    monkeypatch.setattr(
        runner_module, 'AutomaticBackupScheduler', _FailingScheduler
    )
    runner = AutomaticBackupRunner(tmp_path)
    done: list = []
    runner.backup_completed.connect(lambda r, e: done.append((r, e)))
    runner.start()
    _drain(app, done)
    assert done and done[0][0] is None and 'disk full' in str(done[0][1])
    runner.shutdown()


class _CountingScheduler:
    """Counts evaluations across every instance the runner builds."""

    evaluate_calls = 0
    run_calls = 0

    def __init__(self, _data_dir: Path) -> None:
        pass

    def evaluate(self, _trigger):
        type(self).evaluate_calls += 1
        return False, 'within interval'

    def run_due(self, _trigger, **_kwargs):
        type(self).run_calls += 1
        raise AssertionError('run_due must not run when not due')


class _SlowDueScheduler:
    """Due, but each evaluation sits on an event so overlap is observable."""

    in_flight = 0
    max_in_flight = 0
    gate = None

    def __init__(self, _data_dir: Path) -> None:
        pass

    def evaluate(self, _trigger):
        cls = type(self)
        cls.in_flight += 1
        cls.max_in_flight = max(cls.max_in_flight, cls.in_flight)
        try:
            if cls.gate is not None:
                cls.gate.wait(10)
        finally:
            cls.in_flight -= 1
        return False, 'within interval'

    def run_due(self, _trigger, **_kwargs):
        raise AssertionError('run_due must not run when not due')


def test_runner_re_drives_the_check_while_session_stays_open(
    tmp_path: Path, monkeypatch
) -> None:
    """REV42: a long-lived session must keep evaluating, not just once per launch."""
    app = _app()
    _CountingScheduler.evaluate_calls = 0
    monkeypatch.setattr(
        runner_module, 'AutomaticBackupScheduler', _CountingScheduler
    )
    runner = AutomaticBackupRunner(tmp_path, periodic_interval_ms=25)
    runner.start()
    done: list = []
    import time

    deadline = time.monotonic() + 30
    while (
        _CountingScheduler.evaluate_calls < 3 and time.monotonic() < deadline
    ):
        app.processEvents()
        time.sleep(0.01)
    runner.shutdown()
    assert _CountingScheduler.evaluate_calls >= 3
    assert _CountingScheduler.run_calls == 0


def test_runner_never_overlaps_an_in_flight_check(
    tmp_path: Path, monkeypatch
) -> None:
    """REV42: a tick landing mid-job must be absorbed, not queued on top."""
    import threading
    import time

    app = _app()
    _SlowDueScheduler.in_flight = 0
    _SlowDueScheduler.max_in_flight = 0
    _SlowDueScheduler.gate = threading.Event()
    monkeypatch.setattr(
        runner_module, 'AutomaticBackupScheduler', _SlowDueScheduler
    )
    runner = AutomaticBackupRunner(tmp_path, periodic_interval_ms=20)
    runner.start()
    # Let several ticks fire against the blocked first evaluation.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    _SlowDueScheduler.gate.set()
    deadline = time.monotonic() + 30
    while _SlowDueScheduler.in_flight and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    runner.shutdown()
    assert _SlowDueScheduler.max_in_flight == 1


def test_runner_stops_ticking_after_shutdown(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app()
    _CountingScheduler.evaluate_calls = 0
    monkeypatch.setattr(
        runner_module, 'AutomaticBackupScheduler', _CountingScheduler
    )
    runner = AutomaticBackupRunner(tmp_path, periodic_interval_ms=20)
    runner.start()
    runner.shutdown()
    import time

    app.processEvents()
    time.sleep(0.2)
    app.processEvents()
    assert _CountingScheduler.evaluate_calls <= 1


def test_runner_creates_a_real_generation_when_due(tmp_path: Path) -> None:
    """End-to-end: a real data dir with no backups must get one."""
    app = _app()
    data_dir = tmp_path / 'data'
    SceneRepository(data_dir / 'cad-scenes.sqlite3')
    runner = AutomaticBackupRunner(data_dir)
    done: list = []
    runner.backup_completed.connect(lambda r, e: done.append((r, e)))
    runner.start()
    _drain(app, done)
    assert done, 'backup run never completed'
    result, error = done[0]
    assert error is None
    assert result is not None
    path, manifest = result
    assert Path(path).is_file()
    runner.shutdown()
