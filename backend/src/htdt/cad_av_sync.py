"""End-to-end audio/video synchronization authority (#560).

Speaker/channel alignment delay (CalibrationPlan, excitation delay) answers
"when should physical speakers radiate relative to each other". A/V sync
answers a different question: "when does audible program output arrive
relative to the corresponding displayed event". End-to-end latency can
change with display processing mode, frame rate, audio path or a user
lip-sync offset without changing any speaker geometry, so it is persisted
as its own immutable authority and is never derived from per-speaker
delay/distance records.

Canonical sign convention — ``audio_minus_video_ms``:
    positive: the audible program event arrives AFTER the corresponding
        displayed event (video leads audio);
    negative: audio arrives BEFORE the corresponding displayed event;
    None:      the end-to-end offset is unknown and stays unknown (missing
        device/display latency evidence is never filled with a guess).
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


AVSyncMeasurementMethod = Literal['manual_external_sync_test', 'auto', 'unknown']
AVSyncLifecycleStatus = Literal[
    'measured',
    'correction_requested',
    'setting_applied',
    'residual_verified',
]
AVSyncAppliedEvidence = Literal[
    'user_confirmed',
    'device_read_back',
    'imported',
    'unknown',
]
"""How a ``setting_applied`` claim was established. ``unknown`` is the only
honest default — a recorded ``applied_setting_ms`` number alone never
implies device read-back."""
# Canonical sign convention (module docstring): positive means audio arrives
# after the corresponding displayed event.
AV_SYNC_SIGN_CONVENTION = 'positive_means_audio_after_video'
AV_SYNC_CONDITION_SCHEMA_VERSION = 'av-sync-condition-1'
AV_SYNC_MEASUREMENT_SCHEMA_VERSION = 'av-latency-measurement-1'


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


def _require_finite(value: float | None, label: str) -> None:
    if value is not None and not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


class AVSyncCondition(BaseModel):
    """Immutable description of one exact audio/video operating condition.

    A lip-sync measurement is only meaningful against the precise playback
    chain that produced it, so every field that can change the end-to-end
    latency is part of the condition identity: source/input path, display
    or projector identity, video mode/refresh/processing state, audio
    processing/playback state, the audio return path and the user-configured
    device lip-sync offset. Two records differing in any field are distinct
    conditions — distinct display modes therefore carry separate latency
    measurements by construction.
    """

    model_config = ConfigDict(frozen=True)

    condition_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str | None = Field(default=None, min_length=1)
    scene_revision_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    # Playback chain as declared by the operator — free-form identifiers are
    # kept as recorded; unknown links stay explicit empty/unknown values.
    source_device_id: str = 'unknown'
    source_input_path: str = 'unknown'
    display_device_id: str = 'unknown'
    video_mode: str = 'unknown'
    refresh_rate_hz: float | None = None
    frame_rate_hz: float | None = None
    video_processing_mode: str = 'unknown'
    audio_processing_mode: str = 'unknown'
    audio_path: str = 'unknown'
    lip_sync_offset_ms: float = 0.0
    # Reserved binding for the room operating-state authority (#556). Optional
    # so conditions recorded before that authority exists stay valid.
    operating_state_id: str | None = Field(default=None, min_length=1)
    operating_state_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    created_at: str = Field(min_length=1)
    provenance_json: str = '{}'
    condition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_condition(self) -> 'AVSyncCondition':
        _require_finite(self.refresh_rate_hz, 'refresh_rate_hz')
        _require_finite(self.frame_rate_hz, 'frame_rate_hz')
        _require_finite(self.lip_sync_offset_ms, 'lip_sync_offset_ms')
        if self.refresh_rate_hz is not None and self.refresh_rate_hz <= 0:
            raise ValueError('refresh_rate_hz must be positive')
        if self.frame_rate_hz is not None and self.frame_rate_hz <= 0:
            raise ValueError('frame_rate_hz must be positive')
        for id_field, hash_field in (
            ('scene_revision_id', 'scene_revision_sha256'),
            ('system_variant_id', 'system_variant_sha256'),
            ('operating_state_id', 'operating_state_sha256'),
        ):
            if (getattr(self, id_field) is None) != (
                getattr(self, hash_field) is None
            ):
                raise ValueError(
                    f'{id_field}/{hash_field} must be supplied together; '
                    'a bare id is not an exact reference'
                )
        if self.condition_sha256 != _hash(self.identity_payload()):
            raise ValueError('A/V sync condition hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': AV_SYNC_CONDITION_SCHEMA_VERSION,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_revision_sha256': self.scene_revision_sha256,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'source_device_id': self.source_device_id,
            'source_input_path': self.source_input_path,
            'display_device_id': self.display_device_id,
            'video_mode': self.video_mode,
            'refresh_rate_hz': self.refresh_rate_hz,
            'frame_rate_hz': self.frame_rate_hz,
            'video_processing_mode': self.video_processing_mode,
            'audio_processing_mode': self.audio_processing_mode,
            'audio_path': self.audio_path,
            'lip_sync_offset_ms': self.lip_sync_offset_ms,
            'operating_state_id': self.operating_state_id,
            'operating_state_sha256': self.operating_state_sha256,
        }


class AVLatencyMeasurement(BaseModel):
    """One immutable A/V latency observation bound to an exact condition.

    The four lifecycle values are kept strictly separate so no stage is
    mistaken for the previous one:

    - ``measured_offset_ms``: observed end-to-end offset under the canonical
      sign convention (``None`` = unknown, never inferred);
    - ``requested_correction_ms``: correction the operator asked the device
      to apply — a request, not proof of application;
    - ``applied_setting_ms``: device lip-sync setting declared as actually
      applied after the request;
    - ``residual_offset_ms``: end-to-end offset verified AFTER the setting
      was applied; ``None`` keeps the residual explicitly unknown.

    ``status`` is monotonic: measured → correction_requested →
    setting_applied → residual_verified. A value for a later stage may only
    be present when the matching status has been reached.
    """

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    condition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    #: Stable identity of the whole lifecycle chain; every stage appended by
    #: :func:`advance_av_latency_measurement` shares it while receiving a new
    #: immutable ``measurement_id``. ``None`` only for records persisted
    #: before chain lineage existed — their chain identity is the
    #: ``measurement_id`` itself.
    chain_id: str | None = Field(default=None, min_length=1)
    predecessor_measurement_id: str | None = Field(default=None, min_length=1)
    predecessor_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    applied_evidence: AVSyncAppliedEvidence = 'unknown'
    method: AVSyncMeasurementMethod = 'unknown'
    sign_convention: str = AV_SYNC_SIGN_CONVENTION
    status: AVSyncLifecycleStatus = 'measured'
    measured_offset_ms: float | None = None
    uncertainty_ms: float | None = None
    requested_correction_ms: float | None = None
    applied_setting_ms: float | None = None
    residual_offset_ms: float | None = None
    captured_at: str = Field(min_length=1)
    source_kind: Literal['user_measured', 'imported', 'device_reported', 'unknown'] = 'unknown'
    external_source_id: str | None = Field(default=None, min_length=1)
    notes: tuple[str, ...] = ()
    provenance_json: str = '{}'
    measurement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_measurement(self) -> 'AVLatencyMeasurement':
        for label, value in (
            ('measured_offset_ms', self.measured_offset_ms),
            ('uncertainty_ms', self.uncertainty_ms),
            ('requested_correction_ms', self.requested_correction_ms),
            ('applied_setting_ms', self.applied_setting_ms),
            ('residual_offset_ms', self.residual_offset_ms),
        ):
            _require_finite(value, label)
        if self.uncertainty_ms is not None and self.uncertainty_ms < 0:
            raise ValueError('uncertainty_ms must be non-negative')
        stage = {
            'measured': 0,
            'correction_requested': 1,
            'setting_applied': 2,
            'residual_verified': 3,
        }[self.status]
        if self.requested_correction_ms is not None and stage < 1:
            raise ValueError('requested_correction_ms requires correction_requested status')
        if self.applied_setting_ms is not None and stage < 2:
            raise ValueError('applied_setting_ms requires setting_applied status')
        if self.residual_offset_ms is not None and stage < 3:
            raise ValueError('residual_offset_ms requires residual_verified status')
        if (self.predecessor_measurement_id is None) != (
            self.predecessor_sha256 is None
        ):
            raise ValueError(
                'predecessor measurement id/hash must be supplied together'
            )
        if self.predecessor_measurement_id is not None:
            if self.predecessor_measurement_id == self.measurement_id:
                raise ValueError('a stage cannot be its own predecessor')
            if self.chain_id is None:
                raise ValueError('a successor stage requires a chain_id')
            if stage == 0:
                raise ValueError(
                    'the chain head is always the measured stage'
                )
        if self.applied_evidence != 'unknown' and self.applied_setting_ms is None:
            raise ValueError(
                'applied_evidence requires an applied_setting_ms value'
            )
        if self.measurement_sha256 != _hash(self.identity_payload()):
            raise ValueError('A/V latency measurement hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': AV_SYNC_MEASUREMENT_SCHEMA_VERSION,
            'document_id': self.document_id,
            'condition_id': self.condition_id,
            'condition_sha256': self.condition_sha256,
            'chain_id': self.chain_id,
            'predecessor_measurement_id': self.predecessor_measurement_id,
            'predecessor_sha256': self.predecessor_sha256,
            'method': self.method,
            'sign_convention': self.sign_convention,
            'status': self.status,
            'measured_offset_ms': self.measured_offset_ms,
            'uncertainty_ms': self.uncertainty_ms,
            'requested_correction_ms': self.requested_correction_ms,
            'applied_setting_ms': self.applied_setting_ms,
            'applied_evidence': self.applied_evidence,
            'residual_offset_ms': self.residual_offset_ms,
            'captured_at': self.captured_at,
            'source_kind': self.source_kind,
            'external_source_id': self.external_source_id,
            'notes': list(self.notes),
        }


def build_av_sync_condition(
    *,
    document_id: str,
    scene_revision_id: str | None = None,
    scene_revision_sha256: str | None = None,
    system_variant_id: str | None = None,
    system_variant_sha256: str | None = None,
    source_device_id: str = 'unknown',
    source_input_path: str = 'unknown',
    display_device_id: str = 'unknown',
    video_mode: str = 'unknown',
    refresh_rate_hz: float | None = None,
    frame_rate_hz: float | None = None,
    video_processing_mode: str = 'unknown',
    audio_processing_mode: str = 'unknown',
    audio_path: str = 'unknown',
    lip_sync_offset_ms: float = 0.0,
    operating_state_id: str | None = None,
    operating_state_sha256: str | None = None,
    created_at: str,
    provenance_json: str = '{}',
) -> AVSyncCondition:
    payload: dict[str, Any] = {
        'condition_id': str(uuid4()),
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'scene_revision_sha256': scene_revision_sha256,
        'system_variant_id': system_variant_id,
        'system_variant_sha256': system_variant_sha256,
        'source_device_id': source_device_id,
        'source_input_path': source_input_path,
        'display_device_id': display_device_id,
        'video_mode': video_mode,
        'refresh_rate_hz': refresh_rate_hz,
        'frame_rate_hz': frame_rate_hz,
        'video_processing_mode': video_processing_mode,
        'audio_processing_mode': audio_processing_mode,
        'audio_path': audio_path,
        'lip_sync_offset_ms': lip_sync_offset_ms,
        'operating_state_id': operating_state_id,
        'operating_state_sha256': operating_state_sha256,
        'created_at': created_at,
        'provenance_json': provenance_json,
    }
    provisional = AVSyncCondition.model_construct(
        **payload,
        condition_sha256='0' * 64,
    )
    return AVSyncCondition(
        **payload,
        condition_sha256=_hash(provisional.identity_payload()),
    )


def build_av_latency_measurement(
    condition: AVSyncCondition,
    *,
    method: AVSyncMeasurementMethod = 'unknown',
    status: AVSyncLifecycleStatus = 'measured',
    measured_offset_ms: float | None = None,
    uncertainty_ms: float | None = None,
    requested_correction_ms: float | None = None,
    applied_setting_ms: float | None = None,
    residual_offset_ms: float | None = None,
    captured_at: str,
    source_kind: Literal['user_measured', 'imported', 'device_reported', 'unknown'] = 'unknown',
    external_source_id: str | None = None,
    chain_id: str | None = None,
    applied_evidence: AVSyncAppliedEvidence = 'unknown',
    notes: tuple[str, ...] = (),
    provenance_json: str = '{}',
) -> AVLatencyMeasurement:
    chain = chain_id or str(uuid4())
    payload: dict[str, Any] = {
        'measurement_id': str(uuid4()),
        'document_id': condition.document_id,
        'condition_id': condition.condition_id,
        'condition_sha256': condition.condition_sha256,
        'chain_id': chain,
        'predecessor_measurement_id': None,
        'predecessor_sha256': None,
        'applied_evidence': applied_evidence,
        'method': method,
        'status': status,
        'measured_offset_ms': measured_offset_ms,
        'uncertainty_ms': uncertainty_ms,
        'requested_correction_ms': requested_correction_ms,
        'applied_setting_ms': applied_setting_ms,
        'residual_offset_ms': residual_offset_ms,
        'captured_at': captured_at,
        'source_kind': source_kind,
        'external_source_id': external_source_id,
        'notes': list(notes),
        'provenance_json': provenance_json,
    }
    provisional = AVLatencyMeasurement.model_construct(
        **payload,
        measurement_sha256='0' * 64,
    )
    return AVLatencyMeasurement(
        **payload,
        measurement_sha256=_hash(provisional.identity_payload()),
    )


def advance_av_latency_measurement(
    measurement: AVLatencyMeasurement,
    *,
    status: AVSyncLifecycleStatus,
    requested_correction_ms: float | None = None,
    applied_setting_ms: float | None = None,
    applied_evidence: AVSyncAppliedEvidence | None = None,
    residual_offset_ms: float | None = None,
    captured_at: str,
    notes: tuple[str, ...] = (),
) -> AVLatencyMeasurement:
    """Append a new lifecycle stage for an existing measurement.

    Transitions are monotonic (measured → correction_requested →
    setting_applied → residual_verified); each step produces a new immutable
    record so earlier states are never overwritten. ``residual_offset_ms``
    may stay ``None`` even at ``residual_verified`` — a performed
    verification with unknowable residual stays honestly unknown.
    """
    order = {
        'measured': 0,
        'correction_requested': 1,
        'setting_applied': 2,
        'residual_verified': 3,
    }
    if order[status] <= order[measurement.status]:
        raise ValueError('A/V sync lifecycle transitions are monotonic')
    payload = measurement.model_dump(mode='python', exclude={'measurement_sha256'})
    # A successor is a new immutable stage in the same chain, never a rewrite
    # of the existing record: fresh stage id + explicit predecessor lineage.
    payload['measurement_id'] = str(uuid4())
    payload['chain_id'] = measurement.chain_id or measurement.measurement_id
    payload['predecessor_measurement_id'] = measurement.measurement_id
    payload['predecessor_sha256'] = measurement.measurement_sha256
    payload['status'] = status
    if applied_evidence is not None:
        payload['applied_evidence'] = applied_evidence
    if requested_correction_ms is not None:
        payload['requested_correction_ms'] = requested_correction_ms
    if applied_setting_ms is not None:
        payload['applied_setting_ms'] = applied_setting_ms
    if residual_offset_ms is not None:
        payload['residual_offset_ms'] = residual_offset_ms
    payload['captured_at'] = captured_at
    payload['notes'] = list(measurement.notes) + list(notes)
    provisional = AVLatencyMeasurement.model_construct(
        **payload,
        measurement_sha256='0' * 64,
    )
    return AVLatencyMeasurement(
        **payload,
        measurement_sha256=_hash(provisional.identity_payload()),
    )
