"""Shared canonical-JSON serialization and digest helpers.

One canonical profile is used across persistence, audit, evidence and
replay hashing so that identical payloads produce byte-identical output
in every module:

- ``ensure_ascii=False`` — UTF-8 text, no surrogate escapes;
- ``sort_keys=True`` — deterministic key order;
- ``separators=(',', ':')`` — compact, insignificant-whitespace-free;
- ``allow_nan=False`` — fail-closed: NaN/Infinity raise ``ValueError``
  instead of emitting non-JSON tokens that would hash divergently.

Anything hashing "the same" document through a different profile produces
a different digest, so these helpers are the single implementation every
call site should share.
"""
from __future__ import annotations

from hashlib import sha256
import json


def canonical_json(payload: object) -> str:
    """Serialize *payload* under the shared canonical JSON profile."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def canonical_sha256(payload: object) -> str:
    """SHA-256 hex digest of ``canonical_json(payload)``."""
    return sha256(canonical_json(payload).encode('utf-8')).hexdigest()


def hash_parts(prefix: str, *parts: str) -> str:
    """SHA-256 hex digest of NUL-separated ``prefix`` and ``parts``.

    Domain-separated identity hashing: sha256 over ``prefix`` then each
    part, NUL-separated — the frame keeps ('a', 'bc') and ('ab', 'c')
    distinct.
    """
    digest = sha256(prefix.encode('utf-8'))
    for part in parts:
        digest.update(b'\x00')
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()
