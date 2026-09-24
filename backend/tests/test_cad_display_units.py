from __future__ import annotations

import pytest

from htdt.cad_display_units import (
    DisplayUnitError,
    display_length_policy,
    display_to_si,
    format_length_ftin,
    format_length_m,
    format_length_m_dual,
    parse_length_input,
    si_to_display,
)


def test_metric_input_maps_to_identical_canonical_value() -> None:
    # 120 mm in the field must produce the same canonical float as 0.12 m —
    # no rounding ladder, no drift between display and SI.
    assert display_to_si(120.0, 'mm') == display_to_si(0.12, 'm')
    assert display_to_si(120.0, 'mm') == 0.12
    assert display_to_si(12.0, 'cm') == 0.12
    assert si_to_display(0.12, 'mm') == 120.0
    assert si_to_display(0.12, 'cm') == 12.0


def test_parse_accepts_explicit_suffixes_and_bare_default() -> None:
    assert parse_length_input('120mm') == 0.12
    assert parse_length_input('12 cm') == 0.12
    assert parse_length_input('0.12m') == 0.12
    assert parse_length_input('0.12', default_unit='m') == 0.12
    assert parse_length_input('120', default_unit='mm') == 0.12


def test_imperial_forms_are_unambiguous() -> None:
    assert parse_length_input('10in') == pytest.approx(0.254)
    assert parse_length_input('10"') == pytest.approx(0.254)
    assert parse_length_input('1 ft') == pytest.approx(0.3048)
    assert parse_length_input("1'") == pytest.approx(0.3048)
    assert parse_length_input('5 ft 10 in') == pytest.approx(1.778)
    assert parse_length_input("5'10\"") == pytest.approx(1.778)
    with pytest.raises(DisplayUnitError):
        parse_length_input("'")
    with pytest.raises(DisplayUnitError):
        parse_length_input('not a length')


def test_display_rounding_is_explicit_and_never_hidden_in_parse() -> None:
    policy = display_length_policy('mm', decimals=1)
    # Parse keeps full precision; only formatting applies the declared policy.
    assert parse_length_input('123.456', default_unit='mm') == 0.123456
    assert format_length_m(0.123456, policy) == '123.5 mm'
    assert format_length_m(1.2345, display_length_policy('m')) == '1.234 m'


def test_ftin_formatting_is_compound() -> None:
    assert format_length_ftin(1.778, decimals=0) == '5 ft 10 in'
    assert format_length_ftin(0.3048, decimals=1) == '1 ft 0.0 in'


def test_dual_format_keeps_si_visible() -> None:
    assert format_length_m_dual(0.025, display_length_policy('mm')) == (
        '25.0 mm (0.025 m)'
    )
    assert format_length_m_dual(0.025, display_length_policy('m')) == '0.025 m'


def test_invalid_units_and_values_fail() -> None:
    with pytest.raises(DisplayUnitError):
        display_to_si(1.0, 'furlong')
    with pytest.raises(DisplayUnitError):
        si_to_display(float('nan'), 'mm')
    with pytest.raises(DisplayUnitError):
        display_length_policy('yard')
