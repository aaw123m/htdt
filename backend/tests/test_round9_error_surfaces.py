from __future__ import annotations

import errno
import os
import time
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow

from htdt.cad_document import CommandHistory, EditStateError
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.command_registry import CommandDefinition, CommandRegistry
from htdt.measurement_workflow import MeasurementAssignment, MeasurementWorkflowController
from htdt.native_diagnostics import (
    configure_diagnostics,
    install_exception_hooks,
    uninstall_exception_hooks,
)
from htdt.native_worker import NativeWorkerPool
from htdt.rew_api import RewApiUnavailable
from htdt.rew_parser import RewParseError
from htdt.user_facing_error import operation_error_message


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _pump_until(predicate, timeout_s: float = 5.0) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------
# Undo/redo under partial failure: the stack must stay consistent.


class _SwapCommand:
    """Duck-typed EditCommand double: apply swaps to marker, revert raises."""

    is_noop = False
    presentation = None

    def __init__(self, marker: object, *, revert_raises: bool = False) -> None:
        self._marker = marker
        self._revert_raises = revert_raises

    def apply(self, document: object) -> object:
        return self._marker

    def revert(self, document: object) -> object:
        if self._revert_raises:
            raise EditStateError('after state does not match')
        return document


def test_failed_push_neither_records_command_nor_truncates_redo() -> None:
    history = CommandHistory()
    first = _SwapCommand('b')
    assert history.push(first, 'a') == 'b'

    class _FailingCommand(_SwapCommand):
        def apply(self, document: object) -> object:
            raise EditStateError('before state does not match')

    history.undo('b')  # index back to 0 so 'first' is the redo tail
    with pytest.raises(EditStateError):
        history.push(_FailingCommand('z'), 'a')

    assert history.length == 1
    assert history.index == 0
    assert history.can_redo is True  # 'first' still redoable — tail intact


def test_failed_undo_and_redo_do_not_consume_commands() -> None:
    history = CommandHistory()
    assert history.push(_SwapCommand('b'), 'a') == 'b'
    history._commands[0] = _SwapCommand('b', revert_raises=True)

    with pytest.raises(EditStateError):
        history.undo('not-b')
    assert history.index == 1
    assert history.can_undo is True

    with pytest.raises(EditStateError):
        history.undo('not-b')
    assert history.index == 1  # retry stays possible, position unchanged

    class _RedoFailing(_SwapCommand):
        def apply(self, document: object) -> object:
            raise EditStateError('before state does not match')

    history._commands[0] = _RedoFailing('b')
    history._index = 0  # simulate an already-undone state
    with pytest.raises(EditStateError):
        history.redo('a')
    assert history.index == 0
    assert history.can_redo is True


# ---------------------------------------------------------------------
# Worker error payload: the exception object must cross the boundary so
# typed mappings (REW API down, parse failures) reach the operator.


def test_worker_error_payload_keeps_exception_type_for_mapping() -> None:
    _app()
    pool = NativeWorkerPool(shutdown_timeout_ms=500)
    completions: list[tuple[object, object, object]] = []

    def failing(_cancel_event: Event) -> object:
        raise RewApiUnavailable('connection refused')

    pool.start(
        'rew',
        failing,
        lambda key, result, error: completions.append((key, result, error)),
    )
    assert _pump_until(lambda: 'rew' not in pool.tasks)

    (key, result, error), = completions
    assert key == 'rew'
    assert result is None
    assert isinstance(error, RewApiUnavailable)
    assert operation_error_message(error) == 'REWに接続できませんでした'
    pool.deleteLater()


# ---------------------------------------------------------------------
# Command dispatch: executor exceptions must reach a failure surface
# instead of escaping the Qt slot silently.


def test_registry_routes_executor_failures_to_error_handler() -> None:
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(command_id='edit.undo', display_name='元に戻す'),
        execute=lambda: (_ for _ in ()).throw(EditStateError('state mismatch')),
    )

    with pytest.raises(EditStateError):
        registry.execute('edit.undo')  # no handler: unchanged behavior

    seen: list[tuple[str, BaseException]] = []
    registry.set_error_handler(
        lambda definition, exc: seen.append((definition.display_name, exc))
    )
    assert registry.execute('edit.undo') is True
    (name, exc), = seen
    assert name == '元に戻す'
    assert isinstance(exc, EditStateError)


# ---------------------------------------------------------------------
# Uncaught slot exceptions: a visible status-bar note accompanies the
# diagnostics log entry instead of vanishing to stderr.


def test_uncaught_exception_posts_statusbar_notice(tmp_path: Path) -> None:
    app = _app()
    diagnostics = configure_diagnostics(tmp_path / "data")
    window = QMainWindow()
    window.show()
    app.processEvents()
    install_exception_hooks(diagnostics)
    try:
        import sys

        sys.excepthook(RuntimeError, RuntimeError('boom'), None)
        assert '予期しないエラー' in window.statusBar().currentMessage()
    finally:
        uninstall_exception_hooks()
        window.close()
        window.deleteLater()


def test_uncaught_exception_notice_is_thread_safe_noop(tmp_path: Path) -> None:
    _app()
    diagnostics = configure_diagnostics(tmp_path / "data")
    install_exception_hooks(diagnostics)
    try:
        import htdt.native_diagnostics as nd

        # From a worker thread the surfacing must simply not run — and must
        # never raise (it is called inside sys.excepthook).
        done = Event()

        def worker() -> None:
            nd._surface_uncaught_on_statusbar(diagnostics)
            done.set()

        import threading

        thread = threading.Thread(target=worker)
        thread.start()
        assert done.wait(5.0)
        thread.join()
    finally:
        uninstall_exception_hooks()


# ---------------------------------------------------------------------
# Batch per-item failure honesty: the row text is the mapped operator
# message, never raw backend exception text.


def _measurement_controller(tmp_path: Path) -> MeasurementWorkflowController:
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    return MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=measurement_repository,
        quality_repository=CadMeasurementQualityRepository(measurement_repository),
    )


def _assignment() -> MeasurementAssignment:
    return MeasurementAssignment(
        measurement_entity_id="point-mlp",
        evidence_type="measured",
        channel_role="front_left",
        source_speaker_ids=("speaker-fl",),
        radiation_scope="single",
        routing_evidence="manual",
    )


def test_batch_stage_failure_reports_mapped_message(tmp_path: Path) -> None:
    controller = _measurement_controller(tmp_path)
    (item,) = controller.stage_rew_text_files(
        [(b"not a frequency response", "broken.txt")]
    )
    assert item.status == "failed"
    assert item.error == 'REWファイルを解析できませんでした'
    assert 'REW' in item.error  # operator-facing, not parser internals


def test_batch_commit_failure_reports_mapped_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _measurement_controller(tmp_path)
    (item,) = controller.stage_rew_text_files(
        [(b"20 70\n40 71\n80 69\n", "seat.txt")]
    )
    controller.set_batch_item_assignment(item.item_id, _assignment())

    def _disk_full(*_args: object) -> None:
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(
        controller, '_save_measurement_for_commit', _disk_full
    )
    (outcome,) = controller.commit_batch([item.item_id])
    assert outcome.outcome == 'failed'
    assert outcome.error == 'ディスク容量が不足しています'
    assert 'No space' not in outcome.error


def test_dataset_listing_failure_reports_mapped_message(tmp_path: Path) -> None:
    controller = _measurement_controller(tmp_path)
    (item,) = controller.stage_rew_text_files(
        [(b"20 70\n40 71\n80 69\n", "seat.txt")]
    )
    controller.set_batch_item_assignment(item.item_id, _assignment())
    (outcome,) = controller.commit_batch([item.item_id])
    assert outcome.outcome == 'committed'

    real_repo = controller.measurement_repository

    class _BrokenRepo:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        def datasets_for_document(
            self, *args: object, **_kwargs: object
        ) -> object:
            return {}, {
                m.measurement_id: RewParseError('corrupt dataset blob')
                for m in self._inner.list_measurements(args[0])
            }

    controller.measurement_repository = _BrokenRepo(real_repo)  # type: ignore[assignment]
    views = controller.measurement_views()
    assert views and views[0].dataset_error == 'REWファイルを解析できませんでした'


# ---------------------------------------------------------------------
# Launch-time data-directory failure: the packaged GUI must show the
# recovery surface, not exit silently.


def test_gui_launch_reports_data_dir_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import htdt.data_relocation as relocation
    import htdt.native_cad as native_cad
    from htdt.data_relocation import ManagedDataUnavailableError

    monkeypatch.setattr(
        relocation,
        'assert_managed_root_available',
        lambda *_a, **_k: (_ for _ in ()).throw(
            ManagedDataUnavailableError('root missing')
        ),
    )
    reported: list[dict] = []
    monkeypatch.setattr(
        native_cad,
        'report_launch_failure',
        lambda **kwargs: reported.append(kwargs),
    )

    rc = native_cad.main(['--data-dir', str(tmp_path / 'missing')])
    assert rc == 1
    (call,) = reported
    assert call['title'] == 'HTDTのデータディレクトリを開けません'
    assert 'root missing' in call['reason']


def test_maintenance_launch_prints_data_dir_failure_without_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import htdt.data_relocation as relocation
    import htdt.native_cad as native_cad
    from htdt.data_relocation import ManagedDataUnavailableError

    monkeypatch.setattr(
        relocation,
        'assert_managed_root_available',
        lambda *_a, **_k: (_ for _ in ()).throw(
            ManagedDataUnavailableError('root missing')
        ),
    )
    reported: list[dict] = []
    monkeypatch.setattr(
        native_cad,
        'report_launch_failure',
        lambda **kwargs: reported.append(kwargs),
    )

    rc = native_cad.main(
        ['--data-dir', str(tmp_path / 'missing'), '--backup', str(tmp_path / 'b.zip')]
    )
    assert rc == 1
    assert reported == []
    assert '利用不可' in capsys.readouterr().err


# ---------------------------------------------------------------------
# Error mapping regressions: ENOSPC and REW transport failures.


def test_disk_full_maps_to_dedicated_message() -> None:
    assert (
        operation_error_message(OSError(errno.ENOSPC, 'No space left'))
        == 'ディスク容量が不足しています'
    )


def test_rew_transport_failure_maps_before_generic_rew_error() -> None:
    assert (
        operation_error_message(RewApiUnavailable('refused'))
        == 'REWに接続できませんでした'
    )
