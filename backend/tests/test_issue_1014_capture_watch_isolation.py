"""#1014 / #1021 — CaptureWatchRunner epoch isolation.

When ``_tick`` abandons a stalled scan job at ``_STALL_BUDGET_S`` it bumps
``_job_generation`` and starts a replacement immediately; the pool only
*detaches* the old QThread — it may still be mid-scan or mid-route.
Before this fix the abandoned worker shared the runner's mutable
``_seen``/``_pending``/``_route_failures`` dicts and
``scan_capture_watch_dir`` wrote them before any generation check, so a
worker resuming late could corrupt the new epoch's baseline, double-route
a drop, or bleed markers across watch roots.

The fix gives every job its own state dicts: the worker computes against
snapshots and returns a ``_WatchScanResult``; the runner publishes the
next-state on the owner thread only while ``(root, generation)`` still
names the live epoch. These tests gate the real scan/route calls on
``threading.Event`` barriers so the old worker provably resumes *after*
the new epoch started.
"""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

import htdt.capture_watch_runner as watch_module  # noqa: E402
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.capture_inbox import CaptureInboxRepository  # noqa: E402
from htdt.capture_ingestion_transaction import (  # noqa: E402
    CaptureIngestionRepository,
)
from htdt.capture_watch_runner import (  # noqa: E402
    CaptureWatchRunner,
    _WatchScanResult,
)
from htdt.native_worker import lingering_thread_count  # noqa: E402


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
    import zipfile

    zip_path = dest_dir / f'{name}.htdtcapture'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bundle_dir.rglob('*')):
            if path.is_file():
                zf.write(path, path.relative_to(bundle_dir).as_posix())
    return zip_path


def _force_stall(runner: CaptureWatchRunner) -> None:
    """Backdate the in-flight latch so the next tick abandons the job."""
    runner._in_flight_since = (
        time.monotonic() - watch_module._STALL_BUDGET_S - 1.0
    )


_POISON = '\x00poisoned-by-abandoned-job'


def _install_gated_scan(
    monkeypatch: pytest.MonkeyPatch, calls: dict
) -> dict:
    """Block scan call #1 (the doomed job) until the test releases it.

    Returns a state dict exposing the exact ``seen``/``pending`` objects
    the blocked worker was handed — the test asserts they are job-local
    copies, never the runner's dicts — plus events marking the block and
    the blocked job's completion.
    """
    state = {
        'started': threading.Event(),
        'release': threading.Event(),
        'done': threading.Event(),
        'seen': None,
        'pending': None,
    }
    real_scan = watch_module.scan_capture_watch_dir

    def gated_scan(directory, seen, pending, skipped_out=None):
        calls['n'] += 1
        if calls['n'] == 1:
            state['seen'] = seen
            state['pending'] = pending
            state['started'].set()
            state['release'].wait(timeout=30)
            result = real_scan(
                directory, seen, pending, skipped_out=skipped_out
            )
            # The stale job's writes must stay job-local: poison its own
            # dict AFTER the real scan (which evicts unknown keys), so
            # any leak into the runner's epoch is detectable.
            seen[_POISON] = (0, 0)
            state['done'].set()
            return result
        return real_scan(directory, seen, pending, skipped_out=skipped_out)

    monkeypatch.setattr(
        watch_module, 'scan_capture_watch_dir', gated_scan
    )
    return state


def test_stalled_worker_resuming_late_cannot_write_new_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Barrier-controlled race: old worker resumes AFTER the new epoch.

    The abandoned job wakes up, writes its (poisoned) scan state and
    returns — the live epoch's dicts must be exactly what the replacement
    jobs published, with zero cross-writes and zero extra routes.
    """
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(
        repository, _Prefs(str(watch)), interval_ms=40
    )
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))
    lingering_before = lingering_thread_count()

    calls = {'n': 0}
    gated = _install_gated_scan(monkeypatch, calls)
    route_calls = {'n': 0}
    real_route = watch_module.route_capture_intent

    def counting_route(intent, *, repository, arrival_source):
        route_calls['n'] += 1
        return real_route(
            intent, repository=repository, arrival_source=arrival_source
        )

    monkeypatch.setattr(
        watch_module, 'route_capture_intent', counting_route
    )

    drop = _write_bundle_zip(watch, 'racedrop')
    # The first gated call is the job that will be abandoned. Wait via
    # _pump — the timer that starts each job needs the Qt event loop.
    assert _pump(app, lambda: gated['started'].is_set())
    _force_stall(runner)
    # The stall-release tick cancels the old job and starts a new epoch;
    # the pool detaches the still-blocked thread.
    assert _pump(app, lambda: calls['n'] >= 2)
    assert runner._job_generation >= 1
    # Let the new epoch settle + route the drop BEFORE releasing the old
    # worker — the race window the issue describes.
    assert _pump(app, lambda: bool(batches))
    # Now the abandoned worker wakes from its slow IO and finishes.
    gated['release'].set()
    assert _pump(app, lambda: gated['done'].is_set())
    runner.shutdown()

    # Isolation: the worker never saw the runner's dicts, and none of its
    # writes (poison or scan markers) reached the live epoch.
    assert gated['seen'] is not runner._seen
    assert gated['pending'] is not runner._pending
    assert _POISON in gated['seen']
    assert _POISON not in runner._seen
    assert _POISON not in runner._pending
    assert str(drop) in runner._seen

    # Exactly-once: the cancelled stale job routed nothing — the live
    # epoch's single route produced the single inbox row.
    assert route_calls['n'] == 1
    inbox = CaptureInboxRepository(
        repository, CaptureIngestionRepository(repository)
    )
    items = inbox.list_items()
    assert len(items) == 1
    # The detached thread exited on its own — no leaked QThread.
    assert _pump(
        app, lambda: lingering_thread_count() == lingering_before
    )


def test_abandoned_route_redelivery_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale job resumes mid-route; the live epoch routes the same drop.

    Suppression of the duplicate lands on content identity at the
    canonical repo: the second ``stage`` reports ``already_staged``
    (arrival_count bumps) instead of writing a second inbox row.
    """
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(
        repository, _Prefs(str(watch)), interval_ms=40
    )
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))

    real_route = watch_module.route_capture_intent
    gate = {
        'started': threading.Event(),
        'release': threading.Event(),
        'done': threading.Event(),
        'n': 0,
    }

    def gated_route(intent, *, repository, arrival_source):
        gate['n'] += 1
        if gate['n'] == 1:
            # The doomed job holds mid-route — before its DB commit —
            # until the new epoch has routed the same bundle.
            gate['started'].set()
            gate['release'].wait(timeout=30)
            result = real_route(
                intent, repository=repository, arrival_source=arrival_source
            )
            gate['done'].set()
            return result
        return real_route(
            intent, repository=repository, arrival_source=arrival_source
        )

    monkeypatch.setattr(
        watch_module, 'route_capture_intent', gated_route
    )

    drop = _write_bundle_zip(watch, 'duproute')
    assert _pump(app, lambda: gate['started'].is_set())
    _force_stall(runner)
    # New epoch: the drop's seen marker was never published by the
    # abandoned job, so it re-settles (already pending) and routes again
    # — call #2 runs the real route and stages first.
    assert _pump(app, lambda: gate['n'] >= 2)
    assert _pump(app, lambda: bool(batches))
    # The abandoned route now commits — same bundle, same lineage digest.
    gate['release'].set()
    assert _pump(app, lambda: gate['done'].is_set())
    runner.shutdown()

    inbox = CaptureInboxRepository(
        repository, CaptureIngestionRepository(repository)
    )
    items = inbox.list_items()
    assert len(items) == 1
    assert items[0].capture_revision_id
    # The second route hit the idempotent re-entry path: one row, and the
    # arrival counter honestly records that two routes occurred.
    assert items[0].arrival_count == 2
    assert gate['n'] == 2


def test_watch_root_change_with_abandoned_job_keeps_epochs_unmixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stalled job on root A + watch moved to B: A's markers never land."""
    app = _app()
    watch_a = tmp_path / 'a'
    watch_a.mkdir()
    watch_b = tmp_path / 'b'
    watch_b.mkdir()
    prefs = _Prefs(str(watch_a))
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(repository, prefs, interval_ms=40)
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))
    # The watch identity is the canonical root (#1019).
    assert runner._watched_root == watch_a.resolve()

    calls = {'n': 0}
    gated = _install_gated_scan(monkeypatch, calls)
    # Same-name drop in both roots; only B's may ever be staged.
    _write_bundle_zip(watch_a, 'same')
    _write_bundle_zip(watch_b, 'same')

    # Doomed job scanning root A.
    assert _pump(app, lambda: gated['started'].is_set())
    prefs.watch_dir = str(watch_b)
    _force_stall(runner)
    assert _pump(app, lambda: runner._watched_root == watch_b.resolve())
    # B's epoch publishes only B markers; let a couple of scans commit.
    assert _pump(
        app,
        lambda: any(
            key.startswith('\x00scanned:')
            and str(watch_b.resolve()) in key
            for key in runner._seen
        ),
    )
    gated['release'].set()
    assert _pump(app, lambda: gated['done'].is_set())
    runner.shutdown()

    # The abandoned root-A job wrote nothing into the live epoch.
    assert _POISON not in runner._seen
    assert not any(str(watch_a) in key for key in runner._seen)
    assert not any(str(watch_a) in key for key in runner._pending)
    assert not any(str(watch_a) in key for key in runner._route_failures)
    # B's first scan baselines its pre-existing file — never staged.
    inbox = CaptureInboxRepository(
        repository, CaptureIngestionRepository(repository)
    )
    assert inbox.list_items() == ()
    assert batches == []


def test_disabled_reenabled_watch_rebaselines_after_abandoned_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """off→on is a new epoch: a stale job's pre-disable state is dropped."""
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    prefs = _Prefs(str(watch))
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(repository, prefs, interval_ms=40)
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))

    calls = {'n': 0}
    gated = _install_gated_scan(monkeypatch, calls)
    assert _pump(app, lambda: gated['started'].is_set())
    # Disable while the job is wedged, force the stall release, then
    # re-enable: the next epoch must re-baseline, not continue A's state.
    prefs.watch_dir = ''
    _force_stall(runner)
    assert _pump(app, lambda: runner._watched_root is None)
    prefs.watch_dir = str(watch)
    preexisting = _write_bundle_zip(watch, 'preexisting')
    assert _pump(app, lambda: runner._watched_root == watch.resolve())
    gated['release'].set()
    assert _pump(app, lambda: gated['done'].is_set())
    runner.shutdown()

    # The file dropped while the lane was off pre-dates the new epoch —
    # it baselines rather than staging, and the stale job leaked nothing.
    assert _POISON not in runner._seen
    assert str(preexisting) in runner._seen
    inbox = CaptureInboxRepository(
        repository, CaptureIngestionRepository(repository)
    )
    assert inbox.list_items() == ()
    assert batches == []


def test_shutdown_with_abandoned_job_leaves_state_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Late completions after shutdown mutate nothing and emit nothing."""
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(
        repository, _Prefs(str(watch)), interval_ms=40
    )
    batches: list = []
    runner.scan_completed.connect(batches.append)
    runner.start()
    assert _pump(app, lambda: bool(runner._seen))

    calls = {'n': 0}
    gated = _install_gated_scan(monkeypatch, calls)
    _write_bundle_zip(watch, 'late')
    assert _pump(app, lambda: gated['started'].is_set())
    _force_stall(runner)
    assert _pump(app, lambda: calls['n'] >= 2)
    assert _pump(app, lambda: bool(batches))
    runner.shutdown()

    frozen_seen = dict(runner._seen)
    frozen_pending = dict(runner._pending)
    frozen_failures = dict(runner._route_failures)
    gated['release'].set()
    assert _pump(app, lambda: gated['done'].is_set())
    # Give the detached worker's queued emissions a chance to (not) land.
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert runner._seen == frozen_seen
    assert runner._pending == frozen_pending
    assert runner._route_failures == frozen_failures
    staged = [
        p.name for batch in batches for p, r, _ in batch
        if r is not None and r.outcome == 'staged_for_review'
    ]
    assert staged == ['late.htdtcapture']


def test_on_completed_drops_stale_state_even_if_delivered(
    tmp_path: Path,
) -> None:
    """The owner-thread publish guard stands on its own: a result whose
    ``(root, generation)`` no longer names the live epoch is discarded
    even if a completion were somehow delivered for it."""
    _app()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(repository, _Prefs(''), interval_ms=30)
    live_root = tmp_path / 'live'
    live_root.mkdir()
    runner._watched_root = live_root
    runner._seen['\x00scanned:' + str(live_root)] = (0, 0)

    stale = _WatchScanResult(
        root=live_root,
        generation=runner._job_generation - 1,
        seen={_POISON: (1, 2)},
        pending={_POISON: (3, 4)},
        route_failures={_POISON: 9},
        results=[],
    )
    runner._on_completed('key', stale, None)
    assert _POISON not in runner._seen
    assert _POISON not in runner._pending
    assert _POISON not in runner._route_failures
    assert runner._seen == {'\x00scanned:' + str(live_root): (0, 0)}

    wrong_root = _WatchScanResult(
        root=tmp_path / 'other',
        generation=runner._job_generation,
        seen={_POISON: (1, 2)},
        pending={},
        route_failures={},
        results=[],
    )
    runner._on_completed('key', wrong_root, None)
    assert _POISON not in runner._seen
    runner.shutdown()


def test_job_finished_ignores_stale_generation(tmp_path: Path) -> None:
    """A late finish callback must not unlatch the replacement job."""
    _app()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    runner = CaptureWatchRunner(repository, _Prefs(''), interval_ms=30)
    runner._in_flight = True
    runner._in_flight_since = time.monotonic()

    stale_generation = runner._job_generation
    runner._job_generation += 1  # a newer job owns the latch now
    runner._job_finished('key', stale_generation)
    assert runner._in_flight is True
    assert runner._in_flight_since is not None

    runner._job_finished('key', runner._job_generation)
    assert runner._in_flight is False
    assert runner._in_flight_since is None
    runner.shutdown()
