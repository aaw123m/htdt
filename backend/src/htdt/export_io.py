"""Atomic writers for user-facing export artifacts.

The repo already publishes its handoff package, project bundle and backups
through staged-then-promoted writes; these helpers give the smaller export
paths the same guarantee without duplicating the pattern per call site.

- :func:`write_bytes_atomic` / :func:`write_text_atomic` publish a single
  file: a crash mid-write leaves the previous file (or no file), never a
  truncated artifact.
- :func:`write_export_generation` publishes a coherent file GROUP as one
  generation directory ``<base>[-N]/``: members are staged, hashed into
  ``manifest.json``, then the staging directory is renamed into place —
  a single publication point. Two concurrent writers can never collide
  (the staging ``mkdir`` is the reservation), a killed writer leaves a
  diagnosable ``*.export-staging`` directory instead of a partial
  generation, and a finished generation is never overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterator


_STAGING_SUFFIX = '.export-staging'
_MANIFEST_NAME = 'manifest.json'
_MANIFEST_SCHEMA = 'export-generation/v1'


@dataclass(frozen=True, slots=True)
class ExportGeneration:
    """One published export generation directory."""

    stem: str
    directory: Path
    members: tuple[Path, ...]
    manifest: dict


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


def _check_member_name(name: str) -> None:
    if (
        not name
        or name in ('.', '..', _MANIFEST_NAME)
        or '/' in name
        or '\\' in name
        or ':' in name
    ):
        raise ValueError(f'invalid export member name: {name!r}')


def write_export_generation(
    directory: str | Path,
    base: str,
    files: dict[str, str | bytes],
    *,
    bom_suffixes: tuple[str, ...] = (),
    manifest_extra: dict | None = None,
) -> ExportGeneration:
    """Publish ``{name: text|bytes}`` as one generation ``<base>[-N]/``.

    The reservation is the atomic ``os.mkdir`` of ``<stem>.export-staging``
    itself: whichever writer creates it owns that stem — a ``exists()``
    pre-check could not give that guarantee. A stem is skipped when its
    staging dir, final dir, or any legacy flat member (``<stem>_*``) is
    present, so numbering stays monotonic across old and new layouts and
    a crashed generation's evidence is never silently reused.

    After staging, every member's bytes are hashed into ``manifest.json``
    and the staging directory is renamed to ``<stem>/`` — the single
    publication point. On any failure the staging tree is removed and the
    exception propagates: the final name is only ever a complete
    generation. ``manifest_extra`` pins caller-side identity (export id,
    source sha) into the manifest.
    """

    if not base or '/' in base or '\\' in base:
        raise ValueError(f'invalid export base name: {base!r}')
    if not files:
        raise ValueError('export generation requires at least one member')
    for name in files:
        _check_member_name(name)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    staging: Path | None = None
    stem = ''
    index = 0
    while staging is None:
        index += 1
        stem = base if index == 1 else f'{base}-{index}'
        candidate = directory / f'{stem}{_STAGING_SUFFIX}'
        try:
            os.mkdir(candidate)
        except FileExistsError:
            # Reserved by a live writer, or the abandoned staging of a
            # crashed generation — either way this stem stays claimed.
            continue
        if (directory / stem).exists() or any(
            directory.glob(f'{stem}_*')
        ):
            # Final generation dir, or a legacy flat member from the
            # pre-generation naming scheme — the stem is dead; release
            # the reservation we just took and move on.
            candidate.rmdir()
            continue
        staging = candidate

    final = directory / stem
    try:
        members: dict[str, dict] = {}
        for name, content in files.items():
            target = staging / name
            if isinstance(content, str):
                encoding = (
                    'utf-8-sig' if name.endswith(bom_suffixes) else 'utf-8'
                )
                write_text_atomic(target, content, encoding=encoding)
            else:
                write_bytes_atomic(target, content)
            data = target.read_bytes()
            members[name] = {
                'sha256': hashlib.sha256(data).hexdigest(),
                'size': len(data),
            }
        manifest = {
            'schema': _MANIFEST_SCHEMA,
            'stem': stem,
            'created_utc': datetime.now(timezone.utc).isoformat(),
            'member_count': len(members),
            'members': members,
        }
        if manifest_extra:
            manifest['extra'] = manifest_extra
        write_text_atomic(
            staging / _MANIFEST_NAME,
            json.dumps(
                manifest, ensure_ascii=False, indent=2, sort_keys=True
            )
            + '\n',
        )
        if final.exists():
            # Another actor squatted the final name between reservation
            # and publication — fail closed, our staging is cleaned.
            raise FileExistsError(
                f'export generation already exists: {final.name}'
            )
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return ExportGeneration(
        stem=stem,
        directory=final,
        members=tuple(final / name for name in files),
        manifest=manifest,
    )


def find_export_staging(directory: str | Path) -> tuple[Path, ...]:
    """Unpublished ``*.export-staging`` dirs — crashed/killed generations.

    These are evidence, not garbage: callers surface them for diagnosis,
    and :func:`cleanup_export_staging` removes them explicitly. Do not run
    cleanup while exports into the same directory may be in flight — a
    staging dir is also a live writer's reservation.
    """

    directory = Path(directory)
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            p
            for p in directory.iterdir()
            if p.is_dir() and p.name.endswith(_STAGING_SUFFIX)
        )
    )


def cleanup_export_staging(directory: str | Path) -> tuple[Path, ...]:
    """Remove every ``*.export-staging`` dir; return what was removed."""

    removed = []
    for path in find_export_staging(directory):
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path)
    return tuple(removed)


def iter_export_generations(
    directory: str | Path, base: str
) -> Iterator[ExportGeneration]:
    """Enumerate published ``<base>[-N]`` generation dirs with manifests."""

    directory = Path(directory)
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if not path.is_dir():
            continue
        if path.name != base and not path.name.startswith(f'{base}-'):
            continue
        manifest_path = path / _MANIFEST_NAME
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        yield ExportGeneration(
            stem=path.name,
            directory=path,
            members=tuple(path / name for name in manifest['members']),
            manifest=manifest,
        )


def verify_export_generation(path: str | Path) -> dict:
    """Re-check a published generation against its manifest.

    Returns the manifest on success. Raises ``ValueError`` when the
    manifest is missing/foreign, a member is missing or hash-mismatched,
    or an unmanifested file was added — a tampered or partial generation
    is never reported as sound.
    """

    path = Path(path)
    manifest_path = path / _MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(f'export generation manifest missing: {path.name}')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('schema') != _MANIFEST_SCHEMA:
        raise ValueError(f'not an export generation: {path.name}')
    members = manifest.get('members') or {}
    if manifest.get('member_count') != len(members):
        raise ValueError(
            f'export generation member count mismatch: {path.name}'
        )
    for name, record in members.items():
        member = path / name
        if not member.is_file():
            raise ValueError(
                f'export generation member missing: {path.name}/{name}'
            )
        data = member.read_bytes()
        if (
            len(data) != record['size']
            or hashlib.sha256(data).hexdigest() != record['sha256']
        ):
            raise ValueError(
                f'export generation member corrupt: {path.name}/{name}'
            )
    extras = {
        p.name
        for p in path.iterdir()
        if p.is_file() and p.name not in members and p.name != _MANIFEST_NAME
    }
    if extras:
        raise ValueError(
            f'export generation has unmanifested files: '
            + ', '.join(sorted(extras))
        )
    return manifest


__all__ = [
    'ExportGeneration',
    'cleanup_export_staging',
    'find_export_staging',
    'iter_export_generations',
    'verify_export_generation',
    'write_bytes_atomic',
    'write_export_generation',
    'write_text_atomic',
]
