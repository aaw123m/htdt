"""Shared XML intake guard.

Python's ``xml.etree.ElementTree`` still expands internal DTD entities —
pyexpat does not expose the billion-laughs protection knobs, so a
``<!DOCTYPE``-bearing document can amplify a small payload into a large
tree (quadratic/billion-laughs), and ``<!ENTITY`` declarations are the
mechanism. HTDT's XML consumers only need element trees, so documents
carrying a DOCTYPE or ENTITY declaration are rejected outright before
parsing. All matches are done case-insensitively on the raw payload so
whitespace tricks (``<!  DOCTYPE``) cannot slip by.
"""

from __future__ import annotations

import re

_DOCTYPE_BYTES = re.compile(rb'<!DOCTYPE|<!ENTITY', re.IGNORECASE)
_DOCTYPE_TEXT = re.compile(r'<!DOCTYPE|<!ENTITY', re.IGNORECASE)


def contains_xml_doctype(payload: bytes | str) -> bool:
    """True when the payload declares a DOCTYPE/ENTITY (entity expansion risk)."""
    if isinstance(payload, str):
        return _DOCTYPE_TEXT.search(payload) is not None
    return _DOCTYPE_BYTES.search(payload) is not None


__all__ = ['contains_xml_doctype']
