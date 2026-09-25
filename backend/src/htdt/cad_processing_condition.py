"""Content-conditioned HDR processing authority (#1040).

Records the exact dynamic-metadata / content-intelligence /
ambient-adaptive / motion-processing state that can change display
behavior scene-to-scene or with room light — without ever decoding
proprietary creative metadata or predicting the proprietary
tone-mapping engine's output.

Authority:

- :class:`VideoProcessingCondition` — one exact active/observed
  processing condition bound to a display spec + signal path +
  presentation state.

Honesty rules:

- **Capability ≠ active condition**: ``supports_*`` flags describe what
  the device can do; ``*_presence``/``*_state`` fields describe what was
  observed active. A capability claim can never stand in for an active
  state — missing readback stays ``unknown``.
- Dynamic metadata is identified by family/version/presence only —
  never copied, never inferred from a logo.
- ``ambient_adaptive_state='active'`` requires measured ambient
  evidence (sensor readback or a bound observation) — an "adaptive"
  checkbox is not the actual ambient value.
- Motion metadata requests and the device's motion-processing mode are
  separate fields — motion quality is never inferred from either alone.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance


def _hash(payload) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


DynamicMetadataFamily = Literal[
    'hdr10_plus',
    'hdr10_plus_advanced',
    'dolby_vision',
    'dolby_vision_2',
    'sl_hdr',
    'other',
    'none',
    'unknown',
]
MetadataPresence = Literal[
    'observed', 'declared', 'absent', 'unknown'
]
"""How metadata presence is known: ``observed`` = readback/wire evidence,
``declared`` = content/library claim, ``unknown`` = unread."""

AdaptiveState = Literal['off', 'enabled', 'active', 'unknown']
ProcessingEvidenceClass = Literal[
    'capability_documented', 'observed_readback', 'inferred', 'unknown'
]


class VideoProcessingCondition(BaseModel):
    """Exact active display-processing condition (#1040 §1-6)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['video-processing-condition-1'] = (
        'video-processing-condition-1'
    )
    condition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    display_instance_id: str | None = None
    display_specification_id: str | None = None
    display_specification_version: str | None = None
    display_specification_sha256: str | None = None
    firmware: str | None = None
    signal_path_id: str | None = None
    signal_path_version: str | None = None
    signal_path_sha256: str | None = None
    presentation_profile_id: str | None = None
    picture_mode: str | None = None
    hdr_format_family: str | None = None
    hdr_profile: str | None = None
    # Device capabilities — never proof of the active condition.
    supports_dynamic_metadata: bool | None = None
    supports_ambient_sensing: bool | None = None
    supports_motion_processing: bool | None = None
    # Active/observed state.
    dynamic_metadata_family: DynamicMetadataFamily = 'unknown'
    dynamic_metadata_version: str | None = None
    dynamic_metadata_presence: MetadataPresence = 'unknown'
    metadata_source_identity: str | None = None
    source_processing_state: str | None = None
    display_tone_map_mode: str | None = None
    ambient_adaptive_state: AdaptiveState = 'unknown'
    ambient_sensor_readback_lux: float | None = Field(
        default=None, ge=0.0
    )
    ambient_observation_id: str | None = None
    motion_metadata_request: str | None = None
    motion_processing_mode: str | None = None
    evidence_class: ProcessingEvidenceClass = 'unknown'
    limitations: tuple[str, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    condition_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'condition_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'VideoProcessingCondition':
        for triple, name in (
            (
                (
                    self.display_specification_id,
                    self.display_specification_version,
                    self.display_specification_sha256,
                ),
                'display specification',
            ),
            (
                (
                    self.signal_path_id,
                    self.signal_path_version,
                    self.signal_path_sha256,
                ),
                'signal path',
            ),
        ):
            if (None in triple) and any(v is not None for v in triple):
                raise ValueError(
                    f'{name} id/version/sha256 must be supplied together '
                    'or not at all'
                )
        if (
            self.dynamic_metadata_presence == 'observed'
            and self.dynamic_metadata_family in ('unknown', 'none')
        ):
            raise ValueError(
                'observed metadata presence requires a real metadata '
                'family — presence is never inferred from capability'
            )
        if (
            self.evidence_class == 'capability_documented'
            and self.dynamic_metadata_presence == 'observed'
        ):
            raise ValueError(
                'capability-documented evidence cannot claim observed '
                'metadata presence'
            )
        if self.ambient_adaptive_state == 'active' and (
            self.ambient_sensor_readback_lux is None
            and self.ambient_observation_id is None
        ):
            raise ValueError(
                "ambient_adaptive_state 'active' requires measured "
                'ambient evidence (sensor readback or bound '
                'observation) — an adaptive checkbox is not a value'
            )
        expected = _hash(self.semantic_payload())
        if expected != self.condition_sha256:
            raise ValueError('video processing condition hash mismatch')
        return self


def build_video_processing_condition(**kwargs) -> VideoProcessingCondition:
    probe = VideoProcessingCondition.model_construct(
        condition_sha256='x' * 64, **kwargs
    )
    digest = _hash(probe.semantic_payload())
    return VideoProcessingCondition(
        **probe.model_dump(
            mode='python', exclude={'condition_sha256', 'schema_version'}
        ),
        condition_sha256=digest,
    )
