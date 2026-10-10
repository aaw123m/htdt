"""Diagnostics paths and build identity — Qt-free domain helpers (#807).

``native_diagnostics`` is the Qt-side application root (exception hooks, the
Qt message handler, status-bar surfacing). The pieces domain modules need —
log locations, the build identity record, one-line failure reasons — are
presentation-free plumbing and live here. ``native_diagnostics`` re-exports
the constants it builds on, so existing ``native_diagnostics`` imports keep
working, while support/diagnostic domain modules take the Qt-free path.

``build_identity`` resolves the Qt version through ``importlib.metadata``
(the installed distribution), never by importing PySide6, so this module
stays importable before Qt exists and keeps the layer audit Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
from pathlib import Path
import platform
import sys

from . import __version__


DIAGNOSTICS_DIRNAME = 'diagnostics'
LOG_FILENAME = 'htdt-native.log'
LOG_FORMAT = '%(asctime)sZ %(levelname)s %(name)s: %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%dT%H:%M:%S'

# Rotation keeps on-disk diagnostics bounded even if a runtime fault loops.
MAX_LOG_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 4


def diagnostics_dir(data_dir: Path) -> Path:
    return Path(data_dir) / DIAGNOSTICS_DIRNAME


def _qt_version() -> str | None:
    try:
        return importlib.metadata.version('PySide6')
    except importlib.metadata.PackageNotFoundError:
        return None


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
    return BuildIdentity(
        version=__version__,
        python=platform.python_version(),
        platform=platform.platform(),
        frozen=bool(getattr(sys, 'frozen', False)),
        qt=_qt_version(),
    )


def concise_reason(exc: BaseException, *, max_chars: int = 300) -> str:
    """One-line, user-safe summary of an exception for the failure dialog."""

    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    first_line = text.splitlines()[0].strip() or type(exc).__name__
    if len(first_line) > max_chars:
        first_line = first_line[:max_chars].rstrip() + '…'
    return f'{type(exc).__name__}: {first_line}'


__all__ = [
    'DIAGNOSTICS_DIRNAME',
    'LOG_BACKUP_COUNT',
    'LOG_DATE_FORMAT',
    'LOG_FILENAME',
    'LOG_FORMAT',
    'MAX_LOG_BYTES',
    'BuildIdentity',
    'build_identity',
    'concise_reason',
    'diagnostics_dir',
]
