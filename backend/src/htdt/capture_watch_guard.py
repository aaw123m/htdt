"""Link/reparse defense and verified staging for the watch folder (#1019).

The opt-in ``.htdtcapture`` drop folder is, by design, a place other
machines and other users can write to — a shared folder on a LAN, a
paired Capture device's export target, or a directory an operator drops
files into. Anything the watcher cannot prove is a real, regular file
inside the watched directory is not a drop. This module is the
classification and staging half of that contract:

* **Classification** — :func:`classify_watch_entry` decides from the
  dirent's own ``lstat`` what an entry *is*, never following links:
  symlinks are skipped with ``link_external``/``link_internal``
  depending on where the (strictly resolved) target sits relative to
  the canonical watch root, an unresolvable target is ``broken_link``,
  any other reparse point (NTFS junctions and mount points, app-exec
  links, cloud placeholders) is ``reparse_point``, non-regular files
  are ``not_regular``, and an entry that cannot be lstat'd at all is
  ``unreadable``. Only real regular files are candidates.
* **Root canonicalization** — :func:`resolve_watch_root` resolves the
  *configured* watch path strictly. A root spelled through a link is
  accepted and watches the resolved directory (the operator's intent is
  the target folder); containment is always judged in canonical space,
  so a re-pointed root link is a different epoch while an alias
  spelling of the same directory is the same watch.
* **Verified staging** — :func:`staged_capture_drop` copies a settled
  drop into a private temp dir before it is routed, binding the opened
  descriptor to the lstat'd dirent ``(st_dev, st_ino, mtime_ns, size)``
  the same way ``DirectorySource.read_bytes`` does, re-checking the
  path signature after the copy, and never letting the router see the
  original path. A ``.htdtcapture`` *descriptor* has its referenced
  bundle staged too — but only when the reference is relative and
  resolves inside the canonical watch root; absolute or escaping
  references are ``descriptor_external``, and a link inside a directory
  bundle is ``bundle_member_blocked``.

Nothing here rewrites or even opens the original for anything but a
single verified read; the staged copy is the only bytes ingest ever
touches, and it flows through the exact same
``route_capture_intent``/``FrozenBundle`` gates as a document-open.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path, PurePath
import shutil
import stat
import tempfile
import time
from typing import Iterator

from .capture_bundle import MAX_ENTRIES, MAX_TOTAL_BYTES
from .launch_intents import HTDTCaptureFile, _read_bounded_json


_LOGGER = logging.getLogger(__name__)

# -- skip vocabulary ---------------------------------------------------------
#
# Reasons an entry in the watched directory is reported SKIPPED rather
# than delivered. They are the stable machine vocabulary the runner's
# ``entries_skipped`` records carry and the failure queue stores.

#: A symlink whose strict-resolved target lands outside the canonical
#: watch root — the escape case the whole fix exists for.
SKIP_LINK_EXTERNAL = 'link_external'
#: A symlink whose target stays inside the root. Still skipped: a drop
#: folder carries real files, and an in-root alias is indistinguishable
#: from a re-point attempt without keeping extra state.
SKIP_LINK_INTERNAL = 'link_internal'
#: A non-symlink reparse point: NTFS junctions/mount points, app-exec
#: links, cloud placeholders — anything the OS would silently redirect.
SKIP_REPARSE = 'reparse_point'
#: Not a regular file (directory, FIFO, device, socket).
SKIP_NOT_REGULAR = 'not_regular'
#: A symlink whose target cannot be resolved at all.
SKIP_BROKEN_LINK = 'broken_link'
#: The dirent could not even be lstat'd (permissions, transient lock).
SKIP_UNREADABLE = 'unreadable'

WATCH_SKIP_REASONS = frozenset(
    {
        SKIP_LINK_EXTERNAL,
        SKIP_LINK_INTERNAL,
        SKIP_REPARSE,
        SKIP_NOT_REGULAR,
        SKIP_BROKEN_LINK,
        SKIP_UNREADABLE,
    }
)

# -- stage failure vocabulary -------------------------------------------------
#
# Reasons the verified staging of a delivered drop refused. ``changed``
# covers every swap the identity checks catch; the rest are named so the
# failure-queue classification can tell permanent conditions from
# transient ones.

STAGE_CHANGED = 'changed'
STAGE_VANISHED = 'vanished'
STAGE_OVERSIZED = 'oversized'
STAGE_DESCRIPTOR_EXTERNAL = 'descriptor_external'
STAGE_MEMBER_BLOCKED = 'bundle_member_blocked'
STAGE_IO_ERROR = 'io_error'

#: Stage reasons that can never heal by re-routing — the descriptor
#: itself tells the lane to leave the trust boundary, or the entry is
#: not something a drop folder should ever carry. Everything else is
#: retryable: a mid-write file settles, a lock lifts, a share returns.
STAGE_PERMANENT_REASONS = frozenset(
    {
        STAGE_OVERSIZED,
        STAGE_DESCRIPTOR_EXTERNAL,
        STAGE_MEMBER_BLOCKED,
        *WATCH_SKIP_REASONS - {SKIP_UNREADABLE},
    }
)

#: Upper bound on files reported skipped per scan. Every skipped entry
#: still gets its seen marker (so it never re-reports unchanged); the
#: bound only caps the *report*, keeping a mass-link drop from flooding
#: the queue surface while remaining honest about the shape.
SKIP_REPORT_LIMIT = 64

#: Temp staging prefix; sweep only ever touches its own prefix.
_STAGE_PREFIX = 'htdt-watch-'
#: An orphaned staging dir (killed mid-copy) is collected after this.
_STAGE_STALE_SECONDS = 3600.0
#: Copy chunk and nesting bound for directory bundles.
_STAGE_CHUNK = 1 << 20
_STAGE_MAX_DEPTH = 16


class WatchStageError(Exception):
    """Staging refused a delivered drop.

    ``reason`` is the stable machine vocabulary above; it doubles as the
    routing failure kind recorded for the drop.
    """

    def __init__(self, reason: str, detail: str = '') -> None:
        super().__init__(detail or reason)
        self.reason = reason


class _StageCancelled(Exception):
    """Cooperative cancel inside a copy — not a failure, just a stop."""


def resolve_watch_root(spelled: Path) -> Path | None:
    """Canonical watch root for a configured path, or ``None``.

    Strict resolve + directory check: a missing or dangling path —
    including a *root link* whose target is gone — reports ``None`` so
    the caller can keep the current epoch (transient gap), and a root
    spelled through links is accepted as the directory it resolves to.
    """

    try:
        canonical = Path(spelled).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    try:
        if not canonical.is_dir():
            return None
    except OSError:
        return None
    return canonical


def _within_root(resolved: Path, canonical_root: Path) -> bool:
    """``resolved`` sits under ``canonical_root`` — both already canonical.

    ``commonpath`` compares normalized, case-folded spellings so a UNC
    share vs a drive letter, different drives, or a parent traversal all
    fail closed instead of relying on a string prefix.
    """

    try:
        root_key = os.path.normcase(str(canonical_root))
        child_key = os.path.normcase(str(resolved))
        return os.path.commonpath((child_key, root_key)) == root_key
    except (OSError, ValueError):
        return False


def classify_watch_entry(
    entry: Path, canonical_root: Path
) -> tuple[tuple[int, int] | None, str | None]:
    """Classify one dirent for the drop contract.

    Returns ``(signature, None)`` for a routable regular file —
    ``signature`` is its ``lstat`` ``(mtime_ns, size)`` — or
    ``(signature_or_None, reason)`` for a skip. The signature belongs to
    the *dirent itself* (a link carries its link signature, not its
    target's), so the caller can dedupe re-reports on the entry's own
    change.

    The link verdict needs one resolve per symlink — cheap at drop
    volumes, and only links pay it; regular entries cost exactly one
    ``lstat``.
    """

    try:
        info = entry.lstat()
    except OSError:
        return (None, SKIP_UNREADABLE)
    signature = (info.st_mtime_ns, info.st_size)
    if stat.S_ISLNK(info.st_mode):
        try:
            resolved = entry.resolve(strict=True)
        except (OSError, RuntimeError):
            return (signature, SKIP_BROKEN_LINK)
        if not _within_root(resolved, canonical_root):
            return (signature, SKIP_LINK_EXTERNAL)
        return (signature, SKIP_LINK_INTERNAL)
    # Any other reparse tag — junctions/mount points, app-exec links,
    # cloud recall placeholders — is an OS-level redirect the spelled
    # name hides; POSIX lstats carry no tag and answer 0.
    if getattr(info, 'st_reparse_tag', 0):
        return (signature, SKIP_REPARSE)
    if not stat.S_ISREG(info.st_mode):
        return (signature, SKIP_NOT_REGULAR)
    return (signature, None)


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    """The descriptor-binding tuple: device, inode, signature."""

    return (
        info.st_dev,
        info.st_ino,
        info.st_mtime_ns,
        info.st_size,
    )


def _copy_verified(
    source: Path,
    destination: Path,
    budget: '_StageBudget',
    cancel: object | None,
) -> int:
    """Verified single-file copy; returns bytes written.

    Same TOCTOU discipline as ``DirectorySource.read_bytes``: lstat the
    spelled path, open once (with ``O_NOFOLLOW`` where the platform
    honors it — absent on Windows, where the inode equality below is
    the binding), fstat the descriptor, and require the identities to
    match before a byte moves. A swap to a link or another file between
    the two calls lands on a different inode and is caught; a swap
    *during* the copy is caught by the post-copy signature check — in
    both cases the staged bytes never reach the router.
    """

    try:
        pre = source.lstat()
    except FileNotFoundError as exc:
        raise WatchStageError(STAGE_VANISHED) from exc
    except OSError as exc:
        raise WatchStageError(STAGE_IO_ERROR) from exc
    if not stat.S_ISREG(pre.st_mode):
        # Whatever the scan settled, it is not a regular file anymore —
        # reuse the skip vocabulary so the failure reads the same way.
        if stat.S_ISLNK(pre.st_mode):
            raise WatchStageError(SKIP_LINK_EXTERNAL)
        raise WatchStageError(SKIP_NOT_REGULAR)
    flags = os.O_RDONLY
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_BINARY', 0)
    try:
        descriptor = os.open(source, flags)
    except FileNotFoundError as exc:
        raise WatchStageError(STAGE_VANISHED) from exc
    except OSError as exc:
        raise WatchStageError(STAGE_IO_ERROR) from exc
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(pre):
            raise WatchStageError(STAGE_CHANGED)
        written = 0
        try:
            out_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except OSError as exc:
            raise WatchStageError(STAGE_IO_ERROR) from exc
        try:
            with os.fdopen(out_fd, 'wb') as out:
                while True:
                    if _cancelled(cancel):
                        raise _StageCancelled()
                    chunk = os.read(descriptor, _STAGE_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    budget.spend_bytes(len(chunk))
                    out.write(chunk)
        except _StageCancelled:
            raise
        except OSError as exc:
            raise WatchStageError(STAGE_IO_ERROR) from exc
    finally:
        os.close(descriptor)
    # The path may not name what we opened anymore — if it changed while
    # copying, the drop was still being written (or actively swapped);
    # the staged bytes are consistent but the settle verdict was not.
    try:
        post = source.lstat()
    except OSError as exc:
        raise WatchStageError(STAGE_CHANGED) from exc
    if _identity(post) != _identity(pre):
        raise WatchStageError(STAGE_CHANGED)
    if written != pre.st_size:
        raise WatchStageError(STAGE_CHANGED)
    return written


def _cancelled(cancel: object | None) -> bool:
    check = getattr(cancel, 'is_set', None)
    return bool(check and check())


class _StageBudget:
    """Shared bounds for one staged drop across descriptor + bundle."""

    def __init__(self) -> None:
        self.bytes_left = MAX_TOTAL_BYTES
        self.members_left = MAX_ENTRIES

    def spend_bytes(self, amount: int) -> None:
        self.bytes_left -= amount
        if self.bytes_left < 0:
            raise WatchStageError(STAGE_OVERSIZED)

    def spend_member(self) -> None:
        self.members_left -= 1
        if self.members_left < 0:
            raise WatchStageError(STAGE_OVERSIZED)


def _stage_descriptor_bundle(
    descriptor_path: Path,
    staged_descriptor: Path,
    staging_dir: Path,
    canonical_root: Path,
    budget: _StageBudget,
    cancel: object | None,
) -> None:
    """Stage the bundle a ``.htdtcapture`` *descriptor* references.

    The descriptor indirection is itself an escape vector — a drop that
    only names ``C:\\elsewhere\\bundle`` is not a drop at all — so the
    reference must be relative, free of ``..`` and absolute spellings,
    and resolve inside the canonical watch root. The companion is staged
    under the same relative name so the descriptor's own resolution
    rules work unchanged inside the staging dir. A directory bundle is
    copied recursively with the same per-member verification.
    """

    payload = _read_bounded_json(staged_descriptor)
    if payload is None:
        return
    try:
        descriptor = HTDTCaptureFile.model_validate(payload)
    except ValueError:
        return
    if not descriptor.bundle_path:
        # No bundle reference — the router's user_action_required path
        # answers this; staging has nothing to add.
        return
    rel = PurePath(descriptor.bundle_path)
    if (
        rel.is_absolute()
        or not rel.parts
        or any(part in ('..', '') for part in rel.parts)
    ):
        raise WatchStageError(STAGE_DESCRIPTOR_EXTERNAL)
    spelled = descriptor_path.parent.joinpath(*rel.parts)
    try:
        resolved = spelled.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WatchStageError(STAGE_VANISHED) from exc
    if not _within_root(resolved, canonical_root):
        raise WatchStageError(STAGE_DESCRIPTOR_EXTERNAL)
    _stage_tree(
        spelled,
        staging_dir.joinpath(*rel.parts),
        canonical_root,
        budget,
        cancel,
        depth=0,
    )


def _stage_tree(
    source: Path,
    destination: Path,
    canonical_root: Path,
    budget: _StageBudget,
    cancel: object | None,
    *,
    depth: int,
) -> None:
    """Recursively stage a bundle member — real files and dirs only."""

    if depth > _STAGE_MAX_DEPTH:
        raise WatchStageError(STAGE_OVERSIZED)
    try:
        info = source.lstat()
    except OSError as exc:
        raise WatchStageError(STAGE_VANISHED) from exc
    if stat.S_ISDIR(info.st_mode):
        if getattr(info, 'st_reparse_tag', 0):
            raise WatchStageError(STAGE_MEMBER_BLOCKED)
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WatchStageError(STAGE_IO_ERROR) from exc
        try:
            members = sorted(source.iterdir())
        except OSError as exc:
            raise WatchStageError(STAGE_IO_ERROR) from exc
        for member in members:
            if _cancelled(cancel):
                raise _StageCancelled()
            _stage_tree(
                member,
                destination / member.name,
                canonical_root,
                budget,
                cancel,
                depth=depth + 1,
            )
        return
    # Files and anything else: only a regular non-reparse file stages.
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0):
        raise WatchStageError(STAGE_MEMBER_BLOCKED)
    if not stat.S_ISREG(info.st_mode):
        raise WatchStageError(STAGE_MEMBER_BLOCKED)
    budget.spend_member()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WatchStageError(STAGE_IO_ERROR) from exc
    _copy_verified(source, destination, budget, cancel)


def sweep_stale_staging(now: float | None = None) -> None:
    """Collect staging dirs an interrupted run left behind.

    Only the lane's own prefix, only past the staleness bound — a live
    job's dir is never touched (it is younger than the bound), and
    failures stay silent: a temp-dir hiccup must not wedge the lane.
    """

    cutoff = (now if now is not None else time.time()) - _STAGE_STALE_SECONDS
    try:
        candidates = sorted(
            Path(tempfile.gettempdir()).glob(f'{_STAGE_PREFIX}*')
        )
    except OSError:
        return
    for candidate in candidates:
        try:
            if (
                candidate.is_dir()
                and candidate.stat().st_mtime < cutoff
            ):
                shutil.rmtree(candidate, ignore_errors=True)
        except OSError:
            continue


@contextlib.contextmanager
def staged_capture_drop(
    path: Path,
    *,
    canonical_root: Path,
    cancel: object | None = None,
) -> Iterator[Path]:
    """Stage one settled drop into a private temp dir for routing.

    Yields the staged path to hand to ``build_launch_intent`` — never
    the original. Raises :class:`WatchStageError` (one of the stage
    vocabularies) when the drop cannot be verified, or
    :class:`_StageCancelled` when the job's cancel event fires
    mid-copy; the temp dir is removed either way, and the original is
    never written.
    """

    staging_dir = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX))
    try:
        budget = _StageBudget()
        staged = staging_dir / path.name
        _copy_verified(path, staged, budget, cancel)
        _stage_descriptor_bundle(
            path, staged, staging_dir, canonical_root, budget, cancel
        )
        yield staged
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


__all__ = [
    'SKIP_BROKEN_LINK',
    'SKIP_LINK_EXTERNAL',
    'SKIP_LINK_INTERNAL',
    'SKIP_NOT_REGULAR',
    'SKIP_REPARSE',
    'SKIP_UNREADABLE',
    'SKIP_REPORT_LIMIT',
    'STAGE_CHANGED',
    'STAGE_DESCRIPTOR_EXTERNAL',
    'STAGE_IO_ERROR',
    'STAGE_MEMBER_BLOCKED',
    'STAGE_OVERSIZED',
    'STAGE_PERMANENT_REASONS',
    'STAGE_VANISHED',
    'WATCH_SKIP_REASONS',
    'WatchStageError',
    'classify_watch_entry',
    'resolve_watch_root',
    'staged_capture_drop',
    'sweep_stale_staging',
]
