"""Spatial room impulse response measurement authority (#974).

A mono measured IR (#474/#511) shows *that* an early arrival exists at
8 ms but not *from which direction*. Spatial RIR capture (SRIR/DRIR) from
a calibrated microphone array preserves directional evidence for
reflection-path validation, source localization and spatial auralization.

This module owns the measurement side — the reproduction side (HRTF/BRIR
assets for auralization) stays with #809:

- ``SpatialMeasurementArrayProfile`` — immutable receiver-array authority
  (kind, per-receiver local positions/orientations, channel mapping,
  handedness, calibration); a product name alone is never geometry;
- ``ArrayPose`` — exact position + orientation of the array in scene
  coordinates with provenance and uncertainty (orientation is never
  inferred from filename or channel order);
- ``SpatialRoomImpulseResponseDataset`` — sealed dataset binding one
  acquisition, the exact array profile+pose, per-channel IR refs and SOFA
  import provenance (``SingleRoomSRIR``/``SingleRoomMIMOSRIR``), with raw
  channels preserved before any directional inference;
- ``DirectionalReflectionEvent`` — estimator-labelled directional early
  arrivals that may be *associated* with predicted paths but never
  silently equated to a physical wall.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import Direction3, Position3
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash






_SHA256_PATTERN = r'^[0-9a-f]{64}$'


ArrayKind = Literal[
    'ambisonic_foa',
    'ambisonic_hoa',
    'spherical',
    'compact_pressure',
    'binaural',
    'custom',
    'unknown',
]

CoordinateHandedness = Literal['right_handed', 'left_handed', 'unknown']

SofaConvention = Literal[
    'SingleRoomSRIR', 'SingleRoomMIMOSRIR', 'other', 'none', 'unknown'
]

PoseProvenance = Literal[
    'surveyed', 'tracked', 'captured_annotated', 'imported', 'manual', 'unknown'
]

DirectionEstimatorKind = Literal[
    'sdm', 'spherical_harmonic', 'beamforming', 'producer', 'imported', 'unknown'
]

PathAssociationState = Literal[
    'associated', 'unassociated', 'ambiguous', 'not_attempted', 'unknown'
]


def _require_finite_seq(values: tuple[float, ...], label: str) -> None:
    if any(not isfinite(float(value)) for value in values):
        raise ValueError(f'{label} must be finite')


class SpatialMeasurementArrayProfile(BaseModel):
    """Immutable receiver-array authority for spatial capture."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    manufacturer: str | None = None
    model: str | None = None
    array_kind: ArrayKind = 'unknown'
    receiver_positions: tuple[Position3, ...] = Field(min_length=1)
    receiver_orientations: tuple[Direction3, ...] | None = None
    channel_mapping: tuple[str, ...] = ()
    handedness: CoordinateHandedness = 'unknown'
    sample_rate_hz: float | None = Field(default=None, gt=0.0)
    calibration_refs: tuple[str, ...] = ()
    per_channel_gain_calibration: tuple[float, ...] | None = None
    per_channel_phase_calibration: tuple[float, ...] | None = None
    geometry_uncertainty_m: float | None = Field(default=None, ge=0.0)
    calibration_uncertainty_json: str = '{}'
    provenance_json: str = '{}'
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'SpatialMeasurementArrayProfile':
        if self.array_kind == 'unknown':
            raise ValueError('array kind must be explicit — a product name is not geometry')
        if self.handedness == 'unknown':
            raise ValueError('coordinate handedness must be declared')
        if self.receiver_orientations is not None and len(
            self.receiver_orientations
        ) != len(self.receiver_positions):
            raise ValueError('receiver_orientations must align with receiver_positions')
        if self.channel_mapping and len(self.channel_mapping) != len(
            self.receiver_positions
        ):
            raise ValueError('channel_mapping must align with receiver_positions')
        for label, seq in (
            ('per_channel_gain_calibration', self.per_channel_gain_calibration),
            ('per_channel_phase_calibration', self.per_channel_phase_calibration),
        ):
            if seq is not None:
                if len(seq) != len(self.receiver_positions):
                    raise ValueError(f'{label} must align with receiver_positions')
                _require_finite_seq(tuple(seq), label)
        if self.profile_sha256 != _hash(self.identity_payload()):
            raise ValueError('array profile hash mismatch')
        return self

    @property
    def receiver_count(self) -> int:
        return len(self.receiver_positions)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'profile_sha256'})


class ArrayPose(BaseModel):
    """Exact array pose in scene coordinates with provenance."""

    model_config = ConfigDict(frozen=True)

    position: Position3
    orientation_xyz_deg: tuple[float, float, float] | None = None
    frame_semantics: str | None = None
    provenance: PoseProvenance = 'unknown'
    position_uncertainty_m: float | None = Field(default=None, ge=0.0)
    orientation_uncertainty_deg: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def valid_pose(self) -> 'ArrayPose':
        if self.orientation_xyz_deg is not None:
            _require_finite_seq(self.orientation_xyz_deg, 'orientation_xyz_deg')
        for value in (
            self.position_uncertainty_m,
            self.orientation_uncertainty_deg,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('pose uncertainty must be finite')
        return self


class SpatialIrChannelBinding(BaseModel):
    """One receiver channel → one raw IR dataset; raw channels are
    preserved before any directional inference."""

    model_config = ConfigDict(frozen=True)

    channel_index: int = Field(ge=0)
    ir_dataset_id: str = Field(min_length=1)
    ir_dataset_sha256: str = Field(pattern=_SHA256_PATTERN)


class SpatialRoomImpulseResponseDataset(BaseModel):
    """Sealed spatial RIR dataset for one acquisition + exact array pose."""

    model_config = ConfigDict(frozen=True)

    dataset_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    acquisition_ref: str | None = None
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    array_profile_id: str = Field(min_length=1)
    array_profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    array_pose: ArrayPose
    channels: tuple[SpatialIrChannelBinding, ...] = Field(min_length=1)
    sample_rate_hz: float = Field(gt=0.0)
    sofa_convention: SofaConvention = 'none'
    sofa_source_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    sofa_producer: str | None = None
    raw_channels_preserved: bool = True
    created_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    dataset_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_dataset(self) -> 'SpatialRoomImpulseResponseDataset':
        if self.array_pose.provenance == 'unknown':
            raise ValueError(
                'array pose provenance is required — orientation is never '
                'inferred from filename or channel order'
            )
        indices = [binding.channel_index for binding in self.channels]
        if len(indices) != len(set(indices)):
            raise ValueError('channel indices must be unique')
        if self.sofa_convention != 'none' and self.sofa_producer is None:
            raise ValueError(
                'SOFA-imported datasets must keep the producer identity — '
                'imported evidence is never "HTDT measured"'
            )
        if self.dataset_sha256 != _hash(self.identity_payload()):
            raise ValueError('spatial IR dataset hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'dataset_sha256'})


class DirectionalReflectionEvent(BaseModel):
    """One estimator-labelled directional arrival inside a spatial IR.

    An association to a predicted path is evidence-grade only: the event
    keeps its own estimator identity and confidence, and an unassociated
    event is preserved rather than force-attributed to a wall.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=_SHA256_PATTERN)
    estimator: DirectionEstimatorKind = 'unknown'
    estimator_version: str = Field(min_length=1)
    delay_s: float = Field(ge=0.0)
    direction: Direction3
    level_db: float | None = None
    confidence: float | None = None
    associated_path_id: str | None = None
    association_state: PathAssociationState = 'not_attempted'

    @model_validator(mode='after')
    def valid_event(self) -> 'DirectionalReflectionEvent':
        if self.estimator == 'unknown':
            raise ValueError('directional event requires an explicit estimator')
        if not isfinite(float(self.delay_s)):
            raise ValueError('delay_s must be finite')
        if self.level_db is not None and not isfinite(float(self.level_db)):
            raise ValueError('level_db must be finite')
        if self.confidence is not None:
            if not isfinite(float(self.confidence)) or not (0.0 <= self.confidence <= 1.0):
                raise ValueError('confidence must lie in [0, 1]')
        if self.associated_path_id is not None and self.association_state in (
            'unassociated',
            'not_attempted',
        ):
            raise ValueError('association state must agree with associated_path_id')
        if self.association_state == 'associated' and self.associated_path_id is None:
            raise ValueError('associated state requires associated_path_id')
        return self


def build_array_profile(**kwargs: Any) -> SpatialMeasurementArrayProfile:
    """Assemble and seal a :class:`SpatialMeasurementArrayProfile`."""
    payload = {'profile_sha256': '0' * 64, **kwargs}
    provisional = SpatialMeasurementArrayProfile.model_construct(**payload)
    payload['profile_sha256'] = _hash(provisional.identity_payload())
    return SpatialMeasurementArrayProfile(**payload)


def build_spatial_ir_dataset(**kwargs: Any) -> SpatialRoomImpulseResponseDataset:
    """Assemble and seal a :class:`SpatialRoomImpulseResponseDataset`."""
    payload = {'dataset_sha256': '0' * 64, **kwargs}
    provisional = SpatialRoomImpulseResponseDataset.model_construct(**payload)
    payload['dataset_sha256'] = _hash(provisional.identity_payload())
    return SpatialRoomImpulseResponseDataset(**payload)


def validate_spatial_dataset_geometry(
    dataset: SpatialRoomImpulseResponseDataset,
    profile: SpatialMeasurementArrayProfile,
) -> None:
    """Fail-closed geometry check: channel count, profile hash, pose.

    Raises ``ValueError`` whenever the dataset's channel bindings do not
    match the referenced array geometry or the profile hash does not pin
    the exact array authority the dataset claims.
    """
    if dataset.array_profile_id != profile.profile_id:
        raise ValueError('dataset array_profile_id does not match profile')
    if dataset.array_profile_sha256 != profile.profile_sha256:
        raise ValueError('dataset array profile hash mismatch')
    if len(dataset.channels) != profile.receiver_count:
        raise ValueError(
            'channel bindings must cover every declared receiver element'
        )
    if not all(binding.ir_dataset_id for binding in dataset.channels):
        raise ValueError('channels must bind raw IR datasets')
