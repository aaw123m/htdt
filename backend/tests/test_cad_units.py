"""Unit-conversion contract: every authoritative numeric comparison funnels
through ``cad_units`` so families can never be silently cross-compared."""

from __future__ import annotations

import math

import pytest

from htdt.cad_units import (
    convert_unit,
    quantity_family,
    units_convertible,
)


def test_length_conversions() -> None:
    assert convert_unit(1.0, 'm', 'mm') == pytest.approx(1000.0)
    assert convert_unit(250.0, 'cm', 'm') == pytest.approx(2.5)
    assert convert_unit(12.0, 'mm', 'm') == pytest.approx(0.012)


def test_angle_conversions() -> None:
    assert convert_unit(180.0, 'deg', 'rad') == pytest.approx(math.pi)
    assert convert_unit(math.pi / 2, 'rad', 'deg') == pytest.approx(90.0)


def test_time_and_identity_conversions() -> None:
    assert convert_unit(1.5, 's', 'ms') == pytest.approx(1500.0)
    assert convert_unit(500.0, 'ms', 's') == pytest.approx(0.5)
    assert convert_unit(3.0, 'db', 'db') == pytest.approx(3.0)
    assert convert_unit(2.0, 'dimensionless', 'dimensionless') == pytest.approx(2.0)


def test_cross_family_conversion_fails_closed() -> None:
    with pytest.raises(ValueError, match='cannot convert'):
        convert_unit(1.0, 'm', 'db')
    with pytest.raises(ValueError, match='cannot convert'):
        convert_unit(1.0, 'deg', 's')
    with pytest.raises(ValueError, match='cannot convert'):
        convert_unit(1.0, 'dimensionless', 'hz')


def test_unknown_unit_fails_closed() -> None:
    with pytest.raises(ValueError, match='unknown unit'):
        quantity_family('furlong')
    with pytest.raises(ValueError, match='unknown unit'):
        convert_unit(1.0, 'm', 'furlong')
    # Convertibility is a predicate, not a raiser.
    assert units_convertible('m', 'furlong') is False


def test_units_convertible_matrix() -> None:
    assert units_convertible('m', 'mm') is True
    assert units_convertible('deg', 'rad') is True
    assert units_convertible('hz', 's') is False
    assert units_convertible('db', 'dimensionless') is False
    assert units_convertible('dimensionless', 'dimensionless') is True
