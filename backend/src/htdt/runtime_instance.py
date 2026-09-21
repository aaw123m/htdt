from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import time
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_ID = 'home-theater-digital-twin'
LOCK_FILENAME = '.instance.lock'


def default_data_dir() -> Path:
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        return Path(local_app_data) / 'HomeTheaterDigitalTwin'
    return Path.home() / '.home-theater-digital-twin'


def _lock_first_byte(file: BinaryIO) -> None:
    """Exclusively lock byte 0 of ``file`` without blocking.

    Raises OSError when another process holds the lock (or the filesystem
    cannot honor byte-range locking), so callers can fail closed.
    """
    file.seek(0)
    if os.name == 'nt':
        import msvcrt

        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_first_byte(file: BinaryIO) -> None:
    file.seek(0)
    if os.name == 'nt':
        import msvcrt

        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


class SingleInstanceGuard:
    """OS-level lock scoped to one HTDT data directory.

    The lock authority is an exclusive byte-range lock on byte 0 of
    ``<root>/.instance.lock`` (``msvcrt.locking`` on Windows, ``flock``
    elsewhere), so exclusion follows the data directory itself rather than the
    current Windows session. On Windows the lock is machine-wide across
    sessions, and on shared filesystems that honor byte-range locking (SMB)
    it extends across machines; filesystems that cannot honor it raise
    OSError and acquisition fails closed.

    The lock is owned by the OS-held file handle and is released on process
    termination, so a crashed holder can never leave the directory locked.
    The lock file is never deleted: owner metadata written after the locked
    byte is advisory diagnostics only and never gates acquisition.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._file: BinaryIO | None = None
        self._acquired = False

    def acquire(self) -> bool:
        if self._acquired:
            return True
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
        file = os.fdopen(fd, 'r+b', buffering=0)
        try:
            _lock_first_byte(file)
        except OSError:
            file.close()
            return False
        self._file = file
        self._acquired = True
        self._write_metadata()
        return True

    def _write_metadata(self) -> None:
        """Record advisory owner details after the locked byte.

        Byte 0 stays reserved for the OS lock so contenders can read this
        JSON payload without touching the locked range. Failures are ignored:
        the byte-range lock alone is the authority.
        """
        file = self._file
        if file is None:
            return
        payload = json.dumps({
            'app_id': APP_ID,
            'pid': os.getpid(),
            'host': socket.gethostname(),
            'acquired_at': datetime.now(timezone.utc).isoformat(),
        }, sort_keys=True).encode('utf-8')
        try:
            file.seek(0, os.SEEK_END)
            if file.tell() < 1:
                file.seek(0)
                file.write(b'L')
            file.truncate(1)
            file.seek(1)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        except OSError:
            pass

    def release(self) -> None:
        if not self._acquired:
            return
        file = self._file
        self._file = None
        self._acquired = False
        if file is None:
            return
        try:
            file.truncate(1)  # drop advisory owner metadata while still holding the lock
        except OSError:
            pass
        try:
            _unlock_first_byte(file)
        finally:
            file.close()

    def __enter__(self) -> 'SingleInstanceGuard':
        if not self.acquire():
            raise RuntimeError('HTDT data directory is already locked by another process')
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


def read_lock_metadata(root: Path) -> dict[str, object] | None:
    """Read advisory owner metadata from a data directory lock file.

    Returns None when no metadata is present or readable. The result is
    diagnostic-only: it reflects the last recorded holder and must never be
    treated as proof the directory is still locked.
    """
    try:
        fd = os.open(root / LOCK_FILENAME, os.O_RDONLY)
    except OSError:
        return None
    try:
        with os.fdopen(fd, 'rb', buffering=0) as file:
            file.seek(1)
            raw = file.read()
        payload = json.loads(raw.decode('utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(frozen=True)
class RuntimeInfo:
    pid: int
    port: int
    url: str


RUNTIME_FILENAME = 'runtime.json'


def write_runtime_info(root: Path, info: RuntimeInfo) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / RUNTIME_FILENAME
    temp = root / f'.{RUNTIME_FILENAME}.{os.getpid()}.tmp'
    temp.write_text(json.dumps({'app_id': APP_ID, 'pid': info.pid, 'port': info.port, 'url': info.url}, sort_keys=True), encoding='utf-8')
    os.replace(temp, path)


def read_runtime_info(root: Path) -> RuntimeInfo | None:
    path = root / RUNTIME_FILENAME
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if payload.get('app_id') != APP_ID:
        return None
    pid = payload.get('pid')
    port = payload.get('port')
    url = payload.get('url')
    if not isinstance(pid, int) or not isinstance(port, int) or not isinstance(url, str):
        return None
    if not (1 <= port <= 65535) or url != f'http://127.0.0.1:{port}/':
        return None
    return RuntimeInfo(pid=pid, port=port, url=url)


def clear_runtime_info(root: Path, pid: int) -> None:
    info = read_runtime_info(root)
    if info is not None and info.pid == pid:
        (root / RUNTIME_FILENAME).unlink(missing_ok=True)


def probe_runtime(info: RuntimeInfo, timeout_s: float = 0.5) -> bool:
    request = Request(f'{info.url}api/health', headers={'Accept': 'application/json'}, method='GET')
    try:
        with urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get('app_id') == APP_ID and payload.get('status') == 'ok'


def wait_for_runtime(root: Path, timeout_s: float = 10.0) -> RuntimeInfo | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = read_runtime_info(root)
        if info is not None and probe_runtime(info):
            return info
        time.sleep(0.1)
    return None
