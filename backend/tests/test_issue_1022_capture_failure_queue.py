"""#1022 — capture-watch failure honesty + bounded failure queue.

Before this fix a watch-folder drop whose route failed still surfaced in
the Activity Center as 「完了」, and a file that exhausted the route cap
kept its ``_seen`` marker with no inbox row and no recovery surface. These
tests pin the typed-accurate completion (success/partial/all-failed), the
persistent per-route failure queue, the fingerprint+epoch retry gate, and
the support-diagnostic redaction.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

import htdt.capture_watch_runner as watch_module  # noqa: E402
import htdt.workflow_application as workflow_module  # noqa: E402
from htdt.activity_center import OperationState  # noqa: E402
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.capture_inbox import CaptureInboxRepository  # noqa: E402
from htdt.capture_ingestion_transaction import (  # noqa: E402
    CaptureIngestionRepository,
)
from htdt.capture_watch_failures import (  # noqa: E402
    WATCH_FAILURE_QUEUE_FILENAME,
    WATCH_QUEUE_ARRIVAL_SOURCE,
    CaptureWatchFailureQueue,
    WatchFailureClass,
    WatchRetryVerdict,
    WatchRouteExhaustion,
    verify_watch_retry,
    write_watch_failure_diagnostic,
)
from htdt.capture_watch_runner import CaptureWatchRunner  # noqa: E402
from htdt.workflow_application import (  # noqa: E402
    WorkflowApplicationComposition,
)
from htdt.workflow_navigation import (  # noqa: E402
    ApplicationDestinationId,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _pump(app: QApplication, predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return predicate()


def _composition(tmp_path: Path) -> WorkflowApplicationComposition:
    repository = SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3")
    return WorkflowApplicationComposition(repository, "document-1")


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


def _watch_ops(composition: WorkflowApplicationComposition) -> list:
    return [
        op
        for op in composition.activity_center.recent(limit=40)
        if op.operation_kind == 'capture_watch_stage'
    ]


def _inbox(composition: WorkflowApplicationComposition):
    return CaptureInboxRepository(
        composition.repository,
        CaptureIngestionRepository(composition.repository),
    )


def _exhaustion_for(path: Path, watch_root: Path) -> WatchRouteExhaustion:
    stat = path.stat()
    return WatchRouteExhaustion(
        path=str(path),
        error_kind='routing_error',
        failure_class=WatchFailureClass.RETRYABLE,
        detail='キャプチャの取り込みに失敗しました',
        attempts=watch_module._ROUTE_MAX_ATTEMPTS,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        watch_root=str(watch_root),
    )


# -- typed-accurate activity completion ----------------------------------


def test_all_success_batch_completes(tmp_path: Path) -> None:
    """All-staged batch → exactly one COMPLETED operation row."""
    app = _app()
    composition = _composition(tmp_path)
    results = [
        (
            Path('/watch/a.htdtcapture'),
            SimpleNamespace(outcome='staged_for_review'),
            None,
        ),
        (
            Path('/watch/b.htdtcapture'),
            SimpleNamespace(outcome='staged_for_review'),
            None,
        ),
    ]
    try:
        composition._on_capture_watch_completed(results)
        ops = _watch_ops(composition)
        assert len(ops) == 1
        assert ops[0].state == OperationState.COMPLETED
        assert '2 件のキャプチャ' in (ops[0].result_summary or '')
        assert not ops[0].error_summary
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()


def test_all_failed_batch_fails_never_fakes_success(tmp_path: Path) -> None:
    """All-failed batch → FAILED via fail(); the error lives in
    error_summary, never smuggled into a completed result_summary."""
    app = _app()
    composition = _composition(tmp_path)
    results = [
        (Path('/watch/a.htdtcapture'), None, 'route blew up'),
        (
            Path('/watch/b.htdtcapture'),
            SimpleNamespace(outcome='failed', detail='corrupt bundle'),
            None,
        ),
    ]
    try:
        composition._on_capture_watch_completed(results)
        ops = _watch_ops(composition)
        assert len(ops) == 1
        assert ops[0].state == OperationState.FAILED
        assert '2 件は取り込めませんでした' in (ops[0].error_summary or '')
        assert not ops[0].result_summary
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()


def test_partial_batch_splits_into_two_operations(tmp_path: Path) -> None:
    """No partial terminal state exists — a mixed batch writes one
    completed row for the staged drops AND one failed row for the rest,
    so neither outcome lies about the other."""
    app = _app()
    composition = _composition(tmp_path)
    results = [
        (
            Path('/watch/good.htdtcapture'),
            SimpleNamespace(outcome='staged_for_review'),
            None,
        ),
        (
            Path('/watch/bad.htdtcapture'),
            SimpleNamespace(outcome='invalid_or_unsupported'),
            None,
        ),
    ]
    try:
        composition._on_capture_watch_completed(results)
        ops = _watch_ops(composition)
        states = {op.state for op in ops}
        assert len(ops) == 2
        assert states == {OperationState.COMPLETED, OperationState.FAILED}
        completed = [op for op in ops if op.state == OperationState.COMPLETED]
        failed = [op for op in ops if op.state == OperationState.FAILED]
        assert '1 件のキャプチャ' in (completed[0].result_summary or '')
        assert '1 件は取り込めませんでした' in (failed[0].error_summary or '')
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()


# -- runner exhaustion → persistent queue --------------------------------


def test_runner_emits_exhaustion_record_at_route_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A route that hits _ROUTE_MAX_ATTEMPTS emits a
    WatchRouteExhaustion carrying the dropped file's settled signature —
    the fingerprint the retry gate needs — while the file keeps its
    _seen marker and still gets no inbox row."""
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    attempts = {'count': 0}

    def always_fail(*_args, **_kwargs):
        attempts['count'] += 1
        raise RuntimeError('permanent failure')

    monkeypatch.setattr(watch_module, 'route_capture_intent', always_fail)
    runner = CaptureWatchRunner(
        repository, _Prefs(str(watch)), interval_ms=40
    )
    exhausted: list = []
    runner.routes_exhausted.connect(exhausted.extend)
    runner.start()
    try:
        assert _pump(app, lambda: bool(runner._seen))
        drop = watch / 'wedged.htdtcapture'
        drop.write_bytes(b'junk that always fails')
        assert _pump(app, lambda: bool(exhausted))
        # Give the cap time to fully settle before asserting.
        settled = time.monotonic() + 0.5
        while time.monotonic() < settled:
            app.processEvents()
            time.sleep(0.01)
    finally:
        runner.shutdown()

    records = exhausted
    assert len(records) == 1
    record = records[0]
    assert Path(record.path).name == 'wedged.htdtcapture'
    assert record.attempts == watch_module._ROUTE_MAX_ATTEMPTS
    assert record.failure_class == WatchFailureClass.RETRYABLE
    # The record's fingerprint IS the kept _seen signature.
    assert (record.mtime_ns, record.size) == runner._seen[str(drop)]
    assert record.watch_root == str(watch)
    # Still no inbox row — this queue is the only recovery surface.
    inbox = CaptureInboxRepository(
        repository, CaptureIngestionRepository(repository)
    )
    assert inbox.list_items() == ()


class _Prefs:
    def __init__(self, watch_dir: str = '') -> None:
        self.watch_dir = watch_dir

    def get(self, key: str) -> object:
        if key == 'integrations.capture_watch_dir':
            return self.watch_dir
        raise KeyError(key)


def test_exhaustion_persists_a_reloadable_queue_entry(
    tmp_path: Path,
) -> None:
    """The queue outlives the 15s statusbar: recorded entries land in a
    JSON store under the data dir and reload into a fresh queue."""
    app = _app()
    composition = _composition(tmp_path)
    watch = tmp_path / 'watch'
    watch.mkdir()
    drop = _write_bundle_zip(watch, 'lost')
    try:
        composition._on_capture_watch_exhausted(
            [_exhaustion_for(drop, watch)]
        )
        store = (
            Path(composition.data_dir) / WATCH_FAILURE_QUEUE_FILENAME
        )
        assert store.is_file()
        reloaded = CaptureWatchFailureQueue.for_data_dir(
            composition.data_dir
        )
        entries = reloaded.entries()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.path == str(drop)
        assert entry.basename == 'lost.htdtcapture'
        assert entry.attempts == watch_module._ROUTE_MAX_ATTEMPTS
        assert entry.error_kind == 'routing_error'
        assert entry.failure_class == WatchFailureClass.RETRYABLE
        assert entry.diagnostic_id
        # And the drop is still absent from the inbox itself.
        assert _inbox(composition).list_items() == ()
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()


# -- retry verification gate ---------------------------------------------


def test_verify_watch_retry_gates(
    tmp_path: Path,
) -> None:
    """Same path + fingerprint + epoch → OK; any drift is refused with
    the operator-facing reason."""
    watch = tmp_path / 'watch'
    watch.mkdir()
    drop = _write_bundle_zip(watch, 'retry')
    queue = CaptureWatchFailureQueue(tmp_path / 'queue.json')
    stat = drop.stat()
    queue.record(
        path=str(drop),
        error_kind='routing_error',
        failure_class=WatchFailureClass.RETRYABLE,
        detail='',
        attempts=3,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        watch_root=str(watch),
    )
    entry = queue.get(drop)
    verdict, _ = verify_watch_retry(entry, watch_root_now=str(watch))
    assert verdict == WatchRetryVerdict.OK

    # A rewrite changes the signature → replaced/mid-write, refused.
    drop.write_bytes(drop.read_bytes() + b'newer bytes')
    verdict, reason = verify_watch_retry(
        entry, watch_root_now=str(watch)
    )
    assert verdict == WatchRetryVerdict.REPLACED
    assert '記録時と内容が変わっています' in reason

    # A different armed root is a different scan epoch — refused even
    # when the fingerprint still matches.
    stat = drop.stat()
    queue.record(
        path=str(drop),
        error_kind='routing_error',
        failure_class=WatchFailureClass.RETRYABLE,
        detail='',
        attempts=3,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        watch_root=str(watch),
    )
    entry = queue.get(drop)
    verdict, reason = verify_watch_retry(
        entry, watch_root_now=str(tmp_path / 'other')
    )
    assert verdict == WatchRetryVerdict.EPOCH_MISMATCH
    assert '監視フォルダー' in reason
    verdict, _ = verify_watch_retry(entry, watch_root_now=None)
    assert verdict == WatchRetryVerdict.EPOCH_MISMATCH

    # Deleted file → refused.
    drop.unlink()
    verdict, _ = verify_watch_retry(entry, watch_root_now=str(watch))
    assert verdict == WatchRetryVerdict.DELETED

    # An unsupported drop is never safe-retryable.
    drop = _write_bundle_zip(watch, 'bad')
    stat = drop.stat()
    queue.record(
        path=str(drop),
        error_kind='invalid_or_unsupported',
        failure_class=WatchFailureClass.UNSUPPORTED,
        detail='バンドル形式が未対応です',
        attempts=3,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        watch_root=str(watch),
    )
    verdict, reason = verify_watch_retry(
        queue.get(drop), watch_root_now=str(watch)
    )
    assert verdict == WatchRetryVerdict.NOT_RETRYABLE
    assert '再試行では解決しません' in reason


def test_safe_retry_refuses_replaced_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """安全に再試行 on a file whose bytes changed since recording warns
    and routes nothing — no fake reprocess, no surprise ingest."""
    app = _app()
    composition = _composition(tmp_path)
    watch = tmp_path / 'watch'
    watch.mkdir()
    drop = _write_bundle_zip(watch, 'moved')
    stat = drop.stat()
    composition._watch_failure_queue.record(
        path=str(drop),
        error_kind='routing_error',
        failure_class=WatchFailureClass.RETRYABLE,
        detail='',
        attempts=3,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        watch_root=str(watch),
    )
    composition.preferences.set(
        'integrations.capture_watch_dir', str(watch)
    )
    warnings: list = []
    monkeypatch.setattr(
        QMessageBox,
        'warning',
        staticmethod(lambda *a, **k: warnings.append(a)),
    )
    try:
        # Rewrite → signature drift.
        drop.write_bytes(b'completely different contents')
        composition._retry_watch_failure(str(drop))
        assert warnings
        assert composition._watch_failure_queue.get(drop) is not None
        assert not composition._bundle_busy
        assert _inbox(composition).list_items() == ()
        # The epoch gate also refuses — watch folder reconfigured away.
        composition.preferences.set('integrations.capture_watch_dir', '')
        stat = drop.stat()
        composition._watch_failure_queue.record(
            path=str(drop),
            error_kind='routing_error',
            failure_class=WatchFailureClass.RETRYABLE,
            detail='',
            attempts=3,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            watch_root=str(watch),
        )
        warnings.clear()
        composition._retry_watch_failure(str(drop))
        assert warnings
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()


def test_manual_retry_stages_exactly_one_inbox_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified 安全に再試行 re-routes the queued file, resolves the
    queue entry, and lands exactly one inbox row stamped with the honest
    watch_queue_retry provenance — no promotion."""
    app = _app()
    composition = _composition(tmp_path)
    watch = tmp_path / 'watch'
    watch.mkdir()
    drop = _write_bundle_zip(watch, 'rescued')
    stat = drop.stat()
    composition._watch_failure_queue.record(
        path=str(drop),
        error_kind='routing_error',
        failure_class=WatchFailureClass.RETRYABLE,
        detail='',
        attempts=3,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        watch_root=str(watch),
    )
    composition.preferences.set(
        'integrations.capture_watch_dir', str(watch)
    )
    monkeypatch.setattr(QMessageBox, 'warning', staticmethod(lambda *a, **k: None))
    try:
        composition._retry_watch_failure(str(drop))
        assert composition._bundle_busy
        assert _pump(app, lambda: not composition._bundle_busy)
        assert composition._watch_failure_queue.get(drop) is None
        items = _inbox(composition).list_items()
        assert len(items) == 1
        assert items[0].arrival_source == WATCH_QUEUE_ARRIVAL_SOURCE
        ops = _watch_ops(composition)
        assert len(ops) == 1
        assert ops[0].state == OperationState.COMPLETED
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()


def test_failed_manual_retry_keeps_entry_and_bumps_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-route that fails again is honest: the queue entry stays, its
    attempt count joins the earlier automatic tries, and the operation
    ends FAILED — never auto-repaired, never a fake success."""
    app = _app()
    composition = _composition(tmp_path)
    watch = tmp_path / 'watch'
    watch.mkdir()
    drop = watch / 'stubborn.htdtcapture'
    drop.write_bytes(b'junk')
    stat = drop.stat()
    composition._watch_failure_queue.record(
        path=str(drop),
        error_kind='routing_error',
        failure_class=WatchFailureClass.RETRYABLE,
        detail='',
        attempts=3,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        watch_root=str(watch),
    )
    composition.preferences.set(
        'integrations.capture_watch_dir', str(watch)
    )
    monkeypatch.setattr(
        workflow_module,
        'route_capture_intent',
        lambda *_a, **_k: SimpleNamespace(
            outcome='failed', detail='取り込みに失敗しました'
        ),
    )
    try:
        composition._retry_watch_failure(str(drop))
        assert _pump(app, lambda: not composition._bundle_busy)
        entry = composition._watch_failure_queue.get(drop)
        assert entry is not None
        assert entry.attempts == 4
        ops = _watch_ops(composition)
        assert len(ops) == 1
        assert ops[0].state == OperationState.FAILED
        assert '取り込みに失敗しました' in (ops[0].error_summary or '')
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()


# -- page + diagnostics ----------------------------------------------------


def test_inbox_page_lists_queue_and_gates_actions(tmp_path: Path) -> None:
    """The failure-queue tab surfaces persisted entries even though the
    drops have no inbox row — the case #988's inbox search can't reach —
    and the retry button follows the live verify verdict."""
    app = _app()
    composition = _composition(tmp_path)
    watch = tmp_path / 'watch'
    watch.mkdir()
    drop = _write_bundle_zip(watch, 'queued')
    try:
        composition.preferences.set(
            'integrations.capture_watch_dir', str(watch)
        )
        composition._on_capture_watch_exhausted(
            [_exhaustion_for(drop, watch)]
        )
        # The router is lazy: navigate materializes the inbox mount.
        assert composition.shell.navigate(
            ApplicationDestinationId.INBOX
        )
        page = composition.shell.router.mount(
            ApplicationDestinationId.INBOX
        ).widget
        assert page.watch_failure_table.rowCount() == 1
        page.watch_failure_table.selectRow(0)
        app.processEvents()
        # Matching fingerprint + epoch → the safe retry is offered.
        assert page.watch_retry_button.isEnabled()
        assert page.watch_import_button.isEnabled()
        assert page.watch_diag_button.isEnabled()
        detail = page.watch_failure_detail.text()
        assert 'queued.htdtcapture' in detail
        # Rewrite the file → the button must immediately refuse.
        drop.write_bytes(b'replaced bytes')
        page._refresh_watch_failures()
        page.watch_failure_table.selectRow(0)
        app.processEvents()
        assert not page.watch_retry_button.isEnabled()
        # An explicit pick-and-import stays possible for a real file.
        assert page.watch_import_button.isEnabled()
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()


def test_diagnostic_note_redacts_the_full_path(tmp_path: Path) -> None:
    """サポート診断 writes a shareable note: basename + fingerprint +
    correlation id — never the raw path, never the bundle."""
    secret_root = tmp_path / 'confidential-root'
    watch = secret_root / 'watch'
    watch.mkdir(parents=True)
    drop = watch / 'capture.htdtcapture'
    drop.write_bytes(b'junk')
    stat = drop.stat()
    queue = CaptureWatchFailureQueue(tmp_path / 'queue.json')
    entry = queue.record(
        path=str(drop),
        error_kind='routing_error',
        failure_class=WatchFailureClass.RETRYABLE,
        detail='取り込みに失敗しました',
        attempts=3,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        watch_root=str(watch),
    )
    note = write_watch_failure_diagnostic(tmp_path / 'data', entry)
    payload = json.loads(note.read_text(encoding='utf-8'))
    text = note.read_text(encoding='utf-8')
    # The sensitive container directory never leaves the machine.
    assert 'confidential-root' not in text
    assert str(drop) not in text
    assert str(watch) not in text
    # But basename + fingerprint + correlation id are all present so the
    # note still identifies the failing drop to support.
    assert payload['basename'] == 'capture.htdtcapture'
    assert payload['diagnostic_id'] == entry.diagnostic_id
    assert payload['file_signature'] == {
        'mtime_ns': stat.st_mtime_ns,
        'size': stat.st_size,
    }
    assert note.parent.name == 'diagnostics'


def test_corrupt_queue_store_loads_empty(tmp_path: Path) -> None:
    """A damaged queue file fails closed to empty — it must never take
    the inbox page down with it."""
    store = tmp_path / 'capture-watch-failures.json'
    store.write_bytes(b'{not json')
    queue = CaptureWatchFailureQueue(store)
    assert queue.entries() == ()
