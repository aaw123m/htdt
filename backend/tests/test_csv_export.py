import csv
import io

import pytest

from htdt.csv_export import csv_safe_cell, csv_safe_row


@pytest.mark.parametrize(
    'value',
    (
        '=HYPERLINK("https://example.invalid","speaker")',
        '=2+1',
        '+cmd|"/c calc"!A0',
        '-2+3+cmd|"/c calc"!A0',
        '@SUM(1,1)*cmd|"/c calc"!A0',
        ' =2+1',
        '\t=2+1',
        '\r=2+1',
        '\n=2+1',
        '\xa0=2+1',
        '\t',
        '\rnormal text',
        '-',
        '=',
    ),
)
def test_formula_prefixed_text_is_neutralized(value: str) -> None:
    safe = csv_safe_cell(value)
    assert safe == "'" + value
    assert safe.lstrip()[0] not in ('=', '+', '-', '@')


@pytest.mark.parametrize(
    'value',
    (
        'speaker-fl',
        'Front Left',
        'scene_entity_origin_z',
        'a=b',
        "'=already literal",
        '',
        '   ',
        '-4',
        '-4.5',
        '+3',
        '1e5',
        ' -4 ',
        '2026-09-19',
    ),
)
def test_ordinary_and_numeric_text_passes_through(value: str) -> None:
    assert csv_safe_cell(value) == value


@pytest.mark.parametrize('value', (0, -1, 4.5, -4.5, None, True))
def test_non_string_cells_pass_through(value) -> None:
    assert csv_safe_cell(value) == value


def test_row_wrapping_is_uniform_and_csv_parseable() -> None:
    row = csv_safe_row(('literal', '=evil', 1.5, '', '-2,3'))
    assert row == ('literal', "'=evil", 1.5, '', "'-2,3")

    stream = io.StringIO(newline='')
    csv.writer(stream, lineterminator='\n').writerow(row)
    parsed = next(csv.reader(io.StringIO(stream.getvalue())))
    assert parsed == ['literal', "'=evil", '1.5', '', "'-2,3"]
