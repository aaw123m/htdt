"""Moving-microphone spatial-average measurement authority (#996).

HTDT's measurement model is built on exact discrete receiver points. A
moving-microphone measurement (MMM) is a different observable class:

- its ordinary use is a deliberate **regional spatial average** — a
  robust magnitude/level descriptor that is intentionally insensitive to
  centimeter-scale local comb structure;
- a *tracked* trajectory variant can later support field reconstruction —
  a separate, richer claim that must never be conflated with the plain
  regional average.

A spatial-average result therefore can never claim
``receiver position = MLP XYZ`` merely because the mic moved around the
MLP; it binds a declared **region** (or an explicit trajectory authority),
never a point.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import Position3
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload






_SHA256_PATTERN = r'^[0-9a-f]{64}$'


MEASUREMENT_KIND_SPATIAL_AVERAGE_MOVING_MIC = 'SPATIAL_AVERAGE_MOVING_MIC'

TrajectoryRepresentation = Literal[
    'region_only_manual',
    'tracked',
    'prescribed_path',
    'imported',
    'unknown',
]
"""How the mic path is evidenced. ``region_only_manual`` is the basic MMM
case: a declared region plus user-attested coverage — never a synthesized
dense XYZ trajectory."""

TrajectoryObservationQuality = Literal[
    'untracked',
    'coarse',
    'tracked',
    'surveyed',
    'unknown',
]

MovementCoverageState = Literal[
    'declared',
    'attested',
    'tracked_verified',
    'partial',
    'unknown',
]

RtaAveragingMode = Literal[
    'linear_time',
    'exponential',
    'fifo',
    'producer',
    'unknown',
]

MovingMicPurpose = Literal[
    'regional_average',
    'spatially_robust_eq',
    'field_reconstruction',
    'unknown',
]


class SpatialAverageRegion(BaseModel):
    """Declared region a moving-mic measurement characterizes.

    Either a named anchor region (seat neighborhood, listening area) or an
    explicit axis-aligned volume — always a region identity, never a
    single point claim.
    """

    model_config = ConfigDict(frozen=True)

    region_id: str | None = None
    anchor_target_id: str | None = None
    bounds_min: Position3 | None = None
    bounds_max: Position3 | None = None

    @model_validator(mode='after')
    def valid_region(self) -> 'SpatialAverageRegion':
        if self.region_id is None and self.bounds_min is None:
            raise ValueError(
                'a spatial-average region needs a region_id or explicit bounds'
            )
        if (self.bounds_min is None) != (self.bounds_max is None):
            raise ValueError('region bounds require both min and max')
        if self.bounds_min is not None and self.bounds_max is not None:
            for axis in ('x_m', 'y_m', 'z_m'):
                if getattr(self.bounds_max, axis) <= getattr(self.bounds_min, axis):
                    raise ValueError('region bounds must satisfy min < max per axis')
        return self


class TrajectorySample(BaseModel):
    model_config = ConfigDict(frozen=True)

    t_s: float = Field(ge=0.0)
    position: Position3

    @model_validator(mode='after')
    def valid_sample(self) -> 'TrajectorySample':
        if not isfinite(float(self.t_s)):
            raise ValueError('trajectory time must be finite')
        return self


class MovingMicrophoneMeasurementSpec(BaseModel):
    """Immutable acquisition spec a spatial-average result replays from."""

    model_config = ConfigDict(frozen=True)

    spec_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    system_variant_id: str | None = None
    source_channel_role: str | None = None
    routing_profile_ref: str | None = None
    stimulus_profile_id: str | None = None
    stimulus_profile_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    microphone_ref: str | None = None
    response_calibration_ref: str | None = None
    absolute_level_capability: bool | None = None
    acquisition_engine: str | None = None
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    region: SpatialAverageRegion
    trajectory_representation: TrajectoryRepresentation
    trajectory: tuple[TrajectorySample, ...] = ()
    trajectory_frame: str | None = None
    trajectory_timing_alignment_s: float | None = None
    trajectory_observation_quality: TrajectoryObservationQuality = 'unknown'
    intended_duration_s: float | None = Field(default=None, gt=0.0)
    averaging_duration_s: float | None = Field(default=None, gt=0.0)
    rta_method: str | None = None
    fft_size: int | None = Field(default=None, ge=256)
    window_kind: str | None = None
    overlap_fraction: float | None = None
    averaging_mode: RtaAveragingMode = 'unknown'
    averaging_time_s: float | None = Field(default=None, gt=0.0)
    frequency_resolution_hz: float | None = Field(default=None, gt=0.0)
    purpose: MovingMicPurpose = 'regional_average'
    room_operating_state: str | None = None
    created_at_utc: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_spec(self) -> 'MovingMicrophoneMeasurementSpec':
        if self.trajectory_representation == 'tracked':
            if not self.trajectory:
                raise ValueError(
                    'tracked representation requires time-indexed trajectory samples'
                )
            if self.trajectory_frame is None:
                raise ValueError('tracked trajectory requires a coordinate frame')
        elif self.trajectory:
            raise ValueError(
                'trajectory samples are only valid under tracked/'
                'prescribed_path representation'
            )
        if self.trajectory:
            times = [sample.t_s for sample in self.trajectory]
            if times != sorted(times):
                raise ValueError('trajectory samples must be time-ordered')
        if self.trajectory_timing_alignment_s is not None and not isfinite(
            float(self.trajectory_timing_alignment_s)
        ):
            raise ValueError('trajectory_timing_alignment_s must be finite')
        for label, value in (
            ('overlap_fraction', self.overlap_fraction),
            ('frequency_resolution_hz', self.frequency_resolution_hz),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{label} must be finite')
        if self.overlap_fraction is not None and not (
            0.0 <= self.overlap_fraction < 1.0
        ):
            raise ValueError('overlap_fraction must lie in [0, 1)')
        if self.spec_sha256 != _hash(self.identity_payload()):
            raise ValueError('moving-mic spec hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'spec_sha256'})


class SpatialAverageMeasurement(BaseModel):
    """Sealed regional spatial-average result.

    Deliberately carries no ``receiver_position`` — a spatial average is
    regional evidence; binding a point would silently claim a discrete
    measurement.
    """

    model_config = ConfigDict(frozen=True)

    result_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    spec_id: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_kind: Literal['SPATIAL_AVERAGE_MOVING_MIC'] = (
        MEASUREMENT_KIND_SPATIAL_AVERAGE_MOVING_MIC
    )
    region: SpatialAverageRegion
    frequency_hz: tuple[float, ...] = Field(min_length=1)
    level_db: tuple[float, ...] = Field(min_length=1)
    level_reference: Literal['absolute_spl', 'dbfs', 'relative', 'unknown'] = 'unknown'
    coverage_state: MovementCoverageState = 'unknown'
    actual_duration_s: float | None = Field(default=None, gt=0.0)
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    quality_status: str = 'unknown'
    quality_reasons: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    result_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_result(self) -> 'SpatialAverageMeasurement':
        if len(self.frequency_hz) != len(self.level_db):
            raise ValueError('frequency/level arrays must align')
        if any(not isfinite(float(v)) or v <= 0 for v in self.frequency_hz):
            raise ValueError('frequency axis must be finite and positive')
        if any(
            self.frequency_hz[i] <= self.frequency_hz[i - 1]
            for i in range(1, len(self.frequency_hz))
        ):
            raise ValueError('frequency axis must be strictly increasing')
        if any(not isfinite(float(v)) for v in self.level_db):
            raise ValueError('level values must be finite')
        if self.result_sha256 != _hash(self.identity_payload()):
            raise ValueError('spatial-average result hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'result_sha256'})


def build_moving_mic_spec(**kwargs: Any) -> MovingMicrophoneMeasurementSpec:
    """Assemble and seal a :class:`MovingMicrophoneMeasurementSpec`."""
    payload = {'spec_sha256': '0' * 64, **kwargs}
    provisional = MovingMicrophoneMeasurementSpec.model_construct(**canonicalize_payload(MovingMicrophoneMeasurementSpec, dict(**payload)))
    payload['spec_sha256'] = _hash(provisional.identity_payload())
    return MovingMicrophoneMeasurementSpec(**payload)


def build_spatial_average_measurement(**kwargs: Any) -> SpatialAverageMeasurement:
    """Assemble and seal a :class:`SpatialAverageMeasurement`."""
    payload = {'result_sha256': '0' * 64, **kwargs}
    provisional = SpatialAverageMeasurement.model_construct(**canonicalize_payload(SpatialAverageMeasurement, dict(**payload)))
    payload['result_sha256'] = _hash(provisional.identity_payload())
    return SpatialAverageMeasurement(**payload)


def validate_regional_identity(result: SpatialAverageMeasurement) -> None:
    """Guard: a spatial average may never be re-labeled as a point.

    The model structurally cannot carry ``receiver_position``; this guard
    additionally rejects provenance notes or downstream labels that try
    to equate the region with a single measurement point, and requires
    the result's region to actually be a region (bounds or region_id),
    matching the spec's declared region.
    """
    if result.region.bounds_min is None and result.region.region_id is None:
        raise ValueError('spatial-average result requires a region identity')
