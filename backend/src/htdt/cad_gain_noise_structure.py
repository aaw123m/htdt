"""Gain-structure / noise-floor authority (#651, REV58-MEASELEC).

A playback chain can have adequate maximum output and correct channel
calibration while still wasting usable dynamic range: reference levels
mismatched between stages, electronics self-noise audible in a quiet
room, or an upstream stage clipping before the documented bottleneck.
``cad_gain_structure`` (#646) already owns the line-level capability
chain (per-stage max input/output and headroom evaluation); this module
owns what that chain does not — the *measured* level references, the
noise-floor evidence with its exact semantics, per-stage clipping-margin
qualification, and the end-to-end verdict that binds them:

- :class:`CadSignalLevelReference` — a sealed digital↔analog reference
  mapping (``X dBFS -> Y Vrms/dBu``) under the exact device/firmware /
  volume/trim / load / sample-rate / processing state. Never a global
  assumption.
- :class:`CadNoiseFloorObservation` — a sealed noise measurement keeping
  its full semantics: noise class (electrical chain vs loudspeaker
  self-noise vs acoustic room background vs hum/EMC vs mechanical),
  level + unit, weighting, bandwidth, input termination, load, gain
  state, instrument pin and measurement state pin.
- :class:`CadClippingMarginQualification` — sealed per-stage clip/limit
  evidence: typed mechanism (digital FS clip vs analog stage clip vs
  amplifier voltage/current limit vs dynamic limiter vs loudspeaker
  compression), threshold with level semantics, and the margin between
  nominal operating level and the limit.
- :class:`CadGainStructureQualification` — the fail-closed end-to-end
  verdict: SNR/dynamic-range claims only with an explicit reference
  numerator and noise condition; noise attributions only with class and
  stage evidence; measurement-floor-limited results reported as such.

Honesty rules baked in:

- ``SNR = 110 dB`` is never a value — the reference level, noise
  condition, weighting and bandwidth are part of the quantity.
- An XLR connector is not a balanced circuit or a professional nominal
  level — interface class follows declared/measured evidence.
- ``+6 dB`` of channel trim and ``+6 dB`` of amplifier gain are not
  operationally identical — margins are evaluated per stage, not at the
  chain ends.
- A noise reading within the analyzer's own floor + margin is
  ``measurement_floor_limited`` — bounded, never implausibly precise.
- Manufacturer declarations can seed design calculations but never
  satisfy an installed qualification that asks for measured state.

Literature / standards basis
----------------------------
- AES17-2020, *AES standard method for digital audio engineering —
  Measurement of digital audio equipment* (current published edition):
  exact FS/dBFS semantics, level definitions; AES17-R review work stays
  research-only until published.
- IEC 60268-3:2018 Ed.5, *Sound system equipment — Part 3: Amplifiers*
  (stability date 2027): amplifier characteristics and measurement
  methods — an equipment-measurement framework, not a universal
  residential gain-setting recipe.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


GNS_AUTHORITY_SCHEMA_VERSION = 'gain-noise-1'
GNS_EVALUATION_VERSION = 'gain-noise-eval-1'

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


def _require_positive(value: float, label: str) -> None:
    _require_finite(value, label)
    if value <= 0:
        raise ValueError(f'{label} must be positive')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#651)
# ---------------------------------------------------------------------------

AnalogLevelUnit = Literal['dbu', 'dbv', 'v_rms', 'unknown']

NoiseClass = Literal[
    'electrical_chain',
    'loudspeaker_self_noise',
    'acoustic_room_background',
    'hum_buzz_emc',
    'mechanical',
    'unknown',
]

NoiseWeighting = Literal[
    'a', 'c', 'z', 'itu_r_468', 'unweighted', 'other', 'unknown'
]

EvidenceClass = Literal[
    'manufacturer_declared',
    'device_readback',
    'electrically_measured',
    'acoustically_inferred',
    'user_entered',
    'unknown',
]

#: Evidence classes that can carry an installed-qualification claim.
_MEASURED_EVIDENCE: frozenset[EvidenceClass] = frozenset(
    {'electrically_measured', 'device_readback'}
)

ClipMechanism = Literal[
    'digital_full_scale_clip',
    'analog_stage_clip',
    'amplifier_voltage_current_limit',
    'dynamic_limiter_protection',
    'loudspeaker_compression_distortion',
    'unknown',
]

LevelSemantics = Literal['peak', 'rms', 'true_peak', 'unknown']

InterfaceClass = Literal[
    'balanced', 'unbalanced', 'internal_digital', 'unknown'
]

LoadStressClass = Literal[
    'single_channel',
    'multi_channel_typical',
    'multi_channel_stress',
    'lfe_bass_stress',
    'unknown',
]

GainStructureState = Literal[
    'gain_structure_qualified',
    'qualified_with_limitations',
    'noise_floor_unattributed',
    'clip_stage_unresolved',
    'measurement_floor_limited',
    'unqualified_insufficient_evidence',
]

GainNoiseCapability = Literal[
    'level_mapping_valid',
    'noise_floor_attributed',
    'snr_semantics_valid',
    'clip_stage_localized',
    'headroom_chain_valid',
    'measurement_floor_not_exceeded',
    'multi_channel_stress_valid',
]

ALL_GAIN_NOISE_CAPABILITIES: tuple[GainNoiseCapability, ...] = (
    'level_mapping_valid',
    'noise_floor_attributed',
    'snr_semantics_valid',
    'clip_stage_localized',
    'headroom_chain_valid',
    'measurement_floor_not_exceeded',
    'multi_channel_stress_valid',
)

CapabilityState = Literal['valid', 'limited', 'invalid', 'unknown']


# ---------------------------------------------------------------------------
# Embedded evidence blocks
# ---------------------------------------------------------------------------


class CadStageIdentity(BaseModel):
    """One signal-chain stage/interface identity for gain/noise
    attribution — a label plus enough context to keep the stage unique."""

    model_config = ConfigDict(frozen=True)

    stage_label: str = Field(min_length=1)
    device: str | None = None
    port: str | None = None
    interface_class: InterfaceClass = 'unknown'
    gain_db: float | None = None
    trim_db: float | None = None
    firmware: str | None = None

    @model_validator(mode='after')
    def valid_stage(self) -> 'CadStageIdentity':
        for label, value in (
            ('gain_db', self.gain_db),
            ('trim_db', self.trim_db),
        ):
            if value is not None:
                _require_finite(value, f'stage {label}')
        return self

    def has_device_identity(self) -> bool:
        """Whether the stage is identified beyond a bare label — a
        typed clip mechanism needs more than a free-text stage name."""
        return self.device is not None or self.port is not None


class CadSnrDeclaration(BaseModel):
    """An SNR / dynamic-range figure with its required semantics.

    A bare ratio is rejected: the reference (numerator) level, noise
    condition and weighting/bandwidth are what make it a quantity.
    """

    model_config = ConfigDict(frozen=True)

    value_db: float
    reference_level_dbfs_or_unit: str = Field(min_length=1)
    noise_condition: str = Field(min_length=1)
    weighting: NoiseWeighting = 'unknown'
    bandwidth_hz: float | None = None
    mute_state: str | None = None

    @model_validator(mode='after')
    def valid_snr(self) -> 'CadSnrDeclaration':
        _require_finite(self.value_db, 'snr value_db')
        if self.bandwidth_hz is not None:
            _require_positive(self.bandwidth_hz, 'snr bandwidth_hz')
        if self.weighting == 'unknown':
            raise ValueError(
                'an SNR/dynamic-range figure requires an explicit '
                'weighting — undeclared weighting silently mixes dBA, '
                'unweighted and ITU-R 468 figures'
            )
        return self


class CadStageMargin(BaseModel):
    """Per-stage margin between the nominal/programme level and the
    evidenced linear limit — never reduced to one chain-wide number."""

    model_config = ConfigDict(frozen=True)

    stage: CadStageIdentity
    nominal_level: float | None = None
    nominal_unit: str | None = None
    max_linear_level: float | None = None
    max_level_unit: str | None = None
    clip_threshold: float | None = None
    clip_threshold_unit: str | None = None
    clip_mechanism: ClipMechanism = 'unknown'
    headroom_db: float | None = None
    evidence_class: EvidenceClass = 'unknown'
    level_semantics: LevelSemantics = 'unknown'

    @model_validator(mode='after')
    def valid_margin(self) -> 'CadStageMargin':
        for label, value in (
            ('nominal_level', self.nominal_level),
            ('max_linear_level', self.max_linear_level),
            ('clip_threshold', self.clip_threshold),
            ('headroom_db', self.headroom_db),
        ):
            if value is not None:
                _require_finite(value, f'stage margin {label}')
        if self.nominal_level is not None and self.nominal_unit is None:
            raise ValueError('nominal level requires its unit')
        if (
            self.max_linear_level is not None
            and self.max_level_unit is None
        ):
            raise ValueError('max linear level requires its unit')
        if (
            self.clip_threshold is not None
            and self.clip_threshold_unit is None
        ):
            raise ValueError('clip threshold requires its unit')
        if self.clip_mechanism != 'unknown' and (
            self.clip_threshold is None
        ):
            raise ValueError(
                'a typed clip mechanism requires the threshold it was '
                'observed at — a bare mechanism label is not evidence'
            )
        return self

    def margin_vs_nominal_db(self) -> float | None:
        """Nominal-to-limit margin when both levels share a unit."""
        if (
            self.nominal_level is None
            or self.max_linear_level is None
            or self.nominal_unit is None
            or self.nominal_unit != self.max_level_unit
        ):
            return None
        return self.max_linear_level - self.nominal_level


# ---------------------------------------------------------------------------
# Sealed authorities
# ---------------------------------------------------------------------------


class CadSignalLevelReference(BaseModel):
    """Sealed digital↔analog level reference for one exact device state.

    The mapping between a digital test level and the analog level it
    produces (or expects) — bound to device, firmware, volume/trim, load,
    sample rate, signal frequency and processing mode. Changing any of
    them creates a different reference.
    """

    model_config = ConfigDict(frozen=True)

    reference_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    stage: CadStageIdentity
    digital_level_dbfs: float
    analog_level: float
    analog_unit: AnalogLevelUnit
    master_volume: str | None = None
    load_description: str | None = None
    sample_rate_hz: float | None = None
    signal_frequency_hz: float | None = None
    processing_mode: str | None = None
    evidence_class: EvidenceClass = 'unknown'
    uncertainty_db: float | None = None
    level_semantics: LevelSemantics = 'unknown'
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    reference_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_reference(self) -> 'CadSignalLevelReference':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        _require_finite(self.digital_level_dbfs, 'digital_level_dbfs')
        _require_finite(self.analog_level, 'analog_level')
        if self.analog_unit == 'unknown':
            raise ValueError(
                'an analog level reference requires a unit — an '
                'unlabeled number silently mixes dBu, dBV and V RMS'
            )
        if self.analog_unit == 'v_rms':
            _require_positive(self.analog_level, 'analog_level V RMS')
        if self.sample_rate_hz is not None:
            _require_positive(self.sample_rate_hz, 'sample_rate_hz')
        if self.signal_frequency_hz is not None:
            _require_positive(
                self.signal_frequency_hz, 'signal_frequency_hz'
            )
        if self.uncertainty_db is not None:
            _require_finite(self.uncertainty_db, 'uncertainty_db')
            if self.uncertainty_db < 0:
                raise ValueError('uncertainty_db must be non-negative')
        expected = _hash(self.identity_payload())
        if self.reference_sha256 != expected:
            raise ValueError('level reference hash mismatch')
        if self.reference_id != _semantic_id('lvlref', expected):
            raise ValueError('reference id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'stage': self.stage.model_dump(mode='json'),
            'digital_level_dbfs': self.digital_level_dbfs,
            'analog_level': self.analog_level,
            'analog_unit': self.analog_unit,
            'master_volume': self.master_volume,
            'load_description': self.load_description,
            'sample_rate_hz': self.sample_rate_hz,
            'signal_frequency_hz': self.signal_frequency_hz,
            'processing_mode': self.processing_mode,
            'evidence_class': self.evidence_class,
            'uncertainty_db': self.uncertainty_db,
            'level_semantics': self.level_semantics,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }


def level_reference_binding(
    reference: CadSignalLevelReference,
) -> AuthorityRef:
    return AuthorityRef(
        kind='signal_level_reference',
        ref_id=reference.reference_id,
        ref_sha256=reference.reference_sha256,
    )


class CadNoiseFloorObservation(BaseModel):
    """Sealed noise-floor evidence with complete measurement semantics.

    ``noise_class`` separates electrical chain noise from loudspeaker
    self-noise, acoustic room background (#580), hum/EMC (#606) and
    mechanical noise (#589) — an acoustic hiss measurement never
    reclassifies itself as an electrical-stage location.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    stage: CadStageIdentity | None = None
    noise_class: NoiseClass
    noise_level: float
    noise_unit: str = Field(min_length=1)
    weighting: NoiseWeighting = 'unknown'
    bandwidth_hz: float | None = None
    input_termination: str | None = None
    load_description: str | None = None
    gain_state: str | None = None
    mute_state: str | None = None
    instrument_ref: AuthorityRef | None = None
    state_ref: AuthorityRef | None = None
    evidence_class: EvidenceClass = 'unknown'
    analyzer_floor_level: float | None = None
    uncertainty_db: float | None = None
    observed_at_utc: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadNoiseFloorObservation':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.observed_at_utc is not None:
            _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        _require_finite(self.noise_level, 'noise_level')
        if self.bandwidth_hz is not None:
            _require_positive(self.bandwidth_hz, 'bandwidth_hz')
        for label, value in (
            ('analyzer_floor_level', self.analyzer_floor_level),
            ('uncertainty_db', self.uncertainty_db),
        ):
            if value is not None:
                _require_finite(value, label)
        if self.uncertainty_db is not None and self.uncertainty_db < 0:
            raise ValueError('uncertainty_db must be non-negative')
        if self.noise_class != 'unknown' and self.stage is None and (
            self.noise_class == 'electrical_chain'
        ):
            # An electrical-chain claim without a stage is allowed only
            # as an unattributed system-level observation.
            pass
        for label, ref in (
            ('#611 instrument', self.instrument_ref),
            ('#573 measurement state', self.state_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'the {label} pin must carry its sha256')
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('noise observation hash mismatch')
        if self.observation_id != _semantic_id('gnobs', expected):
            raise ValueError('observation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'stage': (
                self.stage.model_dump(mode='json')
                if self.stage is not None
                else None
            ),
            'noise_class': self.noise_class,
            'noise_level': self.noise_level,
            'noise_unit': self.noise_unit,
            'weighting': self.weighting,
            'bandwidth_hz': self.bandwidth_hz,
            'input_termination': self.input_termination,
            'load_description': self.load_description,
            'gain_state': self.gain_state,
            'mute_state': self.mute_state,
            'instrument_ref': (
                self.instrument_ref.model_dump(mode='json')
                if self.instrument_ref is not None
                else None
            ),
            'state_ref': (
                self.state_ref.model_dump(mode='json')
                if self.state_ref is not None
                else None
            ),
            'evidence_class': self.evidence_class,
            'analyzer_floor_level': self.analyzer_floor_level,
            'uncertainty_db': self.uncertainty_db,
            'observed_at_utc': self.observed_at_utc,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }

    def measurement_floor_limited(self) -> bool:
        """Whether the analyzer floor is too close to the reading for a
        precise claim (within 6 dB or the reading is at/below it)."""
        if self.analyzer_floor_level is None:
            return False
        return self.noise_level - self.analyzer_floor_level < 6.0


def noise_observation_binding(
    observation: CadNoiseFloorObservation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='noise_floor_observation',
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )


class CadClippingMarginQualification(BaseModel):
    """Sealed per-stage clipping/overload evidence with a typed
    mechanism — a generic 'system clipping' label is never produced."""

    model_config = ConfigDict(frozen=True)

    margin_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    stage: CadStageIdentity
    clip_mechanism: ClipMechanism
    threshold_level: float | None = None
    threshold_unit: str | None = None
    level_semantics: LevelSemantics = 'unknown'
    nominal_level: float | None = None
    nominal_unit: str | None = None
    load_stress: LoadStressClass = 'unknown'
    evidence_class: EvidenceClass = 'unknown'
    observation_ref: AuthorityRef | None = None
    uncertainty_db: float | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    margin_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_margin(self) -> 'CadClippingMarginQualification':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        for label, value in (
            ('threshold_level', self.threshold_level),
            ('nominal_level', self.nominal_level),
            ('uncertainty_db', self.uncertainty_db),
        ):
            if value is not None:
                _require_finite(value, f'clipping margin {label}')
        if self.clip_mechanism != 'unknown' and (
            self.threshold_level is None
        ):
            raise ValueError(
                'a typed clip mechanism requires the threshold it was '
                'observed at — a bare mechanism label is not evidence'
            )
        if self.threshold_level is not None and (
            self.threshold_unit is None
        ):
            raise ValueError('clip threshold requires its unit')
        if self.nominal_level is not None and self.nominal_unit is None:
            raise ValueError('nominal level requires its unit')
        if self.uncertainty_db is not None and self.uncertainty_db < 0:
            raise ValueError('uncertainty_db must be non-negative')
        if self.observation_ref is not None and (
            self.observation_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the overload observation pin must carry its sha256'
            )
        expected = _hash(self.identity_payload())
        if self.margin_sha256 != expected:
            raise ValueError('clipping margin hash mismatch')
        if self.margin_id != _semantic_id('clipm', expected):
            raise ValueError('margin id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'stage': self.stage.model_dump(mode='json'),
            'clip_mechanism': self.clip_mechanism,
            'threshold_level': self.threshold_level,
            'threshold_unit': self.threshold_unit,
            'level_semantics': self.level_semantics,
            'nominal_level': self.nominal_level,
            'nominal_unit': self.nominal_unit,
            'load_stress': self.load_stress,
            'evidence_class': self.evidence_class,
            'observation_ref': (
                self.observation_ref.model_dump(mode='json')
                if self.observation_ref is not None
                else None
            ),
            'uncertainty_db': self.uncertainty_db,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }

    def clipping_margin_db(self) -> float | None:
        """Nominal-to-clip margin when units agree."""
        if (
            self.nominal_level is None
            or self.threshold_level is None
            or self.nominal_unit is None
            or self.nominal_unit != self.threshold_unit
        ):
            return None
        return self.threshold_level - self.nominal_level


def clipping_margin_binding(
    margin: CadClippingMarginQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='clipping_margin_qualification',
        ref_id=margin.margin_id,
        ref_sha256=margin.margin_sha256,
    )


class CadGainStructureQualification(BaseModel):
    """Sealed fail-closed end-to-end gain-structure verdict for one
    declared use case."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    use_case: str = Field(min_length=1)
    level_reference_refs: tuple[AuthorityRef, ...] = ()
    noise_observation_refs: tuple[AuthorityRef, ...] = ()
    clipping_margin_refs: tuple[AuthorityRef, ...] = ()
    gain_structure_scenario_ref: AuthorityRef | None = None
    required_headroom_db: float | None = None
    requested_load_stress: LoadStressClass = 'unknown'
    state: GainStructureState
    limiting_stage_label: str | None = None
    capabilities: tuple[tuple[GainNoiseCapability, CapabilityState], ...]
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadGainStructureQualification':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if self.required_headroom_db is not None:
            _require_finite(
                self.required_headroom_db, 'required_headroom_db'
            )
            if self.required_headroom_db < 0:
                raise ValueError('required_headroom_db must be >= 0')
        covered = {capability for capability, _ in self.capabilities}
        if covered != set(ALL_GAIN_NOISE_CAPABILITIES):
            raise ValueError(
                'a gain-structure qualification must report every '
                'capability'
            )
        for label, refs in (
            ('level reference', self.level_reference_refs),
            ('noise observation', self.noise_observation_refs),
            ('clipping margin', self.clipping_margin_refs),
        ):
            for ref in refs:
                if ref.ref_sha256 is None:
                    raise ValueError(
                        f'{label} pins must carry their sha256'
                    )
        if self.gain_structure_scenario_ref is not None and (
            self.gain_structure_scenario_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the #646 gain-structure scenario pin must carry sha256'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('qualification hash mismatch')
        if self.qualification_id != _semantic_id('gnqual', expected):
            raise ValueError('qualification id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'use_case': self.use_case,
            'level_reference_refs': [
                r.model_dump(mode='json')
                for r in self.level_reference_refs
            ],
            'noise_observation_refs': [
                r.model_dump(mode='json')
                for r in self.noise_observation_refs
            ],
            'clipping_margin_refs': [
                r.model_dump(mode='json')
                for r in self.clipping_margin_refs
            ],
            'gain_structure_scenario_ref': (
                self.gain_structure_scenario_ref.model_dump(mode='json')
                if self.gain_structure_scenario_ref is not None
                else None
            ),
            'required_headroom_db': self.required_headroom_db,
            'requested_load_stress': self.requested_load_stress,
            'state': self.state,
            'limiting_stage_label': self.limiting_stage_label,
            'capabilities': [list(item) for item in self.capabilities],
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def capability_state(
        self, capability: GainNoiseCapability
    ) -> CapabilityState:
        return dict(self.capabilities)[capability]


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


def build_signal_level_reference(
    *,
    document_id: str,
    stage: CadStageIdentity,
    digital_level_dbfs: float,
    analog_level: float,
    analog_unit: AnalogLevelUnit,
    master_volume: str | None = None,
    load_description: str | None = None,
    sample_rate_hz: float | None = None,
    signal_frequency_hz: float | None = None,
    processing_mode: str | None = None,
    evidence_class: EvidenceClass = 'unknown',
    uncertainty_db: float | None = None,
    level_semantics: LevelSemantics = 'unknown',
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadSignalLevelReference:
    """Seal a digital↔analog level reference (#651 §3)."""
    payload = dict(
        document_id=document_id,
        stage=stage,
        digital_level_dbfs=digital_level_dbfs,
        analog_level=analog_level,
        analog_unit=analog_unit,
        master_volume=master_volume,
        load_description=load_description,
        sample_rate_hz=sample_rate_hz,
        signal_frequency_hz=signal_frequency_hz,
        processing_mode=processing_mode,
        evidence_class=evidence_class,
        uncertainty_db=uncertainty_db,
        level_semantics=level_semantics,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=GNS_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadSignalLevelReference, payload,
        'reference_id', 'reference_sha256', 'lvlref',
    )


def build_noise_floor_observation(
    *,
    document_id: str,
    noise_class: NoiseClass,
    noise_level: float,
    noise_unit: str,
    stage: CadStageIdentity | None = None,
    weighting: NoiseWeighting = 'unknown',
    bandwidth_hz: float | None = None,
    input_termination: str | None = None,
    load_description: str | None = None,
    gain_state: str | None = None,
    mute_state: str | None = None,
    instrument_ref: AuthorityRef | None = None,
    state_ref: AuthorityRef | None = None,
    evidence_class: EvidenceClass = 'unknown',
    analyzer_floor_level: float | None = None,
    uncertainty_db: float | None = None,
    observed_at_utc: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadNoiseFloorObservation:
    """Seal a noise-floor observation (#651 §5)."""
    payload = dict(
        document_id=document_id,
        stage=stage,
        noise_class=noise_class,
        noise_level=noise_level,
        noise_unit=noise_unit,
        weighting=weighting,
        bandwidth_hz=bandwidth_hz,
        input_termination=input_termination,
        load_description=load_description,
        gain_state=gain_state,
        mute_state=mute_state,
        instrument_ref=instrument_ref,
        state_ref=state_ref,
        evidence_class=evidence_class,
        analyzer_floor_level=analyzer_floor_level,
        uncertainty_db=uncertainty_db,
        observed_at_utc=observed_at_utc,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=GNS_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadNoiseFloorObservation, payload,
        'observation_id', 'observation_sha256', 'gnobs',
    )


def build_clipping_margin(
    *,
    document_id: str,
    stage: CadStageIdentity,
    clip_mechanism: ClipMechanism,
    threshold_level: float | None = None,
    threshold_unit: str | None = None,
    level_semantics: LevelSemantics = 'unknown',
    nominal_level: float | None = None,
    nominal_unit: str | None = None,
    load_stress: LoadStressClass = 'unknown',
    evidence_class: EvidenceClass = 'unknown',
    observation_ref: AuthorityRef | None = None,
    uncertainty_db: float | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadClippingMarginQualification:
    """Seal per-stage clipping-margin evidence (#651 §8/§9)."""
    payload = dict(
        document_id=document_id,
        stage=stage,
        clip_mechanism=clip_mechanism,
        threshold_level=threshold_level,
        threshold_unit=threshold_unit,
        level_semantics=level_semantics,
        nominal_level=nominal_level,
        nominal_unit=nominal_unit,
        load_stress=load_stress,
        evidence_class=evidence_class,
        observation_ref=observation_ref,
        uncertainty_db=uncertainty_db,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=GNS_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadClippingMarginQualification, payload,
        'margin_id', 'margin_sha256', 'clipm',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_gain_structure(
    *,
    document_id: str,
    use_case: str,
    level_references: tuple[CadSignalLevelReference, ...]
    | list[CadSignalLevelReference] = (),
    noise_observations: tuple[CadNoiseFloorObservation, ...]
    | list[CadNoiseFloorObservation] = (),
    clipping_margins: tuple[CadClippingMarginQualification, ...]
    | list[CadClippingMarginQualification] = (),
    stage_margins: tuple[CadStageMargin, ...]
    | list[CadStageMargin] = (),
    gain_structure_scenario_ref: AuthorityRef | None = None,
    required_headroom_db: float | None = None,
    requested_load_stress: LoadStressClass = 'unknown',
    noise_target: CadSnrDeclaration | None = None,
    evaluated_at_utc: str | None = None,
) -> CadGainStructureQualification:
    """Fail-closed end-to-end gain/noise/headroom verdict.

    Derivation order: evidence coverage → noise attribution → clipping
    margin → measurement floor → load-stress scope. A chain qualified at
    one channel state never silently covers multi-channel/LFE stress.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    caps: dict[GainNoiseCapability, CapabilityState] = {
        capability: 'unknown'
        for capability in ALL_GAIN_NOISE_CAPABILITIES
    }

    level_references = tuple(level_references)
    noise_observations = tuple(noise_observations)
    clipping_margins = tuple(clipping_margins)
    stage_margins = tuple(stage_margins)

    # --- level mapping ----------------------------------------------------
    measured_maps = [
        r for r in level_references
        if r.evidence_class in _MEASURED_EVIDENCE
    ]
    if not level_references:
        caps['level_mapping_valid'] = 'invalid'
        reasons.append(
            'no digital↔analog level reference — dBFS↔dBu/V is never '
            'assumed globally'
        )
    elif measured_maps:
        caps['level_mapping_valid'] = 'valid'
    else:
        caps['level_mapping_valid'] = 'limited'
        reasons.append(
            'level references rest on declared/user-entered evidence only'
        )

    # --- noise attribution --------------------------------------------------
    unattributed = [
        o for o in noise_observations
        if o.noise_class in ('electrical_chain', 'unknown')
        and o.stage is None
    ]
    electrical = [
        o for o in noise_observations
        if o.noise_class == 'electrical_chain' and o.stage is not None
    ]
    floor_limited = [
        o for o in noise_observations if o.measurement_floor_limited()
    ]
    if not noise_observations:
        caps['noise_floor_attributed'] = 'invalid'
        reasons.append('no noise-floor observations bound')
    elif unattributed:
        caps['noise_floor_attributed'] = 'limited'
        reasons.append(
            'electrical noise observed without a stage attribution — '
            'audible hiss is evidence, stage location still needs '
            'chain-level evidence'
        )
    else:
        caps['noise_floor_attributed'] = (
            'valid' if electrical else 'limited'
        )
        if not electrical:
            reasons.append(
                'noise observations carry no electrical-chain stage '
                'evidence'
            )

    # --- SNR semantics -------------------------------------------------------
    if noise_target is None:
        caps['snr_semantics_valid'] = 'unknown'
    else:
        matching = [
            o for o in noise_observations
            if o.weighting == noise_target.weighting
        ]
        caps['snr_semantics_valid'] = (
            'valid' if matching else 'limited'
        )
        if not matching:
            reasons.append(
                'the declared SNR reference has no observation at the '
                'same weighting — the numerator/denominator semantics '
                'must match'
            )

    # --- clipping margin ------------------------------------------------------
    clipped = [
        m for m in clipping_margins
        if m.clip_mechanism != 'unknown'
    ]
    under_margined = [
        m for m in clipping_margins
        if m.clipping_margin_db() is not None
        and required_headroom_db is not None
        and m.clipping_margin_db() < required_headroom_db
    ]
    unknown_stage_clips = [
        m for m in clipping_margins
        if m.clip_mechanism != 'unknown'
        and not m.stage.has_device_identity()
    ]
    if not clipping_margins and not stage_margins:
        caps['clip_stage_localized'] = 'invalid'
        reasons.append(
            'no clipping-margin evidence — a clean capture never proves '
            'headroom'
        )
    elif unknown_stage_clips:
        caps['clip_stage_localized'] = 'invalid'
        reasons.append(
            'clipping observed without a stage identity — never emit a '
            'generic system-clipping diagnosis'
        )
    else:
        caps['clip_stage_localized'] = (
            'valid' if clipped else 'limited'
        )

    # --- headroom chain --------------------------------------------------------
    margins_db = [
        (m.stage.stage_label, m.margin_vs_nominal_db())
        for m in stage_margins
    ]
    computable = [(s, v) for s, v in margins_db if v is not None]
    if not stage_margins and gain_structure_scenario_ref is None:
        caps['headroom_chain_valid'] = 'invalid'
        reasons.append(
            'no stage-margin evidence and no #646 scenario pin — the '
            'limiting stage is unknown'
        )
    elif stage_margins and not computable:
        caps['headroom_chain_valid'] = 'limited'
        reasons.append(
            'stage margins exist but none compute against the nominal '
            'unit — per-stage units must agree'
        )
    else:
        worst = min(computable, key=lambda item: item[1])[0] if (
            computable
        ) else None
        if required_headroom_db is not None and computable:
            deficient = [
                s for s, v in computable if v < required_headroom_db
            ]
            caps['headroom_chain_valid'] = (
                'invalid' if deficient else 'valid'
            )
            if deficient:
                reasons.append(
                    f'stage(s) {deficient} margin below the required '
                    f'{required_headroom_db} dB'
                )
        else:
            caps['headroom_chain_valid'] = 'valid'
        if worst is not None:
            reasons.append(f'limiting stage: {worst}')

    # --- measurement floor ------------------------------------------------------
    if not noise_observations:
        caps['measurement_floor_not_exceeded'] = 'unknown'
    elif floor_limited:
        caps['measurement_floor_not_exceeded'] = 'invalid'
        reasons.append(
            'one or more noise readings sit inside the analyzer floor — '
            'report bounded results, not implausible precision'
        )
    else:
        caps['measurement_floor_not_exceeded'] = 'valid'

    # --- load stress ------------------------------------------------------------
    if requested_load_stress in (
        'multi_channel_stress', 'lfe_bass_stress',
    ):
        stress_evidence = any(
            m.load_stress == requested_load_stress
            for m in clipping_margins
        )
        caps['multi_channel_stress_valid'] = (
            'valid' if stress_evidence else 'invalid'
        )
        if not stress_evidence:
            reasons.append(
                f'no clipping evidence under {requested_load_stress} — '
                'single-channel margin never describes the whole device'
            )
    elif requested_load_stress == 'multi_channel_typical':
        caps['multi_channel_stress_valid'] = (
            'valid'
            if any(
                m.load_stress in (
                    'multi_channel_typical', 'multi_channel_stress'
                )
                for m in clipping_margins
            )
            else 'limited'
        )
    else:
        caps['multi_channel_stress_valid'] = 'limited'

    # --- state -------------------------------------------------------------------
    if not level_references and not noise_observations and (
        not clipping_margins
    ):
        state: GainStructureState = 'unqualified_insufficient_evidence'
        reasons.append('no gain/noise/margin evidence bound at all')
    elif floor_limited:
        state = 'measurement_floor_limited'
    elif unknown_stage_clips:
        state = 'clip_stage_unresolved'
    elif unattributed:
        state = 'noise_floor_unattributed'
    elif (
        caps['level_mapping_valid'] == 'valid'
        and caps['noise_floor_attributed'] == 'valid'
        and caps['clip_stage_localized'] == 'valid'
        and caps['headroom_chain_valid'] == 'valid'
    ):
        state = 'gain_structure_qualified'
    else:
        state = 'qualified_with_limitations'

    payload = dict(
        document_id=document_id,
        use_case=use_case,
        level_reference_refs=tuple(
            level_reference_binding(r) for r in level_references
        ),
        noise_observation_refs=tuple(
            noise_observation_binding(o) for o in noise_observations
        ),
        clipping_margin_refs=tuple(
            clipping_margin_binding(m) for m in clipping_margins
        ),
        gain_structure_scenario_ref=gain_structure_scenario_ref,
        required_headroom_db=required_headroom_db,
        requested_load_stress=requested_load_stress,
        state=state,
        limiting_stage_label=(
            min(computable, key=lambda item: item[1])[0]
            if computable
            else None
        ),
        capabilities=tuple(
            (capability, caps[capability])
            for capability in ALL_GAIN_NOISE_CAPABILITIES
        ),
        reasons=tuple(reasons),
        evaluation_version=GNS_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadGainStructureQualification, payload,
        'qualification_id', 'qualification_sha256', 'gnqual',
    )


__all__ = [
    'ALL_GAIN_NOISE_CAPABILITIES',
    'AnalogLevelUnit',
    'CadClippingMarginQualification',
    'CadGainStructureQualification',
    'CadNoiseFloorObservation',
    'CadSignalLevelReference',
    'CadSnrDeclaration',
    'CadStageIdentity',
    'CadStageMargin',
    'CapabilityState',
    'ClipMechanism',
    'EvidenceClass',
    'GNS_AUTHORITY_SCHEMA_VERSION',
    'GNS_EVALUATION_VERSION',
    'GainNoiseCapability',
    'GainStructureState',
    'InterfaceClass',
    'LevelSemantics',
    'LoadStressClass',
    'NoiseClass',
    'NoiseWeighting',
    'build_clipping_margin',
    'build_noise_floor_observation',
    'build_signal_level_reference',
    'clipping_margin_binding',
    'evaluate_gain_structure',
    'level_reference_binding',
    'noise_observation_binding',
]
