from __future__ import annotations

from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any


class IngressTooLargeError(ValueError):
    """User-correctable rejection: a selected input exceeds a resource limit.

    Native file import and localhost REW API reads share the resource-safety
    policy in htdt.limits with the legacy browser transport. Oversized inputs
    are rejected before the payload is decoded or expanded, and the message
    names the limit so the user can pick a smaller export or fix the source.
    """


def _too_large(label: str, max_bytes: int, actual_bytes: int | None) -> IngressTooLargeError:
    detail = (
        f'{actual_bytes} bytes'
        if actual_bytes is not None
        else 'more than the limit allows'
    )
    return IngressTooLargeError(
        f'{label} is too large: {detail} (limit {max_bytes} bytes)'
    )


def read_file_bounded(path: Path, max_bytes: int, *, label: str = 'file') -> bytes:
    """Read ``path`` only when its size is within ``max_bytes``.

    The ``stat()`` preflight rejects oversized files before the payload is
    allocated; the bounded read also rejects a file that grows between the
    preflight and the read.
    """
    if max_bytes < 0:
        raise ValueError('max_bytes must be non-negative')
    size = path.stat().st_size
    if size > max_bytes:
        raise _too_large(label, max_bytes, size)
    with path.open('rb') as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise _too_large(label, max_bytes, None)
    return payload


def read_response_bounded(response: Any, max_bytes: int, *, label: str = 'response') -> bytes:
    """Read an HTTP response body with a hard byte ceiling.

    An honest Content-Length above ``max_bytes`` is rejected without reading
    the body; the bounded ``read()`` remains authoritative when the header is
    missing or inaccurate.
    """
    if max_bytes < 0:
        raise ValueError('max_bytes must be non-negative')
    getheader = getattr(response, 'getheader', None)
    if callable(getheader):
        try:
            declared = int(getheader('Content-Length') or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > max_bytes:
            raise _too_large(label, max_bytes, declared)
    payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise _too_large(label, max_bytes, None)
    return payload


async def read_stream_bounded(stream: AsyncIterable[bytes], max_bytes: int, *, label: str = 'stream') -> bytes:
    """Consume an async byte stream with a hard byte ceiling.

    The bound is enforced on the bytes actually delivered, so a chunked or
    undeclared-length stream cannot grow past ``max_bytes`` even when no size
    was advertised up front. The error is raised as soon as the cumulative
    size exceeds the limit, without buffering beyond the offending chunk.
    """
    if max_bytes < 0:
        raise ValueError('max_bytes must be non-negative')
    chunks = bytearray()
    async for chunk in stream:
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise _too_large(label, max_bytes, None)
    return bytes(chunks)
