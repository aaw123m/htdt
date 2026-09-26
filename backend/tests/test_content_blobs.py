"""Direct contract tests for the content-addressed blob store.

``content_blobs`` backs every stored payload family (capture bundles,
measurement assets, generated artifacts); its fail-closed digest checks are
the last line of defense against silent persistence corruption.
"""

from __future__ import annotations

from hashlib import sha256
import sqlite3

import pytest

from htdt.content_blobs import (
    CONTENT_BLOB_TABLE,
    ContentBlobStoreError,
    ensure_content_blob_store,
    read_content_blob,
    store_content_blob,
)


@pytest.fixture
def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    try:
        ensure_content_blob_store(conn)
        yield conn
    finally:
        conn.close()


def test_store_returns_sha256_and_roundtrips(connection: sqlite3.Connection) -> None:
    payload = b'canonical payload bytes'
    digest = store_content_blob(connection, payload)

    assert digest == sha256(payload).hexdigest()
    assert read_content_blob(connection, digest) == payload


def test_store_is_idempotent_and_deduplicates(connection: sqlite3.Connection) -> None:
    payload = b'duplicate content'
    first = store_content_blob(connection, payload)
    second = store_content_blob(connection, payload)

    assert first == second
    rows = connection.execute(
        f'SELECT COUNT(*) FROM {CONTENT_BLOB_TABLE}'
    ).fetchone()
    assert rows[0] == 1


def test_store_rejects_expected_sha256_mismatch(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(ContentBlobStoreError, match='expected SHA-256'):
        store_content_blob(connection, b'x', expected_sha256='0' * 64)

    # The rejected write must not have persisted anything under the real digest.
    assert read_content_blob(connection, sha256(b'x').hexdigest()) is None


def test_store_accepts_matching_expected_sha256(
    connection: sqlite3.Connection,
) -> None:
    payload = b'pinned'
    digest = store_content_blob(
        connection, payload, expected_sha256=sha256(payload).hexdigest()
    )
    assert digest == sha256(payload).hexdigest()


def test_store_rejects_non_bytes_payload(connection: sqlite3.Connection) -> None:
    with pytest.raises(TypeError, match='immutable bytes'):
        store_content_blob(connection, 'not-bytes')  # type: ignore[arg-type]
    with pytest.raises(TypeError, match='immutable bytes'):
        store_content_blob(connection, bytearray(b'mutable'))  # type: ignore[arg-type]


def test_read_returns_none_for_absent_digest(
    connection: sqlite3.Connection,
) -> None:
    assert read_content_blob(connection, 'f' * 64) is None


def test_read_fails_closed_on_tampered_payload(
    connection: sqlite3.Connection,
) -> None:
    payload = b'original bytes'
    digest = store_content_blob(connection, payload)

    connection.execute(
        f'UPDATE {CONTENT_BLOB_TABLE} SET payload_blob=? '
        'WHERE payload_sha256=?',
        (b'tampered bytes!', digest),
    )

    with pytest.raises(ContentBlobStoreError, match='integrity mismatch'):
        read_content_blob(connection, digest)


def test_read_fails_closed_on_tampered_byte_count(
    connection: sqlite3.Connection,
) -> None:
    payload = b'exact bytes'
    digest = store_content_blob(connection, payload)

    connection.execute(
        f'UPDATE {CONTENT_BLOB_TABLE} SET byte_count=byte_count+1 '
        'WHERE payload_sha256=?',
        (digest,),
    )

    with pytest.raises(ContentBlobStoreError, match='integrity mismatch'):
        read_content_blob(connection, digest)


def test_store_fails_closed_on_forged_row_same_length(
    connection: sqlite3.Connection,
) -> None:
    """A pre-existing row keyed by the payload's digest whose bytes do not
    hash back — even at equal length — must fail the write path, not pass as
    a silent dedup."""
    payload = b'abcd'
    digest = sha256(payload).hexdigest()
    connection.execute(
        f'INSERT INTO {CONTENT_BLOB_TABLE}'
        '(payload_sha256, byte_count, payload_blob) VALUES (?, ?, ?)',
        (digest, 4, b'junk'),
    )

    with pytest.raises(ContentBlobStoreError, match='integrity mismatch'):
        store_content_blob(connection, payload)
    with pytest.raises(ContentBlobStoreError, match='integrity mismatch'):
        read_content_blob(connection, digest)


def test_store_fails_closed_on_forged_row_other_key(
    connection: sqlite3.Connection,
) -> None:
    """A forged row under an unrelated digest must not disturb normal writes."""
    connection.execute(
        f'INSERT INTO {CONTENT_BLOB_TABLE}'
        '(payload_sha256, byte_count, payload_blob) VALUES (?, ?, ?)',
        ('a' * 64, 4, b'junk'),
    )
    payload = b'legit payload'
    assert store_content_blob(connection, payload) == sha256(payload).hexdigest()
    with pytest.raises(ContentBlobStoreError, match='integrity mismatch'):
        read_content_blob(connection, 'a' * 64)


def test_ensure_is_idempotent(connection: sqlite3.Connection) -> None:
    ensure_content_blob_store(connection)
    ensure_content_blob_store(connection)
    digest = store_content_blob(connection, b'still works')
    assert read_content_blob(connection, digest) == b'still works'
