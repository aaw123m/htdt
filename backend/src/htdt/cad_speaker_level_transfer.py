"""Speaker-level electrical path transfer authority (#1004).

Before acoustic headroom means anything, the *electrical* path between the
amplifier output and the loudspeaker terminals must be a recordable,
evaluable object: amplifier output impedance, the cable's series R/L and
shunt C, and the complex loudspeaker load all shape the terminal voltage
and current. Previously none of that existed — an amplifier's marketing
damping factor or a nominal loudspeaker impedance could be silently
treated as an exact transfer model.

This module keeps the three authorities separate and composable:

- :class:`AmplifierOutputImpedanceAuthority` records exactly what the
  amplifier's output impedance evidence is: ``unknown`` / ``scalar`` /
  ``damping_factor_derived`` (the DF figure keeps its exact reference
  load and reference frequency — it is not a universal constant) /
  ``magnitude_curve`` / ``complex_curve``.
- :class:`SpeakerCableElectricalProfile` records the cable's per-length
  electrical constants — distinct from :class:`~htdt.cad_cable_run.CableRun`,
  which records *where the cable physically goes*. The run supplies
  topology/length; this profile supplies the electrical model.
- :class:`SpeakerElectricalPath` binds amplifier + cable run + load
  topology into the actual connected path. Series/parallel driver
  combinations are only computable when the topology is declared —
  ``unknown`` topology is never silently treated as a single driver.
- :func:`evaluate_speaker_level_transfer` produces
  :class:`SpeakerLevelTransferResult` — per-frequency voltage transfer,
  terminal current, cable loss and response deviation. A nominal-only
  loudspeaker impedance cannot unlock an exact transfer: it yields
  ``not_computable``, not a fabricated curve.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite, log10, pi
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance, FrequencyDomain
from .cad_speaker_impedance import (
    ImpedanceInterpolation,
    ImpedanceSample,
    SpeakerElectricalImpedanceAuthority,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


SPEAKER_LEVEL_TRANSFER_AUTHORITY_VERSION = 'speaker-level-transfer-1'
AMPLIFIER_OUTPUT_IMPEDANCE_AUTHORITY_VERSION = 'amplifier-output-impedance-1'
CABLE_ELECTRICAL_AUTHORITY_VERSION = 'speaker-cable-electrical-1'

#: What kind of output-impedance evidence the amplifier authority carries.
AmplifierOutputImpedanceTier = Literal[
    'unknown',
    'scalar',
    'damping_factor_derived',
    'magnitude_curve',
    'complex_curve',
]

#: How the drivers on one path combine electrically. ``unknown`` means no
#: topology was declared — never treated as a single driver.
LoadTopology = Literal['single', 'series', 'parallel', 'unknown']

CableConductorMaterial = Literal[
    'copper', 'cca', 'silver', 'other', 'unknown'
]

TransferComputationStatus = Literal[
    'computed_complex', 'computed_magnitude', 'not_computable'
]






def _finite(value: object, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class AmplifierOutputImpedanceAuthority(BaseModel):
    """Evidence-tiered amplifier output impedance record.

    ``damping_factor_derived`` keeps the reference load and frequency it
    was published at — ``Z_out = Z_ref / DF`` is only exact at that point
    and is *not* extrapolated into a curve.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'amplifier-output-impedance-1'
    ] = AMPLIFIER_OUTPUT_IMPEDANCE_AUTHORITY_VERSION
    impedance_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    amplifier_ref: str = Field(min_length=1)
    tier: AmplifierOutputImpedanceTier
    #: Constant output impedance for the ``scalar`` tier.
    scalar_ohm: float | None = Field(default=None, ge=0.0)
    #: Damping-factor record for the ``damping_factor_derived`` tier.
    damping_factor: float | None = Field(default=None, gt=0.0)
    damping_factor_reference_load_ohm: float | None = Field(
        default=None, gt=0.0
    )
    damping_factor_reference_frequency_hz: float | None = Field(
        default=None, gt=0.0
    )
    samples: tuple[ImpedanceSample, ...] = ()
    interpolation: ImpedanceInterpolation | None = None
    valid_frequency_domain: FrequencyDomain | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_authority(self) -> 'AmplifierOutputImpedanceAuthority':
        if self.scalar_ohm is not None:
            _finite(self.scalar_ohm, field_name='scalar_ohm')
        if self.tier == 'scalar':
            if self.scalar_ohm is None:
                raise ValueError('scalar tier requires scalar_ohm')
        else:
            if self.scalar_ohm is not None:
                raise ValueError('scalar_ohm only valid on scalar tier')
        if self.tier == 'damping_factor_derived':
            if (
                self.damping_factor is None
                or self.damping_factor_reference_load_ohm is None
                or self.damping_factor_reference_frequency_hz is None
            ):
                raise ValueError(
                    'damping_factor_derived requires damping factor plus '
                    'its reference load and reference frequency'
                )
        else:
            if self.damping_factor is not None:
                raise ValueError(
                    'damping factor only valid on damping_factor_derived '
                    'tier'
                )
        curve_tiers = ('magnitude_curve', 'complex_curve')
        if self.tier in curve_tiers:
            if len(self.samples) < 2:
                raise ValueError(
                    f'{self.tier} requires at least two samples'
                )
            if self.interpolation is None:
                raise ValueError('impedance curve requires interpolation')
            frequencies = [s.frequency_hz for s in self.samples]
            if len(set(frequencies)) != len(frequencies):
                raise ValueError('sample frequencies must be unique')
            if self.tier == 'complex_curve' and not all(
                s.has_complex_representation for s in self.samples
            ):
                raise ValueError(
                    'complex_curve samples require a complex representation'
                )
            if self.tier == 'magnitude_curve' and not all(
                s.is_magnitude_only for s in self.samples
            ):
                raise ValueError(
                    'magnitude_curve samples carry magnitude only'
                )
        else:
            if self.samples:
                raise ValueError(f'{self.tier} does not carry samples')
            if self.interpolation is not None:
                raise ValueError('interpolation requires a curve tier')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'AmplifierOutputImpedanceAuthority semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'impedance_id': self.impedance_id,
            'version': self.version,
            'amplifier_ref': self.amplifier_ref,
            'tier': self.tier,
            'scalar_ohm': self.scalar_ohm,
            'damping_factor': self.damping_factor,
            'damping_factor_reference_load_ohm':
                self.damping_factor_reference_load_ohm,
            'damping_factor_reference_frequency_hz':
                self.damping_factor_reference_frequency_hz,
            'samples': [s.model_dump(mode='json') for s in self.samples],
            'interpolation': self.interpolation,
            'valid_frequency_domain': (
                self.valid_frequency_domain.model_dump(mode='json')
                if self.valid_frequency_domain is not None
                else None
            ),
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }

    @property
    def supports_complex_transfer(self) -> bool:
        return self.tier in ('scalar', 'complex_curve')

    def complex_at(self, frequency_hz: float) -> complex | None:
        """Complex output impedance at *frequency_hz* where supported."""
        if self.tier == 'scalar':
            assert self.scalar_ohm is not None
            return complex(self.scalar_ohm, 0.0)
        if self.tier == 'complex_curve':
            return _interpolated_complex(
                self.samples, frequency_hz, self.interpolation
            )
        return None

    def magnitude_at(self, frequency_hz: float) -> float | None:
        """Scalar output impedance estimate at *frequency_hz*.

        ``damping_factor_derived`` resolves only to its declared reference
        frequency — the DF figure is exact there, not a broadband constant.
        """
        value = _finite(frequency_hz, field_name='frequency_hz')
        if self.tier == 'scalar':
            return self.scalar_ohm
        if self.tier == 'damping_factor_derived':
            assert (
                self.damping_factor is not None
                and self.damping_factor_reference_load_ohm is not None
                and self.damping_factor_reference_frequency_hz is not None
            )
            if frequency_hz != self.damping_factor_reference_frequency_hz:
                return None
            return (
                self.damping_factor_reference_load_ohm / self.damping_factor
            )
        if self.tier == 'complex_curve':
            value = _interpolated_complex(
                self.samples, frequency_hz, self.interpolation
            )
            return abs(value) if value is not None else None
        if self.tier == 'magnitude_curve':
            return _interpolated_magnitude(
                self.samples, frequency_hz, self.interpolation
            )
        return None


class SpeakerCableElectricalProfile(BaseModel):
    """Per-length electrical constants of a speaker cable.

    Distinct from :class:`~htdt.cad_cable_run.CableRun`, which records the
    physical route. Constants are per conductor metre; the loop resistance
    the signal sees is the round trip (go and return conductors).
    Missing constants stay ``None`` — a cable with only resistance is a
    resistance model, not a zero-L/zero-C claim.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'speaker-cable-electrical-1'
    ] = CABLE_ELECTRICAL_AUTHORITY_VERSION
    cable_profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    material: CableConductorMaterial = 'unknown'
    conductor_gauge: str | None = Field(default=None, min_length=1)
    cross_section_mm2: float | None = Field(default=None, gt=0.0)
    series_resistance_ohm_per_m: float | None = Field(default=None, ge=0.0)
    series_inductance_h_per_m: float | None = Field(default=None, ge=0.0)
    shunt_capacitance_f_per_m: float | None = Field(default=None, ge=0.0)
    valid_frequency_domain: FrequencyDomain | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'SpeakerCableElectricalProfile':
        if self.series_resistance_ohm_per_m is None:
            raise ValueError(
                'a cable electrical profile requires at least its series '
                'resistance per metre'
            )
        for name in (
            'series_resistance_ohm_per_m',
            'series_inductance_h_per_m',
            'shunt_capacitance_f_per_m',
        ):
            value = getattr(self, name)
            if value is not None:
                _finite(value, field_name=name)
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'SpeakerCableElectricalProfile semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'cable_profile_id': self.cable_profile_id,
            'version': self.version,
            'material': self.material,
            'conductor_gauge': self.conductor_gauge,
            'cross_section_mm2': self.cross_section_mm2,
            'series_resistance_ohm_per_m': self.series_resistance_ohm_per_m,
            'series_inductance_h_per_m': self.series_inductance_h_per_m,
            'shunt_capacitance_f_per_m': self.shunt_capacitance_f_per_m,
            'valid_frequency_domain': (
                self.valid_frequency_domain.model_dump(mode='json')
                if self.valid_frequency_domain is not None
                else None
            ),
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }


class SpeakerElectricalPath(BaseModel):
    """The actual connected electrical path: amplifier → cable → load(s).

    ``load_topology`` is declared, never inferred: 'single' binds exactly
    one load impedance, 'series'/'parallel' combine the listed load ids in
    that topology, and 'unknown' records that no topology was declared.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'speaker-level-transfer-1'
    ] = SPEAKER_LEVEL_TRANSFER_AUTHORITY_VERSION
    path_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    amplifier_impedance_id: str = Field(min_length=1)
    cable_profile_id: str = Field(min_length=1)
    cable_run_id: str | None = Field(default=None, min_length=1)
    cable_length_m: float = Field(gt=0.0)
    load_topology: LoadTopology = 'unknown'
    load_impedance_ids: tuple[str, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_path(self) -> 'SpeakerElectricalPath':
        _finite(self.cable_length_m, field_name='cable_length_m')
        if self.load_topology == 'single' and len(
            self.load_impedance_ids
        ) != 1:
            raise ValueError(
                "load_topology 'single' requires exactly one load impedance"
            )
        if self.load_topology in ('series', 'parallel') and len(
            self.load_impedance_ids
        ) < 2:
            raise ValueError(
                f"load_topology '{self.load_topology}' requires at least "
                'two load impedances'
            )
        if self.load_topology == 'unknown' and self.load_impedance_ids:
            raise ValueError(
                'unknown topology cannot carry declared load ids'
            )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('SpeakerElectricalPath semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'path_id': self.path_id,
            'version': self.version,
            'amplifier_impedance_id': self.amplifier_impedance_id,
            'cable_profile_id': self.cable_profile_id,
            'cable_run_id': self.cable_run_id,
            'cable_length_m': self.cable_length_m,
            'load_topology': self.load_topology,
            'load_impedance_ids': list(self.load_impedance_ids),
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }


def build_amplifier_output_impedance(
    *,
    impedance_id: str | None = None,
    version: str = '1',
    amplifier_ref: str,
    tier: AmplifierOutputImpedanceTier = 'unknown',
    scalar_ohm: float | None = None,
    damping_factor: float | None = None,
    damping_factor_reference_load_ohm: float | None = None,
    damping_factor_reference_frequency_hz: float | None = None,
    samples: tuple[ImpedanceSample, ...] = (),
    interpolation: ImpedanceInterpolation | None = None,
    valid_frequency_domain: FrequencyDomain | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> AmplifierOutputImpedanceAuthority:
    payload: dict[str, Any] = {
        'authority_version': AMPLIFIER_OUTPUT_IMPEDANCE_AUTHORITY_VERSION,
        'impedance_id': impedance_id or str(uuid4()),
        'version': version,
        'amplifier_ref': amplifier_ref,
        'tier': tier,
        'scalar_ohm': scalar_ohm,
        'damping_factor': damping_factor,
        'damping_factor_reference_load_ohm': damping_factor_reference_load_ohm,
        'damping_factor_reference_frequency_hz':
            damping_factor_reference_frequency_hz,
        'samples': samples,
        'interpolation': interpolation,
        'valid_frequency_domain': valid_frequency_domain,
        'provenance': provenance,
    }
    provisional = AmplifierOutputImpedanceAuthority.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return AmplifierOutputImpedanceAuthority(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


def build_speaker_cable_electrical_profile(
    *,
    cable_profile_id: str | None = None,
    version: str = '1',
    material: CableConductorMaterial = 'unknown',
    conductor_gauge: str | None = None,
    cross_section_mm2: float | None = None,
    series_resistance_ohm_per_m: float,
    series_inductance_h_per_m: float | None = None,
    shunt_capacitance_f_per_m: float | None = None,
    valid_frequency_domain: FrequencyDomain | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> SpeakerCableElectricalProfile:
    payload: dict[str, Any] = {
        'authority_version': CABLE_ELECTRICAL_AUTHORITY_VERSION,
        'cable_profile_id': cable_profile_id or str(uuid4()),
        'version': version,
        'material': material,
        'conductor_gauge': conductor_gauge,
        'cross_section_mm2': cross_section_mm2,
        'series_resistance_ohm_per_m': series_resistance_ohm_per_m,
        'series_inductance_h_per_m': series_inductance_h_per_m,
        'shunt_capacitance_f_per_m': shunt_capacitance_f_per_m,
        'valid_frequency_domain': valid_frequency_domain,
        'provenance': provenance,
    }
    provisional = SpeakerCableElectricalProfile.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return SpeakerCableElectricalProfile(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


def build_speaker_electrical_path(
    *,
    path_id: str | None = None,
    version: str = '1',
    amplifier_impedance_id: str,
    cable_profile_id: str,
    cable_length_m: float,
    cable_run_id: str | None = None,
    load_topology: LoadTopology = 'unknown',
    load_impedance_ids: tuple[str, ...] = (),
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> SpeakerElectricalPath:
    payload: dict[str, Any] = {
        'authority_version': SPEAKER_LEVEL_TRANSFER_AUTHORITY_VERSION,
        'path_id': path_id or str(uuid4()),
        'version': version,
        'amplifier_impedance_id': amplifier_impedance_id,
        'cable_profile_id': cable_profile_id,
        'cable_run_id': cable_run_id,
        'cable_length_m': cable_length_m,
        'load_topology': load_topology,
        'load_impedance_ids': load_impedance_ids,
        'provenance': provenance,
    }
    provisional = SpeakerElectricalPath.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return SpeakerElectricalPath(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


def _nearest_sample(
    ordered: Sequence[ImpedanceSample], frequency_hz: float
) -> ImpedanceSample:
    """In-band nearest-frequency lookup; ties resolve to the lower sample."""
    return min(
        ordered, key=lambda sample: abs(sample.frequency_hz - frequency_hz)
    )


def _interpolated_complex(
    samples: tuple[ImpedanceSample, ...],
    frequency_hz: float,
    interpolation: ImpedanceInterpolation | None = None,
) -> complex | None:
    """Complex curve lookup honoring the declared interpolation policy."""
    ordered = sorted(samples, key=lambda sample: sample.frequency_hz)
    if frequency_hz < ordered[0].frequency_hz or (
        frequency_hz > ordered[-1].frequency_hz
    ):
        return None
    if interpolation == 'nearest':
        sample = _nearest_sample(ordered, frequency_hz)
        return complex(sample.real(), sample.imag())
    previous = ordered[0]
    for sample in ordered:
        if sample.frequency_hz >= frequency_hz:
            if sample.frequency_hz == previous.frequency_hz:
                return complex(sample.real(), sample.imag())
            ratio = (frequency_hz - previous.frequency_hz) / (
                sample.frequency_hz - previous.frequency_hz
            )
            return complex(
                previous.real() + ratio * (sample.real() - previous.real()),
                previous.imag() + ratio * (sample.imag() - previous.imag()),
            )
        previous = sample
    return complex(ordered[-1].real(), ordered[-1].imag())


def _interpolated_magnitude(
    samples: tuple[ImpedanceSample, ...],
    frequency_hz: float,
    interpolation: ImpedanceInterpolation | None = None,
) -> float | None:
    ordered = sorted(samples, key=lambda sample: sample.frequency_hz)
    if frequency_hz < ordered[0].frequency_hz or (
        frequency_hz > ordered[-1].frequency_hz
    ):
        return None
    if interpolation == 'nearest':
        return _nearest_sample(ordered, frequency_hz).magnitude()
    previous = ordered[0]
    for sample in ordered:
        if sample.frequency_hz >= frequency_hz:
            if sample.frequency_hz == previous.frequency_hz:
                return sample.magnitude()
            ratio = (frequency_hz - previous.frequency_hz) / (
                sample.frequency_hz - previous.frequency_hz
            )
            return previous.magnitude() + ratio * (
                sample.magnitude() - previous.magnitude()
            )
        previous = sample
    return ordered[-1].magnitude()


class TransferPoint(BaseModel):
    """One frequency's computed electrical transfer."""

    model_config = ConfigDict(frozen=True)

    frequency_hz: float = Field(gt=0.0)
    #: Complex V_load / V_source ratio (None when magnitude-only).
    voltage_transfer_real: float | None = None
    voltage_transfer_imag: float | None = None
    voltage_transfer_db: float
    terminal_current_a: float
    cable_loss_db: float
    response_deviation_db: float


class SpeakerLevelTransferResult(BaseModel):
    """Computed electrical transfer for one path over a frequency set.

    ``status`` records what was computable:

    - ``computed_complex``: amplifier, cable and load evidence all support
      complex (phase-aware) transfer;
    - ``computed_magnitude``: magnitude-only evidence — voltage/current
      figures are magnitudes and carry no phase;
    - ``not_computable``: at least one input lacked the evidence to compute
      anything (see ``reasons``) — nominal-only load impedance and
      undeclared topology land here, never as a fabricated curve.
    """

    model_config = ConfigDict(frozen=True)

    result_id: str = Field(min_length=1)
    path_id: str
    path_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    amplifier_impedance_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    cable_profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    load_impedance_sha256: tuple[str, ...] = ()
    status: TransferComputationStatus
    points: tuple[TransferPoint, ...] = ()
    reasons: tuple[str, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_result(self) -> 'SpeakerLevelTransferResult':
        if self.status == 'not_computable' and not self.reasons:
            raise ValueError('not_computable results require reasons')
        if self.status == 'not_computable' and self.points:
            raise ValueError('not_computable results carry no points')
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('SpeakerLevelTransferResult hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': SPEAKER_LEVEL_TRANSFER_AUTHORITY_VERSION,
            'result_id': self.result_id,
            'path_id': self.path_id,
            'path_sha256': self.path_sha256,
            'amplifier_impedance_sha256': self.amplifier_impedance_sha256,
            'cable_profile_sha256': self.cable_profile_sha256,
            'load_impedance_sha256': list(self.load_impedance_sha256),
            'status': self.status,
            'points': [p.model_dump(mode='json') for p in self.points],
            'reasons': list(self.reasons),
        }


def _load_impedance_at(
    load: SpeakerElectricalImpedanceAuthority, frequency_hz: float
) -> complex | float | None:
    """Load value at *frequency_hz*: complex when the tier supports it,
    magnitude otherwise, None when the tier has no curve/nominal to use.
    """
    if load.tier == 'complex_curve':
        ordered = sorted(load.samples, key=lambda s: s.frequency_hz)
        if frequency_hz < ordered[0].frequency_hz or (
            frequency_hz > ordered[-1].frequency_hz
        ):
            return None
        if load.interpolation == 'nearest':
            sample = _nearest_sample(ordered, frequency_hz)
            return complex(sample.real(), sample.imag())
        previous = ordered[0]
        for sample in ordered:
            if sample.frequency_hz >= frequency_hz:
                if sample.frequency_hz == previous.frequency_hz:
                    return complex(sample.real(), sample.imag())
                ratio = (frequency_hz - previous.frequency_hz) / (
                    sample.frequency_hz - previous.frequency_hz
                )
                return complex(
                    previous.real()
                    + ratio * (sample.real() - previous.real()),
                    previous.imag()
                    + ratio * (sample.imag() - previous.imag()),
                )
            previous = sample
        return complex(ordered[-1].real(), ordered[-1].imag())
    magnitude = load.magnitude_at(frequency_hz)
    if magnitude is not None:
        return magnitude
    if load.tier == 'minimum_impedance':
        # Nominal + declared minimum: the nominal is the design value, not
        # an exact frequency-dependent load — magnitude-only, constant.
        return load.nominal_impedance_ohm
    return None


def evaluate_speaker_level_transfer(
    *,
    path: SpeakerElectricalPath,
    amplifier_impedance: AmplifierOutputImpedanceAuthority,
    cable_profile: SpeakerCableElectricalProfile,
    load_impedances: tuple[SpeakerElectricalImpedanceAuthority, ...],
    frequencies_hz: tuple[float, ...],
    source_voltage_v: float = 1.0,
) -> SpeakerLevelTransferResult:
    """Compute the terminal transfer of the connected path.

    Fail-closed rules:

    - ``load_topology`` must be declared ('single'/'series'/'parallel') and
      every listed load id must resolve — else ``not_computable``;
    - a load authority with only a nominal rating cannot produce an exact
      transfer — 'single' topology against ``nominal_impedance_only``
      yields ``not_computable`` with a reason;
    - ``computed_complex`` requires complex amplifier output impedance and
      complex load curve(s); otherwise the result is magnitude-only.
    """

    reasons: list[str] = []
    load_by_id = {load.impedance_id: load for load in load_impedances}
    resolved_loads: list[SpeakerElectricalImpedanceAuthority] = []
    if path.load_topology == 'unknown':
        reasons.append('load topology not declared')
    elif path.load_topology == 'single' and not path.load_impedance_ids:
        reasons.append('single load topology requires one load impedance')
    else:
        for load_id in path.load_impedance_ids:
            load = load_by_id.get(load_id)
            if load is None:
                reasons.append(f'load impedance unresolved: {load_id}')
            else:
                resolved_loads.append(load)
    if not resolved_loads and not reasons:
        reasons.append('no load impedances resolved')

    if amplifier_impedance.tier == 'unknown':
        reasons.append('amplifier output impedance evidence is unknown')

    if reasons:
        return _transfer_result(
            path=path,
            amplifier_impedance=amplifier_impedance,
            cable_profile=cable_profile,
            load_impedances=resolved_loads,
            status='not_computable',
            points=(),
            reasons=tuple(reasons),
        )

    complex_path = amplifier_impedance.supports_complex_transfer and all(
        load.tier == 'complex_curve' for load in resolved_loads
    )
    magnitude_only = any(
        load.tier in ('nominal_impedance_only', 'minimum_impedance')
        for load in resolved_loads
    )
    nominal_only = all(
        load.tier == 'nominal_impedance_only' for load in resolved_loads
    )

    cable_r_m = cable_profile.series_resistance_ohm_per_m
    cable_l_m = cable_profile.series_inductance_h_per_m
    cable_c_m = cable_profile.shunt_capacitance_f_per_m
    assert cable_r_m is not None
    loop_m = 2.0 * path.cable_length_m  # round trip: go + return conductor

    points: list[TransferPoint] = []
    skipped: list[str] = []
    for frequency_hz in sorted(frequencies_hz):
        f = _finite(frequency_hz, field_name='frequency_hz')
        z_amp = (
            amplifier_impedance.complex_at(f)
            if complex_path
            else amplifier_impedance.magnitude_at(f)
        )
        if z_amp is None:
            skipped.append(
                f'{f:g}Hz: amplifier impedance not defined at this frequency'
            )
            continue

        omega = 2.0 * pi * f
        cable_series = complex(
            cable_r_m * loop_m,
            (cable_l_m or 0.0) * loop_m * omega,
        )
        # Load values per topology.
        load_values: list[complex] = []
        missing_load = False
        for load in resolved_loads:
            value = _load_impedance_at(load, f)
            if value is None:
                missing_load = True
                break
            load_values.append(
                value if isinstance(value, complex) else complex(value, 0.0)
            )
        if missing_load:
            skipped.append(
                f'{f:g}Hz: load impedance undefined at this frequency'
            )
            continue

        if path.load_topology == 'parallel':
            admittance = sum(
                (1.0 / value for value in load_values), complex(0.0)
            )
            if abs(admittance) == 0.0:
                skipped.append(f'{f:g}Hz: parallel load is an open circuit')
                continue
            z_load = 1.0 / admittance
        elif path.load_topology == 'series':
            z_load = sum(load_values, complex(0.0))
        else:
            z_load = load_values[0]

        # Optional shunt capacitance across the load terminals.
        if cable_c_m:
            y_shunt = complex(0.0, omega * cable_c_m * loop_m)
            z_load = 1.0 / (1.0 / z_load + y_shunt)

        z_total = complex(z_amp) + cable_series + z_load
        if abs(z_total) == 0.0:
            skipped.append(f'{f:g}Hz: total impedance is zero')
            continue
        transfer = z_load / z_total
        current = source_voltage_v / abs(z_total)
        # Cable loss: load voltage vs. what the load would see with an
        # ideal (zero-impedance) cable — response deviation vs the
        # amplifier-only path.
        z_total_ideal = complex(z_amp) + z_load
        transfer_ideal = (
            z_load / z_total_ideal if abs(z_total_ideal) else None
        )
        deviation_db = 0.0
        if transfer_ideal is not None and abs(transfer_ideal) > 0.0:
            deviation_db = 20.0 * log10(
                abs(transfer) / abs(transfer_ideal)
            )
        loss_db = (
            20.0 * log10(abs(z_load / (cable_series + z_load)))
            if abs(cable_series + z_load) > 0
            else 0.0
        )

        if complex_path:
            transfer_real = transfer.real
            transfer_imag = transfer.imag
        else:
            transfer_real = None
            transfer_imag = None

        points.append(
            TransferPoint(
                frequency_hz=f,
                voltage_transfer_real=transfer_real,
                voltage_transfer_imag=transfer_imag,
                voltage_transfer_db=20.0 * log10(max(abs(transfer), 1e-12)),
                terminal_current_a=abs(current),
                cable_loss_db=loss_db,
                response_deviation_db=deviation_db,
            )
        )

    if nominal_only:
        # A nominal rating is a nameplate figure — never an exact curve.
        return _transfer_result(
            path=path,
            amplifier_impedance=amplifier_impedance,
            cable_profile=cable_profile,
            load_impedances=resolved_loads,
            status='not_computable',
            points=(),
            reasons=(
                'nominal-only load impedance cannot produce an exact '
                'transfer — a frequency-resolved load authority is required',
            ),
        )
    if not points and not skipped:
        skipped.append('no frequencies supplied')
    status: TransferComputationStatus = (
        'computed_complex' if complex_path else 'computed_magnitude'
    )
    if not points:
        status = 'not_computable'
    return _transfer_result(
        path=path,
        amplifier_impedance=amplifier_impedance,
        cable_profile=cable_profile,
        load_impedances=resolved_loads,
        status=status,
        points=tuple(points),
        reasons=tuple(reasons) + tuple(skipped),
    )


def _transfer_result(
    *,
    path: SpeakerElectricalPath,
    amplifier_impedance: AmplifierOutputImpedanceAuthority,
    cable_profile: SpeakerCableElectricalProfile,
    load_impedances: list[SpeakerElectricalImpedanceAuthority],
    status: TransferComputationStatus,
    points: tuple[TransferPoint, ...],
    reasons: tuple[str, ...],
) -> SpeakerLevelTransferResult:
    payload: dict[str, Any] = {
        'result_id': str(uuid4()),
        'path_id': path.path_id,
        'path_sha256': path.semantic_sha256,
        'amplifier_impedance_sha256': amplifier_impedance.semantic_sha256,
        'cable_profile_sha256': cable_profile.semantic_sha256,
        'load_impedance_sha256': tuple(
            load.semantic_sha256 for load in load_impedances
        ),
        'status': status,
        'points': points,
        'reasons': reasons,
    }
    provisional = SpeakerLevelTransferResult.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return SpeakerLevelTransferResult(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


__all__ = [
    'AMPLIFIER_OUTPUT_IMPEDANCE_AUTHORITY_VERSION',
    'AmplifierOutputImpedanceAuthority',
    'AmplifierOutputImpedanceTier',
    'CABLE_ELECTRICAL_AUTHORITY_VERSION',
    'CableConductorMaterial',
    'LoadTopology',
    'SPEAKER_LEVEL_TRANSFER_AUTHORITY_VERSION',
    'SpeakerCableElectricalProfile',
    'SpeakerElectricalPath',
    'SpeakerLevelTransferResult',
    'TransferComputationStatus',
    'TransferPoint',
    'build_amplifier_output_impedance',
    'build_speaker_cable_electrical_profile',
    'build_speaker_electrical_path',
    'evaluate_speaker_level_transfer',
]
