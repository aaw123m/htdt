from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import htdt.native_cad as native_cad
from PySide6.QtCore import QObject
from htdt import __version__
from htdt.cad_scene import F1_DOCUMENT_ID
from htdt.cad_schema import NativeSchemaError
from htdt.native_diagnostics import (
    LOG_FILENAME,
    MAX_LOG_RECORD_CHARS,
    build_identity,
    concise_reason,
    configure_diagnostics,
    diagnostics_dir,
    install_exception_hooks,
    report_launch_failure,
    uninstall_exception_hooks,
)
from htdt.runtime_instance import SingleInstanceGuard


@pytest.fixture(autouse=True)
def _restore_exception_hooks():
    yield
    uninstall_exception_hooks()


def _log_text(log_path: Path) -> str:
    assert log_path.is_file()
    return log_path.read_text(encoding="utf-8")


def test_diagnostics_log_lands_under_data_dir_with_build_identity(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    diagnostics = configure_diagnostics(data_dir)

    assert diagnostics.log_path == data_dir / "diagnostics" / LOG_FILENAME
    diagnostics.log_session_start("gui")
    diagnostics.flush()

    text = _log_text(diagnostics.log_path)
    assert "mode=gui" in text
    assert f"version={__version__}" in text
    assert "platform=" in text
    assert "frozen=" in text


def test_build_identity_describes_source_build() -> None:
    identity = build_identity()
    assert identity.version == __version__
    assert f"version={__version__}" in identity.describe()
    assert "python=" in identity.describe()


def test_diagnostics_log_rotates_and_stays_bounded(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    diagnostics = configure_diagnostics(data_dir, max_bytes=600, backup_count=2)
    for index in range(40):
        diagnostics.logger.warning("record %02d %s", index, "x" * 200)
    diagnostics.flush()

    files = sorted(diagnostics_dir(data_dir).glob(f"{LOG_FILENAME}*"))
    assert (data_dir / "diagnostics" / f"{LOG_FILENAME}.1").is_file()
    assert len(files) <= 3  # active log plus at most two rotated backups
    assert sum(path.stat().st_size for path in files) <= 3 * 600 + 600


def test_diagnostics_log_scrubs_secrets_and_bounds_records(tmp_path: Path) -> None:
    diagnostics = configure_diagnostics(tmp_path / "data")
    diagnostics.logger.info(
        "auth token=hunter2 password = \"pw-123\" Authorization: Bearer abcdef123"
    )
    diagnostics.flush()

    text = _log_text(diagnostics.log_path)
    assert "hunter2" not in text
    assert "pw-123" not in text
    assert "abcdef123" not in text
    assert "***" in text

    diagnostics.logger.info("payload %s", "y" * (MAX_LOG_RECORD_CHARS * 2))
    diagnostics.flush()
    lines = _log_text(diagnostics.log_path).splitlines()
    assert max(len(line) for line in lines) <= MAX_LOG_RECORD_CHARS + 64
    assert "[truncated]" in lines[-1]


def test_concise_reason_is_single_line_and_bounded() -> None:
    reason = concise_reason(ValueError("first line\nsecond line with details"))
    assert "\n" not in reason
    assert reason.startswith("ValueError: first line")

    long_reason = concise_reason(RuntimeError("y" * 500), max_chars=50)
    assert len(long_reason) <= len("RuntimeError: ") + 51
    assert long_reason.endswith("…")


def test_uncaught_exception_hook_logs_traceback_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chained: list[tuple] = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: chained.append(args))
    diagnostics = configure_diagnostics(tmp_path / "data")
    install_exception_hooks(diagnostics)
    try:
        try:
            raise ValueError("boom")
        except ValueError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
    finally:
        uninstall_exception_hooks()
    diagnostics.flush()

    text = _log_text(diagnostics.log_path)
    assert "uncaught python exception" in text
    assert "ValueError: boom" in text
    assert "Traceback" in text
    assert f"version={__version__}" in text
    assert len(chained) == 1  # the previously installed hook still ran


def test_thread_exception_hook_logs_uncaught_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chained: list[tuple] = []
    monkeypatch.setattr(threading, "excepthook", lambda *args: chained.append(args))
    diagnostics = configure_diagnostics(tmp_path / "data")
    install_exception_hooks(diagnostics)
    try:
        try:
            raise RuntimeError("thread boom")
        except RuntimeError as exc:
            thread = threading.Thread(target=lambda: None, name="diagnostics-probe")
            args = threading.ExceptHookArgs(
                (type(exc), exc, exc.__traceback__, thread)
            )
            threading.excepthook(args)
    finally:
        uninstall_exception_hooks()
    diagnostics.flush()

    text = _log_text(diagnostics.log_path)
    assert "uncaught thread diagnostics-probe exception" in text
    assert "RuntimeError: thread boom" in text
    assert len(chained) == 1


def test_qt_messages_are_routed_to_diagnostics_log(tmp_path: Path) -> None:
    diagnostics = configure_diagnostics(tmp_path / "data")
    install_exception_hooks(diagnostics)
    try:
        from PySide6.QtCore import qWarning

        qWarning("qt-diagnostic-probe-311")
    finally:
        uninstall_exception_hooks()
    diagnostics.flush()

    assert "qt-diagnostic-probe-311" in _log_text(diagnostics.log_path)


def test_reinstall_replaces_hooks_without_stacking(tmp_path: Path) -> None:
    first = configure_diagnostics(tmp_path / "one")
    second = configure_diagnostics(tmp_path / "two")
    install_exception_hooks(first)
    install_exception_hooks(second)
    try:
        try:
            raise RuntimeError("single write")
        except RuntimeError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
    finally:
        uninstall_exception_hooks()
    first.flush()
    second.flush()

    first_log = tmp_path / "one" / "diagnostics" / LOG_FILENAME
    if first_log.exists():
        assert "single write" not in first_log.read_text(encoding="utf-8")
    assert "single write" in _log_text(second.log_path)


class _FakeApplication(QObject):
    """QObject-backed stand-in: native_cad passes the app as the receiver
    of QTimer.singleShot, which requires a real QObject instance."""

    def __init__(self, argv: list[str]) -> None:
        super().__init__()
        self.argv = argv
        self.exit_code = 0

    def setWindowIcon(self, _icon) -> None:  # noqa: N802 - Qt API surface
        pass

    def setApplicationVersion(self, _version: str) -> None:  # noqa: N802 - Qt API surface
        pass

    def exec(self) -> int:
        return self.exit_code


class _FakeRepository:
    def __init__(self, path: Path) -> None:
        self.path = path


class _FakeProjectLibraryRepository:
    """Schema-bypassing stand-in: the real library verifies the migrated
    schema, which `_FakeRepository` deliberately never creates."""

    def __init__(self, _repository) -> None:
        pass

    def resolve_startup_document(self, document_id: str | None):
        return SimpleNamespace(document_id=document_id or F1_DOCUMENT_ID)


class _FakeWindow:
    def __init__(
        self, repository, document_id: str, project_library=None, **_kwargs
    ) -> None:
        self.repository = repository
        self.document_id = document_id
        self.shown = False

    def show(self) -> None:
        self.shown = True


def test_gui_startup_failure_is_logged_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    reported: list[dict] = []
    monkeypatch.setattr(
        native_cad, "report_launch_failure", lambda **kwargs: reported.append(kwargs)
    )
    monkeypatch.setattr(native_cad, "QApplication", _FakeApplication)
    # The bundled theme reaches real Qt font APIs that need a live QApplication.
    monkeypatch.setattr(native_cad, "apply_dark_theme", lambda _app: None)

    def _broken_repository(_path: Path) -> None:
        raise NativeSchemaError(
            "native database schema v99 is newer than this application supports"
        )

    monkeypatch.setattr(native_cad, "SceneRepository", _broken_repository)

    assert native_cad.main(["--data-dir", str(data_dir)]) == 1

    assert len(reported) == 1
    failure = reported[0]
    assert failure["title"] == "HTDT did not start"
    assert "schema v99" in failure["reason"]
    assert "\n" not in failure["reason"]
    assert failure["log_path"] == data_dir / "diagnostics" / LOG_FILENAME

    text = _log_text(failure["log_path"])
    assert "startup failed" in text
    assert "NativeSchemaError" in text
    assert "Traceback" in text
    assert f"version={__version__}" in text


def test_successful_gui_startup_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        native_cad,
        "report_launch_failure",
        lambda **kwargs: pytest.fail(f"unexpected failure report: {kwargs}"),
    )
    monkeypatch.setattr(native_cad, "QApplication", _FakeApplication)
    monkeypatch.setattr(native_cad, "SceneRepository", _FakeRepository)
    monkeypatch.setattr(native_cad, "apply_dark_theme", lambda _app: None)
    monkeypatch.setattr(native_cad, "OptimizationWorkspaceWindow", _FakeWindow)
    monkeypatch.setattr(native_cad, "build_workflow_shell", _FakeWindow)
    monkeypatch.setattr(
        native_cad,
        "ProjectLibraryRepository",
        _FakeProjectLibraryRepository,
    )

    assert native_cad.main(["--data-dir", str(data_dir)]) == 0

    text = _log_text(data_dir / "diagnostics" / LOG_FILENAME)
    assert "mode=gui" in text


def test_lock_contention_is_visible_for_gui_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    data_dir = tmp_path / "data"
    guard = SingleInstanceGuard(data_dir)
    assert guard.acquire()
    reported: list[dict] = []
    monkeypatch.setattr(
        native_cad, "report_launch_failure", lambda **kwargs: reported.append(kwargs)
    )
    try:
        assert native_cad.main(["--data-dir", str(data_dir)]) == 2
    finally:
        guard.release()

    assert len(reported) == 1
    assert reported[0]["title"] == "HTDT is already running"
    assert "already" in reported[0]["reason"]
    assert reported[0]["log_path"] == data_dir / "diagnostics" / LOG_FILENAME

    captured = capsys.readouterr()
    assert "already in use by another process" in captured.err

    text = _log_text(reported[0]["log_path"])
    assert "already in use by another process" in text


def test_lock_contention_stays_quiet_for_maintenance_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    data_dir = tmp_path / "data"
    guard = SingleInstanceGuard(data_dir)
    assert guard.acquire()
    monkeypatch.setattr(
        native_cad,
        "report_launch_failure",
        lambda **kwargs: pytest.fail("maintenance CLI must not open dialogs"),
    )
    try:
        assert native_cad.main(
            ["--data-dir", str(data_dir), "--seed-synthetic-demo"]
        ) == 2
    finally:
        guard.release()

    captured = capsys.readouterr()
    assert "already in use by another process" in captured.err


def test_maintenance_failure_exits_nonzero_without_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Maintenance CLI failures stay headless: deterministic exit code, no dialog."""

    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        native_cad,
        "report_launch_failure",
        lambda **kwargs: pytest.fail("maintenance CLI must not open dialogs"),
    )
    monkeypatch.setattr(
        native_cad,
        "QApplication",
        lambda *_args: pytest.fail("QApplication must not be created"),
    )

    assert (
        native_cad.main(
            ["--data-dir", str(data_dir), "--restore", str(tmp_path / "missing.htdt-backup")]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert "missing" in captured.err


def test_report_launch_failure_falls_back_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import PySide6.QtWidgets

    def _broken_application(*_args, **_kwargs):
        raise RuntimeError("Qt unavailable")

    monkeypatch.setattr(PySide6.QtWidgets, "QApplication", _broken_application)
    log_path = tmp_path / "data" / "diagnostics" / LOG_FILENAME

    report_launch_failure(
        title="HTDT did not start",
        reason="NativeSchemaError: schema v99",
        recovery="Start HTDT again.",
        log_path=log_path,
    )

    captured = capsys.readouterr()
    assert "HTDT did not start" in captured.err
    assert "NativeSchemaError: schema v99" in captured.err
    assert str(log_path) in captured.err
