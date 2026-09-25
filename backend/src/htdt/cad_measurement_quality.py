from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite, sqrt
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurement_authorities import CadRoutingProfileBinding
from .cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementRecord
from .cad_scene import Direction3, Position3
from .r120_geometry_compiler import ExactExternalAuthorityRef


QUALITY_ALGORITHM_VERSION = 'measurement-quality-1'

QualityDecision = Literal['PASS', 'FAIL', 'UNKNOWN', 'NOT_EVALUATED']
CapabilityDecision = Literal['ALLOWED', 'BLOCKED', 'UNKNOWN']
RetakeRecommendation = Literal['RETAKE', 'NOT_NEEDED', 'UNKNOWN']
MeasurementCapabilityClaim = Literal[
    'magnitude_response',
    'phase_response',
    'common_timing',
    'arrival_time',
    'decay',
    'calibrated_response',
    'repeatability',
    'polarity',
]

AcquisitionContextSourceKind = Literal['native', 'legacy', 'manual', 'unknown']
EvidenceSourceKind = Literal['rew_metadata', 'raw_asset', 'manual', 'mixed', 'unknown']

# Evidence fields the persisted ``CadAcquisitionContext`` is the sole
# authority for. A report's timing evidence must equal the resolved context's
# values exactly; without a resolved context no timing evidence may be
# claimed at all.
TIMING_EVIDENCE_FIELDS: tuple[str, ...] = (
    'timing_reference_valid',
    'timing_reference_id',
    'clock_source',
    'sample_rate_hz',
    'delay_correction_s',
)

# Per-measurement acquisition metadata a persisted ``CadMeasurementObservation``
# is the sole authority for. Any non-default value here requires a resolved
# observation whose field values equal the claimed evidence verbatim.
OBSERVATION_EVIDENCE_FIELDS: tuple[str, ...] = (
    'clipping_detected',
    'peak_dbfs',
    'noise_floor_db_spl',
    'signal_level_db_spl',
    'snr_db',
    'usable_frequency_band_hz',
    'polarity_correct',
    'polarity_confidence',
    'has_impulse_response',
    'ir_window_start_s',
    'ir_window_end_s',
    'ir_truncated',
)

# Observation source kinds whose values were extracted from an exact machine
# artifact: the observation must pin that artifact in ``source_asset_sha256``
# and the pin must resolve to the subject measurement's verified raw asset.
# ``manual``/``unknown`` observations are themselves the authority and must
# not bind a source asset.
MACHINE_OBSERVATION_SOURCES: tuple[EvidenceSourceKind, ...] = (
    'rew_metadata',
    'raw_asset',
    'mixed',
)

_ALL_CAPABILITY_CLAIMS: tuple[MeasurementCapabilityClaim, ...] = (
    'magnitude_response',
    'phase_response',
    'common_timing',
    'arrival_time',
    'decay',
    'calibrated_response',
    'repeatability',
    'polarity',
)

# Canonical independent-check ordering surfaced to read models/UX. The pinned
# QUALITY_ALGORITHM_IDENTITY below keeps its own literal list because that
# payload feeds the algorithm hash and must never drift.
MEASUREMENT_QUALITY_CHECKS: tuple[str, ...] = (
    'clipping',
    'noise_snr',
    'usable_frequency_band',
    'timing_reference',
    'polarity',
    'ir_window',
    'calibration',
    'repeatability',
)


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


QUALITY_ALGORITHM_IDENTITY = {
    'algorithm_version': QUALITY_ALGORITHM_VERSION,
    'decision_semantics': ['PASS', 'FAIL', 'UNKNOWN', 'NOT_EVALUATED'],
    'capability_semantics': ['ALLOWED', 'BLOCKED', 'UNKNOWN'],
    'checks': [
        'clipping',
        'noise_snr',
        'usable_frequency_band',
        'timing_reference',
        'polarity',
        'ir_window',
        'calibration',
        'repeatability',
    ],
    'claims': list(_ALL_CAPABILITY_CLAIMS),
    'missing_evidence': 'unknown_or_not_evaluated_never_pass',
    'phase_does_not_imply_common_timing': True,
    'fr_does_not_imply_acquisition_quality': True,
    'common_timing_requires': [
        'authoritative_acquisition_context',
        'timing_reference',
    ],
    'arrival_time_requires': ['impulse_response', 'common_timing', 'ir_window'],
    'decay_requires': ['impulse_response', 'ir_window'],
    'calibrated_response_requires': [
        'authoritative_acquisition_context',
        'clipping:PASS',
        'noise_snr:PASS',
        'usable_frequency_band:PASS',
        'calibration:PASS',
    ],
    'polarity_requires': ['polarity:PASS'],
    'repeatability_requires': ['repeatability:PASS'],
    'unconfigured_thresholds_do_not_pass': True,
}
QUALITY_ALGORITHM_SHA256 = _hash(QUALITY_ALGORITHM_IDENTITY)


def measurement_sha256(record: CadMeasurementRecord) -> str:
    return _hash(record.model_dump(mode='json'))


def dataset_sha256(dataset: CadFrequencyResponseDataset) -> str:
    """Persisted semantic dataset identity — delegates to the model property."""
    return dataset.dataset_sha256


class CadMeasurementQualityProfile(BaseModel):
    """Versioned thresholds used to interpret explicit acquisition evidence."""

    model_config = ConfigDict(frozen=True)

    profile_version: str = Field(min_length=1)
    minimum_snr_db: float | None = None
    required_usable_band_hz: tuple[float, float] | None = None
    minimum_polarity_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    maximum_repeatability_rms_db: float | None = Field(default=None, gt=0.0)
    profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadMeasurementQualityProfile':
        if self.minimum_snr_db is not None and not isfinite(float(self.minimum_snr_db)):
            raise ValueError('minimum_snr_db must be finite')
        if self.required_usable_band_hz is not None:
            low, high = self.required_usable_band_hz
            if not isfinite(low) or not isfinite(high) or low <= 0 or high <= low:
                raise ValueError('required usable frequency band is invalid')
        if self.profile_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement quality profile hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'profile_version': self.profile_version,
            'minimum_snr_db': self.minimum_snr_db,
            'required_usable_band_hz': (
                None
                if self.required_usable_band_hz is None
                else list(self.required_usable_band_hz)
            ),
            'minimum_polarity_confidence': self.minimum_polarity_confidence,
            'maximum_repeatability_rms_db': self.maximum_repeatability_rms_db,
        }


def build_measurement_quality_profile(
    *,
    profile_version: str = 'default-1',
    minimum_snr_db: float | None = None,
    required_usable_band_hz: tuple[float, float] | None = None,
    minimum_polarity_confidence: float | None = None,
    maximum_repeatability_rms_db: float | None = None,
) -> CadMeasurementQualityProfile:
    payload = {
        'profile_version': profile_version,
        'minimum_snr_db': None if minimum_snr_db is None else float(minimum_snr_db),
        'required_usable_band_hz': required_usable_band_hz,
        'minimum_polarity_confidence': (
            None if minimum_polarity_confidence is None else float(minimum_polarity_confidence)
        ),
        'maximum_repeatability_rms_db': (
            None if maximum_repeatability_rms_db is None else float(maximum_repeatability_rms_db)
        ),
    }
    provisional = CadMeasurementQualityProfile.model_construct(
        **payload,
        profile_sha256='0' * 64,
    )
    return CadMeasurementQualityProfile(
        **payload,
        profile_sha256=_hash(provisional.identity_payload()),
    )


class CadAcquisitionContextBinding(BaseModel):
    """Reference to an existing acquisition-context authority, not a replacement for it."""

    model_config = ConfigDict(frozen=True)

    acquisition_context_id: str = Field(min_length=1)
    acquisition_context_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_kind: AcquisitionContextSourceKind = 'unknown'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _validate_observation_field_values(
    *,
    usable_frequency_band_hz: tuple[float, float] | None,
    ir_window_start_s: float | None,
    ir_window_end_s: float | None,
    numeric: tuple[tuple[str, float | None], ...],
) -> None:
    """Shared validity rules for acquisition-observation evidence fields."""
    if usable_frequency_band_hz is not None:
        low, high = usable_frequency_band_hz
        if not isfinite(low) or not isfinite(high) or low <= 0 or high <= low:
            raise ValueError('usable frequency band is invalid')
    for name, value in numeric:
        if value is not None and not isfinite(float(value)):
            raise ValueError(f'{name} must be finite')
    if (
        ir_window_start_s is not None
        and ir_window_end_s is not None
        and ir_window_end_s <= ir_window_start_s
    ):
        raise ValueError('IR window end must be after start')


class CadMicrophoneCapture(BaseModel):
    """Microphone instrument identity and orientation at acquisition time.

    Mirrors the field-side instrument identity used by ``MicrophoneSnapshot``
    (manufacturer/model/serial, connection, sample rate, calibration profile)
    plus the physical direction the capsule actually faced — the
    ``calibration_profile`` (0°/90°) must match the direction the microphone
    was used in for the calibration to be meaningful.
    """

    model_config = ConfigDict(frozen=True)

    manufacturer: str | None = None
    model: str | None = None
    serial: str | None = None
    connection: str | None = None
    sample_rate_hz: int | None = Field(default=None, gt=0)
    calibration_profile: Literal['0deg', '90deg', 'unknown'] | None = None
    calibration_filename: str | None = None
    calibration_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    direction: Direction3 | None = None


class CadPlaybackCapture(BaseModel):
    """Playback/output condition the acquisition was taken under.

    Output device identity plus the AVR condition (model/firmware identity,
    input, volume, processing and PEQ mode). A change in any of these is a
    different acquisition condition — presets create a new context version
    rather than mutating a persisted one.
    """

    model_config = ConfigDict(frozen=True)

    output_device_label: str | None = None
    avr_manufacturer: str | None = None
    avr_model: str | None = None
    avr_firmware: str | None = None
    avr_input_name: str | None = None
    avr_volume_db: float | None = None
    avr_processing_mode: str | None = None
    avr_peq_mode: str | None = None

    @model_validator(mode='after')
    def valid_playback(self) -> 'CadPlaybackCapture':
        if self.avr_volume_db is not None and not isfinite(float(self.avr_volume_db)):
            raise ValueError('avr_volume_db must be finite')
        return self


class CadAcquisitionContext(BaseModel):
    """Persisted acquisition-context authority a report binding resolves to.

    The context is the sole authority for the ``TIMING_EVIDENCE_FIELDS`` it
    carries: a quality report bound to it must claim the context's timing
    values verbatim, and only for a measurement listed in
    ``subject_measurement_ids``. The binding's ``source_kind`` must equal the
    persisted kind — a caller cannot upgrade a ``manual``/``unknown`` context
    into ``native`` provenance by editing the binding alone. A persisted
    ``unknown`` context still resolves, but the capability algorithm keeps
    context-dependent claims UNKNOWN for it.

    ``microphone``/``playback``/``measurement_direction`` extend the context
    with instrument identity, orientation and playback/AVR condition (#471).
    ``timing_reference_sha256`` binds the exact persisted
    ``CadMeasurementTimingReference`` authority the context used (#642).
    Both remain optional: they are excluded from ``identity_payload`` when
    absent so previously persisted contexts keep their identity hash.
    """

    model_config = ConfigDict(frozen=True)

    acquisition_context_id: str = Field(min_length=1)
    source_kind: AcquisitionContextSourceKind
    subject_measurement_ids: tuple[str, ...] = Field(min_length=1)
    timing_reference_valid: bool | None = None
    timing_reference_id: str | None = None
    clock_source: str | None = None
    sample_rate_hz: int | None = Field(default=None, gt=0)
    delay_correction_s: float | None = None
    microphone: CadMicrophoneCapture | None = None
    playback: CadPlaybackCapture | None = None
    measurement_direction: Direction3 | None = None
    timing_reference_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    # #479: exact AcousticEnvironmentProfile the measurement was captured
    # under; predicted-vs-measured comparisons claim environment
    # compatibility from this authority, never from ambient assumptions.
    environment_ref: ExactExternalAuthorityRef | None = None
    # #849/#850/#858: scope/acquisition identity used to prove timing
    # reference membership, SPL-calibration applicability and the exact
    # routing profile the acquisition ran under. ``acquisition_session_id``
    # names the immutable acquisition session the capture belonged to;
    # ``signal_path_identity`` fingerprints the device/path/clock
    # configuration a persistent timing reference pins;
    # ``input_path_identity`` fingerprints the input chain (mic,
    # interface, gain, channel) an instrument-scoped level calibration
    # requires; ``routing_profile`` binds the verified channel map by
    # exact id+hash.
    acquisition_session_id: str | None = None
    signal_path_identity: str | None = None
    input_path_identity: str | None = None
    routing_profile: CadRoutingProfileBinding | None = None

    created_at_utc: str = Field(min_length=1)
    notes: tuple[str, ...] = ()
    provenance_json: str = '{}'
    acquisition_context_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_context(self) -> 'CadAcquisitionContext':
        ids = self.subject_measurement_ids
        if len(ids) != len(set(ids)) or any(not item for item in ids):
            raise ValueError(
                'subject measurement ids must be unique non-empty values'
            )
        _require_iso8601(self.created_at_utc, 'acquisition context created_at_utc')
        if self.delay_correction_s is not None and not isfinite(
            float(self.delay_correction_s)
        ):
            raise ValueError('delay_correction_s must be finite')
        if self.acquisition_context_sha256 != _hash(self.identity_payload()):
            raise ValueError('acquisition context hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'acquisition_context_id': self.acquisition_context_id,
            'source_kind': self.source_kind,
            'subject_measurement_ids': list(self.subject_measurement_ids),
            'timing_reference_valid': self.timing_reference_valid,
            'timing_reference_id': self.timing_reference_id,
            'clock_source': self.clock_source,
            'sample_rate_hz': self.sample_rate_hz,
            'delay_correction_s': self.delay_correction_s,
            'created_at_utc': self.created_at_utc,
            'notes': list(self.notes),
            'provenance_json': self.provenance_json,
        }
        # Optional post-#471/#642 fields join the sealed identity only when
        # present so contexts persisted before they existed keep their hash.
        if self.microphone is not None:
            payload['microphone'] = self.microphone.model_dump(mode='json')
        if self.playback is not None:
            payload['playback'] = self.playback.model_dump(mode='json')
        if self.measurement_direction is not None:
            payload['measurement_direction'] = (
                self.measurement_direction.model_dump(mode='json')
            )
        if self.timing_reference_sha256 is not None:
            payload['timing_reference_sha256'] = self.timing_reference_sha256
        if self.environment_ref is not None:
            payload['environment_ref'] = self.environment_ref.model_dump(mode='json')
        if self.acquisition_session_id is not None:
            payload['acquisition_session_id'] = self.acquisition_session_id
        if self.signal_path_identity is not None:
            payload['signal_path_identity'] = self.signal_path_identity
        if self.input_path_identity is not None:
            payload['input_path_identity'] = self.input_path_identity
        if self.routing_profile is not None:
            payload['routing_profile'] = self.routing_profile.model_dump(mode='json')

        return payload


class CadMeasurementObservation(BaseModel):
    """Immutable observation authority for per-measurement acquisition metadata.

    Subject, timestamp and provenance are explicit: ``measurement_id`` binds
    the exact measurement observed, ``observed_at_utc`` records when the
    observation was taken, and ``source_kind`` declares where the values came
    from. Machine-extracted observations (``MACHINE_OBSERVATION_SOURCES``)
    must pin the exact artifact they were derived from in
    ``source_asset_sha256`` — the repository requires that pin to equal the
    subject measurement's verified raw asset. ``manual``/``unknown``
    observations are themselves the authority and must not bind a source
    asset. At least one evidence field must be attested.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    observed_at_utc: str = Field(min_length=1)
    source_kind: EvidenceSourceKind
    source_asset_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    clipping_detected: bool | None = None
    peak_dbfs: float | None = None
    noise_floor_db_spl: float | None = None
    signal_level_db_spl: float | None = None
    snr_db: float | None = None
    usable_frequency_band_hz: tuple[float, float] | None = None
    polarity_correct: bool | None = None
    polarity_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    has_impulse_response: bool = False
    ir_window_start_s: float | None = None
    ir_window_end_s: float | None = None
    ir_truncated: bool | None = None

    notes: tuple[str, ...] = ()
    provenance_json: str = '{}'
    observation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadMeasurementObservation':
        _require_iso8601(self.observed_at_utc, 'observation observed_at_utc')
        if self.source_kind in MACHINE_OBSERVATION_SOURCES:
            if self.source_asset_sha256 is None:
                raise ValueError(
                    'machine-derived observation must pin its source asset'
                )
        elif self.source_asset_sha256 is not None:
            raise ValueError(
                'manual/unknown observation must not bind a source asset'
            )
        _validate_observation_field_values(
            usable_frequency_band_hz=self.usable_frequency_band_hz,
            ir_window_start_s=self.ir_window_start_s,
            ir_window_end_s=self.ir_window_end_s,
            numeric=(
                ('peak_dbfs', self.peak_dbfs),
                ('noise_floor_db_spl', self.noise_floor_db_spl),
                ('signal_level_db_spl', self.signal_level_db_spl),
                ('snr_db', self.snr_db),
                ('ir_window_start_s', self.ir_window_start_s),
                ('ir_window_end_s', self.ir_window_end_s),
            ),
        )
        if all(
            getattr(self, name) == OBSERVATION_FIELD_DEFAULTS[name]
            for name in OBSERVATION_EVIDENCE_FIELDS
        ):
            raise ValueError(
                'measurement observation must attest at least one evidence field'
            )
        if self.observation_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement observation hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'observation_id': self.observation_id,
            'measurement_id': self.measurement_id,
            'observed_at_utc': self.observed_at_utc,
            'source_kind': self.source_kind,
            'source_asset_sha256': self.source_asset_sha256,
            **{name: getattr(self, name) for name in OBSERVATION_EVIDENCE_FIELDS},
            'notes': list(self.notes),
            'provenance_json': self.provenance_json,
        }


class CadMeasurementObservationBinding(BaseModel):
    """Reference to a persisted measurement-observation authority."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


# Default (unattested) value of each observation evidence field. A report may
# only carry a non-default value here by binding a persisted
# ``CadMeasurementObservation`` that attests the same value verbatim.
OBSERVATION_FIELD_DEFAULTS: dict[str, Any] = {
    name: (False if name == 'has_impulse_response' else None)
    for name in OBSERVATION_EVIDENCE_FIELDS
}


def build_acquisition_context(
    *,
    source_kind: AcquisitionContextSourceKind,
    subject_measurement_ids: Sequence[str],
    timing_reference_valid: bool | None = None,
    timing_reference_id: str | None = None,
    clock_source: str | None = None,
    sample_rate_hz: int | None = None,
    delay_correction_s: float | None = None,
    microphone: CadMicrophoneCapture | dict[str, Any] | None = None,
    playback: CadPlaybackCapture | dict[str, Any] | None = None,
    measurement_direction: Direction3 | None = None,
    timing_reference_sha256: str | None = None,
    environment_ref: ExactExternalAuthorityRef | None = None,
    acquisition_session_id: str | None = None,
    signal_path_identity: str | None = None,
    input_path_identity: str | None = None,
    routing_profile: CadRoutingProfileBinding | None = None,

    acquisition_context_id: str | None = None,
    created_at_utc: str | None = None,
    notes: Sequence[str] = (),
    provenance_json: str = '{}',
) -> CadAcquisitionContext:
    """Assemble a sealed acquisition-context authority record.

    The returned record only becomes authoritative once persisted through the
    quality repository, which re-validates that every subject measurement
    exists before a report binding can resolve to it.
    """
    if isinstance(microphone, dict):
        microphone = CadMicrophoneCapture.model_validate(microphone)
    if isinstance(playback, dict):
        playback = CadPlaybackCapture.model_validate(playback)
    payload: dict[str, Any] = {
        'acquisition_context_id': acquisition_context_id or str(uuid4()),
        'source_kind': source_kind,
        'subject_measurement_ids': tuple(subject_measurement_ids),
        'timing_reference_valid': timing_reference_valid,
        'timing_reference_id': timing_reference_id,
        'clock_source': clock_source,
        'sample_rate_hz': sample_rate_hz,
        'delay_correction_s': delay_correction_s,
        'microphone': microphone,
        'playback': playback,
        'measurement_direction': measurement_direction,
        'timing_reference_sha256': timing_reference_sha256,
        'environment_ref': environment_ref,
        'acquisition_session_id': acquisition_session_id,
        'signal_path_identity': signal_path_identity,
        'input_path_identity': input_path_identity,
        'routing_profile': routing_profile,

        'created_at_utc': created_at_utc or datetime.now(timezone.utc).isoformat(),
        'notes': tuple(notes),
        'provenance_json': provenance_json,
    }
    provisional = CadAcquisitionContext.model_construct(
        **payload,
        acquisition_context_sha256='0' * 64,
    )
    return CadAcquisitionContext(
        **payload,
        acquisition_context_sha256=_hash(provisional.identity_payload()),
    )


def build_measurement_observation(
    *,
    measurement_id: str,
    source_kind: EvidenceSourceKind,
    source_asset_sha256: str | None = None,
    observed_at_utc: str | None = None,
    observation_id: str | None = None,
    clipping_detected: bool | None = None,
    peak_dbfs: float | None = None,
    noise_floor_db_spl: float | None = None,
    signal_level_db_spl: float | None = None,
    snr_db: float | None = None,
    usable_frequency_band_hz: tuple[float, float] | None = None,
    polarity_correct: bool | None = None,
    polarity_confidence: float | None = None,
    has_impulse_response: bool = False,
    ir_window_start_s: float | None = None,
    ir_window_end_s: float | None = None,
    ir_truncated: bool | None = None,
    notes: Sequence[str] = (),
    provenance_json: str = '{}',
) -> CadMeasurementObservation:
    """Assemble a sealed observation authority for one exact measurement.

    The record only becomes authoritative once persisted through the quality
    repository, which re-validates the subject binding and — for machine
    ``source_kind`` values — that ``source_asset_sha256`` is the subject
    measurement's verified raw asset.
    """
    payload: dict[str, Any] = {
        'observation_id': observation_id or str(uuid4()),
        'measurement_id': measurement_id,
        'observed_at_utc': observed_at_utc or datetime.now(timezone.utc).isoformat(),
        'source_kind': source_kind,
        'source_asset_sha256': source_asset_sha256,
        'clipping_detected': clipping_detected,
        'peak_dbfs': peak_dbfs,
        'noise_floor_db_spl': noise_floor_db_spl,
        'signal_level_db_spl': signal_level_db_spl,
        'snr_db': snr_db,
        'usable_frequency_band_hz': usable_frequency_band_hz,
        'polarity_correct': polarity_correct,
        'polarity_confidence': polarity_confidence,
        'has_impulse_response': has_impulse_response,
        'ir_window_start_s': ir_window_start_s,
        'ir_window_end_s': ir_window_end_s,
        'ir_truncated': ir_truncated,
        'notes': tuple(notes),
        'provenance_json': provenance_json,
    }
    provisional = CadMeasurementObservation.model_construct(
        **payload,
        observation_sha256='0' * 64,
    )
    return CadMeasurementObservation(
        **payload,
        observation_sha256=_hash(provisional.identity_payload()),
    )


def observation_binding(
    observation: CadMeasurementObservation,
) -> CadMeasurementObservationBinding:
    """Exact id/hash binding for a persisted observation authority."""
    return CadMeasurementObservationBinding(
        observation_id=observation.observation_id,
        observation_sha256=observation.observation_sha256,
    )


def acquisition_context_binding(
    context: CadAcquisitionContext,
) -> CadAcquisitionContextBinding:
    """Exact id/hash binding for a persisted acquisition-context authority."""
    return CadAcquisitionContextBinding(
        acquisition_context_id=context.acquisition_context_id,
        acquisition_context_sha256=context.acquisition_context_sha256,
        source_kind=context.source_kind,
    )


class CadMeasurementQualityEvidence(BaseModel):
    """Explicit metadata/raw evidence consumed by the quality evaluator.

    None means no evidence was supplied. The evaluator never derives these fields
    from frequency/level arrays.
    """

    model_config = ConfigDict(frozen=True)

    clipping_detected: bool | None = None
    peak_dbfs: float | None = None

    noise_floor_db_spl: float | None = None
    signal_level_db_spl: float | None = None
    snr_db: float | None = None

    usable_frequency_band_hz: tuple[float, float] | None = None

    timing_reference_valid: bool | None = None
    timing_reference_id: str | None = None
    clock_source: str | None = None
    sample_rate_hz: int | None = Field(default=None, gt=0)
    delay_correction_s: float | None = None

    polarity_correct: bool | None = None
    polarity_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    has_impulse_response: bool = False
    ir_window_start_s: float | None = None
    ir_window_end_s: float | None = None
    ir_truncated: bool | None = None

    calibration_filename: str | None = None
    calibration_file_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    expected_calibration_file_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    repeat_measurement_ids: tuple[str, ...] = ()
    repeatability_rms_db: float | None = Field(default=None, ge=0.0)

    evidence_source: Literal['rew_metadata', 'raw_asset', 'manual', 'mixed', 'unknown'] = 'unknown'
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_evidence(self) -> 'CadMeasurementQualityEvidence':
        _validate_observation_field_values(
            usable_frequency_band_hz=self.usable_frequency_band_hz,
            ir_window_start_s=self.ir_window_start_s,
            ir_window_end_s=self.ir_window_end_s,
            numeric=(
                ('peak_dbfs', self.peak_dbfs),
                ('noise_floor_db_spl', self.noise_floor_db_spl),
                ('signal_level_db_spl', self.signal_level_db_spl),
                ('snr_db', self.snr_db),
                ('delay_correction_s', self.delay_correction_s),
                ('ir_window_start_s', self.ir_window_start_s),
                ('ir_window_end_s', self.ir_window_end_s),
                ('repeatability_rms_db', self.repeatability_rms_db),
            ),
        )
        if len(self.repeat_measurement_ids) != len(set(self.repeat_measurement_ids)):
            raise ValueError('repeat measurement ids must be unique')
        if any(not item for item in self.repeat_measurement_ids):
            raise ValueError('repeat measurement ids must not contain empty values')
        return self


class CadMeasurementQualityCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: QualityDecision
    reason: str = Field(min_length=1)


class CadMeasurementCapability(BaseModel):
    model_config = ConfigDict(frozen=True)

    claim: MeasurementCapabilityClaim
    decision: CapabilityDecision
    reasons: tuple[str, ...] = Field(min_length=1)


class CadMeasurementQualityReport(BaseModel):
    """Immutable evidence report for one exact native measurement dataset."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)

    measurement_id: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_entity_id: str = Field(min_length=1)
    measurement_position: Position3
    acquisition_context: CadAcquisitionContextBinding | None = None
    observation: CadMeasurementObservationBinding | None = None

    algorithm_version: str = Field(min_length=1)
    algorithm_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    profile: CadMeasurementQualityProfile
    evidence: CadMeasurementQualityEvidence

    clipping: CadMeasurementQualityCheck
    noise_snr: CadMeasurementQualityCheck
    usable_frequency_band: CadMeasurementQualityCheck
    timing_reference: CadMeasurementQualityCheck
    polarity: CadMeasurementQualityCheck
    ir_window: CadMeasurementQualityCheck
    calibration: CadMeasurementQualityCheck
    repeatability: CadMeasurementQualityCheck

    retake_recommendation: RetakeRecommendation
    retake_reasons: tuple[str, ...]
    capabilities: tuple[CadMeasurementCapability, ...] = Field(min_length=1)
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'CadMeasurementQualityReport':
        claims = tuple(item.claim for item in self.capabilities)
        if len(claims) != len(set(claims)):
            raise ValueError('measurement capability matrix must not contain duplicate claims')
        canonical_subset = tuple(claim for claim in _ALL_CAPABILITY_CLAIMS if claim in claims)
        if claims != canonical_subset:
            raise ValueError('measurement capability matrix must use canonical claim ordering')
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement quality report hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = {
            'report_id': self.report_id,
            'created_at_utc': self.created_at_utc,
            'measurement_id': self.measurement_id,
            'measurement_sha256': self.measurement_sha256,
            'dataset_id': self.dataset_id,
            'dataset_sha256': self.dataset_sha256,
            'raw_asset_sha256': self.raw_asset_sha256,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'measurement_entity_id': self.measurement_entity_id,
            'measurement_position': self.measurement_position.model_dump(mode='json'),
            'acquisition_context': (
                None
                if self.acquisition_context is None
                else self.acquisition_context.model_dump(mode='json')
            ),
            'algorithm_version': self.algorithm_version,
            'algorithm_sha256': self.algorithm_sha256,
            'profile': self.profile.model_dump(mode='json'),
            'evidence': self.evidence.model_dump(mode='json'),
            'clipping': self.clipping.model_dump(mode='json'),
            'noise_snr': self.noise_snr.model_dump(mode='json'),
            'usable_frequency_band': self.usable_frequency_band.model_dump(mode='json'),
            'timing_reference': self.timing_reference.model_dump(mode='json'),
            'polarity': self.polarity.model_dump(mode='json'),
            'ir_window': self.ir_window.model_dump(mode='json'),
            'calibration': self.calibration.model_dump(mode='json'),
            'repeatability': self.repeatability.model_dump(mode='json'),
            'retake_recommendation': self.retake_recommendation,
            'retake_reasons': list(self.retake_reasons),
            'capabilities': [item.model_dump(mode='json') for item in self.capabilities],
        }
        # ``observation`` joined the identity payload with typed observation
        # authority (#392). Reports persisted before it existed carry no such
        # key, so an unbound observation is omitted entirely — their sealed
        # identity hash stays reproducible and honest legacy reports still
        # self-verify on read.
        if self.observation is not None:
            payload['observation'] = self.observation.model_dump(mode='json')
        return payload

    def capability(self, claim: MeasurementCapabilityClaim) -> CadMeasurementCapability:
        for item in self.capabilities:
            if item.claim == claim:
                return item
        raise KeyError(claim)

    def allows(self, claim: MeasurementCapabilityClaim) -> bool:
        return self.capability(claim).decision == 'ALLOWED'


class CadMeasurementLineageRecord(BaseModel):
    """Append-only retake/selection evidence. Measurements themselves are never rewritten."""

    model_config = ConfigDict(frozen=True)

    lineage_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    supersedes_measurement_id: str = Field(min_length=1)
    selected_measurement_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    lineage_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_lineage(self) -> 'CadMeasurementLineageRecord':
        if self.measurement_id == self.supersedes_measurement_id:
            raise ValueError('retake measurement must differ from superseded measurement')
        if self.selected_measurement_id not in {
            self.measurement_id,
            self.supersedes_measurement_id,
        }:
            raise ValueError('selected measurement must be one side of the retake relation')
        if self.lineage_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement lineage hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'lineage_id': self.lineage_id,
            'document_id': self.document_id,
            'measurement_id': self.measurement_id,
            'supersedes_measurement_id': self.supersedes_measurement_id,
            'selected_measurement_id': self.selected_measurement_id,
            'reason': self.reason,
            'created_at_utc': self.created_at_utc,
        }


def build_measurement_lineage(
    *,
    document_id: str,
    measurement_id: str,
    supersedes_measurement_id: str,
    selected_measurement_id: str,
    reason: str,
    lineage_id: str | None = None,
    created_at_utc: str | None = None,
) -> CadMeasurementLineageRecord:
    payload = {
        'lineage_id': lineage_id or str(uuid4()),
        'document_id': document_id,
        'measurement_id': measurement_id,
        'supersedes_measurement_id': supersedes_measurement_id,
        'selected_measurement_id': selected_measurement_id,
        'reason': reason,
        'created_at_utc': created_at_utc or datetime.now(timezone.utc).isoformat(),
    }
    return CadMeasurementLineageRecord(
        **payload,
        lineage_sha256=_hash(payload),
    )


def _check(status: QualityDecision, reason: str) -> CadMeasurementQualityCheck:
    return CadMeasurementQualityCheck(status=status, reason=reason)


def _status_to_capability(
    claim: MeasurementCapabilityClaim,
    check: CadMeasurementQualityCheck,
) -> CadMeasurementCapability:
    if check.status == 'PASS':
        decision: CapabilityDecision = 'ALLOWED'
    elif check.status == 'FAIL':
        decision = 'BLOCKED'
    else:
        decision = 'UNKNOWN'
    return CadMeasurementCapability(claim=claim, decision=decision, reasons=(check.reason,))


def _derive_checks(
    evidence: CadMeasurementQualityEvidence,
    profile: CadMeasurementQualityProfile,
) -> dict[str, CadMeasurementQualityCheck]:
    if evidence.clipping_detected is None:
        clipping = _check('UNKNOWN', 'clipping metadata is unavailable')
    elif evidence.clipping_detected:
        clipping = _check('FAIL', 'acquisition metadata reports clipping')
    else:
        clipping = _check('PASS', 'acquisition metadata reports no clipping')

    if evidence.snr_db is None:
        noise_snr = _check('UNKNOWN', 'explicit SNR evidence is unavailable')
    elif profile.minimum_snr_db is None:
        noise_snr = _check('NOT_EVALUATED', 'SNR evidence exists but the profile has no SNR threshold')
    elif evidence.snr_db < profile.minimum_snr_db:
        noise_snr = _check(
            'FAIL',
            f'SNR {evidence.snr_db:.3f} dB is below profile minimum '
            f'{profile.minimum_snr_db:.3f} dB',
        )
    else:
        noise_snr = _check(
            'PASS',
            f'SNR {evidence.snr_db:.3f} dB meets profile minimum '
            f'{profile.minimum_snr_db:.3f} dB',
        )

    usable = evidence.usable_frequency_band_hz
    if usable is None:
        usable_band = _check('UNKNOWN', 'usable frequency band evidence is unavailable')
    elif profile.required_usable_band_hz is None:
        usable_band = _check('PASS', 'explicit usable frequency band is recorded')
    else:
        req_low, req_high = profile.required_usable_band_hz
        low, high = usable
        if low <= req_low and high >= req_high:
            usable_band = _check('PASS', 'usable frequency band covers the profile requirement')
        else:
            usable_band = _check('FAIL', 'usable frequency band does not cover the profile requirement')

    if evidence.timing_reference_valid is False:
        timing = _check('FAIL', 'acquisition metadata reports an invalid timing reference')
    elif evidence.timing_reference_valid is None:
        timing = _check('UNKNOWN', 'common timing reference evidence is unavailable')
    elif not (
        evidence.timing_reference_id
        and evidence.clock_source
        and evidence.sample_rate_hz is not None
        and evidence.delay_correction_s is not None
    ):
        timing = _check('UNKNOWN', 'timing reference metadata is incomplete')
    else:
        timing = _check('PASS', 'timing reference identity, clock, sample rate and delay correction are recorded')

    if evidence.polarity_correct is False:
        polarity = _check('FAIL', 'polarity evidence reports reversed polarity')
    elif evidence.polarity_correct is None or evidence.polarity_confidence is None:
        polarity = _check('UNKNOWN', 'polarity evidence or confidence is unavailable')
    elif profile.minimum_polarity_confidence is None:
        polarity = _check(
            'NOT_EVALUATED',
            'polarity evidence exists but the profile has no confidence threshold',
        )
    elif evidence.polarity_confidence < profile.minimum_polarity_confidence:
        polarity = _check('UNKNOWN', 'polarity confidence is below the profile confidence threshold')
    else:
        polarity = _check('PASS', 'polarity evidence meets the profile confidence threshold')

    if not evidence.has_impulse_response:
        ir_window = _check('NOT_EVALUATED', 'no impulse-response evidence is bound to this report')
    elif evidence.ir_truncated is None:
        ir_window = _check('UNKNOWN', 'IR truncation evidence is unavailable')
    elif evidence.ir_truncated:
        ir_window = _check('FAIL', 'IR evidence is reported as truncated')
    elif evidence.ir_window_start_s is None or evidence.ir_window_end_s is None:
        ir_window = _check('UNKNOWN', 'IR window bounds are unavailable')
    else:
        ir_window = _check('PASS', 'IR window bounds are recorded and truncation is not reported')

    expected_cal = evidence.expected_calibration_file_sha256
    applied_cal = evidence.calibration_file_sha256
    if expected_cal is None and applied_cal is None:
        calibration = _check('UNKNOWN', 'calibration-file provenance is unavailable')
    elif expected_cal is None or applied_cal is None:
        calibration = _check('UNKNOWN', 'calibration-file provenance is incomplete')
    elif expected_cal != applied_cal:
        calibration = _check('FAIL', 'applied calibration file does not match the expected calibration file')
    else:
        calibration = _check('PASS', 'applied calibration file matches the expected calibration file')

    if len(evidence.repeat_measurement_ids) < 2 or evidence.repeatability_rms_db is None:
        repeatability = _check('NOT_EVALUATED', 'repeatability evidence requires at least two measurements and an explicit metric')
    elif profile.maximum_repeatability_rms_db is None:
        repeatability = _check(
            'NOT_EVALUATED',
            'repeatability evidence exists but the profile has no repeatability threshold',
        )
    elif evidence.repeatability_rms_db > profile.maximum_repeatability_rms_db:
        repeatability = _check(
            'FAIL',
            f'repeatability RMS {evidence.repeatability_rms_db:.3f} dB exceeds profile maximum '
            f'{profile.maximum_repeatability_rms_db:.3f} dB',
        )
    else:
        repeatability = _check(
            'PASS',
            f'repeatability RMS {evidence.repeatability_rms_db:.3f} dB meets profile maximum '
            f'{profile.maximum_repeatability_rms_db:.3f} dB',
        )

    return {
        'clipping': clipping,
        'noise_snr': noise_snr,
        'usable_frequency_band': usable_band,
        'timing_reference': timing,
        'polarity': polarity,
        'ir_window': ir_window,
        'calibration': calibration,
        'repeatability': repeatability,
    }


def phase_response_capability(
    dataset: CadFrequencyResponseDataset,
) -> CadMeasurementCapability:
    """Canonical dataset-only phase_response claim.

    Valid phase samples authorize phase-response inspection only. They never
    imply a common timing reference — ``common_timing`` requires explicit
    timing-reference evidence plus an authoritative acquisition context.
    """

    if dataset.phase_status == 'valid' and dataset.phase_deg is not None:
        return CadMeasurementCapability(
            claim='phase_response',
            decision='ALLOWED',
            reasons=('dataset contains phase explicitly marked valid',),
        )
    if dataset.phase_status == 'absent':
        return CadMeasurementCapability(
            claim='phase_response',
            decision='BLOCKED',
            reasons=('dataset explicitly has no phase evidence',),
        )
    return CadMeasurementCapability(
        claim='phase_response',
        decision='UNKNOWN',
        reasons=('phase evidence is not verified',),
    )


def unestablished_common_timing_capability() -> CadMeasurementCapability:
    """Fail-closed common_timing claim used when no quality report exists.

    Without a replay-validated ``CadMeasurementQualityReport`` there is no
    timing authority at all, so common timing stays UNKNOWN regardless of any
    phase samples the dataset may contain.
    """

    return CadMeasurementCapability(
        claim='common_timing',
        decision='UNKNOWN',
        reasons=('no measurement quality report is bound to this dataset',),
    )


def unestablished_capability_claims(
    dataset: CadFrequencyResponseDataset,
) -> tuple[CadMeasurementCapability, ...]:
    """Fail-closed claim matrix used when no quality report exists.

    Only claims decidable from the dataset alone keep their dataset-only
    verdicts (the immutable FR samples support magnitude inspection, and
    phase_response follows the canonical dataset rule). Every claim that
    needs acquisition evidence fails closed at UNKNOWN — never implicit
    ALLOWED/BLOCKED — in canonical claim order.
    """

    def _unestablished(claim: MeasurementCapabilityClaim) -> CadMeasurementCapability:
        return CadMeasurementCapability(
            claim=claim,
            decision='UNKNOWN',
            reasons=('no measurement quality report is bound to this dataset',),
        )

    return (
        CadMeasurementCapability(
            claim='magnitude_response',
            decision='ALLOWED',
            reasons=('immutable frequency/level dataset is present',),
        ),
        phase_response_capability(dataset),
        unestablished_common_timing_capability(),
        _unestablished('arrival_time'),
        _unestablished('decay'),
        _unestablished('calibrated_response'),
        _unestablished('repeatability'),
        _unestablished('polarity'),
    )


def derive_measurement_capabilities(
    *,
    dataset: CadFrequencyResponseDataset,
    acquisition_context: CadAcquisitionContextBinding | None,
    evidence: CadMeasurementQualityEvidence,
    checks: dict[str, CadMeasurementQualityCheck],
) -> tuple[CadMeasurementCapability, ...]:
    magnitude = CadMeasurementCapability(
        claim='magnitude_response',
        decision='ALLOWED',
        reasons=('immutable frequency/level dataset is present',),
    )

    phase = phase_response_capability(dataset)

    timing_check = checks['timing_reference']
    authoritative_context = (
        acquisition_context is not None
        and acquisition_context.source_kind != 'unknown'
    )
    if timing_check.status == 'FAIL':
        common_timing = _status_to_capability('common_timing', timing_check)
    elif not authoritative_context:
        common_timing = CadMeasurementCapability(
            claim='common_timing',
            decision='UNKNOWN',
            reasons=('authoritative AcquisitionContext binding is unavailable',),
        )
    else:
        common_timing = _status_to_capability('common_timing', timing_check)

    if not evidence.has_impulse_response:
        arrival = CadMeasurementCapability(
            claim='arrival_time',
            decision='BLOCKED',
            reasons=('arrival-time claims require impulse-response evidence',),
        )
        decay = CadMeasurementCapability(
            claim='decay',
            decision='BLOCKED',
            reasons=('decay claims require impulse-response evidence',),
        )
    else:
        if common_timing.decision == 'BLOCKED' or checks['ir_window'].status == 'FAIL':
            arrival_decision: CapabilityDecision = 'BLOCKED'
        elif common_timing.decision == 'ALLOWED' and checks['ir_window'].status == 'PASS':
            arrival_decision = 'ALLOWED'
        else:
            arrival_decision = 'UNKNOWN'
        arrival = CadMeasurementCapability(
            claim='arrival_time',
            decision=arrival_decision,
            reasons=(
                common_timing.reasons[0],
                checks['ir_window'].reason,
            ),
        )
        decay = _status_to_capability('decay', checks['ir_window'])

    calibrated_checks = (
        checks['clipping'],
        checks['noise_snr'],
        checks['usable_frequency_band'],
        checks['calibration'],
    )
    calibrated_reasons = tuple(check.reason for check in calibrated_checks)
    if any(check.status == 'FAIL' for check in calibrated_checks):
        calibrated = CadMeasurementCapability(
            claim='calibrated_response',
            decision='BLOCKED',
            reasons=calibrated_reasons,
        )
    elif not authoritative_context:
        calibrated = CadMeasurementCapability(
            claim='calibrated_response',
            decision='UNKNOWN',
            reasons=calibrated_reasons + ('authoritative AcquisitionContext binding is unavailable',),
        )
    elif all(check.status == 'PASS' for check in calibrated_checks):
        calibrated = CadMeasurementCapability(
            claim='calibrated_response',
            decision='ALLOWED',
            reasons=calibrated_reasons,
        )
    else:
        calibrated = CadMeasurementCapability(
            claim='calibrated_response',
            decision='UNKNOWN',
            reasons=calibrated_reasons,
        )

    repeatability = _status_to_capability('repeatability', checks['repeatability'])
    polarity = _status_to_capability('polarity', checks['polarity'])

    return (
        magnitude,
        phase,
        common_timing,
        arrival,
        decay,
        calibrated,
        repeatability,
        polarity,
    )


def _retake(checks: dict[str, CadMeasurementQualityCheck]) -> tuple[RetakeRecommendation, tuple[str, ...]]:
    failures = tuple(
        f'{name}: {check.reason}'
        for name, check in checks.items()
        if check.status == 'FAIL'
    )
    if failures:
        return 'RETAKE', failures
    unknown = tuple(
        f'{name}: {check.reason}'
        for name, check in checks.items()
        if check.status == 'UNKNOWN'
    )
    if unknown:
        return 'UNKNOWN', unknown
    return 'NOT_NEEDED', ()


@dataclass(frozen=True, slots=True)
class MeasurementRetakeGuidance:
    """Structured retake guidance derived from a replay-validated report.

    All fields are stable machine-readable codes/identifiers so read models
    and UX layers can localize without re-interpreting authority reasons:

    - ``failed_checks`` / ``unknown_checks`` / ``not_evaluated_checks``:
      independent-check names grouped by outcome;
    - ``missing_evidence``: which acquisition context/evidence is absent —
      ``'clipping_metadata'``, ``'snr_evidence'``, ``'usable_band_evidence'``,
      ``'timing_reference_evidence'``, ``'polarity_evidence'``,
      ``'impulse_response'``, ``'ir_window_evidence'``,
      ``'calibration_provenance'``, ``'repeat_measurements'`` or
      ``'acquisition_context'``;
    - ``remeasure``: what to re-measure — ``'same_binding'``,
      ``'timed_acquisition'``, ``'calibrated_microphone'``,
      ``'repeat_measurement'``, ``'impulse_response_capture'`` or
      ``'acquisition_metadata'``.
    """

    recommendation: RetakeRecommendation
    reasons: tuple[str, ...]
    failed_checks: tuple[str, ...]
    unknown_checks: tuple[str, ...]
    not_evaluated_checks: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    remeasure: tuple[str, ...]


def measurement_retake_guidance(
    report: CadMeasurementQualityReport,
) -> MeasurementRetakeGuidance:
    """Explain what a report's retake recommendation means for acquisition.

    The guidance never upgrades missing evidence: checks stay FAIL / UNKNOWN /
    NOT_EVALUATED and the missing-evidence list names exactly which explicit
    inputs were absent so the user knows what a retake must capture.
    """

    evidence = report.evidence
    checks = {name: getattr(report, name) for name in MEASUREMENT_QUALITY_CHECKS}
    failed = tuple(name for name, check in checks.items() if check.status == 'FAIL')
    unknown = tuple(name for name, check in checks.items() if check.status == 'UNKNOWN')
    not_evaluated = tuple(
        name for name, check in checks.items() if check.status == 'NOT_EVALUATED'
    )

    missing: list[str] = []
    if evidence.clipping_detected is None:
        missing.append('clipping_metadata')
    if evidence.snr_db is None:
        missing.append('snr_evidence')
    if evidence.usable_frequency_band_hz is None:
        missing.append('usable_band_evidence')
    if not (
        evidence.timing_reference_valid
        and evidence.timing_reference_id
        and evidence.clock_source
        and evidence.sample_rate_hz is not None
        and evidence.delay_correction_s is not None
    ):
        missing.append('timing_reference_evidence')
    if evidence.polarity_correct is None or evidence.polarity_confidence is None:
        missing.append('polarity_evidence')
    if not evidence.has_impulse_response:
        missing.append('impulse_response')
    elif (
        evidence.ir_truncated is None
        or evidence.ir_window_start_s is None
        or evidence.ir_window_end_s is None
    ):
        missing.append('ir_window_evidence')
    if (
        evidence.calibration_file_sha256 is None
        or evidence.expected_calibration_file_sha256 is None
    ):
        missing.append('calibration_provenance')
    if (
        len(evidence.repeat_measurement_ids) < 2
        or evidence.repeatability_rms_db is None
    ):
        missing.append('repeat_measurements')
    authoritative_context = (
        report.acquisition_context is not None
        and report.acquisition_context.source_kind != 'unknown'
    )
    if not authoritative_context:
        missing.append('acquisition_context')

    remeasure: list[str] = []
    if report.retake_recommendation == 'RETAKE':
        remeasure.append('same_binding')
    if 'timing_reference_evidence' in missing or 'acquisition_context' in missing:
        remeasure.append('timed_acquisition')
    if 'calibration_provenance' in missing:
        remeasure.append('calibrated_microphone')
    if 'repeat_measurements' in missing:
        remeasure.append('repeat_measurement')
    if 'impulse_response' in missing or 'ir_window_evidence' in missing:
        remeasure.append('impulse_response_capture')
    if any(
        code in missing
        for code in (
            'clipping_metadata',
            'snr_evidence',
            'usable_band_evidence',
            'polarity_evidence',
        )
    ):
        remeasure.append('acquisition_metadata')

    return MeasurementRetakeGuidance(
        recommendation=report.retake_recommendation,
        reasons=report.retake_reasons,
        failed_checks=failed,
        unknown_checks=unknown,
        not_evaluated_checks=not_evaluated,
        missing_evidence=tuple(missing),
        remeasure=tuple(remeasure),
    )


REPEATABILITY_METRIC_VERSION = 'measurement-repeatability-1'

# Canonical repeatability metric identity: the only repeatability RMS a
# quality report may claim is this exact computation over the resolved
# immutable repeat datasets. A caller-supplied value that diverges from the
# recomputation is rejected by the repository rather than trusted.
REPEATABILITY_METRIC_IDENTITY = {
    'metric_version': REPEATABILITY_METRIC_VERSION,
    'statistic': 'rms_db_of_per_sample_deviation_from_per_frequency_mean_level',
    'band': 'full_shared_frequency_grid',
    'grid_requirement': 'identical_frequency_hz_tuple',
    'level_reference_requirement': 'identical_level_reference',
    'minimum_datasets': 2,
}
REPEATABILITY_METRIC_SHA256 = _hash(REPEATABILITY_METRIC_IDENTITY)


def measurement_repeatability_rms_db(
    datasets: Sequence[CadFrequencyResponseDataset],
) -> float:
    """Canonical repeatability RMS (dB) over exact immutable repeat datasets.

    For every shared frequency point, each dataset's level deviates from the
    per-frequency mean level; the metric is the RMS of all such deviations
    over the full shared grid — no band selection, no interpolation, no
    smoothing. All datasets must sit on the identical frequency grid and
    declare the same ``level_reference``; anything else fails closed instead
    of comparing incomparable levels or silently narrowing the band.
    """

    if len(datasets) < 2:
        raise ValueError('repeatability requires at least two repeat datasets')
    grid = datasets[0].frequency_hz
    level_reference = datasets[0].level_reference
    for dataset in datasets[1:]:
        if dataset.frequency_hz != grid:
            raise ValueError(
                'repeat datasets must share the identical frequency grid'
            )
        if dataset.level_reference != level_reference:
            raise ValueError(
                'repeat datasets must share the identical level reference'
            )
    count = len(datasets)
    total = 0.0
    samples = 0
    for index in range(len(grid)):
        mean = sum(dataset.level_db[index] for dataset in datasets) / count
        for dataset in datasets:
            deviation = dataset.level_db[index] - mean
            total += deviation * deviation
            samples += 1
    return sqrt(total / samples)


def build_measurement_quality_report(
    *,
    measurement: CadMeasurementRecord,
    dataset: CadFrequencyResponseDataset,
    evidence: CadMeasurementQualityEvidence,
    profile: CadMeasurementQualityProfile,
    acquisition_context: CadAcquisitionContextBinding | None = None,
    observation: CadMeasurementObservationBinding | None = None,
    report_id: str | None = None,
    created_at_utc: str | None = None,
) -> CadMeasurementQualityReport:
    if dataset.measurement_id != measurement.measurement_id:
        raise ValueError('quality report dataset does not belong to measurement')

    checks = _derive_checks(evidence, profile)
    capabilities = derive_measurement_capabilities(
        dataset=dataset,
        acquisition_context=acquisition_context,
        evidence=evidence,
        checks=checks,
    )
    retake_recommendation, retake_reasons = _retake(checks)

    payload: dict[str, Any] = {
        'report_id': report_id or str(uuid4()),
        'created_at_utc': created_at_utc or datetime.now(timezone.utc).isoformat(),
        'measurement_id': measurement.measurement_id,
        'measurement_sha256': measurement_sha256(measurement),
        'dataset_id': dataset.dataset_id,
        'dataset_sha256': dataset_sha256(dataset),
        'raw_asset_sha256': dataset.source_sha256,
        'document_id': measurement.document_id,
        'scene_revision_id': measurement.scene_revision_id,
        'scene_content_hash': measurement.scene_content_hash,
        'measurement_entity_id': measurement.measurement_entity_id,
        'measurement_position': measurement.measurement_position,
        'acquisition_context': acquisition_context,
        'observation': observation,
        'algorithm_version': QUALITY_ALGORITHM_VERSION,
        'algorithm_sha256': QUALITY_ALGORITHM_SHA256,
        'profile': profile,
        'evidence': evidence,
        **checks,
        'retake_recommendation': retake_recommendation,
        'retake_reasons': retake_reasons,
        'capabilities': capabilities,
    }
    provisional = CadMeasurementQualityReport.model_construct(
        **payload,
        report_sha256='0' * 64,
    )
    return CadMeasurementQualityReport(
        **payload,
        report_sha256=_hash(provisional.identity_payload()),
    )


# Explicit versioned replay support: every algorithm version that can produce
# persisted reports keeps its pinned identity hash and builder here so
# historical reports stay replayable. Reports pinned to an identity that is not
# registered fail closed instead of silently trusting payload-only decisions.
QualityReportReplay = Callable[..., CadMeasurementQualityReport]

_QUALITY_REPORT_REPLAY: dict[str, tuple[str, QualityReportReplay]] = {
    QUALITY_ALGORITHM_VERSION: (
        QUALITY_ALGORITHM_SHA256,
        build_measurement_quality_report,
    ),
}


def replay_measurement_quality_report(
    report: CadMeasurementQualityReport,
    *,
    measurement: CadMeasurementRecord,
    dataset: CadFrequencyResponseDataset,
) -> CadMeasurementQualityReport:
    """Rerun the report's pinned algorithm/profile against bound evidence.

    Authoritative reads replay the canonical algorithm registered for the
    report's (algorithm_version, algorithm_sha256) identity so persisted
    capability decisions are never trusted on payload alone.
    """
    replay = _QUALITY_REPORT_REPLAY.get(report.algorithm_version)
    if replay is None:
        raise ValueError(
            'quality report algorithm version is not replayable: '
            f'{report.algorithm_version}'
        )
    algorithm_sha256, builder = replay
    if report.algorithm_sha256 != algorithm_sha256:
        raise ValueError('quality report algorithm hash mismatch')
    return builder(
        measurement=measurement,
        dataset=dataset,
        evidence=report.evidence,
        profile=report.profile,
        acquisition_context=report.acquisition_context,
        observation=report.observation,
        report_id=report.report_id,
        created_at_utc=report.created_at_utc,
    )


def gate_measurement_claim(
    report: CadMeasurementQualityReport,
    claim: MeasurementCapabilityClaim,
    *,
    required_band_hz: tuple[float, float] | None = None,
) -> CadMeasurementCapability:
    """Return the claim gate, optionally constrained to an explicit frequency band.

    A report may support magnitude inspection while the usable quality band is
    unknown. Consumers such as #173/#174 can pass their required band and fail
    closed without inventing coverage from the imported FR grid.
    """

    try:
        capability = report.capability(claim)
    except KeyError:
        return CadMeasurementCapability(
            claim=claim,
            decision='UNKNOWN',
            reasons=('claim was not evaluated by this historical quality report',),
        )
    if capability.decision != 'ALLOWED' or required_band_hz is None:
        return capability

    low, high = required_band_hz
    if not isfinite(low) or not isfinite(high) or low <= 0 or high <= low:
        raise ValueError('required downstream capability band is invalid')

    usable_check = report.usable_frequency_band
    usable_band = report.evidence.usable_frequency_band_hz
    if usable_check.status == 'FAIL':
        return CadMeasurementCapability(
            claim=claim,
            decision='BLOCKED',
            reasons=capability.reasons + (usable_check.reason,),
        )
    if usable_check.status != 'PASS' or usable_band is None:
        return CadMeasurementCapability(
            claim=claim,
            decision='UNKNOWN',
            reasons=capability.reasons + ('usable frequency band is not established',),
        )

    usable_low, usable_high = usable_band
    if usable_low > low or usable_high < high:
        return CadMeasurementCapability(
            claim=claim,
            decision='BLOCKED',
            reasons=capability.reasons + (
                f'usable frequency band {usable_low:g}-{usable_high:g} Hz '
                f'does not cover required band {low:g}-{high:g} Hz',
            ),
        )
    return capability
