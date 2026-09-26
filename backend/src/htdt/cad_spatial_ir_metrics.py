"""Spatial-impression diagnostic authority (#990, SPAT10+SPAT20 slices).

Derives spatial-impression metrics from exact binaural/directional room
responses — never from a mono IR. SPAT10 implements the IACC (interaural
cross-correlation coefficient) on an eligible left/right ear pair. SPAT20
implements the ISO 3382-1 directional metrics on pinned channels of a
``SpatialRoomImpulseResponseDataset`` (#974 directional receiver
authority):

- ``lateral_fraction`` — early lateral energy fraction J_LF: figure-of-8
  (null toward source) energy in the pinned early window over
  omnidirectional energy 0→window end;
- ``lateral_fraction_cosine`` — J_LFC: |p_L·p| over the same windows
  (weights each arrival by |cos θ| rather than cos²θ);
- ``listener_envelopment`` — late lateral level L_J in dB: figure-8
  energy in the pinned late window over the *full* energy of a pinned
  free-field omni reference artifact with absolute-amplitude authority.

Every result persists the time window, lag search, head orientation,
method/version and the measured/predicted evidence class of the IRs — no
metric may pretend to a different evidence class, and no concert-hall
reference thresholds are applied as home-theater targets.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator


SPATIAL_IR_SCHEMA_VERSION = 2
SPATIAL_IR_METRIC_AUTHORITY_VERSION = 'spatial-ir-metrics-2'

SpatialIRMetric = Literal[
    'iacc',
    'lateral_fraction',
    'lateral_fraction_cosine',
    'listener_envelopment',
]
SpatialIRMetricState = Literal['computed', 'blocked', 'unsupported']
IrEvidenceKind = Literal['predicted', 'measured']

# Method identity is pinned on the spec; the computation is version-pinned.
IACC_METHOD = 'iacc_normalized_cross_correlation_v1'
LATERAL_FRACTION_METHOD = 'iso3382_lateral_fraction_v1'
LATERAL_FRACTION_COSINE_METHOD = 'iso3382_lateral_fraction_cosine_v1'
LATE_LATERAL_LEVEL_METHOD = 'iso3382_late_lateral_level_v1'
DEFAULT_IACC_MAX_LAG_S = 0.001  # ITD search ±1 ms

SpatialIRAnalysisMethod = Literal[
    'iacc_normalized_cross_correlation_v1',
    'iso3382_lateral_fraction_v1',
    'iso3382_lateral_fraction_cosine_v1',
    'iso3382_late_lateral_level_v1',
]

_METRIC_METHOD: dict[str, str] = {
    'iacc': IACC_METHOD,
    'lateral_fraction': LATERAL_FRACTION_METHOD,
    'lateral_fraction_cosine': LATERAL_FRACTION_COSINE_METHOD,
    'listener_envelopment': LATE_LATERAL_LEVEL_METHOD,
}


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


class ImpulseEarEvidenceRef(BaseModel):
    """Exact impulse-response authority for one ear channel."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: IrEvidenceKind
    artifact_id: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    decoded_pcm_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_rate_hz: int = Field(gt=0)
    sample_count: int = Field(gt=0)
    ear: Literal['left', 'right']
    receiver_id: str = Field(min_length=1)
    receiver_entity_id: str = Field(min_length=1)
    absolute_amplitude_authority: bool


DirectionalChannelRole = Literal[
    'omnidirectional',
    'figure8_lateral',
    'free_field_omni_reference',
]


class DirectionalIrChannelRef(BaseModel):
    """Exact impulse-response authority for one directional channel.

    Lateral/envelopment metrics pin channels of a sealed spatial IR
    dataset (#974): a figure-of-8 whose null points at the source
    (``figure8_lateral``), a co-located omni (``omnidirectional``), or a
    calibrated free-field omni reference (``free_field_omni_reference``)
    used as the listener-envelopment level datum.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: IrEvidenceKind
    artifact_id: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    decoded_pcm_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_rate_hz: int = Field(gt=0)
    sample_count: int = Field(gt=0)
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    channel_role: DirectionalChannelRole
    receiver_id: str = Field(min_length=1)
    receiver_entity_id: str = Field(min_length=1)
    absolute_amplitude_authority: bool


class SpatialIRMetricSpec(BaseModel):
    """Pinned spatial-metric analysis design (SPAT10/SPAT20)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[2] = SPATIAL_IR_SCHEMA_VERSION
    authority_version: Literal[
        'spatial-ir-metrics-2'
    ] = SPATIAL_IR_METRIC_AUTHORITY_VERSION
    spec_id: str = Field(pattern=r'^spatial-ir-metric-spec:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    acoustic_scene_snapshot_id: str = Field(min_length=1)
    acoustic_scene_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_scenario_id: str = Field(min_length=1)
    source_scenario_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    metric: SpatialIRMetric
    left_ear: ImpulseEarEvidenceRef | None = None
    right_ear: ImpulseEarEvidenceRef | None = None
    lateral_channel: DirectionalIrChannelRef | None = None
    omni_channel: DirectionalIrChannelRef | None = None
    reference_channel: DirectionalIrChannelRef | None = None
    time_window_start_s: float = Field(ge=0.0)
    time_window_end_s: float = Field(gt=0.0)
    window_semantics: Literal['early', 'late', 'custom'] = 'custom'
    iacc_max_lag_s: float = Field(gt=0.0, default=DEFAULT_IACC_MAX_LAG_S)
    head_orientation_deg: float = 0.0
    analysis_method: SpatialIRAnalysisMethod = IACC_METHOD

    @model_validator(mode='after')
    def validate_spec(self) -> 'SpatialIRMetricSpec':
        if self.time_window_end_s <= self.time_window_start_s:
            raise ValueError('metric window end must exceed start')
        if self.analysis_method != _METRIC_METHOD[self.metric]:
            raise ValueError(
                f'{self.metric} requires analysis method '
                f'{_METRIC_METHOD[self.metric]!r}'
            )
        if self.metric == 'iacc':
            if (
                self.lateral_channel is not None
                or self.omni_channel is not None
                or self.reference_channel is not None
            ):
                raise ValueError(
                    'IACC uses ear evidence refs, not directional channels'
                )
            if self.left_ear is None or self.right_ear is None:
                raise ValueError(
                    'IACC requires an exact binaural pair — it can never be '
                    'derived from a mono IR'
                )
            for ear in (self.left_ear, self.right_ear):
                if (
                    ear.receiver_id != self.left_ear.receiver_id
                    or ear.receiver_entity_id
                    != self.left_ear.receiver_entity_id
                ):
                    raise ValueError(
                        'binaural pair must share one receiver identity'
                    )
            if self.left_ear.ear != 'left' or self.right_ear.ear != 'right':
                raise ValueError('ear channels must be labelled left/right')
            if (
                self.left_ear.sample_rate_hz != self.right_ear.sample_rate_hz
                or self.left_ear.sample_count != self.right_ear.sample_count
                or self.left_ear.kind != self.right_ear.kind
            ):
                raise ValueError(
                    'binaural pair must share rate, length and evidence kind'
                )
        else:
            # SPAT20: directional metrics evaluate pinned channels of an
            # exact spatial IR dataset — never synthesized directions.
            if self.left_ear is not None or self.right_ear is not None:
                raise ValueError(
                    'directional metrics use channel refs, not an ear pair'
                )
            if self.lateral_channel is None:
                raise ValueError(
                    'directional metrics require a pinned figure-8 lateral '
                    'channel — never derived from omnidirectional data alone'
                )
            if self.lateral_channel.channel_role != 'figure8_lateral':
                raise ValueError(
                    'lateral_channel must be a figure-8 receiver whose null '
                    'points at the source'
                )
            if self.metric == 'listener_envelopment':
                if self.omni_channel is not None:
                    raise ValueError(
                        'listener envelopment takes a free-field reference '
                        'channel, not an in-room omni'
                    )
                if self.reference_channel is None:
                    raise ValueError(
                        'listener envelopment requires a pinned free-field '
                        'omni reference — ISO 3382-1 L_J is a level, never '
                        'an unreferenced ratio'
                    )
                if (
                    self.reference_channel.channel_role
                    != 'free_field_omni_reference'
                ):
                    raise ValueError(
                        'reference_channel must be a free-field omni '
                        'reference artifact'
                    )
                if not self.reference_channel.absolute_amplitude_authority:
                    raise ValueError(
                        'free-field reference must carry absolute-amplitude '
                        'authority — relative references cannot yield L_J'
                    )
                if (
                    self.reference_channel.sample_rate_hz
                    != self.lateral_channel.sample_rate_hz
                ):
                    raise ValueError(
                        'reference and lateral channels must share a sample '
                        'rate'
                    )
            else:
                if self.reference_channel is not None:
                    raise ValueError(
                        'lateral fractions take a co-located omni channel, '
                        'not a free-field reference'
                    )
                if self.omni_channel is None:
                    raise ValueError(
                        'lateral fractions require a pinned co-located omni '
                        'channel'
                    )
                if self.omni_channel.channel_role != 'omnidirectional':
                    raise ValueError(
                        'omni_channel must be an omnidirectional channel'
                    )
                pair = (self.lateral_channel, self.omni_channel)
                if (
                    pair[0].dataset_id != pair[1].dataset_id
                    or pair[0].dataset_sha256 != pair[1].dataset_sha256
                ):
                    raise ValueError(
                        'lateral and omni channels must come from the same '
                        'sealed dataset'
                    )
                if (
                    pair[0].sample_rate_hz != pair[1].sample_rate_hz
                    or pair[0].sample_count != pair[1].sample_count
                    or pair[0].kind != pair[1].kind
                ):
                    raise ValueError(
                        'directional channels must share rate, length and '
                        'evidence kind'
                    )
        if not math.isfinite(float(self.head_orientation_deg)):
            raise ValueError('head orientation must be finite')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('spatial IR metric spec semantic hash mismatch')
        if self.spec_id != f'spatial-ir-metric-spec:{expected}':
            raise ValueError('spatial IR metric spec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'spec_id', 'semantic_sha256'},
        )


def build_spatial_ir_metric_spec(**kwargs: Any) -> SpatialIRMetricSpec:
    kwargs.setdefault(
        'analysis_method',
        _METRIC_METHOD.get(kwargs.get('metric'), IACC_METHOD),
    )
    probe = SpatialIRMetricSpec.model_construct(
        spec_id='spatial-ir-metric-spec:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )
    digest = _digest(probe.semantic_payload())
    return SpatialIRMetricSpec(
        spec_id=f'spatial-ir-metric-spec:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


class SpatialIRMetricResult(BaseModel):
    """Persisted metric outcome; BLOCKED/UNSUPPORTED states are explicit."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[2] = SPATIAL_IR_SCHEMA_VERSION
    authority_version: Literal[
        'spatial-ir-metrics-2'
    ] = SPATIAL_IR_METRIC_AUTHORITY_VERSION
    result_id: str = Field(pattern=r'^spatial-ir-metric-result:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    spec_id: str = Field(pattern=r'^spatial-ir-metric-spec:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    metric: SpatialIRMetric
    state: SpatialIRMetricState
    value: float | None = None
    #: Dimensionless ratios/coefficients are 'dimensionless'; level metrics
    #: (listener envelopment L_J) report 'decibel'.
    value_unit: Literal['dimensionless', 'decibel'] | None = None
    blocked_reason: str | None = None
    analysis_method: str = Field(min_length=1)
    evidence_kind: IrEvidenceKind | None = None

    @model_validator(mode='after')
    def validate_result(self) -> 'SpatialIRMetricResult':
        if self.state == 'computed':
            if self.value is None:
                raise ValueError('a computed metric requires its value')
            if not math.isfinite(float(self.value)):
                raise ValueError('metric value must be finite')
            if self.value_unit is None:
                raise ValueError('a computed metric requires its value unit')
            if self.blocked_reason is not None:
                raise ValueError('computed metrics carry no blocked reason')
        else:
            if self.value is not None or self.value_unit is not None:
                raise ValueError(
                    'blocked/unsupported metrics never report a value'
                )
            if not self.blocked_reason:
                raise ValueError('blocked/unsupported metrics require a reason')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('spatial IR metric result hash mismatch')
        if self.result_id != f'spatial-ir-metric-result:{expected}':
            raise ValueError('spatial IR metric result id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'result_id', 'semantic_sha256'},
        )


def _emit_result(
    spec: SpatialIRMetricSpec,
    *,
    state: SpatialIRMetricState,
    value: float | None,
    value_unit: Literal['dimensionless', 'decibel'] | None = None,
    blocked_reason: str | None,
    evidence_kind: IrEvidenceKind | None,
) -> SpatialIRMetricResult:
    payload = {
        'schema_version': SPATIAL_IR_SCHEMA_VERSION,
        'authority_version': SPATIAL_IR_METRIC_AUTHORITY_VERSION,
        'spec_id': spec.spec_id,
        'spec_semantic_sha256': spec.semantic_sha256,
        'metric': spec.metric,
        'state': state,
        'value': value,
        'value_unit': value_unit,
        'blocked_reason': blocked_reason,
        'analysis_method': spec.analysis_method,
        'evidence_kind': evidence_kind,
    }
    digest = _digest(payload)
    return SpatialIRMetricResult(
        spec_id=spec.spec_id,
        spec_semantic_sha256=spec.semantic_sha256,
        metric=spec.metric,
        state=state,
        value=value,
        value_unit=value_unit,
        blocked_reason=blocked_reason,
        analysis_method=spec.analysis_method,
        evidence_kind=evidence_kind,
        result_id=f'spatial-ir-metric-result:{digest}',
        semantic_sha256=digest,
    )


def _iacc(
    left: np.ndarray,
    right: np.ndarray,
    sample_rate_hz: int,
    window_start_s: float,
    window_end_s: float,
    max_lag_s: float,
) -> float:
    """Normalized interaural cross-correlation peak over a bounded lag."""
    i0 = int(round(window_start_s * sample_rate_hz))
    i1 = min(len(left), int(round(window_end_s * sample_rate_hz)))
    if i1 - i0 < 2:
        raise ValueError('metric window covers fewer than 2 samples')
    left_w = left[i0:i1]
    right_w = right[i0:i1]
    norm = float(np.sqrt(np.sum(left_w**2) * np.sum(right_w**2)))
    if norm <= 0.0:
        raise ValueError('IACC undefined on a silent ear channel')
    max_lag = int(round(max_lag_s * sample_rate_hz))
    best = 0.0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            l_seg = left_w[lag:]
            r_seg = right_w[: len(left_w) - lag]
        else:
            l_seg = left_w[: lag]
            r_seg = right_w[-lag:]
        corr = float(np.sum(l_seg * r_seg)) / norm
        if abs(corr) > abs(best):
            best = corr
    return best


def _window_slice(
    ir: np.ndarray,
    sample_rate_hz: int,
    window_start_s: float,
    window_end_s: float,
) -> np.ndarray:
    i0 = int(round(window_start_s * sample_rate_hz))
    i1 = min(len(ir), int(round(window_end_s * sample_rate_hz)))
    if i1 - i0 < 2:
        raise ValueError('metric window covers fewer than 2 samples')
    return ir[i0:i1]


def _lateral_fraction(
    lateral: np.ndarray,
    omni: np.ndarray,
    sample_rate_hz: int,
    window_start_s: float,
    window_end_s: float,
) -> float:
    """ISO 3382-1 J_LF: early figure-8 energy over 0→end omni energy."""
    lateral_w = _window_slice(
        lateral, sample_rate_hz, window_start_s, window_end_s
    )
    omni_w = _window_slice(omni, sample_rate_hz, 0.0, window_end_s)
    denominator = float(np.sum(omni_w**2))
    if denominator <= 0.0:
        raise ValueError('omnidirectional reference energy is zero')
    return float(np.sum(lateral_w**2)) / denominator


def _lateral_fraction_cosine(
    lateral: np.ndarray,
    omni: np.ndarray,
    sample_rate_hz: int,
    window_start_s: float,
    window_end_s: float,
) -> float:
    """ISO 3382-1 J_LFC: |p_L·p| in the early window over 0→end omni."""
    lateral_w = _window_slice(
        lateral, sample_rate_hz, window_start_s, window_end_s
    )
    omni_w = _window_slice(
        omni, sample_rate_hz, window_start_s, window_end_s
    )
    denominator = float(
        np.sum(_window_slice(omni, sample_rate_hz, 0.0, window_end_s) ** 2)
    )
    if denominator <= 0.0:
        raise ValueError('omnidirectional reference energy is zero')
    return float(np.sum(np.abs(lateral_w * omni_w))) / denominator


def _late_lateral_level(
    lateral: np.ndarray,
    reference: np.ndarray,
    sample_rate_hz: int,
    window_start_s: float,
    window_end_s: float,
) -> float:
    """ISO 3382-1 L_J (dB): late figure-8 energy vs free-field omni."""
    lateral_w = _window_slice(
        lateral, sample_rate_hz, window_start_s, window_end_s
    )
    denominator = float(np.sum(reference**2))
    if denominator <= 0.0:
        raise ValueError('free-field reference energy is zero')
    numerator = float(np.sum(lateral_w**2))
    if numerator <= 0.0:
        raise ValueError('late lateral energy is below the measurement floor')
    return 10.0 * math.log10(numerator / denominator)


def _require_pinned_ir(
    samples: tuple[float, ...] | np.ndarray | None,
    ref: DirectionalIrChannelRef,
    label: str,
) -> np.ndarray | None:
    if samples is None:
        return None
    if (
        sha256(np.asarray(samples, dtype='<f8').tobytes()).hexdigest()
        != ref.decoded_pcm_sha256
    ):
        raise ValueError(f'{label} IR does not match the pinned artifact')
    vector = np.asarray(samples, dtype=np.float64)
    if len(vector) != ref.sample_count:
        raise ValueError(f'{label} IR length does not match the pinned artifact')
    if not np.all(np.isfinite(vector)):
        raise ValueError(f'{label} IR samples must be finite')
    return vector


def evaluate_spatial_ir_metric(
    spec: SpatialIRMetricSpec,
    *,
    left_ir: tuple[float, ...] | np.ndarray | None = None,
    right_ir: tuple[float, ...] | np.ndarray | None = None,
    lateral_ir: tuple[float, ...] | np.ndarray | None = None,
    omni_ir: tuple[float, ...] | np.ndarray | None = None,
    reference_ir: tuple[float, ...] | np.ndarray | None = None,
) -> SpatialIRMetricResult:
    """Evaluate the pinned metric against its exact IR authorities."""
    if spec.metric != 'iacc':
        return _evaluate_directional_metric(
            spec,
            lateral_ir=lateral_ir,
            omni_ir=omni_ir,
            reference_ir=reference_ir,
        )
    left = np.asarray(left_ir, dtype=np.float64) if left_ir is not None else None
    right = (
        np.asarray(right_ir, dtype=np.float64) if right_ir is not None else None
    )
    if left is None or right is None:
        return _emit_result(
            spec,
            state='blocked',
            value=None,
            blocked_reason='binaural ear IR samples were not supplied',
            evidence_kind=spec.left_ear.kind,
        )
    expected_kind = spec.left_ear.kind
    if (
        sha256(np.asarray(left_ir, dtype='<f8').tobytes()).hexdigest()
        != spec.left_ear.decoded_pcm_sha256
    ):
        raise ValueError('left-ear IR does not match the pinned artifact')
    if (
        sha256(np.asarray(right_ir, dtype='<f8').tobytes()).hexdigest()
        != spec.right_ear.decoded_pcm_sha256
    ):
        raise ValueError('right-ear IR does not match the pinned artifact')
    if len(left) != spec.left_ear.sample_count or (
        len(right) != spec.right_ear.sample_count
    ):
        raise ValueError('ear IR lengths do not match the pinned artifacts')
    if not np.all(np.isfinite(left)) or not np.all(np.isfinite(right)):
        raise ValueError('ear IR samples must be finite')

    try:
        value = _iacc(
            left,
            right,
            spec.left_ear.sample_rate_hz,
            spec.time_window_start_s,
            spec.time_window_end_s,
            spec.iacc_max_lag_s,
        )
    except ValueError as exc:
        return _emit_result(
            spec,
            state='blocked',
            value=None,
            blocked_reason=str(exc),
            evidence_kind=expected_kind,
        )
    return _emit_result(
        spec,
        state='computed',
        value=value,
        value_unit='dimensionless',
        blocked_reason=None,
        evidence_kind=expected_kind,
    )


def _evaluate_directional_metric(
    spec: SpatialIRMetricSpec,
    *,
    lateral_ir: tuple[float, ...] | np.ndarray | None,
    omni_ir: tuple[float, ...] | np.ndarray | None,
    reference_ir: tuple[float, ...] | np.ndarray | None,
) -> SpatialIRMetricResult:
    evidence_kind = spec.lateral_channel.kind
    lateral = _require_pinned_ir(lateral_ir, spec.lateral_channel, 'lateral')
    if spec.metric == 'listener_envelopment':
        reference = _require_pinned_ir(
            reference_ir, spec.reference_channel, 'reference'
        )
        omni = None
        missing = lateral is None or reference is None
    else:
        omni = _require_pinned_ir(omni_ir, spec.omni_channel, 'omni')
        reference = None
        missing = lateral is None or omni is None
    if missing:
        return _emit_result(
            spec,
            state='blocked',
            value=None,
            blocked_reason=(
                'directional channel IR samples were not supplied'
            ),
            evidence_kind=evidence_kind,
        )
    try:
        if spec.metric == 'lateral_fraction':
            value = _lateral_fraction(
                lateral,
                omni,
                spec.lateral_channel.sample_rate_hz,
                spec.time_window_start_s,
                spec.time_window_end_s,
            )
            unit: Literal['dimensionless', 'decibel'] = 'dimensionless'
        elif spec.metric == 'lateral_fraction_cosine':
            value = _lateral_fraction_cosine(
                lateral,
                omni,
                spec.lateral_channel.sample_rate_hz,
                spec.time_window_start_s,
                spec.time_window_end_s,
            )
            unit = 'dimensionless'
        else:
            value = _late_lateral_level(
                lateral,
                reference,
                spec.lateral_channel.sample_rate_hz,
                spec.time_window_start_s,
                spec.time_window_end_s,
            )
            unit = 'decibel'
    except ValueError as exc:
        return _emit_result(
            spec,
            state='blocked',
            value=None,
            blocked_reason=str(exc),
            evidence_kind=evidence_kind,
        )
    return _emit_result(
        spec,
        state='computed',
        value=value,
        value_unit=unit,
        blocked_reason=None,
        evidence_kind=evidence_kind,
    )
