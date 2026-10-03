"""REW automation helpers — launch detection, watch-folder scan, and
name-match assignment proposals.

Every helper here automates a step the user already performs by hand:
launching REW with its documented ``-api`` flag, picking up files the user
dropped into an *opted-in* folder, and proposing a measurement target only
when the source label names exactly one eligible target. Nothing here
commits, promotes, or mutates REW or project state — the staged-import and
promotion gates stay exactly where the manual flow put them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from .ingress import read_file_bounded
from .limits import MAX_NATIVE_REW_TEXT_FILE_BYTES


#: REW documents its API launch flags (``-api``, ``-port``, ``-host``,
#: ``-nogui``) on https://www.roomeqwizard.com/help/help_en-GB/html/api.html.
REW_API_FLAG = '-api'
REW_API_PORT_FLAG = '-port'

#: File extensions the text importer accepts (same set as the dialogs).
REW_TEXT_SUFFIXES = frozenset({'.txt', '.frd', '.mdat'})

_WINDOWS_REW_CANDIDATES = (
    r'C:\Program Files\REW\roomeqwizard.exe',
    r'C:\Program Files (x86)\REW\roomeqwizard.exe',
)
_MACOS_REW_CANDIDATES = (
    '/Applications/REW.app',
)
_ENV_REW_PATH = 'HTDT_REW_PATH'


@dataclass(frozen=True, slots=True)
class RewInstall:
    """One discovered local REW installation."""

    kind: str  # 'windows' | 'macos' | 'script'
    path: str
    display: str


def _candidate_installs(
    environ: Mapping[str, str],
    platform_name: str,
) -> tuple[tuple[str, str], ...]:
    if platform_name.startswith('win'):
        candidates = list(_WINDOWS_REW_CANDIDATES)
        local = environ.get('LOCALAPPDATA')
        if local:
            candidates.append(
                str(Path(local) / 'Programs' / 'REW' / 'roomeqwizard.exe')
            )
        return tuple(('windows', candidate) for candidate in candidates)
    if platform_name == 'darwin':
        candidates = list(_MACOS_REW_CANDIDATES)
        home = environ.get('HOME')
        if home:
            candidates.append(str(Path(home) / 'Applications' / 'REW.app'))
        return tuple(('macos', candidate) for candidate in candidates)
    return ()


def find_rew_install(
    *,
    install_path: str = '',
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> RewInstall | None:
    """Locate a usable local REW installation, or None when none is found.

    Resolution order: an explicit ``install_path`` (the preference override),
    then the ``HTDT_REW_PATH`` environment override, then the documented
    standard install locations for the current platform. A path that exists
    but is not a plausible REW install is still honored — the user pointed
    at it deliberately; ``launch_rew`` surfaces failures honestly.
    """
    env = os.environ if environ is None else environ
    platform = sys.platform if platform_name is None else platform_name

    for candidate in (install_path.strip(), env.get(_ENV_REW_PATH, '').strip()):
        if candidate and Path(candidate).exists():
            return RewInstall(
                kind=_kind_for_path(candidate, platform),
                path=candidate,
                display=Path(candidate).name,
            )

    for kind, candidate in _candidate_installs(env, platform):
        if Path(candidate).exists():
            return RewInstall(kind=kind, path=candidate, display=Path(candidate).name)
    return None


def _kind_for_path(path: str, platform_name: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in {'.bat', '.cmd', '.sh', '.py'}:
        return 'script'
    if suffix == '.app' or platform_name == 'darwin':
        return 'macos'
    return 'windows'


def launch_rew(
    install: RewInstall,
    *,
    port: int | None = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> subprocess.Popen:
    """Spawn REW detached with its API enabled; returns the handle.

    The port argument is passed explicitly whenever supplied so the launched
    instance serves exactly the endpoint HTDT polls — REW's own persisted
    API port preference is not consulted. ``-nogui`` is deliberately never
    used: a GUI REW is what the operator expects to see, and a headless REW
    would have to be shut down over the API to avoid an orphaned process.

    The process is fully detached (new process group on Windows) so closing
    or crashing HTDT never leaves a child bound to the workspace lifecycle —
    the user owns the REW window, same as launching it themselves.
    """
    port_args = [REW_API_PORT_FLAG, str(port)] if port is not None else []
    if install.kind == 'macos':
        argv = ['open', '-a', install.path, '--args', REW_API_FLAG, *port_args]
        return popen(argv)
    if install.kind == 'script':
        suffix = Path(install.path).suffix.lower()
        if suffix in {'.bat', '.cmd'}:
            argv = ['cmd', '/c', install.path, REW_API_FLAG, *port_args]
        elif suffix == '.py':
            argv = [sys.executable, install.path, REW_API_FLAG, *port_args]
        else:
            argv = [install.path, REW_API_FLAG, *port_args]
    else:
        argv = [install.path, REW_API_FLAG, *port_args]
    if sys.platform.startswith('win'):
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — the REW window must
        # not share HTDT's console or die with it.
        return popen(
            argv,
            creationflags=(
                getattr(subprocess, 'DETACHED_PROCESS', 0)
                | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
            ),
            close_fds=True,
        )
    return popen(argv, start_new_session=True, close_fds=True)


def _normalize_label(value: str) -> str:
    return ' '.join(value.casefold().split())


def propose_assignment_target(
    label: str,
    targets: tuple[Any, ...],
) -> tuple[Any | None, tuple[Any, ...]]:
    """Match a staged source label to measurement targets by name.

    ``targets`` are the workspace's eligible AssignmentTarget objects (only
    ``.name``/``.entity_id`` are read). Returns ``(chosen, candidates)``:
    exactly one surviving candidate yields ``chosen``; zero or several
    leave the decision to the user.

    A candidate's normalized name must appear as a substring of the
    normalized label (REW titles like ``FL seat1 up`` match a target named
    ``seat1``). Nested matches collapse to the longest name — ``seat1``
    resolves even when a ``seat`` target also exists, while genuinely
    competing names (``seat1`` *and* ``seat2``) stay flagged as ambiguous.
    One-character names never auto-match: everything contains them.
    """
    normalized = _normalize_label(label)
    if not normalized:
        return None, ()
    matched: list[Any] = []
    for target in targets:
        name = getattr(target, 'name', None)
        if not isinstance(name, str):
            continue
        normalized_name = _normalize_label(name)
        if len(normalized_name) < 2:
            continue
        if normalized_name in normalized:
            matched.append(target)
    if not matched:
        return None, ()
    if len(matched) == 1:
        return matched[0], (matched[0],)
    # Nested-name collapse: drop every candidate whose name is a substring
    # of another candidate's name — 'seat' loses to 'seat1' when the label
    # actually said 'seat1'.
    names = {
        target: _normalize_label(getattr(target, 'name', ''))
        for target in matched
    }
    survivors = tuple(
        target
        for target in matched
        if not any(
            names[target] != other_name and names[target] in other_name
            for other_name in names.values()
        )
    )
    if len(survivors) == 1:
        return survivors[0], survivors
    return None, survivors


#: Sentinel marking that a baseline scan completed — kept inside ``seen``
#: so an initially-empty directory still counts as 'watched before'.
_SCANNED_SENTINEL = '\x00scanned'


def scan_rew_watch_dir(
    directory: str | Path,
    seen: MutableMapping[str, tuple[int, int]],
) -> tuple[list[tuple[bytes, str]], list[str]]:
    """Scan an opted-in folder for new REW text drops.

    ``seen`` maps ``str(path) -> (mtime_ns, size)`` across calls and is
    updated in place. The first call establishes the baseline — files that
    pre-date watching are marked seen but NOT staged (the opt-in contract
    is *new drops*, not a bulk import of whatever was already there).
    Returns ``(files, skipped_names)``: files as ``(raw, filename)`` pairs
    ready for ``stage_rew_text_files``; skipped names are files that failed
    the bounded read (oversized, unreadable) — they are marked seen so a
    wedged file does not retry every tick.
    """
    root = Path(directory)
    entries = sorted(root.iterdir())
    first_scan = _SCANNED_SENTINEL not in seen
    seen[_SCANNED_SENTINEL] = (0, 0)
    files: list[tuple[bytes, str]] = []
    skipped: list[str] = []
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in REW_TEXT_SUFFIXES:
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        key = str(entry)
        signature = (stat.st_mtime_ns, stat.st_size)
        if seen.get(key) == signature:
            continue
        seen[key] = signature
        if first_scan:
            continue
        try:
            raw = read_file_bounded(
                entry,
                MAX_NATIVE_REW_TEXT_FILE_BYTES,
                label='REWテキストファイル',
            )
        except Exception:
            skipped.append(entry.name)
            continue
        files.append((raw, entry.name))
    return files, skipped


__all__ = [
    'REW_API_FLAG',
    'REW_API_PORT_FLAG',
    'REW_TEXT_SUFFIXES',
    'RewInstall',
    'find_rew_install',
    'launch_rew',
    'propose_assignment_target',
    'scan_rew_watch_dir',
]
