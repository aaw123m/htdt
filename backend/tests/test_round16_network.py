"""Round-16 network-resilience regression tests.

Covers the HTDT-side HTTP surfaces against stub servers and hostile peers:

- ``htdt.rew_api.RewApiClient`` / ``htdt.rew_roomsim_batch.RewRoomSimControlClient``
  HTTP-status taxonomy and the wall-clock transfer deadline.
- ``htdt.ingress.read_response_bounded`` deadline behaviour.
- ``htdt.__main__.probe_htdt`` bounded health reads.
- ``htdt.capture_receiver`` request framing: truncated bodies, chunked
  Transfer-Encoding, and keep-alive desync prevention.
"""

from __future__ import annotations

import http.client
import io
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

import pytest

from htdt.__main__ import probe_htdt
from htdt.capture_receiver import (
    RECEIVER_MAX_RECEIPT_BYTES,
    RECEIVER_PATH_PREFIX,
    CaptureReceiverService,
    _make_handler,
)
from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import CaptureIngestionRepository
from htdt.capture_inbox import CaptureInboxRepository
from htdt.ingress import read_response_bounded
from htdt.rew_api import (
    RewApiClient,
    RewApiError,
    RewApiNotFound,
    RewApiUnavailable,
)
from htdt.rew_roomsim_batch import RewRoomSimControlClient


# ---------------------------------------------------------------------------
# helpers


@contextmanager
def _served(handler_cls):
    """Run ``handler_cls`` on a throwaway loopback HTTP server."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


def _fixed_json(body: bytes, status: int = 200):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    return Handler


def _raise_http(code: int):
    def opener(request, timeout):
        raise HTTPError(request.full_url, code, 'status', hdrs={}, fp=None)

    return opener


def _recv_until_eof(sock: socket.socket) -> bytes:
    data = bytearray()
    while True:
        try:
            chunk = sock.recv(65536)
        except OSError:
            break
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _receiver_service(tmp_path) -> CaptureReceiverService:
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    return CaptureReceiverService(
        scene,
        inbox,
        ingestion,
        bundle_reader=lambda body: ({}, {}),
        data_dir=tmp_path / 'receiver',
    )


def _receiver_port(service: CaptureReceiverService):
    server = ThreadingHTTPServer(('127.0.0.1', 0), _make_handler(service))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# REW API client: HTTP status taxonomy


class TestRewHttpStatusMapping:
    def test_404_maps_to_not_found_not_unavailable(self):
        client = RewApiClient(opener=_raise_http(404))
        with pytest.raises(RewApiNotFound):
            client.list_measurements()

    def test_500_maps_to_unavailable(self):
        client = RewApiClient(opener=_raise_http(500))
        with pytest.raises(RewApiUnavailable):
            client.list_measurements()

    def test_400_maps_to_api_error_not_unavailable(self):
        client = RewApiClient(opener=_raise_http(400))
        with pytest.raises(RewApiError) as excinfo:
            client.list_measurements()
        assert not isinstance(excinfo.value, (RewApiUnavailable, RewApiNotFound))

    def test_404_maps_to_not_found_end_to_end(self):
        with _served(_fixed_json(b'{"detail":"no such id"}', status=404)) as port:
            client = RewApiClient(f'http://127.0.0.1:{port}')
            with pytest.raises(RewApiNotFound):
                client.get_measurement('missing-id')

    def test_roomsim_post_404_maps_to_not_found(self):
        client = RewRoomSimControlClient(opener=_raise_http(404))
        with pytest.raises(RewApiNotFound):
            client.set_roomsim_head_position({'unit': 'metres', 'fromRear': 1.0})

    def test_roomsim_post_503_maps_to_unavailable(self):
        client = RewRoomSimControlClient(opener=_raise_http(503))
        with pytest.raises(RewApiUnavailable):
            client.set_roomsim_head_position({'unit': 'metres', 'fromRear': 1.0})


# ---------------------------------------------------------------------------
# Transfer deadline: drip-feeding peers cannot hold a request open


class _DripResponse:
    """Minimal response double with one-recv-per-call ``read1``."""

    def __init__(self, delay_s: float, chunk: bytes):
        self.delay_s = delay_s
        self.chunk = chunk

    def getheader(self, name):
        return None

    def read1(self, n: int) -> bytes:
        time.sleep(self.delay_s)
        return self.chunk[:n]

    def read(self, n: int) -> bytes:
        return self.chunk[:n]


class TestTransferDeadline:
    def test_bounded_read_deadline_stops_drip_feed(self):
        response = _DripResponse(delay_s=0.05, chunk=b'x')
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            read_response_bounded(response, 4096, deadline_s=0.3)
        assert time.monotonic() - started < 10

    def test_bounded_read_deadline_still_returns_full_body(self):
        class Buffer:
            def __init__(self, raw: bytes):
                self._buf = io.BytesIO(raw)

            def read1(self, n: int) -> bytes:
                return self._buf.read1(n)

        payload = read_response_bounded(
            Buffer(b'{"ok": true}'), 1024, deadline_s=5.0
        )
        assert payload == b'{"ok": true}'

    def test_bounded_read_without_read1_ignores_deadline(self):
        class Flat:
            def __init__(self, raw: bytes):
                self.raw = raw

            def read(self, n: int) -> bytes:
                out, self.raw = self.raw[:n], self.raw[n:]
                return out

        payload = read_response_bounded(Flat(b'abc'), 8, deadline_s=0.1)
        assert payload == b'abc'

    def test_slow_drip_get_raises_unavailable_via_stub_server(self):
        class Drip(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', '1048576')
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                        time.sleep(0.02)
                except OSError:
                    pass

            def log_message(self, *args):
                return

        with _served(Drip) as port:
            client = RewApiClient(
                f'http://127.0.0.1:{port}',
                timeout_s=5.0,
                transfer_timeout_s=0.3,
            )
            started = time.monotonic()
            with pytest.raises(RewApiUnavailable):
                client.list_measurements()
            assert time.monotonic() - started < 10


# ---------------------------------------------------------------------------
# Launcher probe


_HEALTHY = json.dumps(
    {'status': 'ok', 'platform_target': 'Windows 11 x64', 'schema_version': 1}
).encode('utf-8')


class TestProbeHtdt:
    def test_probe_accepts_healthy_backend(self):
        with _served(_fixed_json(_HEALTHY)) as port:
            assert probe_htdt(f'http://127.0.0.1:{port}/', timeout_s=2.0) is True

    def test_probe_rejects_oversized_body(self):
        with _served(_fixed_json(b' ' * (128 * 1024))) as port:
            assert probe_htdt(f'http://127.0.0.1:{port}/', timeout_s=2.0) is False

    def test_probe_rejects_stalled_body(self):
        class Stalled(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header('Content-Length', '4096')
                self.end_headers()
                try:
                    time.sleep(30)
                finally:
                    return

            def log_message(self, *args):
                return

        with _served(Stalled) as port:
            started = time.monotonic()
            assert probe_htdt(f'http://127.0.0.1:{port}/', timeout_s=0.3) is False
            assert time.monotonic() - started < 10


# ---------------------------------------------------------------------------
# Capture receiver request framing


class TestReceiverFraming:
    def test_truncated_post_body_rejected(self, tmp_path):
        service = _receiver_service(tmp_path)
        server = _receiver_port(service)
        try:
            port = server.server_address[1]
            sock = socket.create_connection(('127.0.0.1', port), timeout=10)
            sock.sendall(
                f'POST {RECEIVER_PATH_PREFIX}/tok/deliveries HTTP/1.1\r\n'
                'Host: x\r\nContent-Length: 64\r\n\r\n'.encode()
                + b'short'
            )
            sock.shutdown(socket.SHUT_WR)
            data = _recv_until_eof(sock)
            sock.close()
        finally:
            server.shutdown()
            server.server_close()
        status_line = data.split(b'\r\n', 1)[0]
        assert b' 400 ' in status_line
        assert b'incomplete request body' in data

    def test_chunked_post_rejected_and_connection_closed(self, tmp_path):
        service = _receiver_service(tmp_path)
        server = _receiver_port(service)
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            # A generator body has no len(), so http.client frames it with
            # real chunked Transfer-Encoding.
            conn.request(
                'POST',
                f'{RECEIVER_PATH_PREFIX}/tok/deliveries',
                body=iter([b'{}']),
                encode_chunked=True,
            )
            response = conn.getresponse()
            body = json.loads(response.read())
            assert response.status == 400
            assert 'Transfer-Encoding' in body['detail']
            # The connection is closed rather than desynced on the unread
            # chunked stream, and the response says so honestly.
            assert response.getheader('Connection') == 'close'
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_oversized_post_closes_connection(self, tmp_path):
        service = _receiver_service(tmp_path)
        server = _receiver_port(service)
        try:
            port = server.server_address[1]
            sock = socket.create_connection(('127.0.0.1', port), timeout=10)
            sock.sendall(
                f'POST {RECEIVER_PATH_PREFIX}/tok/missions/pkg-1/receipt HTTP/1.1\r\n'
                f'Host: x\r\nContent-Length: {RECEIVER_MAX_RECEIPT_BYTES + 1}\r\n'
                '\r\n'.encode()
            )
            data = _recv_until_eof(sock)
            sock.close()
        finally:
            server.shutdown()
            server.server_close()
        assert b' 413 ' in data.split(b'\r\n', 1)[0]
        assert b'byte ceiling' in data

    def test_unknown_route_post_with_body_closes_connection(self, tmp_path):
        service = _receiver_service(tmp_path)
        server = _receiver_port(service)
        try:
            port = server.server_address[1]
            sock = socket.create_connection(('127.0.0.1', port), timeout=10)
            sock.sendall(
                b'POST /elsewhere HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\nJUNK'
            )
            data = _recv_until_eof(sock)
            sock.close()
        finally:
            server.shutdown()
            server.server_close()
        assert b' 404 ' in data.split(b'\r\n', 1)[0]

    def test_get_with_declared_body_closes_instead_of_desyncing(self, tmp_path):
        service = _receiver_service(tmp_path)
        server = _receiver_port(service)
        try:
            port = server.server_address[1]
            sock = socket.create_connection(('127.0.0.1', port), timeout=10)
            sock.sendall(
                f'GET {RECEIVER_PATH_PREFIX}/tok/capabilities HTTP/1.1\r\n'
                'Host: x\r\nContent-Length: 7\r\n\r\nGARBAGE'.encode()
            )
            data = _recv_until_eof(sock)
            sock.close()
        finally:
            server.shutdown()
            server.server_close()
        # One clean 404 response, then EOF: the stray body bytes are never
        # parsed as a second request.
        assert data.count(b'HTTP/1.1 ') == 1
        assert b' 404 ' in data.split(b'\r\n', 1)[0]

    def test_keep_alive_ordering_after_fully_drained_post(self, tmp_path):
        service = _receiver_service(tmp_path)
        server = _receiver_port(service)
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            conn.request(
                'POST',
                f'{RECEIVER_PATH_PREFIX}/tok/deliveries',
                body=b'abc',
                headers={'Content-Length': '3'},
            )
            first = conn.getresponse()
            first.read()
            assert first.status == 404  # unpaired token, body fully consumed
            conn.request('GET', f'{RECEIVER_PATH_PREFIX}/tok/capabilities')
            second = conn.getresponse()
            second.read()
            assert second.status == 404
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
