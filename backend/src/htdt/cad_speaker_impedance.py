"""Frequency-dependent speaker electrical load authority (#544).

Extends the scalar ``SpeakerElectricalLoadAuthority`` with evidence-tiered
impedance: nominal-only, declared minimum, magnitude-only curves and complex
curves are stored with explicit semantics so amplifier/headroom evaluation can
go beyond one resistive reference — and so that nominal impedance is never
promoted to an exact load model.

Evaluation is frequency-resolved: for a required RMS drive voltage the current
demand is derived per impedance sample over the requested band, the minimum
magnitude point is compared against the amplifier's evidenced load domain, and
optional ``AmplifierElectricalLimitAuthority`` ceilings add voltage/current
margins. Missing evidence reports 'missing'/'unsupported', never a guessed
number.
"""

from __future__ import annotations

from math import cos, isfinite, log10, pi, sin, sqrt
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_amplifier_headroom import (
    AmplifierChannelCountCondition,
    AmplifierOutputCapability,
    AuthorityRef,
    PlaybackChainScalarResult,
)
from .cad_direct_level import DirectLevelFrequencyBand
from .cad_equipment import (
    EquipmentDataProvenance,
    EquipmentDefinition,
    FrequencyDomain,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


SPEAKER_IMPEDANCE_SCHEMA_VERSION = 1
SPEAKER_IMPEDANCE_AUTHORITY_VERSION = 'speaker-electrical-impedance-1'
AMPLIFIER_ELECTRICAL_LIMIT_AUTHORITY_VERSION = (
    'amplifier-electrical-limit-1'
)
FREQUENCY_RESOLVED_EVALUATION_VERSION = (
    'frequency-resolved-electrical-evaluation-1'
)

ImpedanceTier = Literal[
    'nominal_impedance_only',
    'minimum_impedance',
    'magnitude_only_curve',
    'complex_curve',
    'exact_resistive_reference',
]
ImpedanceInterpolation = Literal['linear', 'nearest']
LoadDomainCheck = Literal[
    'within_evidenced_domain',
    'below_evidenced_minimum',
    'above_evidenced_maximum',
    'unavailable',
]
FrequencyResolvedLimiter = Literal[
    'voltage', 'current', 'load_domain', 'equal', 'unknown'
]
FrequencyResolvedState = Literal['available', 'partial', 'unsupported']






def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class ImpedanceSample(BaseModel):
    """One measured/declared impedance point.

    Either a complex representation (``real_ohm`` + ``imag_ohm``) or a
    magnitude representation (``magnitude_ohm``, ``phase_deg`` optional) must
    be supplied — never both.
    """

    model_config = ConfigDict(frozen=True)

    frequency_hz: float = Field(gt=0.0)
    magnitude_ohm: float | None = Field(default=None, gt=0.0)
    phase_deg: float | None = None
    real_ohm: float | None = None
    imag_ohm: float | None = None

    @field_validator(
        'frequency_hz', 'magnitude_ohm', 'phase_deg', 'real_ohm', 'imag_ohm'
    )
    @classmethod
    def finite_sample(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='impedance sample')

    @model_validator(mode='after')
    def valid_representation(self) -> 'ImpedanceSample':
        complex_parts = (self.real_ohm is not None) + (self.imag_ohm is not None)
        magnitude_parts = (self.magnitude_ohm is not None) + (
            self.phase_deg is not None
        )
        if complex_parts == 1:
            raise ValueError(
                'complex impedance sample requires real_ohm and imag_ohm'
            )
        if complex_parts and magnitude_parts:
            raise ValueError(
                'impedance sample must use either complex or magnitude '
                'representation, not both'
            )
        if not complex_parts and not magnitude_parts:
            raise ValueError(
                'impedance sample requires magnitude_ohm or complex parts'
            )
        return self

    @property
    def is_complex(self) -> bool:
        return self.real_ohm is not None

    @property
    def has_complex_representation(self) -> bool:
        """Whether the sample carries a full complex value (any encoding)."""
        return self.is_complex or (
            self.magnitude_ohm is not None and self.phase_deg is not None
        )

    @property
    def is_magnitude_only(self) -> bool:
        """Whether the sample carries magnitude without any phase data."""
        return self.magnitude_ohm is not None and self.phase_deg is None

    def magnitude(self) -> float:
        if self.magnitude_ohm is not None:
            return float(self.magnitude_ohm)
        assert self.real_ohm is not None and self.imag_ohm is not None
        return sqrt(self.real_ohm**2 + self.imag_ohm**2)

    def real(self) -> float:
        if self.real_ohm is not None:
            return float(self.real_ohm)
        assert self.magnitude_ohm is not None
        phase = self.phase_deg or 0.0
        return self.magnitude_ohm * cos(phase * pi / 180.0)

    def imag(self) -> float:
        if self.imag_ohm is not None:
            return float(self.imag_ohm)
        assert self.magnitude_ohm is not None
        phase = self.phase_deg or 0.0
        return self.magnitude_ohm * sin(phase * pi / 180.0)


class SpeakerElectricalImpedanceAuthority(BaseModel):
    """Evidence-tiered, frequency-dependent speaker load authority.

    ``tier`` records exactly what was evidenced:

    - ``nominal_impedance_only``: rated nominal impedance, no curve and no
      guaranteed minimum — never used as an exact load model.
    - ``minimum_impedance``: nominal plus a declared minimum and its
      frequency; enables a bounded amplifier-load check without a curve.
    - ``magnitude_only_curve``: |Z|(f) samples; phase unknown.
    - ``complex_curve``: real/imag or magnitude+phase samples.
    - ``exact_resistive_reference``: a scalar resistive reference usable for
      exact V <-> W conversion (parity with the O100d load authority).
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SPEAKER_IMPEDANCE_SCHEMA_VERSION
    authority_version: Literal[
        'speaker-electrical-impedance-1'
    ] = SPEAKER_IMPEDANCE_AUTHORITY_VERSION
    impedance_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    tier: ImpedanceTier
    nominal_impedance_ohm: float = Field(gt=0.0)
    minimum_impedance_ohm: float | None = Field(default=None, gt=0.0)
    minimum_frequency_hz: float | None = Field(default=None, gt=0.0)
    samples: tuple[ImpedanceSample, ...] = ()
    interpolation: ImpedanceInterpolation | None = None
    valid_frequency_domain: FrequencyDomain
    provenance: EquipmentDataProvenance
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('nominal_impedance_ohm')
    @classmethod
    def finite_nominal(cls, value: float) -> float:
        return _finite(value, field_name='nominal impedance')

    @model_validator(mode='after')
    def valid_authority(self) -> 'SpeakerElectricalImpedanceAuthority':
        if (self.minimum_impedance_ohm is None) != (
            self.minimum_frequency_hz is None
        ):
            raise ValueError(
                'minimum impedance and its frequency must be supplied together'
            )
        curve_tiers = {'magnitude_only_curve', 'complex_curve'}
        if self.tier in curve_tiers:
            if len(self.samples) < 2:
                raise ValueError(
                    f'{self.tier} requires at least two impedance samples'
                )
            frequencies = [sample.frequency_hz for sample in self.samples]
            if len(set(frequencies)) != len(frequencies):
                raise ValueError('impedance sample frequencies must be unique')
            if tuple(self.samples) != tuple(
                sorted(self.samples, key=lambda sample: sample.frequency_hz)
            ):
                raise ValueError(
                    'impedance samples must be sorted by ascending frequency'
                )
            if self.interpolation is None:
                raise ValueError('impedance curve requires interpolation policy')
            for sample in self.samples:
                if not self.valid_frequency_domain.contains(sample.frequency_hz):
                    raise ValueError(
                        'impedance sample outside valid frequency domain'
                    )
                if (
                    self.tier == 'complex_curve'
                    and not sample.has_complex_representation
                ):
                    raise ValueError(
                        'complex_curve tier requires complex samples '
                        '(real/imag or magnitude+phase)'
                    )
                if (
                    self.tier == 'magnitude_only_curve'
                    and not sample.is_magnitude_only
                ):
                    raise ValueError(
                        'magnitude_only_curve samples carry magnitude only'
                    )
        else:
            if self.samples:
                raise ValueError(
                    f'{self.tier} does not carry impedance samples'
                )
            if self.interpolation is not None:
                raise ValueError(
                    'interpolation policy requires a sampled curve tier'
                )
        if self.tier in ('minimum_impedance', 'magnitude_only_curve', 'complex_curve'):
            if self.minimum_impedance_ohm is None:
                raise ValueError(
                    f'{self.tier} requires a declared minimum impedance'
                )
        if self.tier == 'nominal_impedance_only' and (
            self.minimum_impedance_ohm is not None
        ):
            raise ValueError(
                'nominal_impedance_only does not carry a guaranteed minimum'
            )
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError(
                'SpeakerElectricalImpedanceAuthority semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'impedance_id': self.impedance_id,
            'version': self.version,
            'equipment_definition_id': self.equipment_definition_id,
            'equipment_definition_version': self.equipment_definition_version,
            'equipment_definition_sha256': self.equipment_definition_sha256,
            'tier': self.tier,
            'nominal_impedance_ohm': self.nominal_impedance_ohm,
            'minimum_impedance_ohm': self.minimum_impedance_ohm,
            'minimum_frequency_hz': self.minimum_frequency_hz,
            'samples': [
                sample.model_dump(mode='json') for sample in self.samples
            ],
            'interpolation': self.interpolation,
            'valid_frequency_domain': self.valid_frequency_domain.model_dump(
                mode='json'
            ),
            'provenance': self.provenance.model_dump(mode='json'),
        }

    @property
    def is_curve(self) -> bool:
        return self.tier in ('magnitude_only_curve', 'complex_curve')

    def magnitude_at(self, frequency_hz: float) -> float | None:
        """Interpolated |Z| at *frequency_hz*; None outside the sample span."""
        if not self.samples:
            return None
        value = _finite(frequency_hz, field_name='frequency_hz')
        ordered = sorted(self.samples, key=lambda sample: sample.frequency_hz)
        if value < ordered[0].frequency_hz or value > ordered[-1].frequency_hz:
            return None
        if self.interpolation == 'nearest':
            nearest = min(
                ordered,
                key=lambda sample: abs(sample.frequency_hz - value),
            )
            return nearest.magnitude()
        previous = ordered[0]
        for sample in ordered:
            if sample.frequency_hz >= value:
                if sample.frequency_hz == previous.frequency_hz:
                    return sample.magnitude()
                span = sample.frequency_hz - previous.frequency_hz
                ratio = (value - previous.frequency_hz) / span
                return previous.magnitude() + ratio * (
                    sample.magnitude() - previous.magnitude()
                )
            previous = sample
        return ordered[-1].magnitude()


def build_speaker_impedance_authority(
    *,
    impedance_id: str,
    version: str,
    equipment_definition: EquipmentDefinition,
    tier: ImpedanceTier,
    nominal_impedance_ohm: float,
    valid_frequency_domain: FrequencyDomain,
    provenance: EquipmentDataProvenance,
    minimum_impedance_ohm: float | None = None,
    minimum_frequency_hz: float | None = None,
    samples: Sequence[ImpedanceSample] = (),
    interpolation: ImpedanceInterpolation | None = None,
) -> SpeakerElectricalImpedanceAuthority:
    sample_items = tuple(samples)
    payload = {
        'schema_version': SPEAKER_IMPEDANCE_SCHEMA_VERSION,
        'authority_version': SPEAKER_IMPEDANCE_AUTHORITY_VERSION,
        'impedance_id': impedance_id,
        'version': version,
        'equipment_definition_id': equipment_definition.definition_id,
        'equipment_definition_version': equipment_definition.version,
        'equipment_definition_sha256': equipment_definition.semantic_sha256,
        'tier': tier,
        'nominal_impedance_ohm': float(nominal_impedance_ohm),
        'minimum_impedance_ohm': minimum_impedance_ohm,
        'minimum_frequency_hz': minimum_frequency_hz,
        'samples': [sample.model_dump(mode='json') for sample in sample_items],
        'interpolation': interpolation,
        'valid_frequency_domain': valid_frequency_domain.model_dump(mode='json'),
        'provenance': provenance.model_dump(mode='json'),
    }
    return SpeakerElectricalImpedanceAuthority(
        impedance_id=impedance_id,
        version=version,
        equipment_definition_id=equipment_definition.definition_id,
        equipment_definition_version=equipment_definition.version,
        equipment_definition_sha256=equipment_definition.semantic_sha256,
        tier=tier,
        nominal_impedance_ohm=nominal_impedance_ohm,
        minimum_impedance_ohm=minimum_impedance_ohm,
        minimum_frequency_hz=minimum_frequency_hz,
        samples=sample_items,
        interpolation=interpolation,
        valid_frequency_domain=valid_frequency_domain,
        provenance=provenance,
        semantic_sha256=_digest(payload),
    )


class AmplifierElectricalLimitAuthority(BaseModel):
    """Additional evidenced amplifier ceilings beyond the O100d capability.

    The base capability already carries continuous/peak V or W ceilings at a
    declared load domain and a simultaneous-channel condition; this authority
    adds optional per-frequency-usable ceilings (voltage, current, minimum
    load magnitude) bound to an exact capability.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SPEAKER_IMPEDANCE_SCHEMA_VERSION
    authority_version: Literal[
        'amplifier-electrical-limit-1'
    ] = AMPLIFIER_ELECTRICAL_LIMIT_AUTHORITY_VERSION
    limit_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    amplifier_capability: AuthorityRef
    rms_voltage_ceiling_v: float | None = Field(default=None, gt=0.0)
    peak_voltage_ceiling_v: float | None = Field(default=None, gt=0.0)
    rms_current_ceiling_a: float | None = Field(default=None, gt=0.0)
    peak_current_ceiling_a: float | None = Field(default=None, gt=0.0)
    minimum_magnitude_load_ohm: float | None = Field(default=None, gt=0.0)
    channel_count_condition: AmplifierChannelCountCondition
    valid_frequency_domain: FrequencyDomain
    provenance: EquipmentDataProvenance
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator(
        'rms_voltage_ceiling_v',
        'peak_voltage_ceiling_v',
        'rms_current_ceiling_a',
        'peak_current_ceiling_a',
        'minimum_magnitude_load_ohm',
    )
    @classmethod
    def finite_limit(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='amplifier electrical limit')

    @model_validator(mode='after')
    def valid_limit(self) -> 'AmplifierElectricalLimitAuthority':
        if all(
            ceiling is None
            for ceiling in (
                self.rms_voltage_ceiling_v,
                self.peak_voltage_ceiling_v,
                self.rms_current_ceiling_a,
                self.peak_current_ceiling_a,
                self.minimum_magnitude_load_ohm,
            )
        ):
            raise ValueError(
                'amplifier electrical limit requires at least one ceiling'
            )
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError(
                'AmplifierElectricalLimitAuthority semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'limit_id': self.limit_id,
            'version': self.version,
            'amplifier_capability': self.amplifier_capability.model_dump(
                mode='json'
            ),
            'rms_voltage_ceiling_v': self.rms_voltage_ceiling_v,
            'peak_voltage_ceiling_v': self.peak_voltage_ceiling_v,
            'rms_current_ceiling_a': self.rms_current_ceiling_a,
            'peak_current_ceiling_a': self.peak_current_ceiling_a,
            'minimum_magnitude_load_ohm': self.minimum_magnitude_load_ohm,
            'channel_count_condition': self.channel_count_condition.model_dump(
                mode='json'
            ),
            'valid_frequency_domain': self.valid_frequency_domain.model_dump(
                mode='json'
            ),
            'provenance': self.provenance.model_dump(mode='json'),
        }


def build_amplifier_electrical_limit(
    *,
    limit_id: str,
    version: str,
    amplifier_capability: AmplifierOutputCapability,
    channel_count_condition: AmplifierChannelCountCondition,
    valid_frequency_domain: FrequencyDomain,
    provenance: EquipmentDataProvenance,
    rms_voltage_ceiling_v: float | None = None,
    peak_voltage_ceiling_v: float | None = None,
    rms_current_ceiling_a: float | None = None,
    peak_current_ceiling_a: float | None = None,
    minimum_magnitude_load_ohm: float | None = None,
) -> AmplifierElectricalLimitAuthority:
    capability_ref = AuthorityRef(
        authority_id=amplifier_capability.capability_id,
        version=amplifier_capability.version,
        semantic_sha256=amplifier_capability.semantic_sha256,
    )
    payload = {
        'schema_version': SPEAKER_IMPEDANCE_SCHEMA_VERSION,
        'authority_version': AMPLIFIER_ELECTRICAL_LIMIT_AUTHORITY_VERSION,
        'limit_id': limit_id,
        'version': version,
        'amplifier_capability': capability_ref.model_dump(mode='json'),
        'rms_voltage_ceiling_v': rms_voltage_ceiling_v,
        'peak_voltage_ceiling_v': peak_voltage_ceiling_v,
        'rms_current_ceiling_a': rms_current_ceiling_a,
        'peak_current_ceiling_a': peak_current_ceiling_a,
        'minimum_magnitude_load_ohm': minimum_magnitude_load_ohm,
        'channel_count_condition': channel_count_condition.model_dump(
            mode='json'
        ),
        'valid_frequency_domain': valid_frequency_domain.model_dump(
            mode='json'
        ),
        'provenance': provenance.model_dump(mode='json'),
    }
    return AmplifierElectricalLimitAuthority(
        limit_id=limit_id,
        version=version,
        amplifier_capability=capability_ref,
        rms_voltage_ceiling_v=rms_voltage_ceiling_v,
        peak_voltage_ceiling_v=peak_voltage_ceiling_v,
        rms_current_ceiling_a=rms_current_ceiling_a,
        peak_current_ceiling_a=peak_current_ceiling_a,
        minimum_magnitude_load_ohm=minimum_magnitude_load_ohm,
        channel_count_condition=channel_count_condition,
        valid_frequency_domain=valid_frequency_domain,
        provenance=provenance,
        semantic_sha256=_digest(payload),
    )


def _scalar(
    state: Literal['available', 'missing', 'unsupported'],
    *,
    value: float | None,
    unit: Literal['ohm', 'A', 'V RMS', 'dB'],
    reason: str | None = None,
) -> PlaybackChainScalarResult:
    return PlaybackChainScalarResult(
        state=state,
        value=value,
        unit=unit,
        reason=reason,
    )


class FrequencyResolvedElectricalEvaluation(BaseModel):
    """Per-band load-domain / current-demand / ceiling evaluation evidence."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SPEAKER_IMPEDANCE_SCHEMA_VERSION
    authority_version: Literal[
        'frequency-resolved-electrical-evaluation-1'
    ] = FREQUENCY_RESOLVED_EVALUATION_VERSION
    impedance_ref: AuthorityRef
    amplifier_capability: AuthorityRef
    amplifier_limit: AuthorityRef | None = None
    frequency_band: DirectLevelFrequencyBand
    required_voltage_v_rms: float = Field(gt=0.0)
    minimum_magnitude_ohm: PlaybackChainScalarResult
    minimum_magnitude_frequency_hz: float | None = None
    worst_current_demand_a: PlaybackChainScalarResult
    worst_current_frequency_hz: float | None = None
    current_headroom_db: PlaybackChainScalarResult
    voltage_margin_v: PlaybackChainScalarResult
    load_domain_check: LoadDomainCheck
    continuous_limiter: FrequencyResolvedLimiter
    state: FrequencyResolvedState
    support_reasons: tuple[str, ...]
    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'FrequencyResolvedElectricalEvaluation':
        digest = _digest(self.identity_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError(
                'FrequencyResolvedElectricalEvaluation semantic hash mismatch'
            )
        if self.evaluation_id != _semantic_id('freq-load', digest):
            raise ValueError(
                'FrequencyResolvedElectricalEvaluation ID does not match '
                'semantic hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'impedance_ref': self.impedance_ref.model_dump(mode='json'),
            'amplifier_capability': self.amplifier_capability.model_dump(
                mode='json'
            ),
            'amplifier_limit': (
                None
                if self.amplifier_limit is None
                else self.amplifier_limit.model_dump(mode='json')
            ),
            'frequency_band': self.frequency_band.model_dump(mode='json'),
            'required_voltage_v_rms': self.required_voltage_v_rms,
            'minimum_magnitude_ohm': self.minimum_magnitude_ohm.model_dump(
                mode='json'
            ),
            'minimum_magnitude_frequency_hz': self.minimum_magnitude_frequency_hz,
            'worst_current_demand_a': self.worst_current_demand_a.model_dump(
                mode='json'
            ),
            'worst_current_frequency_hz': self.worst_current_frequency_hz,
            'current_headroom_db': self.current_headroom_db.model_dump(
                mode='json'
            ),
            'voltage_margin_v': self.voltage_margin_v.model_dump(mode='json'),
            'load_domain_check': self.load_domain_check,
            'continuous_limiter': self.continuous_limiter,
            'state': self.state,
            'support_reasons': list(self.support_reasons),
        }


def _authority_ref(authority_id: str, version: str, sha: str) -> AuthorityRef:
    return AuthorityRef(
        authority_id=authority_id,
        version=version,
        semantic_sha256=sha,
    )


def evaluate_frequency_resolved_load(
    *,
    impedance: SpeakerElectricalImpedanceAuthority,
    amplifier_capability: AmplifierOutputCapability,
    frequency_band: DirectLevelFrequencyBand,
    required_voltage_v_rms: float,
    amplifier_limit: AmplifierElectricalLimitAuthority | None = None,
) -> FrequencyResolvedElectricalEvaluation:
    """Evaluate one evidence-tiered impedance against amplifier authority.

    The required RMS drive voltage is applied flat across the band (the
    caller owns any SPL-to-voltage derivation upstream); per-sample current
    demand is ``V / |Z|(f)``. Checks that need unavailable evidence report
    'missing'/'unsupported' with an exact reason — a nominal-only or
    magnitude-only load is never silently treated as an exact model.
    """
    required_voltage = _finite(
        required_voltage_v_rms, field_name='required_voltage_v_rms'
    )
    if required_voltage <= 0.0:
        raise ValueError('required_voltage_v_rms must be > 0')
    if amplifier_limit is not None and (
        amplifier_limit.amplifier_capability.authority_id
        != amplifier_capability.capability_id
        or amplifier_limit.amplifier_capability.version
        != amplifier_capability.version
        or amplifier_limit.amplifier_capability.semantic_sha256
        != amplifier_capability.semantic_sha256
    ):
        raise ValueError(
            'amplifier electrical limit does not reference the supplied '
            'capability exactly'
        )

    impedance_ref = _authority_ref(
        impedance.impedance_id, impedance.version, impedance.semantic_sha256
    )
    capability_ref = _authority_ref(
        amplifier_capability.capability_id,
        amplifier_capability.version,
        amplifier_capability.semantic_sha256,
    )
    limit_ref = (
        None
        if amplifier_limit is None
        else _authority_ref(
            amplifier_limit.limit_id,
            amplifier_limit.version,
            amplifier_limit.semantic_sha256,
        )
    )

    reasons: list[str] = []
    state: FrequencyResolvedState = 'available'
    limiter: FrequencyResolvedLimiter = 'unknown'

    if not impedance.valid_frequency_domain.contains(
        frequency_band.low_hz
    ) or not impedance.valid_frequency_domain.contains(frequency_band.high_hz):
        reasons.append(
            'requested frequency band is outside the impedance authority '
            'domain'
        )
        state = 'unsupported'

    minimum_magnitude = _scalar(
        'missing',
        value=None,
        unit='ohm',
        reason='no impedance evidence resolves |Z| inside the band',
    )
    minimum_frequency: float | None = None
    worst_current = _scalar(
        'missing',
        value=None,
        unit='A',
        reason='no impedance evidence resolves current demand',
    )
    worst_frequency: float | None = None
    load_domain_check: LoadDomainCheck = 'unavailable'

    if state != 'unsupported':
        if impedance.is_curve:
            points: list[tuple[float, float]] = []
            for sample in impedance.samples:
                if sample.frequency_hz < frequency_band.low_hz:
                    continue
                if sample.frequency_hz > frequency_band.high_hz:
                    break
                points.append((sample.frequency_hz, sample.magnitude()))
            edges = (frequency_band.low_hz, frequency_band.high_hz)
            for edge in edges:
                magnitude = impedance.magnitude_at(edge)
                if magnitude is not None:
                    points.append((edge, magnitude))
            if len(points) < 2:
                reasons.append(
                    'impedance curve does not cover the requested band'
                )
                state = 'unsupported'
            else:
                if impedance.tier == 'magnitude_only_curve':
                    reasons.append(
                        'phase is unevidenced; current demand uses |Z| '
                        'magnitude only'
                    )
                minimum_frequency, min_ohm = min(
                    points, key=lambda item: item[1]
                )
                minimum_magnitude = _scalar(
                    'available', value=min_ohm, unit='ohm'
                )
                worst_frequency = minimum_frequency
                worst_current = _scalar(
                    'available',
                    value=required_voltage / min_ohm,
                    unit='A',
                )
                if minimum_impedance_out_of_domain(
                    amplifier_capability, min_ohm
                ):
                    load_domain_check = 'below_evidenced_minimum'
                    reasons.append(
                        'minimum |Z| inside the band is below the amplifier '
                        'evidenced minimum load'
                    )
                else:
                    load_domain_check = 'within_evidenced_domain'
        elif impedance.tier == 'minimum_impedance':
            assert impedance.minimum_impedance_ohm is not None
            assert impedance.minimum_frequency_hz is not None
            if impedance.valid_frequency_domain.contains(
                impedance.minimum_frequency_hz
            ) and (
                frequency_band.low_hz
                <= impedance.minimum_frequency_hz
                <= frequency_band.high_hz
            ):
                minimum_magnitude = _scalar(
                    'available',
                    value=impedance.minimum_impedance_ohm,
                    unit='ohm',
                )
                minimum_frequency = impedance.minimum_frequency_hz
                worst_current = _scalar(
                    'available',
                    value=required_voltage / impedance.minimum_impedance_ohm,
                    unit='A',
                )
                worst_frequency = impedance.minimum_frequency_hz
                if minimum_impedance_out_of_domain(
                    amplifier_capability, impedance.minimum_impedance_ohm
                ):
                    load_domain_check = 'below_evidenced_minimum'
                    reasons.append(
                        'declared minimum impedance is below the amplifier '
                        'evidenced minimum load'
                    )
                else:
                    load_domain_check = 'within_evidenced_domain'
            else:
                reasons.append(
                    'declared minimum impedance point lies outside the '
                    'requested band; band worst case is unevidenced'
                )
                state = 'partial'
        elif impedance.tier == 'exact_resistive_reference':
            resistance = impedance.nominal_impedance_ohm
            minimum_magnitude = _scalar(
                'available', value=resistance, unit='ohm'
            )
            worst_current = _scalar(
                'available', value=required_voltage / resistance, unit='A'
            )
            load_domain_check = (
                'within_evidenced_domain'
                if amplifier_capability.supported_load.contains(resistance)
                else (
                    'below_evidenced_minimum'
                    if resistance
                    < amplifier_capability.supported_load.minimum_load_ohm
                    else 'above_evidenced_maximum'
                )
            )
            if load_domain_check != 'within_evidenced_domain':
                reasons.append(
                    'resistive reference is outside the amplifier evidenced '
                    'load domain'
                )
        else:
            reasons.append(
                'nominal impedance only: |Z|(f) and its minimum are '
                'unevidenced; nominal impedance is not a load model'
            )
            state = 'partial'

    voltage_margin = _scalar(
        'missing',
        value=None,
        unit='V RMS',
        reason='amplifier voltage ceiling is unevidenced',
    )
    capability = amplifier_capability.continuous_capability
    if capability is not None and state != 'unsupported':
        if capability.quantity == 'voltage_v_rms':
            margin = float(capability.value) - required_voltage
            voltage_margin = _scalar('available', value=margin, unit='V RMS')
            if margin < 0:
                limiter = 'voltage'
                reasons.append(
                    'required drive voltage exceeds the evidenced '
                    'continuous voltage ceiling'
                )
        elif impedance.tier == 'exact_resistive_reference':
            ceiling_v = sqrt(
                float(capability.value) * impedance.nominal_impedance_ohm
            )
            margin = ceiling_v - required_voltage
            voltage_margin = _scalar('available', value=margin, unit='V RMS')
            if margin < 0:
                limiter = 'voltage'
                reasons.append(
                    'required drive voltage exceeds the evidenced power '
                    'ceiling converted at the exact resistive reference'
                )
        else:
            voltage_margin = _scalar(
                'unsupported',
                value=None,
                unit='V RMS',
                reason=(
                    'amplifier ceiling is evidenced in watts; V<->W '
                    'conversion requires an exact resistive reference load'
                ),
            )
            if state == 'available':
                state = 'partial'

    current_headroom = _scalar(
        'missing',
        value=None,
        unit='dB',
        reason='no amplifier current ceiling is evidenced',
    )
    if (
        amplifier_limit is not None
        and amplifier_limit.rms_current_ceiling_a is not None
        and worst_current.state == 'available'
        and worst_current.value is not None
    ):
        if amplifier_limit.valid_frequency_domain.contains(
            frequency_band.low_hz
        ) and amplifier_limit.valid_frequency_domain.contains(
            frequency_band.high_hz
        ):
            ratio = (
                amplifier_limit.rms_current_ceiling_a
                / float(worst_current.value)
            )
            headroom_db = 20.0 * log10(ratio)
            current_headroom = _scalar(
                'available', value=headroom_db, unit='dB'
            )
            if headroom_db < 0:
                limiter = 'current'
                reasons.append(
                    'worst-case current demand exceeds the evidenced RMS '
                    'current ceiling'
                )
        else:
            current_headroom = _scalar(
                'unsupported',
                value=None,
                unit='dB',
                reason=(
                    'amplifier current ceiling is unevidenced for the '
                    'requested band'
                ),
            )
            if state == 'available':
                state = 'partial'

    if load_domain_check in (
        'below_evidenced_minimum',
        'above_evidenced_maximum',
    ) and limiter == 'unknown':
        limiter = 'load_domain'
        state = 'unsupported'
    if limiter == 'unknown' and state == 'available':
        if (
            voltage_margin.state == 'available'
            and current_headroom.state == 'available'
            and voltage_margin.value is not None
            and current_headroom.value is not None
        ):
            if abs(voltage_margin.value) < 1e-9 and abs(
                current_headroom.value
            ) < 1e-9:
                limiter = 'equal'

    payload = {
        'schema_version': SPEAKER_IMPEDANCE_SCHEMA_VERSION,
        'authority_version': FREQUENCY_RESOLVED_EVALUATION_VERSION,
        'impedance_ref': impedance_ref.model_dump(mode='json'),
        'amplifier_capability': capability_ref.model_dump(mode='json'),
        'amplifier_limit': (
            None if limit_ref is None else limit_ref.model_dump(mode='json')
        ),
        'frequency_band': frequency_band.model_dump(mode='json'),
        'required_voltage_v_rms': required_voltage,
        'minimum_magnitude_ohm': minimum_magnitude.model_dump(mode='json'),
        'minimum_magnitude_frequency_hz': minimum_frequency,
        'worst_current_demand_a': worst_current.model_dump(mode='json'),
        'worst_current_frequency_hz': worst_frequency,
        'current_headroom_db': current_headroom.model_dump(mode='json'),
        'voltage_margin_v': voltage_margin.model_dump(mode='json'),
        'load_domain_check': load_domain_check,
        'continuous_limiter': limiter,
        'state': state,
        'support_reasons': list(reasons),
    }
    digest = _digest(payload)
    return FrequencyResolvedElectricalEvaluation(
        impedance_ref=impedance_ref,
        amplifier_capability=capability_ref,
        amplifier_limit=limit_ref,
        frequency_band=frequency_band,
        required_voltage_v_rms=required_voltage,
        minimum_magnitude_ohm=minimum_magnitude,
        minimum_magnitude_frequency_hz=minimum_frequency,
        worst_current_demand_a=worst_current,
        worst_current_frequency_hz=worst_frequency,
        current_headroom_db=current_headroom,
        voltage_margin_v=voltage_margin,
        load_domain_check=load_domain_check,
        continuous_limiter=limiter,
        state=state,
        support_reasons=tuple(reasons),
        evaluation_id=_semantic_id('freq-load', digest),
        evaluation_sha256=digest,
    )


def minimum_impedance_out_of_domain(
    amplifier_capability: AmplifierOutputCapability,
    magnitude_ohm: float,
) -> bool:
    """True when |Z| dips below the amplifier's evidenced minimum load."""
    return magnitude_ohm < amplifier_capability.supported_load.minimum_load_ohm


__all__ = [
    'AMPLIFIER_ELECTRICAL_LIMIT_AUTHORITY_VERSION',
    'FREQUENCY_RESOLVED_EVALUATION_VERSION',
    'SPEAKER_IMPEDANCE_AUTHORITY_VERSION',
    'SPEAKER_IMPEDANCE_SCHEMA_VERSION',
    'AmplifierElectricalLimitAuthority',
    'FrequencyResolvedElectricalEvaluation',
    'FrequencyResolvedLimiter',
    'FrequencyResolvedState',
    'ImpedanceInterpolation',
    'ImpedanceSample',
    'ImpedanceTier',
    'LoadDomainCheck',
    'SpeakerElectricalImpedanceAuthority',
    'build_amplifier_electrical_limit',
    'build_speaker_impedance_authority',
    'evaluate_frequency_resolved_load',
    'minimum_impedance_out_of_domain',
]
