"""Simultaneous multi-receiver acquisition authority (#1046).

REW 5.40-style multi-input capture is not "one louder measurement": one
acquisition event records several physically distinct receivers, each
with its own instrument, calibration, pose, gain state and quality — and
derived averages (RMS / magnitude / vector / weighted) are *new derived
evidence* that never masquerade as an additional physical receiver.

- ``MultiReceiverChannel`` — per-input bindings; a clipped/failed input
  does not poison sibling channels;
- ``MultiReceiverAcquisition`` — sealed acquisition proving the shared
  capture event. ``simultaneous_capture`` and ``common_timing`` are
  distinct claims: simultaneous capture does not automatically grant
  absolute common timing suitable for time-of-flight;
- ``DerivedReceiverAverage`` — sealed derived aggregate with explicit
  method/alignment semantics, never binding a physical receiver pose;
- ``map_acquisition_to_plan_cells`` — one capture can satisfy several
  #529 plan cells, per channel, with independent retake state.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import Direction3, Position3
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload






_SHA256_PATTERN = r'^[0-9a-f]{64}$'


TimingCapability = Literal[
    'simultaneous_capture',
    'common_clock',
    'timing_reference_bound',
    'relative_timing_preserved',
    'correlation_aligned_derived',
    'unknown',
]
"""Distinct claims, strongest to weakest: ``correlation_aligned_derived``
is a post-hoc derived alignment and never upgrades to a real common
timing reference."""

ChannelQualityState = Literal[
    'valid', 'clipped', 'noisy', 'failed', 'unknown'
]

AverageMethod = Literal['rms', 'magnitude', 'vector', 'weighted', 'unknown']

AverageAlignment = Literal[
    'none', 'level_aligned', 'correlation_aligned', 'unknown'
]


class MultiReceiverChannel(BaseModel):
    """Per-input binding: instrument, calibration, pose, gain, quality."""

    model_config = ConfigDict(frozen=True)

    channel_id: str = Field(min_length=1)
    channel_index: int = Field(ge=0)
    microphone_ref: str | None = None
    input_device: str | None = None
    input_channel_label: str | None = None
    response_calibration_ref: str | None = None
    spl_calibration_ref: str | None = None
    receiver_position: Position3 | None = None
    receiver_target_id: str | None = None
    receiver_pose_unknown: bool = False
    microphone_direction: Direction3 | None = None
    gain_state_db: float | None = None
    raw_response_ref: str | None = None
    raw_response_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    quality_state: ChannelQualityState = 'unknown'
    quality_reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_channel(self) -> 'MultiReceiverChannel':
        if self.receiver_position is None and not self.receiver_pose_unknown:
            raise ValueError(
                'channel requires an exact receiver_position or explicit '
                'receiver_pose_unknown — position is never inferred from '
                'input-channel order'
            )
        if self.receiver_position is not None and self.receiver_pose_unknown:
            raise ValueError(
                'receiver_pose_unknown is incompatible with an exact position'
            )
        if self.gain_state_db is not None and not isfinite(float(self.gain_state_db)):
            raise ValueError('gain_state_db must be finite')
        if self.quality_state == 'valid' and self.receiver_position is None:
            raise ValueError(
                'a valid channel requires exact pose evidence'
            )
        return self


class MultiReceiverAcquisition(BaseModel):
    """Sealed simultaneous-capture evidence across N receiver channels."""

    model_config = ConfigDict(frozen=True)

    acquisition_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    system_variant_id: str | None = None
    source_ref: str | None = None
    routing_profile_ref: str | None = None
    stimulus_profile_id: str | None = None
    stimulus_profile_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    room_operating_state_ref: str | None = None
    producer: str = Field(min_length=1)
    acquisition_session_id: str | None = None
    producer_version: str = Field(min_length=1)
    sample_rate_hz: float = Field(gt=0.0)
    capture_mode: Literal['simultaneous', 'sequential', 'unknown'] = 'simultaneous'
    timing_capabilities: tuple[TimingCapability, ...] = ()
    timing_reference_id: str | None = None
    timing_reference_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    channels: tuple[MultiReceiverChannel, ...] = Field(min_length=2)
    raw_asset_refs: tuple[str, ...] = ()
    observed_at_utc: str = Field(min_length=1)
    quality_json: str = '{}'
    provenance_json: str = '{}'
    acquisition_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_acquisition(self) -> 'MultiReceiverAcquisition':
        ids = [channel.channel_id for channel in self.channels]
        if len(ids) != len(set(ids)):
            raise ValueError('channel ids must be unique')
        indices = [channel.channel_index for channel in self.channels]
        if len(indices) != len(set(indices)):
            raise ValueError('channel indices must be unique')
        caps = set(self.timing_capabilities)
        if self.capture_mode != 'simultaneous' and 'simultaneous_capture' in caps:
            raise ValueError(
                'simultaneous_capture capability requires simultaneous mode'
            )
        if 'timing_reference_bound' in caps and self.timing_reference_id is None:
            raise ValueError(
                'timing_reference_bound requires a bound timing reference '
                '(#642) — simultaneous capture alone does not grant it'
            )
        if 'correlation_aligned_derived' in caps and 'simultaneous_capture' not in caps:
            raise ValueError(
                'correlation alignment is derived alignment of a '
                'simultaneous capture, not a timing source'
            )
        if self.acquisition_sha256 != _hash(self.identity_payload()):
            raise ValueError('multi-receiver acquisition hash mismatch')
        return self

    def channel(self, channel_id: str) -> MultiReceiverChannel | None:
        for channel in self.channels:
            if channel.channel_id == channel_id:
                return channel
        return None

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'acquisition_sha256'})


class DerivedReceiverAverage(BaseModel):
    """Sealed derived average over an acquisition's member channels.

    The result is derived evidence: it carries no physical receiver pose
    and can never be consumed as another measured receiver.
    """

    model_config = ConfigDict(frozen=True)

    average_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    acquisition_id: str = Field(min_length=1)
    acquisition_sha256: str = Field(pattern=_SHA256_PATTERN)
    member_channel_ids: tuple[str, ...] = Field(min_length=1)
    method: AverageMethod = 'unknown'
    alignment: AverageAlignment = 'unknown'
    weights: tuple[float, ...] | None = None
    derived_dataset_ref: str | None = None
    derived_dataset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    region_ref: str | None = None
    receiver_position: Position3 | None = None
    created_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    average_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_average(self) -> 'DerivedReceiverAverage':
        if len(self.member_channel_ids) != len(set(self.member_channel_ids)):
            raise ValueError('member channel ids must be unique')
        if self.method == 'unknown':
            raise ValueError('derived average requires an explicit method')
        if self.receiver_position is not None:
            raise ValueError(
                'a derived average is not a physical receiver — it cannot '
                'carry a receiver position'
            )
        if self.method == 'weighted':
            if self.weights is None or len(self.weights) != len(self.member_channel_ids):
                raise ValueError('weighted average requires one weight per member')
            if any(not isfinite(float(w)) for w in self.weights):
                raise ValueError('weights must be finite')
        elif self.weights is not None:
            raise ValueError('weights are only valid for the weighted method')
        if self.average_sha256 != _hash(self.identity_payload()):
            raise ValueError('derived average hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'average_sha256'})


def build_multi_receiver_acquisition(**kwargs: Any) -> MultiReceiverAcquisition:
    """Assemble and seal a :class:`MultiReceiverAcquisition`."""
    payload = {'acquisition_sha256': '0' * 64, **kwargs}
    provisional = MultiReceiverAcquisition.model_construct(**canonicalize_payload(MultiReceiverAcquisition, dict(**payload)))
    payload['acquisition_sha256'] = _hash(provisional.identity_payload())
    return MultiReceiverAcquisition(**payload)


def build_derived_receiver_average(
    acquisition: MultiReceiverAcquisition,
    **kwargs: Any,
) -> DerivedReceiverAverage:
    """Assemble and seal a :class:`DerivedReceiverAverage` bound to an
    acquisition; member channels must exist on it and a failed/clipped
    member is preserved but flagged by its own quality state."""
    member_ids = kwargs.get('member_channel_ids', ())
    known = {channel.channel_id for channel in acquisition.channels}
    for member_id in member_ids:
        if member_id not in known:
            raise ValueError(
                f'average member {member_id!r} is not a channel of the '
                'bound acquisition'
            )
    payload = {
        'average_sha256': '0' * 64,
        'acquisition_id': acquisition.acquisition_id,
        'acquisition_sha256': acquisition.acquisition_sha256,
        'document_id': acquisition.document_id,
        **kwargs,
    }
    provisional = DerivedReceiverAverage.model_construct(**canonicalize_payload(DerivedReceiverAverage, dict(**payload)))
    payload['average_sha256'] = _hash(provisional.identity_payload())
    return DerivedReceiverAverage(**payload)


def map_acquisition_to_plan_cells(
    acquisition: MultiReceiverAcquisition,
    *,
    channel_cell_map: dict[str, str],
) -> dict[str, str]:
    """Map one capture onto #529 plan cells per channel.

    Returns ``cell_id → channel_id`` for channels that both exist and are
    not ``failed``; a failed channel yields no cell entry (its retake
    state is the cell's own concern) and other channels stay unaffected.
    """
    mapped: dict[str, str] = {}
    for channel_id, cell_id in channel_cell_map.items():
        channel = acquisition.channel(channel_id)
        if channel is None:
            raise ValueError(f'cell map references unknown channel {channel_id!r}')
        if channel.quality_state == 'failed':
            continue
        mapped[cell_id] = channel_id
    return mapped
