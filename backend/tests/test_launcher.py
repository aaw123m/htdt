from __future__ import annotations

import socket
from pathlib import Path

import pytest

from htdt.__main__ import (
    DEFAULT_PORT,
    HOST,
    InstanceLock,
    _valid_local_url,
    build_parser,
    default_data_dir,
    local_url,
    reserve_listening_socket,
    wait_for_existing_instance,
)


def test_local_url_is_loopback_only() -> None:
    assert local_url(8765) == 'http://127.0.0.1:8765/'


def test_preferred_port_is_reserved_when_free() -> None:
    listener, port = reserve_listening_socket(preferred_port=DEFAULT_PORT)
    try:
        assert port == DEFAULT_PORT
        assert listener.getsockname()[0] == HOST
    finally:
        listener.close()


def test_busy_preferred_port_falls_back_to_another_loopback_port() -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind((HOST, 0))
    occupied.listen(1)
    busy_port = int(occupied.getsockname()[1])
    listener = None
    try:
        listener, selected = reserve_listening_socket(preferred_port=busy_port)
        assert selected != busy_port
        assert 1 <= selected <= 65535
        assert listener.getsockname()[0] == HOST
    finally:
        if listener is not None:
            listener.close()
        occupied.close()


def test_invalid_preferred_port_is_rejected() -> None:
    with pytest.raises(ValueError, match='port'):
        reserve_listening_socket(preferred_port=0)


def test_cli_no_browser_and_port_are_parsed() -> None:
    args = build_parser().parse_args(['--port', '9000', '--no-browser'])
    assert args.port == 9000
    assert args.no_browser is True


def test_default_data_dir_uses_local_app_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert default_data_dir() == tmp_path / 'HomeTheaterDigitalTwin'


def test_instance_lock_is_exclusive_and_metadata_is_readable(tmp_path: Path) -> None:
    path = tmp_path / '.htdt-instance.lock'
    owner = InstanceLock(path)
    contender = InstanceLock(path)
    replacement = InstanceLock(path)
    try:
        assert owner.acquire() is True
        owner.write_metadata({'url': 'http://127.0.0.1:8765/', 'pid': 123})
        assert contender.acquire() is False
        assert contender.read_metadata() == {'pid': 123, 'url': 'http://127.0.0.1:8765/'}
        contender.close()
        owner.close()
        assert replacement.acquire() is True
    finally:
        contender.close()
        owner.close()
        replacement.close()


def test_existing_instance_requires_local_url_and_successful_probe(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / '.htdt-instance.lock')
    try:
        assert lock.acquire() is True
        lock.write_metadata({'url': 'http://127.0.0.1:9123/'})
        assert wait_for_existing_instance(lock, timeout_s=0.02, probe=lambda url: url.endswith(':9123/')) == 'http://127.0.0.1:9123/'
        assert wait_for_existing_instance(lock, timeout_s=0.02, probe=lambda _url: False) is None
    finally:
        lock.close()


def test_existing_instance_metadata_rejects_non_loopback_urls() -> None:
    assert _valid_local_url('http://127.0.0.1:8765/') == 'http://127.0.0.1:8765/'
    assert _valid_local_url('http://localhost:8765/') == 'http://localhost:8765/'
    assert _valid_local_url('http://192.168.1.20:8765/') is None
    assert _valid_local_url('https://127.0.0.1:8765/') is None
    assert _valid_local_url('http://127.0.0.1:8765/api') is None
