from pathlib import Path

import pytest

from htdt.rew_parser import RewParseError, parse_rew_frequency_response


FIXTURES = Path(__file__).parent / 'fixtures'


def test_parse_frequency_response_preserves_contract() -> None:
    parsed = parse_rew_frequency_response((FIXTURES / 'basic_rew.txt').read_bytes())
    assert parsed.frequency_hz == (20.0, 40.0, 80.0, 160.0, 320.0)
    assert parsed.level_db[1] == 72.0
    assert parsed.phase_status == 'unknown'
    assert 'phase_all_zero_unverified' in parsed.warnings
    assert parsed.source_sha256


def test_utf8_bom_and_crlf_are_supported() -> None:
    raw = b'\xef\xbb\xbfHeader\r\n20\t70\r\n40\t71\r\n'
    parsed = parse_rew_frequency_response(raw)
    assert parsed.phase_status == 'absent'
    assert parsed.header_lines == ('Header',)


@pytest.mark.parametrize('raw', [b'20 70\n20 71\n', b'40 70\n20 71\n', b'20 NaN\n40 71\n', b'20 70 0\n40 71\n', b'20\n40 71\n'])
def test_invalid_rows_are_rejected(raw: bytes) -> None:
    with pytest.raises(RewParseError):
        parse_rew_frequency_response(raw)
