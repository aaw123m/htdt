"""Typed logarithmic quantity / dB-reference authority (#691, REV58-IDENT).

``dB`` is not one physical quantity. Sound-pressure level, digital level,
voltage level, gain, power ratio, insertion loss, sound exposure,
loudness-related units and provider-relative volume scales have different
references and different algebra. Persisting them all as bare
``value_db`` floats lets a correct number flow into a physically
incompatible calculation — this module is the shared typed layer that
closes that failure class.

- :class:`CadLogQuantity` — the sealed quantity-identity record: quantity
  class (absolute log level / relative gain-loss / device-relative
  setting / linear value), domain (acoustic / digital / electrical /
  dimensionless), exact quantity kind, ratio basis (field vs
  power/energy — the 20·log10 vs 10·log10 decision comes from the
  quantity definition, never from a UI unit string), and for absolute
  levels a :class:`CadLogReference` carrying reference value, unit,
  standard/profile binding, weighting, band and time integration. A
  source-less ``dB`` cannot be sealed.
- :class:`CadCalibrationBridge` — a sealed, versioned
  observed/derived transfer relation between two typed quantities
  (e.g. a ``-20 dBFS`` stimulus to a measured ``85 dBC`` seat SPL through
  a pinned device state). It is a *calibration-chain relation*, not a
  unit conversion: a change of device trim/master volume supersedes it.
- :func:`evaluate_log_operation` + :class:`CadLogOperation` — the typed
  log-domain arithmetic gate. Level differences require same
  domain+quantity+reference; ``apply_gain`` requires a declared linear
  chain (limiter/compression per #649 breaks it); ``energetic_sum``
  applies to like power-capable levels; coherent sums require phase data
  (#690); ``dBFS - dBSPL`` arithmetic fails closed; ``arithmetic_mean``
  of log values is rejected without a declared method.

Composition (bindings, never merges):

- #618 ``cad_playback_reference_authority`` — measured channel SPL
  semantics, LFE +10 dB in-band gain profile and the device-state pin a
  bridge must keep; this layer supplies the typed quantity identity those
  observations bind to.
- ``cad_gain_structure`` — evidenced ``0 dBFS = full_scale_v_rms``
  domain crossing is exactly a ``CadCalibrationBridge`` over digital →
  electrical quantities.
- #576 isolation/insertion-loss, #602 exposure metrics, #592 device
  snapshots, #649 nonlinear-chain evidence — quantity identities stay
  distinct and operations cross them only through declared methods or
  bridges.
- #572/#604 uncertainty — an operation carries operand uncertainty refs
  through unchanged; the method layer remains theirs.

Literature / standards basis
----------------------------
- ISO 80000-8:2020 + Amd 1:2025 (*Quantities and units — Part 8:
  Acoustics*) — acoustic quantities are distinct quantities with named
  references (e.g. ``Lp`` re 20 µPa); a display label is not the
  quantity identity.
- AES17-2020 — the clarified ``FS``/``dBFS`` digital-level semantics;
  ``0 dBFS`` is a digital reference boundary, not ``0 dB SPL`` and not an
  analog voltage.
- SMPTE RP 200:2012 — the cinema ``-20 dBFS → 85 dBC`` chain is a
  *calibration* relationship through specific equipment states, the
  canonical example of a bridge rather than a conversion factor.
- ITU-R BS.775-4 Annex 7 — LFE ``+10 dB`` is an in-band reproduction
  gain relationship, not an unconditional broadband acoustic target.
- IEC 61672 / IEC 61260 — weighting (A/C/Z) and band-filter identity are
  part of a measured level's identity, not suffix cosmetics.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite, log10
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


LOGQ_AUTHORITY_SCHEMA_VERSION = 'logq-typed-quantity-1'
LOGQ_EVALUATION_VERSION = 'logq-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


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


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#691)
# ---------------------------------------------------------------------------

QuantityClass = Literal[
    'absolute_log_level',
    'relative_gain_loss',
    'device_relative_setting',
    'linear_value',
]
"""#691 §1 — an absolute level, a dimensionless ratio between like
quantities, a provider control scale and a linear-domain value are
different classes of thing; none silently becomes another."""

QuantityDomain = Literal[
    'acoustic',
    'digital',
    'electrical',
    'dimensionless',
    'other_declared',
]

QuantityKind = Literal[
    # acoustic (#691 §2)
    'sound_pressure_level',
    'sound_power_level',
    'sound_intensity_level',
    'sound_exposure_level',
    'peak_sound_pressure_level',
    'equivalent_continuous_sound_level',
    'band_level',
    'sound_pressure_pa',
    'sound_power_w',
    'other_acoustic',
    # digital (#691 §3, AES17-2020)
    'fs_relative_amplitude',
    'dbfs_sample_peak',
    'dbfs_true_peak',
    'dbfs_rms_level',
    'dbfs_declared_convention',
    'other_digital',
    # electrical (#691 §4)
    'voltage_level_dbv',
    'voltage_level_dbu',
    'voltage_level_declared',
    'voltage_v_rms',
    'power_w',
    'current_a',
    'load_impedance_ohm',
    'other_electrical',
    # dimensionless ratios (#691 §7/§9/§13)
    'gain_ratio',
    'power_ratio',
    'level_difference',
    'transmission_loss',
    'insertion_loss',
    'other_dimensionless',
    # explicit escape hatch — must carry declared_quantity_label
    'other_declared',
]

DOMAIN_QUANTITIES: dict[QuantityDomain, frozenset[str]] = {
    'acoustic': frozenset({
        'sound_pressure_level', 'sound_power_level', 'sound_intensity_level',
        'sound_exposure_level', 'peak_sound_pressure_level',
        'equivalent_continuous_sound_level', 'band_level',
        'sound_pressure_pa', 'sound_power_w', 'other_acoustic',
    }),
    'digital': frozenset({
        'fs_relative_amplitude', 'dbfs_sample_peak', 'dbfs_true_peak',
        'dbfs_rms_level', 'dbfs_declared_convention', 'other_digital',
    }),
    'electrical': frozenset({
        'voltage_level_dbv', 'voltage_level_dbu', 'voltage_level_declared',
        'voltage_v_rms', 'power_w', 'current_a', 'load_impedance_ohm',
        'other_electrical',
    }),
    'dimensionless': frozenset({
        'gain_ratio', 'power_ratio', 'level_difference',
        'transmission_loss', 'insertion_loss', 'other_dimensionless',
    }),
    'other_declared': frozenset({'other_declared'}),
}

RatioBasis = Literal[
    'field_amplitude_like',
    'power_energy_like',
    'declared_other',
    'unknown',
]
"""Whether the base quantity is field/amplitude-like (20·log10) or
power/energy-like (10·log10) — the logarithm convention derives from the
physical quantity definition (#691 §5), never from the display unit."""

StandardBinding = Literal[
    'iso_80000-8:2020',
    'aes17:2020',
    'iec61672',
    'iec61260',
    'smpte_rp200',
    'itu_bs775',
    'vendor_declared',
    'custom_declared',
    'undeclared',
]

WeightingKind = Literal['a', 'c', 'z', 'other_declared', 'unweighted', 'undeclared']

TimeIntegration = Literal[
    'instantaneous',
    'peak',
    'fast',
    'slow',
    'impulse',
    'leq_period',
    'other_declared',
    'undeclared',
]


# ---------------------------------------------------------------------------
# Embedded descriptors
# ---------------------------------------------------------------------------


class CadBandSpec(BaseModel):
    """The band identity of a banded level (#691 §11/§12).

    Changing FFT/bin width or filter bandwidth changes the numeric value,
    so band geometry is part of quantity identity — a per-Hz spectral
    density, a fractional-octave band level and a broadband-integrated
    level never share arithmetic.
    """

    model_config = ConfigDict(frozen=True)

    band_kind: Literal[
        'octave', 'third_octave', 'fractional_octave',
        'fft_bin', 'custom_hz', 'broadband_integrated',
    ]
    center_hz: float | None = None
    low_hz: float | None = None
    high_hz: float | None = None
    bandwidth_hz: float | None = None

    @model_validator(mode='after')
    def valid_band(self) -> 'CadBandSpec':
        for label, value in (
            ('center_hz', self.center_hz),
            ('low_hz', self.low_hz),
            ('high_hz', self.high_hz),
            ('bandwidth_hz', self.bandwidth_hz),
        ):
            if value is not None:
                _require_finite(value, f'band spec {label}')
        if self.band_kind == 'broadband_integrated':
            return self
        if self.band_kind in ('octave', 'third_octave') and (
            self.center_hz is None
        ):
            raise ValueError(
                'a standard octave/fractional band requires its nominal '
                'center frequency — a "125 Hz band" without center is '
                'not a reproducible quantity'
            )
        if self.band_kind == 'custom_hz' and (
            self.low_hz is None or self.high_hz is None
        ):
            raise ValueError(
                'a custom band requires explicit low/high limits'
            )
        if self.band_kind == 'fft_bin' and self.bandwidth_hz is None:
            raise ValueError(
                'an FFT bin quantity requires its bin bandwidth — '
                'spectral-density vs band-integrated values differ by '
                'exactly this number'
            )
        if (
            self.low_hz is not None
            and self.high_hz is not None
            and self.high_hz < self.low_hz
        ):
            raise ValueError('band limits must be ascending')
        return self


class CadLogReference(BaseModel):
    """The reference identity of an absolute logarithmic level (#691 §6).

    A level is a ratio against a reference; the reference value, its unit,
    the governing standard/profile and any weighting/band/time integration
    are part of the quantity's identity — not formatting.
    """

    model_config = ConfigDict(frozen=True)

    reference_value: float | None = None
    reference_unit: str | None = None
    reference_label: str | None = None
    standard_profile: StandardBinding = 'undeclared'
    weighting: WeightingKind = 'undeclared'
    band: CadBandSpec | None = None
    time_integration: TimeIntegration = 'undeclared'

    @model_validator(mode='after')
    def valid_reference(self) -> 'CadLogReference':
        if self.reference_value is not None:
            _require_finite(
                self.reference_value, 'reference value'
            )
            if self.reference_value <= 0:
                raise ValueError('reference value must be positive')
        if self.standard_profile == 'undeclared' and (
            self.reference_value is None
            or not self.reference_unit
            or not self.reference_label
        ):
            raise ValueError(
                'an undeclared-profile reference must carry value + unit '
                '+ label — no source-less dB (#691 §6)'
            )
        return self

    def same_identity(self, other: 'CadLogReference') -> bool:
        return self.model_dump(mode='json') == other.model_dump(mode='json')


class CadDeviceScale(BaseModel):
    """A provider-defined control scale (#691 §10).

    An AVR ``0.0 dB`` display is a device-relative setting — its acoustic
    meaning exists only through a measured calibration relation (#618),
    which is pinned here as ``calibration_ref`` once it exists.
    """

    model_config = ConfigDict(frozen=True)

    provider: str = Field(min_length=1)
    device_identity: str = Field(min_length=1)
    scale_marker: str = Field(min_length=1)
    firmware_identity: str | None = None
    calibration_ref: AuthorityRef | None = None


class CadDerivedLogValue(BaseModel):
    """A computed result descriptor embedded in an operation verdict.

    Carries the same typed identity as a persisted quantity; it is a
    *derived* value (not sealed separately) whose provenance is the
    operation record itself.
    """

    model_config = ConfigDict(frozen=True)

    value_db: float
    quantity_class: QuantityClass
    domain: QuantityDomain
    quantity: QuantityKind
    reference: CadLogReference | None = None
    ratio_basis: RatioBasis = 'unknown'
    derivation_label: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class CadLogQuantity(BaseModel):
    """One sealed typed quantity value (#691 goal).

    The authority-boundary atom: every persisted logarithmic value carries
    its class, domain, exact quantity kind, ratio basis and — for absolute
    levels — the full reference identity. ``value_db`` is never a bare
    float at an authority boundary; a linear value uses ``linear_value``
    with its own unit instead.
    """

    model_config = ConfigDict(frozen=True)

    quantity_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    quantity_class: QuantityClass
    domain: QuantityDomain
    quantity: QuantityKind
    value_db: float | None = None
    linear_value: float | None = None
    linear_unit: str | None = None
    ratio_basis: RatioBasis = 'unknown'
    reference: CadLogReference | None = None
    device_scale: CadDeviceScale | None = None
    declared_quantity_label: str | None = None
    uncertainty_ref: AuthorityRef | None = None
    evidence_ref: AuthorityRef | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    quantity_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_quantity(self) -> 'CadLogQuantity':
        _require_iso8601(self.declared_at_utc, 'quantity declared_at_utc')
        allowed = DOMAIN_QUANTITIES[self.domain]
        if self.quantity not in allowed:
            raise ValueError(
                f'quantity {self.quantity!r} is not a {self.domain} '
                'quantity — cross-domain kinds never share a record'
            )
        if self.quantity.startswith('other_') and (
            not self.declared_quantity_label
        ):
            raise ValueError(
                'an other_* quantity requires declared_quantity_label'
            )
        if self.quantity_class == 'absolute_log_level':
            if self.value_db is None:
                raise ValueError(
                    'an absolute log level requires value_db'
                )
            if self.reference is None:
                raise ValueError(
                    'an absolute log level requires its reference — a '
                    'dB without a named zero-point is not a quantity '
                    '(#691 §6)'
                )
            if self.ratio_basis == 'unknown':
                raise ValueError(
                    'an absolute log level requires its ratio basis '
                    '(field vs power) — the 20·log10/10·log10 convention '
                    'is part of the quantity definition'
                )
        if self.quantity_class == 'relative_gain_loss':
            if self.value_db is None:
                raise ValueError('a gain/loss requires value_db')
            if self.ratio_basis == 'unknown':
                raise ValueError(
                    'a gain/loss ratio requires its declared basis'
                )
        if self.quantity_class == 'device_relative_setting':
            if self.value_db is None:
                raise ValueError(
                    'a device-relative setting requires value_db'
                )
            if self.device_scale is None:
                raise ValueError(
                    'a device-relative setting requires its provider/'
                    'device/scale identity — an AVR "0.0 dB" is not an '
                    'acoustic reference (#691 §10)'
                )
        if self.quantity_class == 'linear_value':
            if self.linear_value is None or not self.linear_unit:
                raise ValueError(
                    'a linear value requires linear_value + linear_unit'
                )
            _require_finite(self.linear_value, 'linear_value')
        if self.value_db is not None:
            _require_finite(self.value_db, 'value_db')
            if self.quantity == 'dbfs_sample_peak' and (
                self.value_db > 0.0
            ):
                raise ValueError(
                    'a digital sample-peak level cannot exceed 0 dBFS'
                )
            if self.quantity == 'fs_relative_amplitude' and (
                self.value_db > 0.0
            ):
                raise ValueError(
                    'a full-scale-relative amplitude cannot exceed 0 dBFS'
                )
            if self.quantity == 'dbfs_rms_level' and self.value_db > 0.0:
                raise ValueError(
                    'a digital RMS level cannot exceed 0 dBFS'
                )
        if self.domain == 'acoustic' and (
            self.quantity_class == 'absolute_log_level'
        ):
            assert self.reference is not None  # enforced above
            if self.reference.weighting == 'undeclared':
                raise ValueError(
                    'a measured acoustic level requires an explicit '
                    'weighting declaration (a/c/z/unweighted/other) — '
                    '"85 dB" without weighting is not a reproducible '
                    'observation (#691 §11)'
                )
            if self.reference.time_integration == 'undeclared':
                raise ValueError(
                    'a measured acoustic level requires an explicit '
                    'time-integration declaration — peak/fast/slow/leq '
                    'change the quantity'
                )
        for label, ref in (
            ('uncertainty_ref', self.uncertainty_ref),
            ('evidence_ref', self.evidence_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        if self.device_scale is not None and (
            self.device_scale.calibration_ref is not None
            and self.device_scale.calibration_ref.ref_sha256 is None
        ):
            raise ValueError(
                'device_scale calibration_ref must carry its sha256 pin'
            )
        expected = _hash(self.identity_payload())
        if self.quantity_sha256 != expected:
            raise ValueError('log quantity hash mismatch')
        if self.quantity_id != _semantic_id('logqty', expected):
            raise ValueError('log quantity id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'quantity_class': self.quantity_class,
            'domain': self.domain,
            'quantity': self.quantity,
            'value_db': self.value_db,
            'linear_value': self.linear_value,
            'linear_unit': self.linear_unit,
            'ratio_basis': self.ratio_basis,
            'reference': (
                self.reference.model_dump(mode='json')
                if self.reference is not None
                else None
            ),
            'device_scale': (
                self.device_scale.model_dump(mode='json')
                if self.device_scale is not None
                else None
            ),
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
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def same_quantity_identity(self, other: 'CadLogQuantity') -> bool:
        """Whether two quantities are the *same kind of thing*: class,
        domain, quantity, ratio basis and full reference identity. The
        values may differ; the identity may not."""
        if (
            self.quantity_class != other.quantity_class
            or self.domain != other.domain
            or self.quantity != other.quantity
            or self.ratio_basis != other.ratio_basis
        ):
            return False
        if self.reference is None or other.reference is None:
            return self.reference is None and other.reference is None
        return self.reference.same_identity(other.reference)


def log_quantity_binding(quantity: CadLogQuantity) -> AuthorityRef:
    return AuthorityRef(
        kind='log_quantity',
        ref_id=quantity.quantity_id,
        ref_sha256=quantity.quantity_sha256,
    )


class CadCalibrationBridge(BaseModel):
    """A sealed versioned transfer relation between two typed quantities
    (#691 §15).

    e.g. ``-20 dBFS stimulus`` → ``85 dBC at seat`` through a pinned
    DAC/amplifier/speaker state. This is an observed *calibration-chain
    relation*, never a global unit conversion: it binds the exact device
    state and measurement evidence, and a device trim/master-volume change
    supersedes it (``superseded_by_ref`` — history is retained).
    """

    model_config = ConfigDict(frozen=True)

    bridge_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    bridge_label: str = Field(min_length=1)
    from_domain: QuantityDomain
    from_quantity: QuantityKind
    to_domain: QuantityDomain
    to_quantity: QuantityKind
    to_reference: CadLogReference | None = None
    transfer_db: float | None = None
    transfer_descriptor: str | None = None
    chain_refs: tuple[AuthorityRef, ...] = ()
    device_state_ref: AuthorityRef | None = None
    scope_json: str = '{}'
    status: Literal['active', 'superseded', 'withdrawn'] = 'active'
    superseded_by_ref: AuthorityRef | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    bridge_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_bridge(self) -> 'CadCalibrationBridge':
        _require_iso8601(self.declared_at_utc, 'bridge declared_at_utc')
        if self.from_domain == self.to_domain and (
            self.from_quantity == self.to_quantity
        ):
            raise ValueError(
                'a bridge must cross domains or quantities — '
                'same-quantity arithmetic is an operation, not a '
                'calibration relation'
            )
        if self.transfer_db is not None:
            _require_finite(self.transfer_db, 'transfer_db')
        if self.transfer_db is None and not self.transfer_descriptor:
            raise ValueError(
                'a bridge requires either a scalar transfer_db or a '
                'transfer_descriptor naming the non-constant relation'
            )
        if not self.chain_refs and self.device_state_ref is None:
            raise ValueError(
                'a calibration bridge requires chain evidence — the '
                'DAC/amplifier/transducer/measurement path it was '
                'observed on'
            )
        for label, ref in (
            *[(f'chain_refs[{i}]', r) for i, r in enumerate(self.chain_refs)],
            ('device_state_ref', self.device_state_ref),
            ('superseded_by_ref', self.superseded_by_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        if self.status == 'superseded' and self.superseded_by_ref is None:
            raise ValueError(
                'a superseded bridge must name its successor — the '
                'device-state change that invalidated it stays visible'
            )
        expected = _hash(self.identity_payload())
        if self.bridge_sha256 != expected:
            raise ValueError('calibration bridge hash mismatch')
        if self.bridge_id != _semantic_id('logbrg', expected):
            raise ValueError('calibration bridge id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'bridge_label': self.bridge_label,
            'from_domain': self.from_domain,
            'from_quantity': self.from_quantity,
            'to_domain': self.to_domain,
            'to_quantity': self.to_quantity,
            'to_reference': (
                self.to_reference.model_dump(mode='json')
                if self.to_reference is not None
                else None
            ),
            'transfer_db': self.transfer_db,
            'transfer_descriptor': self.transfer_descriptor,
            'chain_refs': [
                r.model_dump(mode='json') for r in self.chain_refs
            ],
            'device_state_ref': (
                self.device_state_ref.model_dump(mode='json')
                if self.device_state_ref is not None
                else None
            ),
            'scope_json': self.scope_json,
            'status': self.status,
            'superseded_by_ref': (
                self.superseded_by_ref.model_dump(mode='json')
                if self.superseded_by_ref is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def connects(
        self,
        source: CadLogQuantity,
        target_domain: QuantityDomain,
        target_quantity: QuantityKind,
    ) -> bool:
        return (
            self.status == 'active'
            and self.from_domain == source.domain
            and self.from_quantity == source.quantity
            and self.to_domain == target_domain
            and self.to_quantity == target_quantity
        )


def calibration_bridge_binding(bridge: CadCalibrationBridge) -> AuthorityRef:
    return AuthorityRef(
        kind='log_calibration_bridge',
        ref_id=bridge.bridge_id,
        ref_sha256=bridge.bridge_sha256,
    )


# ---------------------------------------------------------------------------
# Operation verdicts
# ---------------------------------------------------------------------------

LogOperationKind = Literal[
    'level_difference',
    'apply_gain',
    'energetic_sum',
    'coherent_sum',
    'arithmetic_mean',
    'convert',
    'compare',
    'other_declared',
]

LogOperationState = Literal[
    'compatible',
    'compatible_with_limitations',
    'requires_calibration_bridge',
    'coherent_requires_phase_data',
    'nonlinear_chain',
    'incompatible_quantities',
    'unverified',
]

#: Chain-linearity declaration for gain application (#691 §7 — a limiter,
#: compressor or loudspeaker compression breaks the fixed gain relation).
LinearityState = Literal[
    'linear_chain_declared',
    'nonlinear_or_dynamic',
    'undeclared',
]

#: Sum kind for multi-source combination (#691 §13/#690 — incoherent
#: energy addition vs coherent phasor addition are different physics).
SumKind = Literal[
    'incoherent_independent',
    'coherent_in_phase',
    'declared_other',
]

#: Declared averaging method — a bare arithmetic mean of dB values is
#: never default-justified.
MeanMethod = Literal[
    'energetic_mean',
    'declared_custom',
]


class CadLogOperation(BaseModel):
    """A sealed typed-operation verdict (#691 §13).

    Every requested log-domain operation is recorded with its operands,
    the fail-closed state and — when valid — the derived value carrying
    full quantity identity. A rejected operation is still evidence: the
    verdict records *why* the arithmetic is not physically defined.
    """

    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    operation: LogOperationKind
    operand_refs: tuple[AuthorityRef, ...]
    state: LogOperationState
    derived: CadDerivedLogValue | None = None
    bridge_ref: AuthorityRef | None = None
    linearity_state: LinearityState | None = None
    sum_kind: SumKind | None = None
    mean_method: MeanMethod | None = None
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    operation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_operation(self) -> 'CadLogOperation':
        _require_iso8601(
            self.evaluated_at_utc, 'operation evaluated_at_utc'
        )
        if not self.operand_refs:
            raise ValueError('an operation requires at least one operand')
        for i, ref in enumerate(self.operand_refs):
            if ref.ref_sha256 is None:
                raise ValueError(
                    f'operand_refs[{i}] must carry its sha256 pin'
                )
        if self.bridge_ref is not None and (
            self.bridge_ref.ref_sha256 is None
        ):
            raise ValueError('bridge_ref must carry its sha256 pin')
        if self.state == 'compatible' and self.derived is None:
            raise ValueError(
                'a compatible operation must carry its derived value — '
                'a verdict without the value is not evidence'
            )
        if self.operation == 'convert' and self.state == 'compatible' and (
            self.bridge_ref is None
        ):
            raise ValueError(
                'a successful conversion requires the calibration bridge '
                'it used — domain crossing without a pinned chain is not '
                'a quantity operation'
            )
        expected = _hash(self.identity_payload())
        if self.operation_sha256 != expected:
            raise ValueError('log operation hash mismatch')
        if self.operation_id != _semantic_id('logop', expected):
            raise ValueError('log operation id does not match its hash')
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
            'bridge_ref': (
                self.bridge_ref.model_dump(mode='json')
                if self.bridge_ref is not None
                else None
            ),
            'linearity_state': self.linearity_state,
            'sum_kind': self.sum_kind,
            'mean_method': self.mean_method,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_log_quantity(
    *,
    document_id: str,
    quantity_class: QuantityClass,
    domain: QuantityDomain,
    quantity: QuantityKind,
    value_db: float | None = None,
    linear_value: float | None = None,
    linear_unit: str | None = None,
    ratio_basis: RatioBasis = 'unknown',
    reference: CadLogReference | None = None,
    device_scale: CadDeviceScale | None = None,
    declared_quantity_label: str | None = None,
    uncertainty_ref: AuthorityRef | None = None,
    evidence_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadLogQuantity:
    """Seal one typed quantity value."""
    payload = dict(
        document_id=document_id,
        quantity_class=quantity_class,
        domain=domain,
        quantity=quantity,
        value_db=value_db,
        linear_value=linear_value,
        linear_unit=linear_unit,
        ratio_basis=ratio_basis,
        reference=reference,
        device_scale=device_scale,
        declared_quantity_label=declared_quantity_label,
        uncertainty_ref=uncertainty_ref,
        evidence_ref=evidence_ref,
        authority_version=LOGQ_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadLogQuantity, payload,
        'quantity_id', 'quantity_sha256', 'logqty',
    )


def build_calibration_bridge(
    *,
    document_id: str,
    bridge_label: str,
    from_domain: QuantityDomain,
    from_quantity: QuantityKind,
    to_domain: QuantityDomain,
    to_quantity: QuantityKind,
    to_reference: CadLogReference | None = None,
    transfer_db: float | None = None,
    transfer_descriptor: str | None = None,
    chain_refs: tuple[AuthorityRef, ...] | list[AuthorityRef] = (),
    device_state_ref: AuthorityRef | None = None,
    scope_json: str = '{}',
    status: Literal['active', 'superseded', 'withdrawn'] = 'active',
    superseded_by_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadCalibrationBridge:
    """Seal a cross-domain calibration relation."""
    payload = dict(
        document_id=document_id,
        bridge_label=bridge_label,
        from_domain=from_domain,
        from_quantity=from_quantity,
        to_domain=to_domain,
        to_quantity=to_quantity,
        to_reference=to_reference,
        transfer_db=transfer_db,
        transfer_descriptor=transfer_descriptor,
        chain_refs=tuple(chain_refs),
        device_state_ref=device_state_ref,
        scope_json=scope_json,
        status=status,
        superseded_by_ref=superseded_by_ref,
        authority_version=LOGQ_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadCalibrationBridge, payload,
        'bridge_id', 'bridge_sha256', 'logbrg',
    )


def _derived_from(source: CadLogQuantity, value_db: float,
                  label: str) -> CadDerivedLogValue:
    return CadDerivedLogValue(
        value_db=value_db,
        quantity_class=source.quantity_class,
        domain=source.domain,
        quantity=source.quantity,
        reference=source.reference,
        ratio_basis=source.ratio_basis,
        derivation_label=label,
    )


def evaluate_log_operation(
    *,
    document_id: str,
    operation: LogOperationKind,
    operands: tuple[CadLogQuantity, ...] | list[CadLogQuantity],
    bridges: tuple[CadCalibrationBridge, ...] | list[
        CadCalibrationBridge
    ] = (),
    target_domain: QuantityDomain | None = None,
    target_quantity: QuantityKind | None = None,
    linearity_state: LinearityState = 'undeclared',
    sum_kind: SumKind | None = None,
    mean_method: MeanMethod | None = None,
    evaluated_at_utc: str | None = None,
) -> CadLogOperation:
    """Fail-closed typed log-arithmetic verdict (#691 §13).

    - ``level_difference``/``compare`` require identical quantity
      identity — differing domain, quantity or reference is
      ``incompatible_quantities``.
    - ``apply_gain`` requires the gain operand to be a
      ``relative_gain_loss``/dimensionless quantity, the target an
      absolute level, and a declared linear chain: a limiter,
      compressor or speaker compression (#649) is
      ``nonlinear_chain``; an undeclared chain is ``unverified`` — the
      extrapolated level is not produced.
    - ``energetic_sum`` requires same-identity absolute levels and a
      declared ``sum_kind``; ``coherent_in_phase``/``coherent_sum``
      fails to ``coherent_requires_phase_data`` because dB-only
      operands carry no phase (#690).
    - ``arithmetic_mean`` requires an explicit declared method
      (``energetic_mean``/``declared_custom``).
    - ``convert`` requires a matching *active* calibration bridge —
      otherwise ``requires_calibration_bridge``; a scalar bridge yields
      the shifted target-domain value.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    operands = tuple(operands)
    if not operands:
        raise ValueError('an operation requires at least one operand')
    reasons: list[str] = []
    state: LogOperationState
    derived: CadDerivedLogValue | None = None
    bridge_ref: AuthorityRef | None = None

    def _same_identity() -> bool:
        first = operands[0]
        return all(o.same_quantity_identity(first) for o in operands[1:])

    if operation in ('level_difference', 'compare'):
        if len(operands) != 2:
            raise ValueError(
                f'{operation} requires exactly two operands'
            )
        if not _same_identity():
            state = 'incompatible_quantities'
            reasons.append(
                'the operands are not the same quantity identity — '
                'domain/quantity/reference all differ would produce a '
                'physically meaningless number'
            )
        else:
            a, b = operands
            assert a.value_db is not None and b.value_db is not None
            if operation == 'compare':
                state = 'compatible'
                derived = CadDerivedLogValue(
                    value_db=a.value_db - b.value_db,
                    quantity_class='relative_gain_loss',
                    domain='dimensionless',
                    quantity='level_difference',
                    ratio_basis=a.ratio_basis,
                    derivation_label='level difference (signed)',
                )
            else:
                state = 'compatible'
                derived = CadDerivedLogValue(
                    value_db=a.value_db - b.value_db,
                    quantity_class='relative_gain_loss',
                    domain='dimensionless',
                    quantity='level_difference',
                    ratio_basis=a.ratio_basis,
                    derivation_label='level difference',
                )

    elif operation == 'apply_gain':
        if len(operands) != 2:
            raise ValueError('apply_gain requires level + gain operands')
        level, gain = operands
        if gain.quantity_class != 'relative_gain_loss' and not (
            gain.quantity in ('gain_ratio', 'level_difference')
        ):
            raise ValueError(
                'the gain operand must be a relative gain/loss quantity '
                '— applying one absolute level to another is not a gain '
                'operation'
            )
        if level.quantity_class not in (
            'absolute_log_level', 'device_relative_setting'
        ):
            raise ValueError(
                'gain applies to an absolute level (or a device-relative '
                'setting in the same scale) — nothing else'
            )
        if linearity_state == 'nonlinear_or_dynamic':
            state = 'nonlinear_chain'
            reasons.append(
                'a limiter/compressor or loudspeaker compression in the '
                'chain breaks the fixed gain relation — +6 dB DSP is '
                'not +6 dB at the listener (#691 §7, #649)'
            )
        elif linearity_state != 'linear_chain_declared':
            state = 'unverified'
            reasons.append(
                'chain linearity is undeclared — the gain→level '
                'relation cannot be assumed linear without evidence'
            )
        else:
            assert level.value_db is not None
            assert gain.value_db is not None
            state = 'compatible'
            derived = _derived_from(
                level, level.value_db + gain.value_db,
                'gain applied under declared linear chain',
            )

    elif operation in ('energetic_sum', 'coherent_sum'):
        if len(operands) < 2:
            raise ValueError('a sum requires at least two operands')
        if any(o.quantity_class != 'absolute_log_level' for o in operands):
            raise ValueError(
                'a sum applies to absolute levels of the same quantity '
                'only'
            )
        if operation == 'coherent_sum' or (
            sum_kind == 'coherent_in_phase'
        ):
            state = 'coherent_requires_phase_data'
            reasons.append(
                'coherent combination is phasor addition — scalar dB '
                'levels carry no phase evidence (#690); energetic '
                'addition would be wrong physics'
            )
        elif sum_kind not in ('incoherent_independent',):
            state = 'unverified'
            reasons.append(
                'the sum kind is undeclared — incoherent energy '
                'addition is only valid for independent contributions'
            )
        elif not _same_identity():
            state = 'incompatible_quantities'
            reasons.append(
                'the operands are not the same quantity identity'
            )
        else:
            total = sum(
                10.0 ** (o.value_db / 10.0)  # type: ignore[operator]
                for o in operands
            )
            state = 'compatible'
            derived = _derived_from(
                operands[0], 10.0 * log10(total),
                'incoherent energetic sum',
            )

    elif operation == 'arithmetic_mean':
        if not _same_identity():
            state = 'incompatible_quantities'
            reasons.append('the operands are not the same quantity identity')
        elif mean_method is None:
            state = 'unverified'
            reasons.append(
                'a bare arithmetic mean of log values is not a declared '
                'method — energetic/declared custom semantics required'
            )
        elif mean_method == 'energetic_mean':
            if any(
                o.quantity_class != 'absolute_log_level' for o in operands
            ):
                raise ValueError(
                    'energetic_mean applies to absolute levels only'
                )
            total = sum(
                10.0 ** (o.value_db / 10.0)  # type: ignore[operator]
                for o in operands
            )
            state = 'compatible'
            derived = _derived_from(
                operands[0], 10.0 * log10(total / len(operands)),
                'energetic mean of like levels',
            )
        else:
            state = 'compatible_with_limitations'
            mean = sum(
                o.value_db for o in operands  # type: ignore[operator]
            ) / len(operands)
            derived = _derived_from(
                operands[0], mean,
                f'arithmetic mean under declared method '
                f'{mean_method}',
            )
            reasons.append(
                'custom-declared mean — the method\'s validity is the '
                'declarer\'s responsibility'
            )

    elif operation == 'convert':
        if len(operands) != 1:
            raise ValueError('convert requires exactly one operand')
        source = operands[0]
        if target_domain is None or target_quantity is None:
            raise ValueError(
                'convert requires explicit target domain + quantity'
            )
        bridge = next(
            (
                b for b in bridges
                if b.connects(source, target_domain, target_quantity)
            ),
            None,
        )
        if bridge is None:
            state = 'requires_calibration_bridge'
            reasons.append(
                f'no active bridge from {source.domain}/'
                f'{source.quantity} to {target_domain}/'
                f'{target_quantity} — a cross-domain conversion is a '
                'calibration-chain relation, not a unit conversion'
            )
        elif bridge.transfer_db is None:
            state = 'unverified'
            bridge_ref = calibration_bridge_binding(bridge)
            reasons.append(
                'the bridge declares a non-constant transfer — '
                'evaluation requires the transfer pipeline, not scalar '
                'arithmetic'
            )
        else:
            assert source.value_db is not None
            state = 'compatible'
            bridge_ref = calibration_bridge_binding(bridge)
            derived = CadDerivedLogValue(
                value_db=source.value_db + bridge.transfer_db,
                quantity_class='absolute_log_level',
                domain=bridge.to_domain,
                quantity=bridge.to_quantity,
                reference=bridge.to_reference,
                ratio_basis=source.ratio_basis,
                derivation_label=(
                    f'bridged via {bridge.bridge_label}'
                ),
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
        operand_refs=tuple(log_quantity_binding(o) for o in operands),
        state=state,
        derived=derived,
        bridge_ref=bridge_ref,
        linearity_state=(
            linearity_state if operation == 'apply_gain' else None
        ),
        sum_kind=(
            sum_kind
            if operation in ('energetic_sum', 'coherent_sum')
            else None
        ),
        mean_method=mean_method if operation == 'arithmetic_mean' else None,
        reasons=tuple(reasons),
        evaluation_version=LOGQ_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadLogOperation, payload,
        'operation_id', 'operation_sha256', 'logop',
    )


__all__ = [
    'CadBandSpec',
    'CadCalibrationBridge',
    'CadDerivedLogValue',
    'CadDeviceScale',
    'CadLogOperation',
    'CadLogQuantity',
    'CadLogReference',
    'DOMAIN_QUANTITIES',
    'LinearityState',
    'LOGQ_AUTHORITY_SCHEMA_VERSION',
    'LOGQ_EVALUATION_VERSION',
    'LogOperationKind',
    'LogOperationState',
    'MeanMethod',
    'QuantityClass',
    'QuantityDomain',
    'QuantityKind',
    'RatioBasis',
    'StandardBinding',
    'SumKind',
    'TimeIntegration',
    'WeightingKind',
    'build_calibration_bridge',
    'build_log_quantity',
    'calibration_bridge_binding',
    'evaluate_log_operation',
    'log_quantity_binding',
]
