"""Shared canonical-JSON contract: the leaf every module delegates to.

Round-2 consolidation moved ~460 module-local strict-json/sha256 helper
copies onto this module; these tests pin the wire contract so aliases and
delegating wrappers cannot drift.
"""

from __future__ import annotations

import json

import pytest

from htdt.canonical_json import canonical_json, canonical_sha256


def test_canonical_json_is_strict_and_sorted() -> None:
    payload = {'b': 1, 'a': 'x', 'nested': {'z': [3, 2], 'y': None}}
    assert canonical_json(payload) == '{"a":"x","b":1,"nested":{"y":null,"z":[3,2]}}'


def test_canonical_json_keeps_non_ascii() -> None:
    assert canonical_json({'name': 'héllo'}) == '{"name":"héllo"}'


def test_canonical_json_rejects_nan_and_infinity() -> None:
    for bad in (float('nan'), float('inf'), float('-inf')):
        with pytest.raises(ValueError):
            canonical_json({'v': bad})
        with pytest.raises(ValueError):
            canonical_sha256({'v': bad})


def test_canonical_sha256_matches_dumps_of_strict_form() -> None:
    from hashlib import sha256

    payload = {'x': [1, 2], 'y': 'z'}
    assert canonical_sha256(payload) == sha256(
        canonical_json(payload).encode('utf-8')
    ).hexdigest()


def test_canonical_helpers_match_representative_module_wrappers() -> None:
    """A few surviving delegating wrappers stay byte-identical with the leaf."""
    from htdt.acoustic_benchmark import (
        canonical_benchmark_json,
        canonical_benchmark_sha256,
    )
    from htdt.capture_connected_space import _hash as _connected_space_hash
    from htdt.treatment_boundary_overlay import _semantic_hash

    payload = {'k': {'j': [1, 'é']}, 'a': None}
    assert canonical_benchmark_json(payload) == canonical_json(payload)
    assert canonical_benchmark_sha256(payload) == canonical_sha256(payload)
    assert _semantic_hash(payload) == canonical_sha256(payload)
    # capture_connected_space._hash wraps the payload in a domain envelope
    assert _connected_space_hash('d', payload) == canonical_sha256(
        {'domain': 'd', 'payload': payload}
    )
