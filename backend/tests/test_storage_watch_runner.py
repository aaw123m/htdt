"""REV42 — scheduled read-only storage integrity scan.

The manual inventory button's report is recomputed on a timer while the
session is alive; only reportable results (missing referenced files or
reclaimable bulk) surface an Activity entry. Deletion stays manual.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

import htdt.storage_watch_runner as runner_module  # noqa: E402
from htdt.storage_watch_runner import StorageWatchRunner  # noqa: E402


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _drain(app: QApplication, done: list, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not done and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


class _Prefs:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def get(self, key: str) -> object:
        if key == 'maintenance.storage_watch_enabled':
            return self.enabled
        raise KeyError(key)


class _Report:
    def __init__(self, missing=(), reclaimable_bytes=0) -> None:
        self.missing_referenced = tuple(missing)
        self.reclaimable_bytes = reclaimable_bytes


def test_unremarkable_scan_stays_silent(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    monkeypatch.setattr(
        runner_module,
        'plan_storage_gc',
        lambda _dir, is_cancelled=None: _Report(),
    )
    runner = StorageWatchRunner(
        tmp_path, _Prefs(), interval_ms=20, reportable_bytes=1024
    )
    notices: list = []
    runner.integrity_notice.connect(notices.append)
    runner.start()
    # Let several ticks run — nothing reportable, so nothing may emit.
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    runner.shutdown()
    assert notices == []


def test_reportable_scan_emits_once_per_report(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app()
    report = _Report(missing=('missing-1',), reclaimable_bytes=10 * 1024 * 1024)
    monkeypatch.setattr(
        runner_module,
        'plan_storage_gc',
        lambda _dir, is_cancelled=None: report,
    )
    runner = StorageWatchRunner(
        tmp_path, _Prefs(), interval_ms=20, reportable_bytes=1024
    )
    notices: list = []
    runner.integrity_notice.connect(lambda r: notices.append(r))
    runner.start()
    deadline = time.monotonic() + 30
    while not notices and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    runner.shutdown()
    assert notices == [report]


def test_disabled_preference_never_scans(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app()
    calls: list = []
    monkeypatch.setattr(
        runner_module,
        'plan_storage_gc',
        lambda _dir, is_cancelled=None: calls.append(1) or _Report(
            missing=('x',)
        ),
    )
    runner = StorageWatchRunner(
        tmp_path, _Prefs(enabled=False), interval_ms=20
    )
    notices: list = []
    runner.integrity_notice.connect(notices.append)
    runner.start()
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    runner.shutdown()
    assert calls == []
    assert notices == []


def test_scan_failure_surfaces_scan_failed(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app()
    def _boom(_dir, is_cancelled=None):
        raise OSError('unreadable data dir')

    monkeypatch.setattr(runner_module, 'plan_storage_gc', _boom)
    runner = StorageWatchRunner(tmp_path, _Prefs(), interval_ms=20)
    failures: list = []
    runner.scan_failed.connect(failures.append)
    runner.start()
    deadline = time.monotonic() + 30
    while not failures and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    runner.shutdown()
    assert failures and 'unreadable' in str(failures[0])


def test_scans_never_overlap(tmp_path: Path, monkeypatch) -> None:
    import threading

    app = _app()
    gate = threading.Event()
    state = {'in_flight': 0, 'max': 0}

    def _slow(_dir, is_cancelled=None):
        state['in_flight'] += 1
        state['max'] = max(state['max'], state['in_flight'])
        try:
            gate.wait(10)
        finally:
            state['in_flight'] -= 1
        return _Report()

    monkeypatch.setattr(runner_module, 'plan_storage_gc', _slow)
    runner = StorageWatchRunner(tmp_path, _Prefs(), interval_ms=15)
    runner.start()
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    gate.set()
    deadline = time.monotonic() + 30
    while state['in_flight'] and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    runner.shutdown()
    assert state['max'] == 1
