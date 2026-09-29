"""Atomic writers for user-facing export artifacts.

The repo already publishes its handoff package, project bundle and backups
through staged-then-promoted writes; these helpers give the smaller export
paths the same guarantee without duplicating the pattern per call site.

- :func:`write_bytes_atomic` / :func:`write_text_atomic` publish a single
  file: a crash mid-write leaves the previous file (or no file), never a
  truncated artifact.
- :func:`write_export_files` publishes a coherent file group whose names
  must not already exist (claim them with :func:`claim_export_stem`):
  on failure the members it created are removed, so a failed generation
  leaves no partial junk and never touches a previous one.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


def write_bytes_atomic(path: str | Path, data: bytes) -> Path:
    """Write ``data`` to ``path`` via a sibling temp file + fsync + replace."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return path


def write_text_atomic(
    path: str | Path, content: str, *, encoding: str = 'utf-8'
) -> Path:
    return write_bytes_atomic(path, content.encode(encoding))


def claim_export_stem(
    directory: str | Path, base: str, suffixes: tuple[str, ...]
) -> str:
    """First ``base``/``base-N`` stem for which no suffixed name exists.

    Multi-file exports share one stem; colliding with a previous
    generation must pick a fresh stem rather than silently overwrite it.
    """

    directory = Path(directory)
    stem = base
    index = 1
    while any(
        (directory / f'{stem}{suffix}').exists() for suffix in suffixes
    ):
        index += 1
        stem = f'{base}-{index}'
    return stem


def write_export_files(
    directory: str | Path,
    files: dict[str, str],
    *,
    bom_suffixes: tuple[str, ...] = (),
) -> dict[str, Path]:
    """Publish ``{name: text}`` into ``directory`` as one generation.

    Every member is written atomically in dict order. Member names must
    not already exist — if any write fails, the members this call created
    are removed, so the directory keeps either the complete new
    generation or none of it.

    Members whose name ends with a ``bom_suffixes`` entry are written
    ``utf-8-sig``: spreadsheet applications open a BOM-less file under the
    host ANSI codepage and mojibake non-ASCII cells, while a BOM declares
    the encoding up front.
    """

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    claimed = {name: directory / name for name in files}
    existing = [path.name for path in claimed.values() if path.exists()]
    if existing:
        raise FileExistsError(
            'export target already exists: ' + ', '.join(sorted(existing))
        )
    written: dict[str, Path] = {}
    try:
        for name, content in files.items():
            encoding = 'utf-8-sig' if name.endswith(bom_suffixes) else 'utf-8'
            write_text_atomic(claimed[name], content, encoding=encoding)
            written[name] = claimed[name]
    except BaseException:
        for path in written.values():
            path.unlink(missing_ok=True)
        raise
    return written


__all__ = [
    'claim_export_stem',
    'write_bytes_atomic',
    'write_export_files',
    'write_text_atomic',
]
