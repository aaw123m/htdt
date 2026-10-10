from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import re
import sys
import threading
from types import TracebackType

from .modal_transient import exec_transient
from . import __version__
from .diagnostics_support import (
    DIAGNOSTICS_DIRNAME,
    LOG_BACKUP_COUNT,
    LOG_DATE_FORMAT,
    LOG_FILENAME,
    LOG_FORMAT,
    MAX_LOG_BYTES,
    BuildIdentity,
    build_identity,
    concise_reason,
    diagnostics_dir,
)


LOGGER_NAME = 'htdt.native'

# Records carry concise lifecycle/error metadata only. The per-record bound and
# the credential scrubber keep raw measurement payloads and secrets out of the
# durable log even if a caller accidentally forwards an oversized or sensitive
# message.
MAX_LOG_RECORD_CHARS = 64 * 1024
REDACTED = '***'

_BEARER_PATTERN = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+')
_SECRET_VALUE = r'("[^"\n]*"|\'[^\'\n]*\'|[^\s,;}\'"]+)'
_SECRET_KEY_PATTERN = re.compile(
    r'(?i)\b('
    r'passwords?|passwds?|secrets?|tokens?|api[-_]?keys?|apikeys?|authorizations?|credentials?'
    r')\b(\s*["\']?\s*[=:]\s*)' + _SECRET_VALUE
)

_HandlerMarker = '_htdt_diagnostics'
_PREVIOUS_HOOK_ATTR = '_htdt_previous_hook'

_LOGGER = logging.getLogger(LOGGER_NAME)

_qt_message_handler = None
_qt_previous_handler = None

#: Persistent-surface sinks for uncaught exceptions (round10). Each live
#: application composition pushes one sink so the error lands on *its*
#: Activity Center; the most recently registered sink receives the report
#: and routes it to the window that surfaced the failure. A sink must be
#: non-blocking and must never raise — it runs inside ``sys.excepthook``.
UncaughtSink = Callable[
    ['NativeDiagnostics', BaseException, object | None], None
]
_uncaught_sinks: list[UncaughtSink] = []


def push_uncaught_sink(sink: UncaughtSink) -> Callable[[], None]:
    """Register a persistent-surface sink; the return value removes it.

    Sinks are invoked on the GUI thread only, after the transient status-bar
    notice. The removal handle is idempotent so a closing window can clear
    its own registration without disturbing other compositions.
    """
    _uncaught_sinks.append(sink)

    def _pop() -> None:
        try:
            _uncaught_sinks.remove(sink)
        except ValueError:
            pass

    return _pop


def _sanitize(text: str) -> str:
    text = _BEARER_PATTERN.sub(f'Bearer {REDACTED}', text)
    text = _SECRET_KEY_PATTERN.sub(lambda match: f'{match.group(1)}{match.group(2)}{REDACTED}', text)
    if len(text) > MAX_LOG_RECORD_CHARS:
        text = text[:MAX_LOG_RECORD_CHARS] + '…[truncated]'
    return text


class _DiagnosticsFormatter(logging.Formatter):
    """Formats records, then scrubs credentials and bounds record size."""

    def format(self, record: logging.LogRecord) -> str:
        return _sanitize(super().format(record))


def write_stderr(message: str) -> None:
    """Write to stderr when it exists; a windowed executable may have none."""

    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(message + '\n')
        stream.flush()
    except Exception:  # error-boundary: stderr is the last-resort channel — a write failure has no lower surface; identity is logged (noqa: BLE001)
        _LOGGER.exception('stderr write failed')


class NativeDiagnostics:
    """Bounded native diagnostics sink for one process launch."""

    def __init__(
        self,
        data_dir: Path,
        log_path: Path | None,
        logger: logging.Logger,
        identity: BuildIdentity,
    ) -> None:
        self.data_dir = data_dir
        self.log_path = log_path
        self.logger = logger
        self.identity = identity

    def flush(self) -> None:
        for handler in self.logger.handlers:
            try:
                handler.flush()
            except Exception:  # error-boundary: per-handler flush sweep — one broken handler must not stop the rest; identity is logged (noqa: BLE001)
                _LOGGER.exception('diagnostics handler flush failed')

    def log_session_start(self, mode: str) -> None:
        self.logger.info('htdt session start: mode=%s %s', mode, self.identity.describe())

    def log_lock_contention(self, holder: dict[str, object] | None = None) -> None:
        detail = ''
        if holder:
            detail = ' (holder: pid=%s host=%s acquired_at=%s)' % (
                holder.get('pid'),
                holder.get('host'),
                holder.get('acquired_at'),
            )
        self.logger.warning(
            'data directory is already in use by another process: %s%s',
            self.data_dir,
            detail,
        )

    def log_startup_failure(self, exc: BaseException) -> None:
        self.logger.critical(
            'startup failed (%s)',
            self.identity.describe(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    def log_uncaught(
        self,
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
        *,
        context: str = 'python',
    ) -> None:
        self.logger.critical(
            'uncaught %s exception (%s)',
            context,
            self.identity.describe(),
            exc_info=(exc_type, exc, tb),
        )


def _drop_diagnostics_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, _HandlerMarker, False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # error-boundary: handler teardown sweep — every marked handler must close; identity is logged (noqa: BLE001)
                _LOGGER.exception('diagnostics handler close failed')


def configure_diagnostics(
    data_dir: Path,
    *,
    max_bytes: int = MAX_LOG_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
    logger: logging.Logger | None = None,
) -> NativeDiagnostics:
    """Install the bounded rotating diagnostics sink.

    Best-effort by contract: diagnostics must never keep the application from
    starting, so an unwritable diagnostics directory degrades to log_path=None
    instead of raising.
    """

    log = logger or logging.getLogger(LOGGER_NAME)
    identity = build_identity()
    _drop_diagnostics_handlers(log)
    log_path = diagnostics_dir(data_dir) / LOG_FILENAME
    handler: RotatingFileHandler | None = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8',
            delay=True,
        )
    except OSError:
        log_path = None
    if handler is not None:
        setattr(handler, _HandlerMarker, True)
        handler.setFormatter(_DiagnosticsFormatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return NativeDiagnostics(Path(data_dir), log_path, log, identity)


_QT_LEVELS: dict[object, int] = {}


def _qt_log_level(mode: object) -> int:
    if not _QT_LEVELS:
        try:
            from PySide6.QtCore import QtMsgType
        except ImportError:  # error-boundary: optional-dependency probe — Qt absent keeps the default level; a broken import propagates
            return logging.INFO
        _QT_LEVELS.update(
            {
                QtMsgType.QtDebugMsg: logging.DEBUG,
                QtMsgType.QtInfoMsg: logging.INFO,
                QtMsgType.QtWarningMsg: logging.WARNING,
                QtMsgType.QtCriticalMsg: logging.ERROR,
                QtMsgType.QtFatalMsg: logging.CRITICAL,
            }
        )
    return _QT_LEVELS.get(mode, logging.INFO)


def _install_qt_message_handler(diagnostics: NativeDiagnostics) -> None:
    global _qt_message_handler, _qt_previous_handler
    try:
        from PySide6.QtCore import qInstallMessageHandler
    except ImportError:  # error-boundary: optional-dependency probe — Qt absent leaves the default handler; a broken import propagates
        return
    log = diagnostics.logger

    def _handler(mode: object, context: object, message: str) -> None:
        try:
            log.log(_qt_log_level(mode), 'qt: %s', message)
        except Exception as exc:  # error-boundary: Qt message dispatch — a logging failure inside the handler must never recurse through Qt; identity goes to the last-resort channel (noqa: BLE001)
            write_stderr(f'qt message logging failed: {type(exc).__name__}: {exc}')
        if _qt_previous_handler is not None:
            try:
                _qt_previous_handler(mode, context, message)
            except Exception as exc:  # error-boundary: Qt message dispatch — the previous handler's failure must not break Qt's message path; identity goes to the last-resort channel (noqa: BLE001)
                write_stderr(f'previous qt message handler failed: {type(exc).__name__}: {exc}')
        else:
            # The Qt default handler prints to stderr; keep that behavior.
            write_stderr(f'qt: {message}')

    _qt_previous_handler = qInstallMessageHandler(_handler)
    _qt_message_handler = _handler


def uninstall_exception_hooks() -> None:
    """Restore hooks captured by a previous install; safe to call repeatedly."""

    global _qt_message_handler, _qt_previous_handler
    previous = getattr(sys.excepthook, _PREVIOUS_HOOK_ATTR, None)
    if previous is not None:
        sys.excepthook = previous
    previous_thread = getattr(threading.excepthook, _PREVIOUS_HOOK_ATTR, None)
    if previous_thread is not None:
        threading.excepthook = previous_thread
    if _qt_message_handler is not None:
        try:
            from PySide6.QtCore import qInstallMessageHandler

            qInstallMessageHandler(_qt_previous_handler)
        except Exception:  # error-boundary: hook teardown — uninstall must complete even when the Qt restore fails; identity is logged (noqa: BLE001)
            _LOGGER.exception('qt message handler restore failed')
        _qt_message_handler = None
        _qt_previous_handler = None


def install_exception_hooks(diagnostics: NativeDiagnostics) -> None:
    """Route uncaught Python, thread, and Qt failures to the diagnostics log.

    Hooks chain to the previously installed hooks so stderr/console behavior is
    preserved in development. Re-installing replaces an earlier HTDT hook rather
    than stacking duplicates.
    """

    uninstall_exception_hooks()

    previous_sys = sys.excepthook

    def _sys_hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        try:
            diagnostics.log_uncaught(exc_type, exc, tb)
        except Exception as log_exc:  # error-boundary: the uncaught recorder itself — a logging failure must not mask the original exception; identity goes to the last-resort channel (noqa: BLE001)
            write_stderr(f'uncaught logging failed: {type(log_exc).__name__}: {log_exc}')
        _surface_uncaught_on_statusbar(diagnostics)
        _post_uncaught_to_sink(diagnostics, exc)
        if previous_sys is not None:
            try:
                previous_sys(exc_type, exc, tb)
            except Exception as prev_exc:  # error-boundary: excepthook chain — the previous hook's failure must not mask the exception being reported; identity goes to the last-resort channel (noqa: BLE001)
                write_stderr(f'previous excepthook failed: {type(prev_exc).__name__}: {prev_exc}')

    _sys_hook._htdt_previous_hook = previous_sys  # type: ignore[attr-defined]
    sys.excepthook = _sys_hook

    previous_threading = threading.excepthook

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        try:
            thread = args.thread
            context = f'thread {thread.name}' if thread is not None else 'thread'
            diagnostics.log_uncaught(
                args.exc_type, args.exc_value, args.exc_traceback, context=context
            )
        except Exception as log_exc:  # error-boundary: the uncaught recorder itself — a logging failure must not mask the thread failure being reported; identity goes to the last-resort channel (noqa: BLE001)
            write_stderr(f'uncaught thread logging failed: {type(log_exc).__name__}: {log_exc}')
        if previous_threading is not None:
            try:
                previous_threading(args)
            except Exception as prev_exc:  # error-boundary: thread excepthook chain — the previous hook's failure must not mask the thread failure being reported; identity goes to the last-resort channel (noqa: BLE001)
                write_stderr(f'previous threading excepthook failed: {type(prev_exc).__name__}: {prev_exc}')

    _thread_hook._htdt_previous_hook = previous_threading  # type: ignore[attr-defined]
    threading.excepthook = _thread_hook

    _install_qt_message_handler(diagnostics)


def _surface_uncaught_on_statusbar(diagnostics: NativeDiagnostics) -> None:
    """Best-effort non-modal notice that an uncaught exception was logged.

    PySide6 routes exceptions escaping a slot to ``sys.excepthook``; without
    this notice the operator sees nothing at all — the failure only exists
    in the diagnostics log. Runs only on the GUI thread with a live window;
    worker-thread exceptions stay log-only (their owner surfaces them via
    the ``completed`` error channel).
    """
    try:
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication
    except ImportError:  # error-boundary: optional-dependency probe — Qt absent leaves this path log-only; a broken import propagates
        return
    try:
        app = QApplication.instance()
        if app is None or QThread.currentThread() is not app.thread():
            return
        window = app.activeWindow()
        if window is None:
            window = next(
                (
                    widget
                    for widget in app.topLevelWidgets()
                    if widget.isVisible()
                ),
                None,
            )
        if window is None or not hasattr(window, 'statusBar'):
            return
        where = (
            f'詳細: {diagnostics.log_path}'
            if diagnostics.log_path is not None
            else '詳細は診断ログを確認してください'
        )
        window.statusBar().showMessage(
            f'予期しないエラーが発生しました（{where}）', 10000
        )
    except Exception:  # error-boundary: best-effort status notice — a surfacing failure inside the uncaught path must not mask the original error; identity is logged (noqa: BLE001)
        _LOGGER.exception('uncaught-exception status notice failed')


def _post_uncaught_to_sink(
    diagnostics: NativeDiagnostics, exc: BaseException
) -> None:
    """Hand the uncaught exception to the registered persistent surface.

    Same GUI-thread gate as the status-bar notice: the sink posts to an
    Activity Center (not thread-safe), so worker-thread exceptions keep
    their log-only contract — their owner surfaces them via the completed
    channel. The window the notice surfaced on is passed through so the
    sink can route the entry to that window's Activity Center.
    """
    if not _uncaught_sinks:
        return
    try:
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication
    except ImportError:  # error-boundary: optional-dependency probe — Qt absent leaves this path log-only; a broken import propagates
        return
    try:
        app = QApplication.instance()
        if app is None or QThread.currentThread() is not app.thread():
            return
        window = app.activeWindow()
        if window is None:
            window = next(
                (
                    widget
                    for widget in app.topLevelWidgets()
                    if widget.isVisible()
                ),
                None,
            )
        _uncaught_sinks[-1](diagnostics, exc, window)
    except Exception:  # error-boundary: sink dispatch — a broken Activity Center sink must not break the uncaught path; identity is logged (noqa: BLE001)
        _LOGGER.exception('uncaught-exception sink failed')


def report_launch_failure(
    *,
    title: str,
    reason: str,
    recovery: str,
    log_path: Path | None,
    technical_detail: str | None = None,
) -> None:
    """Show a concise visible failure; full detail stays in the diagnostics log.

    ``technical_detail`` (exception class/text) is presented collapsed under
    the dialog's Details expander — the primary message stays a localized
    reason + recovery. Falls back to stderr when Qt cannot present a dialog
    (for example when the display layer itself is what failed).
    """

    sections = [reason]
    if recovery:
        sections.append(recovery)
    if log_path is not None:
        sections.append(f'診断ログ: {log_path}')
    message = '\n\n'.join(sections)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance()
        if app is None:
            app = QApplication([sys.argv[0]])
        box = QMessageBox(
            QMessageBox.Icon.Critical,
            title,
            message,
            QMessageBox.StandardButton.Ok,
        )
        if technical_detail:
            box.setDetailedText(technical_detail)
        exec_transient(box)
        return
    except Exception:  # error-boundary: last-resort reporter — any dialog failure falls back to stderr so the launch failure is never lost; identity is logged (noqa: BLE001)
        _LOGGER.exception('launch failure dialog unavailable; falling back to stderr')
    if technical_detail:
        write_stderr(f'{title}\n{message}\n{technical_detail}')
    else:
        write_stderr(f'{title}\n{message}')
