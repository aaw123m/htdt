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


#: Characters that must not touch a name match — a name embedded inside a
#: longer ASCII run-on (``seat1`` inside ``seat10``, ``seat`` inside
#: ``seat_1``) is a different token, not the named target. Punctuation and
#: non-ASCII text do not count as run-on characters, so Japanese compound
#: names and dash-separated labels still match.
_NAME_RUN_CHARS = frozenset('abcdefghijklmnopqrstuvwxyz0123456789_')


def _name_token_spans(name: str, label: str) -> list[tuple[int, int]]:
    """Every ``(start, end)`` span where ``name`` occurs in ``label`` as a
    whole token under the run-character rule."""
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = label.find(name, start)
        if index < 0:
            return spans
        before = label[index - 1] if index > 0 else ' '
        after_index = index + len(name)
        after = label[after_index] if after_index < len(label) else ' '
        if before not in _NAME_RUN_CHARS and after not in _NAME_RUN_CHARS:
            spans.append((index, after_index))
        start = index + 1


def _name_within_label(name: str, label: str) -> bool:
    """True when normalized ``name`` appears in normalized ``label`` as a
    whole token, not embedded inside a longer ASCII letter/digit run.

    ``seat1`` matches inside ``fl seat1 up`` but never inside ``seat10`` —
    the run-on would bind the measurement to a different seat.
    """
    return bool(_name_token_spans(name, label))


def propose_assignment_target(
    label: str,
    targets: tuple[Any, ...],
) -> tuple[Any | None, tuple[Any, ...]]:
    """Match a staged source label to measurement targets by name.

    ``targets`` are the workspace's eligible AssignmentTarget objects (only
    ``.name``/``.entity_id`` are read). Returns ``(chosen, candidates)``:
    exactly one surviving candidate yields ``chosen``; zero or several
    leave the decision to the user.

    A candidate's normalized name must appear as a whole token of the
    normalized label (REW titles like ``FL seat1 up`` match a target named
    ``seat1``; ``seat10`` does not). Nested matches collapse to the longest
    name — ``seat1`` resolves even when a ``seat`` target also exists,
    while genuinely competing names (``seat1`` *and* ``seat2``) stay
    flagged as ambiguous. One-character names never auto-match: everything
    contains them.
    """
    normalized = _normalize_label(label)
    if not normalized:
        return None, ()
    matched: list[Any] = []
    matched_names: list[str] = []
    for target in targets:
        name = getattr(target, 'name', None)
        if not isinstance(name, str):
            continue
        normalized_name = _normalize_label(name)
        if len(normalized_name) < 2:
            continue
        if _name_within_label(normalized_name, normalized):
            matched.append(target)
            matched_names.append(normalized_name)
    if not matched:
        return None, ()
    if len(matched) == 1:
        return matched[0], (matched[0],)
    # Nested-name collapse: drop a candidate only when every whole-token
    # occurrence of its name sits inside a longer matched name's span —
    # 'seat' inside 'fl seat-1' resolves to 'seat-1'. A name the label
    # also names as an independent token stays a competitor: 'seat1' and
    # 'seat10' in one label are different seats, not a nested match.
    spans_by_target = tuple(
        _name_token_spans(name, normalized) for name in matched_names
    )
    survivors = tuple(
        target
        for target, name, spans, index in zip(
            matched, matched_names, spans_by_target,
            range(len(matched)),
        )
        if not any(
            len(matched_names[j]) > len(name)
            and all(
                any(
                    s_start >= o_start and s_end <= o_end
                    for o_start, o_end in spans_by_target[j]
                )
                for s_start, s_end in spans
            )
            for j in range(len(matched))
            if j != index
        )
    )
    if len(survivors) == 1:
        return survivors[0], survivors
    return None, survivors


#: Sentinel prefix marking that a baseline scan completed for one root —
#: kept inside ``seen`` per directory so an initially-empty directory
#: still counts as 'watched before' and a different watch path always
#: owes its own baseline (REV43: a single global sentinel let a path
#: change stage every pre-existing file in the new folder).
_SCANNED_SENTINEL_PREFIX = '\x00scanned:'


def scan_rew_watch_dir(
    directory: str | Path,
    seen: MutableMapping[str, tuple[int, int]],
    pending: MutableMapping[str, tuple[int, int]],
) -> tuple[list[tuple[bytes, str]], list[str]]:
    """Scan an opted-in folder for new REW text drops.

    ``seen`` maps ``str(path) -> (mtime_ns, size)`` for baseline and
    already-delivered files; ``pending`` does the same for candidates
    sighted exactly once. Both persist across calls and are updated in
    place. The first call *for a directory* establishes its baseline —
    files that pre-date watching that directory are marked seen but NOT
    staged (the opt-in contract is *new drops*, not a bulk import of
    whatever was already there).

    A new or changed signature is only *delivered* once it survives
    unchanged into the following scan: a file still being copied defers
    instead of staging a truncated read, and a file that keeps changing
    stays pending until it settles. A file delivered but whose staging
    failed can be re-queued by removing its ``seen`` marker — the next
    scan re-enters it as a candidate. ``seen`` markers for files no
    longer in the directory are evicted: the map stays bounded by the
    watched file set, and a file dropped again after deletion is
    delivered again instead of matching its stale marker. Returns
    ``(files, skipped_names)``: files as ``(raw, filename)`` pairs ready
    for ``stage_rew_text_files``; skipped names are delivered files that
    failed the bounded read (oversized, unreadable) — they are marked
    seen so a wedged file does not retry every tick.
    """
    root = Path(directory)
    entries = sorted(root.iterdir())
    sentinel = f'{_SCANNED_SENTINEL_PREFIX}{root}'
    first_scan = sentinel not in seen
    seen[sentinel] = (0, 0)
    files: list[tuple[bytes, str]] = []
    skipped: list[str] = []
    observed: set[str] = set()
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in REW_TEXT_SUFFIXES:
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        key = str(entry)
        observed.add(key)
        signature = (stat.st_mtime_ns, stat.st_size)
        if seen.get(key) == signature:
            pending.pop(key, None)
            continue
        if first_scan:
            # Baseline: pre-existing files are marked, never staged.
            seen[key] = signature
            pending.pop(key, None)
            continue
        if pending.get(key) != signature:
            # First sighting — or the signature is still changing because
            # the copy is in flight. Hold one scan so a mid-write file is
            # never read truncated.
            pending[key] = signature
            continue
        # Same signature on two consecutive scans: the write has settled.
        pending.pop(key, None)
        seen[key] = signature
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
    for stale_key in [key for key in pending if key not in observed]:
        del pending[stale_key]
    for stale_key in [
        key
        for key in seen
        if not key.startswith(_SCANNED_SENTINEL_PREFIX)
        and key not in observed
    ]:
        del seen[stale_key]
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
