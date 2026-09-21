from __future__ import annotations

from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import re
import sys
import threading
from types import TracebackType

from . import __version__


LOGGER_NAME = 'htdt.native'
DIAGNOSTICS_DIRNAME = 'diagnostics'
LOG_FILENAME = 'htdt-native.log'
LOG_FORMAT = '%(asctime)sZ %(levelname)s %(name)s: %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%dT%H:%M:%S'

# Rotation keeps on-disk diagnostics bounded even if a runtime fault loops.
MAX_LOG_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 4

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

_qt_message_handler = None
_qt_previous_handler = None


def diagnostics_dir(data_dir: Path) -> Path:
    return Path(data_dir) / DIAGNOSTICS_DIRNAME


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


@dataclass(frozen=True)
class BuildIdentity:
    """Source build identity attached to diagnostics records."""

    version: str
    python: str
    platform: str
    frozen: bool
    qt: str | None

    def describe(self) -> str:
        parts = [
            f'version={self.version}',
            f'python={self.python}',
            f'platform={self.platform}',
            f'frozen={self.frozen}',
        ]
        if self.qt:
            parts.append(f'qt={self.qt}')
        return ' '.join(parts)


def build_identity() -> BuildIdentity:
    qt_version: str | None = None
    try:
        import PySide6

        qt_version = PySide6.__version__
    except Exception:
        qt_version = None
    return BuildIdentity(
        version=__version__,
        python=platform.python_version(),
        platform=platform.platform(),
        frozen=bool(getattr(sys, 'frozen', False)),
        qt=qt_version,
    )


def write_stderr(message: str) -> None:
    """Write to stderr when it exists; a windowed executable may have none."""

    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(message + '\n')
        stream.flush()
    except Exception:
        pass


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
            except Exception:
                pass

    def log_session_start(self, mode: str) -> None:
        self.logger.info('htdt session start: mode=%s %s', mode, self.identity.describe())

    def log_lock_contention(self) -> None:
        self.logger.warning('data directory is already in use by another process: %s', self.data_dir)

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
            except Exception:
                pass


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
        except Exception:
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
    except Exception:
        return
    log = diagnostics.logger

    def _handler(mode: object, context: object, message: str) -> None:
        try:
            log.log(_qt_log_level(mode), 'qt: %s', message)
        except Exception:
            pass
        if _qt_previous_handler is not None:
            try:
                _qt_previous_handler(mode, context, message)
            except Exception:
                pass
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
        except Exception:
            pass
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
        except Exception:
            pass
        if previous_sys is not None:
            try:
                previous_sys(exc_type, exc, tb)
            except Exception:
                pass

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
        except Exception:
            pass
        if previous_threading is not None:
            try:
                previous_threading(args)
            except Exception:
                pass

    _thread_hook._htdt_previous_hook = previous_threading  # type: ignore[attr-defined]
    threading.excepthook = _thread_hook

    _install_qt_message_handler(diagnostics)


def concise_reason(exc: BaseException, *, max_chars: int = 300) -> str:
    """One-line, user-safe summary of an exception for the failure dialog."""

    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    first_line = text.splitlines()[0].strip() or type(exc).__name__
    if len(first_line) > max_chars:
        first_line = first_line[:max_chars].rstrip() + '…'
    return f'{type(exc).__name__}: {first_line}'


def report_launch_failure(
    *,
    title: str,
    reason: str,
    recovery: str,
    log_path: Path | None,
) -> None:
    """Show a concise visible failure; full detail stays in the diagnostics log.

    Falls back to stderr when Qt cannot present a dialog (for example when the
    display layer itself is what failed).
    """

    sections = [reason]
    if recovery:
        sections.append(recovery)
    if log_path is not None:
        sections.append(f'Diagnostic log: {log_path}')
    message = '\n\n'.join(sections)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance()
        if app is None:
            app = QApplication([sys.argv[0]])
        QMessageBox.critical(None, title, message)
        return
    except Exception:
        pass
    write_stderr(f'{title}\n{message}')
