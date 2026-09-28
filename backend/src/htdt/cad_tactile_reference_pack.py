"""Tactile actuator reference pack (#1079).

Curated manufacturer-published data for common tactile transducers
(bass shakers) — electrical ratings, physical/mounting data — for
binding into ``cad_tactile`` actuator instances.

Rules:

- every record carries ``manufacturer`` provenance pinned to the
  published datasheet/manual — fields the datasheet leaves blank stay
  ``None`` (never interpolated from a sibling model);
- a reference record describes the *device*, never an installed
  seat response: there is no seat-transfer, acceleration or
  "felt strength" field — measured seat response is user evidence,
  produced by commissioning measurement, not curated here;
- electrical ratings are nominal published values; impedance is the
  nominal voice-coil impedance.
"""

from __future__ import annotations

from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash, canonicalize_payload


TACTILE_PACK_AUTHORITY_VERSION = 'tactile-reference-1'






ActuatorMounting = Literal[
    'surface_mount',
    'bracket',
    'flush_mount',
    'adhesive',
    'other',
]


class TactileActuatorReference(BaseModel):
    """Manufacturer-published data for one tactile actuator model."""

    model_config = ConfigDict(frozen=True)

    reference_id: str = Field(min_length=1)
    manufacturer: str = Field(min_length=1)
    model: str = Field(min_length=1)
    nominal_impedance_ohm: float | None = Field(default=None, gt=0.0)
    power_min_w: float | None = Field(default=None, ge=0.0)
    power_max_w: float | None = Field(default=None, gt=0.0)
    power_rms_w: float | None = Field(default=None, gt=0.0)
    frequency_response_hz: tuple[float, float] | None = None
    resonant_frequency_hz: float | None = Field(default=None, gt=0.0)
    voice_coil_dc_resistance_ohm: float | None = Field(
        default=None, gt=0.0
    )
    voice_coil_inductance_mh: float | None = Field(default=None, gt=0.0)
    dimensions_mm: tuple[float, ...] | None = None
    weight_g: float | None = Field(default=None, gt=0.0)
    mounting: ActuatorMounting | None = None
    mounting_detail: str | None = None
    datasheet_uri: str = Field(min_length=1)
    datasheet_title: str = Field(min_length=1)
    notes: str = ''
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def _check(self) -> 'TactileActuatorReference':
        if self.frequency_response_hz is not None:
            lo, hi = self.frequency_response_hz
            if not (0.0 < lo < hi):
                raise ValueError(
                    'frequency response band must be increasing'
                )
        if (
            self.power_min_w is not None
            and self.power_max_w is not None
            and self.power_max_w < self.power_min_w
        ):
            raise ValueError('power_max must not be below power_min')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'tactile reference semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )


def build_tactile_reference(**kwargs: Any) -> TactileActuatorReference:
    probe = TactileActuatorReference.model_construct(**canonicalize_payload(TactileActuatorReference, dict(
        semantic_sha256='', **kwargs
    )))
    return TactileActuatorReference(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


# --- curated records (datasheet-verified fields only) --------------------

BUTTKICKER_LFE_REFERENCE: TactileActuatorReference = (
    build_tactile_reference(
        reference_id='buttkicker-lfe',
        manufacturer='ButtKicker (The Guitammer Company)',
        model='ButtKicker LFE (BK-LFE)',
        nominal_impedance_ohm=4.0,
        power_min_w=400.0,
        power_max_w=1500.0,
        frequency_response_hz=(5.0, 200.0),
        dimensions_mm=(136.5, 139.7),
        weight_g=5000.0,
        mounting='surface_mount',
        mounting_detail=(
            'Mounts to topside of a surface or inverted underneath; '
            'red terminal is + (positive voltage pushes toward feet).'
        ),
        datasheet_uri='https://thebuttkicker.com/products/'
        'buttkicker-lfe-haptic-transducer',
        datasheet_title='ButtKicker LFE product page / BK manual',
        notes=(
            'Passive 4-ohm inductive load; internal thermal limit '
            '150 F (70 C). Piston weight 3.75 lb. Requires dedicated '
            'power amplifier — never wired as a speaker.'
        ),
    )
)

DAYTON_BST300EX_REFERENCE: TactileActuatorReference = (
    build_tactile_reference(
        reference_id='dayton-bst-300ex',
        manufacturer='Dayton Audio',
        model='BST-300EX Extreme High Power Pro Tactile Bass Shaker',
        nominal_impedance_ohm=4.0,
        power_rms_w=300.0,
        weight_g=1700.0,
        mounting='surface_mount',
        mounting_detail=(
            'Series wiring of two raises impedance to 8 ohms; parallel '
            'to 2 ohms. Manufacturer suggests an 80 Hz low-pass '
            'crossover.'
        ),
        datasheet_uri='https://www.daytonaudio.com/images/resources/'
        '295-243--dayton-audio-bst-300ex-user-manual.pdf',
        datasheet_title='BST-300EX high power bass shaker manual '
        '(295-243)',
        notes='High-grade aluminum housing; 300 W RMS per unit.',
    )
)

DAYTON_TT25_16_REFERENCE: TactileActuatorReference = (
    build_tactile_reference(
        reference_id='dayton-tt25-16',
        manufacturer='Dayton Audio',
        model='TT25-16 PUCK tactile transducer',
        nominal_impedance_ohm=16.0,
        power_rms_w=15.0,
        power_max_w=30.0,
        frequency_response_hz=(20.0, 80.0),
        resonant_frequency_hz=40.0,
        voice_coil_dc_resistance_ohm=14.9,
        voice_coil_inductance_mh=2.93,
        dimensions_mm=(88.9, 25.4),
        mounting='surface_mount',
        mounting_detail=(
            'Six #6 wood/sheet-metal screws; 70 mm cutout with 25 mm '
            'wire pass-through slot.'
        ),
        datasheet_uri='https://daytonaudio.com/product/1036/'
        'tt25-16-puck-tactile-transducer-mini-bass-shaker',
        datasheet_title='TT25-16 PUCK product page',
        notes=(
            '4-layer voice coil on 1" former; usable response 20-80 Hz '
            'per manufacturer.'
        ),
    )
)

TACTILE_REFERENCE_PACK: tuple[TactileActuatorReference, ...] = (
    BUTTKICKER_LFE_REFERENCE,
    DAYTON_BST300EX_REFERENCE,
    DAYTON_TT25_16_REFERENCE,
)


class ActuatorLookup(NamedTuple):
    found: bool
    reference: TactileActuatorReference | None
    detail: str


def lookup_tactile_reference(reference_id: str) -> ActuatorLookup:
    for r in TACTILE_REFERENCE_PACK:
        if r.reference_id == reference_id:
            return ActuatorLookup(True, r, 'found')
    return ActuatorLookup(False, None, 'reference not in pack')


def electrical_load_bounds(reference: TactileActuatorReference) -> (
    dict[str, float] | None
):
    """Published electrical envelope: nominal impedance + power bounds.

    ``None`` fields inside the dict are absent only when the datasheet
    doesn't publish them — never estimated.
    """
    if reference.nominal_impedance_ohm is None:
        return None
    return {
        'nominal_impedance_ohm': reference.nominal_impedance_ohm,
        **(
            {'power_min_w': reference.power_min_w}
            if reference.power_min_w is not None
            else {}
        ),
        **(
            {'power_max_w': reference.power_max_w}
            if reference.power_max_w is not None
            else {}
        ),
        **(
            {'power_rms_w': reference.power_rms_w}
            if reference.power_rms_w is not None
            else {}
        ),
    }


__all__ = [
    'ActuatorLookup',
    'ActuatorMounting',
    'BUTTKICKER_LFE_REFERENCE',
    'DAYTON_BST300EX_REFERENCE',
    'DAYTON_TT25_16_REFERENCE',
    'TACTILE_PACK_AUTHORITY_VERSION',
    'TACTILE_REFERENCE_PACK',
    'TactileActuatorReference',
    'build_tactile_reference',
    'electrical_load_bounds',
    'lookup_tactile_reference',
]
