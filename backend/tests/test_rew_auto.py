"""REV40-REWAUTO: unit tests for the REW automation helpers."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from htdt.rew_auto import (
    RewInstall,
    find_rew_install,
    launch_rew,
    propose_assignment_target,
    scan_rew_watch_dir,
)


class _Target:
    def __init__(self, entity_id: str, name: str, kind: str = 'listener') -> None:
        self.entity_id = entity_id
        self.name = name
        self.kind = kind


class _FakePopen:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs


def test_find_rew_install_prefers_explicit_path(tmp_path: Path) -> None:
    exe = tmp_path / 'roomeqwizard.exe'
    exe.write_bytes(b'x')
    install = find_rew_install(
        install_path=str(exe), environ={}, platform_name='win32'
    )
    assert install is not None
    assert install.path == str(exe)
    assert install.kind == 'windows'


def test_find_rew_install_env_override(tmp_path: Path) -> None:
    exe = tmp_path / 'REW.app'
    exe.mkdir()
    install = find_rew_install(
        install_path='',
        environ={'HTDT_REW_PATH': str(exe)},
        platform_name='darwin',
    )
    assert install is not None
    assert install.kind == 'macos'


def test_find_rew_install_standard_windows_location() -> None:
    install = find_rew_install(
        install_path='', environ={}, platform_name='win32'
    )
    if Path(r'C:\Program Files\REW\roomeqwizard.exe').exists():
        assert install is not None
        assert install.kind == 'windows'
    else:
        # This box has no REW — None is the honest answer.
        assert install is None


def test_find_rew_install_none_when_nothing_found(tmp_path: Path) -> None:
    install = find_rew_install(
        install_path='',
        environ={'LOCALAPPDATA': str(tmp_path)},
        platform_name='linux',
    )
    assert install is None


def test_launch_rew_windows_argv_and_detach() -> None:
    install = RewInstall('windows', r'C:\REW\roomeqwizard.exe', 'roomeqwizard.exe')
    proc = launch_rew(install, port=4735, popen=_FakePopen)
    assert proc.argv[0].lower().endswith('roomeqwizard.exe')
    assert '-api' in proc.argv
    port_index = proc.argv.index('-port')
    assert proc.argv[port_index + 1] == '4735'
    if sys.platform.startswith('win'):
        flags = proc.kwargs.get('creationflags', 0)
        assert flags & getattr(subprocess, 'DETACHED_PROCESS', 0)
        assert flags & getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        assert proc.kwargs.get('close_fds') is True
    else:
        assert proc.kwargs.get('start_new_session') is True


def test_launch_rew_macos_uses_open_args() -> None:
    install = RewInstall('macos', '/Applications/REW.app', 'REW.app')
    proc = launch_rew(install, port=4736, popen=_FakePopen)
    assert proc.argv[:3] == ['open', '-a', '/Applications/REW.app']
    assert '--args' in proc.argv
    tail = proc.argv[proc.argv.index('--args') + 1:]
    assert tail == ['-api', '-port', '4736']


def test_propose_assignment_target_unique_match() -> None:
    targets = (
        _Target('e1', 'Seat 1'),
        _Target('e2', 'Seat 2'),
    )
    chosen, candidates = propose_assignment_target('REW left seat 1 up', targets)
    assert chosen is targets[0]
    assert candidates == (targets[0],)


def test_propose_assignment_target_ambiguous() -> None:
    targets = (
        _Target('e1', 'Seat 1'),
        _Target('e2', 'Seat 2'),
    )
    chosen, candidates = propose_assignment_target('seat 1 and seat 2', targets)
    assert chosen is None
    assert set(candidates) == {targets[0], targets[1]}


def test_propose_assignment_target_nested_names_pick_longest() -> None:
    targets = (
        _Target('e1', 'seat'),
        _Target('e2', 'seat 1'),
    )
    chosen, _ = propose_assignment_target('REW seat 1 position', targets)
    assert chosen is targets[1]


def test_propose_assignment_target_no_match_and_short_names() -> None:
    targets = (
        _Target('e1', 'LP'),
        _Target('x', 'L'),  # one-char names never auto-match
    )
    chosen, candidates = propose_assignment_target('totally unrelated', targets)
    assert chosen is None
    assert candidates == ()
    chosen, candidates = propose_assignment_target('L measurement', targets)
    assert chosen is None
    assert candidates == ()


def test_propose_assignment_target_never_binds_inside_run_on() -> None:
    """'seat1' inside 'seat10' must not auto-assign — that is a different
    seat, and a wrong confident bind is worse than no bind."""
    targets = (
        _Target('e1', 'seat1'),
        _Target('e10', 'seat10'),
    )
    chosen, _ = propose_assignment_target('REW seat10 sweep', targets)
    assert chosen is targets[1]

    chosen, candidates = propose_assignment_target(
        'seat10 measurement', (_Target('e1', 'seat1'),)
    )
    assert chosen is None
    assert candidates == ()

    # Punctuation-adjacent hits still count as whole tokens.
    chosen, _ = propose_assignment_target(
        'pos seat-a', (_Target('ea', 'seat'),)
    )
    assert chosen is not None and chosen.entity_id == 'ea'


def test_propose_assignment_target_unhashable_targets() -> None:
    """Targets are matched by identity, not hashing — a slotted/unhashable
    target object must never break auto-assign."""

    class _UnhashableTarget:
        __hash__ = None

        def __init__(self, entity_id: str, name: str) -> None:
            self.entity_id = entity_id
            self.name = name

    targets = (
        _UnhashableTarget('e1', 'seat'),
        _UnhashableTarget('e2', 'seat 1'),
    )
    chosen, _ = propose_assignment_target('REW seat 1 pos', targets)
    assert chosen is targets[1]


def _write_rew_text(path: Path, body: bytes) -> None:
    path.write_bytes(body)


def test_scan_rew_watch_dir_baseline_then_new_drop(tmp_path: Path) -> None:
    first = tmp_path / 'first.txt'
    _write_rew_text(first, b'* rew\n20 80\n')
    seen: dict[str, tuple[int, int]] = {}
    pending: dict[str, tuple[int, int]] = {}
    files, skipped = scan_rew_watch_dir(tmp_path, seen, pending)
    assert files == []  # baseline pass stages nothing
    assert skipped == []

    second = tmp_path / 'second.frd'
    _write_rew_text(second, b'* rew\n30 90\n')
    # First sighting only registers the candidate — a file that is still
    # being copied must never be read mid-write.
    files, skipped = scan_rew_watch_dir(tmp_path, seen, pending)
    assert files == []
    files, skipped = scan_rew_watch_dir(tmp_path, seen, pending)
    assert [name for _raw, name in files] == ['second.frd']
    assert skipped == []

    # Unchanged files are not re-staged.
    files, _ = scan_rew_watch_dir(tmp_path, seen, pending)
    assert files == []


def test_scan_rew_watch_dir_changed_file_re_staged(tmp_path: Path) -> None:
    f = tmp_path / 'm.txt'
    _write_rew_text(f, b'* v1\n')
    seen: dict[str, tuple[int, int]] = {}
    pending: dict[str, tuple[int, int]] = {}
    scan_rew_watch_dir(tmp_path, seen, pending)
    _write_rew_text(f, b'* v2 changed\n')
    scan_rew_watch_dir(tmp_path, seen, pending)  # sighting — defers
    files, _ = scan_rew_watch_dir(tmp_path, seen, pending)
    assert [name for _raw, name in files] == ['m.txt']


def test_scan_rew_watch_dir_mid_write_never_staged(tmp_path: Path) -> None:
    """A file whose signature keeps changing stays pending; it is read
    exactly once, after it settles — never truncated mid-copy."""
    f = tmp_path / 'copying.frd'
    seen: dict[str, tuple[int, int]] = {}
    pending: dict[str, tuple[int, int]] = {}
    scan_rew_watch_dir(tmp_path, seen, pending)  # baseline

    _write_rew_text(f, b'* part1\n')
    files, _ = scan_rew_watch_dir(tmp_path, seen, pending)
    assert files == []
    _write_rew_text(f, b'* part1\n20 70\n')  # still growing
    files, _ = scan_rew_watch_dir(tmp_path, seen, pending)
    assert files == []  # signature changed — deferred again

    files, _ = scan_rew_watch_dir(tmp_path, seen, pending)
    assert [name for _raw, name in files] == ['copying.frd']
    # Delivered once; never re-staged while unchanged.
    files, _ = scan_rew_watch_dir(tmp_path, seen, pending)
    assert files == []


def test_scan_rew_watch_dir_pending_pruned_on_delete(tmp_path: Path) -> None:
    """A candidate deleted between scans must not linger in pending — a
    same-named later drop would otherwise inherit the stale signature."""
    f = tmp_path / 'gone.txt'
    seen: dict[str, tuple[int, int]] = {}
    pending: dict[str, tuple[int, int]] = {}
    scan_rew_watch_dir(tmp_path, seen, pending)
    _write_rew_text(f, b'* v1\n')
    scan_rew_watch_dir(tmp_path, seen, pending)
    assert str(f) in pending

    f.unlink()
    files, _ = scan_rew_watch_dir(tmp_path, seen, pending)
    assert files == []
    assert pending == {}


def test_scan_rew_watch_dir_unmarked_file_retries(tmp_path: Path) -> None:
    """Dropping a file's ``seen`` marker re-queues it — how the workspace
    retries a drop whose staging failed."""
    f = tmp_path / 'retry.txt'
    seen: dict[str, tuple[int, int]] = {}
    pending: dict[str, tuple[int, int]] = {}
    scan_rew_watch_dir(tmp_path, seen, pending)  # baseline
    _write_rew_text(f, b'* v1\n')
    scan_rew_watch_dir(tmp_path, seen, pending)  # sighting
    files, _ = scan_rew_watch_dir(tmp_path, seen, pending)
    assert [name for _raw, name in files] == ['retry.txt']
    key = str(f)
    assert key in seen

    # Stage failure → caller drops the marker → the file re-pends and is
    # delivered again instead of being silently lost.
    del seen[key]
    files, _ = scan_rew_watch_dir(tmp_path, seen, pending)
    assert files == []
    assert key in pending
    files, _ = scan_rew_watch_dir(tmp_path, seen, pending)
    assert [name for _raw, name in files] == ['retry.txt']


def test_scan_rew_watch_dir_ignores_other_suffixes(tmp_path: Path) -> None:
    (tmp_path / 'note.md').write_text('not rew', encoding='utf-8')
    (tmp_path / 'data.mdat').write_bytes(b'* binary-ish\n')
    seen: dict[str, tuple[int, int]] = {}
    pending: dict[str, tuple[int, int]] = {}
    scan_rew_watch_dir(tmp_path, seen, pending)  # baseline
    (tmp_path / 'note2.md').write_text('x', encoding='utf-8')
    scan_rew_watch_dir(tmp_path, seen, pending)
    files, _ = scan_rew_watch_dir(tmp_path, seen, pending)
    assert files == []


def test_scan_rew_watch_dir_oversized_marked_seen(tmp_path: Path, monkeypatch) -> None:
    import htdt.rew_auto as rew_auto

    monkeypatch.setattr(rew_auto, 'MAX_NATIVE_REW_TEXT_FILE_BYTES', 8)
    f = tmp_path / 'big.txt'
    _write_rew_text(f, b'0123456789abcdef')
    seen: dict[str, tuple[int, int]] = {}
    pending: dict[str, tuple[int, int]] = {}
    scan_rew_watch_dir(tmp_path, seen, pending)  # baseline
    _write_rew_text(f, b'0123456789abcdef2')
    scan_rew_watch_dir(tmp_path, seen, pending)  # sighting — defers
    files, skipped = scan_rew_watch_dir(tmp_path, seen, pending)
    assert files == []
    assert skipped == ['big.txt']
    files, skipped = scan_rew_watch_dir(tmp_path, seen, pending)
    assert skipped == []  # wedged file does not retry forever
