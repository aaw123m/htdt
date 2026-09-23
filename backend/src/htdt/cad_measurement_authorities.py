"""Immutable acquisition-side authority records for the measurement domain.

The persisted ``CadMeasurementRecord``/``CadFrequencyResponseDataset`` pair
proves *what* was measured. The authorities here prove the surrounding
conditions a later consumer must resolve verbatim instead of trusting a
label: the exact timing reference a measurement used (#642), the acoustic
level calibration that authorizes an absolute-SPL reading (#643), the
verified output-channel-to-physical-speaker map (#473) and the independent
speaker wiring commissioning checks (#645).

Every model is frozen pydantic sealed by a SHA-256 of its canonical
``identity_payload`` — the same sealing convention as
``CadAcquisitionContext`` — and is persisted through
``CadMeasurementQualityRepository``. The repository re-validates the exact
bindings on save and on every read; a record that no longer resolves fails
closed rather than silently downgrading to 'unknown'.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_unique_non_empty(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)) or any(not item for item in values):
        raise ValueError(f'{label} must be unique non-empty values')


# ---------------------------------------------------------------------------
# Measurement timing-reference authority (#642)


TimingReferenceMethod = Literal[
    'acoustic_reference',
    'loopback',
    'shared_clock',
    'external_sync',
    'imported',
    'manual',
    'unknown',
]

TimingT0Convention = Literal[
    'acoustic_reference_signal',
    'loopback_edge',
    'sweep_start',
    'imported',
    'manual',
    'unknown',
]

TimingCorrectionKind = Literal[
    'loopback_path',
    'output_buffer',
    'input_buffer',
    'driver_latency',
    'acoustic_distance',
    'imported',
    'manual',
    'unknown',
]

TimingReferenceScope = Literal['measurement', 'session', 'persistent', 'unknown']


class CadTimingDelayCorrection(BaseModel):
    """One typed delay correction applied to the timing reference."""

    model_config = ConfigDict(frozen=True)

    correction_kind: TimingCorrectionKind
    value_s: float
    provenance: str | None = None

    @model_validator(mode='after')
    def valid_correction(self) -> 'CadTimingDelayCorrection':
        if not isfinite(float(self.value_s)):
            raise ValueError('delay correction value must be finite')
        return self


class CadMeasurementTimingReference(BaseModel):
    """Immutable timing-reference authority for IR arrival/alignment claims.

    ``method`` is the semantic the reference actually carries: an acoustic
    reference signal, a loopback channel, a shared hardware clock, external
    sync, an imported capture whose timing was fixed outside HTDT, a manual
    declaration, or unknown. ``imported``/``manual``/``unknown`` methods are
    recorded honestly but never authorize common-timing claims — a manual
    entry is not a verified timing source, and two measurements sharing a
    sample rate or a label do not prove a shared clock.
    """

    model_config = ConfigDict(frozen=True)

    timing_reference_id: str = Field(min_length=1)
    version: str = Field(min_length=1, default='1')
    method: TimingReferenceMethod
    reference_channel: str | None = None
    input_clock_identity: str | None = None
    output_clock_identity: str | None = None
    sample_rate_hz: int | None = Field(default=None, gt=0)
    t0_convention: TimingT0Convention = 'unknown'
    delay_corrections: tuple[CadTimingDelayCorrection, ...] = ()
    validity_scope: TimingReferenceScope = 'unknown'
    provenance_json: str = '{}'
    created_at_utc: str = Field(min_length=1)
    timing_reference_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_reference(self) -> 'CadMeasurementTimingReference':
        _require_iso8601(self.created_at_utc, 'timing reference created_at_utc')
        if self.method == 'shared_clock' and not (
            self.input_clock_identity and self.output_clock_identity
        ):
            raise ValueError(
                'shared-clock timing requires both clock identities'
            )
        if self.timing_reference_sha256 != _hash(self.identity_payload()):
            raise ValueError('timing reference hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'timing_reference_id': self.timing_reference_id,
            'version': self.version,
            'method': self.method,
            'reference_channel': self.reference_channel,
            'input_clock_identity': self.input_clock_identity,
            'output_clock_identity': self.output_clock_identity,
            'sample_rate_hz': self.sample_rate_hz,
            't0_convention': self.t0_convention,
            'delay_corrections': [
                correction.model_dump(mode='json')
                for correction in self.delay_corrections
            ],
            'validity_scope': self.validity_scope,
            'provenance_json': self.provenance_json,
            'created_at_utc': self.created_at_utc,
        }


_TIMING_METHODS_WITH_COMMON_TIMING = frozenset(
    {'acoustic_reference', 'loopback', 'shared_clock', 'external_sync'}
)


def timing_reference_supports_common_timing(
    reference: CadMeasurementTimingReference,
) -> bool:
    """Whether this authority can carry a common-timing claim at all.

    Only genuinely synchronized methods qualify. ``imported``, ``manual``
    and ``unknown`` references are honest records of what was captured but
    never pass the timing check by themselves.
    """

    return reference.method in _TIMING_METHODS_WITH_COMMON_TIMING


def timing_clocks_shared(reference: CadMeasurementTimingReference) -> bool:
    """True only for a declared shared hardware clock on both ends.

    Matching sample rates or matching clock labels are not proof — this is
    exactly the 'loopback/shared clock' distinction the authority exists
    for.
    """

    return (
        reference.method == 'shared_clock'
        and reference.input_clock_identity is not None
        and reference.input_clock_identity == reference.output_clock_identity
    )


def build_timing_reference(
    *,
    method: TimingReferenceMethod,
    timing_reference_id: str | None = None,
    version: str = '1',
    reference_channel: str | None = None,
    input_clock_identity: str | None = None,
    output_clock_identity: str | None = None,
    sample_rate_hz: int | None = None,
    t0_convention: TimingT0Convention = 'unknown',
    delay_corrections: Sequence[CadTimingDelayCorrection | dict[str, Any]] = (),
    validity_scope: TimingReferenceScope = 'unknown',
    created_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadMeasurementTimingReference:
    """Assemble a sealed timing-reference authority record.

    The record only becomes authoritative once persisted through the quality
    repository; acquisition contexts then bind the exact id+hash.
    """
    corrections = tuple(
        item
        if isinstance(item, CadTimingDelayCorrection)
        else CadTimingDelayCorrection.model_validate(item)
        for item in delay_corrections
    )
    payload: dict[str, Any] = {
        'timing_reference_id': timing_reference_id or str(uuid4()),
        'version': version,
        'method': method,
        'reference_channel': reference_channel,
        'input_clock_identity': input_clock_identity,
        'output_clock_identity': output_clock_identity,
        'sample_rate_hz': sample_rate_hz,
        't0_convention': t0_convention,
        'delay_corrections': corrections,
        'validity_scope': validity_scope,
        'provenance_json': provenance_json,
        'created_at_utc': created_at_utc or _utc_now(),
    }
    provisional = CadMeasurementTimingReference.model_construct(
        **payload,
        timing_reference_sha256='0' * 64,
    )
    return CadMeasurementTimingReference(
        **payload,
        timing_reference_sha256=_hash(provisional.identity_payload()),
    )


# ---------------------------------------------------------------------------
# Acoustic level-calibration authority (#643)


LevelCalibrationMethod = Literal[
    'manufacturer_sensitivity',
    'acoustic_calibrator',
    'rew_spl_session',
    'reference_meter_transfer',
    'imported',
    'manual',
    'unknown',
]

LevelCalibrationScope = Literal['measurement', 'session', 'instrument', 'unknown']


class CadAcousticLevelCalibration(BaseModel):
    """Immutable calibration authority for absolute dB-SPL claims.

    This is deliberately distinct from the microphone frequency-response
    correction file: a mic cal file corrects the *shape* of a response and
    never by itself authorizes an absolute SPL reading. Absolute SPL requires
    a calibrator trace, a REW SPL-session level agreement, or a calibrated
    reference-meter transfer — manufacturer sensitivity datasheets,
    imported/manual entries and unknown methods are recorded but do not
    authorize absolute SPL.
    """

    model_config = ConfigDict(frozen=True)

    calibration_id: str = Field(min_length=1)
    version: str = Field(min_length=1, default='1')
    method: LevelCalibrationMethod
    instrument_identity: str | None = None
    instrument_profile: str | None = None
    input_device_label: str | None = None
    input_channel: str | None = None
    sensitivity_v_per_pa: float | None = Field(default=None, gt=0.0)
    reference_level_db_spl: float | None = None
    reference_frequency_hz: float | None = Field(default=None, gt=0.0)
    calibrated_at_utc: str = Field(min_length=1)
    validity_scope: LevelCalibrationScope = 'unknown'
    provenance_json: str = '{}'
    calibration_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_calibration(self) -> 'CadAcousticLevelCalibration':
        _require_iso8601(self.calibrated_at_utc, 'calibration calibrated_at_utc')
        for label, value in (
            ('reference_level_db_spl', self.reference_level_db_spl),
            ('reference_frequency_hz', self.reference_frequency_hz),
            ('sensitivity_v_per_pa', self.sensitivity_v_per_pa),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{label} must be finite')
        if self.calibration_sha256 != _hash(self.identity_payload()):
            raise ValueError('acoustic level calibration hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'calibration_id': self.calibration_id,
            'version': self.version,
            'method': self.method,
            'instrument_identity': self.instrument_identity,
            'instrument_profile': self.instrument_profile,
            'input_device_label': self.input_device_label,
            'input_channel': self.input_channel,
            'sensitivity_v_per_pa': self.sensitivity_v_per_pa,
            'reference_level_db_spl': self.reference_level_db_spl,
            'reference_frequency_hz': self.reference_frequency_hz,
            'calibrated_at_utc': self.calibrated_at_utc,
            'validity_scope': self.validity_scope,
            'provenance_json': self.provenance_json,
        }


_LEVEL_METHODS_WITH_ABSOLUTE_SPL = frozenset(
    {'acoustic_calibrator', 'rew_spl_session', 'reference_meter_transfer'}
)


def calibration_supports_absolute_spl(
    calibration: CadAcousticLevelCalibration,
) -> bool:
    """Whether this calibration can authorize an absolute dB-SPL claim.

    ``manufacturer_sensitivity``, ``imported``, ``manual`` and ``unknown``
    methods are never SPL authorities on their own.
    """

    return calibration.method in _LEVEL_METHODS_WITH_ABSOLUTE_SPL


def build_acoustic_level_calibration(
    *,
    method: LevelCalibrationMethod,
    calibration_id: str | None = None,
    version: str = '1',
    instrument_identity: str | None = None,
    instrument_profile: str | None = None,
    input_device_label: str | None = None,
    input_channel: str | None = None,
    sensitivity_v_per_pa: float | None = None,
    reference_level_db_spl: float | None = None,
    reference_frequency_hz: float | None = None,
    calibrated_at_utc: str | None = None,
    validity_scope: LevelCalibrationScope = 'unknown',
    provenance_json: str = '{}',
) -> CadAcousticLevelCalibration:
    """Assemble a sealed acoustic level-calibration authority record."""
    payload: dict[str, Any] = {
        'calibration_id': calibration_id or str(uuid4()),
        'version': version,
        'method': method,
        'instrument_identity': instrument_identity,
        'instrument_profile': instrument_profile,
        'input_device_label': input_device_label,
        'input_channel': input_channel,
        'sensitivity_v_per_pa': sensitivity_v_per_pa,
        'reference_level_db_spl': reference_level_db_spl,
        'reference_frequency_hz': reference_frequency_hz,
        'calibrated_at_utc': calibrated_at_utc or _utc_now(),
        'validity_scope': validity_scope,
        'provenance_json': provenance_json,
    }
    provisional = CadAcousticLevelCalibration.model_construct(
        **payload,
        calibration_sha256='0' * 64,
    )
    return CadAcousticLevelCalibration(
        **payload,
        calibration_sha256=_hash(provisional.identity_payload()),
    )


MeasurementLevelReferenceKind = Literal[
    'absolute_spl',
    'spl_uncalibrated',
    'dbfs',
    'pa',
    'relative',
    'unknown',
]


class CadDatasetLevelReference(BaseModel):
    """Immutable binding between one exact dataset and its level semantics.

    The reference says what the persisted ``level_db`` values actually mean
    and — only for ``absolute_spl`` — pins the exact
    ``CadAcousticLevelCalibration`` authority that authorizes the reading.
    The repository refuses an ``absolute_spl`` row whose bound calibration
    does not support absolute SPL, so a persisted row can never claim more
    level semantics than its evidence.
    """

    model_config = ConfigDict(frozen=True)

    level_reference_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=_SHA256_PATTERN)
    level_reference_kind: MeasurementLevelReferenceKind
    calibration_id: str | None = None
    calibration_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    created_at_utc: str = Field(min_length=1)
    level_reference_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_level_reference(self) -> 'CadDatasetLevelReference':
        _require_iso8601(self.created_at_utc, 'level reference created_at_utc')
        if self.level_reference_kind == 'absolute_spl':
            if self.calibration_id is None or self.calibration_sha256 is None:
                raise ValueError(
                    'absolute SPL requires a bound acoustic level calibration'
                )
        elif self.calibration_id is not None or self.calibration_sha256 is not None:
            raise ValueError(
                'non-absolute level references must not bind a calibration'
            )
        if self.level_reference_sha256 != _hash(self.identity_payload()):
            raise ValueError('dataset level reference hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'level_reference_id': self.level_reference_id,
            'measurement_id': self.measurement_id,
            'dataset_id': self.dataset_id,
            'dataset_sha256': self.dataset_sha256,
            'level_reference_kind': self.level_reference_kind,
            'calibration_id': self.calibration_id,
            'calibration_sha256': self.calibration_sha256,
            'created_at_utc': self.created_at_utc,
        }


def build_dataset_level_reference(
    *,
    measurement_id: str,
    dataset_id: str,
    dataset_sha256: str,
    level_reference_kind: MeasurementLevelReferenceKind,
    calibration_id: str | None = None,
    calibration_sha256: str | None = None,
    level_reference_id: str | None = None,
    created_at_utc: str | None = None,
) -> CadDatasetLevelReference:
    """Assemble a sealed dataset level-reference binding."""
    payload: dict[str, Any] = {
        'level_reference_id': level_reference_id or str(uuid4()),
        'measurement_id': measurement_id,
        'dataset_id': dataset_id,
        'dataset_sha256': dataset_sha256,
        'level_reference_kind': level_reference_kind,
        'calibration_id': calibration_id,
        'calibration_sha256': calibration_sha256,
        'created_at_utc': created_at_utc or _utc_now(),
    }
    provisional = CadDatasetLevelReference.model_construct(
        **payload,
        level_reference_sha256='0' * 64,
    )
    return CadDatasetLevelReference(
        **payload,
        level_reference_sha256=_hash(provisional.identity_payload()),
    )


# ---------------------------------------------------------------------------
# Verified channel-map / routing-profile authority (#473)


RoutingVerification = Literal['verified', 'unverified', 'mixed', 'unavailable']


class CadChannelMapEntry(BaseModel):
    """One verified output-path to physical-radiator binding.

    Captures the exact path seen while a test signal was playing: the
    output device label as the OS/REW named it, the REW channel label and
    hardware channel index, the logical role HTDT assigned, which physical
    speakers were expected vs actually observed, and the frequency band the
    observation covered — a bass-managed low band can legitimately land on
    a different radiator than the main band. ``avr_configuration_id``
    binds the AVR condition the map was verified under so later topology
    changes are detectable instead of silently reused.
    """

    model_config = ConfigDict(frozen=True)

    output_device_label: str = Field(min_length=1)
    rew_channel_label: str = Field(min_length=1)
    hardware_channel_index: int | None = Field(default=None, ge=0)
    logical_role: str = 'unknown'
    expected_speaker_ids: tuple[str, ...] = ()
    observed_speaker_ids: tuple[str, ...] = ()
    verification: RoutingVerification = 'unverified'
    verified_at_utc: str | None = None
    avr_configuration_id: str | None = None
    verified_band_hz: tuple[float, float] | None = None

    @field_validator('expected_speaker_ids', 'observed_speaker_ids')
    @classmethod
    def unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_unique_non_empty(value, 'speaker id lists')
        return value

    @model_validator(mode='after')
    def valid_entry(self) -> 'CadChannelMapEntry':
        if self.verified_at_utc is not None:
            _require_iso8601(self.verified_at_utc, 'channel map verified_at_utc')
        if self.verified_band_hz is not None:
            low, high = self.verified_band_hz
            if not (isfinite(low) and isfinite(high)) or low <= 0 or high <= low:
                raise ValueError('verified band is invalid')
        return self


class CadRoutingProfile(BaseModel):
    """Immutable verified channel map: output path to physical speakers.

    A saved profile is the authority a measurement assignment resolves
    against; it deliberately excludes electrical-commissioning truth
    (continuity, terminal polarity, acoustic polarity, load) — those stay
    with ``CadWiringVerificationCheck`` (#645) which may reference a profile
    but is never implied by it.
    """

    model_config = ConfigDict(frozen=True)

    routing_profile_id: str = Field(min_length=1)
    profile_name: str = Field(min_length=1, default='routing')
    entries: tuple[CadChannelMapEntry, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    routing_profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadRoutingProfile':
        seen = set()
        for entry in self.entries:
            key = (
                entry.output_device_label,
                entry.rew_channel_label,
                entry.hardware_channel_index,
                entry.verified_band_hz,
            )
            if key in seen:
                raise ValueError(
                    'duplicate channel map entry for the same output path/band'
                )
            seen.add(key)
        _require_iso8601(self.created_at_utc, 'routing profile created_at_utc')
        if self.routing_profile_sha256 != _hash(self.identity_payload()):
            raise ValueError('routing profile hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'routing_profile_id': self.routing_profile_id,
            'profile_name': self.profile_name,
            'entries': [
                entry.model_dump(mode='json') for entry in self.entries
            ],
            'created_at_utc': self.created_at_utc,
            'provenance_json': self.provenance_json,
        }

    def entry_for_role(self, logical_role: str) -> CadChannelMapEntry | None:
        """Resolve the channel-map entry carrying ``logical_role``."""
        matches = [
            entry for entry in self.entries if entry.logical_role == logical_role
        ]
        return matches[0] if len(matches) == 1 else None


class CadRoutingProfileBinding(BaseModel):
    """Exact id/hash reference to a persisted routing profile."""

    model_config = ConfigDict(frozen=True)

    routing_profile_id: str = Field(min_length=1)
    routing_profile_sha256: str = Field(pattern=_SHA256_PATTERN)


def routing_profile_binding(
    profile: CadRoutingProfile,
) -> CadRoutingProfileBinding:
    return CadRoutingProfileBinding(
        routing_profile_id=profile.routing_profile_id,
        routing_profile_sha256=profile.routing_profile_sha256,
    )


def build_routing_profile(
    *,
    entries: Sequence[CadChannelMapEntry | dict[str, Any]],
    profile_name: str = 'routing',
    routing_profile_id: str | None = None,
    created_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadRoutingProfile:
    """Assemble a sealed routing-profile authority record."""
    resolved = tuple(
        item if isinstance(item, CadChannelMapEntry) else CadChannelMapEntry.model_validate(item)
        for item in entries
    )
    payload: dict[str, Any] = {
        'routing_profile_id': routing_profile_id or str(uuid4()),
        'profile_name': profile_name,
        'entries': resolved,
        'created_at_utc': created_at_utc or _utc_now(),
        'provenance_json': provenance_json,
    }
    provisional = CadRoutingProfile.model_construct(
        **payload,
        routing_profile_sha256='0' * 64,
    )
    return CadRoutingProfile(
        **payload,
        routing_profile_sha256=_hash(provisional.identity_payload()),
    )


def routing_profile_staleness(
    profile: CadRoutingProfile,
    *,
    avr_configuration_id: str | None = None,
    output_device_labels: frozenset[str] | tuple[str, ...] | None = None,
    speaker_ids: frozenset[str] | tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Concrete reasons a saved profile must not be silently reused.

    An empty tuple means the profile still applies. A change to the AVR
    configuration any entry was verified under, to an output device label,
    or to the physical speaker set is each reported separately so the
    caller can surface *what* moved instead of a bare 'stale' flag.
    """
    reasons: list[str] = []
    if avr_configuration_id is not None:
        for entry in profile.entries:
            if (
                entry.avr_configuration_id is not None
                and entry.avr_configuration_id != avr_configuration_id
            ):
                reasons.append(
                    'AVR configuration changed for channel '
                    f'{entry.rew_channel_label}'
                )
    if output_device_labels is not None:
        labels = frozenset(output_device_labels)
        for entry in profile.entries:
            if entry.output_device_label not in labels:
                reasons.append(
                    f'output device label no longer present: '
                    f'{entry.output_device_label}'
                )
    if speaker_ids is not None:
        available = frozenset(speaker_ids)
        for entry in profile.entries:
            missing = [
                speaker
                for speaker in (*entry.expected_speaker_ids, *entry.observed_speaker_ids)
                if speaker not in available
            ]
            if missing:
                reasons.append(
                    'speaker topology changed: '
                    + ', '.join(sorted(set(missing)))
                )
    return tuple(dict.fromkeys(reasons))


# ---------------------------------------------------------------------------
# Speaker wiring commissioning checks (#645)


WiringCheckKind = Literal[
    'routing',
    'continuity',
    'terminal_polarity',
    'acoustic_polarity',
    'load',
]

WiringCheckResult = Literal['PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE']

# Applied polarity-compensation token: each occurrence of this token in a
# check's ``applied_compensation`` list is one intended signal-polarity
# inversion (e.g. a DSP/AVR polarity control). Composition is explicit —
# the acoustic result is evaluated against the expected net polarity, never
# collapsed into a single ambiguous 'polarity ok' flag.
POLARITY_INVERSION_TOKEN = 'polarity_invert'


class CadWiringVerificationCheck(BaseModel):
    """One independent wiring-commissioning check result.

    Each check kind is an independent claim: continuity passing does not
    imply the routing is right, terminal polarity passing does not imply
    the acoustic polarity is right (in-room driver/crossover behaviour can
    still invert), and a load observation (e.g. DCR) is not an impedance
    curve. ``check_kind`` and ``result`` are always separate axes so a
    record can honestly carry ``NOT_APPLICABLE`` for a channel that does
    not expose that check.
    """

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    check_kind: WiringCheckKind
    expected_output_reference: str | None = None
    expected_speaker_ids: tuple[str, ...] = ()
    source_speaker_ids: tuple[str, ...] = ()
    method: str = Field(min_length=1)
    observed_output_reference: str | None = None
    observed_speaker_ids: tuple[str, ...] = ()
    applied_compensation: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    measured_at_utc: str = Field(min_length=1)
    operator: str | None = None
    result: WiringCheckResult
    reason: str | None = None
    notes: tuple[str, ...] = ()
    provenance_json: str = '{}'
    check_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator(
        'expected_speaker_ids',
        'source_speaker_ids',
        'observed_speaker_ids',
        'applied_compensation',
        'evidence_refs',
    )
    @classmethod
    def unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item for item in value):
            raise ValueError('list fields must hold unique non-empty values')
        return value

    @model_validator(mode='after')
    def valid_check(self) -> 'CadWiringVerificationCheck':
        _require_iso8601(self.measured_at_utc, 'wiring check measured_at_utc')
        if self.result in ('FAIL', 'UNKNOWN') and not (
            self.reason and self.reason.strip()
        ):
            raise ValueError('wiring check FAIL/UNKNOWN requires a reason')
        if self.check_sha256 != _hash(self.identity_payload()):
            raise ValueError('wiring verification check hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'check_id': self.check_id,
            'document_id': self.document_id,
            'check_kind': self.check_kind,
            'expected_output_reference': self.expected_output_reference,
            'expected_speaker_ids': list(self.expected_speaker_ids),
            'source_speaker_ids': list(self.source_speaker_ids),
            'method': self.method,
            'observed_output_reference': self.observed_output_reference,
            'observed_speaker_ids': list(self.observed_speaker_ids),
            'applied_compensation': list(self.applied_compensation),
            'evidence_refs': list(self.evidence_refs),
            'measured_at_utc': self.measured_at_utc,
            'operator': self.operator,
            'result': self.result,
            'reason': self.reason,
            'notes': list(self.notes),
            'provenance_json': self.provenance_json,
        }


def build_wiring_check(
    *,
    document_id: str,
    check_kind: WiringCheckKind,
    method: str,
    result: WiringCheckResult,
    measured_at_utc: str,
    check_id: str | None = None,
    expected_output_reference: str | None = None,
    expected_speaker_ids: Sequence[str] = (),
    source_speaker_ids: Sequence[str] = (),
    observed_output_reference: str | None = None,
    observed_speaker_ids: Sequence[str] = (),
    applied_compensation: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
    operator: str | None = None,
    reason: str | None = None,
    notes: Sequence[str] = (),
    provenance_json: str = '{}',
) -> CadWiringVerificationCheck:
    """Assemble a sealed wiring-verification check record."""
    payload: dict[str, Any] = {
        'check_id': check_id or str(uuid4()),
        'document_id': document_id,
        'check_kind': check_kind,
        'expected_output_reference': expected_output_reference,
        'expected_speaker_ids': tuple(expected_speaker_ids),
        'source_speaker_ids': tuple(source_speaker_ids),
        'method': method,
        'observed_output_reference': observed_output_reference,
        'observed_speaker_ids': tuple(observed_speaker_ids),
        'applied_compensation': tuple(applied_compensation),
        'evidence_refs': tuple(evidence_refs),
        'measured_at_utc': measured_at_utc,
        'operator': operator,
        'result': result,
        'reason': reason,
        'notes': tuple(notes),
        'provenance_json': provenance_json,
    }
    provisional = CadWiringVerificationCheck.model_construct(
        **payload,
        check_sha256='0' * 64,
    )
    return CadWiringVerificationCheck(
        **payload,
        check_sha256=_hash(provisional.identity_payload()),
    )


def latest_wiring_checks(
    checks: Sequence[CadWiringVerificationCheck],
) -> dict[WiringCheckKind, CadWiringVerificationCheck]:
    """Latest result per check kind, ordered by ``measured_at_utc``.

    Commissioning reads a per-kind headline — but each row stays an
    independent check; this helper never merges kinds into one verdict.
    """
    latest: dict[WiringCheckKind, CadWiringVerificationCheck] = {}
    for check in checks:
        previous = latest.get(check.check_kind)
        if (
            previous is None
            or (check.measured_at_utc, check.check_id)
            >= (previous.measured_at_utc, previous.check_id)
        ):
            latest[check.check_kind] = check
    return latest


def net_polarity_state(
    *,
    terminal_polarity_correct: bool | None,
    applied_compensation: Sequence[str],
    acoustic_polarity_correct: bool | None,
) -> WiringCheckResult:
    """Compose applied inversions with the acoustic polarity observation.

    ``applied_compensation`` counts intended inversions (each
    ``POLARITY_INVERSION_TOKEN`` is one). The expected acoustic sign is the
    composition of intended inversions and the terminal conductor state;
    the acoustic observation must match that expectation for PASS. Missing
    observations report UNKNOWN rather than collapsing into the other
    checks.
    """
    if acoustic_polarity_correct is None or terminal_polarity_correct is None:
        return 'UNKNOWN'
    inversions = sum(
        1
        for token in applied_compensation
        if token == POLARITY_INVERSION_TOKEN
    )
    expected_inverted = inversions % 2 == 1
    if not terminal_polarity_correct:
        expected_inverted = not expected_inverted
    acoustic_inverted = not acoustic_polarity_correct
    return 'PASS' if acoustic_inverted == expected_inverted else 'FAIL'


__all__ = [
    'POLARITY_INVERSION_TOKEN',
    'CadAcousticLevelCalibration',
    'CadChannelMapEntry',
    'CadDatasetLevelReference',
    'CadMeasurementTimingReference',
    'CadRoutingProfile',
    'CadRoutingProfileBinding',
    'CadTimingDelayCorrection',
    'CadWiringVerificationCheck',
    'LevelCalibrationMethod',
    'LevelCalibrationScope',
    'MeasurementLevelReferenceKind',
    'RoutingVerification',
    'TimingCorrectionKind',
    'TimingReferenceMethod',
    'TimingReferenceScope',
    'TimingT0Convention',
    'WiringCheckKind',
    'WiringCheckResult',
    'build_acoustic_level_calibration',
    'build_dataset_level_reference',
    'build_routing_profile',
    'build_timing_reference',
    'build_wiring_check',
    'calibration_supports_absolute_spl',
    'latest_wiring_checks',
    'net_polarity_state',
    'routing_profile_binding',
    'routing_profile_staleness',
    'timing_clocks_shared',
    'timing_reference_supports_common_timing',
]
