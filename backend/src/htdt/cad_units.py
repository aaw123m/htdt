"""Canonical quantity/unit semantics for authoritative numeric comparisons.

Numeric observations, tolerances and reconciliation bounds carry an explicit
unit — never a bare float whose unit is inferred from a label or key. One
canonical conversion policy lives here so every authority compares values in
the same quantity family; incompatible quantities fail closed instead of
being silently numerically compared.
"""

from __future__ import annotations

import math
from typing import Literal


#: Units participating in canonical conversion. ``dimensionless`` covers
#: quantities deliberately measured without a unit (ratios, counts); it
#: never converts into a physical unit.
UnitKind = Literal[
    'm',
    'cm',
    'mm',
    'deg',
    'rad',
    'db',
    'hz',
    's',
    'dimensionless',
]


#: The quantity family each unit measures. Two units convert only inside a
#: shared family — dB never becomes seconds, degrees never become metres.
_QUANTITY_FAMILY: dict[str, str] = {
    'm': 'length',
    'cm': 'length',
    'mm': 'length',
    'deg': 'angle',
    'rad': 'angle',
    'db': 'level',
    'hz': 'frequency',
    's': 'time',
    'dimensionless': 'dimensionless',
}

#: Multiplicative factor to the family's base unit (m, rad, db, hz, s, 1).
_TO_BASE: dict[str, float] = {
    'm': 1.0,
    'cm': 0.01,
    'mm': 0.001,
    'deg': math.pi / 180.0,
    'rad': 1.0,
    'db': 1.0,
    'hz': 1.0,
    's': 1.0,
    'dimensionless': 1.0,
}


def quantity_family(unit: str) -> str:
    """The quantity family a unit measures; unknown units raise."""

    try:
        return _QUANTITY_FAMILY[unit]
    except KeyError:
        raise ValueError(f'unknown unit: {unit!r}') from None


def units_convertible(from_unit: str, to_unit: str) -> bool:
    """True when both units measure the same quantity family."""

    try:
        return quantity_family(from_unit) == quantity_family(to_unit)
    except ValueError:
        return False


def convert_unit(value: float, from_unit: str, to_unit: str) -> float:
    """Convert ``value`` between units of one quantity family.

    Conversion runs through the family's base unit, so an m↔mm or deg↔rad
    observation compares on equal footing. Units of different families —
    or a family paired with ``dimensionless`` — raise ``ValueError``.
    """

    from_family = quantity_family(from_unit)
    to_family = quantity_family(to_unit)
    if from_family != to_family:
        raise ValueError(
            f'cannot convert {from_unit!r} ({from_family}) to '
            f'{to_unit!r} ({to_family})'
        )
    return value * _TO_BASE[from_unit] / _TO_BASE[to_unit]


__all__ = [
    'UnitKind',
    'convert_unit',
    'quantity_family',
    'units_convertible',
]
