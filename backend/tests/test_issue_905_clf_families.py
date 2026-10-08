# -*- coding: utf-8 -*-
"""#905: CLF payload-family honesty — authoring text vs secured binaries.

The distributed form manufacturers ship is binary ``.CF1``/``.CF2``
(ODEON manual §3.3, CATT, PRONOM fmt/1944+fmt/1945), not the authoring
text the qualifier validates. A recognized binary must qualify
``unsupported`` — never ``not_clf`` (which claimed the payload was not
CLF at all) and never ``qualified`` (which would assert compatibility
from text fixtures). Unknown binaries stay rejected with the reason.
"""

from __future__ import annotations

import os

from htdt.cad_loudspeaker_interchange import (
    INTERCHANGE_MATRIX,
    ClfQualification,
    detect_clf_family,
    qualify_clf,
)


# --- signature-correct binary fixtures (PRONOM seqs, not real payloads) ---

def _cf(lead: int, version_digit: bytes = b'1') -> bytes:
    """Minimal signature-conforming .CF1/.CF2-shaped payload.

    Layout per PRONOM: lead byte (0x40/0x41) + BD 0A 00 01, 15 arbitrary
    bytes, ASCII ``v{1,2}.0`` at offset 20, then filler. Payload bytes
    beyond the signature are intentionally meaningless — detection must
    never imply we decoded them.
    """
    head = bytes([lead, 0xBD, 0x0A, 0x00, 0x01])
    gap = bytes(15)
    marker = b'v' + version_digit + b'.0'
    tail = bytes(12) + bytes(16) + b'\xde\xad\xbe\xef' * 32
    return head + gap + marker + tail


CF1_SAMPLE = _cf(0x40, b'1')
CF2_SAMPLE = _cf(0x41, b'2')

CLF1_TEXT = (
    b'[HEADER]\nManufacturer: Acme\n[MEASUREMENT]\n'
    b'[FREQUENCY]\n100 90.0 0.0\n200 91.0 0.5\n'
    b'[POLAR]\n0 0\n[ROTATION]\nR(0)\n'
)


class TestFamilyDetection:
    def test_cf1_signature(self) -> None:
        assert detect_clf_family(CF1_SAMPLE) == 'binary_cf1'

    def test_cf2_signature(self) -> None:
        assert detect_clf_family(CF2_SAMPLE) == 'binary_cf2'

    def test_authoring_text(self) -> None:
        assert detect_clf_family(CLF1_TEXT) == 'authoring_text'

    def test_unknown_binary(self) -> None:
        assert detect_clf_family(os.urandom(1024)) == 'unknown'

    def test_truncated_signature_fails_closed(self) -> None:
        # Lead bytes present but the v{x}.0 marker missing → unknown,
        # never a binary claim on an unproven signature.
        truncated = CF2_SAMPLE[:19]
        assert detect_clf_family(truncated) == 'unknown'

    def test_wrong_version_marker_fails_closed(self) -> None:
        bad = _cf(0x41, b'9')
        assert detect_clf_family(bad) == 'unknown'


class TestBinaryVerdicts:
    def test_cf1_unsupported_not_not_clf(self) -> None:
        q = qualify_clf(CF1_SAMPLE)
        assert q.verdict == 'unsupported'
        assert q.family == 'binary_cf1'
        assert q.source_sha256
        assert 'CF1' in q.detail

    def test_cf2_unsupported_not_not_clf(self) -> None:
        q = qualify_clf(CF2_SAMPLE)
        assert q.verdict == 'unsupported'
        assert q.family == 'binary_cf2'
        assert 'CF2' in q.detail

    def test_binary_never_qualified(self) -> None:
        # Even a binary whose tail happens to contain section-like text
        # stays unsupported — we never parse the secured payload.
        sneak = CF2_SAMPLE + b'\n[HEADER]\n[FREQUENCY]\n100 90.0 0.0\n'
        q = qualify_clf(sneak)
        assert q.verdict == 'unsupported'
        assert q.frequency_rows == 0


class TestTextAndUnknownPaths:
    def test_authoring_text_still_qualifies(self) -> None:
        q = qualify_clf(CLF1_TEXT)
        assert q.verdict == 'qualified'
        assert q.family == 'authoring_text'

    def test_text_without_sections_is_not_clf(self) -> None:
        q = qualify_clf(b'hello world, no sections here')
        assert q.verdict == 'not_clf'
        assert q.family == 'authoring_text'

    def test_random_bytes_not_clf(self) -> None:
        q = qualify_clf(os.urandom(1024))
        assert q.verdict == 'not_clf'
        assert q.family == 'unknown'

    def test_empty_not_clf(self) -> None:
        q = qualify_clf(b'')
        assert q.verdict == 'not_clf'

    def test_utf16_not_clf(self) -> None:
        assert qualify_clf(''.encode('utf-16')).verdict == 'not_clf'


class TestInterchangeMatrix:
    def test_binary_families_are_import_candidates_not_admitted(self) -> None:
        states = {i.format_name: i.state for i in INTERCHANGE_MATRIX}
        # Distribution binaries are user_import_candidate — only
        # legitimately obtained tabular derivatives may enter, never
        # the binary payload itself.
        assert states['binary_cf1'] == 'user_import_candidate'
        assert states['binary_cf2'] == 'user_import_candidate'
        assert states['clf1'] == 'ready_for_admission_review'
        assert states['clf2'] == 'ready_for_admission_review'

    def test_qualification_model_records_family(self) -> None:
        q = ClfQualification(verdict='unsupported', family='binary_cf2')
        assert q.family == 'binary_cf2'
