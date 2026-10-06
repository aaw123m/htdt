"""Typed physical-quantity / unit authority (#728, REV59-UNITS).

CAD geometry, acoustics, HVAC, projection and device APIs exchange
dimensional data. A bare ``float`` plus a UI label cannot prevent
mm/m, ms/s, deg/rad, Pa/kPa, °C/K, L/s↔m³/s or percent/fraction
mistakes — the number stays numerically plausible while the physics
drifts by factors of 10–1000. This module makes *what the number is*
first-class authority data so invalid arithmetic fails closed instead
of producing a confident wrong answer.

- :class:`CadTypedQuantity` — the sealed quantity-identity record:
  semantic quantity kind (not merely shared SI base dimensions), value
  kind (absolute / delta / gauge / normalized / count), the canonical
  stored value, and the preserved source observation (source token,
  unit, resolution, significant digits) so ``96 in`` never silently
  becomes a survey-grade ``2.438400000 m``.
- :data:`UNIT_DEFS` — the one versioned unit registry. Every unit maps
  to a *dimension family* and an exact scale/offset to the family's
  canonical SI unit; affine conversions (°C → K is ``v + 273.15``) are
  marked affine so absolute-temperature arithmetic is governed
  differently from plain scaling.
- :func:`evaluate_quantity_operation` + :class:`CadQuantityOperation` —
  the fail-closed arithmetic gate. ``add``/``subtract``/``compare``
  require identical quantity kind with legal absolute/delta algebra;
  ``convert`` stays inside one quantity kind; frequency↔angular,
  samples→duration and airflow→ACH run only through the named,
  pinned operations; pressure→dB SPL always answers
  ``requires_logarithmic_authority`` because that crossing belongs to
  the #691 typed-dB authority with its reference/weighting identity.

Composition (bindings, never merges):

- #691 ``cad_logarithmic_quantity`` owns logarithmic/reference
  quantities (dB SPL/dBFS/gain). Linear→log crossings route there and
  are never computed here.
- #496 ``cad_display_units`` owns UI presentation/parsing; this layer
  is the canonical identity beneath it — display rounding never
  mutates a sealed quantity.
- #572/#604 uncertainty — quantities carry ``uncertainty_ref`` pins;
  propagation semantics stay with those authorities.
- #609/#582 timebase — count→duration conversion requires the exact
  rate identity; a plausible-looking rate is never assumed.

Literature / standards basis
----------------------------
- BIPM SI Brochure, 9th edition v4.01 (June 2026) — coherent SI base /
  derived units and the exact definitions of the non-SI units used
  here (inch = 0.0254 m exactly, foot = 0.3048 m exactly).
- ISO 80000-1:2022 — quantity kind is distinct from unit and from
  numerical value; dimension compatibility is necessary but not
  sufficient for valid arithmetic.
- Affine-quantity algebra (temperature): an absolute Celsius
  temperature and a temperature difference are different quantities;
  ``20 °C + Δ10 K = 30 °C`` while ``20 °C + 10 °C`` is undefined.
- Angular vs cyclic frequency: ω = 2πf is a declared named
  conversion, not a unit-scale relation (#728 §10).
- HVAC airflow: CFM→m³/s is an exact unit conversion inside one
  volume-flow quantity; ACH is a *derived* rate requiring the room
  volume identity (#728 §14).
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite, pi
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


TYPED_QUANTITY_SCHEMA_VERSION = 'typed-quantity-1'
TYPED_QUANTITY_EVALUATION_VERSION = 'tq-eval-1'
#: Version pin of the unit registry — a quantity record names the
#: conversion policy it was sealed under so a registry revision never
#: silently reinterprets history (#728 §19).
UNIT_REGISTRY_VERSION = 'rev59-unitdefs-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


# ---------------------------------------------------------------------------
# Taxonomies (#728)
# ---------------------------------------------------------------------------

DimensionFamily = Literal[
    'length',
    'area',
    'volume',
    'time',
    'frequency',
    'angle',
    'velocity',
    'acceleration',
    'pressure',
    'temperature',
    'mass',
    'force',
    'power',
    'energy',
    'voltage',
    'current',
    'resistance',
    'volume_flow',
    'mass_flow',
    'rate_per_time',
    'luminance',
    'illuminance',
    'luminous_flux',
    'ratio',
    'count',
    'dimensionless',
]
"""The physical dimension a unit measures. Same-family units convert;
different families never do. Distinct quantity *kinds* may share one
family (W measures electrical, acoustic, radiometric and thermal power)
without ever sharing arithmetic — kind identity governs operations,
family governs unit scaling only."""

QuantityKind = Literal[
    'length',
    'area',
    'volume',
    'time_duration',
    'timestamp_instant',
    'frequency',
    'angular_frequency',
    'plane_angle',
    'velocity',
    'acceleration',
    'pressure_absolute',
    'pressure_gauge',
    'pressure_differential',
    'acoustic_pressure_rms',
    'acoustic_pressure_peak',
    'temperature_absolute',
    'temperature_difference',
    'mass',
    'force',
    'power_electrical',
    'power_acoustic',
    'power_radiometric',
    'power_thermal',
    'power_other_declared',
    'energy',
    'voltage_rms',
    'voltage_peak',
    'voltage_peak_to_peak',
    'current_rms',
    'current_peak',
    'resistance_impedance',
    'airflow_volume_rate',
    'mass_flow_rate',
    'air_changes_per_hour',
    'luminance',
    'illuminance',
    'luminous_flux',
    'ratio',
    'sample_count',
    'frame_count',
    'generic_count',
    'dimensionless',
    'other_declared',
]
"""Semantic quantity identity (#728 §1). Values sharing SI dimensions but
naming different physics — gauge vs absolute pressure, electrical vs
acoustic power, frequency vs angular frequency — are different kinds and
never silently interoperate."""

#: The dimension family each quantity kind measures in.
_KIND_FAMILY: dict[str, DimensionFamily] = {
    'length': 'length',
    'area': 'area',
    'volume': 'volume',
    'time_duration': 'time',
    'timestamp_instant': 'time',
    'frequency': 'frequency',
    'angular_frequency': 'frequency',
    'plane_angle': 'angle',
    'velocity': 'velocity',
    'acceleration': 'acceleration',
    'pressure_absolute': 'pressure',
    'pressure_gauge': 'pressure',
    'pressure_differential': 'pressure',
    'acoustic_pressure_rms': 'pressure',
    'acoustic_pressure_peak': 'pressure',
    'temperature_absolute': 'temperature',
    'temperature_difference': 'temperature',
    'mass': 'mass',
    'force': 'force',
    'power_electrical': 'power',
    'power_acoustic': 'power',
    'power_radiometric': 'power',
    'power_thermal': 'power',
    'power_other_declared': 'power',
    'energy': 'energy',
    'voltage_rms': 'voltage',
    'voltage_peak': 'voltage',
    'voltage_peak_to_peak': 'voltage',
    'current_rms': 'current',
    'current_peak': 'current',
    'resistance_impedance': 'resistance',
    'airflow_volume_rate': 'volume_flow',
    'mass_flow_rate': 'mass_flow',
    'air_changes_per_hour': 'rate_per_time',
    'luminance': 'luminance',
    'illuminance': 'illuminance',
    'luminous_flux': 'luminous_flux',
    'ratio': 'ratio',
    'sample_count': 'count',
    'frame_count': 'count',
    'generic_count': 'count',
    'dimensionless': 'dimensionless',
    'other_declared': 'dimensionless',
}

ValueKind = Literal[
    'absolute',
    'difference_delta',
    'gauge_relative',
    'normalized',
    'count',
    'declared_other',
]
"""Whether the number is an absolute point on a scale, a difference
between two points, a reading relative to a declared reference, a
normalized 0..1/percent value, or a pure count. Addition legality
depends on it (#728 §6/§8)."""

RatioSemantics = Literal[
    'plain_fraction',
    'percent_of_full',
    'efficiency',
    'probability_weight',
    'normalized_coordinate',
    'duty_cycle',
    'other_declared',
]
"""What a ``ratio`` quantity means (#728 §8). A duty cycle, an efficiency
and a probability are numerically alike but semantically distinct — the
semantics are part of quantity identity, so ``0.5`` the efficiency never
silently compares to ``50 %`` the duty cycle."""

AngleSemantics = Literal[
    'absolute_heading',
    'relative_rotation',
    'undeclared',
]
"""Absolute heading vs relative rotation (#728 §5). Kept as identity so a
heading never silently flows into a 'rotate by' API."""


class CadUnitDef(BaseModel):
    """One registered unit: family membership plus the exact affine map
    to the family canonical unit (``canonical = value * scale + offset``)."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    family: DimensionFamily
    scale: float
    offset: float = 0.0
    #: Human label (JA) for display surfaces (#728 — quantities render
    #: with their unit label, never a bare number).
    label_ja: str = Field(min_length=1)
    exact_definition: str = ''

    @property
    def affine(self) -> bool:
        return self.offset != 0.0

    @model_validator(mode='after')
    def valid_unit(self) -> 'CadUnitDef':
        _require_finite(self.scale, f'unit {self.symbol} scale')
        _require_finite(self.offset, f'unit {self.symbol} offset')
        if self.scale == 0.0:
            raise ValueError(f'unit {self.symbol} scale must be non-zero')
        return self


def _u(symbol: str, family: DimensionFamily, scale: float,
       label_ja: str, offset: float = 0.0, exact: str = '') -> CadUnitDef:
    return CadUnitDef(
        symbol=symbol, family=family, scale=scale, offset=offset,
        label_ja=label_ja, exact_definition=exact,
    )


#: The canonical unit registry (UNIT_REGISTRY_VERSION). Scales are exact
#: definitions where one exists (inch, foot, psi, °C offset); empirical
#: approximations are never admitted as 'exact'.
UNIT_DEFS: dict[str, CadUnitDef] = {d.symbol: d for d in (
    # length — canonical metre (SI Brochure)
    _u('m', 'length', 1.0, 'm', exact='SI base unit'),
    _u('cm', 'length', 0.01, 'cm', exact='exact 1/100 m'),
    _u('mm', 'length', 0.001, 'mm', exact='exact 1/1000 m'),
    _u('km', 'length', 1000.0, 'km', exact='exact 1000 m'),
    _u('in', 'length', 0.0254, 'in', exact='exact 0.0254 m'),
    _u('ft', 'length', 0.3048, 'ft', exact='exact 0.3048 m'),
    # area / volume — canonical m² / m³
    _u('m2', 'area', 1.0, 'm²', exact='SI coherent'),
    _u('cm2', 'area', 1e-4, 'cm²'),
    _u('mm2', 'area', 1e-6, 'mm²'),
    _u('m3', 'volume', 1.0, 'm³', exact='SI coherent'),
    _u('l', 'volume', 1e-3, 'L', exact='exact 1e-3 m³ (SI-accepted)'),
    _u('ml', 'volume', 1e-6, 'mL'),
    # time — canonical second
    _u('s', 'time', 1.0, 's', exact='SI base unit'),
    _u('ms', 'time', 1e-3, 'ms'),
    _u('us', 'time', 1e-6, 'µs'),
    _u('min', 'time', 60.0, 'min', exact='exact 60 s'),
    _u('h', 'time', 3600.0, 'h', exact='exact 3600 s'),
    # frequency / angular frequency — distinct families of identity;
    # crossing is a named 2π operation, never a unit conversion.
    _u('hz', 'frequency', 1.0, 'Hz', exact='SI coherent'),
    _u('khz', 'frequency', 1e3, 'kHz'),
    _u('mhz', 'frequency', 1e6, 'MHz'),
    _u('rad_per_s', 'frequency', 1.0, 'rad/s'),
    # angle — canonical radian (SI coherent)
    _u('rad', 'angle', 1.0, 'rad', exact='SI coherent'),
    _u('deg', 'angle', pi / 180.0, '°', exact='exact π/180 rad'),
    # velocity / acceleration
    _u('m_per_s', 'velocity', 1.0, 'm/s'),
    _u('mm_per_s', 'velocity', 1e-3, 'mm/s'),
    _u('ft_per_s', 'velocity', 0.3048, 'ft/s'),
    _u('m_per_s2', 'acceleration', 1.0, 'm/s²'),
    _u('g_unit', 'acceleration', 9.80665, 'g', exact='standard gravity'),
    # pressure — canonical pascal
    _u('pa', 'pressure', 1.0, 'Pa', exact='SI coherent'),
    _u('kpa', 'pressure', 1e3, 'kPa'),
    _u('bar', 'pressure', 1e5, 'bar', exact='exact 1e5 Pa'),
    _u('psi', 'pressure', 6894.757293168, 'psi',
       exact='exact via 0.45359237 kg lbm definition'),
    _u('inwc', 'pressure', 249.08891, 'inH₂O',
       exact='conventional 249.08891 Pa (39.2 °F water)'),
    _u('mmhg', 'pressure', 133.322387415, 'mmHg',
       exact='conventional 133.322387415 Pa'),
    # temperature — canonical kelvin; degc/degf are affine
    _u('k', 'temperature', 1.0, 'K', exact='SI base unit'),
    _u('degc', 'temperature', 1.0, '°C', offset=273.15,
       exact='exact offset +273.15 (affine)'),
    _u('degf', 'temperature', 5.0 / 9.0, '°F', offset=255.37222222222222,
       exact='exact 5/9 scale + offset (affine)'),
    _u('delta_k', 'temperature', 1.0, 'ΔK', exact='temperature interval'),
    _u('delta_degc', 'temperature', 1.0, 'Δ°C',
       exact='1 °C interval = 1 K'),
    _u('delta_degf', 'temperature', 5.0 / 9.0, 'Δ°F'),
    # mass / force
    _u('kg', 'mass', 1.0, 'kg', exact='SI base unit'),
    _u('g', 'mass', 1e-3, 'g'),
    _u('n', 'force', 1.0, 'N', exact='SI coherent'),
    _u('lbf', 'force', 4.4482216152605, 'lbf'),
    # power — canonical watt; quantity kind (electrical/acoustic/...)
    # disambiguates which power it is.
    _u('w', 'power', 1.0, 'W', exact='SI coherent'),
    _u('kw', 'power', 1e3, 'kW'),
    _u('mw', 'power', 1e-3, 'mW'),
    # energy — canonical joule
    _u('j', 'energy', 1.0, 'J', exact='SI coherent'),
    _u('wh', 'energy', 3600.0, 'Wh', exact='exact 3600 J'),
    _u('kwh', 'energy', 3.6e6, 'kWh'),
    # electrical
    _u('v', 'voltage', 1.0, 'V', exact='SI coherent'),
    _u('mv', 'voltage', 1e-3, 'mV'),
    _u('a', 'current', 1.0, 'A', exact='SI base unit'),
    _u('ma', 'current', 1e-3, 'mA'),
    _u('ohm', 'resistance', 1.0, 'Ω', exact='SI coherent'),
    _u('kohm', 'resistance', 1e3, 'kΩ'),
    # HVAC flow — canonical m³/s; CFM/L/s stay the same quantity
    _u('m3_per_s', 'volume_flow', 1.0, 'm³/s'),
    _u('l_per_s', 'volume_flow', 1e-3, 'L/s'),
    _u('cfm', 'volume_flow', 0.00047194745, 'CFM',
       exact='exact via 0.3048³ m³/ft³ ÷ 60 s'),
    _u('kg_per_s', 'mass_flow', 1.0, 'kg/s'),
    _u('ach', 'rate_per_time', 1.0, 'ACH',
       exact='air changes per hour (3600 s base)'),
    _u('per_s', 'rate_per_time', 1.0 / 3600.0, '1/s'),
    # photometric / radiometric — distinct families so lx, cd/m² and lm
    # never silently interoperate (#728 §13)
    _u('cd_per_m2', 'luminance', 1.0, 'cd/m²', exact='SI coherent'),
    _u('nit', 'luminance', 1.0, 'nt', exact='1 nt = 1 cd/m²'),
    _u('lx', 'illuminance', 1.0, 'lx', exact='SI coherent'),
    _u('lm', 'luminous_flux', 1.0, 'lm', exact='SI coherent'),
    # ratio — canonical fraction; percent is a unit of the same quantity
    _u('fraction', 'ratio', 1.0, '比率(0–1)'),
    _u('percent', 'ratio', 0.01, '%', exact='exact 1/100'),
    # counts / dimensionless — never convert to a physical unit
    _u('count', 'count', 1.0, '個'),
    _u('samples', 'count', 1.0, 'サンプル'),
    _u('frames', 'count', 1.0, 'フレーム'),
    _u('one', 'dimensionless', 1.0, '無次元'),
)}


def unit_def(symbol: str) -> CadUnitDef:
    """The registered definition of a unit; unknown units raise."""
    try:
        return UNIT_DEFS[symbol]
    except KeyError:
        raise ValueError(
            f'unregistered unit: {symbol!r} — an unmapped provider unit '
            'stays UNKNOWN rather than being guessed (#728 §18)'
        ) from None


def quantity_family_of(kind: QuantityKind) -> DimensionFamily:
    return _KIND_FAMILY[kind]


def unit_allowed_for(kind: QuantityKind, unit: str) -> bool:
    """Whether ``unit`` measures the family ``kind`` lives in."""
    try:
        return UNIT_DEFS[unit].family == _KIND_FAMILY[kind]
    except KeyError:
        return False


#: Units each quantity kind accepts, plus per-kind unit restrictions.
#: Absolute temperature takes affine units (k/degc/degf); a temperature
#: difference takes interval units (delta_*). Same family, disjoint
#: unit sets — a ``Δ10 K`` record never reads as ``10 °C``.
_KIND_UNITS: dict[str, frozenset[str]] = {
    'length': frozenset({'m', 'cm', 'mm', 'km', 'in', 'ft'}),
    'area': frozenset({'m2', 'cm2', 'mm2'}),
    'volume': frozenset({'m3', 'l', 'ml'}),
    'time_duration': frozenset({'s', 'ms', 'us', 'min', 'h'}),
    'timestamp_instant': frozenset(),
    'frequency': frozenset({'hz', 'khz', 'mhz'}),
    'angular_frequency': frozenset({'rad_per_s'}),
    'plane_angle': frozenset({'rad', 'deg'}),
    'velocity': frozenset({'m_per_s', 'mm_per_s', 'ft_per_s'}),
    'acceleration': frozenset({'m_per_s2', 'g_unit'}),
    'pressure_absolute': frozenset({'pa', 'kpa', 'bar', 'psi', 'inwc', 'mmhg'}),
    'pressure_gauge': frozenset({'pa', 'kpa', 'bar', 'psi', 'inwc', 'mmhg'}),
    'pressure_differential': frozenset({'pa', 'kpa', 'bar', 'psi', 'inwc', 'mmhg'}),
    'acoustic_pressure_rms': frozenset({'pa'}),
    'acoustic_pressure_peak': frozenset({'pa'}),
    'temperature_absolute': frozenset({'k', 'degc', 'degf'}),
    'temperature_difference': frozenset({'delta_k', 'delta_degc', 'delta_degf'}),
    'mass': frozenset({'kg', 'g'}),
    'force': frozenset({'n', 'lbf'}),
    'power_electrical': frozenset({'w', 'kw', 'mw'}),
    'power_acoustic': frozenset({'w', 'kw', 'mw'}),
    'power_radiometric': frozenset({'w', 'kw', 'mw'}),
    'power_thermal': frozenset({'w', 'kw', 'mw'}),
    'power_other_declared': frozenset({'w', 'kw', 'mw'}),
    'energy': frozenset({'j', 'wh', 'kwh'}),
    'voltage_rms': frozenset({'v', 'mv'}),
    'voltage_peak': frozenset({'v', 'mv'}),
    'voltage_peak_to_peak': frozenset({'v', 'mv'}),
    'current_rms': frozenset({'a', 'ma'}),
    'current_peak': frozenset({'a', 'ma'}),
    'resistance_impedance': frozenset({'ohm', 'kohm'}),
    'airflow_volume_rate': frozenset({'m3_per_s', 'l_per_s', 'cfm'}),
    'mass_flow_rate': frozenset({'kg_per_s'}),
    'air_changes_per_hour': frozenset({'ach', 'per_s'}),
    'luminance': frozenset({'cd_per_m2', 'nit'}),
    'illuminance': frozenset({'lx'}),
    'luminous_flux': frozenset({'lm'}),
    'ratio': frozenset({'fraction', 'percent'}),
    'sample_count': frozenset({'samples', 'count'}),
    'frame_count': frozenset({'frames', 'count'}),
    'generic_count': frozenset({'count'}),
    'dimensionless': frozenset({'one'}),
    'other_declared': frozenset(),
}

#: Kinds whose absolute values sit on an affine scale — absolute+absolute
#: addition is undefined for them (#728 §6).
_AFFINE_KINDS: frozenset[str] = frozenset({
    'temperature_absolute', 'timestamp_instant',
})

#: Affine absolute-kind → difference-kind pairing (#728 §6). An
#: absolute temperature and a temperature difference are distinct kinds
#: that nonetheless combine under add/subtract: abs + Δ → abs,
#: abs − Δ → abs, abs − abs → Δ. Compare/ratio_of never cross the pair.
_ABS_DIFF_PAIR: dict[str, str] = {
    'temperature_absolute': 'temperature_difference',
}

#: Declared derived-quantity products for multiply/divide (#728 §4 —
#: dimensional validity is necessary but not sufficient: only named,
#: physically meaningful derivations are permitted).
_PRODUCT_KINDS: dict[tuple[str, str], QuantityKind] = {
    ('length', 'length'): 'area',
    ('area', 'length'): 'volume',
    ('velocity', 'time_duration'): 'length',
    ('airflow_volume_rate', 'time_duration'): 'volume',
    ('power_electrical', 'time_duration'): 'energy',
    ('power_thermal', 'time_duration'): 'energy',
    ('mass_flow_rate', 'time_duration'): 'mass',
}
_QUOTIENT_KINDS: dict[tuple[str, str], QuantityKind] = {
    ('length', 'time_duration'): 'velocity',
    ('velocity', 'time_duration'): 'acceleration',
    ('volume', 'time_duration'): 'airflow_volume_rate',
    ('mass', 'time_duration'): 'mass_flow_rate',
    ('energy', 'time_duration'): 'power_electrical',
    # NB: airflow_volume_rate ÷ volume is intentionally absent — that
    # derivation is per-second while the ACH kind is per-hour; it runs
    # only through the named 'airflow_to_air_changes' operation which
    # applies the exact 3600 s factor (#728 §14).
}


# ---------------------------------------------------------------------------
# Embedded descriptors
# ---------------------------------------------------------------------------


class CadSourceObservation(BaseModel):
    """The exact imported/observed representation (#728 §3/§16).

    Re-export and audit need the source token (``96 in``), the source
    unit, the parsed value and — separately — the source's resolution and
    significant digits. Conversion never manufactures precision: a coarse
    ``96 in`` keeps its declared resolution even though the canonical
    float carries more digits.
    """

    model_config = ConfigDict(frozen=True)

    source_value: float
    source_unit: str = Field(min_length=1)
    source_token: str | None = None
    #: Smallest representable increment of the source (e.g. a drawing
    #: dimensioned to whole millimetres carries ``0.001`` m).
    source_resolution: float | None = None
    #: Declared significant digits of the source observation (1..17).
    significant_digits: int | None = Field(default=None, ge=1, le=17)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadSourceObservation':
        _require_finite(self.source_value, 'source_value')
        if self.source_resolution is not None:
            _require_finite(
                self.source_resolution, 'source_resolution'
            )
            if self.source_resolution <= 0:
                raise ValueError('source_resolution must be positive')
        return self


class CadDerivedQuantity(BaseModel):
    """A computed value descriptor embedded in an operation verdict.

    Carries full quantity identity (kind, value kind, canonical unit);
    its provenance is the operation record — it is not separately
    sealed.
    """

    model_config = ConfigDict(frozen=True)

    value: float
    quantity_kind: QuantityKind
    value_kind: ValueKind
    result_unit: str = Field(min_length=1)
    ratio_semantics: RatioSemantics | None = None
    derivation_label: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_derived(self) -> 'CadDerivedQuantity':
        _require_finite(self.value, 'derived value')
        return self


class CadQuantityRef(BaseModel):
    """A lightweight inline operand for evaluations where the operand is
    not yet a persisted ``CadTypedQuantity`` (e.g. a declared rate or a
    constant). Identity rules still apply — kind/unit are validated
    against the registry."""

    model_config = ConfigDict(frozen=True)

    quantity_kind: QuantityKind
    value_kind: ValueKind
    unit: str = Field(min_length=1)
    value: float
    ratio_semantics: RatioSemantics | None = None

    @model_validator(mode='after')
    def valid_ref(self) -> 'CadQuantityRef':
        _require_finite(self.value, 'operand value')
        allowed = _KIND_UNITS[self.quantity_kind]
        if allowed and self.unit not in allowed:
            raise ValueError(
                f'unit {self.unit!r} is not registered for quantity '
                f'{self.quantity_kind}'
            )
        return self

    def canonical_value(self) -> float:
        return self.value * UNIT_DEFS[self.unit].scale + (
            UNIT_DEFS[self.unit].offset
        )


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class CadTypedQuantity(BaseModel):
    """One sealed typed physical quantity (#728 goal).

    The authority-boundary atom: every persisted dimensional value carries
    its quantity kind, value kind, canonical SI value, the display unit it
    was stated in and the preserved source observation. ``canonical_value``
    is never a bare float — kind and unit registry pin its meaning.
    """

    model_config = ConfigDict(frozen=True)

    quantity_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    quantity_kind: QuantityKind
    value_kind: ValueKind
    canonical_value: float
    canonical_unit: str = Field(min_length=1)
    display_unit: str = Field(min_length=1)
    source: CadSourceObservation | None = None
    ratio_semantics: RatioSemantics | None = None
    angle_semantics: AngleSemantics | None = None
    #: Free-text reference declaration for pressure/electrical kinds
    #: (e.g. gauge vs atmospheric datum, 2.83 V RMS at 1 m) — the exact
    #: reference condition is identity, not formatting (#728 §7/§11).
    reference_condition: str = ''
    declared_quantity_label: str | None = None
    uncertainty_ref: AuthorityRef | None = None
    evidence_ref: AuthorityRef | None = None
    conversion_profile: str = UNIT_REGISTRY_VERSION
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    quantity_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_quantity(self) -> 'CadTypedQuantity':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        _require_finite(self.canonical_value, 'canonical_value')
        allowed = _KIND_UNITS[self.quantity_kind]
        if self.quantity_kind == 'other_declared':
            if not self.declared_quantity_label:
                raise ValueError(
                    'other_declared requires declared_quantity_label — '
                    'an unmapped quantity stays named, never anonymous'
                )
        elif not allowed:
            raise ValueError(
                f'quantity {self.quantity_kind} carries no registered '
                'unit — timestamps and declared-only kinds cannot assert '
                'a canonical numeric value'
            )
        else:
            for unit, label in (
                (self.canonical_unit, 'canonical_unit'),
                (self.display_unit, 'display_unit'),
            ):
                if unit not in allowed:
                    raise ValueError(
                        f'{label} {unit!r} is not a registered unit for '
                        f'quantity {self.quantity_kind}'
                    )
            if UNIT_DEFS[self.canonical_unit].affine:
                raise ValueError(
                    'canonical storage must be the affine-free family '
                    'base unit — store kelvin, never degc (#728 §2)'
                )
        if self.quantity_kind == 'ratio' and self.ratio_semantics is None:
            raise ValueError(
                'a ratio quantity requires ratio_semantics — "0.5" is '
                'not self-describing (#728 §8)'
            )
        if self.quantity_kind != 'ratio' and (
            self.ratio_semantics is not None
        ):
            raise ValueError(
                'ratio_semantics is only valid on ratio quantities'
            )
        if self.quantity_kind == 'plane_angle' and (
            self.angle_semantics in (None, 'undeclared')
        ):
            raise ValueError(
                'an angle requires angle_semantics — heading vs rotation '
                'is identity, not formatting (#728 §5)'
            )
        if self.quantity_kind != 'plane_angle' and (
            self.angle_semantics is not None
        ):
            raise ValueError(
                'angle_semantics is only valid on plane_angle'
            )
        if self.quantity_kind == 'pressure_gauge' and (
            self.value_kind != 'gauge_relative'
        ):
            raise ValueError(
                'a gauge pressure carries value_kind gauge_relative — '
                'relabeling a unit does not make it absolute (#728 §7)'
            )
        if self.quantity_kind == 'pressure_differential' and (
            self.value_kind not in ('difference_delta', 'gauge_relative')
        ):
            raise ValueError(
                'a differential pressure is a relative/delta quantity'
            )
        if self.quantity_kind == 'pressure_absolute' and (
            self.value_kind != 'absolute'
        ):
            raise ValueError(
                'an absolute pressure carries value_kind absolute'
            )
        if self.quantity_kind == 'temperature_absolute' and (
            self.value_kind != 'absolute'
        ):
            raise ValueError(
                'an absolute temperature carries value_kind absolute'
            )
        if self.quantity_kind == 'temperature_difference' and (
            self.value_kind != 'difference_delta'
        ):
            raise ValueError(
                'a temperature difference carries value_kind '
                'difference_delta'
            )
        if self.quantity_kind in (
            'sample_count', 'frame_count', 'generic_count'
        ) and self.value_kind != 'count':
            raise ValueError('a count quantity carries value_kind count')
        if self.quantity_kind in (
            'acoustic_pressure_rms', 'acoustic_pressure_peak',
            'voltage_rms', 'voltage_peak', 'voltage_peak_to_peak',
            'current_rms', 'current_peak',
        ) and not self.reference_condition:
            raise ValueError(
                f'{self.quantity_kind} requires reference_condition — '
                'RMS/peak identity and measurement reference are part of '
                'the quantity (#728 §11/§12)'
            )
        if self.source is not None and (
            self.quantity_kind != 'other_declared'
            and self.source.source_unit not in allowed
        ):
            raise ValueError(
                'source unit must be registered for the quantity kind — '
                'an unmapped provider unit stays UNKNOWN (#728 §18)'
            )
        for label, ref in (
            ('uncertainty_ref', self.uncertainty_ref),
            ('evidence_ref', self.evidence_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        expected = _hash(self.identity_payload())
        if self.quantity_sha256 != expected:
            raise ValueError('typed quantity hash mismatch')
        if self.quantity_id != _semantic_id('tqty', expected):
            raise ValueError('typed quantity id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'quantity_kind': self.quantity_kind,
            'value_kind': self.value_kind,
            'canonical_value': self.canonical_value,
            'canonical_unit': self.canonical_unit,
            'display_unit': self.display_unit,
            'source': (
                self.source.model_dump(mode='json')
                if self.source is not None
                else None
            ),
            'ratio_semantics': self.ratio_semantics,
            'angle_semantics': self.angle_semantics,
            'reference_condition': self.reference_condition,
            'declared_quantity_label': self.declared_quantity_label,
            'uncertainty_ref': (
                self.uncertainty_ref.model_dump(mode='json')
                if self.uncertainty_ref is not None
                else None
            ),
            'evidence_ref': (
                self.evidence_ref.model_dump(mode='json')
                if self.evidence_ref is not None
                else None
            ),
            'conversion_profile': self.conversion_profile,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def same_quantity_identity(self, other: 'CadTypedQuantity') -> bool:
        """Whether two quantities are the *same kind of thing* for
        arithmetic: kind, value kind and the semantic sub-identities
        (ratio semantics, angle semantics, reference condition). Values
        may differ; identity may not."""
        return (
            self.quantity_kind == other.quantity_kind
            and self.value_kind == other.value_kind
            and self.ratio_semantics == other.ratio_semantics
            and self.angle_semantics == other.angle_semantics
            and (
                self.reference_condition == other.reference_condition
            )
        )


def typed_quantity_binding(quantity: CadTypedQuantity) -> AuthorityRef:
    return AuthorityRef(
        kind='typed_quantity',
        ref_id=quantity.quantity_id,
        ref_sha256=quantity.quantity_sha256,
    )


# ---------------------------------------------------------------------------
# Operation verdicts
# ---------------------------------------------------------------------------

QuantityOperationKind = Literal[
    'convert',
    'compare',
    'add',
    'subtract',
    'ratio_of',
    'scalar_multiply',
    'scalar_divide',
    'multiply',
    'divide',
    'frequency_to_angular',
    'angular_to_frequency',
    'samples_to_duration',
    'frames_to_duration',
    'airflow_to_air_changes',
    'pressure_reference_apply',
    'to_logarithmic',
    'other_declared',
]

QuantityOperationState = Literal[
    'computed',
    'computed_with_limitations',
    'incompatible_quantities',
    'requires_timebase_identity',
    'requires_logarithmic_authority',
    'requires_declared_reference',
    'unsupported_operation',
    'unverified',
]

OPERATION_STATE_LABELS: dict[str, str] = {
    'computed': '演算成立',
    'computed_with_limitations': '演算成立（制約付き）',
    'incompatible_quantities': '物理量の不一致',
    'requires_timebase_identity': 'タイムベース同一性が必要',
    'requires_logarithmic_authority': '対数量権威(#691)が必要',
    'requires_declared_reference': '基準宣言が必要',
    'unsupported_operation': '未定義の演算',
    'unverified': '未検証',
}

QUANTITY_KIND_LABELS: dict[str, str] = {
    'length': '長さ',
    'area': '面積',
    'volume': '体積',
    'time_duration': '時間幅',
    'timestamp_instant': '時刻',
    'frequency': '周波数',
    'angular_frequency': '角周波数',
    'plane_angle': '角度',
    'velocity': '速度',
    'acceleration': '加速度',
    'pressure_absolute': '絶対圧',
    'pressure_gauge': 'ゲージ圧',
    'pressure_differential': '差圧',
    'acoustic_pressure_rms': '音圧(実効値)',
    'acoustic_pressure_peak': '音圧(ピーク)',
    'temperature_absolute': '絶対温度',
    'temperature_difference': '温度差',
    'mass': '質量',
    'force': '力',
    'power_electrical': '電力',
    'power_acoustic': '音響パワー',
    'power_radiometric': '放射束',
    'power_thermal': '熱量率',
    'power_other_declared': 'その他パワー',
    'energy': 'エネルギー',
    'voltage_rms': '電圧(実効値)',
    'voltage_peak': '電圧(ピーク)',
    'voltage_peak_to_peak': '電圧(峰々)',
    'current_rms': '電流(実効値)',
    'current_peak': '電流(ピーク)',
    'resistance_impedance': '抵抗・インピーダンス',
    'airflow_volume_rate': '体積流量',
    'mass_flow_rate': '質量流量',
    'air_changes_per_hour': '換気回数',
    'luminance': '輝度',
    'illuminance': '照度',
    'luminous_flux': '光束',
    'ratio': '比率',
    'sample_count': 'サンプル数',
    'frame_count': 'フレーム数',
    'generic_count': '個数',
    'dimensionless': '無次元量',
    'other_declared': '宣言済みその他',
}


class CadQuantityOperation(BaseModel):
    """A sealed fail-closed quantity-operation verdict (#728 §4).

    Every requested arithmetic is recorded with its operands, the
    fail-closed state and — when valid — the derived value carrying full
    quantity identity. A rejected operation is still evidence: the
    verdict records *why* the arithmetic is not physically defined.
    """

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    operation: QuantityOperationKind
    operand_refs: tuple[AuthorityRef, ...]
    state: QuantityOperationState
    derived: CadDerivedQuantity | None = None
    target_unit: str | None = None
    timebase_ref: AuthorityRef | None = None
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    operation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_operation(self) -> 'CadQuantityOperation':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if not self.operand_refs:
            raise ValueError('an operation requires at least one operand')
        for i, ref in enumerate(self.operand_refs):
            if ref.ref_sha256 is None:
                raise ValueError(
                    f'operand_refs[{i}] must carry its sha256 pin'
                )
        if self.timebase_ref is not None and (
            self.timebase_ref.ref_sha256 is None
        ):
            raise ValueError('timebase_ref must carry its sha256 pin')
        if self.state in ('computed', 'computed_with_limitations') and (
            self.derived is None
        ):
            raise ValueError(
                'a computed operation must carry its derived value — '
                'a verdict without the value is not evidence'
            )
        if self.operation == 'convert' and self.state == 'computed' and (
            not self.target_unit
        ):
            raise ValueError(
                'a successful conversion names the unit it produced'
            )
        expected = _hash(self.identity_payload())
        if self.operation_sha256 != expected:
            raise ValueError('quantity operation hash mismatch')
        if self.operation_id != _semantic_id('tqop', expected):
            raise ValueError('quantity operation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'operation': self.operation,
            'operand_refs': [
                r.model_dump(mode='json') for r in self.operand_refs
            ],
            'state': self.state,
            'derived': (
                self.derived.model_dump(mode='json')
                if self.derived is not None
                else None
            ),
            'target_unit': self.target_unit,
            'timebase_ref': (
                self.timebase_ref.model_dump(mode='json')
                if self.timebase_ref is not None
                else None
            ),
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


def quantity_operation_binding(
    operation: CadQuantityOperation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='quantity_operation',
        ref_id=operation.operation_id,
        ref_sha256=operation.operation_sha256,
    )


# ---------------------------------------------------------------------------
# Conversion + evaluation
# ---------------------------------------------------------------------------


def to_canonical(value: float, unit: str) -> float:
    """Map ``value`` expressed in ``unit`` to the family canonical unit."""
    definition = unit_def(unit)
    return value * definition.scale + definition.offset


def from_canonical(canonical: float, unit: str) -> float:
    """Map a family-canonical value back to ``unit``."""
    definition = unit_def(unit)
    return (canonical - definition.offset) / definition.scale


def _identity_tuple(
    q: CadTypedQuantity | CadQuantityRef,
) -> tuple[Any, ...]:
    if isinstance(q, CadTypedQuantity):
        return (
            q.quantity_kind,
            q.value_kind,
            q.ratio_semantics,
            q.angle_semantics,
            q.reference_condition,
        )
    return (
        q.quantity_kind,
        q.value_kind,
        q.ratio_semantics,
        None,
        '',
    )


def _same_identity(
    a: CadTypedQuantity | CadQuantityRef,
    b: CadTypedQuantity | CadQuantityRef,
) -> bool:
    # Inline operands carry no angle semantics / reference condition, so
    # they only match a sealed quantity that declares neither.
    return _identity_tuple(a) == _identity_tuple(b)


def _operand_ref(
    q: CadTypedQuantity | CadQuantityRef,
) -> AuthorityRef:
    if isinstance(q, CadTypedQuantity):
        return typed_quantity_binding(q)
    # Inline operands get an AuthorityRef keyed by their own identity
    # hash so a verdict still names exactly what it consumed.
    digest = _hash({
        'quantity_kind': q.quantity_kind,
        'value_kind': q.value_kind,
        'unit': q.unit,
        'value': q.value,
        'ratio_semantics': q.ratio_semantics,
    })
    return AuthorityRef(
        kind='inline_quantity',
        ref_id=f'inline-{digest[:24]}',
        ref_sha256=digest,
    )


def _canonical_of(q: CadTypedQuantity | CadQuantityRef) -> float:
    if isinstance(q, CadTypedQuantity):
        return q.canonical_value
    return q.canonical_value()


def _kind_of(q: CadTypedQuantity | CadQuantityRef) -> QuantityKind:
    return q.quantity_kind


def _vkind_of(q: CadTypedQuantity | CadQuantityRef) -> ValueKind:
    return q.value_kind


def evaluate_quantity_operation(
    *,
    document_id: str,
    operation: QuantityOperationKind,
    operands: tuple[CadTypedQuantity | CadQuantityRef, ...]
    | list[CadTypedQuantity | CadQuantityRef],
    target_unit: str | None = None,
    timebase_ref: AuthorityRef | None = None,
    evaluated_at_utc: str | None = None,
) -> CadQuantityOperation:
    """Fail-closed typed-quantity arithmetic verdict (#728 §4–§14).

    - ``convert`` — same quantity kind, registered target unit; the
      affine map runs through the canonical unit so degc→degf is exact,
      never a guessed factor.
    - ``compare``/``add``/``subtract`` — identical quantity identity;
      absolute/delta algebra is enforced: affine kinds (absolute
      temperature, timestamps) never add absolutes; ``a - a`` yields a
      difference; ``a + Δ`` yields an absolute.
    - ``ratio_of`` — same identity → a ``ratio`` result whose semantics
      are the operands' own (the quotient of two lengths is a plain
      fraction; naming it a probability is a new declaration).
    - ``scalar_multiply``/``scalar_divide`` — the scalar operand must be
      dimensionless or ratio; result keeps the dimensional identity.
    - ``multiply``/``divide`` — only declared derived kinds (length²→
      area, volume/time→flow); anything else is ``unsupported_operation``.
    - ``frequency_to_angular``/``angular_to_frequency`` — the named 2π
      crossing between ``frequency`` and ``angular_frequency`` kinds.
    - ``samples_to_duration``/``frames_to_duration`` — require an exact
      rate operand (frequency kind) plus a ``timebase_ref`` pinning the
      clock/rate identity; without it ``requires_timebase_identity``.
    - ``airflow_to_air_changes`` — airflow_volume_rate ÷ room volume →
      ACH; the room volume operand is required so the derivation names
      its geometry.
    - ``pressure_reference_apply`` — gauge + declared ambient absolute
      pressure → absolute; the only legal gauge→absolute crossing.
    - ``to_logarithmic`` — always ``requires_logarithmic_authority``:
      linear→dB crossings belong to #691 with reference/weighting
      identity, never to a scale factor.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    ops = tuple(operands)
    if not ops:
        raise ValueError('an operation requires at least one operand')
    reasons: list[str] = []
    state: QuantityOperationState
    derived: CadDerivedQuantity | None = None

    def _derived(kind: QuantityKind, vkind: ValueKind, value: float,
                 label: str, unit: str | None = None) -> CadDerivedQuantity:
        return CadDerivedQuantity(
            value=value,
            quantity_kind=kind,
            value_kind=vkind,
            result_unit=unit or _canonical_unit_for(kind),
            ratio_semantics=(
                ops[0].ratio_semantics
                if isinstance(ops[0], CadTypedQuantity)
                else getattr(ops[0], 'ratio_semantics', None)
            ) if kind == 'ratio' else None,
            derivation_label=label,
        )

    if operation == 'convert':
        if len(ops) != 1:
            raise ValueError('convert requires exactly one operand')
        source = ops[0]
        if target_unit is None:
            raise ValueError('convert requires an explicit target_unit')
        kind = _kind_of(source)
        if kind == 'other_declared':
            state = 'unverified'
            reasons.append(
                'an other_declared quantity converts only under its own '
                'declared policy — the registry has no semantics for it'
            )
        elif target_unit not in _KIND_UNITS[kind]:
            state = 'incompatible_quantities'
            reasons.append(
                f'target unit {target_unit!r} is not a {kind} unit — '
                'conversion never crosses quantity kinds'
            )
        else:
            canonical = _canonical_of(source)
            value_out = from_canonical(canonical, target_unit)
            state = 'computed'
            derived = _derived(
                kind, _vkind_of(source), value_out,
                f'convert to {target_unit}', unit=target_unit,
            )
            src_unit = (
                source.display_unit
                if isinstance(source, CadTypedQuantity)
                else source.unit
            )
            if UNIT_DEFS[src_unit].affine or UNIT_DEFS[target_unit].affine:
                reasons.append(
                    'affine conversion applied through the canonical '
                    'unit — offsets are scale geometry, not factors'
                )
                state = 'computed_with_limitations'

    elif operation in ('compare', 'add', 'subtract', 'ratio_of'):
        if len(ops) != 2:
            raise ValueError(f'{operation} requires exactly two operands')
        a, b = ops
        kind_a, kind_b = _kind_of(a), _kind_of(b)
        # Affine absolute/difference pairing (#728 §6): an absolute
        # temperature combines with a temperature difference even
        # though they are declared as distinct kinds — 20 °C + Δ10 K is
        # a physical operation; 20 °C + 10 °C is not.
        vk_a0, vk_b0 = _vkind_of(a), _vkind_of(b)
        cross_affine = (
            operation in ('add', 'subtract')
            and (
                (
                    kind_a in _ABS_DIFF_PAIR
                    and _ABS_DIFF_PAIR[kind_a] == kind_b
                    and vk_a0 == 'absolute'
                    and vk_b0 == 'difference_delta'
                )
                or (
                    kind_b in _ABS_DIFF_PAIR
                    and _ABS_DIFF_PAIR[kind_b] == kind_a
                    and vk_b0 == 'absolute'
                    and vk_a0 == 'difference_delta'
                )
            )
        )
        if kind_a != kind_b and not cross_affine:
            state = 'incompatible_quantities'
            reasons.append(
                f'{kind_a} vs {kind_b} are different quantity kinds — '
                'equal dimensions are not equal physics (#728 §4)'
            )
        elif not cross_affine and not _same_identity(a, b):
            state = 'incompatible_quantities'
            reasons.append(
                'value kind / ratio semantics / reference condition '
                'differ — the numbers are not the same kind of quantity'
            )
        elif cross_affine:
            abs_op = a if vk_a0 == 'absolute' else b
            diff_op = b if vk_a0 == 'absolute' else a
            abs_kind = _kind_of(abs_op)
            va = _canonical_of(abs_op)
            vb = _canonical_of(diff_op)
            if operation == 'add':
                state = 'computed'
                derived = _derived(
                    abs_kind, 'absolute', va + vb,
                    'absolute + difference_delta → absolute',
                )
            elif abs_op is a:
                state = 'computed'
                derived = _derived(
                    abs_kind, 'absolute', va - vb,
                    'absolute − difference_delta → absolute',
                )
            else:
                state = 'incompatible_quantities'
                reasons.append(
                    'difference_delta − absolute is undefined — a '
                    'relative offset has no absolute origin'
                )
        else:
            va, vb = _canonical_of(a), _canonical_of(b)
            affine = kind_a in _AFFINE_KINDS
            vk = _vkind_of(a)
            if operation == 'compare':
                state = 'computed'
                if affine:
                    diff_kind = _ABS_DIFF_PAIR[kind_a]
                    derived = CadDerivedQuantity(
                        value=va - vb,
                        quantity_kind=diff_kind,
                        value_kind='absolute',
                        result_unit=_canonical_unit_for(diff_kind),
                        derivation_label=(
                            'signed difference (a − b) → temperature '
                            'difference'
                        ),
                    )
                else:
                    derived = CadDerivedQuantity(
                        value=va - vb,
                        quantity_kind=kind_a,
                        value_kind='difference_delta',
                        result_unit=_canonical_unit_for(kind_a),
                        derivation_label='signed difference (a − b)',
                    )
            elif operation == 'add':
                if affine and vk == 'absolute':
                    state = 'incompatible_quantities'
                    reasons.append(
                        'absolute + absolute is undefined on an affine '
                        'scale — 20 °C + 10 °C is not a physical '
                        'operation (#728 §6); use absolute + '
                        'difference_delta'
                    )
                elif vk in ('absolute', 'declared_other'):
                    state = 'computed'
                    derived = _derived(
                        kind_a, 'absolute', va + vb,
                        'absolute + absolute',
                    )
                elif vk == 'difference_delta':
                    state = 'computed'
                    derived = _derived(
                        kind_a, 'difference_delta', va + vb,
                        'delta + delta',
                    )
                else:
                    state = 'incompatible_quantities'
                    reasons.append(
                        f'add of {vk} operands is not defined — a '
                        'gauge/normalized value does not add to itself'
                    )
            elif operation == 'subtract':
                if vk == 'absolute':
                    state = 'computed'
                    if affine:
                        diff_kind = _ABS_DIFF_PAIR[kind_a]
                        derived = CadDerivedQuantity(
                            value=va - vb,
                            quantity_kind=diff_kind,
                            value_kind='absolute',
                            result_unit=_canonical_unit_for(diff_kind),
                            derivation_label=(
                                'absolute − absolute → temperature '
                                'difference'
                            ),
                        )
                    else:
                        derived = _derived(
                            kind_a, 'difference_delta', va - vb,
                            'absolute − absolute → difference',
                        )
                elif vk == 'difference_delta':
                    state = 'computed'
                    derived = _derived(
                        kind_a, 'difference_delta', va - vb,
                        'delta − delta',
                    )
                else:
                    state = 'incompatible_quantities'
                    reasons.append(
                        f'subtract of {vk} operands is not defined'
                    )
            else:  # ratio_of
                if vk not in ('absolute', 'difference_delta'):
                    state = 'incompatible_quantities'
                    reasons.append(
                        'a ratio of non-absolute/non-delta values is '
                        'not a declared physical ratio'
                    )
                else:
                    derived = CadDerivedQuantity(
                        value=va / vb,
                        quantity_kind='ratio',
                        value_kind='absolute',
                        result_unit='fraction',
                        ratio_semantics='plain_fraction',
                        derivation_label='ratio of like quantities',
                    )
                    state = 'computed'
                    if vk == 'difference_delta' or affine:
                        state = 'computed_with_limitations'
                        reasons.append(
                            'a ratio of interval/affine-scale values is '
                            'a ratio of deltas, not of absolute '
                            'positions — the scale zero is arbitrary'
                        )

    elif operation in ('scalar_multiply', 'scalar_divide'):
        if len(ops) != 2:
            raise ValueError(f'{operation} requires exactly two operands')
        value_q, scalar = ops
        scalar_kind = _kind_of(scalar)
        if scalar_kind not in ('dimensionless', 'ratio'):
            state = 'incompatible_quantities'
            reasons.append(
                'a scalar operand must be dimensionless or a declared '
                'ratio — scaling a quantity by a dimensioned number is '
                'a multiply, not a scalar'
            )
        elif _vkind_of(value_q) == 'count':
            state = 'incompatible_quantities'
            reasons.append(
                'counts are exact integers in meaning — apply declared '
                'rate/timebase operations instead'
            )
        else:
            sv = _canonical_of(scalar)
            vv = _canonical_of(value_q)
            result = (
                vv * sv if operation == 'scalar_multiply' else vv / sv
            )
            state = 'computed'
            derived = _derived(
                _kind_of(value_q), _vkind_of(value_q), result,
                f'{operation} by dimensionless/ratio scalar',
            )
            if _kind_of(value_q) in _AFFINE_KINDS and (
                _vkind_of(value_q) == 'absolute'
            ):
                state = 'incompatible_quantities'
                derived = None
                reasons.append(
                    'scaling an absolute affine-scale value scales its '
                    'arbitrary zero too — only differences scale on '
                    'affine scales (#728 §6)'
                )

    elif operation in ('multiply', 'divide'):
        if len(ops) != 2:
            raise ValueError(f'{operation} requires exactly two operands')
        a, b = ops
        key = (_kind_of(a), _kind_of(b))
        table = _PRODUCT_KINDS if operation == 'multiply' else _QUOTIENT_KINDS
        result_kind = table.get(key)
        if result_kind is None:
            state = 'unsupported_operation'
            reasons.append(
                f'{key[0]} × {key[1]} is not a declared derived '
                'quantity — inventing the product kind would fabricate '
                'semantics (#728 §4)'
            )
        else:
            state = 'computed'
            derived = _derived(
                result_kind, 'absolute',
                _canonical_of(a) * _canonical_of(b)
                if operation == 'multiply'
                else _canonical_of(a) / _canonical_of(b),
                f'declared {operation} → {result_kind}',
            )

    elif operation in ('frequency_to_angular', 'angular_to_frequency'):
        if len(ops) != 1:
            raise ValueError('the 2π crossing takes exactly one operand')
        source = ops[0]
        kind = _kind_of(source)
        if operation == 'frequency_to_angular':
            if kind != 'frequency':
                state = 'incompatible_quantities'
                reasons.append('operand is not a frequency quantity')
            else:
                state = 'computed'
                derived = _derived(
                    'angular_frequency', 'absolute',
                    _canonical_of(source) * 2.0 * pi,
                    'ω = 2πf',
                )
        else:
            if kind != 'angular_frequency':
                state = 'incompatible_quantities'
                reasons.append(
                    'operand is not an angular_frequency quantity'
                )
            else:
                state = 'computed'
                derived = _derived(
                    'frequency', 'absolute',
                    _canonical_of(source) / (2.0 * pi),
                    'f = ω / 2π',
                )

    elif operation in ('samples_to_duration', 'frames_to_duration'):
        if len(ops) != 2:
            raise ValueError(
                'count→duration requires the count and the exact rate'
            )
        count_q, rate_q = ops
        wanted = (
            'sample_count' if operation == 'samples_to_duration'
            else 'frame_count'
        )
        if _kind_of(count_q) != wanted or _kind_of(rate_q) != 'frequency':
            state = 'incompatible_quantities'
            reasons.append(
                f'{operation} requires a {wanted} operand and a '
                'frequency (rate) operand — a plausible-looking rate is '
                'never assumed (#728 §9)'
            )
        elif timebase_ref is None:
            state = 'requires_timebase_identity'
            reasons.append(
                'no timebase/rate identity is pinned — 480 samples are '
                'not milliseconds until the exact clock is declared '
                '(#728 §9, compose #609)'
            )
        else:
            state = 'computed'
            derived = _derived(
                'time_duration', 'absolute',
                _canonical_of(count_q) / _canonical_of(rate_q),
                f'{operation} under pinned timebase',
            )

    elif operation == 'airflow_to_air_changes':
        if len(ops) != 2:
            raise ValueError('ACH derivation requires flow + room volume')
        flow_q, volume_q = ops
        if _kind_of(flow_q) != 'airflow_volume_rate' or (
            _kind_of(volume_q) != 'volume'
        ):
            state = 'incompatible_quantities'
            reasons.append(
                'ACH requires an airflow_volume_rate and the room '
                'volume identity it is derived against (#728 §14)'
            )
        else:
            state = 'computed'
            derived = _derived(
                'air_changes_per_hour', 'absolute',
                _canonical_of(flow_q) / _canonical_of(volume_q) * 3600.0,
                'flow ÷ room volume → air changes per hour',
            )

    elif operation == 'pressure_reference_apply':
        if len(ops) != 2:
            raise ValueError(
                'pressure_reference_apply requires gauge + ambient '
                'absolute pressure'
            )
        gauge_q, ambient_q = ops
        if _kind_of(gauge_q) != 'pressure_gauge' or (
            _kind_of(ambient_q) != 'pressure_absolute'
        ):
            state = 'incompatible_quantities'
            reasons.append(
                'gauge→absolute requires a declared ambient absolute '
                'pressure operand — relabeling the unit never performs '
                'the crossing (#728 §7)'
            )
        else:
            state = 'computed'
            derived = _derived(
                'pressure_absolute', 'absolute',
                _canonical_of(gauge_q) + _canonical_of(ambient_q),
                'gauge + declared ambient → absolute pressure',
            )
            reasons.append(
                'result is only as strong as the ambient datum — the '
                'ambient reference stays in provenance'
            )
            state = 'computed_with_limitations'

    elif operation == 'to_logarithmic':
        state = 'requires_logarithmic_authority'
        reasons.append(
            'linear→logarithmic conversion is not a unit conversion: it '
            'requires the exact reference value, ratio basis and '
            'weighting identity owned by the #691 typed-dB authority — '
            'Pa→dB SPL never happens as a scale factor (#728 §12/§20)'
        )

    else:  # other_declared
        state = 'unverified'
        reasons.append(
            'an other_declared operation carries no built-in semantics — '
            'its method must be established elsewhere'
        )

    payload = dict(
        document_id=document_id,
        operation=operation,
        operand_refs=tuple(_operand_ref(o) for o in ops),
        state=state,
        derived=derived,
        target_unit=target_unit if operation == 'convert' else None,
        timebase_ref=(
            timebase_ref
            if operation in ('samples_to_duration', 'frames_to_duration')
            else None
        ),
        reasons=tuple(reasons),
        evaluation_version=TYPED_QUANTITY_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal(
        CadQuantityOperation, payload,
        'operation_id', 'operation_sha256', 'tqop',
    )


def _canonical_unit_for(kind: QuantityKind) -> str:
    preferred = {
        'length': 'm', 'area': 'm2', 'volume': 'm3',
        'time_duration': 's', 'frequency': 'hz',
        'angular_frequency': 'rad_per_s', 'plane_angle': 'rad',
        'velocity': 'm_per_s', 'acceleration': 'm_per_s2',
        'pressure_absolute': 'pa', 'pressure_gauge': 'pa',
        'pressure_differential': 'pa', 'acoustic_pressure_rms': 'pa',
        'acoustic_pressure_peak': 'pa',
        'temperature_absolute': 'k', 'temperature_difference': 'delta_k',
        'mass': 'kg', 'force': 'n',
        'power_electrical': 'w', 'power_acoustic': 'w',
        'power_radiometric': 'w', 'power_thermal': 'w',
        'power_other_declared': 'w', 'energy': 'j',
        'voltage_rms': 'v', 'voltage_peak': 'v',
        'voltage_peak_to_peak': 'v', 'current_rms': 'a',
        'current_peak': 'a', 'resistance_impedance': 'ohm',
        'airflow_volume_rate': 'm3_per_s',
        'mass_flow_rate': 'kg_per_s',
        'air_changes_per_hour': 'ach',
        'luminance': 'cd_per_m2', 'illuminance': 'lx',
        'luminous_flux': 'lm', 'ratio': 'fraction',
        'sample_count': 'samples', 'frame_count': 'frames',
        'generic_count': 'count', 'dimensionless': 'one',
        'timestamp_instant': 's', 'other_declared': 'one',
    }
    return preferred[kind]


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_typed_quantity(
    *,
    document_id: str,
    quantity_kind: QuantityKind,
    value: float,
    unit: str,
    value_kind: ValueKind | None = None,
    display_unit: str | None = None,
    source: CadSourceObservation | None = None,
    ratio_semantics: RatioSemantics | None = None,
    angle_semantics: AngleSemantics | None = None,
    reference_condition: str = '',
    declared_quantity_label: str | None = None,
    uncertainty_ref: AuthorityRef | None = None,
    evidence_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadTypedQuantity:
    """Seal one typed physical quantity.

    ``value``+``unit`` are the declared observation; the canonical value
    is computed through the registry so ``1000 mm`` and ``1 m`` seal the
    identical quantity kind + canonical value while keeping their own
    display/source identity (#728 §3).
    """
    allowed = _KIND_UNITS[quantity_kind]
    if quantity_kind != 'other_declared':
        if not allowed:
            raise ValueError(
                f'{quantity_kind} cannot assert a numeric value'
            )
        if unit not in allowed:
            raise ValueError(
                f'unit {unit!r} is not registered for {quantity_kind}'
            )
        canonical_unit = _canonical_unit_for(quantity_kind)
        canonical = to_canonical(value, unit)
    else:
        canonical_unit = unit
        canonical = value
    if value_kind is None:
        value_kind = _default_value_kind(quantity_kind)
    payload = dict(
        document_id=document_id,
        quantity_kind=quantity_kind,
        value_kind=value_kind,
        canonical_value=canonical,
        canonical_unit=canonical_unit,
        display_unit=display_unit or unit,
        source=source,
        ratio_semantics=ratio_semantics,
        angle_semantics=angle_semantics,
        reference_condition=reference_condition,
        declared_quantity_label=declared_quantity_label,
        uncertainty_ref=uncertainty_ref,
        evidence_ref=evidence_ref,
        conversion_profile=UNIT_REGISTRY_VERSION,
        authority_version=TYPED_QUANTITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal(
        CadTypedQuantity, payload,
        'quantity_id', 'quantity_sha256', 'tqty',
    )


def _default_value_kind(kind: QuantityKind) -> ValueKind:
    if kind == 'pressure_gauge':
        return 'gauge_relative'
    if kind == 'pressure_differential':
        return 'difference_delta'
    if kind == 'temperature_difference':
        return 'difference_delta'
    if kind in ('sample_count', 'frame_count', 'generic_count'):
        return 'count'
    if kind == 'ratio':
        return 'normalized'
    return 'absolute'


__all__ = [
    'AngleSemantics',
    'CadDerivedQuantity',
    'CadQuantityOperation',
    'CadQuantityRef',
    'CadSourceObservation',
    'CadTypedQuantity',
    'CadUnitDef',
    'DimensionFamily',
    'OPERATION_STATE_LABELS',
    'QuantityKind',
    'QuantityOperationKind',
    'QuantityOperationState',
    'QUANTITY_KIND_LABELS',
    'RatioSemantics',
    'TYPED_QUANTITY_EVALUATION_VERSION',
    'TYPED_QUANTITY_SCHEMA_VERSION',
    'UNIT_DEFS',
    'UNIT_REGISTRY_VERSION',
    'ValueKind',
    'build_typed_quantity',
    'evaluate_quantity_operation',
    'from_canonical',
    'quantity_family_of',
    'quantity_operation_binding',
    'to_canonical',
    'typed_quantity_binding',
    'unit_allowed_for',
    'unit_def',
]
