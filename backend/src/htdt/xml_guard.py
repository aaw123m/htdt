"""Shared XML intake guard.

Python's ``xml.etree.ElementTree`` still expands internal DTD entities —
pyexpat does not expose the billion-laughs protection knobs, so a
``<!DOCTYPE``-bearing document can amplify a small payload into a large
tree (quadratic/billion-laughs), and ``<!ENTITY`` declarations are the
mechanism. HTDT's XML consumers only need element trees, so documents
carrying a DOCTYPE or ENTITY declaration are rejected outright before
parsing. All matches are done case-insensitively on the raw payload so
whitespace tricks (``<!  DOCTYPE``) cannot slip by.

BOM-prefixed UTF-16/UTF-32 payloads carry non-ASCII bytes that never
match the byte pattern, so those encodings are decoded and rescanned —
pyexpat expands internal DTD entities rather than rejecting them, so
declining at the guard is the actual barrier.
"""

from __future__ import annotations

import codecs
import re

_DOCTYPE_BYTES = re.compile(rb'<!DOCTYPE|<!ENTITY', re.IGNORECASE)
_DOCTYPE_TEXT = re.compile(r'<!DOCTYPE|<!ENTITY', re.IGNORECASE)

# Byte-order marks for encodings whose units are wider than ASCII bytes.
_UTF_WIDE_BOMS = (
    (codecs.BOM_UTF32_LE, 'utf-32-le'),
    (codecs.BOM_UTF32_BE, 'utf-32-be'),
    (codecs.BOM_UTF16_LE, 'utf-16-le'),
    (codecs.BOM_UTF16_BE, 'utf-16-be'),
)


def contains_xml_doctype(payload: bytes | str) -> bool:
    """True when the payload declares a DOCTYPE/ENTITY (entity expansion risk)."""
    if isinstance(payload, str):
        return _DOCTYPE_TEXT.search(payload) is not None
    if _DOCTYPE_BYTES.search(payload) is not None:
        return True
    for bom, encoding in _UTF_WIDE_BOMS:
        if not payload.startswith(bom):
            continue
        try:
            text = payload[len(bom):].decode(encoding)
        except UnicodeDecodeError:
            continue
        return _DOCTYPE_TEXT.search(text) is not None
    # BOM-less UTF-16: pyexpat also sniffs '<\x00?\x00' / '\x00<\x00?'
    # document starts, so the guard must too.
    for prefix, encoding in ((b'<\x00?\x00', 'utf-16-le'), (b'\x00<\x00?', 'utf-16-be')):
        if payload.startswith(prefix):
            try:
                text = payload.decode(encoding)
            except UnicodeDecodeError:
                return False
            return _DOCTYPE_TEXT.search(text) is not None
    return False


__all__ = ['contains_xml_doctype']
