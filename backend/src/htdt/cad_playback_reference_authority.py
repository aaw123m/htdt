"""Playback reference-calibration authority (#618, REV57-INST).

``reference level``, ``test-tone level``, ``channel trim``, measured
``dBC``, ``LFE +10 dB`` and ``maximum clean capability`` are different
quantities. Collapsing them into one calibration number makes every
downstream headroom, bass and listening-level recommendation
numerically polished but wrong. This module keeps them distinct:

- :class:`CadReferenceProfile` — the exact calibration profile
  identity (SMPTE cinema reference, SMPTE RP 2096-1 baseline,
  ITU-R BS.775-4 LFE semantics, Dolby provider profile, CEDIA/RP22
  project profile, device-vendor internal calibration, project
  defined, diagnostic only), each pinned to publisher/document/
  revision, scope, required stimulus, measured quantity and known
  limitations. Cinema, broadcast, music-studio and residential
  assumptions never share one ``reference`` preset.
- :class:`CadCalibrationStimulus` — the exact stimulus identity:
  external #608 asset (asset/generator id, digital level dBFS,
  spectrum/bandwidth, channel routing, LFE/main identity) or a
  device-internal test signal whose semantics stay
  provider/version-specific and UNKNOWN when undocumented.
- :class:`CadChannelCalibrationObservation` — one measured channel
  state: acoustic SPL is bound to weighting, bandwidth, time
  weighting, integration quantity, instrument (#611), position
  (#581) and room/device state — ``85 dB`` without those is not a
  reproducible measurement.
- :class:`CadReferenceCalibrationQualification` +
  :func:`evaluate_reference_calibration` — the fail-closed verdict.
  Axes stay independent: stimulus authority, measurement semantics,
  LFE semantics, per-channel alignment and capability separation.

Honesty rules baked in:

- LFE ``+10 dB`` is an *in-band reproduction gain* relationship
  (ITU-R BS.775-4 Annex 7: -10 dB recording offset compensated by
  +10 dB reproduction gain) — never a command that a broadband SPL
  meter must read exactly 10 dB above a main channel.
- Native LFE content, redirected main-channel bass and summed
  subwoofer output stay separate signal classes (#574 composition);
  the +10 dB semantics never apply to redirected bass merely because
  both reach the subwoofer.
- Reference alignment, maximum clean capability (#579/#593) and
  everyday listening level are independent authorities — a calibrated
  channel is not proven capable of programme peaks, and a calibrated
  reference is not a recommended daily volume (#602 exposure).
- Level alignment, delay/time alignment, spectral EQ and bass
  management are distinct operations even when one guided workflow
  performs them together.
- A device master-volume ``0.0 dB`` marker is device-relative state —
  never a substitute for measured acoustic reference.
- Programme loudness / normalization stays #607 authority — different
  masters never trigger a trim change.

Literature basis
----------------
- SMPTE RP 200:2012 — cinema ``-20 dBFS -> 85 dBC`` reference chain.
- SMPTE RP 2096-1:2017 — cinema sound-system baseline calibration
  process.
- ITU-R BS.775-4 (12/2022) Annex 7 — LFE -10 dB recording offset /
  +10 dB reproduction gain; LFE vs domestic bass-management
  distinction.
- Dolby professional guidance — +10 dB in-band LFE gain vs broadband
  meter difference (typically ~4-6 dB depending on LFE bandwidth);
  Atmos Music 89-91.5 dBC example window at 85 dBC mains.
- CEDIA 2025 white paper — theatrical reference is a production/
  calibration standard, not a universal domestic listening target;
  RP22 levels describe capability/headroom, not mandatory use level.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


REFCAL_SCHEMA_VERSION = 'refcal-1'
REFCAL_EVALUATION_VERSION = 'refcal-eval-1'

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
# Taxonomies (#618)
# ---------------------------------------------------------------------------

CalibrationQuantity = Literal[
    'digital_test_signal_level_dbfs',
    'electrical_reference_level',
    'acoustic_channel_reference_spl',
    'lfe_in_band_reproduction_gain',
    'broadband_meter_spl',
    'channel_trim',
    'master_volume',
    'device_output_gain',
    'calibration_reference_state',
    'system_max_clean_capability',
    'user_typical_listening_level',
    'programme_loudness',
]
"""The quantity taxonomy — never collapsed into one calibration
number. No implicit conversion without an exact calibrated chain."""

ProfileKind = Literal[
    'smpte_cinema_reference',
    'smpte_rp2096_baseline',
    'itu_bs775_lfe',
    'dolby_provider_profile',
    'cedia_rp22_project_profile',
    'device_vendor_internal_calibration',
    'project_defined',
    'diagnostic_only',
]
"""Exact profile identities — cinema, broadcast, music-studio and
residential assumptions never share one preset."""

StimulusSourceKind = Literal[
    'external_registry_asset',
    'external_file',
    'device_internal_test',
    'generator',
    'unknown',
]
"""A ``device_internal_test`` stimulus is provider/firmware behavior —
its digital level and processing path stay UNKNOWN until documented."""

SignalClass = Literal[
    'main_channel',
    'native_lfe',
    'redirected_bass',
    'summed_subwoofer_output',
    'unknown',
]
"""Native LFE, redirected bass and the summed subwoofer output remain
separate — LFE offset semantics never apply to redirected bass just
because both reach the sub (#574 composition)."""

WeightingKind = Literal['c', 'z', 'a', 'other', 'unknown']
TimeWeighting = Literal['slow', 'fast', 'integrating', 'other', 'unknown']
IntegrationQuantity = Literal[
    'rms', 'leq', 'peak', 'lzpeak', 'other', 'unknown'
]

OperationKind = Literal[
    'level_alignment',
    'delay_time_alignment',
    'spectral_eq',
    'bass_management',
]
"""Distinct guided-workflow operations — a trim is never used to
compensate a spectral/null problem."""

StimulusAuthorityState = Literal[
    'exact_stimulus_bound',
    'device_internal_documented',
    'device_internal_undocumented',
    'unbound',
]

MeasurementSemanticsState = Literal[
    'semantics_complete',
    'semantics_incomplete',
    'no_measurements',
]

LfeSemanticState = Literal[
    'not_applicable',
    'in_band_gain_declared',
    'in_band_gain_verified',
    'meter_delta_only_not_proof',
    'redirected_bass_isolated',
]

AlignmentState = Literal[
    'aligned',
    'misaligned',
    'unverifiable',
    'not_evaluated',
]

CapabilitySeparation = Literal[
    'capability_evaluated_separately',
    'capability_not_evaluated',
]

RefCalVerdict = Literal[
    'reference_calibrated',
    'calibrated_with_limitations',
    'device_profile_only',
    'insufficient_evidence',
    'failed',
]


# ---------------------------------------------------------------------------
# Reference profile
# ---------------------------------------------------------------------------


class CadReferenceProfile(BaseModel):
    """The exact calibration/reference profile identity.

    Pins publisher/document/revision, applicable scope/environment,
    stimulus requirements, measured quantity, target semantics and
    limitations. ``profile_document`` is required — a profile without
    its exact source is not a profile.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_kind: ProfileKind
    label: str | None = None
    publisher: str | None = None
    profile_document: str = Field(min_length=1)
    document_revision: str | None = None
    lifecycle_state: Literal[
        'published_current', 'draft_research_only', 'superseded',
        'unknown',
    ] = 'unknown'
    scope: str | None = None
    required_stimulus: str | None = None
    measured_quantity: str | None = None
    target_semantics: str | None = None
    target_spl_dbc: float | None = None
    lfe_in_band_gain_db: float | None = None
    limitations: str | None = None
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadReferenceProfile':
        _require_iso8601(
            self.declared_at_utc, 'profile declared_at_utc'
        )
        for label, value in (
            ('target_spl_dbc', self.target_spl_dbc),
            ('lfe_in_band_gain_db', self.lfe_in_band_gain_db),
        ):
            if value is not None:
                _require_finite(value, f'profile {label}')
        if self.lifecycle_state == 'draft_research_only' and (
            self.target_spl_dbc is not None
            or self.lfe_in_band_gain_db is not None
        ):
            raise ValueError(
                'a draft/research-only profile never carries final '
                'thresholds — developing SMPTE B-chain work stays '
                'research-only until published (#599)'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('reference profile hash mismatch')
        if self.profile_id != _semantic_id('refprof', expected):
            raise ValueError('profile id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_kind': self.profile_kind,
            'label': self.label,
            'publisher': self.publisher,
            'profile_document': self.profile_document,
            'document_revision': self.document_revision,
            'lifecycle_state': self.lifecycle_state,
            'scope': self.scope,
            'required_stimulus': self.required_stimulus,
            'measured_quantity': self.measured_quantity,
            'target_semantics': self.target_semantics,
            'target_spl_dbc': self.target_spl_dbc,
            'lfe_in_band_gain_db': self.lfe_in_band_gain_db,
            'limitations': self.limitations,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def reference_profile_binding(
    profile: CadReferenceProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='reference_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


# ---------------------------------------------------------------------------
# Calibration stimulus
# ---------------------------------------------------------------------------


class CadCalibrationStimulus(BaseModel):
    """The exact stimulus identity for a calibration run.

    An external asset pins its #608 registry identity (asset id +
    sha) plus the digital level and spectrum it was generated at. A
    device-internal test signal pins device/firmware — its digital
    level, processing bypass state and bass-management behavior stay
    UNKNOWN until provider-documented, and such a stimulus can never
    claim a standardized reference.
    """

    model_config = ConfigDict(frozen=True)

    stimulus_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_kind: StimulusSourceKind
    asset_ref: AuthorityRef | None = None
    generator_identity: str | None = None
    digital_level_dbfs: float | None = None
    spectrum_description: str | None = None
    bandwidth_hz_low: float | None = None
    bandwidth_hz_high: float | None = None
    crest_factor_db: float | None = None
    channel_routing: str | None = None
    signal_class: SignalClass = 'unknown'
    device_identity: str | None = None
    device_firmware: str | None = None
    processing_bypass_state: Literal[
        'bypassed', 'included', 'partial', 'unknown'
    ] = 'unknown'
    bass_management_state: Literal[
        'engaged', 'bypassed', 'not_applicable', 'unknown'
    ] = 'unknown'
    master_volume_dependency: Literal[
        'dependent', 'independent', 'unknown'
    ] = 'unknown'
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    stimulus_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_stimulus(self) -> 'CadCalibrationStimulus':
        _require_iso8601(
            self.declared_at_utc, 'stimulus declared_at_utc'
        )
        if self.asset_ref is not None and (
            self.asset_ref.ref_sha256 is None
        ):
            raise ValueError('asset_ref must pin its sha256')
        for label, value in (
            ('digital_level_dbfs', self.digital_level_dbfs),
            ('bandwidth_hz_low', self.bandwidth_hz_low),
            ('bandwidth_hz_high', self.bandwidth_hz_high),
            ('crest_factor_db', self.crest_factor_db),
        ):
            if value is not None:
                _require_finite(value, f'stimulus {label}')
        if (
            self.bandwidth_hz_low is not None
            and self.bandwidth_hz_high is not None
            and self.bandwidth_hz_low >= self.bandwidth_hz_high
        ):
            raise ValueError('stimulus bandwidth range is inverted')
        if self.source_kind == 'external_registry_asset' and (
            self.asset_ref is None
        ):
            raise ValueError(
                'an external registry stimulus must pin its #608 '
                'asset identity'
            )
        if self.source_kind == 'device_internal_test' and (
            self.device_identity is None
        ):
            raise ValueError(
                'a device-internal test stimulus must name the exact '
                'device — provider test tones are device behavior'
            )
        expected = _hash(self.identity_payload())
        if self.stimulus_sha256 != expected:
            raise ValueError('calibration stimulus hash mismatch')
        if self.stimulus_id != _semantic_id('refstim', expected):
            raise ValueError('stimulus id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'source_kind': self.source_kind,
            'asset_ref': (
                self.asset_ref.model_dump(mode='json')
                if self.asset_ref is not None else None
            ),
            'generator_identity': self.generator_identity,
            'digital_level_dbfs': self.digital_level_dbfs,
            'spectrum_description': self.spectrum_description,
            'bandwidth_hz_low': self.bandwidth_hz_low,
            'bandwidth_hz_high': self.bandwidth_hz_high,
            'crest_factor_db': self.crest_factor_db,
            'channel_routing': self.channel_routing,
            'signal_class': self.signal_class,
            'device_identity': self.device_identity,
            'device_firmware': self.device_firmware,
            'processing_bypass_state': self.processing_bypass_state,
            'bass_management_state': self.bass_management_state,
            'master_volume_dependency': self.master_volume_dependency,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def standardized(self) -> bool:
        """True only when the stimulus carries documented authority —
        a device-internal tone with undocumented semantics is not a
        standardized reference stimulus."""
        if self.source_kind == 'device_internal_test':
            return self.digital_level_dbfs is not None
        return self.source_kind in (
            'external_registry_asset',
            'external_file',
            'generator',
        ) and self.digital_level_dbfs is not None


def calibration_stimulus_binding(
    stimulus: CadCalibrationStimulus,
) -> AuthorityRef:
    return AuthorityRef(
        kind='calibration_stimulus',
        ref_id=stimulus.stimulus_id,
        ref_sha256=stimulus.stimulus_sha256,
    )


# ---------------------------------------------------------------------------
# Channel observation
# ---------------------------------------------------------------------------


class CadChannelCalibrationObservation(BaseModel):
    """One channel's measured calibration state.

    Every acoustic SPL binds weighting, bandwidth, time weighting,
    integration quantity, instrument, position and room/device state.
    ``quantity`` declares which taxonomy member the value represents —
    a broadband meter SPL and a channel reference SPL are different
    quantities even at equal readings.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    channel_role: str = Field(min_length=1)
    signal_class: SignalClass = 'main_channel'
    stimulus_ref: AuthorityRef | None = None
    quantity: CalibrationQuantity = 'acoustic_channel_reference_spl'
    measured_spl_db: float | None = None
    weighting: WeightingKind = 'unknown'
    bandwidth_hz_low: float | None = None
    bandwidth_hz_high: float | None = None
    time_weighting: TimeWeighting = 'unknown'
    integration_quantity: IntegrationQuantity = 'unknown'
    channel_trim_db: float | None = None
    master_volume_state: str | None = None
    device_output_gain_db: float | None = None
    lfe_in_band_gain_db: float | None = None
    broadband_meter_delta_db: float | None = None
    instrument_ref: AuthorityRef | None = None
    position_ref: AuthorityRef | None = None
    room_state_ref: AuthorityRef | None = None
    operations_applied: tuple[OperationKind, ...] = ()
    uncertainty_db: float | None = None
    measured_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadChannelCalibrationObservation':
        _require_iso8601(
            self.measured_at_utc, 'observation measured_at_utc'
        )
        for ref, label in (
            (self.stimulus_ref, 'stimulus_ref'),
            (self.instrument_ref, 'instrument_ref'),
            (self.position_ref, 'position_ref'),
            (self.room_state_ref, 'room_state_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        for label, value in (
            ('measured_spl_db', self.measured_spl_db),
            ('bandwidth_hz_low', self.bandwidth_hz_low),
            ('bandwidth_hz_high', self.bandwidth_hz_high),
            ('channel_trim_db', self.channel_trim_db),
            ('device_output_gain_db', self.device_output_gain_db),
            ('lfe_in_band_gain_db', self.lfe_in_band_gain_db),
            ('broadband_meter_delta_db', self.broadband_meter_delta_db),
            ('uncertainty_db', self.uncertainty_db),
        ):
            if value is not None:
                _require_finite(value, f'observation {label}')
        if self.measured_spl_db is not None:
            missing = []
            if self.weighting == 'unknown':
                missing.append('weighting')
            if self.time_weighting == 'unknown':
                missing.append('time_weighting')
            if self.integration_quantity == 'unknown':
                missing.append('integration_quantity')
            if missing:
                raise ValueError(
                    'an acoustic SPL measurement binds weighting, time '
                    'weighting and integration quantity — missing: '
                    + ', '.join(missing)
                )
        if (
            self.bandwidth_hz_low is not None
            and self.bandwidth_hz_high is not None
            and self.bandwidth_hz_low >= self.bandwidth_hz_high
        ):
            raise ValueError('observation bandwidth range is inverted')
        if (
            self.signal_class == 'redirected_bass'
            and self.lfe_in_band_gain_db is not None
        ):
            raise ValueError(
                'redirected bass never carries native-LFE reproduction '
                'gain — the +10 dB semantics apply to the LFE channel '
                'only (#574 composition)'
            )
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('channel observation hash mismatch')
        if self.observation_id != _semantic_id('refobs', expected):
            raise ValueError('observation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'channel_role': self.channel_role,
            'signal_class': self.signal_class,
            'stimulus_ref': (
                self.stimulus_ref.model_dump(mode='json')
                if self.stimulus_ref is not None else None
            ),
            'quantity': self.quantity,
            'measured_spl_db': self.measured_spl_db,
            'weighting': self.weighting,
            'bandwidth_hz_low': self.bandwidth_hz_low,
            'bandwidth_hz_high': self.bandwidth_hz_high,
            'time_weighting': self.time_weighting,
            'integration_quantity': self.integration_quantity,
            'channel_trim_db': self.channel_trim_db,
            'master_volume_state': self.master_volume_state,
            'device_output_gain_db': self.device_output_gain_db,
            'lfe_in_band_gain_db': self.lfe_in_band_gain_db,
            'broadband_meter_delta_db': self.broadband_meter_delta_db,
            'instrument_ref': (
                self.instrument_ref.model_dump(mode='json')
                if self.instrument_ref is not None else None
            ),
            'position_ref': (
                self.position_ref.model_dump(mode='json')
                if self.position_ref is not None else None
            ),
            'room_state_ref': (
                self.room_state_ref.model_dump(mode='json')
                if self.room_state_ref is not None else None
            ),
            'operations_applied': list(self.operations_applied),
            'uncertainty_db': self.uncertainty_db,
            'measured_at_utc': self.measured_at_utc,
            'provenance_json': self.provenance_json,
        }


def channel_observation_binding(
    observation: CadChannelCalibrationObservation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='channel_calibration_observation',
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------


class CadReferenceCalibrationQualification(BaseModel):
    """Sealed reference-calibration verdict.

    Stimulus authority, measurement semantics, LFE semantics and
    per-channel alignment stay independent. ``capability_separation``
    records that reference alignment never implies maximum clean
    capability — #579/#593 own that axis.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    stimulus_ref: AuthorityRef | None = None
    observation_refs: tuple[AuthorityRef, ...] = ()
    stimulus_state: StimulusAuthorityState
    measurement_state: MeasurementSemanticsState
    lfe_state: LfeSemanticState
    alignment_state: AlignmentState
    capability_separation: CapabilitySeparation
    verdict: RefCalVerdict
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(
        self,
    ) -> 'CadReferenceCalibrationQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('qualifications must pin the profile sha')
        if self.stimulus_ref is not None and (
            self.stimulus_ref.ref_sha256 is None
        ):
            raise ValueError('stimulus_ref must pin its sha256')
        for ref in self.observation_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'observation refs must pin their sha256'
                )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('calibration qualification hash mismatch')
        if self.qualification_id != _semantic_id('refqual', expected):
            raise ValueError('qualification id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'stimulus_ref': (
                self.stimulus_ref.model_dump(mode='json')
                if self.stimulus_ref is not None else None
            ),
            'observation_refs': [
                r.model_dump(mode='json') for r in self.observation_refs
            ],
            'stimulus_state': self.stimulus_state,
            'measurement_state': self.measurement_state,
            'lfe_state': self.lfe_state,
            'alignment_state': self.alignment_state,
            'capability_separation': self.capability_separation,
            'verdict': self.verdict,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


def reference_qualification_binding(
    qualification: CadReferenceCalibrationQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='reference_calibration_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


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


def build_reference_profile(**kwargs: Any) -> CadReferenceProfile:
    """Seal one calibration profile identity."""
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _seal_model(
        CadReferenceProfile, dict(kwargs),
        'profile_id', 'profile_sha256', 'refprof',
    )


def build_calibration_stimulus(
    **kwargs: Any,
) -> CadCalibrationStimulus:
    """Seal one calibration stimulus identity."""
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _seal_model(
        CadCalibrationStimulus, dict(kwargs),
        'stimulus_id', 'stimulus_sha256', 'refstim',
    )


def build_channel_observation(
    *,
    document_id: str,
    channel_role: str,
    signal_class: SignalClass = 'main_channel',
    stimulus: CadCalibrationStimulus | AuthorityRef | None = None,
    quantity: CalibrationQuantity = 'acoustic_channel_reference_spl',
    measured_spl_db: float | None = None,
    weighting: WeightingKind = 'unknown',
    bandwidth_hz_low: float | None = None,
    bandwidth_hz_high: float | None = None,
    time_weighting: TimeWeighting = 'unknown',
    integration_quantity: IntegrationQuantity = 'unknown',
    channel_trim_db: float | None = None,
    master_volume_state: str | None = None,
    device_output_gain_db: float | None = None,
    lfe_in_band_gain_db: float | None = None,
    broadband_meter_delta_db: float | None = None,
    instrument_ref: AuthorityRef | None = None,
    position_ref: AuthorityRef | None = None,
    room_state_ref: AuthorityRef | None = None,
    operations_applied: tuple[OperationKind, ...] = (),
    uncertainty_db: float | None = None,
    measured_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadChannelCalibrationObservation:
    """Seal one channel calibration observation."""
    stimulus_ref = (
        calibration_stimulus_binding(stimulus)
        if isinstance(stimulus, CadCalibrationStimulus)
        else stimulus
    )
    payload = dict(
        document_id=document_id,
        channel_role=channel_role,
        signal_class=signal_class,
        stimulus_ref=stimulus_ref,
        quantity=quantity,
        measured_spl_db=measured_spl_db,
        weighting=weighting,
        bandwidth_hz_low=bandwidth_hz_low,
        bandwidth_hz_high=bandwidth_hz_high,
        time_weighting=time_weighting,
        integration_quantity=integration_quantity,
        channel_trim_db=channel_trim_db,
        master_volume_state=master_volume_state,
        device_output_gain_db=device_output_gain_db,
        lfe_in_band_gain_db=lfe_in_band_gain_db,
        broadband_meter_delta_db=broadband_meter_delta_db,
        instrument_ref=instrument_ref,
        position_ref=position_ref,
        room_state_ref=room_state_ref,
        operations_applied=operations_applied,
        uncertainty_db=uncertainty_db,
        measured_at_utc=measured_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadChannelCalibrationObservation, payload,
        'observation_id', 'observation_sha256', 'refobs',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_reference_calibration(
    *,
    document_id: str,
    profile: CadReferenceProfile,
    stimulus: CadCalibrationStimulus | None = None,
    observations: tuple[CadChannelCalibrationObservation, ...] = (),
    capability_evaluated: bool = False,
    tolerance_db: float | None = None,
    evaluated_at_utc: str | None = None,
) -> CadReferenceCalibrationQualification:
    """Fail-closed reference-calibration verdict.

    ``reference_calibrated`` requires an exact profile, a bound
    documented stimulus, complete measurement semantics on every
    observed channel, and per-channel alignment inside the declared
    tolerance. A device-internal stimulus whose semantics are
    undocumented caps the verdict at ``device_profile_only`` — it is
    never a standardized reference proof. ``tolerance_db`` is the
    project/profile-supplied alignment window; HTDT never invents one.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []

    if profile.lifecycle_state == 'draft_research_only':
        raise ValueError(
            'a draft/research-only profile cannot qualify calibration '
            '— developing documents stay research-only until published'
        )
    for obs in observations:
        if obs.stimulus_ref is not None and stimulus is not None and (
            obs.stimulus_ref.ref_id != stimulus.stimulus_id
            or obs.stimulus_ref.ref_sha256 != stimulus.stimulus_sha256
        ):
            raise ValueError(
                'channel observation binds a different stimulus'
            )

    # ---------------- stimulus authority -----------------------------
    stimulus_state: StimulusAuthorityState
    if stimulus is None:
        stimulus_state = 'unbound'
        reasons.append(
            'no stimulus bound — the digital level/spectrum/routing of '
            'the test signal is undocumented'
        )
    elif stimulus.source_kind == 'device_internal_test':
        if stimulus.digital_level_dbfs is not None:
            stimulus_state = 'device_internal_documented'
            reasons.append(
                'device-internal test signal with documented level — '
                'still provider/firmware behavior, not a standardized '
                'reference stimulus'
            )
        else:
            stimulus_state = 'device_internal_undocumented'
            reasons.append(
                'device-internal test signal with undocumented digital '
                'level/processing — UNKNOWN by default'
            )
    elif stimulus.digital_level_dbfs is None:
        stimulus_state = 'unbound'
        reasons.append(
            'stimulus bound but its digital level (dBFS) is undeclared'
        )
    else:
        stimulus_state = 'exact_stimulus_bound'

    # ---------------- measurement semantics --------------------------
    measurement_state: MeasurementSemanticsState
    if not observations:
        measurement_state = 'no_measurements'
        reasons.append('no channel observations bound')
    elif all(
        obs.measured_spl_db is not None
        and obs.instrument_ref is not None
        and obs.position_ref is not None
        for obs in observations
    ):
        measurement_state = 'semantics_complete'
    else:
        measurement_state = 'semantics_incomplete'
        reasons.append(
            'some channels lack measured SPL with bound instrument/'
            'position — a bare dB figure is not reproducible'
        )

    # ---------------- LFE semantics ----------------------------------
    lfe_state: LfeSemanticState
    lfe_obs = [
        obs for obs in observations
        if obs.signal_class == 'native_lfe'
    ]
    redirected_obs = [
        obs for obs in observations
        if obs.signal_class == 'redirected_bass'
    ]
    profile_lfe_gain = profile.lfe_in_band_gain_db
    if lfe_obs:
        obs = lfe_obs[0]
        if obs.lfe_in_band_gain_db is None and (
            obs.broadband_meter_delta_db is not None
        ):
            lfe_state = 'meter_delta_only_not_proof'
            reasons.append(
                'only a broadband meter delta is recorded for LFE — '
                'a meter reading is not the +10 dB in-band '
                'reproduction-gain relationship (BS.775-4 Annex 7)'
            )
        elif obs.lfe_in_band_gain_db is not None:
            if profile_lfe_gain is not None and abs(
                obs.lfe_in_band_gain_db - profile_lfe_gain
            ) <= 0.1:
                lfe_state = 'in_band_gain_verified'
            elif profile_lfe_gain is not None:
                lfe_state = 'meter_delta_only_not_proof'
                reasons.append(
                    f'declared in-band LFE gain '
                    f'{obs.lfe_in_band_gain_db} dB does not match the '
                    f'profile\'s {profile_lfe_gain} dB'
                )
            else:
                lfe_state = 'in_band_gain_declared'
        else:
            lfe_state = 'meter_delta_only_not_proof'
            reasons.append(
                'native LFE channel observed but no in-band gain '
                'declared'
            )
    elif profile_lfe_gain is not None:
        lfe_state = 'meter_delta_only_not_proof'
        reasons.append(
            'profile declares LFE gain semantics but no native-LFE '
            'channel observation exists'
        )
    elif redirected_obs:
        lfe_state = 'redirected_bass_isolated'
    else:
        lfe_state = 'not_applicable'

    # ---------------- per-channel alignment --------------------------
    alignment_state: AlignmentState
    if measurement_state != 'semantics_complete':
        alignment_state = 'unverifiable'
    elif profile.target_spl_dbc is None:
        alignment_state = 'not_evaluated'
        reasons.append(
            'profile carries no acoustic target — alignment cannot '
            'be judged against an absent target'
        )
    elif tolerance_db is None:
        alignment_state = 'unverifiable'
        reasons.append(
            'no alignment tolerance supplied by the profile/project — '
            'HTDT does not invent one'
        )
    else:
        # Alignment judges main-channel reference SPL only — the LFE
        # channel's +10 dB in-band reproduction gain is evaluated by
        # the LFE axis, not against the main-channel target.
        misaligned = [
            obs.channel_role
            for obs in observations
            if obs.signal_class == 'main_channel'
            and obs.measured_spl_db is not None
            and abs(obs.measured_spl_db - profile.target_spl_dbc)
            > tolerance_db
        ]
        if misaligned:
            alignment_state = 'misaligned'
            reasons.append(
                'channels outside tolerance: ' + ', '.join(misaligned)
            )
        else:
            alignment_state = 'aligned'

    capability_separation: CapabilitySeparation = (
        'capability_evaluated_separately'
        if capability_evaluated
        else 'capability_not_evaluated'
    )
    if not capability_evaluated:
        reasons.append(
            'reference alignment says nothing about maximum clean '
            'capability — #579/#593 own that axis'
        )

    # ---------------- joint verdict ----------------------------------
    if alignment_state == 'misaligned':
        verdict: RefCalVerdict = 'failed'
    elif stimulus_state in ('unbound',) or measurement_state in (
        'no_measurements',
    ):
        verdict = 'insufficient_evidence'
    elif stimulus_state == 'device_internal_undocumented':
        verdict = 'device_profile_only'
    elif (
        stimulus_state == 'exact_stimulus_bound'
        and measurement_state == 'semantics_complete'
        and alignment_state == 'aligned'
        and lfe_state in (
            'not_applicable',
            'in_band_gain_verified',
            'in_band_gain_declared',
            'redirected_bass_isolated',
        )
    ):
        verdict = 'reference_calibrated'
    elif measurement_state == 'semantics_complete':
        verdict = 'calibrated_with_limitations'
    else:
        verdict = 'insufficient_evidence'

    payload = dict(
        document_id=document_id,
        profile_ref=reference_profile_binding(profile),
        stimulus_ref=(
            calibration_stimulus_binding(stimulus)
            if stimulus is not None else None
        ),
        observation_refs=tuple(
            channel_observation_binding(o) for o in observations
        ),
        stimulus_state=stimulus_state,
        measurement_state=measurement_state,
        lfe_state=lfe_state,
        alignment_state=alignment_state,
        capability_separation=capability_separation,
        verdict=verdict,
        reasons=tuple(reasons),
        evaluation_version=REFCAL_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadReferenceCalibrationQualification, payload,
        'qualification_id', 'qualification_sha256', 'refqual',
    )


__all__ = [
    'REFCAL_EVALUATION_VERSION',
    'REFCAL_SCHEMA_VERSION',
    'AlignmentState',
    'CadCalibrationStimulus',
    'CadChannelCalibrationObservation',
    'CadReferenceCalibrationQualification',
    'CadReferenceProfile',
    'CalibrationQuantity',
    'CapabilitySeparation',
    'IntegrationQuantity',
    'LfeSemanticState',
    'MeasurementSemanticsState',
    'OperationKind',
    'ProfileKind',
    'RefCalVerdict',
    'SignalClass',
    'StimulusAuthorityState',
    'StimulusSourceKind',
    'TimeWeighting',
    'WeightingKind',
    'build_calibration_stimulus',
    'build_channel_observation',
    'build_reference_profile',
    'calibration_stimulus_binding',
    'channel_observation_binding',
    'evaluate_reference_calibration',
    'reference_profile_binding',
    'reference_qualification_binding',
]
