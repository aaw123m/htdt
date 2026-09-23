from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from htdt.limits import (
    MAX_ATTACHMENT_REQUEST_BODY_BYTES,
    MAX_REW_REQUEST_BODY_BYTES,
    MAX_SMALL_JSON_BODY_BYTES,
)
from htdt.security import install_local_request_boundary, is_allowed_host, is_allowed_origin, request_body_limit


def make_client(*, allow_testserver: bool = False) -> TestClient:
    app = FastAPI()
    install_local_request_boundary(app, allow_testserver=allow_testserver)

    @app.get('/read')
    def read() -> dict[str, bool]:
        return {'ok': True}

    @app.post('/write')
    def write() -> dict[str, bool]:
        return {'ok': True}

    @app.post('/echo')
    async def echo(request: Request) -> dict[str, object]:
        body = await request.body()
        return {'received': len(body), 'content_length': request.headers.get('content-length')}

    return TestClient(app, base_url='http://127.0.0.1:8765')


def test_loopback_host_and_origin_are_allowed() -> None:
    client = make_client()
    assert client.get('/read').status_code == 200
    assert client.post('/write', headers={'Origin': 'http://127.0.0.1:8765'}).status_code == 200
    assert client.post('/write', headers={'Origin': 'http://localhost:5173'}).status_code == 200


def test_non_loopback_host_is_rejected_even_for_get() -> None:
    client = make_client()
    response = client.get('/read', headers={'Host': 'example.test'})
    assert response.status_code == 400
    assert response.json()['detail'] == 'Host must be localhost or loopback'


def test_non_loopback_origin_is_rejected_for_write() -> None:
    client = make_client()
    response = client.post('/write', headers={'Origin': 'https://example.test'})
    assert response.status_code == 403
    assert response.json()['detail'] == 'Origin must be localhost or loopback'


def test_write_without_origin_remains_available_to_local_clients() -> None:
    client = make_client()
    assert client.post('/write').status_code == 200


def test_oversized_declared_body_is_rejected_before_route() -> None:
    client = make_client()
    response = client.post('/write', content=b'{}', headers={'Content-Length': str(MAX_SMALL_JSON_BODY_BYTES + 1)})
    assert response.status_code == 413


def test_oversized_declared_body_uses_endpoint_specific_limit() -> None:
    client = make_client()
    response = client.post(
        '/api/import/preview',
        content=b'{}',
        headers={'Content-Length': str(MAX_REW_REQUEST_BODY_BYTES + 1)},
    )
    assert response.status_code == 413


def test_chunked_body_at_exactly_the_limit_reaches_route() -> None:
    client = make_client()
    half = MAX_SMALL_JSON_BODY_BYTES // 2
    chunks = iter([b'x' * half, b'y' * (MAX_SMALL_JSON_BODY_BYTES - half)])
    response = client.post('/echo', content=chunks)
    assert response.status_code == 200
    # An iterable body is sent chunked, without a Content-Length header.
    assert response.json() == {
        'received': MAX_SMALL_JSON_BODY_BYTES,
        'content_length': None,
    }


def test_chunked_body_one_byte_over_limit_is_rejected_before_route() -> None:
    client = make_client()
    # The byte that crosses the limit arrives in a later chunk: the cap is on
    # the bytes actually delivered, not on a declared length.
    chunks = iter([b'x' * MAX_SMALL_JSON_BODY_BYTES, b'x'])
    response = client.post('/echo', content=chunks)
    assert response.status_code == 413
    assert response.json()['detail'] == f'Request body exceeds {MAX_SMALL_JSON_BODY_BYTES} bytes'


def test_honest_chunked_body_reaches_route_intact() -> None:
    client = make_client()
    payload = b'{"a": 1}'
    response = client.post('/echo', content=iter([payload[:3], payload[3:]]))
    assert response.status_code == 200
    assert response.json()['received'] == len(payload)


def test_honest_declared_body_still_passes() -> None:
    client = make_client()
    response = client.post('/echo', content=b'{"a": 1}')
    assert response.status_code == 200
    assert response.json()['received'] == len(b'{"a": 1}')


def test_endpoint_specific_body_limits() -> None:
    assert request_body_limit('/api/import/preview', 'POST') == MAX_REW_REQUEST_BODY_BYTES
    assert request_body_limit('/api/projects/p1/measurements', 'POST') == MAX_REW_REQUEST_BODY_BYTES
    assert request_body_limit('/api/projects/p1/attachments', 'POST') == MAX_ATTACHMENT_REQUEST_BODY_BYTES
    # The retired /api/restore endpoint gets no dedicated allowance: it falls
    # under the strict default bound like every other small JSON write.
    assert request_body_limit('/api/restore', 'POST') == MAX_SMALL_JSON_BODY_BYTES
    assert request_body_limit('/api/projects', 'POST') == MAX_SMALL_JSON_BODY_BYTES
    assert request_body_limit('/api/projects', 'GET') is None


def test_https_loopback_origin_is_rejected_because_htdt_is_http_only() -> None:
    assert is_allowed_origin('https://127.0.0.1:8765') is False


def test_testserver_is_only_allowed_when_explicitly_enabled() -> None:
    assert is_allowed_host('testserver') is False
    assert is_allowed_host('testserver', allow_testserver=True) is True


def test_ipv6_loopback_authority_is_supported() -> None:
    assert is_allowed_host('[::1]:8765') is True
    assert is_allowed_origin('http://[::1]:8765') is True
