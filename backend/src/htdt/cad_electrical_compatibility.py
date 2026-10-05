"""Amplifier↔loudspeaker electrical compatibility qualification (#593).

The electrical layers already exist as separate authorities — the tiered
speaker impedance authority (#544), the amplifier output capability plus
optional electrical limits (#O100D/#544), the speaker-level electrical
transfer including cable loss (#1004) and the line-level gain structure
(#646). What was missing is the *qualification* that composes them into a
single honest verdict about whether a declared amplifier↔loudspeaker chain
can actually deliver a target SPL electrically.

Contract points honoured:

- the required terminal input is *derived* — loudspeaker sensitivity,
  inverse-square distance law, target SPL, declared EQ boost and declared
  headroom — never asserted from ``nominal watts`` alone;
- every gate reports the issue's failure taxonomy
  (``load_below_amplifier_rating``, ``current_margin_insufficient``,
  ``voltage_margin_insufficient``, ``multichannel_power_limit``,
  ``digital_headroom_limit``, ``amplifier_clipping``, ``thermal_derating``,
  ``cable_loss_excessive``, ``loudspeaker_compression_limit``,
  ``protection_engagement``, ``insufficient_evidence``) instead of one
  generic "amp too small" verdict;
- a single-channel amplifier rating is never promoted to simultaneous
  multichannel capability (IEC 60268-3 rated-channel conditions);
- ``capability_class`` records the strongest *evidence basis* actually used
  — ``spec_sheet_estimate`` < ``electrically_qualified_model`` <
  ``acoustic_output_measured`` < ``in_room_commissioned`` — and is never
  upgraded by a passing model;
- the active-loudspeaker lane qualifies on acoustic output evidence alone
  (IEC 60268-21 system output) without fabricating an internal amplifier
  wattage model;
- missing evidence reports ``insufficient_evidence`` / 'missing' /
  'unsupported', never a guessed number — the verdict stays
  ``indeterminate`` rather than passing silently.
"""

from __future__ import annotations

from math import isfinite, log10, sqrt
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_amplifier_headroom import (
    AmplifierOutputCapability,
    AuthorityRef,
    PlaybackChainScalarResult,
)
from .cad_direct_level import DirectLevelFrequencyBand
from .cad_equipment import EquipmentDefinition
from .cad_speaker_impedance import (
    AmplifierElectricalLimitAuthority,
    SpeakerElectricalImpedanceAuthority,
)
from .cad_speaker_level_transfer import (
    AmplifierOutputImpedanceAuthority,
    SpeakerCableElectricalProfile,
    SpeakerElectricalPath,
    evaluate_speaker_level_transfer,
)
from .canonical_json import canonical_sha256 as _digest


ELECTRICAL_QUALIFICATION_SCHEMA_VERSION = 1
ELECTRICAL_SCENARIO_AUTHORITY_VERSION = 'rev56-electrical-qualification-scenario-1'
ELECTRICAL_QUALIFICATION_AUTHORITY_VERSION = 'rev56-electrical-qualification-1'


#: Failure taxonomy from #593 — no generic ``amp too small`` verdict.
ElectricalFailureCode = Literal[
    'load_below_amplifier_rating',
    'current_margin_insufficient',
    'voltage_margin_insufficient',
    'multichannel_power_limit',
    'digital_headroom_limit',
    'amplifier_clipping',
    'thermal_derating',
    'cable_loss_excessive',
    'loudspeaker_compression_limit',
    'protection_engagement',
    'insufficient_evidence',
]

#: Strongest evidence basis actually used — never promoted upward.
CapabilityEvidenceClass = Literal[
    'spec_sheet_estimate',
    'electrically_qualified_model',
    'acoustic_output_measured',
    'in_room_commissioned',
]

#: Time domain of the requested qualification (#593 §10).
StressProfile = Literal[
    'short_burst',
    'program_like',
    'sustained',
    'thermally_stabilized',
]

DriveKind = Literal['passive_external_amplifier', 'active_system']

QualificationVerdict = Literal['qualified', 'unqualified', 'indeterminate']

DominantLimiter = Literal[
    'voltage',
    'current',
    'load_domain',
    'multichannel',
    'digital',
    'speaker_compression',
    'protection',
    'cable',
    'thermal',
    'none',
    'unknown',
]


_FAILURE_ORDER: tuple[ElectricalFailureCode, ...] = (
    'load_below_amplifier_rating',
    'current_margin_insufficient',
    'voltage_margin_insufficient',
    'multichannel_power_limit',
    'digital_headroom_limit',
    'amplifier_clipping',
    'thermal_derating',
    'cable_loss_excessive',
    'loudspeaker_compression_limit',
    'protection_engagement',
    'insufficient_evidence',
)


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


def _scalar(
    state: Literal['available', 'missing', 'unsupported'],
    *,
    value: float | None,
    unit: Literal['V RMS', 'W', 'dB', 'dB SPL', 'ohm', 'A'],
    reason: str | None = None,
) -> PlaybackChainScalarResult:
    return PlaybackChainScalarResult(
        state=state, value=value, unit=unit, reason=reason
    )


def _missing(reason: str, unit: Literal['V RMS', 'W', 'dB', 'dB SPL', 'ohm', 'A']) -> PlaybackChainScalarResult:
    return _scalar('missing', value=None, unit=unit, reason=reason)


def _unsupported(reason: str, unit: Literal['V RMS', 'W', 'dB', 'dB SPL', 'ohm', 'A']) -> PlaybackChainScalarResult:
    return _scalar('unsupported', value=None, unit=unit, reason=reason)


class ElectricalQualificationScenario(BaseModel):
    """Exact qualification request: what target, which load, which evidence.

    Binds the speaker definition, the amplifier capability (passive lane),
    optional load/limit/path authorities, and the requested acoustic target
    — target SPL at a listening distance over a frequency band under a
    declared stress profile — into one sealed request.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ELECTRICAL_QUALIFICATION_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-electrical-qualification-scenario-1'
    ] = ELECTRICAL_SCENARIO_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    equipment: AuthorityRef
    drive_kind: DriveKind = 'passive_external_amplifier'
    amplifier_capability: AuthorityRef | None = None
    impedance: AuthorityRef | None = None
    amplifier_limit: AuthorityRef | None = None
    electrical_path: AuthorityRef | None = None
    target_spl_db_spl: float
    listening_distance_m: float = Field(gt=0.0)
    frequency_band: DirectLevelFrequencyBand
    weighting: str = Field(min_length=1)
    simultaneous_channel_count: int = Field(ge=1)
    stress_profile: StressProfile = 'sustained'
    eq_boost_db: float = Field(default=0.0, ge=0.0)
    eq_boost_band: DirectLevelFrequencyBand | None = None
    headroom_db: float = Field(default=0.0, ge=0.0)
    digital_headroom_db: float | None = Field(default=None, ge=0.0)
    commissioning_evidence_ref: str | None = Field(default=None, min_length=1)
    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator(
        'target_spl_db_spl',
        'listening_distance_m',
        'eq_boost_db',
        'headroom_db',
        'digital_headroom_db',
    )
    @classmethod
    def finite_scenario_value(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='qualification scenario value')

    @model_validator(mode='after')
    def valid_scenario(self) -> 'ElectricalQualificationScenario':
        if self.drive_kind == 'active_system':
            if (
                self.amplifier_capability is not None
                or self.impedance is not None
                or self.amplifier_limit is not None
                or self.electrical_path is not None
            ):
                raise ValueError(
                    'active loudspeaker qualification must not bind '
                    'amplifier/load/path authorities — the internal '
                    'amplifier is not a user-separable component'
                )
        else:
            if self.amplifier_capability is None:
                raise ValueError(
                    'passive qualification requires an amplifier '
                    'capability reference'
                )
        if self.eq_boost_band is not None and self.eq_boost_db <= 0.0:
            raise ValueError(
                'an EQ boost band requires a positive boost amount'
            )
        digest = _digest(self.semantic_payload())
        if self.scenario_sha256 != digest:
            raise ValueError('ElectricalQualificationScenario hash mismatch')
        if self.scenario_id != _semantic_id('elec-scenario', digest):
            raise ValueError('ElectricalQualificationScenario id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'equipment': self.equipment.model_dump(mode='json'),
            'drive_kind': self.drive_kind,
            'amplifier_capability': (
                None
                if self.amplifier_capability is None
                else self.amplifier_capability.model_dump(mode='json')
            ),
            'impedance': (
                None
                if self.impedance is None
                else self.impedance.model_dump(mode='json')
            ),
            'amplifier_limit': (
                None
                if self.amplifier_limit is None
                else self.amplifier_limit.model_dump(mode='json')
            ),
            'electrical_path': (
                None
                if self.electrical_path is None
                else self.electrical_path.model_dump(mode='json')
            ),
            'target_spl_db_spl': self.target_spl_db_spl,
            'listening_distance_m': self.listening_distance_m,
            'frequency_band': self.frequency_band.model_dump(mode='json'),
            'weighting': self.weighting,
            'simultaneous_channel_count': self.simultaneous_channel_count,
            'stress_profile': self.stress_profile,
            'eq_boost_db': self.eq_boost_db,
            'eq_boost_band': (
                None
                if self.eq_boost_band is None
                else self.eq_boost_band.model_dump(mode='json')
            ),
            'headroom_db': self.headroom_db,
            'digital_headroom_db': self.digital_headroom_db,
            'commissioning_evidence_ref': self.commissioning_evidence_ref,
        }


def _ref(model: BaseModel, authority_id: str, version: str, sha: str) -> AuthorityRef:
    return AuthorityRef(
        authority_id=authority_id, version=version, semantic_sha256=sha
    )


def build_electrical_scenario(
    *,
    document_id: str,
    equipment: EquipmentDefinition,
    target_spl_db_spl: float,
    listening_distance_m: float,
    frequency_band: DirectLevelFrequencyBand,
    weighting: str,
    drive_kind: DriveKind = 'passive_external_amplifier',
    amplifier_capability: AmplifierOutputCapability | None = None,
    impedance: SpeakerElectricalImpedanceAuthority | None = None,
    amplifier_limit: AmplifierElectricalLimitAuthority | None = None,
    electrical_path: SpeakerElectricalPath | None = None,
    simultaneous_channel_count: int = 1,
    stress_profile: StressProfile = 'sustained',
    eq_boost_db: float = 0.0,
    eq_boost_band: DirectLevelFrequencyBand | None = None,
    headroom_db: float = 0.0,
    digital_headroom_db: float | None = None,
    commissioning_evidence_ref: str | None = None,
) -> ElectricalQualificationScenario:
    """Build the sealed qualification scenario with exact authority refs."""
    equipment_ref = _ref(
        equipment,
        equipment.definition_id,
        equipment.version,
        equipment.semantic_sha256,
    )
    amplifier_ref = (
        None
        if amplifier_capability is None
        else _ref(
            amplifier_capability,
            amplifier_capability.capability_id,
            amplifier_capability.version,
            amplifier_capability.semantic_sha256,
        )
    )
    impedance_ref = (
        None
        if impedance is None
        else _ref(
            impedance,
            impedance.impedance_id,
            impedance.version,
            impedance.semantic_sha256,
        )
    )
    limit_ref = (
        None
        if amplifier_limit is None
        else _ref(
            amplifier_limit,
            amplifier_limit.limit_id,
            amplifier_limit.version,
            amplifier_limit.semantic_sha256,
        )
    )
    path_ref = (
        None
        if electrical_path is None
        else _ref(
            electrical_path,
            electrical_path.path_id,
            electrical_path.version,
            electrical_path.semantic_sha256,
        )
    )
    payload: dict[str, Any] = {
        'schema_version': ELECTRICAL_QUALIFICATION_SCHEMA_VERSION,
        'authority_version': ELECTRICAL_SCENARIO_AUTHORITY_VERSION,
        'document_id': document_id,
        'equipment': equipment_ref.model_dump(mode='json'),
        'drive_kind': drive_kind,
        'amplifier_capability': (
            None if amplifier_ref is None else amplifier_ref.model_dump(mode='json')
        ),
        'impedance': (
            None if impedance_ref is None else impedance_ref.model_dump(mode='json')
        ),
        'amplifier_limit': (
            None if limit_ref is None else limit_ref.model_dump(mode='json')
        ),
        'electrical_path': (
            None if path_ref is None else path_ref.model_dump(mode='json')
        ),
        'target_spl_db_spl': float(target_spl_db_spl),
        'listening_distance_m': float(listening_distance_m),
        'frequency_band': frequency_band.model_dump(mode='json'),
        'weighting': weighting,
        'simultaneous_channel_count': int(simultaneous_channel_count),
        'stress_profile': stress_profile,
        'eq_boost_db': float(eq_boost_db),
        'eq_boost_band': (
            None
            if eq_boost_band is None
            else eq_boost_band.model_dump(mode='json')
        ),
        'headroom_db': float(headroom_db),
        'digital_headroom_db': (
            None if digital_headroom_db is None else float(digital_headroom_db)
        ),
        'commissioning_evidence_ref': commissioning_evidence_ref,
    }
    digest = _digest(payload)
    return ElectricalQualificationScenario(
        document_id=document_id,
        equipment=equipment_ref,
        drive_kind=drive_kind,
        amplifier_capability=amplifier_ref,
        impedance=impedance_ref,
        amplifier_limit=limit_ref,
        electrical_path=path_ref,
        target_spl_db_spl=target_spl_db_spl,
        listening_distance_m=listening_distance_m,
        frequency_band=frequency_band,
        weighting=weighting,
        simultaneous_channel_count=simultaneous_channel_count,
        stress_profile=stress_profile,
        eq_boost_db=eq_boost_db,
        eq_boost_band=eq_boost_band,
        headroom_db=headroom_db,
        digital_headroom_db=digital_headroom_db,
        commissioning_evidence_ref=commissioning_evidence_ref,
        scenario_id=_semantic_id('elec-scenario', digest),
        scenario_sha256=digest,
    )


class ElectricalPlaybackQualification(BaseModel):
    """Sealed electrical compatibility verdict for one scenario.

    ``failure_codes`` carries the issue's failure taxonomy — an
    ``unqualified`` verdict always names the limiting electrical domain(s),
    and ``indeterminate`` is only reached through ``insufficient_evidence``.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ELECTRICAL_QUALIFICATION_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-electrical-qualification-1'
    ] = ELECTRICAL_QUALIFICATION_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    scenario: ElectricalQualificationScenario
    required_input: PlaybackChainScalarResult
    required_terminal_voltage_v_rms: PlaybackChainScalarResult
    amplifier_output_ceiling: PlaybackChainScalarResult
    delivered_terminal_voltage_v_rms: PlaybackChainScalarResult
    cable_loss_db: PlaybackChainScalarResult
    worst_current_demand_a: PlaybackChainScalarResult
    current_headroom_db: PlaybackChainScalarResult
    voltage_margin_v_rms: PlaybackChainScalarResult
    speaker_spl_ceiling_db_spl: PlaybackChainScalarResult
    spl_margin_db: PlaybackChainScalarResult
    minimum_load_ohm: PlaybackChainScalarResult
    failure_codes: tuple[ElectricalFailureCode, ...]
    dominant_limiter: DominantLimiter
    verdict: QualificationVerdict
    capability_class: CapabilityEvidenceClass
    support_reasons: tuple[str, ...]
    qualification_id: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluated_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'ElectricalPlaybackQualification':
        if len(self.failure_codes) != len(set(self.failure_codes)):
            raise ValueError('failure codes must be unique')
        real_failures = [
            code
            for code in self.failure_codes
            if code != 'insufficient_evidence'
        ]
        if self.verdict == 'qualified' and self.failure_codes:
            raise ValueError('qualified verdict cannot carry failure codes')
        if self.verdict == 'unqualified' and not real_failures:
            raise ValueError('unqualified verdict requires a real failure code')
        if (
            self.verdict == 'indeterminate'
            and 'insufficient_evidence' not in self.failure_codes
        ):
            raise ValueError(
                'indeterminate verdict requires insufficient_evidence'
            )
        digest = _digest(self.identity_payload())
        if self.qualification_sha256 != digest:
            raise ValueError('ElectricalPlaybackQualification hash mismatch')
        if self.qualification_id != _semantic_id('elec-qual', digest):
            raise ValueError('ElectricalPlaybackQualification id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'scenario': self.scenario.model_dump(mode='json'),
            'required_input': self.required_input.model_dump(mode='json'),
            'required_terminal_voltage_v_rms': (
                self.required_terminal_voltage_v_rms.model_dump(mode='json')
            ),
            'amplifier_output_ceiling': (
                self.amplifier_output_ceiling.model_dump(mode='json')
            ),
            'delivered_terminal_voltage_v_rms': (
                self.delivered_terminal_voltage_v_rms.model_dump(mode='json')
            ),
            'cable_loss_db': self.cable_loss_db.model_dump(mode='json'),
            'worst_current_demand_a': (
                self.worst_current_demand_a.model_dump(mode='json')
            ),
            'current_headroom_db': self.current_headroom_db.model_dump(mode='json'),
            'voltage_margin_v_rms': (
                self.voltage_margin_v_rms.model_dump(mode='json')
            ),
            'speaker_spl_ceiling_db_spl': (
                self.speaker_spl_ceiling_db_spl.model_dump(mode='json')
            ),
            'spl_margin_db': self.spl_margin_db.model_dump(mode='json'),
            'minimum_load_ohm': self.minimum_load_ohm.model_dump(mode='json'),
            'failure_codes': list(self.failure_codes),
            'dominant_limiter': self.dominant_limiter,
            'verdict': self.verdict,
            'capability_class': self.capability_class,
            'support_reasons': list(self.support_reasons),
            'evaluated_at_utc': self.evaluated_at_utc,
        }


def _check_ref(
    ref: AuthorityRef | None,
    *,
    authority_id: str,
    version: str,
    semantic_sha256: str,
    label: str,
) -> None:
    if ref is None:
        raise ValueError(f'scenario omits supplied {label} authority')
    if (
        ref.authority_id != authority_id
        or ref.version != version
        or ref.semantic_sha256 != semantic_sha256
    ):
        raise ValueError(f'scenario {label} authority reference mismatch')


def _bands_overlap(a: DirectLevelFrequencyBand, b: DirectLevelFrequencyBand) -> bool:
    return a.low_hz < b.high_hz and b.low_hz < a.high_hz


def _band_contains(domain_lo: float, domain_hi: float, band: DirectLevelFrequencyBand) -> bool:
    return band.low_hz >= domain_lo and band.high_hz <= domain_hi


def _convert_electrical_value(
    value: float,
    from_quantity: str,
    to_quantity: str,
    impedance: SpeakerElectricalImpedanceAuthority | None,
) -> float | None:
    """V↔W conversion only under an exact resistive reference (#544)."""
    if from_quantity == to_quantity:
        return float(value)
    if (
        impedance is None
        or impedance.tier != 'exact_resistive_reference'
    ):
        return None
    resistance = float(impedance.nominal_impedance_ohm)
    if from_quantity == 'power_w' and to_quantity == 'voltage_v_rms':
        return sqrt(float(value) * resistance)
    if from_quantity == 'voltage_v_rms' and to_quantity == 'power_w':
        return float(value) ** 2 / resistance
    return None


def _min_band_magnitude(
    impedance: SpeakerElectricalImpedanceAuthority,
    band: DirectLevelFrequencyBand,
) -> tuple[float, float] | None:
    """Minimum |Z| inside *band* as ``(frequency_hz, ohm)`` where evidenced.

    Only curve tiers resolve a band minimum; the ``minimum_impedance`` tier
    resolves its declared point only when it falls inside the band.
    """
    if impedance.tier in ('magnitude_only_curve', 'complex_curve'):
        points: list[tuple[float, float]] = []
        for sample in impedance.samples:
            if sample.frequency_hz < band.low_hz:
                continue
            if sample.frequency_hz > band.high_hz:
                break
            points.append((sample.frequency_hz, sample.magnitude()))
        for edge in (band.low_hz, band.high_hz):
            magnitude = impedance.magnitude_at(edge)
            if magnitude is not None:
                points.append((edge, magnitude))
        if len(points) < 2:
            return None
        frequency, minimum = min(points, key=lambda item: item[1])
        return frequency, minimum
    if impedance.tier == 'minimum_impedance':
        if (
            impedance.minimum_impedance_ohm is not None
            and impedance.minimum_frequency_hz is not None
            and band.low_hz <= impedance.minimum_frequency_hz <= band.high_hz
        ):
            return (
                impedance.minimum_frequency_hz,
                impedance.minimum_impedance_ohm,
            )
        return None
    if impedance.tier == 'exact_resistive_reference':
        return band.low_hz, impedance.nominal_impedance_ohm
    return None


def _stress_uses_peak(stress_profile: StressProfile) -> bool:
    return stress_profile in ('short_burst', 'program_like')


def evaluate_electrical_compatibility(
    *,
    scenario: ElectricalQualificationScenario,
    equipment: EquipmentDefinition,
    evaluated_at_utc: str,
    amplifier_capability: AmplifierOutputCapability | None = None,
    impedance: SpeakerElectricalImpedanceAuthority | None = None,
    amplifier_limit: AmplifierElectricalLimitAuthority | None = None,
    electrical_path: SpeakerElectricalPath | None = None,
    amplifier_output_impedance: AmplifierOutputImpedanceAuthority | None = None,
    cable_profile: SpeakerCableElectricalProfile | None = None,
) -> ElectricalPlaybackQualification:
    """Qualify one amplifier↔loudspeaker chain electrically.

    Fail-closed: missing load/capability/limit evidence degrades the verdict
    to ``indeterminate`` via ``insufficient_evidence`` — never to a silent
    pass. Real failures name their domain code.
    """

    _check_ref(
        scenario.equipment,
        authority_id=equipment.definition_id,
        version=equipment.version,
        semantic_sha256=equipment.semantic_sha256,
        label='equipment',
    )
    if amplifier_capability is not None:
        _check_ref(
            scenario.amplifier_capability,
            authority_id=amplifier_capability.capability_id,
            version=amplifier_capability.version,
            semantic_sha256=amplifier_capability.semantic_sha256,
            label='amplifier_capability',
        )
    if impedance is not None:
        _check_ref(
            scenario.impedance,
            authority_id=impedance.impedance_id,
            version=impedance.version,
            semantic_sha256=impedance.semantic_sha256,
            label='impedance',
        )
        if (
            impedance.equipment_definition_id != equipment.definition_id
            or impedance.equipment_definition_version != equipment.version
            or impedance.equipment_definition_sha256
            != equipment.semantic_sha256
        ):
            raise ValueError(
                'impedance authority is not bound to the scenario speaker'
            )
    if amplifier_limit is not None:
        _check_ref(
            scenario.amplifier_limit,
            authority_id=amplifier_limit.limit_id,
            version=amplifier_limit.version,
            semantic_sha256=amplifier_limit.semantic_sha256,
            label='amplifier_limit',
        )
        if amplifier_capability is None:
            raise ValueError(
                'an amplifier electrical limit requires the amplifier '
                'capability it extends'
            )
        if (
            amplifier_limit.amplifier_capability.authority_id
            != amplifier_capability.capability_id
            or amplifier_limit.amplifier_capability.version
            != amplifier_capability.version
            or amplifier_limit.amplifier_capability.semantic_sha256
            != amplifier_capability.semantic_sha256
        ):
            raise ValueError(
                'amplifier limit does not reference the scenario capability'
            )
    if electrical_path is not None:
        _check_ref(
            scenario.electrical_path,
            authority_id=electrical_path.path_id,
            version=electrical_path.version,
            semantic_sha256=electrical_path.semantic_sha256,
            label='electrical_path',
        )
        if impedance is not None and electrical_path.load_impedance_ids:
            if impedance.impedance_id not in electrical_path.load_impedance_ids:
                raise ValueError(
                    'scenario impedance is not part of the supplied '
                    'electrical path load set'
                )

    failures: list[ElectricalFailureCode] = []
    reasons: list[str] = []

    def _fail(code: ElectricalFailureCode, reason: str) -> None:
        if code not in failures:
            failures.append(code)
        reasons.append(reason)

    active = scenario.drive_kind == 'active_system'
    use_peak = _stress_uses_peak(scenario.stress_profile)

    # --- 1. required terminal input (sensitivity + distance + target) ----
    sensitivity = equipment.sensitivity
    if scenario.drive_kind == 'active_system':
        required_input = _unsupported(
            'active loudspeaker lane qualifies on acoustic output '
            'evidence — no separable input quantity exists',
            'V RMS',
        )
        required_v = required_input
    else:
        required_input = _missing(
            'speaker sensitivity/reference level is not evidenced',
            'V RMS',
        )
        required_v = _missing(
            'required terminal voltage cannot be derived', 'V RMS'
        )
    required_power = _missing(
        'required input power cannot be derived', 'W'
    )
    boost_effective = 0.0
    if scenario.eq_boost_db > 0.0 and (
        scenario.eq_boost_band is None
        or _bands_overlap(scenario.eq_boost_band, scenario.frequency_band)
    ):
        boost_effective = scenario.eq_boost_db
    if not active:
        if sensitivity is None:
            _fail(
                'insufficient_evidence',
                'speaker sensitivity/reference level is not evidenced',
            )
        else:
            reasons_from_sensitivity = False
            if sensitivity.valid_frequency_domain is None:
                _fail(
                    'insufficient_evidence',
                    'speaker sensitivity has no valid frequency domain',
                )
                reasons_from_sensitivity = True
            elif not _band_contains(
                sensitivity.valid_frequency_domain.minimum_hz,
                sensitivity.valid_frequency_domain.maximum_hz,
                scenario.frequency_band,
            ):
                _fail(
                    'insufficient_evidence',
                    'requested band is outside the speaker sensitivity domain',
                )
                reasons_from_sensitivity = True
            if sensitivity.weighting is None:
                if scenario.weighting != 'unweighted':
                    _fail(
                        'insufficient_evidence',
                        'speaker sensitivity has no weighting authority for '
                        'the requested weighting',
                    )
                    reasons_from_sensitivity = True
            elif sensitivity.weighting != scenario.weighting:
                _fail(
                    'insufficient_evidence',
                    'speaker sensitivity weighting differs from the scenario',
                )
                reasons_from_sensitivity = True
            if not reasons_from_sensitivity:
                # Literature formula: required level at the reference
                # distance is target SPL plus inverse-square loss
                # (20·log10(d/d_ref)) plus declared EQ boost and headroom.
                required_level_db = (
                    float(scenario.target_spl_db_spl)
                    + 20.0
                    * log10(
                        float(scenario.listening_distance_m)
                        / float(sensitivity.distance_m)
                    )
                    + boost_effective
                    + float(scenario.headroom_db)
                )
                delta_db = required_level_db - float(sensitivity.level_db_spl)
                if sensitivity.input_quantity == 'voltage_v_rms':
                    value = float(sensitivity.input_value) * (
                        10.0 ** (delta_db / 20.0)
                    )
                    required_input = _scalar(
                        'available', value=value, unit='V RMS'
                    )
                    required_v = required_input
                else:
                    value = float(sensitivity.input_value) * (
                        10.0 ** (delta_db / 10.0)
                    )
                    required_input = _scalar(
                        'available', value=value, unit='W'
                    )
                    required_power = required_input

    # --- 2. required terminal voltage -----------------------------------
    if (
        not active
        and required_input.state == 'available'
        and sensitivity is not None
        and sensitivity.input_quantity == 'power_w'
    ):
        if (
            impedance is not None
            and impedance.tier == 'exact_resistive_reference'
        ):
            required_v = _scalar(
                'available',
                value=sqrt(
                    float(required_input.value)
                    * float(impedance.nominal_impedance_ohm)
                ),
                unit='V RMS',
            )
        else:
            required_v = _unsupported(
                'power-quantity sensitivity needs an exact resistive '
                'reference to derive the required terminal voltage',
                'V RMS',
            )
            _fail(
                'insufficient_evidence',
                'required terminal voltage unresolvable without an exact '
                'resistive reference load',
            )

    # --- 3. amplifier gates (passive lane only) --------------------------
    ceiling = _missing('no amplifier capability is bound', 'V RMS')
    delivered_v = _missing('delivered terminal voltage not evaluated', 'V RMS')
    cable_loss = _missing('cable loss not evaluated', 'dB')
    worst_current = _missing('current demand not evaluated', 'A')
    current_headroom = _missing('current headroom not evaluated', 'dB')
    voltage_margin = _missing('voltage margin not evaluated', 'V RMS')
    minimum_load = _missing('minimum load not resolved', 'ohm')

    if not active:
        if amplifier_capability is None:
            _fail(
                'insufficient_evidence',
                'no amplifier output capability authority is bound',
            )
        else:
            if not _band_contains(
                amplifier_capability.valid_frequency_band.minimum_hz,
                amplifier_capability.valid_frequency_band.maximum_hz,
                scenario.frequency_band,
            ):
                _fail(
                    'insufficient_evidence',
                    'requested band is outside the amplifier capability '
                    'domain',
                )
            if amplifier_capability.weighting != scenario.weighting:
                _fail(
                    'insufficient_evidence',
                    'scenario weighting differs from the amplifier '
                    'capability authority',
                )
            capability_channels = (
                amplifier_capability.channel_count_condition
                .simultaneous_channel_count
            )
            if capability_channels != scenario.simultaneous_channel_count:
                if (
                    scenario.simultaneous_channel_count > 1
                    and capability_channels == 1
                ):
                    _fail(
                        'multichannel_power_limit',
                        'single-channel amplifier rating cannot qualify '
                        'simultaneous multichannel operation (IEC 60268-3 '
                        'multichannel rated conditions)',
                    )
                else:
                    _fail(
                        'insufficient_evidence',
                        'simultaneous channel-count condition differs from '
                        'the amplifier capability authority',
                    )

            # capability ceiling selected for the requested stress profile
            capability = (
                amplifier_capability.peak_capability
                if use_peak
                else amplifier_capability.continuous_capability
            )
            capability_duration = (
                amplifier_capability.peak_duration_s
                if use_peak
                else amplifier_capability.continuous_duration_s
            )
            fallback_used = False
            if capability is None and use_peak:
                capability = amplifier_capability.continuous_capability
                capability_duration = (
                    amplifier_capability.continuous_duration_s
                )
                fallback_used = True
            if capability is None:
                ceiling = _missing(
                    f'{scenario.stress_profile} amplifier capability is '
                    'not evidenced',
                    'V RMS',
                )
                _fail(
                    'insufficient_evidence',
                    f'{scenario.stress_profile} amplifier capability is '
                    'not evidenced',
                )
            elif capability_duration is None:
                _fail(
                    'insufficient_evidence',
                    'amplifier capability duration evidence is missing',
                )
            elif fallback_used:
                reasons.append(
                    'no peak capability evidence; program evaluated '
                    'against the continuous ceiling'
                )

            # compare required input vs the evidenced ceiling in the
            # scenario's own quantity — conversion needs an exact load.
            ceiling_in_quantity: float | None = None
            if capability is not None and required_input.state == 'available':
                assert sensitivity is not None
                ceiling_in_quantity = _convert_electrical_value(
                    float(capability.value),
                    capability.quantity,
                    sensitivity.input_quantity,
                    impedance,
                )
                quantity_unit = (
                    'V RMS'
                    if sensitivity.input_quantity == 'voltage_v_rms'
                    else 'W'
                )
                ceiling = (
                    _scalar(
                        'available',
                        value=ceiling_in_quantity,
                        unit=quantity_unit,
                    )
                    if ceiling_in_quantity is not None
                    else _unsupported(
                        'amplifier ceiling cannot be converted to the '
                        'sensitivity input quantity without an exact '
                        'resistive reference',
                        quantity_unit,
                    )
                )
                if ceiling_in_quantity is not None:
                    if float(required_input.value) > ceiling_in_quantity:
                        _fail(
                            'amplifier_clipping',
                            'required input exceeds the evidenced amplifier '
                            'output ceiling (clipping reference '
                            f'{amplifier_capability.clipping_reference_definition!r})',
                        )
                    # sustained derating: burst ceiling holds but the
                    # sustained ceiling does not.
                    if (
                        scenario.stress_profile
                        in ('sustained', 'thermally_stabilized')
                        and amplifier_capability.peak_capability is not None
                    ):
                        peak_in_quantity = _convert_electrical_value(
                            float(amplifier_capability.peak_capability.value),
                            amplifier_capability.peak_capability.quantity,
                            sensitivity.input_quantity,
                            impedance,
                        )
                        if (
                            peak_in_quantity is not None
                            and float(required_input.value)
                            > ceiling_in_quantity
                            and float(required_input.value)
                            <= peak_in_quantity
                        ):
                            _fail(
                                'thermal_derating',
                                'sustained capability is derated below the '
                                'required input while the burst ceiling '
                                'still holds',
                            )

    # --- 4. load-domain gate ---------------------------------------------
    min_magnitude: float | None = None
    if not active:
        if impedance is None:
            _fail(
                'insufficient_evidence',
                'speaker electrical load authority is missing',
            )
        else:
            if not _band_contains(
                impedance.valid_frequency_domain.minimum_hz,
                impedance.valid_frequency_domain.maximum_hz,
                scenario.frequency_band,
            ):
                _fail(
                    'insufficient_evidence',
                    'requested band is outside the impedance authority '
                    'domain',
                )
            elif impedance.tier == 'nominal_impedance_only':
                _fail(
                    'insufficient_evidence',
                    'nominal impedance only: |Z|(f) and its minimum are '
                    'unevidenced; nominal impedance is not a load model',
                )
            else:
                resolved = _min_band_magnitude(
                    impedance, scenario.frequency_band
                )
                if resolved is None:
                    _fail(
                        'insufficient_evidence',
                        'impedance evidence does not resolve a minimum '
                        'inside the requested band',
                    )
                else:
                    min_frequency, min_magnitude = resolved
                    minimum_load = _scalar(
                        'available', value=min_magnitude, unit='ohm'
                    )
                    if amplifier_capability is not None:
                        domain = amplifier_capability.supported_load
                        if min_magnitude < domain.minimum_load_ohm:
                            _fail(
                                'load_below_amplifier_rating',
                                f'minimum |Z| {min_magnitude:.3g} ohm at '
                                f'{min_frequency:g} Hz is below the '
                                'amplifier evidenced minimum load',
                            )
                        elif min_magnitude > domain.maximum_load_ohm:
                            _fail(
                                'insufficient_evidence',
                                'speaker load exceeds the amplifier '
                                'evidenced maximum load — capability '
                                'unverified at that load',
                            )
            # current demand: required V over min |Z|
            if (
                required_v.state == 'available'
                and min_magnitude is not None
                and min_magnitude > 0.0
            ):
                demand = float(required_v.value) / min_magnitude
                worst_current = _scalar(
                    'available', value=demand, unit='A'
                )

    # --- 5. voltage / current ceilings (limit authority) -----------------
    if not active and required_v.state == 'available':
        if amplifier_limit is not None:
            limit_v = (
                amplifier_limit.peak_voltage_ceiling_v
                if use_peak
                else amplifier_limit.rms_voltage_ceiling_v
            )
            if limit_v is not None:
                if not _band_contains(
                    amplifier_limit.valid_frequency_domain.minimum_hz,
                    amplifier_limit.valid_frequency_domain.maximum_hz,
                    scenario.frequency_band,
                ):
                    _fail(
                        'insufficient_evidence',
                        'amplifier voltage ceiling is unevidenced for the '
                        'requested band',
                    )
                else:
                    margin = float(limit_v) - float(required_v.value)
                    voltage_margin = _scalar(
                        'available', value=margin, unit='V RMS'
                    )
                    if margin < 0.0:
                        _fail(
                            'voltage_margin_insufficient',
                            'required terminal voltage exceeds the '
                            'evidenced amplifier voltage ceiling',
                        )
            if (
                amplifier_limit.rms_current_ceiling_a is not None
                or amplifier_limit.peak_current_ceiling_a is not None
            ):
                limit_i = (
                    amplifier_limit.peak_current_ceiling_a
                    if use_peak
                    else amplifier_limit.rms_current_ceiling_a
                )
                if (
                    limit_i is not None
                    and worst_current.state == 'available'
                ):
                    if not _band_contains(
                        amplifier_limit.valid_frequency_domain.minimum_hz,
                        amplifier_limit.valid_frequency_domain.maximum_hz,
                        scenario.frequency_band,
                    ):
                        _fail(
                            'insufficient_evidence',
                            'amplifier current ceiling is unevidenced for '
                            'the requested band',
                        )
                    else:
                        headroom_db = 20.0 * log10(
                            float(limit_i) / float(worst_current.value)
                        )
                        current_headroom = _scalar(
                            'available', value=headroom_db, unit='dB'
                        )
                        if headroom_db < 0.0:
                            _fail(
                                'current_margin_insufficient',
                                'worst-case current demand exceeds the '
                                'evidenced amplifier current ceiling',
                            )

    # --- 6. cable path / delivered voltage -------------------------------
    if (
        not active
        and electrical_path is not None
        and cable_profile is not None
        and amplifier_output_impedance is not None
        and impedance is not None
    ):
        if (
            required_v.state == 'available'
            and amplifier_capability is not None
        ):
            frequencies = {scenario.frequency_band.low_hz, scenario.frequency_band.high_hz}
            if min_magnitude is not None:
                resolved = _min_band_magnitude(
                    impedance, scenario.frequency_band
                )
                if resolved is not None:
                    frequencies.add(resolved[0])
            transfer = evaluate_speaker_level_transfer(
                path=electrical_path,
                amplifier_impedance=amplifier_output_impedance,
                cable_profile=cable_profile,
                load_impedances=(impedance,),
                frequencies_hz=tuple(sorted(frequencies)),
                source_voltage_v=float(required_v.value),
            )
            if transfer.status == 'not_computable':
                _fail(
                    'insufficient_evidence',
                    'declared cable path cannot be computed: '
                    + '; '.join(transfer.reasons),
                )
            else:
                worst_transfer_db = min(
                    point.voltage_transfer_db for point in transfer.points
                )
                delivered = float(required_v.value) * (
                    10.0 ** (worst_transfer_db / 20.0)
                )
                delivered_v = _scalar(
                    'available', value=delivered, unit='V RMS'
                )
                cable_loss = _scalar(
                    'available', value=worst_transfer_db, unit='dB'
                )
                # Required *source* voltage the amplifier must produce so
                # that the terminal still sees the required voltage.
                required_source_v = (
                    float(required_v.value) / (10.0 ** (worst_transfer_db / 20.0))
                    if worst_transfer_db != 0.0
                    else float(required_v.value)
                )
                ceiling_v: float | None = None
                if ceiling.state == 'available' and ceiling.value is not None:
                    if ceiling.unit == 'V RMS':
                        ceiling_v = float(ceiling.value)
                if ceiling_v is None and voltage_margin.state == 'available':
                    # voltage ceiling evidenced via limit authority
                    limit_v = (
                        amplifier_limit.peak_voltage_ceiling_v
                        if amplifier_limit is not None and use_peak
                        else (
                            amplifier_limit.rms_voltage_ceiling_v
                            if amplifier_limit is not None
                            else None
                        )
                    )
                    if limit_v is not None:
                        ceiling_v = float(limit_v)
                if ceiling_v is not None and required_source_v > ceiling_v:
                    if float(required_v.value) <= ceiling_v:
                        _fail(
                            'cable_loss_excessive',
                            'the amplifier ceiling would satisfy the '
                            'terminal requirement but the declared cable '
                            'loss pushes the required source voltage above '
                            'it',
                        )
                    else:
                        reasons.append(
                            'cable loss deepens the required source '
                            'voltage beyond the amplifier ceiling'
                        )
    elif not active and electrical_path is not None:
        _fail(
            'insufficient_evidence',
            'an electrical path was bound but its cable/amplifier-'
            'impedance evidence is incomplete',
        )

    # --- 7. speaker acoustic output ceiling ------------------------------
    spl_ceiling = _missing(
        'speaker acoustic SPL capability is not evidenced', 'dB SPL'
    )
    spl_margin = _missing(
        'speaker acoustic margin cannot be derived', 'dB'
    )
    spl_capability = equipment.spl_capability
    if spl_capability is not None:
        level = (
            spl_capability.peak_db_spl
            if use_peak
            else spl_capability.continuous_db_spl
        )
        duration = (
            spl_capability.peak_duration_s
            if use_peak
            else spl_capability.continuous_duration_s
        )
        if level is None and use_peak:
            level = spl_capability.continuous_db_spl
            duration = spl_capability.continuous_duration_s
        if level is None:
            _fail(
                'insufficient_evidence',
                f'speaker {scenario.stress_profile} SPL capability is '
                'not evidenced',
            )
        elif spl_capability.valid_frequency_domain is None:
            _fail(
                'insufficient_evidence',
                'speaker SPL capability has no valid frequency domain',
            )
        elif not _band_contains(
            spl_capability.valid_frequency_domain.minimum_hz,
            spl_capability.valid_frequency_domain.maximum_hz,
            scenario.frequency_band,
        ):
            _fail(
                'insufficient_evidence',
                'requested band is outside the speaker SPL capability '
                'domain',
            )
        elif scenario.weighting != 'unweighted':
            _fail(
                'insufficient_evidence',
                'speaker SPL capability has no weighting provenance for '
                'the requested weighting',
            )
        elif duration is None:
            _fail(
                'insufficient_evidence',
                'speaker SPL capability has no duration authority',
            )
        else:
            ceiling_at_distance = float(level) + 20.0 * log10(
                float(spl_capability.reference_distance_m)
                / float(scenario.listening_distance_m)
            )
            # declared limiter headroom caps the usable ceiling — a
            # protection-defined ceiling, not a compression one.
            usable = ceiling_at_distance
            protection_ceiling = None
            if (
                spl_capability.declared_headroom_db is not None
                and spl_capability.headroom_reference_level_db_spl is not None
            ):
                protection_ceiling = (
                    float(spl_capability.headroom_reference_level_db_spl)
                    + float(spl_capability.declared_headroom_db)
                ) + 20.0 * log10(
                    float(spl_capability.reference_distance_m)
                    / float(scenario.listening_distance_m)
                )
                usable = min(usable, protection_ceiling)
            spl_ceiling = _scalar(
                'available', value=usable, unit='dB SPL'
            )
            margin_db = usable - float(scenario.target_spl_db_spl)
            spl_margin = _scalar('available', value=margin_db, unit='dB')
            if margin_db < 0.0:
                if (
                    protection_ceiling is not None
                    and usable == protection_ceiling
                    and ceiling_at_distance >= float(scenario.target_spl_db_spl)
                ):
                    _fail(
                        'protection_engagement',
                        'target SPL exceeds the declared limiter/protection '
                        'ceiling while the raw driver ceiling still holds',
                    )
                else:
                    _fail(
                        'loudspeaker_compression_limit',
                        'target SPL exceeds the speaker evidenced acoustic '
                        'output ceiling at the listening distance',
                    )
    else:
        _fail(
            'insufficient_evidence',
            'speaker acoustic output capability is not evidenced',
        )

    # --- 8. digital headroom ---------------------------------------------
    if (
        scenario.digital_headroom_db is not None
        and scenario.eq_boost_db > float(scenario.digital_headroom_db)
    ):
        _fail(
            'digital_headroom_limit',
            'EQ boost exceeds the declared digital headroom — upstream '
            'trim/DSP clips before the power stage',
        )

    # --- verdict / evidence class ----------------------------------------
    ordered_failures = tuple(
        code for code in _FAILURE_ORDER if code in failures
    )
    real_failures = [
        code for code in ordered_failures if code != 'insufficient_evidence'
    ]
    if real_failures:
        verdict: QualificationVerdict = 'unqualified'
    elif 'insufficient_evidence' in ordered_failures:
        verdict = 'indeterminate'
    else:
        verdict = 'qualified'
    # Dominant limiter = the most causal failure domain present; the
    # ordering is fixed so composite failures report one stable answer.
    limiter: DominantLimiter = 'unknown'
    for code in (
        'multichannel_power_limit',
        'load_below_amplifier_rating',
        'thermal_derating',
        'amplifier_clipping',
        'current_margin_insufficient',
        'voltage_margin_insufficient',
        'cable_loss_excessive',
        'loudspeaker_compression_limit',
        'protection_engagement',
        'digital_headroom_limit',
    ):
        if code in failures:
            limiter = {
                'multichannel_power_limit': 'multichannel',
                'load_below_amplifier_rating': 'load_domain',
                'thermal_derating': 'thermal',
                'amplifier_clipping': 'voltage',
                'current_margin_insufficient': 'current',
                'voltage_margin_insufficient': 'voltage',
                'cable_loss_excessive': 'cable',
                'loudspeaker_compression_limit': 'speaker_compression',
                'protection_engagement': 'protection',
                'digital_headroom_limit': 'digital',
            }[code]
            break
    if limiter == 'unknown' and not real_failures:
        limiter = 'none' if verdict == 'qualified' else 'unknown'

    if scenario.commissioning_evidence_ref is not None:
        capability_class: CapabilityEvidenceClass = 'in_room_commissioned'
    elif active:
        capability_class = (
            'acoustic_output_measured'
            if spl_capability is not None
            and spl_capability.provenance.evidence_kind == 'measured'
            else 'spec_sheet_estimate'
        )
    elif (
        spl_capability is not None
        and spl_capability.provenance.evidence_kind == 'measured'
        and not real_failures
    ):
        capability_class = 'acoustic_output_measured'
    elif (
        impedance is not None
        and impedance.tier != 'nominal_impedance_only'
        and amplifier_capability is not None
        and required_input.state == 'available'
    ):
        capability_class = 'electrically_qualified_model'
    else:
        capability_class = 'spec_sheet_estimate'

    payload = {
        'schema_version': ELECTRICAL_QUALIFICATION_SCHEMA_VERSION,
        'authority_version': ELECTRICAL_QUALIFICATION_AUTHORITY_VERSION,
        'document_id': scenario.document_id,
        'scenario': scenario.model_dump(mode='json'),
        'required_input': required_input.model_dump(mode='json'),
        'required_terminal_voltage_v_rms': required_v.model_dump(mode='json'),
        'amplifier_output_ceiling': ceiling.model_dump(mode='json'),
        'delivered_terminal_voltage_v_rms': delivered_v.model_dump(mode='json'),
        'cable_loss_db': cable_loss.model_dump(mode='json'),
        'worst_current_demand_a': worst_current.model_dump(mode='json'),
        'current_headroom_db': current_headroom.model_dump(mode='json'),
        'voltage_margin_v_rms': voltage_margin.model_dump(mode='json'),
        'speaker_spl_ceiling_db_spl': spl_ceiling.model_dump(mode='json'),
        'spl_margin_db': spl_margin.model_dump(mode='json'),
        'minimum_load_ohm': minimum_load.model_dump(mode='json'),
        'failure_codes': list(ordered_failures),
        'dominant_limiter': limiter,
        'verdict': verdict,
        'capability_class': capability_class,
        'support_reasons': list(dict.fromkeys(reasons)),
        'evaluated_at_utc': evaluated_at_utc,
    }
    digest = _digest(payload)
    return ElectricalPlaybackQualification(
        document_id=scenario.document_id,
        scenario=scenario,
        required_input=required_input,
        required_terminal_voltage_v_rms=required_v,
        amplifier_output_ceiling=ceiling,
        delivered_terminal_voltage_v_rms=delivered_v,
        cable_loss_db=cable_loss,
        worst_current_demand_a=worst_current,
        current_headroom_db=current_headroom,
        voltage_margin_v_rms=voltage_margin,
        speaker_spl_ceiling_db_spl=spl_ceiling,
        spl_margin_db=spl_margin,
        minimum_load_ohm=minimum_load,
        failure_codes=ordered_failures,
        dominant_limiter=limiter,
        verdict=verdict,
        capability_class=capability_class,
        support_reasons=tuple(dict.fromkeys(reasons)),
        qualification_id=_semantic_id('elec-qual', digest),
        qualification_sha256=digest,
        evaluated_at_utc=evaluated_at_utc,
    )


__all__ = [
    'CapabilityEvidenceClass',
    'DominantLimiter',
    'DriveKind',
    'ELECTRICAL_QUALIFICATION_AUTHORITY_VERSION',
    'ELECTRICAL_QUALIFICATION_SCHEMA_VERSION',
    'ELECTRICAL_SCENARIO_AUTHORITY_VERSION',
    'ElectricalFailureCode',
    'ElectricalPlaybackQualification',
    'ElectricalQualificationScenario',
    'QualificationVerdict',
    'StressProfile',
    'build_electrical_scenario',
    'evaluate_electrical_compatibility',
]
