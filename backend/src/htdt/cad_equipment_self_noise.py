"""Equipment acoustic self-noise authority (#1011 / ENOISE10).

Projectors, PCs, AVRs and rack gear emit acoustic noise that varies with
operating state — a fan curve is not a single dB figure. This module
records what equipment self-noise *is*: the quantity being claimed
(sound power, sound pressure at a reference point, octave bands), the
operating state it was recorded under, and the evidence provenance —
without ever conflating a manufacturer "24 dB" marketing figure with a
measured in-room spectrum.

Fail-closed contract:

- a manufacturer-declared overall figure stays
  ``manufacturer_declared_overall`` — it is never transformed into a
  spectrum, never treated as measured;
- an omni-directional approximation is labelled ``omni_approximation`` —
  it is not measured directivity;
- no spectrum is fabricated from a single dBA number;
- sources combine by energy (10·log10 of summed powers), never by
  summing dB values;
- listener impact is bounded: free-field spreading from a source level,
  with room contribution explicitly UNKNOWN.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite, log10
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus


EQUIPMENT_NOISE_AUTHORITY_VERSION = 'equipment-noise-1'

#: What the noise figure physically is. These are not interchangeable —
#: a declared overall rating is never rewritten as a measured spectrum.
EquipmentNoiseQuantity = Literal[
    'sound_power_level',
    'sound_pressure_at_reference_point',
    'octave_band_sound_power',
    'octave_band_sound_pressure',
    'manufacturer_declared_overall',
    'measured_installed_source',
    'unknown',
]

#: How the level is expressed — needed before any two figures may be
#: combined or compared.
EquipmentNoiseWeighting = Literal['A', 'C', 'Z', 'flat', 'unknown']

#: Equipment operating state binding — mandatory, since fan speed is
#: state-dependent.
EquipmentNoiseOperatingState = Literal[
    'powered_off',
    'standby',
    'idle',
    'typical_load',
    'high_load',
    'vendor_defined',
    'unknown',
]


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _finite(value: object, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class EquipmentNoiseBand(BaseModel):
    """One octave/third-octave band level in a noise record."""

    model_config = ConfigDict(frozen=True)

    band_center_hz: float = Field(gt=0.0)
    band_level_db: float
    band_kind: Literal['octave', 'third_octave', 'other'] = 'octave'

    @model_validator(mode='after')
    def finite_band(self) -> 'EquipmentNoiseBand':
        _finite(self.band_center_hz, field_name='band_center_hz')
        _finite(self.band_level_db, field_name='band_level_db')
        return self


class EquipmentAcousticNoiseProfile(BaseModel):
    """Self-noise record for one device in one operating state.

    ``quantity`` declares what the figure is (power vs pressure vs
    declared overall). ``radiation_model='omni_approximation'`` is an
    explicit label — omni is an assumption, not measured directivity.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'equipment-noise-1'
    ] = EQUIPMENT_NOISE_AUTHORITY_VERSION
    noise_profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    equipment_id: str = Field(min_length=1)
    quantity: EquipmentNoiseQuantity = 'unknown'
    operating_state: EquipmentNoiseOperatingState = 'unknown'
    weighting: EquipmentNoiseWeighting = 'unknown'
    overall_level_db: float | None = None
    bands: tuple[EquipmentNoiseBand, ...] = ()
    #: For pressure quantities: distance from the device's acoustic
    #: reference point where the pressure was recorded.
    reference_distance_m: float | None = Field(default=None, gt=0.0)
    radiation_model: Literal[
        'measured_directivity', 'omni_approximation', 'unknown'
    ] = 'unknown'
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'EquipmentAcousticNoiseProfile':
        if self.overall_level_db is not None:
            _finite(self.overall_level_db, field_name='overall_level_db')
        if self.reference_distance_m is not None:
            _finite(
                self.reference_distance_m,
                field_name='reference_distance_m',
            )
        band_quantity = self.quantity in (
            'octave_band_sound_power',
            'octave_band_sound_pressure',
        )
        if band_quantity and not self.bands:
            raise ValueError(
                'octave-band quantity requires at least one band level'
            )
        pressure = self.quantity in (
            'sound_pressure_at_reference_point',
            'octave_band_sound_pressure',
        )
        if pressure and self.reference_distance_m is None:
            raise ValueError(
                'sound-pressure quantity requires a reference distance'
            )
        centers = [b.band_center_hz for b in self.bands]
        if len(set(centers)) != len(centers):
            raise ValueError('duplicate noise band centers')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'EquipmentAcousticNoiseProfile semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'noise_profile_id': self.noise_profile_id,
            'version': self.version,
            'equipment_id': self.equipment_id,
            'quantity': self.quantity,
            'operating_state': self.operating_state,
            'weighting': self.weighting,
            'overall_level_db': self.overall_level_db,
            'bands': [
                band.model_dump(mode='json') for band in self.bands
            ],
            'reference_distance_m': self.reference_distance_m,
            'radiation_model': self.radiation_model,
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }


def build_equipment_noise_profile(
    *,
    noise_profile_id: str | None = None,
    version: str = '1',
    equipment_id: str,
    quantity: EquipmentNoiseQuantity = 'unknown',
    operating_state: EquipmentNoiseOperatingState = 'unknown',
    weighting: EquipmentNoiseWeighting = 'unknown',
    overall_level_db: float | None = None,
    bands: tuple[EquipmentNoiseBand, ...] = (),
    reference_distance_m: float | None = None,
    radiation_model: Literal[
        'measured_directivity', 'omni_approximation', 'unknown'
    ] = 'unknown',
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> EquipmentAcousticNoiseProfile:
    payload: dict[str, Any] = {
        'authority_version': EQUIPMENT_NOISE_AUTHORITY_VERSION,
        'noise_profile_id': noise_profile_id or str(uuid4()),
        'version': version,
        'equipment_id': equipment_id,
        'quantity': quantity,
        'operating_state': operating_state,
        'weighting': weighting,
        'overall_level_db': overall_level_db,
        'bands': bands,
        'reference_distance_m': reference_distance_m,
        'radiation_model': radiation_model,
        'provenance': provenance,
    }
    provisional = EquipmentAcousticNoiseProfile.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return EquipmentAcousticNoiseProfile(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


def combine_noise_sources_energy(
    levels_db: tuple[float, ...],
) -> float | None:
    """Combine uncorrelated source levels by energy summation.

    ``10 * log10(sum(10 ** (L / 10)))`` — never a direct dB sum. Returns
    ``None`` for an empty input rather than a fabricated level.
    """

    if not levels_db:
        return None
    total = sum(10.0 ** (level / 10.0) for level in levels_db)
    return 10.0 * log10(total)


def predict_free_field_listener_level(
    *, source_level_db: float, reference_distance_m: float,
    listener_distance_m: float,
) -> float:
    """Predict the listener level under free-field spreading.

    ``L_listener = L_source - 20 * log10(d_listener / d_ref)``.

    This bounds the *direct* contribution only — room reflections,
    modal gain and background noise are UNKNOWN here.
    """

    _finite(source_level_db, field_name='source_level_db')
    _finite(reference_distance_m, field_name='reference_distance_m')
    _finite(listener_distance_m, field_name='listener_distance_m')
    if reference_distance_m <= 0.0 or listener_distance_m <= 0.0:
        raise ValueError('distances must be positive')
    return source_level_db - 20.0 * log10(
        listener_distance_m / reference_distance_m
    )


class EquipmentNoiseCheck(BaseModel):
    """One readiness check on a self-noise profile."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str


class EquipmentNoiseReadinessReport(BaseModel):
    """Readiness of one equipment self-noise profile for listener-impact
    analysis — the profile's identity plus per-aspect checks."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1)
    noise_profile_id: str
    profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    checks: tuple[EquipmentNoiseCheck, ...]
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_report(self) -> 'EquipmentNoiseReadinessReport':
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('noise readiness report hash mismatch')
        expected = 'enr-' + self.report_sha256[:24]
        if self.report_id != expected:
            raise ValueError('noise readiness report id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': EQUIPMENT_NOISE_AUTHORITY_VERSION,
            'noise_profile_id': self.noise_profile_id,
            'profile_sha256': self.profile_sha256,
            'checks': [
                check.model_dump(mode='json') for check in self.checks
            ],
        }


def evaluate_equipment_noise_readiness(
    *, profile: EquipmentAcousticNoiseProfile
) -> EquipmentNoiseReadinessReport:
    """Report what a self-noise profile can support.

    - ``operating_state_bound``: a real operating state is recorded —
      self-noise is meaningless without it;
    - ``quantity_declared``: the figure's physical quantity is known —
      ``unknown`` means the number cannot be interpreted;
    - ``level_recorded``: an overall level or band set exists;
    - ``manufacturer_figure_contained``: a
      ``manufacturer_declared_overall`` figure is flagged — it supports
      a bounded estimate only and is never promoted to measured data.
    """

    checks: list[EquipmentNoiseCheck] = []

    checks.append(
        EquipmentNoiseCheck(
            check='operating_state_bound',
            status=(
                'PASS'
                if profile.operating_state not in ('unknown',)
                else 'UNKNOWN'
            ),
            reason=(
                f'operating state: {profile.operating_state}'
                if profile.operating_state != 'unknown'
                else 'no operating state recorded — self-noise is '
                'state-dependent'
            ),
        )
    )

    checks.append(
        EquipmentNoiseCheck(
            check='quantity_declared',
            status=(
                'PASS' if profile.quantity != 'unknown' else 'UNKNOWN'
            ),
            reason=(
                f'noise quantity: {profile.quantity}'
                if profile.quantity != 'unknown'
                else 'noise quantity unknown — level cannot be '
                'interpreted'
            ),
        )
    )

    has_level = (
        profile.overall_level_db is not None or len(profile.bands) > 0
    )
    checks.append(
        EquipmentNoiseCheck(
            check='level_recorded',
            status='PASS' if has_level else 'UNKNOWN',
            reason=(
                'overall level or bands recorded'
                if has_level
                else 'no level evidence recorded'
            ),
        )
    )

    if profile.quantity == 'manufacturer_declared_overall':
        checks.append(
            EquipmentNoiseCheck(
                check='manufacturer_figure_contained',
                status='UNKNOWN',
                reason='manufacturer-declared overall figure — usable '
                'only as a bounded estimate, never transformed into a '
                'spectrum or measured level',
            )
        )
    else:
        checks.append(
            EquipmentNoiseCheck(
                check='manufacturer_figure_contained',
                status='NOT_APPLICABLE',
                reason='not a manufacturer-declared overall figure',
            )
        )

    if profile.radiation_model == 'omni_approximation':
        checks.append(
            EquipmentNoiseCheck(
                check='directivity_labelled',
                status='UNKNOWN',
                reason='omni-directional approximation — not measured '
                'directivity',
            )
        )
    elif profile.radiation_model == 'unknown':
        checks.append(
            EquipmentNoiseCheck(
                check='directivity_labelled',
                status='UNKNOWN',
                reason='radiation model unrecorded',
            )
        )
    else:
        checks.append(
            EquipmentNoiseCheck(
                check='directivity_labelled',
                status='PASS',
                reason='measured directivity bound',
            )
        )

    probe = EquipmentNoiseReadinessReport.model_construct(
        report_id='',
        noise_profile_id=profile.noise_profile_id,
        profile_sha256=profile.semantic_sha256,
        checks=tuple(checks),
        report_sha256='',
    )
    digest = _hash(probe.identity_payload())
    return EquipmentNoiseReadinessReport(
        **probe.model_dump(
            mode='python',
            exclude={'report_sha256', 'report_id'},
        ),
        report_id='enr-' + digest[:24],
        report_sha256=digest,
    )


__all__ = [
    'EQUIPMENT_NOISE_AUTHORITY_VERSION',
    'EquipmentAcousticNoiseProfile',
    'EquipmentNoiseBand',
    'EquipmentNoiseCheck',
    'EquipmentNoiseOperatingState',
    'EquipmentNoiseQuantity',
    'EquipmentNoiseReadinessReport',
    'EquipmentNoiseWeighting',
    'build_equipment_noise_profile',
    'combine_noise_sources_energy',
    'evaluate_equipment_noise_readiness',
    'predict_free_field_listener_level',
]
