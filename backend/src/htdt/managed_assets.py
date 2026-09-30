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

from collections.abc import Callable
from hashlib import sha256
import os
import tempfile
import time

from pathlib import Path, PurePosixPath

from .ingress import read_file_bounded
from .limits import MAX_ATTACHMENT_BYTES


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


def _resolve_managed_asset_path(
    *,
    data_dir: Path,
    digest: str,
    relative_path: str,
    size_bytes: int,
    required_root: Path | None = None,
) -> Path:
    """Digest shape + safe-path + containment + file-existence checks.

    Shared by ``verify_managed_asset`` (hash stream only) and
    ``read_managed_asset_verified`` (one read that returns the bytes).
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
    return asset_path


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
    asset_path = _resolve_managed_asset_path(
        data_dir=data_dir,
        digest=digest,
        relative_path=relative_path,
        size_bytes=size_bytes,
        required_root=required_root,
    )
    if sha256_file(asset_path) != digest:
        raise ManagedAssetError(
            f'measurement asset SHA-256 mismatch: {relative_path}'
        )
    return asset_path


def read_managed_asset_verified(
    *,
    data_dir: Path,
    digest: str,
    relative_path: str,
    size_bytes: int,
    required_root: Path | None = None,
    read: Callable[[Path], bytes] | None = None,
) -> tuple[Path, bytes]:
    """``verify_managed_asset`` contract that also returns the bytes.

    Identical checks, one disk pass: the returned bytes are exactly the
    bytes that hashed to *digest* — strictly stronger than stream-hash-then-
    reread (no window between the two reads) at half the I/O. ``read`` lets
    the caller keep its resilient reader (e.g. the store's Windows
    replace-race retry) without changing the verification contract.
    """
    asset_path = _resolve_managed_asset_path(
        data_dir=data_dir,
        digest=digest,
        relative_path=relative_path,
        size_bytes=size_bytes,
        required_root=required_root,
    )
    reader = read or (
        lambda target: read_file_bounded(
            target, MAX_ATTACHMENT_BYTES, label='managed asset'
        )
    )
    raw = reader(asset_path)
    if len(raw) != size_bytes or sha256(raw).hexdigest() != digest:
        raise ManagedAssetError(
            f'measurement asset SHA-256 mismatch: {relative_path}'
        )
    return asset_path, raw


# The shared managed asset directory next to the native CAD database. The
# name predates the second store consumer and is kept for backward
# compatibility with the native backup/restore contract; it holds every
# content-addressed managed asset, not only measurement sources.
MANAGED_ASSETS_DIRNAME = 'measurement-assets'


class ManagedAssetStore:
    """Content-addressed managed asset files with atomic installs.

    Assets are named by their SHA-256 digest under ``assets_dir`` and are
    installed with the #305 atomic-write strategy: the payload is written to
    a unique temporary file in the same filesystem, flushed, fsynced and
    re-verified before ``os.replace`` publishes it, so the digest path never
    exposes a partially written file. A directory fsync makes the rename
    durable on POSIX. Identical content is installed once; a digest path
    holding different bytes is a hash collision and fails closed. Reads
    re-verify the digest so tampered assets never serve as evidence.
    """

    def __init__(self, assets_dir: Path) -> None:
        self.assets_dir = Path(assets_dir)
        self.assets_dir.mkdir(parents=True, exist_ok=True)

    def asset_path(self, digest: str) -> Path:
        return self.assets_dir / digest

    @staticmethod
    def fsync_directory(directory: Path) -> None:
        # Making a rename durable requires a directory fsync, which is only
        # meaningful on POSIX filesystems.
        if os.name != 'posix':
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def read_file(target: Path, attempts: int = 100) -> bytes:
        # A file being atomically replaced may briefly refuse reads on
        # Windows while the previous handle is pending deletion.
        for attempt in range(attempts):
            try:
                return read_file_bounded(
                    target, MAX_ATTACHMENT_BYTES, label='managed asset'
                )
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.01)
        raise RuntimeError('unreachable')

    def install(self, digest: str, raw_bytes: bytes) -> None:
        """Durably install *raw_bytes* at the content-addressed digest path.

        The payload is written to a unique temporary file in the same
        filesystem, flushed, fsynced and verified before being atomically
        renamed onto the digest path, so the final path never exposes a
        partially written file. Only the private temporary file is removed
        on failure; an already-installed digest path is never touched.
        """

        target = self.asset_path(digest)
        descriptor, temp_name = tempfile.mkstemp(
            dir=self.assets_dir, prefix='.asset-', suffix='.tmp'
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(raw_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            persisted = read_file_bounded(
                temp, len(raw_bytes), label='staged managed asset'
            )
            if len(persisted) != len(raw_bytes) or sha256(persisted).hexdigest() != digest:
                raise RuntimeError('managed asset write verification failed')
            try:
                os.replace(temp, target)
            except PermissionError:
                # Windows can refuse a replace while a racing install holds
                # the destination; an identical already-installed digest is a
                # success, anything else means the install genuinely failed.
                try:
                    installed = self.read_file(target) == raw_bytes
                except OSError:
                    installed = False
                if not installed:
                    raise
            self.fsync_directory(self.assets_dir)
        finally:
            temp.unlink(missing_ok=True)

    def install_stream(
        self,
        digest: str,
        reader,
        *,
        max_bytes: int,
        expected_size: int | None = None,
    ) -> int:
        """Durably install bytes pulled from *reader* at the digest path.

        Same atomic contract as ``install`` — a unique temporary file in
        the same filesystem, fsync, verification, then ``os.replace`` —
        but reads bounded chunks, so arbitrarily large assets install in
        constant memory. Returns the byte count written. A stream longer
        than *max_bytes*, shorter/longer than *expected_size*, or hashing
        to anything but *digest* fails closed with ``ManagedAssetError``
        and leaves no file behind.
        """

        target = self.asset_path(digest)
        descriptor, temp_name = tempfile.mkstemp(
            dir=self.assets_dir, prefix='.asset-', suffix='.tmp'
        )
        temp = Path(temp_name)
        written = 0
        hasher = sha256()
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                while True:
                    chunk = reader.read(min(1 << 20, max_bytes + 1 - written))
                    if not chunk:
                        break
                    handle.write(chunk)
                    hasher.update(chunk)
                    written += len(chunk)
                    if written > max_bytes:
                        raise ManagedAssetError(
                            'streamed managed asset exceeds the byte bound'
                        )
                handle.flush()
                os.fsync(handle.fileno())
            if expected_size is not None and written != expected_size:
                raise ManagedAssetError(
                    'streamed managed asset size mismatch'
                )
            if hasher.hexdigest() != digest:
                raise ManagedAssetError(
                    'streamed managed asset digest mismatch'
                )
            if sha256_file(temp) != digest:
                raise RuntimeError('managed asset write verification failed')
            try:
                os.replace(temp, target)
            except PermissionError:
                # Same Windows replace-race fallback as install(): an
                # identical digest already at the target is a success.
                if sha256_file(target) != digest:
                    raise
            self.fsync_directory(self.assets_dir)
        finally:
            temp.unlink(missing_ok=True)
        return written

    def ensure_installed(self, digest: str, raw_bytes: bytes) -> None:
        """Install *raw_bytes* unless the identical digest is already stored.

        An already-installed identical digest is a successful dedup hit;
        anything else at the path is corrupt or a genuine collision and
        fails closed.
        """

        target = self.asset_path(digest)
        if target.exists():
            if self.read_file(target) != raw_bytes:
                raise ValueError(
                    'content-addressed managed asset hash collision'
                )
        else:
            self.install(digest, raw_bytes)

    def read_verified(self, digest: str) -> bytes | None:
        """Return the managed bytes for *digest*, or None when absent.

        The content-addressed contract is self-verifying: stored bytes must
        hash back to their digest before they are returned.
        """

        try:
            raw = self.read_file(self.asset_path(digest))
        except FileNotFoundError:
            return None
        if sha256(raw).hexdigest() != digest:
            raise ValueError(
                'managed asset content does not match its content address'
            )
        return raw
