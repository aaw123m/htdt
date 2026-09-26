"""#1064: EDID/DisplayID conformance corpus harness."""

from __future__ import annotations

import pytest

from htdt.edid_conformance import (
    EDID_HEADER,
    build_corpus_entry,
    parse_edid,
    run_conformance,
)


def _fix_checksum(block: bytes) -> bytes:
    return block[:-1] + bytes([(256 - sum(block[:-1])) % 256])


def _base_block(*, extension_count: int = 0, fix: bool = True) -> bytes:
    base = bytearray(EDID_HEADER)
    base += b'SNY'[0:0]  # placeholder replaced below
    packed = (
        ((ord('S') - ord('A') + 1) << 10)
        | ((ord('N') - ord('A') + 1) << 5)
        | (ord('Y') - ord('A') + 1)
    )
    base += packed.to_bytes(2, 'big')
    base += (0x0101).to_bytes(2, 'little')  # product code
    base += (42).to_bytes(4, 'little')  # serial
    base += bytes([30, 36])  # week 30, 2026
    base += bytes([1, 4])  # EDID 1.4
    base += bytes([0xA5])  # digital input
    base += bytes([52, 29])  # 52cm x 29cm
    base += bytes([0x78, 0x02])  # gamma/display features
    base += bytes(10)  # chromaticity coordinates
    base += bytes([0x01, 0x00, 0x01])  # established timings
    base += bytes(16)  # standard timings (8 x 2 bytes)
    base += bytes(18 * 4)  # descriptors
    base += bytes([extension_count, 0])
    block = bytes(base)
    return _fix_checksum(block) if fix else block


def _extension(tag: int, *, fix: bool = True) -> bytes:
    block = bytearray([tag] + [0] * 127)
    out = bytes(block)
    return _fix_checksum(out) if fix else out


LINUXHW = 'github.com/linuxhw/EDID'


def _entry(raw: bytes, corpus_class: str, status: str, **kw):
    kwargs = {
        'corpus_class': corpus_class,
        'source_repository': LINUXHW,
        'source_path': 'tests/fixtures/edid-corpus',
        'license_name': 'CC BY 4.0',
        'acquisition_revision': 'fixture-v1',
        'acquisition_date': '2026-09-25',
        'expected_status': status,
        'parser_version': 'htdt-edid-parser-1',
    }
    kwargs.update(kw)
    return build_corpus_entry(raw=raw, **kwargs)


def test_valid_base_block_parses_clean():
    raw = _base_block()
    result = parse_edid(raw)
    assert result.status == 'parse_ok'
    assert result.manufacturer_id == 'SNY'
    assert result.edid_version == (1, 4)
    assert result.display_width_cm == 52
    assert result.declared_extension_count == 0


def test_bad_checksum_and_header_fail_closed():
    bad_checksum = _base_block(fix=False)
    assert parse_edid(bad_checksum).status == 'parse_error'
    assert 'bad_base_checksum' in parse_edid(bad_checksum).errors
    bad_header = b'\x01' + _base_block()[1:]
    assert 'bad_base_header' in parse_edid(bad_header).errors


def test_truncated_and_mismatched_extensions():
    assert parse_edid(_base_block()[:64]).status == 'parse_error'
    missing_ext = _base_block(extension_count=1)
    result = parse_edid(missing_ext)
    assert result.status == 'parse_warning'
    assert any('extension_count_mismatch' in w for w in result.warnings)


def test_cta_and_displayid_and_unknown_extensions_listed_not_guessed():
    raw = (
        _base_block(extension_count=3)
        + _extension(0x02)
        + _extension(0x70)
        + _extension(0x99)
    )
    result = parse_edid(raw)
    assert result.status == 'parse_warning'
    kinds = [e.kind for e in result.extensions]
    assert kinds == ['cta_861', 'displayid', 'unknown']
    assert any('unknown_extension_tag:153' == w for w in result.warnings)
    # Unknown block stays raw — no capability invented from tag 0x99.
    assert result.extensions[2].kind == 'unknown'


def test_corpus_entry_binds_content_and_expectations():
    raw = _base_block()
    entry = _entry(raw, 'valid_reference', 'parse_ok')
    outcome = run_conformance(entry, raw)
    assert outcome.outcome == 'match'

    bad = _base_block(fix=False)
    bad_entry = _entry(
        bad,
        'malformed',
        'parse_error',
        expected_warnings=('bad_base_checksum',),
    )
    outcome = run_conformance(bad_entry, bad)
    assert outcome.outcome == 'match'
    assert outcome.actual_status == 'parse_error'

    # Wrong bytes for a pinned entry -> rejected, never silently re-bound.
    with pytest.raises(ValueError, match='pinned sha256'):
        run_conformance(bad_entry, raw)


def test_oracle_disagreement_is_ambiguous_never_preferred():
    raw = _base_block(fix=False)
    entry = _entry(
        raw,
        'malformed',
        'parse_error',
        expected_warnings=('bad_base_checksum',),
    )
    # Oracle claims the same bytes are clean — disagreement is recorded.
    outcome = run_conformance(entry, raw, oracle=lambda _: 'parse_ok')
    assert outcome.outcome == 'ambiguous'
    assert outcome.oracle_disagreement is not None
    # An unavailable oracle does not mask a real mismatch.
    mismatch = _entry(raw, 'valid_reference', 'parse_ok')
    outcome = run_conformance(mismatch, raw, oracle=lambda _: 'parse_ok')
    assert outcome.outcome == 'ambiguous'


def test_fuzz_mutations_never_crash_or_fabricate_clean_parse():
    base = _base_block()
    mutations = [
        base[:n] for n in (0, 1, 63, 127)
    ] + [
        bytes([b]) + base[1:] for b in range(8)
    ] + [
        base + bytes(128),  # undeclared all-zero extension
        base + base,        # duplicated base block as extension
        bytes(len(base) * 2),
    ]
    for mutated in mutations:
        result = parse_edid(mutated)
        # Never crashes; a structurally wrong blob is never parse_ok-clean.
        if len(mutated) < 128 or mutated[:8] != EDID_HEADER:
            assert result.status != 'parse_ok'
    # Checksum-only corruption is always flagged.
    for i in range(0, 128, 17):
        corrupt = bytearray(base)
        corrupt[i] ^= 0xFF
        result = parse_edid(bytes(corrupt))
        if sum(bytes(corrupt)) % 256 != 0:
            assert result.status == 'parse_error'
