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
