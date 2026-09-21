from __future__ import annotations

from hashlib import sha256
import sqlite3


CONTENT_BLOB_TABLE = 'htdt_content_blobs'

CONTENT_BLOB_DDL = f'''
CREATE TABLE IF NOT EXISTS {CONTENT_BLOB_TABLE} (
    payload_sha256 TEXT PRIMARY KEY,
    byte_count INTEGER NOT NULL,
    payload_blob BLOB NOT NULL
)
'''


class ContentBlobStoreError(ValueError):
    pass


def ensure_content_blob_store(connection: sqlite3.Connection) -> None:
    """Create the canonical content-addressed blob table when missing.

    The table is normally created by the native schema v4 migration; this
    helper keeps repository initialisation self-sufficient and idempotent.
    """

    connection.execute(CONTENT_BLOB_DDL)


def store_content_blob(
    connection: sqlite3.Connection,
    payload: bytes,
    *,
    expected_sha256: str | None = None,
) -> str:
    """Store payload once under its SHA-256 and return the digest.

    Identical content is deduplicated: re-storing the same bytes is a no-op.
    A digest collision with different stored content fails closed.
    """

    if not isinstance(payload, bytes):
        raise TypeError('content blob payload must be immutable bytes')
    digest = sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ContentBlobStoreError(
            'content blob payload does not match its expected SHA-256'
        )
    connection.execute(
        f'''
        INSERT OR IGNORE INTO {CONTENT_BLOB_TABLE}(
            payload_sha256, byte_count, payload_blob
        ) VALUES (?, ?, ?)
        ''',
        (digest, len(payload), payload),
    )
    row = connection.execute(
        f'SELECT length(payload_blob) FROM {CONTENT_BLOB_TABLE} '
        'WHERE payload_sha256=?',
        (digest,),
    ).fetchone()
    if row is None or int(row[0]) != len(payload):
        raise ContentBlobStoreError(
            'content blob store integrity mismatch after write'
        )
    return digest


def read_content_blob(
    connection: sqlite3.Connection,
    payload_sha256: str,
) -> bytes | None:
    """Return the canonical bytes for a digest, or None when absent.

    The content-addressed contract is self-verifying: stored bytes must hash
    back to their key before they are returned.
    """

    row = connection.execute(
        f'SELECT byte_count, payload_blob FROM {CONTENT_BLOB_TABLE} '
        'WHERE payload_sha256=?',
        (payload_sha256,),
    ).fetchone()
    if row is None:
        return None
    payload = bytes(row[1])
    if (
        len(payload) != int(row[0])
        or sha256(payload).hexdigest() != payload_sha256
    ):
        raise ContentBlobStoreError(
            f'content blob integrity mismatch: {payload_sha256}'
        )
    return payload
