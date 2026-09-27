"""Video input-to-photon latency commissioning authority (#1005).

Distinct from A/V lip-sync: this authority records **when the display
shows a response**, for one exact display/projector mode — not whether
audio and video agree.

Authorities:

- :class:`VideoLatencyCondition` — the exact operating state a latency
  result binds to: signal path, source mode, resolution, refresh
  (fixed/VRR), bit depth/chroma, HDR format, VRR/ALLM/QFT states,
  picture/processing modes, projector light-engine state and firmware.
  A latency number without this condition is not reusable authority.
- :class:`VideoLatencyMeasurement` — repeated observations of one
  ``LatencyMetricKind`` (``input_to_photon``, ``video_pipeline``,
  ``pixel_response``, ``controller_to_photon``, ``device_reported``)
  under one ``LatencyMethod`` (``dedicated_lag_tester``,
  ``photodiode_trigger``, ``high_speed_camera``, ``device_reported``,
  ``published``, ``visual_estimate``) at a declared scan position.

Honesty rules:

- Metric kinds and methods are never conflated: a device-reported LIP
  value is ``device_reported`` evidence, not a measured photon result;
  a published review number is reference evidence, not a measurement of
  the user's installed unit.
- ``latency_frames`` is derived only when a stable effective rate
  exists (``fixed`` refresh or an explicit effective VRR rate).
- Scan position (top/center/bottom/normalized) is bound when the method
  is scanout-sensitive — top/bottom results are not interchangeable.
- VRR conditions carry min/max/effective rate; two fixed-rate points
  never become a claimed continuous VRR curve.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash




RefreshKind = Literal['fixed', 'vrr', 'unknown']
HdmilFeatureState = Literal[
    'unknown', 'unsupported', 'supported', 'negotiated', 'observed_active'
]
"""Feature signaling states (#1005): capability, negotiation and observed
activation are separate facts — ``ALLM = on`` is never a latency value."""

LatencyMetricKind = Literal[
    'input_to_photon',
    'video_pipeline',
    'pixel_response',
    'controller_to_photon',
    'device_reported',
]
LatencyMethod = Literal[
    'dedicated_lag_tester',
    'photodiode_trigger',
    'high_speed_camera',
    'device_reported',
    'published',
    'visual_estimate',
]
ScanPosition = Literal[
    'top', 'center', 'bottom', 'normalized_y', 'full_screen', 'unknown'
]


class VideoLatencyCondition(BaseModel):
    """One exact display/projector latency condition (#1005 §2)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['video-latency-condition-1'] = (
        'video-latency-condition-1'
    )
    condition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    # Exact signal path binding (all-or-none triple).
    signal_path_id: str | None = None
    signal_path_version: str | None = None
    signal_path_sha256: str | None = None
    source_device_mode: str | None = None
    resolution_label: str | None = None
    refresh_rate_hz: float | None = Field(default=None, gt=0.0)
    refresh_kind: RefreshKind = 'unknown'
    vrr_min_hz: float | None = Field(default=None, gt=0.0)
    vrr_max_hz: float | None = Field(default=None, gt=0.0)
    vrr_effective_hz: float | None = Field(default=None, gt=0.0)
    vrr_cadence: str | None = None
    bit_depth: int | None = Field(default=None, ge=0)
    chroma_format: str | None = None
    hdr_format_family: str | None = None
    vrr_state: HdmilFeatureState = 'unknown'
    allm_state: HdmilFeatureState = 'unknown'
    qft_state: HdmilFeatureState = 'unknown'
    picture_mode: str | None = None
    scaling_mode: Literal[
        'native_1to1', 'scaled', 'overscan', 'anamorphic', 'unknown'
    ] = 'unknown'
    motion_interpolation_mode: str | None = None
    tone_map_mode: str | None = None
    projector_light_engine_state: str | None = None
    projector_low_latency_mode: str | None = None
    presentation_profile_id: str | None = None
    firmware_versions: tuple[str, ...] = ()
    condition_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'condition_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'VideoLatencyCondition':
        path = (
            self.signal_path_id,
            self.signal_path_version,
            self.signal_path_sha256,
        )
        if (None in path) and any(v is not None for v in path):
            raise ValueError(
                'signal path id/version/sha256 must be supplied together '
                'or not at all'
            )
        if self.refresh_kind == 'vrr':
            if self.vrr_min_hz is None or self.vrr_max_hz is None:
                raise ValueError(
                    'vrr refresh_kind requires vrr_min_hz and vrr_max_hz'
                )
            if (
                self.vrr_effective_hz is not None
                and not self.vrr_min_hz <= self.vrr_effective_hz <= self.vrr_max_hz
            ):
                raise ValueError(
                    'vrr_effective_hz must lie inside [vrr_min_hz, vrr_max_hz]'
                )
        expected = _hash(self.semantic_payload())
        if expected != self.condition_sha256:
            raise ValueError('video latency condition semantic hash mismatch')
        return self


class LatencyObservation(BaseModel):
    """One repeated latency observation at the bound scan position."""

    model_config = ConfigDict(frozen=True)

    observed_at_utc: str = Field(min_length=1)
    latency_seconds: float = Field(gt=0.0)
    note: str | None = None


class VideoLatencyMeasurement(BaseModel):
    """Repeated latency observations of one kind under one method (#1005 §3)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['video-latency-measurement-1'] = (
        'video-latency-measurement-1'
    )
    measurement_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    condition_version: str = Field(min_length=1)
    condition_sha256: str = Field(min_length=16)
    metric_kind: LatencyMetricKind
    method: LatencyMethod
    method_version: str | None = None
    trigger_definition: str | None = None
    response_detection: str | None = None
    scan_position: ScanPosition = 'unknown'
    scan_position_y_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0
    )
    instrument: str | None = None
    observations: tuple[LatencyObservation, ...] = ()
    mean_latency_seconds: float | None = None
    min_latency_seconds: float | None = None
    max_latency_seconds: float | None = None
    latency_frames: float | None = None
    uncertainty_seconds: float | None = Field(default=None, ge=0.0)
    reported_source: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    measurement_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'measurement_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'VideoLatencyMeasurement':
        if self.method == 'device_reported' or (
            self.metric_kind == 'device_reported'
        ):
            if self.reported_source is None:
                raise ValueError(
                    'device-reported latency requires reported_source — '
                    'LIP/vendor metadata is reported evidence, not a '
                    'measured photon result'
                )
        if self.scan_position == 'normalized_y' and (
            self.scan_position_y_fraction is None
        ):
            raise ValueError(
                'normalized_y scan position requires '
                'scan_position_y_fraction'
            )
        if self.method == 'published' and self.reported_source is None:
            raise ValueError(
                'published latency requires reported_source — review-site '
                'numbers are reference evidence, not measurements of the '
                'installed unit'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.measurement_sha256:
            raise ValueError(
                'video latency measurement semantic hash mismatch'
            )
        return self


def build_video_latency_condition(**kwargs) -> VideoLatencyCondition:
    probe = VideoLatencyCondition.model_construct(
        condition_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return VideoLatencyCondition(
        **probe.model_dump(
            mode='python', exclude={'condition_sha256', 'schema_version'}
        ),
        condition_sha256=digest,
    )


def build_video_latency_measurement(
    *,
    condition: VideoLatencyCondition,
    metric_kind: LatencyMetricKind,
    method: LatencyMethod,
    observations: tuple[LatencyObservation, ...] = (),
    refresh_rate_hz_effective: float | None = None,
    **kwargs,
) -> VideoLatencyMeasurement:
    """Build a measurement bound to an exact condition.

    ``refresh_rate_hz_effective`` is the stable rate used only for the
    optional ``latency_frames`` derivation — for VRR it must be the
    measured effective rate, never the declared maximum.
    """
    latencies = [o.latency_seconds for o in observations]
    mean = sum(latencies) / len(latencies) if latencies else None
    frames = None
    if mean is not None and refresh_rate_hz_effective is not None:
        frames = mean * refresh_rate_hz_effective
    probe = VideoLatencyMeasurement.model_construct(
        condition_id=condition.condition_id,
        condition_version=condition.version,
        condition_sha256=condition.condition_sha256,
        metric_kind=metric_kind,
        method=method,
        observations=observations,
        mean_latency_seconds=mean,
        min_latency_seconds=min(latencies) if latencies else None,
        max_latency_seconds=max(latencies) if latencies else None,
        latency_frames=frames,
        measurement_sha256='x' * 64,
        **kwargs,
    )
    digest = _hash(probe.semantic_payload())
    return VideoLatencyMeasurement(
        **probe.model_dump(
            mode='python', exclude={'measurement_sha256', 'schema_version'}
        ),
        measurement_sha256=digest,
    )
