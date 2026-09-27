"""Last-used-directory memory for native file dialogs (round-8 deferred).

Every ``QFileDialog`` call used to start at the process working directory
or ``Path.home()`` — the operator re-navigated the filesystem on every
open/save. This module keeps a small ``dialog key -> directory`` map in
one JSON file under the data dir (UI convenience state only, never
authority, never serialized into semantic hashes) and wraps the four
QFileDialog statics so call sites stay one line.

Lookup order for a dialog's initial directory:

1. the directory remembered for that dialog's own ``key``;
2. the directory remembered for the most recently used dialog
   (``GLOBAL_KEY``) — a dialog the operator has never seen still lands
   where they last worked;
3. the caller's ``default_dir``;
4. Qt's own default (empty string).

A successful pick updates both the dialog's own key and the global slot.
Cancelled dialogs write nothing. The composition binds the store once via
:func:`configure`; tests or embedders that skip it get an in-memory store
so behavior (dir memory within the process) still works.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from PySide6.QtWidgets import QFileDialog, QWidget


FILE_DIALOG_DIRS_FILENAME = 'file_dialog_dirs.json'
SCHEMA_VERSION = 1
GLOBAL_KEY = '__last__'


class FileDialogMemoryStore:
    """Dialog-key -> last directory map, atomically persisted as JSON.

    Same conventions as :class:`LibraryMetaStore`: corrupt or foreign
    content degrades to empty (a remembered dir is convenience, never
    evidence), writes go through tmp + fsync + ``os.replace``.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._dirs: dict[str, str] = {}
        self._load()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> 'FileDialogMemoryStore':
        return cls(Path(data_dir) / FILE_DIALOG_DIRS_FILENAME)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if (
            not isinstance(payload, dict)
            or payload.get('schema_version') != SCHEMA_VERSION
        ):
            return
        dirs = payload.get('dirs')
        if isinstance(dirs, dict):
            self._dirs = {
                str(key): str(value)
                for key, value in dirs.items()
                if isinstance(value, str) and value
            }

    def _persist(self) -> None:
        payload = {
            'schema_version': SCHEMA_VERSION,
            'authority': 'htdt-file-dialog-dirs',
            'dirs': self._dirs,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=self.path.name + '.',
            suffix='.tmp',
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(
                    json.dumps(payload, sort_keys=True, allow_nan=False)
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def recall(self, key: str) -> str:
        """Best initial directory for ``key`` ('' when nothing is known).

        A remembered path that no longer exists (deleted folder, detached
        drive) is skipped rather than offered to the picker; the entry is
        kept on disk so a reconnected drive works again.
        """
        for candidate in (self._dirs.get(key), self._dirs.get(GLOBAL_KEY)):
            if candidate and Path(candidate).is_dir():
                return candidate
        return ''

    def remember(self, key: str, directory: str) -> None:
        directory = str(directory)
        if not directory:
            return
        self._dirs[key] = directory
        self._dirs[GLOBAL_KEY] = directory
        self._persist()

    def remembered(self) -> dict[str, str]:
        return dict(self._dirs)


_active_store: FileDialogMemoryStore | None = None
_fallback_store: FileDialogMemoryStore | None = None


def configure(store: FileDialogMemoryStore) -> None:
    """Bind the process-wide store (called once by the composition)."""
    global _active_store
    _active_store = store


def active_store() -> FileDialogMemoryStore:
    """The configured store, or a process-local one when unconfigured."""
    global _fallback_store
    if _active_store is not None:
        return _active_store
    if _fallback_store is None:
        _fallback_store = FileDialogMemoryStore(
            Path(tempfile.gettempdir()) / 'htdt-file-dialog-dirs-fallback.json'
        )
    return _fallback_store


def _initial_dir(key: str, default_dir: str = '') -> str:
    return active_store().recall(key) or default_dir


def _note_file_selection(key: str, selected: str) -> None:
    if selected:
        active_store().remember(key, str(Path(selected).parent))


def _note_dir_selection(key: str, selected: str) -> None:
    if selected:
        active_store().remember(key, selected)


def get_open_file_name(
    parent: QWidget | None,
    caption: str,
    key: str,
    filter: str = '',  # noqa: A002 - mirrors the QFileDialog signature
    *,
    default_dir: str = '',
) -> tuple[str, str]:
    selected, selected_filter = QFileDialog.getOpenFileName(
        parent, caption, _initial_dir(key, default_dir), filter
    )
    _note_file_selection(key, selected)
    return selected, selected_filter


def get_open_file_names(
    parent: QWidget | None,
    caption: str,
    key: str,
    filter: str = '',  # noqa: A002
    *,
    default_dir: str = '',
) -> tuple[list[str], str]:
    selected, selected_filter = QFileDialog.getOpenFileNames(
        parent, caption, _initial_dir(key, default_dir), filter
    )
    if selected:
        _note_file_selection(key, selected[0])
    return selected, selected_filter


def get_save_file_name(
    parent: QWidget | None,
    caption: str,
    key: str,
    filter: str = '',  # noqa: A002
    *,
    suggested_name: str = '',
    default_dir: str = '',
) -> tuple[str, str]:
    directory = _initial_dir(key, default_dir)
    if suggested_name:
        directory = (
            str(Path(directory) / suggested_name) if directory else suggested_name
        )
    selected, selected_filter = QFileDialog.getSaveFileName(
        parent, caption, directory, filter
    )
    _note_file_selection(key, selected)
    return selected, selected_filter


def get_existing_directory(
    parent: QWidget | None,
    caption: str,
    key: str,
    *,
    default_dir: str = '',
) -> str:
    selected = QFileDialog.getExistingDirectory(
        parent, caption, _initial_dir(key, default_dir)
    )
    _note_dir_selection(key, selected)
    return selected


__all__ = [
    'FILE_DIALOG_DIRS_FILENAME',
    'FileDialogMemoryStore',
    'GLOBAL_KEY',
    'SCHEMA_VERSION',
    'active_store',
    'configure',
    'get_existing_directory',
    'get_open_file_name',
    'get_open_file_names',
    'get_save_file_name',
]
