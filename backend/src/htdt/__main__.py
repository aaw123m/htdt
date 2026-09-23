from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import BinaryIO, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import webbrowser

import uvicorn


HOST = '127.0.0.1'
DEFAULT_PORT = 8765
LOCK_FILENAME = '.htdt-instance.lock'


def default_data_dir() -> Path:
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        return Path(local_app_data) / 'HomeTheaterDigitalTwin'
    return Path.home() / '.home-theater-digital-twin'


class InstanceLock:
    """Process-held lock for the default HTDT data directory.

    Byte 0 is reserved for the OS lock. JSON metadata starts at byte 1 so a
    second launcher can read the existing instance URL without touching the
    locked byte range on Windows.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None
        self._owned = False

    def _open(self) -> BinaryIO:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, 'r+b', buffering=0)
        handle.seek(0, os.SEEK_END)
        if handle.tell() < 1:
            handle.seek(0)
            handle.write(b'L')
            handle.flush()
        return handle

    def acquire(self) -> bool:
        if self._handle is None:
            self._handle = self._open()
        if self._owned:
            return True
        handle = self._handle
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self._owned = True
        return True

    def write_metadata(self, payload: dict[str, object]) -> None:
        if not self._owned or self._handle is None:
            raise RuntimeError('instance lock is not owned')
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
        self._handle.truncate(1)
        self._handle.seek(1)
        self._handle.write(encoded)
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def read_metadata(self) -> dict[str, object] | None:
        handle = self._handle
        close_after = False
        if handle is None:
            try:
                handle = self._open()
            except OSError:
                return None
            close_after = True
        try:
            handle.seek(1)
            raw = handle.read()
            if not raw:
                return None
            payload = json.loads(raw.decode('utf-8'))
            return payload if isinstance(payload, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        finally:
            if close_after:
                handle.close()

    def close(self) -> None:
        if self._handle is None:
            return
        if self._owned:
            self._handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt

                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._owned = False
        self._handle.close()
        self._handle = None

    def __enter__(self) -> 'InstanceLock':
        if not self.acquire():
            raise RuntimeError('instance lock is already held')
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def reserve_listening_socket(host: str = HOST, preferred_port: int = DEFAULT_PORT) -> tuple[socket.socket, int]:
    """Reserve the preferred loopback port, or an OS-selected free port when it is busy."""
    if not (1 <= preferred_port <= 65535):
        raise ValueError('port must be between 1 and 65535')

    preferred = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        preferred.bind((host, preferred_port))
        preferred.listen(2048)
        return preferred, int(preferred.getsockname()[1])
    except OSError:
        preferred.close()

    fallback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        fallback.bind((host, 0))
        fallback.listen(2048)
        return fallback, int(fallback.getsockname()[1])
    except Exception:
        fallback.close()
        raise


def local_url(port: int) -> str:
    return f'http://{HOST}:{port}/'


def _valid_local_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or port is None:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if parsed.path not in {'', '/'}:
        return None
    return f'http://{parsed.hostname}:{port}/'


def probe_htdt(url: str, timeout_s: float = 0.35) -> bool:
    request = Request(f'{url}api/health', headers={'Accept': 'application/json'}, method='GET')
    try:
        with urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get('status') == 'ok'
        and payload.get('platform_target') == 'Windows 11 x64'
        and isinstance(payload.get('schema_version'), int)
    )


def wait_for_existing_instance(
    lock: InstanceLock,
    *,
    timeout_s: float = 8.0,
    probe: Callable[[str], bool] = probe_htdt,
) -> str | None:
    deadline = time.monotonic() + timeout_s
    last_url: str | None = None
    while time.monotonic() < deadline:
        metadata = lock.read_metadata()
        last_url = _valid_local_url(metadata.get('url')) if metadata else None
        if last_url and probe(last_url):
            return last_url
        time.sleep(0.12)
    return None


def open_browser_when_ready(port: int, *, timeout_s: float = 10.0) -> None:
    """Open the local UI after the reserved socket starts accepting connections."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=0.25):
                webbrowser.open(local_url(port), new=2)
                return
        except OSError:
            time.sleep(0.1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run Home Theater Digital Twin on localhost.')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'preferred localhost port (default: {DEFAULT_PORT})')
    parser.add_argument('--no-browser', action='store_true', help='do not open the default browser automatically')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lock = InstanceLock(default_data_dir() / LOCK_FILENAME)
    if not lock.acquire():
        existing_url = wait_for_existing_instance(lock)
        if existing_url:
            print(f'Home Theater Digital Twin is already running: {existing_url}')
            if not args.no_browser:
                webbrowser.open(existing_url, new=2)
            lock.close()
            return 0
        print('Home Theater Digital Twin is already starting or running, but its local UI did not answer yet.')
        lock.close()
        return 2

    listener: socket.socket | None = None
    try:
        listener, port = reserve_listening_socket(preferred_port=args.port)
        url = local_url(port)
        lock.write_metadata({
            'url': url,
            'pid': os.getpid(),
            'started_at': datetime.now(timezone.utc).isoformat(),
        })
        print(f'Home Theater Digital Twin: {url}')
        if port != args.port:
            print(f'Preferred port {args.port} was busy; using {port} instead.')

        if not args.no_browser:
            threading.Thread(target=open_browser_when_ready, args=(port,), daemon=True).start()

        config = uvicorn.Config(
            'htdt.server:app',
            host=HOST,
            port=port,
            reload=False,
            log_level='info',
        )
        server = uvicorn.Server(config)
        server.run(sockets=[listener])
        return 0
    finally:
        if listener is not None:
            listener.close()
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
