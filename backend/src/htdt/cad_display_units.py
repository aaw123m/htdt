"""Display/input unit presentation authority (#496).

Canonical geometry stays SI metres inside every domain model; only the UI
boundary presents or accepts other units. This module is the *one* shared
conversion policy — Room inspector fields, geometry panels, the ruler,
constraint/projector/treatment surfaces and human-readable report values all
run through the same functions instead of each panel inventing a conversion.

Contract properties:

- no SceneRevision / SearchSpec / Prediction identity may depend on the
  selected presentation unit — the functions here are pure and never touch
  domain models;
- ``display_to_si`` is exact for metric units: ``120 mm`` input produces the
  identical canonical value as ``0.12 m`` (conversion divides by the exact
  integer scale, so the correctly-rounded result equals the SI literal);
- rounding is an explicit display concern — parsing never rounds, only
  :func:`format_length_m` / :func:`display_round` apply the declared decimal
  policy;
- imperial presentation is limited to unambiguous forms: ``in``, and the
  explicit compound ``ft + in`` (``5 ft 10 in`` / ``5'10"``); a bare ``'``
  reads as feet and a bare ``"`` as inches — never silently as one or the
  other;
- the user's selection lives in ``application_preferences`` as
  ``display_input.length_unit`` / ``display_input.numeric_precision`` —
  user-local presentation state, never project evidence.
"""

from __future__ import annotations

from math import isfinite
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


#: Presentation units selectable in the application preference. The values
#: must stay in sync with ``display_input.length_unit`` in
#: ``application_preferences.py`` — the preference is the storage, this type
#: is the semantics.
DisplayLengthUnit = Literal['m', 'cm', 'mm', 'inch']

DISPLAY_LENGTH_UNITS: tuple[str, ...] = ('m', 'cm', 'mm', 'inch')

#: Exact scale from SI metres to the display unit (display = si * scale).
#: Metric scales are exact powers of ten; inch is fixed at 0.0254 m.
_DISPLAY_SCALE: dict[str, float] = {
    'm': 1.0,
    'cm': 100.0,
    'mm': 1000.0,
    'inch': 1.0 / 0.0254,
}

#: Exact integer denominators for display → SI conversion. Dividing by these
#: is correctly-rounded, so ``120 mm`` and ``0.12 m`` map to the identical
#: canonical float.
_DISPLAY_DENOMINATOR: dict[str, float] = {
    'm': 1.0,
    'cm': 100.0,
    'mm': 1000.0,
}

#: Default digits after the decimal point per unit. The user-visible
#: ``display_input.numeric_precision`` preference may override this, but the
#: unit-specific default keeps 25 mm from rendering as "25.000 mm".
DEFAULT_DISPLAY_DECIMALS: dict[str, int] = {
    'm': 3,
    'cm': 2,
    'mm': 1,
    'inch': 2,
}

#: Unit suffix labels used in formatted output (and accepted on parse).
_UNIT_SUFFIX: dict[str, str] = {
    'm': 'm',
    'cm': 'cm',
    'mm': 'mm',
    'inch': 'in',
}

#: Inch → metre is exact by international definition.
_INCH_TO_M = 0.0254

#: Maximum magnitude accepted by parsers/formatters — guards against absurd
#: input producing non-finite display values.
_MAX_ABS_M = 1.0e9


class DisplayUnitError(ValueError):
    """A display-unit value could not be parsed or was out of bounds."""


class LengthDisplayPolicy(BaseModel):
    """The explicit presentation policy applied at the UI boundary.

    ``unit`` is the selected presentation unit; ``decimals`` is the explicit
    display-rounding precision. Neither is ever stored on a domain model.
    """

    model_config = ConfigDict(frozen=True)

    unit: DisplayLengthUnit
    decimals: int = Field(ge=0, le=12)


def display_length_policy(
    unit: str,
    *,
    decimals: int | None = None,
) -> LengthDisplayPolicy:
    """Build a policy; ``decimals`` defaults to the per-unit policy."""

    if unit not in _DISPLAY_SCALE:
        raise DisplayUnitError(f'unsupported display length unit: {unit!r}')
    if decimals is None:
        decimals = DEFAULT_DISPLAY_DECIMALS[unit]
    return LengthDisplayPolicy(unit=unit, decimals=decimals)


def length_display_policy_from_preferences(preferences) -> LengthDisplayPolicy:
    """Build the policy from an :class:`ApplicationPreferenceStore`.

    Reads ``display_input.length_unit`` and ``display_input.numeric_precision``
    — the only persisted presentation state; nothing here writes to the store.
    """

    unit = str(preferences.get('display_input.length_unit'))
    decimals = preferences.get('display_input.numeric_precision')
    if unit not in _DISPLAY_SCALE:
        unit = 'm'
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        decimals = None
    return display_length_policy(unit, decimals=decimals)


def si_to_display(value_m: float, unit: str) -> float:
    """Convert a canonical SI-metre value into the display unit."""

    value = _check_finite_m(value_m)
    if unit not in _DISPLAY_SCALE:
        raise DisplayUnitError(f'unsupported display length unit: {unit!r}')
    return value * _DISPLAY_SCALE[unit]


def display_to_si(value: float, unit: str) -> float:
    """Convert a display-unit input back to canonical SI metres.

    Metric units divide by their exact integer scale so the canonical value
    is identical to having typed the SI literal (``120 mm`` == ``0.12 m``).
    Inches convert through the exact 0.0254 m definition. The result is never
    rounded — display rounding is a separate explicit step.
    """

    value = float(value)
    if not isfinite(value) or abs(value) > _MAX_ABS_M:
        raise DisplayUnitError(f'length value must be finite: {value!r}')
    if unit in _DISPLAY_DENOMINATOR:
        return value / _DISPLAY_DENOMINATOR[unit]
    if unit == 'inch':
        return value * _INCH_TO_M
    raise DisplayUnitError(f'unsupported display length unit: {unit!r}')


def display_round(value: float, policy: LengthDisplayPolicy) -> float:
    """Round a *display-unit* value per the explicit decimals policy."""

    number = float(value)
    if not isfinite(number):
        raise DisplayUnitError('display value must be finite')
    return round(number, policy.decimals)


def format_length_m(value_m: float, policy: LengthDisplayPolicy) -> str:
    """Render a canonical metre value in the policy unit, e.g. ``25.0 mm``."""

    value = si_to_display(value_m, policy.unit)
    return f'{value:.{policy.decimals}f} {_UNIT_SUFFIX[policy.unit]}'


def format_length_m_dual(value_m: float, policy: LengthDisplayPolicy) -> str:
    """Render ``value`` in the policy unit plus the canonical SI fallback.

    When the policy is already metres this renders the plain SI form;
    otherwise e.g. ``25.0 mm (0.025 m)`` so the SI authority stays visible in
    human-readable output.
    """

    if policy.unit == 'm':
        return format_length_m(value_m, policy)
    return (
        f'{format_length_m(value_m, policy)} '
        f'({si_to_display(_check_finite_m(value_m), "m"):.3f} m)'
    )


def format_length_ftin(value_m: float, *, decimals: int = 2) -> str:
    """Render a canonical metre value as compound ``ft + in`` text."""

    value = _check_finite_m(value_m)
    sign = '-' if value < 0 else ''
    total_inches = abs(value) / _INCH_TO_M
    feet = int(total_inches // 12)
    inches = round(total_inches - feet * 12, decimals)
    if inches >= 12.0:  # rounding carried into a foot
        feet += 1
        inches = 0.0
    return f'{sign}{feet} ft {inches:.{decimals}f} in'


_NUMBER_UNIT_RE = re.compile(
    r"""^\s*
    (?P<sign>[+-]?)\s*
    (?:(?P<ft>(?:\d+(?:\.\d*)?|\.\d+))\s*(?:ft|'))?\s*
    (?:(?P<in>(?:\d+(?:\.\d*)?|\.\d+))\s*(?:in|"))?\s*
    (?:(?P<plain>(?:\d+(?:\.\d*)?|\.\d+))\s*(?P<unit>m|cm|mm))?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)


def parse_length_input(
    text: str,
    *,
    default_unit: DisplayLengthUnit = 'm',
) -> float:
    """Parse one length input string into canonical SI metres.

    Accepted forms: a bare number in ``default_unit``; a number with an
    explicit ``m``/``cm``/``mm``/``in``/``ft`` suffix; ``in`` also via ``"``;
    compound ``ft + in`` via ``5 ft 10 in`` or ``5'10"``. A bare ``'`` reads
    as feet and a bare ``"`` as inches — a lone apostrophe is never silently
    treated as inches and vice versa.

    The result is never display-rounded: the domain receives the exact
    canonical value, and any rounding visible in the field is applied only by
    :func:`format_length_m` / :func:`display_round`.
    """

    if not isinstance(text, str) or not text.strip():
        raise DisplayUnitError('length input must be non-empty text')
    stripped = text.strip()

    # Single number with explicit single-letter/word suffix or none.
    single = re.fullmatch(
        r'([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(m|cm|mm|in|ft)?',
        stripped,
        re.IGNORECASE,
    )
    if single is not None and single.group(1):
        value = float(single.group(1))
        suffix = (single.group(2) or '').lower()
        if not suffix:
            return display_to_si(value, default_unit)
        if suffix == 'ft':
            return value * 12.0 * _INCH_TO_M
        return display_to_si(value, 'inch' if suffix == 'in' else suffix)

    # Compound and symbol forms: 5'10", 5 ft 10 in, 5', 10".
    match = _NUMBER_UNIT_RE.fullmatch(stripped)
    if match is None or not any(match.group(g) for g in ('ft', 'in')):
        raise DisplayUnitError(f'cannot parse length input: {text!r}')
    sign = -1.0 if match.group('sign') == '-' else 1.0
    total = 0.0
    if match.group('ft'):
        total += float(match.group('ft')) * 12.0 * _INCH_TO_M
    if match.group('in'):
        total += float(match.group('in')) * _INCH_TO_M
    result = sign * total
    if not isfinite(result):
        raise DisplayUnitError('length input must be finite')
    return result


def _check_finite_m(value_m: float) -> float:
    value = float(value_m)
    if not isfinite(value) or abs(value) > _MAX_ABS_M:
        raise DisplayUnitError(f'length value must be finite: {value_m!r}')
    return value


__all__ = [
    'DEFAULT_DISPLAY_DECIMALS',
    'DISPLAY_LENGTH_UNITS',
    'DisplayLengthUnit',
    'DisplayUnitError',
    'LengthDisplayPolicy',
    'display_length_policy',
    'display_round',
    'display_to_si',
    'format_length_ftin',
    'format_length_m',
    'format_length_m_dual',
    'length_display_policy_from_preferences',
    'parse_length_input',
    'si_to_display',
]
