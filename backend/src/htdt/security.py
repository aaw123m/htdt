from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

from .ingress import IngressTooLargeError, read_stream_bounded
from .limits import (
    MAX_ATTACHMENT_REQUEST_BODY_BYTES,
    MAX_REW_REQUEST_BODY_BYTES,
    MAX_SMALL_JSON_BODY_BYTES,
)


LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})
UNSAFE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})


def _hostname_from_authority(authority: str) -> str | None:
    if not authority or any(character in authority for character in '\r\n'):
        return None
    try:
        return urlsplit(f'//{authority}').hostname
    except ValueError:
        return None


def is_allowed_host(authority: str | None, *, allow_testserver: bool = False) -> bool:
    if authority is None:
        return False
    hostname = _hostname_from_authority(authority)
    if hostname is None:
        return False
    hostname = hostname.lower()
    return hostname in LOOPBACK_HOSTS or (allow_testserver and hostname == 'testserver')


def is_allowed_origin(origin: str | None) -> bool:
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme != 'http' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    hostname = parsed.hostname.lower() if parsed.hostname else None
    return hostname in LOOPBACK_HOSTS


def request_body_limit(path: str, method: str) -> int | None:
    if method.upper() not in UNSAFE_METHODS:
        return None
    if path == '/api/import/preview' or (path.startswith('/api/projects/') and path.endswith('/measurements')):
        return MAX_REW_REQUEST_BODY_BYTES
    if path.startswith('/api/projects/') and path.endswith('/attachments'):
        return MAX_ATTACHMENT_REQUEST_BODY_BYTES
    return MAX_SMALL_JSON_BODY_BYTES


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError as exc:
        raise ValueError('Invalid Content-Length') from exc
    if length < 0:
        raise ValueError('Invalid Content-Length')
    return length


def install_local_request_boundary(app: object, *, allow_testserver: bool = False) -> None:
    # FastAPI exposes `middleware` dynamically; keeping the dependency surface here small
    # makes the policy functions independently testable.
    middleware = getattr(app, 'middleware')

    @middleware('http')
    async def local_request_boundary(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not is_allowed_host(request.headers.get('host'), allow_testserver=allow_testserver):
            return JSONResponse({'detail': 'Host must be localhost or loopback'}, status_code=400)

        if request.method.upper() in UNSAFE_METHODS and not is_allowed_origin(request.headers.get('origin')):
            return JSONResponse({'detail': 'Origin must be localhost or loopback'}, status_code=403)

        body_limit = request_body_limit(request.url.path, request.method)
        if body_limit is not None:
            try:
                content_length = _content_length(request.headers.get('content-length'))
            except ValueError:
                return JSONResponse({'detail': 'Invalid Content-Length'}, status_code=400)
            if content_length is not None and content_length > body_limit:
                return JSONResponse({'detail': f'Request body exceeds {body_limit} bytes'}, status_code=413)
            # Content-Length is only a fast-path preflight: a chunked or absent
            # declaration still delivers a byte stream, so the body is bounded
            # on the bytes actually received. When it fits, the buffered body is
            # cached like Request.body() does so BaseHTTPMiddleware replays it to
            # the downstream handler exactly once.
            try:
                request._body = await read_stream_bounded(
                    request.stream(), body_limit, label='request body'
                )
            except IngressTooLargeError:
                return JSONResponse({'detail': f'Request body exceeds {body_limit} bytes'}, status_code=413)
            except ClientDisconnect:
                return JSONResponse({'detail': 'Client disconnected'}, status_code=400)

        return await call_next(request)
