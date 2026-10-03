from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest

import htdt.runtime_instance as runtime_instance
from htdt.runtime_instance import (
    APP_ID,
    LOCK_FILENAME,
    SingleInstanceGuard,
    read_lock_metadata,
)


def _src_dir() -> Path:
    return Path(runtime_instance.__file__).resolve().parents[1]


def _lock_path(data_dir: Path) -> Path:
    return data_dir / LOCK_FILENAME


def test_second_guard_cannot_acquire_held_data_dir(tmp_path: Path) -> None:
    owner = SingleInstanceGuard(tmp_path)
    contender = SingleInstanceGuard(tmp_path)
    try:
        assert owner.acquire() is True
        assert owner.acquire() is True  # re-entrant on the same guard
        assert contender.acquire() is False
    finally:
        owner.release()
        contender.release()


def test_release_frees_data_dir_for_next_guard(tmp_path: Path) -> None:
    owner = SingleInstanceGuard(tmp_path)
    assert owner.acquire() is True
    owner.release()
    owner.release()  # release is idempotent

    replacement = SingleInstanceGuard(tmp_path)
    try:
        assert replacement.acquire() is True
    finally:
        replacement.release()


def test_distinct_data_dirs_do_not_conflict(tmp_path: Path) -> None:
    first = SingleInstanceGuard(tmp_path / 'one')
    second = SingleInstanceGuard(tmp_path / 'two')
    try:
        assert first.acquire() is True
        assert second.acquire() is True
    finally:
        first.release()
        second.release()


def test_context_manager_raises_when_dir_is_held(tmp_path: Path) -> None:
    owner = SingleInstanceGuard(tmp_path)
    assert owner.acquire() is True
    try:
        with pytest.raises(RuntimeError, match='already locked'):
            with SingleInstanceGuard(tmp_path):
                pass
    finally:
        owner.release()


def test_lock_authority_is_a_file_inside_the_data_dir(tmp_path: Path) -> None:
    guard = SingleInstanceGuard(tmp_path)
    try:
        assert guard.acquire() is True
        # The exclusion artifact lives in the data directory itself, so the
        # lock follows the filesystem path rather than a Windows session or
        # process-local namespace.
        assert _lock_path(tmp_path).is_file()
    finally:
        guard.release()
    # The file is a permanent sentinel: it is never deleted on release.
    assert _lock_path(tmp_path).is_file()


def test_owner_metadata_is_advisory_and_cleared_on_release(tmp_path: Path) -> None:
    guard = SingleInstanceGuard(tmp_path)
    assert read_lock_metadata(tmp_path) is None
    try:
        assert guard.acquire() is True
        metadata = read_lock_metadata(tmp_path)
        assert metadata is not None
        assert metadata.get('app_id') == APP_ID
        assert metadata.get('pid') == os.getpid()
        assert isinstance(metadata.get('host'), str)
    finally:
        guard.release()
    # Orderly release drops the advisory record; nothing stale is left behind.
    assert read_lock_metadata(tmp_path) is None


def test_stale_lock_file_and_metadata_do_not_block_acquire(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    # A leftover lock file from a dead process must never brick the directory:
    # the OS owns the lock itself, so only a live holder can exclude others.
    _lock_path(tmp_path).write_bytes(b'L{"app_id": "home-theater-digital-twin", "pid": 999999}')
    guard = SingleInstanceGuard(tmp_path)
    try:
        assert guard.acquire() is True
        metadata = read_lock_metadata(tmp_path)
        assert metadata is not None
        assert metadata.get('pid') == os.getpid()
    finally:
        guard.release()


def test_corrupt_stale_metadata_does_not_block_acquire(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _lock_path(tmp_path).write_bytes(b'L{not-json')
    assert read_lock_metadata(tmp_path) is None
    guard = SingleInstanceGuard(tmp_path)
    try:
        assert guard.acquire() is True
    finally:
        guard.release()


@pytest.mark.skipif(os.name != 'nt', reason='Windows byte-range locking')
def test_windows_lock_authority_is_byte_range_lock_on_lock_file(tmp_path: Path) -> None:
    import msvcrt

    # A foreign OS handle holding the byte-0 range of the data-dir lock file
    # must block the guard: the same primitive every process/session that
    # opens the path contends on, not a session-scoped kernel object.
    fd = os.open(tmp_path / LOCK_FILENAME, os.O_RDWR | os.O_CREAT)
    foreign = os.fdopen(fd, 'r+b', buffering=0)
    foreign.write(b'L')
    foreign.flush()
    foreign.seek(0)
    msvcrt.locking(foreign.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        guard = SingleInstanceGuard(tmp_path)
        assert guard.acquire() is False
        foreign.seek(0)
        msvcrt.locking(foreign.fileno(), msvcrt.LK_UNLCK, 1)
        assert guard.acquire() is True
        guard.release()
    finally:
        foreign.close()

    # And in reverse: while the guard holds the lock, a foreign handle on the
    # same path cannot take the byte-0 range.
    guard = SingleInstanceGuard(tmp_path)
    assert guard.acquire() is True
    fd = os.open(tmp_path / LOCK_FILENAME, os.O_RDWR)
    foreign = os.fdopen(fd, 'r+b', buffering=0)
    try:
        foreign.seek(0)
        with pytest.raises(OSError):
            msvcrt.locking(foreign.fileno(), msvcrt.LK_NBLCK, 1)
    finally:
        foreign.close()
        guard.release()


def test_lock_excludes_other_processes_and_os_releases_on_exit(tmp_path: Path) -> None:
    child_src = (
        'import os, site, sys, time\n'
        'from pathlib import Path\n'
        'site.addsitedir(sys.argv[2])\n'
        'if sys.argv[3] != sys.argv[2]:\n'
        '    site.addsitedir(sys.argv[3])\n'
        'sys.path.insert(0, sys.argv[4])\n'
        'from htdt.runtime_instance import SingleInstanceGuard\n'
        'guard = SingleInstanceGuard(Path(sys.argv[1]))\n'
        'if not guard.acquire():\n'
        '    print("denied", flush=True)\n'
        '    sys.exit(3)\n'
        'print("locked", os.getpid(), flush=True)\n'
        'time.sleep(60)\n'
    )
    env = dict(os.environ)
    env['PYTHONPATH'] = str(_src_dir()) + os.pathsep + env.get('PYTHONPATH', '')
    # _base_executable bypasses the venv launcher shim so Popen.pid is the
    # actual interpreter process and terminate() kills the lock holder.
    python = getattr(sys, '_base_executable', sys.executable)
    # The base interpreter must still use this environment's declared packages.
    package_paths = sysconfig.get_paths()
    child = subprocess.Popen(
        [python, '-c', child_src, str(tmp_path), package_paths['purelib'],
         package_paths['platlib'], str(_src_dir())],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        assert child.stdout is not None
        banner = child.stdout.readline().split()
        assert banner[:1] == ['locked'], f'child failed to lock: {child.stderr.read() if child.stderr else ""}'
        child_pid = int(banner[1])
        assert child_pid != os.getpid()
        assert child_pid == child.pid

        contender = SingleInstanceGuard(tmp_path)
        # A holder in a different OS process excludes us through the lock file
        # (byte-range locks are enforced by the OS for any opener of the path,
        # in any session) and its metadata stays readable as advisory info.
        assert contender.acquire() is False
        metadata = read_lock_metadata(tmp_path)
        assert metadata is not None
        assert metadata.get('pid') == child_pid
    finally:
        child.terminate()
        child.wait(timeout=15)

    # Process termination released the OS-owned lock; the stale lock file and
    # its leftover metadata cannot brick the data directory.
    replacement = SingleInstanceGuard(tmp_path)
    try:
        assert replacement.acquire() is True
    finally:
        replacement.release()
