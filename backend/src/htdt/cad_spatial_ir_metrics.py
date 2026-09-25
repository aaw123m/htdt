"""Spatial-impression diagnostic authority (#990, SPAT10 slice).

Derives spatial-impression metrics from exact binaural/directional room
responses — never from a mono IR. SPAT10 implements the IACC (interaural
cross-correlation coefficient) on an eligible left/right ear pair;
laterality and envelopment metrics are declared but report BLOCKED until a
validated directional receiver authority supplies them.

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


SPATIAL_IR_SCHEMA_VERSION = 1
SPATIAL_IR_METRIC_AUTHORITY_VERSION = 'spatial-ir-metrics-1'

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
DEFAULT_IACC_MAX_LAG_S = 0.001  # ITD search ±1 ms


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


class SpatialIRMetricSpec(BaseModel):
    """Pinned spatial-metric analysis design (SPAT10)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SPATIAL_IR_SCHEMA_VERSION
    authority_version: Literal[
        'spatial-ir-metrics-1'
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
    time_window_start_s: float = Field(ge=0.0)
    time_window_end_s: float = Field(gt=0.0)
    window_semantics: Literal['early', 'late', 'custom'] = 'custom'
    iacc_max_lag_s: float = Field(gt=0.0, default=DEFAULT_IACC_MAX_LAG_S)
    head_orientation_deg: float = 0.0
    analysis_method: Literal[
        'iacc_normalized_cross_correlation_v1'
    ] = IACC_METHOD

    @model_validator(mode='after')
    def validate_spec(self) -> 'SpatialIRMetricSpec':
        if self.time_window_end_s <= self.time_window_start_s:
            raise ValueError('metric window end must exceed start')
        if self.metric == 'iacc':
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
            # SPAT20+ metrics need a directional receiver authority this
            # slice does not define; keep them declared-but-unsupported.
            if self.left_ear is not None or self.right_ear is not None:
                raise ValueError(
                    'non-IACC metrics require a directional receiver '
                    'authority (SPAT20+)'
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

    schema_version: Literal[1] = SPATIAL_IR_SCHEMA_VERSION
    authority_version: Literal[
        'spatial-ir-metrics-1'
    ] = SPATIAL_IR_METRIC_AUTHORITY_VERSION
    result_id: str = Field(pattern=r'^spatial-ir-metric-result:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    spec_id: str = Field(pattern=r'^spatial-ir-metric-spec:[0-9a-f]{64}$')
    spec_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    metric: SpatialIRMetric
    state: SpatialIRMetricState
    value: float | None = None
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
            if self.blocked_reason is not None:
                raise ValueError('computed metrics carry no blocked reason')
        else:
            if self.value is not None:
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


def evaluate_spatial_ir_metric(
    spec: SpatialIRMetricSpec,
    *,
    left_ir: tuple[float, ...] | np.ndarray | None = None,
    right_ir: tuple[float, ...] | np.ndarray | None = None,
) -> SpatialIRMetricResult:
    """Evaluate the pinned metric against its exact ear IR authorities."""
    if spec.metric != 'iacc':
        return _emit_result(
            spec,
            state='unsupported',
            value=None,
            blocked_reason=(
                'metric requires a validated directional receiver authority '
                '(SPAT20+); never derived from omnidirectional data'
            ),
            evidence_kind=None,
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
        blocked_reason=None,
        evidence_kind=expected_kind,
    )
