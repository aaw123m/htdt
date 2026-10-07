"""Sealed evidence records for the HTDT-native sweep acquisition engine
(#869, REV66).

Three sealed record types persist a measurement run:

- :class:`CadSweepStimulusDefinition` — the rendered stimulus: full
  parameter set + generator version + params/samples hashes. The sweep is
  reproducible and hashed, so an evidence record pins exactly the waveform
  that was played.
- :class:`CadSweepAcquisitionStageEvent` — one row per engine transition
  with its explicit reason, so the state machine's path through
  PRECHECK..COMPLETED/FAILED/CANCELLED is itself retained evidence.
- :class:`CadSweepAcquisitionRun` — the whole run: engine/backend versions
  and identity, device/channel/routing, requested-vs-actual rate/format,
  timing method + latency evidence, raw-audio + derived-IR hashes and
  managed-asset paths, calibration/SPL binding, project/scene/campaign/
  role/position/orientation refs, timestamps, quality warnings and the
  outcome. Fields that cannot be known stay ``None``/``'unknown'`` —
  unknown stays UNKNOWN, never silently resolved.

Honesty rules baked in:

- ``backend_is_simulated`` is mandatory: a fake-backend run is retained as
  test evidence only and can never masquerade as a device capture.
- A run that did not reach a terminal stage cannot be sealed — the
  builder raises, because 'in-flight evidence' would read as a claim.
- ``capture_outcome`` is backend-reported, ``quality_verdict`` is
  gate-evaluated; a 'completed' capture with verdict 'invalid' is
  retained as invalid evidence, not hidden.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_json, canonical_sha256 as _hash
from .cad_delegated_provider import _seal, _require_iso8601

from .cad_sweep_acquisition import (
    AcquisitionQualityVerdict,
    AcquisitionResult,
    AcquisitionStage,
    CalibrationBindingState,
    CaptureOutcome,
    MeasurementAcquisitionEngine,
    TimingQuality,
    TimingResolutionMethod,
    _FAKE_BACKEND_ID,
    _WASAPI_BACKEND_ID,
)

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

#: Vocabulary mirrors of the engine-side Literals — pinned here so the
#: sealed record's schema states them explicitly.
AcquisitionTerminalStage = Literal['completed', 'failed', 'cancelled']


# ---------------------------------------------------------------------------
# CadSweepStimulusDefinition
# ---------------------------------------------------------------------------


class CadSweepStimulusDefinition(BaseModel):
    """Sealed identity of a rendered sweep stimulus block."""

    model_config = ConfigDict(frozen=True)

    stimulus_definition_id: str = Field(min_length=1)
    stimulus_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)

    generator_version: str = Field(min_length=1)
    start_frequency_hz: float
    end_frequency_hz: float
    duration_s: float
    level_dbfs: float
    sample_rate_hz: int
    pre_roll_s: float
    post_roll_s: float
    fade_in_s: float
    fade_out_s: float
    repetitions: int
    repetition_gap_s: float
    block_frames: int
    sweep_start_sample: int
    sweep_end_sample: int
    params_sha256: str = Field(pattern=_SHA256_PATTERN)
    samples_sha256: str = Field(pattern=_SHA256_PATTERN)
    stimulus_profile_ref: AuthorityRef | None = None
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _valid(self) -> 'CadSweepStimulusDefinition':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        if self.stimulus_sha256 != _hash(self.identity_payload()):
            raise ValueError('stimulus definition hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'generator_version': self.generator_version,
            'start_frequency_hz': self.start_frequency_hz,
            'end_frequency_hz': self.end_frequency_hz,
            'duration_s': self.duration_s,
            'level_dbfs': self.level_dbfs,
            'sample_rate_hz': self.sample_rate_hz,
            'pre_roll_s': self.pre_roll_s,
            'post_roll_s': self.post_roll_s,
            'fade_in_s': self.fade_in_s,
            'fade_out_s': self.fade_out_s,
            'repetitions': self.repetitions,
            'repetition_gap_s': self.repetition_gap_s,
            'block_frames': self.block_frames,
            'sweep_start_sample': self.sweep_start_sample,
            'sweep_end_sample': self.sweep_end_sample,
            'params_sha256': self.params_sha256,
            'samples_sha256': self.samples_sha256,
            'stimulus_profile_ref': (
                self.stimulus_profile_ref.model_dump(mode='json')
                if self.stimulus_profile_ref is not None else None
            ),
            'created_at_utc': self.created_at_utc,
        }


def build_stimulus_definition(
    *,
    document_id: str,
    stimulus: Any,  # GeneratedStimulus
    stimulus_profile_ref: AuthorityRef | None = None,
    created_at_utc: str,
) -> CadSweepStimulusDefinition:
    spec = stimulus.spec
    return _seal(
        CadSweepStimulusDefinition,
        {
            'document_id': document_id,
            'generator_version': stimulus.generator_version,
            'start_frequency_hz': spec.start_frequency_hz,
            'end_frequency_hz': spec.end_frequency_hz,
            'duration_s': spec.duration_s,
            'level_dbfs': spec.level_dbfs,
            'sample_rate_hz': spec.sample_rate_hz,
            'pre_roll_s': spec.pre_roll_s,
            'post_roll_s': spec.post_roll_s,
            'fade_in_s': spec.fade_in_s,
            'fade_out_s': spec.fade_out_s,
            'repetitions': spec.repetitions,
            'repetition_gap_s': spec.repetition_gap_s,
            'block_frames': stimulus.block_frames,
            'sweep_start_sample': stimulus.sweep_start_sample,
            'sweep_end_sample': stimulus.sweep_end_sample,
            'params_sha256': stimulus.params_sha256,
            'samples_sha256': stimulus.samples_sha256,
            'stimulus_profile_ref': (
                stimulus_profile_ref.model_dump(mode='python')
                if stimulus_profile_ref is not None else None
            ),
            'created_at_utc': created_at_utc,
        },
        'stimulus_definition_id', 'stimulus_sha256', 'swstim',
    )


# ---------------------------------------------------------------------------
# CadSweepAcquisitionStageEvent
# ---------------------------------------------------------------------------


class CadSweepAcquisitionStageEvent(BaseModel):
    """One recorded engine transition: stage, explicit reason, timestamp."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    event_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    run_seq: int = Field(ge=0)
    stage: AcquisitionStage
    reason: str
    entered_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _valid(self) -> 'CadSweepAcquisitionStageEvent':
        _require_iso8601(self.entered_at_utc, 'entered_at_utc')
        if self.event_sha256 != _hash(self.identity_payload()):
            raise ValueError('stage event hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'run_id': self.run_id,
            'run_seq': self.run_seq,
            'stage': self.stage,
            'reason': self.reason,
            'entered_at_utc': self.entered_at_utc,
        }


def build_stage_event(
    *,
    document_id: str,
    run_id: str,
    run_seq: int,
    stage: AcquisitionStage,
    reason: str,
    entered_at_utc: str,
) -> CadSweepAcquisitionStageEvent:
    return _seal(
        CadSweepAcquisitionStageEvent,
        {
            'document_id': document_id,
            'run_id': run_id,
            'run_seq': run_seq,
            'stage': stage,
            'reason': reason,
            'entered_at_utc': entered_at_utc,
        },
        'event_id', 'event_sha256', 'swstg',
    )


# ---------------------------------------------------------------------------
# CadSweepAcquisitionRun — the retained measurement-evidence record
# ---------------------------------------------------------------------------


class CadSweepAcquisitionRun(BaseModel):
    """Sealed record of one acquisition run through the engine."""

    model_config = ConfigDict(frozen=True)

    acquisition_id: str = Field(min_length=1)
    acquisition_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)

    # Engine / algorithm identity — a version change must never mutate
    # historical evidence, so each record pins the versions that made it.
    engine_version: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)
    derivation_version: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    backend_version: str = Field(min_length=1)
    backend_is_simulated: bool

    # What was played.
    stimulus_ref: AuthorityRef

    # Device/channel/routing — exactly what the operator armed.
    playback_device_id: str = Field(min_length=1)
    playback_channel: int = Field(ge=0)
    capture_device_id: str = Field(min_length=1)
    capture_channel: int = Field(ge=0)
    loopback_input_channel: int | None = Field(default=None, ge=0)
    captured_channels: tuple[int, ...] = ()

    # Requested vs actual — divergent actuals stay recorded as divergent.
    requested_sample_rate_hz: int
    requested_sample_format: str = Field(min_length=1)
    actual_sample_rate_hz: int | None = None
    actual_sample_format: str | None = None

    # Timing evidence.
    timing_method: TimingResolutionMethod | None = None
    timing_quality: TimingQuality | None = None
    onset_sample_index: int | None = None
    playback_to_capture_latency_s: float | None = None
    drift_ppm: float | None = None
    timing_reference_channel: int | None = None
    timing_reasons: tuple[str, ...] = ()

    # Raw + derived evidence artifacts (managed assets).
    raw_audio_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    raw_audio_asset_path: str | None = None
    recorded_frames: int | None = None
    expected_frames: int | None = None
    xrun_count: int | None = None
    clipped_samples: int | None = None
    capture_outcome: CaptureOutcome | None = None

    ir_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    # Content bytes hash = managed-asset address; the derivation identity
    # hash from the deriver is separate and pinned alongside.
    ir_derivation_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)
    ir_asset_path: str | None = None
    ir_origin_sample_index: int | None = None
    ir_window_seconds: float | None = None
    ir_status: str | None = None

    # Calibration / level authority.
    calibration_state: CalibrationBindingState
    calibration_ref: AuthorityRef | None = None
    requires_absolute_level: bool = False

    # Scene authority refs — optional but sealed when present.
    project_ref: AuthorityRef | None = None
    scene_ref: AuthorityRef | None = None
    campaign_ref: AuthorityRef | None = None
    role_refs: tuple[AuthorityRef, ...] = ()
    position_ref: AuthorityRef | None = None
    orientation_ref: AuthorityRef | None = None

    # Outcome + quality gate.
    stage: AcquisitionStage
    outcome: AcquisitionTerminalStage | None = None
    quality_verdict: AcquisitionQualityVerdict | None = None
    quality_reasons: tuple[str, ...] = ()
    quality_warnings: tuple[str, ...] = ()
    quality_measured_json: str = '{}'
    snr_db: float | None = None
    noise_floor_dbfs: float | None = None
    repetition_consistency: float | None = None

    armed_at_utc: str | None = None
    captured_at_utc: str | None = None
    completed_at_utc: str = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _valid(self) -> 'CadSweepAcquisitionRun':
        _require_iso8601(self.completed_at_utc, 'completed_at_utc')
        for label, value in (
            ('armed_at_utc', self.armed_at_utc),
            ('captured_at_utc', self.captured_at_utc),
        ):
            if value is not None:
                _require_iso8601(value, label)
        if self.stage in ('completed', 'failed', 'cancelled'):
            if self.outcome != self.stage:
                raise ValueError(
                    'terminal stage must equal outcome')
        if self.acquisition_sha256 != _hash(self.identity_payload()):
            raise ValueError('acquisition run hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'run_id': self.run_id,
            'engine_version': self.engine_version,
            'generator_version': self.generator_version,
            'derivation_version': self.derivation_version,
            'backend_id': self.backend_id,
            'backend_version': self.backend_version,
            'backend_is_simulated': self.backend_is_simulated,
            'stimulus_ref': self.stimulus_ref.model_dump(mode='json'),
            'playback_device_id': self.playback_device_id,
            'playback_channel': self.playback_channel,
            'capture_device_id': self.capture_device_id,
            'capture_channel': self.capture_channel,
            'loopback_input_channel': self.loopback_input_channel,
            'captured_channels': list(self.captured_channels),
            'requested_sample_rate_hz': self.requested_sample_rate_hz,
            'requested_sample_format': self.requested_sample_format,
            'actual_sample_rate_hz': self.actual_sample_rate_hz,
            'actual_sample_format': self.actual_sample_format,
            'timing_method': self.timing_method,
            'timing_quality': self.timing_quality,
            'onset_sample_index': self.onset_sample_index,
            'playback_to_capture_latency_s': self.playback_to_capture_latency_s,
            'drift_ppm': self.drift_ppm,
            'timing_reference_channel': self.timing_reference_channel,
            'timing_reasons': list(self.timing_reasons),
            'raw_audio_sha256': self.raw_audio_sha256,
            'raw_audio_asset_path': self.raw_audio_asset_path,
            'recorded_frames': self.recorded_frames,
            'expected_frames': self.expected_frames,
            'xrun_count': self.xrun_count,
            'clipped_samples': self.clipped_samples,
            'capture_outcome': self.capture_outcome,
            'ir_sha256': self.ir_sha256,
            'ir_derivation_sha256': self.ir_derivation_sha256,
            'ir_asset_path': self.ir_asset_path,
            'ir_origin_sample_index': self.ir_origin_sample_index,
            'ir_window_seconds': self.ir_window_seconds,
            'ir_status': self.ir_status,
            'calibration_state': self.calibration_state,
            'calibration_ref': (
                self.calibration_ref.model_dump(mode='json')
                if self.calibration_ref is not None else None
            ),
            'requires_absolute_level': self.requires_absolute_level,
            'project_ref': _ref_payload(self.project_ref),
            'scene_ref': _ref_payload(self.scene_ref),
            'campaign_ref': _ref_payload(self.campaign_ref),
            'role_refs': [
                r.model_dump(mode='json') for r in self.role_refs],
            'position_ref': _ref_payload(self.position_ref),
            'orientation_ref': _ref_payload(self.orientation_ref),
            'stage': self.stage,
            'outcome': self.outcome,
            'quality_verdict': self.quality_verdict,
            'quality_reasons': list(self.quality_reasons),
            'quality_warnings': list(self.quality_warnings),
            'quality_measured_json': self.quality_measured_json,
            'snr_db': self.snr_db,
            'noise_floor_dbfs': self.noise_floor_dbfs,
            'repetition_consistency': self.repetition_consistency,
            'armed_at_utc': self.armed_at_utc,
            'captured_at_utc': self.captured_at_utc,
            'completed_at_utc': self.completed_at_utc,
            'notes': list(self.notes),
        }


def _ref_payload(ref: AuthorityRef | None) -> dict[str, Any] | None:
    return ref.model_dump(mode='json') if ref is not None else None


def build_acquisition_run(
    *,
    document_id: str,
    engine: MeasurementAcquisitionEngine,
    result: AcquisitionResult,
    stimulus_ref: AuthorityRef,
    calibration_ref: AuthorityRef | None = None,
    project_ref: AuthorityRef | None = None,
    scene_ref: AuthorityRef | None = None,
    campaign_ref: AuthorityRef | None = None,
    role_refs: tuple[AuthorityRef, ...] = (),
    position_ref: AuthorityRef | None = None,
    orientation_ref: AuthorityRef | None = None,
    raw_audio_sha256: str | None = None,
    raw_audio_asset_path: str | None = None,
    ir_sha256: str | None = None,
    ir_derivation_sha256: str | None = None,
    ir_asset_path: str | None = None,
    notes: tuple[str, ...] = (),
) -> CadSweepAcquisitionRun:
    """Seal one finished run. Raises unless the engine reached a terminal
    stage — in-flight state is not evidence."""

    if engine.stage not in ('completed', 'failed', 'cancelled'):
        raise ValueError(
            f'cannot seal a run still in stage {engine.stage}')
    request = result.request
    stimulus = result.stimulus
    if request is None:
        raise ValueError('run has no request — configure() never ran')
    config = result.stream_config
    capture = result.capture
    timing = result.timing
    ir = result.impulse_response
    quality = result.quality

    at = {t.stage: t.at_utc for t in engine.transitions}
    backend_is_simulated = engine.backend.backend_id == _FAKE_BACKEND_ID
    run_notes = list(notes)
    if backend_is_simulated:
        run_notes.append(
            'fake backend — simulated capture is test evidence only')
    if timing is not None and timing.quality == 'unqualified':
        run_notes.append('timing unqualified: ' +
                         '; '.join(timing.reasons))

    return _seal(
        CadSweepAcquisitionRun,
        {
            'document_id': document_id,
            'run_id': engine.run_id,
            'engine_version': engine.engine_version,
            'generator_version': (
                stimulus.generator_version if stimulus is not None
                else engine.stimulus_generator.generator_version
            ),
            'derivation_version': engine.deriver.derivation_version,
            'backend_id': engine.backend.backend_id,
            'backend_version': engine.backend.backend_version,
            'backend_is_simulated': backend_is_simulated,
            'stimulus_ref': stimulus_ref.model_dump(mode='python'),
            'playback_device_id': request.routing.playback_device_id,
            'playback_channel': request.routing.playback_channel,
            'capture_device_id': request.routing.capture_device_id,
            'capture_channel': request.routing.capture_channel,
            'loopback_input_channel': request.routing.loopback_input_channel,
            'captured_channels': (
                list(capture.captured_channels) if capture is not None else []
            ),
            'requested_sample_rate_hz': (
                config.sample_rate_hz if config is not None
                else request.stimulus.sample_rate_hz
            ),
            'requested_sample_format': (
                config.sample_format if config is not None else 'float64'
            ),
            'actual_sample_rate_hz': (
                capture.actual_sample_rate_hz if capture is not None else None
            ),
            'actual_sample_format': (
                capture.actual_sample_format if capture is not None else None
            ),
            'timing_method': timing.method if timing is not None else None,
            'timing_quality': timing.quality if timing is not None else None,
            'onset_sample_index': (
                timing.onset_sample_index if timing is not None else None),
            'playback_to_capture_latency_s': (
                timing.playback_to_capture_latency_s
                if timing is not None else None),
            'drift_ppm': timing.drift_ppm if timing is not None else None,
            'timing_reference_channel': (
                timing.reference_channel if timing is not None else None),
            'timing_reasons': list(timing.reasons) if timing is not None else [],
            'raw_audio_sha256': raw_audio_sha256,
            'raw_audio_asset_path': raw_audio_asset_path,
            'recorded_frames': (
                capture.recorded_frames if capture is not None else None),
            'expected_frames': (
                capture.expected_frames if capture is not None else None),
            'xrun_count': capture.xrun_count if capture is not None else None,
            'clipped_samples': (
                capture.clipped_samples if capture is not None else None),
            'capture_outcome': (
                capture.outcome if capture is not None else None),
            'ir_sha256': ir_sha256,
            'ir_derivation_sha256': ir_derivation_sha256,
            'ir_asset_path': ir_asset_path,
            'ir_origin_sample_index': (
                ir.origin_sample_index if ir is not None else None),
            'ir_window_seconds': (
                ir.window_seconds if ir is not None else None),
            'ir_status': ir.status if ir is not None else None,
            'calibration_state': request.calibration_state,
            'calibration_ref': (
                calibration_ref.model_dump(mode='python')
                if calibration_ref is not None else None),
            'requires_absolute_level': request.requires_absolute_level,
            'project_ref': (
                project_ref.model_dump(mode='python')
                if project_ref is not None else None),
            'scene_ref': (
                scene_ref.model_dump(mode='python')
                if scene_ref is not None else None),
            'campaign_ref': (
                campaign_ref.model_dump(mode='python')
                if campaign_ref is not None else None),
            'role_refs': [r.model_dump(mode='python') for r in role_refs],
            'position_ref': (
                position_ref.model_dump(mode='python')
                if position_ref is not None else None),
            'orientation_ref': (
                orientation_ref.model_dump(mode='python')
                if orientation_ref is not None else None),
            'stage': engine.stage,
            'outcome': engine.stage,
            'quality_verdict': (
                quality.verdict if quality is not None else 'unevaluated'),
            'quality_reasons': (
                list(quality.reasons) if quality is not None else []),
            'quality_warnings': (
                list(quality.warnings) if quality is not None else []),
            'quality_measured_json': canonical_json(
                dict(quality.measured) if quality is not None else {}),
            'snr_db': (
                quality.measured.get('snr_db') if quality is not None else None),
            'noise_floor_dbfs': (
                quality.measured.get('noise_floor_dbfs')
                if quality is not None else None),
            'repetition_consistency': (
                quality.measured.get('repetition_consistency')
                if quality is not None else None),
            'armed_at_utc': at.get('armed'),
            'captured_at_utc': (
                at.get('processing') or at.get('playing_recording')),
            'completed_at_utc': at.get(engine.stage, ''),
            'notes': run_notes,
        },
        'acquisition_id', 'acquisition_sha256', 'swrun',
    )


# ---------------------------------------------------------------------------
# JA vocabulary labels — operator-facing text lives in one registry so the
# UI never invents its own wording for the state machine.
# ---------------------------------------------------------------------------

SWEEP_ACQUISITION_STAGE_LABELS: dict[str, str] = {
    'precheck': '事前チェック',
    'ready': '準備完了',
    'armed': '出力アーム済み',
    'playing_recording': '再生・録音中',
    'processing': '処理中',
    'quality_check': '品質判定中',
    'completed': '完了',
    'failed': '失敗',
    'cancelled': '中止',
}

SWEEP_ACQUISITION_OUTCOME_LABELS: dict[str, str] = {
    'completed': '取得完了',
    'failed': '取得失敗',
    'cancelled': '中止',
}

SWEEP_ACQUISITION_QUALITY_LABELS: dict[str, str] = {
    'valid': '有効',
    'limited': '限定有効',
    'invalid': '無効',
    'unevaluated': '未評価',
}

SWEEP_ACQUISITION_REASON_LABELS: dict[str, str] = {
    'clipping_detected': 'クリッピング検出',
    'insufficient_snr': 'SNR不足',
    'capture_truncated': '録音が途中で途切れました',
    'xrun_detected': 'バッファアンダーラン/オーバーラン検出',
    'device_lost': 'デバイスが途中で切断されました',
    'calibration_missing': '校正が未設定です',
    'calibration_invalid': '校正が無効です',
    'synchronization_unusable': '同期証跡が不十分です',
    'timing_limited': 'タイミング証跡が限定的です',
    'excessive_noise': 'ノイズフロアが高すぎます',
    'inconsistent_repetitions': 'リピート間で応答が一致しません',
    'cancelled': 'オペレータによる中止',
    'backend_unavailable': 'オーディオバックエンドが利用できません',
    'open_failed': 'ストリームを開けませんでした',
}

SWEEP_TIMING_QUALITY_LABELS: dict[str, str] = {
    'qualified': '有資格',
    'limited': '限定的',
    'unqualified': '不適格',
}

SWEEP_TIMING_METHOD_LABELS: dict[str, str] = {
    'loopback_cross_correlation': 'ループバック相互相関',
    'stimulus_onset_detection': '刺激立ち上がり検出',
    'unsynchronized_declared': '同期宣言あり・証跡なし',
    'unqualified': '証跡なし',
}

SWEEP_CALIBRATION_STATE_LABELS: dict[str, str] = {
    'bound': '校正適用済み',
    'missing': '校正なし',
    'invalid': '校正無効',
    'unknown': '校正状態不明',
}


__all__ = [
    'AcquisitionTerminalStage', 'CadSweepAcquisitionRun',
    'CadSweepAcquisitionStageEvent', 'CadSweepStimulusDefinition',
    'SWEEP_ACQUISITION_OUTCOME_LABELS', 'SWEEP_ACQUISITION_QUALITY_LABELS',
    'SWEEP_ACQUISITION_REASON_LABELS', 'SWEEP_ACQUISITION_STAGE_LABELS',
    'SWEEP_CALIBRATION_STATE_LABELS', 'SWEEP_TIMING_METHOD_LABELS',
    'SWEEP_TIMING_QUALITY_LABELS',
    'build_acquisition_run', 'build_stage_event',
    'build_stimulus_definition',
]
