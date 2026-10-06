"""Temporal display / motion-artifact qualification authority (#647).

Refresh rate, HDMI mode and static colour/HDR calibration do **not**
establish temporal image quality.  Response time, overshoot, flicker,
moving-edge blur, ghosting and image retention are measured states under
an exact temporal display state — never marketing labels like ``120 Hz``
or ``1 ms``.

Three measurement kinds are related but never interchangeable
(issue §4):

- ``PIXEL_TRANSITION_RESPONSE`` — optical step response;
- ``DISPLAY_PERSISTENCE`` / sample-and-hold;
- ``PERCEIVED/MEASURED_MOVING_EDGE_BLUR``.

Contract properties:

- qualification binds the exact temporal state — input frame rate, VRR,
  motion interpolation, BFI/strobing, overdrive, low-latency mode; a
  different refresh or motion mode is a different qualification
  identity;
- a step response stores the waveform reference and the declared
  rise/fall/settling definitions — never one vendor-style
  ``response_time_ms`` scalar;
- overshoot/undershoot are measured independently of the crossing time —
  a fast crossing with severe inverse ghosting is not automatically
  better;
- flicker observations carry method (waveform / JEITA / FMA / Pst-LM),
  brightness and refresh state;
- image retention distinguishes transient vs persistent under the
  declared method;
- ``instrumented`` vs ``subjective`` observations are kept apart;
- IDMS measurement procedures are mapped as methods — IDMS does not set
  compliance values, so this authority never imports vendor pass/fail
  marketing claims.

Literature basis (issue §research): SID/ICDM IDMS v1.3 (2025-05-31) —
chapter 10 temporal measurements (response time, video latency, residual
image, flicker, flicker visibility, spatial jitter, image retention) and
chapter 12 motion-artifact measurements; the standard supplies
procedures, not universal pass/fail values.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


TEMPORAL_SCHEMA_VERSION = 'temporal-display-1'
TEMPORAL_EVALUATION_VERSION = 'temporal-display-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

TriState = Literal['off', 'on', 'auto', 'unknown']

MotionProcessingState = Literal['off', 'low', 'medium', 'high', 'unknown']

StrobeState = Literal['off', 'on', 'adaptive', 'unknown']

VrrState = Literal['off', 'on', 'unknown']

TemporalClaimKind = Literal[
    'response_time',
    'overshoot_free',
    'moving_edge_blur',
    'ghosting_free',
    'flicker_free',
    'retention_free',
    'temporal_latency',
]

BlurMechanism = Literal[
    'pixel_transition_response',
    'display_persistence_sample_hold',
    'moving_edge_blur_measured',
    'strobe_bfi_persistence',
    'unknown',
]
"""Blur mechanisms are related but never interchangeable (issue §4)."""

FlickerMethod = Literal[
    'idms_waveform',
    'jeita',
    'fma',
    'pst_lm',
    'provider_documented',
    'subjective_observation',
    'unknown',
]

ObservationClass = Literal['instrumented', 'provider_documented', 'subjective']

RetentionPersistence = Literal[
    'transient_recovered',
    'persistent',
    'not_observed',
    'unknown',
]

TemporalVerdict = Literal[
    'supported',
    'supported_with_limitations',
    'insufficient_evidence',
    'unsupported',
]

TemporalReason = Literal[
    'TEMPORAL_QUALIFIED',
    'TEMPORAL_LIMITED',
    'INSUFFICIENT_MEASUREMENTS',
    'DISPLAY_STATE_INCOMPLETE',
    'METHOD_UNDECLARED',
    'WAVEFORM_MISSING',
    'SUBJECTIVE_ONLY',
    'CLAIM_BEYOND_COVERAGE',
    'STATE_MISMATCH',
]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class TemporalDisplayState(BaseModel):
    """The exact temporal/motion state under test (issue §1).

    Composes with the #625 display-state pin — the same display in a
    different refresh or motion mode is a different qualification
    identity.
    """

    model_config = ConfigDict(frozen=True)

    state_id: str = Field(pattern=r'^tdstate-[0-9a-f]{24}$')
    state_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    display_state_ref: AuthorityRef
    input_frame_rate_hz: float = Field(gt=0)
    refresh_rate_hz: float = Field(gt=0)
    vrr: VrrState = 'unknown'
    motion_interpolation: MotionProcessingState = 'unknown'
    black_frame_insertion: StrobeState = 'unknown'
    backlight_strobing: StrobeState = 'unknown'
    overdrive_mode: str = 'unknown'
    low_latency_mode: TriState = 'unknown'
    notes: str = ''

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'state_id', 'state_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'TemporalDisplayState':
        _require_refs(self.display_state_ref)
        if (
            not isfinite(self.input_frame_rate_hz)
            or not isfinite(self.refresh_rate_hz)
        ):
            raise ValueError('frame/refresh rates must be finite')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'TemporalDisplayState':
        return _seal(cls, kwargs, 'state_id', 'state_sha256', 'tdstate')


class StepResponseDefinition(BaseModel):
    """Declared rise/fall/settling definitions for a step response —
    threshold percentages and window are part of the result's identity.
    """

    model_config = ConfigDict(frozen=True)

    rise_threshold_low_percent: float = Field(ge=0.0, lt=100.0)
    rise_threshold_high_percent: float = Field(ge=0.0, lt=100.0)
    settle_tolerance_percent: float = Field(gt=0.0, lt=100.0)
    definition_note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'StepResponseDefinition':
        if self.rise_threshold_low_percent >= self.rise_threshold_high_percent:
            raise ValueError('rise thresholds must be ordered')
        return self


class TemporalStepResponseMeasurement(BaseModel):
    """One measured optical step response (issue §2–§3)."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(pattern=r'^tstep-[0-9a-f]{24}$')
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    state_ref: AuthorityRef
    stimulus_ref: AuthorityRef
    start_level_percent: float = Field(ge=0.0, le=100.0)
    end_level_percent: float = Field(ge=0.0, le=100.0)
    transition_label: str = ''
    definitions: StepResponseDefinition
    waveform_sha256: str = Field(pattern=_SHA256_PATTERN)
    rise_ms: float | None = Field(default=None, ge=0.0)
    fall_ms: float | None = Field(default=None, ge=0.0)
    settle_ms: float | None = Field(default=None, ge=0.0)
    overshoot_percent: float | None = Field(default=None, ge=0.0)
    undershoot_percent: float | None = Field(default=None, ge=0.0)
    sensor_bandwidth_hz: float | None = Field(default=None, gt=0)
    sample_rate_hz: float | None = Field(default=None, gt=0)
    algorithm_version: str = ''
    measured_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'measurement_id', 'measurement_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'TemporalStepResponseMeasurement':
        _require_refs(self.state_ref, self.stimulus_ref)
        _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        if self.start_level_percent == self.end_level_percent:
            raise ValueError('a step response needs a level change')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'TemporalStepResponseMeasurement':
        return _seal(
            cls, kwargs, 'measurement_id', 'measurement_sha256', 'tstep'
        )


class MotionArtifactMeasurement(BaseModel):
    """Moving-pattern measurement — blur/ghosting evidence (§4–§5)."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(pattern=r'^mart-[0-9a-f]{24}$')
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    state_ref: AuthorityRef
    stimulus_ref: AuthorityRef
    mechanism: BlurMechanism
    speed_pixels_per_frame: float = Field(gt=0)
    method: str = Field(min_length=1)
    """Pursuit-camera / sensor / algorithm identity — never marketing."""
    moving_edge_blur_metric: str = ''
    blur_value: float | None = Field(default=None, ge=0.0)
    artifact_amplitude_percent: float | None = Field(
        default=None, ge=0.0
    )
    artifact_extent_px: float | None = Field(default=None, ge=0.0)
    color_channel_detail: str = ''
    observation_class: ObservationClass
    measured_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'measurement_id', 'measurement_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'MotionArtifactMeasurement':
        _require_refs(self.state_ref, self.stimulus_ref)
        _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        if self.mechanism == 'unknown':
            raise ValueError(
                'blur mechanism must be declared — unknown is not a '
                'measurement'
            )
        if (
            self.observation_class == 'instrumented'
            and self.blur_value is None
            and self.artifact_amplitude_percent is None
        ):
            raise ValueError(
                'an instrumented motion observation needs a numeric '
                'result'
            )
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'MotionArtifactMeasurement':
        return _seal(
            cls, kwargs, 'measurement_id', 'measurement_sha256', 'mart'
        )


class FlickerMeasurement(BaseModel):
    """Temporal modulation observation (issue §6)."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(pattern=r'^tflick-[0-9a-f]{24}$')
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    state_ref: AuthorityRef
    method: FlickerMethod
    modulation_depth_percent: float | None = Field(
        default=None, ge=0.0, le=100.0
    )
    dominant_frequency_hz: float | None = Field(default=None, gt=0)
    brightness_state: str = ''
    observation_class: ObservationClass
    measured_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'measurement_id', 'measurement_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'FlickerMeasurement':
        _require_refs(self.state_ref)
        _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        if self.method == 'unknown':
            raise ValueError('flicker method must be declared')
        if self.observation_class == 'instrumented' and (
            self.modulation_depth_percent is None
            or self.dominant_frequency_hz is None
        ):
            raise ValueError(
                'an instrumented flicker measurement needs depth and '
                'frequency'
            )
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'FlickerMeasurement':
        return _seal(
            cls, kwargs, 'measurement_id', 'measurement_sha256', 'tflick'
        )


class ImageRetentionObservation(BaseModel):
    """Residual-image observation under a declared method (issue §7)."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(pattern=r'^tret-[0-9a-f]{24}$')
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    state_ref: AuthorityRef
    stimulus_ref: AuthorityRef
    method: str = Field(min_length=1)
    """IDMS 10.8/10.9-style method identity or project method."""
    persistence: RetentionPersistence
    observation_class: ObservationClass
    measured_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'observation_id', 'observation_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'ImageRetentionObservation':
        _require_refs(self.state_ref, self.stimulus_ref)
        _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'ImageRetentionObservation':
        return _seal(
            cls, kwargs, 'observation_id', 'observation_sha256', 'tret'
        )


class TemporalClaimVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: TemporalClaimKind
    verdict: TemporalVerdict
    reason: TemporalReason
    evidence_refs: tuple[AuthorityRef, ...] = ()


class TemporalDisplayQualification(BaseModel):
    """Sealed temporal qualification for one display state (issue goal)."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(pattern=r'^tdq-[0-9a-f]{24}$')
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    state_ref: AuthorityRef
    claim_verdicts: tuple[TemporalClaimVerdict, ...]
    evaluated_at_utc: str
    evaluation_version: str = TEMPORAL_EVALUATION_VERSION

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'TemporalDisplayQualification':
        _require_refs(self.state_ref)
        if not self.claim_verdicts:
            raise ValueError('a qualification needs at least one claim')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'TemporalDisplayQualification':
        return _seal(
            cls, kwargs, 'qualification_id', 'qualification_sha256', 'tdq'
        )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_temporal_qualification(
    state: TemporalDisplayState,
    *,
    step_responses: Sequence[TemporalStepResponseMeasurement],
    motion: Sequence[MotionArtifactMeasurement],
    flicker: Sequence[FlickerMeasurement],
    retention: Sequence[ImageRetentionObservation],
    claims: Sequence[TemporalClaimKind],
    response_time_threshold_ms: float,
    flicker_depth_threshold_percent: float,
    evaluated_at_utc: str | None = None,
) -> TemporalDisplayQualification:
    """Fail-closed per-claim verdicts — refresh labels never qualify.

    Only measurements sha-bound to *this* state count; every claim
    without matching evidence is ``insufficient_evidence``.
    """
    state_ref = AuthorityRef(
        kind='temporal_display_state',
        ref_id=state.state_id,
        ref_sha256=state.state_sha256,
    )
    in_state_steps = [
        m for m in step_responses
        if m.state_ref.ref_sha256 == state.state_sha256
    ]
    in_state_motion = [
        m for m in motion
        if m.state_ref.ref_sha256 == state.state_sha256
    ]
    in_state_flicker = [
        m for m in flicker
        if m.state_ref.ref_sha256 == state.state_sha256
    ]
    in_state_retention = [
        m for m in retention
        if m.state_ref.ref_sha256 == state.state_sha256
    ]
    verdicts: list[TemporalClaimVerdict] = []
    for claim in claims:
        if claim == 'response_time':
            if not in_state_steps:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='insufficient_evidence',
                    reason='INSUFFICIENT_MEASUREMENTS',
                ))
                continue
            refs = tuple(
                AuthorityRef(
                    kind='temporal_step_response',
                    ref_id=m.measurement_id,
                    ref_sha256=m.measurement_sha256,
                )
                for m in in_state_steps
            )
            worst = max(
                m.settle_ms for m in in_state_steps
                if m.settle_ms is not None
            ) if any(
                m.settle_ms is not None for m in in_state_steps
            ) else None
            if worst is None:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='insufficient_evidence',
                    reason='WAVEFORM_MISSING', evidence_refs=refs,
                ))
            elif worst <= response_time_threshold_ms and all(
                (m.overshoot_percent or 0.0) <= 10.0
                for m in in_state_steps
            ):
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='supported',
                    reason='TEMPORAL_QUALIFIED', evidence_refs=refs,
                ))
            else:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='supported_with_limitations',
                    reason='TEMPORAL_LIMITED', evidence_refs=refs,
                ))
        elif claim == 'overshoot_free':
            if not in_state_steps:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='insufficient_evidence',
                    reason='INSUFFICIENT_MEASUREMENTS',
                ))
                continue
            refs = tuple(
                AuthorityRef(
                    kind='temporal_step_response',
                    ref_id=m.measurement_id,
                    ref_sha256=m.measurement_sha256,
                )
                for m in in_state_steps
            )
            if any(m.overshoot_percent is None for m in in_state_steps):
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='insufficient_evidence',
                    reason='METHOD_UNDECLARED', evidence_refs=refs,
                ))
            elif all(
                (m.overshoot_percent or 0.0) <= 5.0
                and (m.undershoot_percent or 0.0) <= 5.0
                for m in in_state_steps
            ):
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='supported',
                    reason='TEMPORAL_QUALIFIED', evidence_refs=refs,
                ))
            else:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='unsupported',
                    reason='CLAIM_BEYOND_COVERAGE', evidence_refs=refs,
                ))
        elif claim in ('moving_edge_blur', 'ghosting_free'):
            if not in_state_motion:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='insufficient_evidence',
                    reason='INSUFFICIENT_MEASUREMENTS',
                ))
                continue
            instrumented = [
                m for m in in_state_motion
                if m.observation_class == 'instrumented'
            ]
            refs = tuple(
                AuthorityRef(
                    kind='motion_artifact_measurement',
                    ref_id=m.measurement_id,
                    ref_sha256=m.measurement_sha256,
                )
                for m in in_state_motion
            )
            if not instrumented:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='insufficient_evidence',
                    reason='SUBJECTIVE_ONLY', evidence_refs=refs,
                ))
            elif claim == 'ghosting_free' and any(
                (m.artifact_amplitude_percent or 0.0) > 0.0
                for m in instrumented
            ):
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='unsupported',
                    reason='CLAIM_BEYOND_COVERAGE', evidence_refs=refs,
                ))
            else:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='supported',
                    reason='TEMPORAL_QUALIFIED', evidence_refs=refs,
                ))
        elif claim == 'flicker_free':
            if not in_state_flicker:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='insufficient_evidence',
                    reason='INSUFFICIENT_MEASUREMENTS',
                ))
                continue
            instrumented = [
                m for m in in_state_flicker
                if m.observation_class == 'instrumented'
            ]
            refs = tuple(
                AuthorityRef(
                    kind='flicker_measurement',
                    ref_id=m.measurement_id,
                    ref_sha256=m.measurement_sha256,
                )
                for m in in_state_flicker
            )
            if not instrumented:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='insufficient_evidence',
                    reason='SUBJECTIVE_ONLY', evidence_refs=refs,
                ))
            elif all(
                (m.modulation_depth_percent or 0.0)
                <= flicker_depth_threshold_percent
                for m in instrumented
            ):
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='supported',
                    reason='TEMPORAL_QUALIFIED', evidence_refs=refs,
                ))
            else:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='unsupported',
                    reason='CLAIM_BEYOND_COVERAGE', evidence_refs=refs,
                ))
        elif claim == 'retention_free':
            if not in_state_retention:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='insufficient_evidence',
                    reason='INSUFFICIENT_MEASUREMENTS',
                ))
                continue
            refs = tuple(
                AuthorityRef(
                    kind='image_retention_observation',
                    ref_id=m.observation_id,
                    ref_sha256=m.observation_sha256,
                )
                for m in in_state_retention
            )
            if all(m.persistence == 'not_observed' for m in in_state_retention):
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='supported',
                    reason='TEMPORAL_QUALIFIED', evidence_refs=refs,
                ))
            elif all(
                m.persistence == 'transient_recovered'
                for m in in_state_retention
            ):
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='supported_with_limitations',
                    reason='TEMPORAL_LIMITED', evidence_refs=refs,
                ))
            else:
                verdicts.append(TemporalClaimVerdict(
                    kind=claim, verdict='unsupported',
                    reason='CLAIM_BEYOND_COVERAGE', evidence_refs=refs,
                ))
        else:
            verdicts.append(TemporalClaimVerdict(
                kind=claim, verdict='insufficient_evidence',
                reason='INSUFFICIENT_MEASUREMENTS',
            ))
    return TemporalDisplayQualification.create(
        document_id=state.document_id,
        state_ref=state_ref,
        claim_verdicts=tuple(verdicts),
        evaluated_at_utc=evaluated_at_utc or _utc_now(),
    )


# ---------------------------------------------------------------------------
# JA labels
# ---------------------------------------------------------------------------

CLAIM_KIND_LABELS: dict[str, str] = {
    'response_time': '応答速度',
    'overshoot_free': 'オーバーシュートなし',
    'moving_edge_blur': '動画ボケ',
    'ghosting_free': 'ゴーストなし',
    'flicker_free': 'フリッカーなし',
    'retention_free': '残像なし',
    'temporal_latency': '時間遅延',
}

VERDICT_LABELS: dict[str, str] = {
    'supported': '検証済み',
    'supported_with_limitations': '条件付き検証済み',
    'insufficient_evidence': '証拠不足',
    'unsupported': '不適格',
}

MECHANISM_LABELS: dict[str, str] = {
    'pixel_transition_response': '画素遷移応答',
    'display_persistence_sample_hold': 'サンプルホールド保持',
    'moving_edge_blur_measured': '移動エッジボケ実測',
    'strobe_bfi_persistence': 'BFI/ストロボ保持',
    'unknown': '不明',
}

REASON_LABELS: dict[str, str] = {
    'TEMPORAL_QUALIFIED': '時間特性は適格です',
    'TEMPORAL_LIMITED': '時間特性は条件付き適格です',
    'INSUFFICIENT_MEASUREMENTS': '測定が不足しています',
    'DISPLAY_STATE_INCOMPLETE': 'ディスプレイ状態が不完全です',
    'METHOD_UNDECLARED': '測定方法が未宣言です',
    'WAVEFORM_MISSING': '波形データがありません',
    'SUBJECTIVE_ONLY': '主観評価のみです',
    'CLAIM_BEYOND_COVERAGE': '主張が測定範囲を超えています',
    'STATE_MISMATCH': '状態が一致しません',
}
