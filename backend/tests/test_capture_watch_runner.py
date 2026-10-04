"""REV42 — opt-in .htdtcapture drop-folder watch.

Mirrors the REW watch-dir contract: baseline files are never staged, a
drop must settle for two scans, and delivery routes through
``route_capture_intent`` — ingest + inbox stage, never evidence
promotion.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
import htdt.capture_watch_runner as watch_module
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.capture_inbox import CaptureInboxRepository  # noqa: E402
from htdt.capture_ingestion_transaction import (  # noqa: E402
    CaptureIngestionRepository,
)
from htdt.capture_watch_runner import (  # noqa: E402
    CaptureWatchRunner,
    WATCH_ARRIVAL_SOURCE,
    scan_capture_watch_dir,
)


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _pump(app: QApplication, predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return predicate()


class _Prefs:
    def __init__(self, watch_dir: str = '') -> None:
        self.watch_dir = watch_dir

    def get(self, key: str) -> object:
        if key == 'integrations.capture_watch_dir':
            return self.watch_dir
        raise KeyError(key)


def _write_bundle_zip(dest_dir: Path, name: str) -> Path:
    bundle_dir, _ = support.write_bundle(
        dest_dir / f'{name}-src', support.default_file_specs()
    )
    zip_path = dest_dir / f'{name}.htdtcapture'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bundle_dir.rglob('*')):
            if path.is_file():
                zf.write(path, path.relative_to(bundle_dir).as_posix())
    return zip_path


# -- scanner contract ------------------------------------------------------


def test_scanner_baseline_never_delivers_preexisting(tmp_path: Path) -> None:
    watch = tmp_path / 'watch'
    watch.mkdir()
    _write_bundle_zip(watch, 'old')
    seen: dict = {}
    pending: dict = {}
    assert scan_capture_watch_dir(watch, seen, pending) == []
    # A file that then changes IS a new drop — signature differs.
    z = watch / 'old.htdtcapture'
    z.write_bytes(z.read_bytes() + b'x')
    assert scan_capture_watch_dir(watch, seen, pending) == []  # pending
    assert scan_capture_watch_dir(watch, seen, pending) == [z]


def test_scanner_delivers_only_after_signature_settles(tmp_path: Path) -> None:
    watch = tmp_path / 'watch'
    watch.mkdir()
    seen: dict = {}
    pending: dict = {}
    scan_capture_watch_dir(watch, seen, pending)  # baseline
    drop = watch / 'new.htdtcapture'
    drop.write_bytes(b'partial')
    assert scan_capture_watch_dir(watch, seen, pending) == []
    # Still being written — signature changed again, timer restarts.
    drop.write_bytes(b'partial-more')
    assert scan_capture_watch_dir(watch, seen, pending) == []
    # Settled across two scans → delivered.
    assert scan_capture_watch_dir(watch, seen, pending) == [drop]


def test_scanner_ignores_other_suffixes_and_redelivers_recreated(
    tmp_path: Path,
) -> None:
    watch = tmp_path / 'watch'
    watch.mkdir()
    seen: dict = {}
    pending: dict = {}
    scan_capture_watch_dir(watch, seen, pending)  # baseline
    other = watch / 'notes.txt'
    other.write_text('hello')
    for _ in range(3):
        assert scan_capture_watch_dir(watch, seen, pending) == []
    # A delivered file deleted then recreated re-enters as a new drop.
    drop = watch / 'again.htdtcapture'
    drop.write_bytes(b'x')
    scan_capture_watch_dir(watch, seen, pending)
    assert scan_capture_watch_dir(watch, seen, pending) == [drop]
    drop.unlink()
    scan_capture_watch_dir(watch, seen, pending)
    # A different size forces a new signature — a same-size rewrite inside
    # one filesystem-timestamp tick is indistinguishable to (mtime, size).
    drop.write_bytes(b'yz')
    scan_capture_watch_dir(watch, seen, pending)
    assert scan_capture_watch_dir(watch, seen, pending) == [drop]


# -- runner end-to-end -----------------------------------------------------


def test_runner_stages_dropped_bundle_into_inbox(tmp_path: Path) -> None:
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(
        repository, _Prefs(str(watch)), interval_ms=50
    )
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    # Baseline is established on the first scan before the drop — wait
    # for the sentinel or the drop itself would be baselined away.
    assert _pump(app, lambda: bool(runner._seen))
    _write_bundle_zip(watch, 'drop1')
    assert _pump(app, lambda: bool(batches))
    runner.shutdown()

    delivered = [
        (p, r, e) for batch in batches for p, r, e in batch
        if r is not None and r.outcome == 'staged_for_review'
    ]
    assert len(delivered) == 1
    path, result, _ = delivered[0]
    assert path.name == 'drop1.htdtcapture'

    inbox = CaptureInboxRepository(
        repository, CaptureIngestionRepository(repository)
    )
    items = inbox.list_items()
    assert len(items) == 1
    assert items[0].inbox_item_id == result.inbox_item_id
    # Provenance is honest: the watch lane, not a document-open.
    assert items[0].arrival_source == WATCH_ARRIVAL_SOURCE


def test_runner_never_promotes_and_rejects_junk(tmp_path: Path) -> None:
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(
        repository, _Prefs(str(watch)), interval_ms=50
    )
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))
    junk = watch / 'junk.htdtcapture'
    junk.write_bytes(b'not a bundle at all')
    assert _pump(app, lambda: bool(batches))
    runner.shutdown()
    outcomes = [r.outcome for batch in batches for _, r, _ in batch if r]
    assert outcomes and all(o == 'failed' for o in outcomes)
    # Nothing staged — a junk drop must not become an inbox row.
    inbox = CaptureInboxRepository(
        repository, CaptureIngestionRepository(repository)
    )
    assert inbox.list_items() == ()


def test_runner_retries_failed_route_and_stages_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REV43-SEAMS: a transient routing failure must not drop the bundle.

    ``scan_capture_watch_dir`` marks a file seen on delivery, so a
    raise/failed route without a requeue loses the drop forever — even
    an app restart cannot recover it because the baseline pass marks
    every pre-existing file seen.
    """
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    import htdt.capture_watch_runner as watch_module
    from htdt.launch_router import route_capture_intent as real_route

    attempts = {'count': 0}

    def flaky_route(intent, *, repository, arrival_source):
        attempts['count'] += 1
        if attempts['count'] < 3:
            raise RuntimeError('simulated transient failure')
        return real_route(
            intent, repository=repository, arrival_source=arrival_source
        )

    monkeypatch.setattr(watch_module, 'route_capture_intent', flaky_route)
    runner = CaptureWatchRunner(
        repository, _Prefs(str(watch)), interval_ms=50
    )
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))
    _write_bundle_zip(watch, 'flaky')
    assert _pump(app, lambda: attempts['count'] >= 3)
    runner.shutdown()

    # The two failures surfaced, then the retried route staged the bundle.
    inbox = CaptureInboxRepository(
        repository, CaptureIngestionRepository(repository)
    )
    items = inbox.list_items()
    assert len(items) == 1
    assert items[0].arrival_source == WATCH_ARRIVAL_SOURCE


def test_runner_gives_up_after_retry_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REV43-SEAMS: a wedged drop stops cycling after the attempt cap."""
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    import htdt.capture_watch_runner as watch_module

    attempts = {'count': 0}

    def always_fail(intent, *, repository, arrival_source):
        attempts['count'] += 1
        raise RuntimeError('permanent failure')

    monkeypatch.setattr(watch_module, 'route_capture_intent', always_fail)
    runner = CaptureWatchRunner(
        repository, _Prefs(str(watch)), interval_ms=40
    )
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))
    drop = watch / 'wedged.htdtcapture'
    drop.write_bytes(b'junk that always fails')
    assert _pump(
        app,
        lambda: attempts['count'] >= watch_module._ROUTE_MAX_ATTEMPTS,
    )
    # Past the cap the seen marker stays: no more routing attempts even
    # after several more scans.
    settled = time.monotonic() + 0.5
    while time.monotonic() < settled:
        app.processEvents()
        time.sleep(0.01)
    assert attempts['count'] == watch_module._ROUTE_MAX_ATTEMPTS
    assert str(drop) in runner._seen
    runner.shutdown()

    inbox = CaptureInboxRepository(
        repository, CaptureIngestionRepository(repository)
    )
    assert inbox.list_items() == ()


def test_runner_rewrite_after_wedge_redelivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REV43-SEAMS: a wedged file still re-routes once when rewritten."""
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    import htdt.capture_watch_runner as watch_module

    attempts = {'count': 0}

    def always_fail(intent, *, repository, arrival_source):
        attempts['count'] += 1
        raise RuntimeError('permanent failure')

    monkeypatch.setattr(watch_module, 'route_capture_intent', always_fail)
    runner = CaptureWatchRunner(
        repository, _Prefs(str(watch)), interval_ms=40
    )
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))
    drop = watch / 'rewritten.htdtcapture'
    drop.write_bytes(b'version one')
    cap = watch_module._ROUTE_MAX_ATTEMPTS
    assert _pump(app, lambda: attempts['count'] >= cap)
    # Rewriting changes the (mtime, size) signature → a fresh delivery;
    # the new failure leaves it wedged again after one more attempt.
    drop.write_bytes(b'version two, longer bytes')
    assert _pump(app, lambda: attempts['count'] >= cap + 1)
    settled = time.monotonic() + 0.5
    while time.monotonic() < settled:
        app.processEvents()
        time.sleep(0.01)
    assert attempts['count'] == cap + 1
    runner.shutdown()


def test_runner_disabled_watch_dir_stays_idle(tmp_path: Path) -> None:
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(repository, _Prefs(''), interval_ms=30)
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    _write_bundle_zip(watch, 'never')
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    runner.shutdown()
    assert batches == []


def test_runner_missing_dir_is_not_an_error(tmp_path: Path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(
        repository, _Prefs(str(tmp_path / 'nope')), interval_ms=30
    )
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    runner.shutdown()
    assert batches == []


def test_runner_recovers_when_scan_job_wedges(tmp_path: Path) -> None:
    """A scan that outlives _STALL_BUDGET_S must not mute the lane forever.

    ``_in_flight`` clears only via the worker's on_finished — a hung route
    would otherwise absorb every later tick with zero diagnostics. The
    stall guard cancels the job, bumps the generation (so the wedged
    worker cannot write runner state) and releases the latch; the next
    tick then routes fresh drops again.
    """
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(
        repository, _Prefs(str(watch)), interval_ms=30
    )
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))

    # Simulate a wedged in-flight job past the stall budget.
    runner._in_flight = True
    runner._in_flight_since = (
        time.monotonic() - watch_module._STALL_BUDGET_S - 1.0
    )
    _write_bundle_zip(watch, 'drop-wedged')
    # The wedged latch must release and a later tick must route the drop.
    assert _pump(app, lambda: bool(batches), timeout=15.0)
    runner.shutdown()
    staged = [
        p.name for batch in batches for p, r, _ in batch
        if r is not None and r.outcome == 'staged_for_review'
    ]
    assert 'drop-wedged.htdtcapture' in staged
