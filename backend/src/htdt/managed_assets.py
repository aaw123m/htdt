"""Shared managed content-addressed asset contract.

``cad_measurement_assets`` rows bind a raw measurement/treatment source
asset — content-addressed by SHA-256 — to a managed relative path under the
native data directory. Two authorities consume those rows:

- the native backup/restore path (``native_backup``), which archives,
  validates and restores the whole managed data directory;
- the N60 runtime measurement authority (``cad_measurement_repository``),
  which treats ``dataset.source_sha256`` as immutable raw-backed evidence.

Both enforce the same per-asset contract through ``verify_managed_asset``
so the integrity boundary cannot diverge between the backup path and
runtime evidence reads: a row must name a safe relative path contained
under the managed data root, resolve to an existing regular file (never a
symlink), match the stored size, and stream a SHA-256 equal to the
content-addressed digest. Any gap fails closed with ``ManagedAssetError``.
"""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path, PurePosixPath


class ManagedAssetError(ValueError):
    """A managed content-addressed asset violated its storage contract."""


def sha256_file(path: Path) -> str:
    """Stream a file's SHA-256 without loading it into memory."""
    digest = sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_data_path(path: Path) -> Path:
    """Canonical absolute path for managed-data identity comparisons.

    resolve() follows symlinks and junctions and collapses dot segments even
    for missing leaves; normcase() additionally folds case on
    case-insensitive filesystems (Windows), so differently-spelled aliases
    compare equal.
    """
    return Path(os.path.normcase(str(path.expanduser().resolve())))


def safe_managed_relative_path(value: str) -> PurePosixPath:
    """Require a managed asset ``relative_path`` to be a safe relative path.

    Windows separators written by older writers are normalized first so the
    same row validates on every platform. Absolute paths, drive-qualified
    roots, empty segments and ``.``/``..`` segments are rejected.
    """
    normalized = value.replace('\\', '/')
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {'', '.', '..'} for part in path.parts)
    ):
        raise ManagedAssetError(f'unsafe managed asset path: {value}')
    if ':' in path.parts[0]:
        raise ManagedAssetError(f'unsafe managed asset path: {value}')
    return path


def managed_asset_path(data_dir: Path, relative_path: str) -> Path:
    """Resolve a managed asset ``relative_path`` under *data_dir*.

    The containment check runs on resolved paths so a symlinked ancestor
    cannot smuggle the target outside the managed data root. The unresolved
    target is returned so callers can still detect a symlink at the leaf
    itself (``Path.resolve()`` would silently follow it).
    """
    safe_path = safe_managed_relative_path(relative_path)
    target = data_dir.joinpath(*safe_path.parts)
    root = data_dir.resolve()
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ManagedAssetError(
            f'managed asset path escapes the managed data root: {relative_path}'
        ) from exc
    return target


def verify_managed_asset(
    *,
    data_dir: Path,
    digest: str,
    relative_path: str,
    size_bytes: int,
    required_root: Path | None = None,
) -> Path:
    """Verify one managed content-addressed asset row against the filesystem.

    Enforces, in order: the digest is a well-formed SHA-256, the declared
    relative path is safe and contained under *data_dir* (and under
    *required_root* when given — the runtime measurement authority pins its
    managed assets directory), the target is an existing regular file and
    not a symlink, its size matches the stored ``size_bytes``, and its
    streamed SHA-256 equals *digest*.

    Returns the verified on-disk path. Every violation raises
    ``ManagedAssetError`` so callers fail closed instead of treating a
    broken asset as usable evidence.
    """
    if len(digest) != 64 or any(
        char not in '0123456789abcdef' for char in digest
    ):
        raise ManagedAssetError(
            f'invalid measurement asset digest in database: {digest}'
        )
    asset_path = managed_asset_path(data_dir, relative_path)
    if required_root is not None:
        try:
            asset_path.resolve().relative_to(required_root.resolve())
        except ValueError as exc:
            raise ManagedAssetError(
                'measurement asset path escapes the managed assets '
                f'directory: {relative_path}'
            ) from exc
    if asset_path.is_symlink() or not asset_path.is_file():
        raise ManagedAssetError(
            f'measurement asset is missing or not a regular file: '
            f'{relative_path}'
        )
    if asset_path.stat().st_size != size_bytes:
        raise ManagedAssetError(
            f'measurement asset size mismatch: {relative_path}'
        )
    if sha256_file(asset_path) != digest:
        raise ManagedAssetError(
            f'measurement asset SHA-256 mismatch: {relative_path}'
        )
    return asset_path
